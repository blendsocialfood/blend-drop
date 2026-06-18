import os
import hmac
import hashlib
import time
import sqlite3
import uuid
import json
import requests as http_requests
from datetime import datetime, timedelta
from flask import Flask, request, redirect, session, jsonify, send_from_directory, send_file, Response
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
import atexit

# Cargar .env local en desarrollo (en Railway las vars vienen del entorno)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Pillow es opcional: si está, validamos specs de historia (9:16)
try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'blend-drop-secret-2026')

UNITY_URL = os.environ.get('UNITY_URL', 'https://positive-appreciation-production.up.railway.app')
OS_URL = os.environ.get('OS_URL', 'https://socialfood-os-production.up.railway.app')
AUTH_SECRET = 'blendsf-auth-2026'

DB_PATH = os.environ.get('DB_PATH', '/data/blend_drop.db')
os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
META_TOKEN = os.environ.get('META_TOKEN', '')          # System User "Drop"
META_TOKEN_NICO = os.environ.get('META_TOKEN_NICO', '') # Token personal Nico
META_BM_ID = os.environ.get('META_BM_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DROP_URL = os.environ.get('DROP_URL', 'https://blend-drop-production.up.railway.app')
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')   # opcional: recordatorios de historias asistidas
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')       # opcional
GRAPH = 'https://graph.facebook.com/v21.0'
MEDIA_DIR = os.environ.get('MEDIA_DIR', '/tmp/drop_media')
os.makedirs(MEDIA_DIR, exist_ok=True)

# Rate limit de la Content Publishing API: 25 posts / 24h por cuenta IG (reels+stories+feed comparten bucket)
IG_DAILY_LIMIT = 25


def get_meta_token_for_client(client_id):
    """Retorna el token Meta correcto según el cliente.
    Primero revisa token_key en BD, luego fallback a mapeo hardcodeado."""
    NICO_CLIENTS = {2, 4}  # Buona Pizza, Fuente Mardoqueo
    conn = get_db()
    row = conn.execute("SELECT token_key FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    if row and row['token_key']:
        key = row['token_key']
    else:
        key = 'nico' if int(client_id) in NICO_CLIENTS else 'system'
    return META_TOKEN_NICO if key == 'nico' else META_TOKEN

# ── Auth ──

def generate_token(username, role):
    ts = str(int(time.time()))
    msg = f"{username}:{role}:{ts}"
    sig = hmac.new(AUTH_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{username}:{role}:{ts}:{sig}"

def verify_token(token, max_age=14400):
    try:
        parts = token.split(':')
        if len(parts) != 4: return None
        username, role, ts, sig = parts
        expected = hmac.new(AUTH_SECRET.encode(), f"{username}:{role}:{ts}".encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, expected): return None
        if time.time() - int(ts) > max_age: return None
        return {'username': username, 'role': role}
    except Exception:
        return None

def require_auth():
    """True si la sesión está autenticada. En modo DEV_NO_AUTH permite todo (solo local)."""
    if os.environ.get('DEV_NO_AUTH') == '1':
        if 'user' not in session:
            session['user'] = 'dev'
            session['role'] = 'admin'
        return True
    return 'user' in session

# ── BD ──

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _safe_migrate(conn, sql):
    try:
        conn.execute(sql)
        conn.commit()
    except Exception:
        pass

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pieza_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            ig_user_id TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            kind TEXT DEFAULT 'feed',          -- feed | story
            mode TEXT DEFAULT 'auto',          -- auto (API) | assisted (recordatorio manual)
            sticker TEXT DEFAULT 'no_usa',
            copy_text TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',     -- pending|processing|published|failed|reminded|done
            ig_container_id TEXT,
            ig_post_id TEXT,
            error_msg TEXT,
            reminded_at TEXT,
            published_at TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_ig_accounts (
            client_id INTEGER PRIMARY KEY,
            client_name TEXT,
            ig_user_id TEXT NOT NULL,
            ig_username TEXT,
            token_key TEXT DEFAULT 'system',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    # Migraciones idempotentes (para BDs viejas del Drop v2)
    for ddl in [
        "ALTER TABLE client_ig_accounts ADD COLUMN token_key TEXT DEFAULT 'system'",
        "ALTER TABLE scheduled_posts ADD COLUMN kind TEXT DEFAULT 'feed'",
        "ALTER TABLE scheduled_posts ADD COLUMN mode TEXT DEFAULT 'auto'",
        "ALTER TABLE scheduled_posts ADD COLUMN sticker TEXT DEFAULT 'no_usa'",
        "ALTER TABLE scheduled_posts ADD COLUMN copy_text TEXT DEFAULT ''",
        "ALTER TABLE scheduled_posts ADD COLUMN ig_post_id TEXT",
        "ALTER TABLE scheduled_posts ADD COLUMN reminded_at TEXT",
        "ALTER TABLE scheduled_posts ADD COLUMN published_at TEXT",
    ]:
        _safe_migrate(conn, ddl)
    conn.close()
    print("[DROP] init_db OK")

init_db()

# ── Helpers de pieza ──

def fetch_pieza(pieza_id, token):
    try:
        r = http_requests.get(f'{UNITY_URL}/api/pieza-detail/{pieza_id}',
            params={'token': token}, timeout=10)
        return r.json() if r.ok else None
    except Exception:
        return None

def is_story(pieza):
    f = (pieza.get('formato') or '').lower()
    t = (pieza.get('tipo_pedido') or '').lower()
    return ('histor' in f) or ('stor' in f) or t in ('historia', 'historias', 'story', 'stories')

def story_mode(sticker):
    """Historias planas (sin sticker interactivo) → auto-publish por API.
    Las que llevan sticker → flujo asistido (la API oficial no soporta stickers)."""
    return 'auto' if (sticker or 'no_usa') == 'no_usa' else 'assisted'

# ── Media pública + validación ──

@app.route('/media/<filename>')
def serve_media(filename):
    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(filepath):
        return 'Not found', 404
    return send_file(filepath)

@app.route('/api/download/<file_id>')
def api_download_file(file_id):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/drive/file/{file_id}',
            params={'token': token}, timeout=30, stream=True)
        if not r.ok:
            return jsonify({'error': 'No se pudo descargar el archivo'}), 502
        content_type = r.headers.get('Content-Type', 'application/octet-stream')
        import re as _re
        cd_orig = r.headers.get('Content-Disposition', '')
        fname_match = _re.search(r'filename="([^"]+)"', cd_orig)
        fname = fname_match.group(1) if fname_match else file_id
        return Response(r.iter_content(chunk_size=8192),
            content_type=content_type,
            headers={'Content-Disposition': f'attachment; filename="{fname}"'})
    except Exception as e:
        return jsonify({'error': str(e)}), 502

def download_drive_file(drive_file_id, token):
    """Descarga archivo de Drive vía Unity proxy y lo guarda en MEDIA_DIR. Retorna filename o None."""
    try:
        r = http_requests.get(f'{UNITY_URL}/api/drive/file/{drive_file_id}',
            params={'token': token}, timeout=30, stream=True)
        if not r.ok:
            return None
        ext = 'jpg'
        ct = r.headers.get('content-type', '')
        if 'mp4' in ct or 'video' in ct:
            ext = 'mp4'
        elif 'png' in ct:
            ext = 'png'
        filename = f"{uuid.uuid4().hex}.{ext}"
        with open(os.path.join(MEDIA_DIR, filename), 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return filename
    except Exception as e:
        print(f"[DROP] Error descargando Drive: {e}")
        return None

def prepare_media_for_publish(filename, kind):
    """La Content Publishing API solo acepta JPEG en imágenes (rechaza PNG/WebP/GIF) y máx 8MB.
    Transcodea a JPEG sRGB, achica si excede el lado largo y recomprime bajo 8MB.
    Retorna (filename_final, warnings). No toca videos."""
    if filename.endswith('.mp4') or not HAS_PIL:
        return filename, []
    warnings = []
    src = os.path.join(MEDIA_DIR, filename)
    try:
        with Image.open(src) as im:
            fmt = im.format
            im = im.convert('RGB')
            w, h = im.size
            max_side = 1920
            if max(w, h) > max_side:
                scale = max_side / max(w, h)
                im = im.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
            out = filename.rsplit('.', 1)[0] + '.jpg'
            out_path = os.path.join(MEDIA_DIR, out)
            q = 90
            im.save(out_path, 'JPEG', quality=q, optimize=True)
            while os.path.getsize(out_path) > 8 * 1024 * 1024 and q > 60:
                q -= 10
                im.save(out_path, 'JPEG', quality=q, optimize=True)
            if out != filename:
                if fmt not in ('JPEG', 'MPO'):
                    warnings.append(f'Imagen {fmt} convertida a JPEG para la API')
                try:
                    os.remove(src)
                except Exception:
                    pass
            return out, warnings
    except Exception as e:
        return filename, [f'No se pudo preparar la imagen: {e}']


def ig_publishing_limit(ig_user_id, meta_token):
    """Lee en runtime el cupo de publicación de la cuenta (50/24h, ventana móvil).
    Retorna {'usage','total','remaining'} o None si no se pudo."""
    try:
        r = http_requests.get(f'{GRAPH}/{ig_user_id}/content_publishing_limit',
            params={'fields': 'config,quota_usage', 'access_token': meta_token}, timeout=15)
        data = (r.json().get('data') or [{}])[0]
        usage = data.get('quota_usage', 0) or 0
        total = (data.get('config') or {}).get('quota_total', 50) or 50
        return {'usage': usage, 'total': total, 'remaining': max(0, total - usage)}
    except Exception:
        return None


def validate_story_media(filename):
    """Valida que una imagen de historia sea apta (≈9:16, ancho ≥1080, JPEG).
    Retorna {'ok': bool, 'warnings': [...], 'ratio': float|None}."""
    warnings = []
    if filename.endswith('.mp4'):
        return {'ok': True, 'warnings': ['Video: la validación de specs se omite (revisa 9:16 y <60s a mano)'], 'ratio': None}
    if not HAS_PIL:
        return {'ok': True, 'warnings': ['Pillow no instalado: no se validaron dimensiones'], 'ratio': None}
    try:
        with Image.open(os.path.join(MEDIA_DIR, filename)) as im:
            w, h = im.size
            ratio = w / h if h else 0
            target = 9 / 16  # 0.5625
            if abs(ratio - target) > 0.06:
                warnings.append(f'Proporción {w}x{h} (ratio {ratio:.3f}) no es 9:16 — Instagram puede recortar la historia')
            if w < 1080:
                warnings.append(f'Ancho {w}px < 1080px recomendado — puede verse pixelada')
            if im.format not in ('JPEG', 'MPO'):
                warnings.append(f'Formato {im.format}: la API solo publica JPEG en historias (el Drop la convierte sola al publicar)')
            try:
                size_mb = os.path.getsize(os.path.join(MEDIA_DIR, filename)) / (1024 * 1024)
                if size_mb > 8:
                    warnings.append(f'Pesa {size_mb:.1f}MB (máx 8MB de la API) — el Drop la recomprime al publicar')
            except Exception:
                pass
            return {'ok': True, 'warnings': warnings, 'ratio': ratio, 'w': w, 'h': h}
    except Exception as e:
        return {'ok': True, 'warnings': [f'No se pudo validar la imagen: {e}'], 'ratio': None}

# ── Rate limit ──

def count_published_24h(ig_user_id):
    """Cuántas publicaciones hizo el Drop a esta cuenta en las últimas 24h (límite 25/día)."""
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    conn = get_db()
    n = conn.execute("""
        SELECT COUNT(*) c FROM scheduled_posts
        WHERE ig_user_id=? AND status='published' AND published_at >= ?
    """, (ig_user_id, since)).fetchone()['c']
    conn.close()
    return n

# ── Meta API publish ──

def publish_to_meta(ig_user_id, media_url, kind, is_video, caption, meta_token):
    """Crea container Meta y publica. kind: feed|story. Retorna dict con ok/ig_post_id/container_id/error."""
    try:
        if kind == 'story':
            params = {'access_token': meta_token, 'media_type': 'STORIES'}
            if is_video:
                params['video_url'] = media_url
            else:
                params['image_url'] = media_url
        elif is_video:  # feed reel
            params = {'access_token': meta_token, 'caption': caption,
                      'media_type': 'REELS', 'video_url': media_url, 'share_to_feed': 'true'}
        else:  # feed imagen
            params = {'access_token': meta_token, 'caption': caption, 'image_url': media_url}

        r = http_requests.post(f'{GRAPH}/{ig_user_id}/media', params=params, timeout=60)
        cdata = r.json()
        container_id = cdata.get('id')
        if not container_id:
            return {'ok': False, 'error': f'Meta container error: {cdata}'}

        # Esperar a que el container esté listo (videos y a veces stories tardan en procesarse)
        if is_video or kind == 'story':
            for _ in range(18):
                time.sleep(5)
                st = http_requests.get(f'{GRAPH}/{container_id}',
                    params={'fields': 'status_code', 'access_token': meta_token}, timeout=20).json()
                code = st.get('status_code')
                if code == 'FINISHED':
                    break
                if code == 'ERROR':
                    return {'ok': False, 'error': f'Container procesó con ERROR: {st}'}

        pub = http_requests.post(f'{GRAPH}/{ig_user_id}/media_publish',
            params={'creation_id': container_id, 'access_token': meta_token}, timeout=30).json()
        if pub.get('id'):
            # Verificar con media_product_type (media_type "miente": devuelve IMAGE/VIDEO)
            ptype = None
            try:
                ptype = http_requests.get(f"{GRAPH}/{pub['id']}",
                    params={'fields': 'media_product_type', 'access_token': meta_token}, timeout=15).json().get('media_product_type')
            except Exception:
                pass
            return {'ok': True, 'ig_post_id': pub['id'], 'container_id': container_id, 'product_type': ptype}
        return {'ok': False, 'error': f'Meta publish error: {pub}', 'container_id': container_id}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def publish_piece(pieza_id, ig_user_id, copy_text, token, meta_token=None, kind=None):
    """Descarga la pieza de Unity, sube media, publica. Retorna {'ok':..}."""
    if not meta_token:
        meta_token = META_TOKEN
    pieza = fetch_pieza(pieza_id, token)
    if not pieza:
        return {'ok': False, 'error': 'No se pudo obtener la pieza desde Unity'}

    drive_file_id = pieza.get('drive_file_id')
    if not drive_file_id:
        return {'ok': False, 'error': 'Sin archivo Drive en la pieza'}
    if kind is None:
        kind = 'story' if is_story(pieza) else 'feed'
    caption = '' if kind == 'story' else (copy_text or pieza.get('copy', ''))

    # Guardia de rate limit: cupo real de Meta en runtime, fallback al conteo local
    limit = ig_publishing_limit(ig_user_id, meta_token)
    if limit and limit['remaining'] <= 0:
        return {'ok': False, 'error': f"Cupo de publicaciones 24h agotado en Meta ({limit['usage']}/{limit['total']})"}
    if not limit and count_published_24h(ig_user_id) >= IG_DAILY_LIMIT:
        return {'ok': False, 'error': f'Límite local de {IG_DAILY_LIMIT} publicaciones/24h alcanzado para esta cuenta'}

    filename = download_drive_file(drive_file_id, token)
    if not filename:
        return {'ok': False, 'error': 'No se pudo descargar archivo de Drive'}

    # La API solo acepta JPEG en imágenes: transcodear PNG/WebP -> JPEG sRGB antes de publicar
    filename, _prep = prepare_media_for_publish(filename, kind)
    media_url = f"{DROP_URL}/media/{filename}"
    is_video = filename.endswith('.mp4')

    result = publish_to_meta(ig_user_id, media_url, kind, is_video, caption, meta_token)

    if result.get('ok'):
        try:
            os.remove(os.path.join(MEDIA_DIR, filename))
        except Exception:
            pass
    return result

# ── Telegram (recordatorios de historias asistidas) ──

def send_telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        http_requests.post(f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage',
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'HTML'}, timeout=10)
        return True
    except Exception:
        return False

# ── Cron ──

def cron_tick():
    """Cada 5 min. Auto-publica feed y historias planas; dispara recordatorio de historias asistidas."""
    now = datetime.utcnow()
    window_end = now + timedelta(minutes=20)
    conn = get_db()
    posts = conn.execute("""
        SELECT * FROM scheduled_posts
        WHERE status='pending' AND scheduled_at <= ? AND scheduled_at >= ?
        ORDER BY scheduled_at
    """, (window_end.isoformat(), (now - timedelta(hours=6)).isoformat())).fetchall()
    conn.close()

    for post in posts:
        pid = post['id']
        kind = post['kind'] or 'feed'
        mode = post['mode'] or 'auto'

        # Historias asistidas: NO se publican por API. Se notifica a la operadora.
        if kind == 'story' and mode == 'assisted':
            conn = get_db()
            conn.execute("UPDATE scheduled_posts SET status='reminded', reminded_at=? WHERE id=?",
                         (now.isoformat(), pid))
            conn.commit(); conn.close()
            send_telegram(f"📲 <b>Historia para subir a mano</b>\nPieza {post['pieza_id']} · sticker: {post['sticker']}\nÁbrela en Drop, descarga y publícala en Instagram.")
            print(f"[DROP CRON] Historia asistida {pid} → recordatorio")
            continue

        # Feed + historias planas: auto-publish por API
        print(f"[DROP CRON] Publicando {kind} {pid} (pieza {post['pieza_id']})")
        conn = get_db()
        conn.execute("UPDATE scheduled_posts SET status='processing' WHERE id=?", (pid,))
        conn.commit(); conn.close()

        token = generate_token('drop-cron', 'admin')
        meta_tok = get_meta_token_for_client(post['client_id'])
        result = publish_piece(post['pieza_id'], post['ig_user_id'], post['copy_text'] or '',
                               token, meta_token=meta_tok, kind=kind)

        conn = get_db()
        if result.get('ok'):
            conn.execute("""UPDATE scheduled_posts
                SET status='published', ig_container_id=?, ig_post_id=?, published_at=? WHERE id=?""",
                (result.get('container_id', ''), result.get('ig_post_id', ''), now.isoformat(), pid))
            try:
                http_requests.post(f'{UNITY_URL}/api/drop/publicar',
                    json={'token': token, 'pieza_id': post['pieza_id']}, timeout=10)
            except Exception:
                pass
        else:
            conn.execute("UPDATE scheduled_posts SET status='failed', error_msg=? WHERE id=?",
                         (result.get('error', 'Unknown'), pid))
        conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(cron_tick, 'interval', minutes=5, id='cron_tick')
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))
print("[DROP] APScheduler started")

# ── Routes base ──

@app.route('/')
def index():
    if 'user' not in session:
        token = request.args.get('token', '')
        if token:
            user = verify_token(token)
            if user:
                session['user'] = user['username']
                session['role'] = user['role']
                session['token'] = token
                return redirect('/')
        if os.environ.get('DEV_NO_AUTH') == '1':
            session['user'] = 'dev'; session['role'] = 'admin'
            return send_from_directory('.', 'app.html')
        return redirect(OS_URL)
    return send_from_directory('.', 'app.html')

@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'pil': HAS_PIL, 'meta_token': bool(META_TOKEN), 'telegram': bool(TELEGRAM_BOT_TOKEN)})

@app.route('/api/hoy')
def api_hoy():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    fecha = request.args.get('fecha', '')
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar',
            params={'fecha': fecha, 'token': token, 'all': 'true'}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/clientes')
def api_clientes():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/list-clients', params={'token': token}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/mes')
def api_mes():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    mes = request.args.get('mes', '')
    client_id = request.args.get('client_id', '')
    token = generate_token(session['user'], session['role'])
    params = {'mes': mes, 'token': token}
    if client_id:
        params['client_id'] = client_id
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar-mes', params=params, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

# ── Programar / publicar ──

@app.route('/api/schedule', methods=['POST'])
def api_schedule():
    """Programa una pieza. Detecta historia vs feed y, para historias, auto vs asistida según sticker."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    client_id = data.get('client_id')
    scheduled_at = data.get('scheduled_at')
    copy_text = data.get('copy', '')
    if not all([pieza_id, client_id, scheduled_at]):
        return jsonify({'error': 'pieza_id, client_id y scheduled_at requeridos'}), 400

    token = generate_token(session['user'], session['role'])
    pieza = fetch_pieza(pieza_id, token) or {}
    es_hist = is_story(pieza)
    sticker = (pieza.get('stickers') or 'no_usa')
    kind = 'story' if es_hist else 'feed'
    mode = story_mode(sticker) if es_hist else 'auto'

    # Guardar copy en Unity (no bloqueante)
    if copy_text:
        try:
            http_requests.put(f'{UNITY_URL}/api/piezas/{pieza_id}/copy',
                json={'copy': copy_text}, params={'token': token}, timeout=10)
        except Exception:
            pass

    conn = get_db()
    ig_row = conn.execute("SELECT ig_user_id FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    if not ig_row:
        conn.close()
        return jsonify({'error': f'No hay cuenta IG configurada para cliente {client_id}. Configúrala en Ajustes.'}), 400
    conn.execute("""INSERT INTO scheduled_posts
        (pieza_id, client_id, ig_user_id, scheduled_at, kind, mode, sticker, copy_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (pieza_id, client_id, ig_row['ig_user_id'], scheduled_at, kind, mode, sticker, copy_text))
    conn.commit(); conn.close()

    label = {'feed': 'Publicación', 'story': ('Historia auto' if mode == 'auto' else 'Historia (recordatorio)')}[kind]
    return jsonify({'ok': True, 'kind': kind, 'mode': mode, 'scheduled_at': scheduled_at, 'label': label})

@app.route('/api/publicar-ahora', methods=['POST'])
def api_publicar_ahora():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    client_id = data.get('client_id')
    copy_text = data.get('copy', '')
    if not all([pieza_id, client_id]):
        return jsonify({'error': 'pieza_id y client_id requeridos'}), 400

    token = generate_token(session['user'], session['role'])
    pieza = fetch_pieza(pieza_id, token) or {}
    es_hist = is_story(pieza)
    sticker = (pieza.get('stickers') or 'no_usa')

    # Historia con sticker: no se puede publicar por API. Avisar.
    if es_hist and story_mode(sticker) == 'assisted':
        return jsonify({'ok': False, 'assisted': True,
            'error': 'Esta historia lleva sticker interactivo: descárgala y súbela a mano en Instagram.'}), 200

    if copy_text and not es_hist:
        try:
            http_requests.put(f'{UNITY_URL}/api/piezas/{pieza_id}/copy',
                json={'copy': copy_text}, params={'token': token}, timeout=10)
        except Exception:
            pass

    conn = get_db()
    ig_row = conn.execute("SELECT ig_user_id FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    if not ig_row:
        return jsonify({'error': 'No hay cuenta IG configurada para este cliente'}), 400

    meta_tok = get_meta_token_for_client(client_id)
    kind = 'story' if es_hist else 'feed'
    result = publish_piece(pieza_id, ig_row['ig_user_id'], copy_text, token, meta_token=meta_tok, kind=kind)
    if result.get('ok'):
        conn = get_db()
        conn.execute("""INSERT INTO scheduled_posts
            (pieza_id, client_id, ig_user_id, scheduled_at, kind, mode, sticker, copy_text,
             status, ig_container_id, ig_post_id, published_at)
            VALUES (?,?,?,?,?,?,?,?, 'published', ?, ?, datetime('now'))""",
            (pieza_id, client_id, ig_row['ig_user_id'], datetime.utcnow().isoformat(), kind,
             'auto', sticker, copy_text, result.get('container_id', ''), result.get('ig_post_id', '')))
        conn.commit(); conn.close()
        try:
            http_requests.post(f'{UNITY_URL}/api/drop/publicar',
                json={'token': token, 'pieza_id': pieza_id}, timeout=10)
        except Exception:
            pass
    return jsonify(result), 200 if result.get('ok') else 502

@app.route('/api/validar-historia/<int:pieza_id>')
def api_validar_historia(pieza_id):
    """Descarga la imagen de la pieza y valida specs de historia (9:16). No publica."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    token = generate_token(session['user'], session['role'])
    pieza = fetch_pieza(pieza_id, token)
    if not pieza or not pieza.get('drive_file_id'):
        return jsonify({'error': 'Sin archivo en la pieza'}), 400
    filename = download_drive_file(pieza['drive_file_id'], token)
    if not filename:
        return jsonify({'error': 'No se pudo descargar'}), 502
    res = validate_story_media(filename)
    try:
        os.remove(os.path.join(MEDIA_DIR, filename))
    except Exception:
        pass
    return jsonify(res)

@app.route('/api/limit/<int:client_id>')
def api_limit(client_id):
    """Cupo de publicación restante de la cuenta del cliente (para mostrar en la UI)."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    row = conn.execute("SELECT ig_user_id FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'cliente sin cuenta IG'}), 400
    lim = ig_publishing_limit(row['ig_user_id'], get_meta_token_for_client(client_id))
    return jsonify(lim or {'error': 'no disponible'})


# ── Cola / agenda / recordatorios ──

@app.route('/api/scheduled')
def api_scheduled():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    estado = request.args.get('status', '')
    conn = get_db()
    if estado:
        rows = conn.execute("SELECT * FROM scheduled_posts WHERE status=? ORDER BY scheduled_at", (estado,)).fetchall()
    else:
        rows = conn.execute("""SELECT * FROM scheduled_posts
            WHERE status IN ('pending','failed','reminded','processing') ORDER BY scheduled_at""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/reminders')
def api_reminders():
    """Historias asistidas que ya tocan (status=reminded) o que tocan dentro de la próxima hora."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    soon = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    conn = get_db()
    rows = conn.execute("""SELECT * FROM scheduled_posts
        WHERE kind='story' AND mode='assisted'
        AND (status='reminded' OR (status='pending' AND scheduled_at <= ? AND scheduled_at >= ?))
        ORDER BY scheduled_at""", (soon, (datetime.utcnow()-timedelta(hours=12)).isoformat())).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/historia/done', methods=['POST'])
def api_historia_done():
    """Marca una historia asistida como publicada a mano por la operadora."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    sched_id = data.get('sched_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    conn = get_db()
    if sched_id:
        conn.execute("UPDATE scheduled_posts SET status='done', published_at=datetime('now') WHERE id=?", (sched_id,))
    else:
        conn.execute("""UPDATE scheduled_posts SET status='done', published_at=datetime('now')
            WHERE pieza_id=? AND kind='story' AND status IN ('reminded','pending')""", (pieza_id,))
    conn.commit(); conn.close()
    try:
        http_requests.post(f'{UNITY_URL}/api/drop/publicar',
            json={'token': token, 'pieza_id': pieza_id}, timeout=10)
    except Exception:
        pass
    return jsonify({'ok': True})

@app.route('/api/unschedule', methods=['POST'])
def api_unschedule():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    sched_id = data.get('sched_id')
    if not sched_id:
        return jsonify({'error': 'sched_id requerido'}), 400
    conn = get_db()
    conn.execute("DELETE FROM scheduled_posts WHERE id=? AND status IN ('pending','reminded','failed')", (sched_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

# ── Copy IA (Claude Haiku) ──

@app.route('/api/generate-copy', methods=['POST'])
def api_generate_copy():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    pieza = fetch_pieza(pieza_id, token)
    if not pieza or pieza.get('error'):
        return jsonify({'error': 'No se pudo obtener la pieza desde Unity'}), 502

    cliente = pieza.get('cliente', {})
    tipo = pieza.get('tipo_pedido', 'grilla')
    stickers_val = pieza.get('stickers', 'no_usa')
    delivery_si = cliente.get('delivery')
    url_web = cliente.get('url_web', 'el link de la bio')

    longitud_regla = {
        'grilla':    'MÁXIMO 150 caracteres en total (sin contar hashtags). Hook + CTA en 2-3 líneas. Si la descripción lo justifica, máximo 50 palabras. Nunca más.',
        'historias': 'Sin caption. El texto va en la imagen. NO escribas caption.',
        'extra':     'MÁXIMO 150 caracteres en total (sin contar hashtags). Hook + CTA en 2-3 líneas.',
        'ads':       'MÁXIMO 125 caracteres. Una sola idea. CTA directo. Sin rodeos. Máximo 3 hashtags.',
        'reel':      'MÁXIMO 125 caracteres. El hook va en el video, no en el texto. Solo nombra el plato/concepto + CTA.',
    }.get(tipo, 'MÁXIMO 150 caracteres. Hook + CTA. Sin rodeos.')

    cta_delivery = 'Pídelo ahora 🛵 — link en bio.' if delivery_si else ''
    cta_ads = f'¿Lo viste en nuestros anuncios? Encuéntranos en {url_web}.'
    sticker_section = '\n\n--- TEXTO STICKER ---\n[texto del sticker]' if tipo == 'historias' else ''

    prompt = f"""Eres un copywriter experto en gastronomía chilena. Tu única tarea es escribir copy de Instagram listo para copiar y pegar, sin ningún tipo de explicación, análisis ni comentario.

DATOS DEL CLIENTE
Nombre: {cliente.get('nombre', '')}
Voz de marca: {cliente.get('voz_marca', '')}
Atributo superior: {cliente.get('atributo_superior', '')}
Horarios: {cliente.get('horarios', '')}
Delivery: {'Sí' if delivery_si else 'No'}
URL web: {url_web}
Restricciones (NUNCA violar): {cliente.get('restricciones', 'ninguna')}

DATOS DE LA PIEZA
Título: {pieza.get('titulo', '')}
Descripción: {pieza.get('descripcion', '')}
Tipo: {tipo}
Pilar: {pieza.get('pilar', '')}
Sticker: {stickers_val}

REGLAS DE ESCRITURA — todas obligatorias, sin excepción:

HOOK (primera línea):
- Máximo 8 palabras
- PROHIBIDO empezar con: el nombre del restaurante, un precio, un hashtag, un emoji, "hoy", "bienvenidos", "hola"
- Debe generar curiosidad, urgencia o prometer algo concreto
- Sin emojis en la primera línea
- Usa hooks distintos en cada opción: Valor / Curiosidad / Historia / Contrarian / Social Proof

LONGITUD — REGLA MÁS IMPORTANTE:
- {longitud_regla}
- Si el copy supera ese límite, está MAL. Recórtalo. No hay excepción.
- Cada palabra que sobre es engagement perdido. Sé brutal con lo que cortas.

ESTRUCTURA:
- Hook (1 línea) → salto → CTA (1 línea) → salto → hashtags
- Si hay algo más que decir, va entre hook y CTA, máximo 1 línea adicional
- NUNCA bloques de texto corrido
- Menciona un detalle específico real: ingrediente, número, horario o proceso — en el hook o CTA
- Español chileno neutro — directo, sin solemnidad, sin "usted", sin Spanglish

CTA:
- {cta_delivery if cta_delivery else 'CTA hacia reserva o visita al local.'}
- {cta_ads}
- Nunca solo "Link en bio" o "Reserva ahora" solos — siempre con contexto de 3-5 palabras más
- "Guarda esto para el fin de semana 📌" cuando aplique como CTA secundario

HASHTAGS:
- Entre 3 y 5. Ni uno más, ni uno menos.
- 1 de marca + 1-2 ciudad/zona + 1-2 nicho
- PROHIBIDO repetir hashtags entre opción principal y alternativa
- Una sola línea al final, separados por espacio
- Historias: sin hashtags

PROHIBICIONES ABSOLUTAS:
- Sin markdown: sin **, sin ##, sin guiones de formato
- Sin etiquetas internas: "Hook:", "Cuerpo:", "CTA:", "Opción 1:"
- Sin explicaciones ni comentarios antes o después del copy
- Sin pensamiento visible de la IA
- Sin hashtags duplicados entre opciones

FORMATO DE SALIDA — exactamente así, nada más:

--- OPCIÓN PRINCIPAL ---
[copy listo para publicar]

--- OPCIÓN ALTERNATIVA ---
[copy con hook diferente y hashtags distintos]{sticker_section}"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1200,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = msg.content[0].text
        principal = ''; alternativa = ''; sticker_txt = ''
        parts = raw.split('--- OPCIÓN ALTERNATIVA ---')
        if len(parts) >= 2:
            principal = parts[0].replace('--- OPCIÓN PRINCIPAL ---', '').strip()
            sticker_parts = parts[1].split('--- TEXTO STICKER ---')
            alternativa = sticker_parts[0].strip()
            if len(sticker_parts) >= 2:
                sticker_txt = sticker_parts[1].strip()
        else:
            principal = raw.replace('--- OPCIÓN PRINCIPAL ---', '').strip()
        return jsonify({'principal': principal, 'alternativa': alternativa, 'sticker': sticker_txt})
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/save-copy', methods=['POST'])
def api_save_copy():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    copy_text = data.get('copy', '')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.put(f'{UNITY_URL}/api/piezas/{pieza_id}/copy',
            json={'copy': copy_text}, params={'token': token}, timeout=10)
        return (jsonify({'ok': True}), 200) if r.ok else (jsonify({'error': 'Unity error'}), 502)
    except Exception as e:
        return jsonify({'error': str(e)}), 502

# ── Cuentas IG por cliente ──

@app.route('/api/ig-accounts', methods=['GET'])
def api_ig_accounts_get():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM client_ig_accounts ORDER BY client_name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/ig-accounts', methods=['POST'])
def api_ig_accounts_save():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    if not data.get('client_id') or not data.get('ig_user_id'):
        return jsonify({'error': 'client_id e ig_user_id requeridos'}), 400
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO client_ig_accounts
        (client_id, client_name, ig_user_id, ig_username, token_key, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (data['client_id'], data.get('client_name', ''), data['ig_user_id'],
         data.get('ig_username', ''), data.get('token_key', 'system')))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/discover-ig-accounts')
def api_discover_ig():
    """Lee del Business Manager las cuentas IG disponibles (para enchufar clientes rápido)."""
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    if not (META_TOKEN and META_BM_ID):
        return jsonify({'error': 'Falta META_TOKEN o META_BM_ID'}), 400
    try:
        r = http_requests.get(f'{GRAPH}/{META_BM_ID}/instagram_accounts',
            params={'fields': 'id,username,name', 'limit': 100, 'access_token': META_TOKEN}, timeout=20)
        return jsonify(r.json().get('data', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/logout')
def logout():
    session.clear()
    return redirect(OS_URL)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5055))
    app.run(debug=True, port=port, use_reloader=False)
