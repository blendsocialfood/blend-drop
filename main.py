import os
import hmac
import hashlib
import time
import sqlite3
import uuid
import requests as http_requests
from datetime import datetime, timezone, timedelta
from flask import Flask, request, redirect, session, jsonify, send_from_directory, send_file, Response
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

import tz_utils

# .env local en desarrollo (en Railway las vars vienen del entorno)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Pillow para transcodear/validar imágenes (la API solo acepta JPEG en historias)
try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'blend-drop-secret-2026')
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB (videos de historia)

OS_URL = os.environ.get('OS_URL', 'https://socialfood-os-production.up.railway.app')
AUTH_SECRET = 'blendsf-auth-2026'

DB_PATH = os.environ.get('DB_PATH', '/data/blend_drop.db')
os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
# Media en el VOLUMEN persistente (no /tmp: si no, los archivos mueren en cada redeploy)
MEDIA_DIR = os.environ.get('MEDIA_DIR') or os.path.join(os.path.dirname(DB_PATH) or '.', 'media')
os.makedirs(MEDIA_DIR, exist_ok=True)

META_TOKEN = os.environ.get('META_TOKEN', '')           # System User "Drop" (no expira)
META_TOKEN_NICO = os.environ.get('META_TOKEN_NICO', '')  # Token personal Nico
META_BM_ID = os.environ.get('META_BM_ID', '')
DROP_URL = os.environ.get('DROP_URL', 'https://blend-drop-production.up.railway.app')
# Meta NO baja media de *.up.railway.app -> se hostea en la web propia (blendsocialfood.cl) via uploader PHP
WEB_UPLOAD_URL = os.environ.get('WEB_UPLOAD_URL', '')
WEB_UPLOAD_TOKEN = os.environ.get('WEB_UPLOAD_TOKEN', '')
GRAPH = 'https://graph.facebook.com/v21.0'

IG_DAILY_LIMIT_FALLBACK = 25  # red de seguridad solo si Meta no responde el cupo real

ALLOWED_MIME = {'image/jpeg', 'image/png', 'image/webp', 'video/mp4', 'video/quicktime'}


def get_meta_token_for_client(client_id):
    """Token Meta correcto según el cliente (token_key en BD, fallback a mapeo)."""
    NICO_CLIENTS = {2, 4}
    conn = get_db()
    row = conn.execute("SELECT token_key FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    if row and row['token_key']:
        key = row['token_key']
    else:
        key = 'nico' if int(client_id) in NICO_CLIENTS else 'system'
    return META_TOKEN_NICO if key == 'nico' else META_TOKEN

# ── Auth (idéntica a v3, login por el OS) ──

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
    if os.environ.get('DEV_NO_AUTH') == '1':
        if 'user' not in session:
            session['user'] = 'dev'; session['role'] = 'admin'
        return True
    return 'user' in session

# ── BD ──

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def _safe(conn, sql):
    try:
        conn.execute(sql); conn.commit()
    except Exception:
        pass

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_ig_accounts (
            client_id INTEGER PRIMARY KEY,
            client_name TEXT,
            ig_user_id TEXT NOT NULL,
            ig_username TEXT,
            token_key TEXT DEFAULT 'system',
            timezone TEXT NOT NULL DEFAULT 'America/Santiago',
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id INTEGER NOT NULL,
            ig_user_id TEXT NOT NULL,
            slot_date TEXT NOT NULL,            -- YYYY-MM-DD, día LOCAL del cliente
            position INTEGER NOT NULL DEFAULT 0,
            media_filename TEXT,
            kind TEXT DEFAULT 'foto',           -- foto | video
            destino TEXT NOT NULL DEFAULT 'story', -- feed | story
            caption TEXT DEFAULT '',
            local_time TEXT,                    -- HH:MM hora local del cliente
            timezone TEXT,                      -- snapshot IANA al programar
            scheduled_at_utc TEXT,              -- ISO UTC con +00:00, NULL si no programada
            status TEXT NOT NULL DEFAULT 'loaded', -- loaded(rojo)|scheduled(verde)|processing|published|failed
            mode TEXT DEFAULT 'manual',
            ig_container_id TEXT,
            ig_post_id TEXT,
            error_msg TEXT,
            published_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(client_id, slot_date, position)
        )
    """)
    conn.commit()
    # Migraciones idempotentes (sobre BDs v3 que solo tenían client_ig_accounts)
    for ddl in [
        "ALTER TABLE client_ig_accounts ADD COLUMN timezone TEXT NOT NULL DEFAULT 'America/Santiago'",
        "CREATE INDEX IF NOT EXISTS idx_slots_cron ON slots(status, scheduled_at_utc)",
        "CREATE INDEX IF NOT EXISTS idx_slots_cal ON slots(client_id, slot_date)",
        "CREATE INDEX IF NOT EXISTS idx_slots_pub ON slots(ig_user_id, status, published_at)",
    ]:
        _safe(conn, ddl)
    conn.close()
    print("[DROP v4] init_db OK · MEDIA_DIR=" + MEDIA_DIR)

init_db()

# ── Media: servir + preparar + validar ──

@app.route('/media/<path:filename>')
def serve_media(filename):
    fp = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(fp):
        return 'Not found', 404
    return send_file(fp)

def _ext_for(mime, fallback_name=''):
    if mime in ('video/mp4', 'video/quicktime') or fallback_name.lower().endswith(('.mp4', '.mov')):
        return 'mp4'
    if mime == 'image/png' or fallback_name.lower().endswith('.png'):
        return 'png'
    if mime == 'image/webp' or fallback_name.lower().endswith('.webp'):
        return 'webp'
    return 'jpg'

def prepare_media_for_publish(filename):
    """La Content Publishing API solo acepta JPEG en imágenes (rechaza PNG/WebP/GIF) y máx 8MB.
    Transcodea a JPEG sRGB, achica si excede el lado largo, recomprime <8MB. No toca videos.
    Retorna (filename_final, warnings)."""
    if filename.endswith('.mp4') or not HAS_PIL:
        return filename, []
    warnings = []
    src = os.path.join(MEDIA_DIR, filename)
    try:
        with Image.open(src) as im:
            fmt = im.format
            im = im.convert('RGB')
            w, h = im.size
            if max(w, h) > 1920:
                s = 1920 / max(w, h)
                im = im.resize((int(w * s), int(h * s)), Image.LANCZOS)
            out = filename.rsplit('.', 1)[0] + '.jpg'
            op = os.path.join(MEDIA_DIR, out)
            q = 90
            im.save(op, 'JPEG', quality=q, optimize=True)
            while os.path.getsize(op) > 8 * 1024 * 1024 and q > 60:
                q -= 10
                im.save(op, 'JPEG', quality=q, optimize=True)
            if out != filename:
                if fmt not in ('JPEG', 'MPO'):
                    warnings.append(f'Imagen {fmt} convertida a JPEG para la API')
                try: os.remove(src)
                except Exception: pass
            return out, warnings
    except Exception as e:
        return filename, [f'No se pudo preparar la imagen: {e}']

def validate_story_media(filename):
    """Warnings de specs de historia (≈9:16, ancho≥1080, ≤8MB). No bloquea."""
    warnings = []
    if filename.endswith('.mp4'):
        return {'warnings': ['Video: revisa 9:16 y 3-60s a mano']}
    if not HAS_PIL:
        return {'warnings': []}
    try:
        with Image.open(os.path.join(MEDIA_DIR, filename)) as im:
            w, h = im.size
            ratio = w / h if h else 0
            if abs(ratio - 9/16) > 0.06:
                warnings.append(f'{w}x{h} no es 9:16 — Instagram puede recortar la historia')
            if w < 1080:
                warnings.append(f'Ancho {w}px < 1080 — puede verse pixelada')
        size_mb = os.path.getsize(os.path.join(MEDIA_DIR, filename)) / 1048576
        if size_mb > 8:
            warnings.append(f'Pesa {size_mb:.1f}MB (máx 8MB) — se recomprime al publicar')
    except Exception:
        pass
    return {'warnings': warnings}

# ── Rate limit ──

def count_published_24h(ig_user_id):
    since = (tz_utils.now_utc() - timedelta(hours=24)).isoformat()
    conn = get_db()
    n = conn.execute("""SELECT COUNT(*) c FROM slots
        WHERE ig_user_id=? AND status='published' AND published_at >= ?""", (ig_user_id, since)).fetchone()['c']
    conn.close()
    return n

def ig_publishing_limit(ig_user_id, meta_token):
    try:
        r = http_requests.get(f'{GRAPH}/{ig_user_id}/content_publishing_limit',
            params={'fields': 'config,quota_usage', 'access_token': meta_token}, timeout=15)
        d = (r.json().get('data') or [{}])[0]
        usage = d.get('quota_usage', 0) or 0
        total = (d.get('config') or {}).get('quota_total', 50) or 50
        return {'usage': usage, 'total': total, 'remaining': max(0, total - usage)}
    except Exception:
        return None

# ── Motor Meta (intacto desde v3) ──

def publish_to_meta(ig_user_id, media_url, kind, is_video, caption, meta_token):
    try:
        if kind == 'story':
            params = {'access_token': meta_token, 'media_type': 'STORIES'}
            params['video_url' if is_video else 'image_url'] = media_url
        elif is_video:
            params = {'access_token': meta_token, 'caption': caption,
                      'media_type': 'REELS', 'video_url': media_url, 'share_to_feed': 'true'}
        else:
            params = {'access_token': meta_token, 'caption': caption, 'image_url': media_url}
        r = http_requests.post(f'{GRAPH}/{ig_user_id}/media', params=params, timeout=60)
        cdata = r.json()
        cid = cdata.get('id')
        if not cid:
            return {'ok': False, 'error': f'Meta container error: {cdata}'}
        if is_video or kind == 'story':
            for _ in range(24):
                time.sleep(5)
                st = http_requests.get(f'{GRAPH}/{cid}',
                    params={'fields': 'status_code', 'access_token': meta_token}, timeout=20).json()
                sc = st.get('status_code')
                if sc == 'FINISHED': break
                if sc == 'ERROR': return {'ok': False, 'error': f'Container ERROR: {st}', 'container_id': cid}
        pub = http_requests.post(f'{GRAPH}/{ig_user_id}/media_publish',
            params={'creation_id': cid, 'access_token': meta_token}, timeout=30).json()
        if pub.get('id'):
            ptype = None
            try:
                ptype = http_requests.get(f"{GRAPH}/{pub['id']}",
                    params={'fields': 'media_product_type', 'access_token': meta_token}, timeout=15).json().get('media_product_type')
            except Exception:
                pass
            return {'ok': True, 'ig_post_id': pub['id'], 'container_id': cid, 'product_type': ptype}
        return {'ok': False, 'error': f'Meta publish error: {pub}', 'container_id': cid}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

def web_upload(filename):
    """Sube el media a la web propia (Meta no baja de *.up.railway.app). Retorna URL pública o None."""
    if not (WEB_UPLOAD_URL and WEB_UPLOAD_TOKEN):
        return None
    path = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'rb') as f:
            r = http_requests.post(WEB_UPLOAD_URL,
                headers={'X-Drop-Token': WEB_UPLOAD_TOKEN,
                         'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'},
                files={'file': (filename, f)}, data={'name': filename}, timeout=90)
        if r.ok and r.text.strip().startswith('http'):
            return r.text.strip()
        print(f"[DROP v4] web_upload bad response: {r.status_code} {r.text[:80]}")
    except Exception as e:
        print(f"[DROP v4] web_upload error: {e}")
    return None


def publish_slot(slot, meta_token=None):
    """Publica un slot cuyo media ya está local en MEDIA_DIR. Sin Unity."""
    if not meta_token:
        meta_token = get_meta_token_for_client(slot['client_id'])
    fn = slot['media_filename']
    if not fn or not os.path.exists(os.path.join(MEDIA_DIR, fn)):
        return {'ok': False, 'error': 'Sin archivo en el slot (¿se perdió el media?)'}
    limit = ig_publishing_limit(slot['ig_user_id'], meta_token)
    if limit and limit['remaining'] <= 0:
        return {'ok': False, 'error': f"Cupo 24h agotado en Meta ({limit['usage']}/{limit['total']})", 'defer': True}
    if not limit and count_published_24h(slot['ig_user_id']) >= IG_DAILY_LIMIT_FALLBACK:
        return {'ok': False, 'error': f'Límite local {IG_DAILY_LIMIT_FALLBACK}/24h', 'defer': True}
    is_video = fn.endswith('.mp4')
    kind = 'story' if slot['destino'] == 'story' else 'feed'
    caption = '' if kind == 'story' else (slot['caption'] or '')
    # Hostear el media en la web propia (Meta no baja de *.up.railway.app); fallback al /media local
    media_url = web_upload(fn) or f"{DROP_URL}/media/{fn}"
    return publish_to_meta(slot['ig_user_id'], media_url, kind, is_video, caption, meta_token)

# ── Cron ──

def cron_tick():
    """Cada 5 min: publica los slots 'scheduled' cuya hora UTC cae en la ventana. Compara aware."""
    now = tz_utils.now_utc()
    win_end = now + timedelta(minutes=20)
    win_start = now - timedelta(hours=6)
    conn = get_db()
    rows = conn.execute("""SELECT * FROM slots
        WHERE status='scheduled' AND ig_post_id IS NULL AND scheduled_at_utc IS NOT NULL
        ORDER BY scheduled_at_utc""").fetchall()
    conn.close()
    for s in rows:
        try:
            sched = datetime.fromisoformat(s['scheduled_at_utc'])
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if not (win_start <= sched <= win_end):
            continue
        sid = s['id']
        conn = get_db()
        conn.execute("UPDATE slots SET status='processing', updated_at=datetime('now') WHERE id=? AND status='scheduled'", (sid,))
        changed = conn.total_changes
        conn.commit(); conn.close()
        if not changed:
            continue  # otro tick la tomó
        print(f"[DROP v4 CRON] publicando slot {sid} ({s['destino']}) cliente {s['client_id']}")
        result = publish_slot(s)
        conn = get_db()
        if result.get('ok'):
            conn.execute("""UPDATE slots SET status='published', ig_post_id=?, ig_container_id=?,
                published_at=?, updated_at=datetime('now') WHERE id=?""",
                (result.get('ig_post_id', ''), result.get('container_id', ''), now.isoformat(), sid))
        elif result.get('defer'):
            conn.execute("UPDATE slots SET status='scheduled', error_msg=?, updated_at=datetime('now') WHERE id=?",
                         (result.get('error', 'cupo'), sid))
        else:
            conn.execute("UPDATE slots SET status='failed', error_msg=?, updated_at=datetime('now') WHERE id=?",
                         (result.get('error', 'Unknown'), sid))
        conn.commit(); conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(cron_tick, 'interval', minutes=5, id='cron_tick')
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))
print("[DROP v4] APScheduler started")

# ── Helpers de slot ──

def slot_dict(s):
    d = dict(s)
    d['thumb_url'] = f"/media/{s['media_filename']}" if s['media_filename'] else None
    return d

def client_row(client_id):
    conn = get_db()
    r = conn.execute("SELECT * FROM client_ig_accounts WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    return r

# ── Routes base ──

@app.route('/')
def index():
    if 'user' not in session:
        token = request.args.get('token', '')
        if token:
            u = verify_token(token)
            if u:
                session['user'] = u['username']; session['role'] = u['role']; session['token'] = token
                return redirect('/')
        if os.environ.get('DEV_NO_AUTH') == '1':
            session['user'] = 'dev'; session['role'] = 'admin'
            return send_from_directory('.', 'app.html')
        return redirect(OS_URL)
    return send_from_directory('.', 'app.html')

@app.route('/healthz')
def healthz():
    return jsonify({'ok': True, 'pil': HAS_PIL, 'meta_token': bool(META_TOKEN),
                    'media_dir': MEDIA_DIR, 'media_writable': os.access(MEDIA_DIR, os.W_OK)})

@app.route('/api/clients')
def api_clients():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    rows = conn.execute("SELECT client_id, client_name, ig_username, timezone, token_key FROM client_ig_accounts ORDER BY client_name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/calendar')
def api_calendar():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    month = request.args.get('month', '')  # YYYY-MM
    conn = get_db()
    rows = conn.execute("""SELECT slot_date,
        SUM(CASE WHEN status='scheduled' THEN 1 ELSE 0 END) scheduled,
        SUM(CASE WHEN status='loaded' THEN 1 ELSE 0 END) loaded,
        SUM(CASE WHEN status='published' THEN 1 ELSE 0 END) published,
        SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
        COUNT(*) total
        FROM slots WHERE slot_date LIKE ? GROUP BY slot_date""", (month + '-%',)).fetchall()
    conn.close()
    return jsonify({r['slot_date']: dict(r) for r in rows})

@app.route('/api/day')
def api_day():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    date = request.args.get('date', '')
    conn = get_db()
    clients = conn.execute("SELECT * FROM client_ig_accounts ORDER BY client_name").fetchall()
    out = []
    for c in clients:
        slots = conn.execute("SELECT * FROM slots WHERE client_id=? AND slot_date=? ORDER BY position",
                             (c['client_id'], date)).fetchall()
        out.append({
            'client_id': c['client_id'], 'client_name': c['client_name'],
            'ig_username': c['ig_username'], 'timezone': c['timezone'],
            'tz_label': tz_utils.offset_label(c['timezone']),
            'slots': [slot_dict(s) for s in slots],
        })
    conn.close()
    return jsonify({'date': date, 'clients': out})

# ── Slots: upload / programar / etc ──

def _save_upload(file_storage, client_id, ig_user_id, slot_date, position, destino):
    mime = file_storage.mimetype or ''
    if mime not in ALLOWED_MIME and not file_storage.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov')):
        return None, f'Tipo no soportado: {mime}'
    ext = _ext_for(mime, file_storage.filename)
    fn = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(os.path.join(MEDIA_DIR, fn))
    fn, _w = prepare_media_for_publish(fn)
    warnings = _w
    kind = 'video' if fn.endswith('.mp4') else 'foto'
    if destino == 'story':
        warnings = warnings + validate_story_media(fn)['warnings']
    conn = get_db()
    conn.execute("""INSERT INTO slots (client_id, ig_user_id, slot_date, position, media_filename, kind, destino, status, mode)
        VALUES (?,?,?,?,?,?,?, 'loaded', 'manual')
        ON CONFLICT(client_id, slot_date, position) DO UPDATE SET
          media_filename=excluded.media_filename, kind=excluded.kind, destino=excluded.destino,
          status='loaded', mode='manual', local_time=NULL, timezone=NULL, scheduled_at_utc=NULL,
          error_msg=NULL, updated_at=datetime('now')""",
        (client_id, ig_user_id, slot_date, position, fn, kind, destino))
    conn.commit()
    sid = conn.execute("SELECT id FROM slots WHERE client_id=? AND slot_date=? AND position=?",
                       (client_id, slot_date, position)).fetchone()['id']
    conn.close()
    return {'id': sid, 'media_filename': fn, 'thumb_url': f'/media/{fn}', 'kind': kind,
            'destino': destino, 'status': 'loaded', 'warnings': warnings}, None

@app.route('/api/slot/upload', methods=['POST'])
def api_slot_upload():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    f = request.files.get('file')
    client_id = request.form.get('client_id')
    slot_date = request.form.get('date')
    position = request.form.get('position', type=int)
    destino = request.form.get('destino', 'story')
    if not (f and client_id and slot_date is not None and position is not None):
        return jsonify({'error': 'file, client_id, date, position requeridos'}), 400
    c = client_row(client_id)
    if not c:
        return jsonify({'error': 'cliente sin cuenta IG'}), 400
    res, err = _save_upload(f, int(client_id), c['ig_user_id'], slot_date, position, destino)
    if err:
        return jsonify({'error': err}), 415
    return jsonify(res)

@app.route('/api/slot/upload-batch', methods=['POST'])
def api_slot_upload_batch():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    files = request.files.getlist('file')
    client_id = request.form.get('client_id')
    slot_date = request.form.get('date')
    destino = request.form.get('destino', 'story')
    if not (files and client_id and slot_date):
        return jsonify({'error': 'file(s), client_id, date requeridos'}), 400
    c = client_row(client_id)
    if not c:
        return jsonify({'error': 'cliente sin cuenta IG'}), 400
    conn = get_db()
    taken = {r['position'] for r in conn.execute(
        "SELECT position FROM slots WHERE client_id=? AND slot_date=? AND media_filename IS NOT NULL",
        (int(client_id), slot_date)).fetchall()}
    conn.close()
    pos = 0
    out = []
    for f in files:
        while pos in taken:
            pos += 1
        res, err = _save_upload(f, int(client_id), c['ig_user_id'], slot_date, pos, destino)
        if res:
            out.append(res); taken.add(pos)
        pos += 1
    return jsonify({'slots': out})

@app.route('/api/slot/<int:sid>/schedule', methods=['POST'])
def api_slot_schedule(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    local_time = data.get('local_time')  # HH:MM
    if not local_time:
        return jsonify({'error': 'local_time requerido'}), 400
    conn = get_db()
    s = conn.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
    if not s or not s['media_filename']:
        conn.close(); return jsonify({'error': 'slot sin media'}), 400
    c = conn.execute("SELECT timezone FROM client_ig_accounts WHERE client_id=?", (s['client_id'],)).fetchone()
    tzname = (c['timezone'] if c else None) or tz_utils.DEFAULT_TZ
    destino = data.get('destino', s['destino'])
    caption = data.get('caption', s['caption'] or '')
    utc_iso = tz_utils.local_to_utc(f"{s['slot_date']}T{local_time}", tzname)
    # rechazar si quedó muy en el pasado (>6h)
    sched = datetime.fromisoformat(utc_iso)
    if sched < tz_utils.now_utc() - timedelta(hours=6):
        conn.close(); return jsonify({'error': 'esa hora ya pasó hace rato'}), 400
    conn.execute("""UPDATE slots SET local_time=?, timezone=?, scheduled_at_utc=?, destino=?, caption=?,
        status='scheduled', mode='auto', error_msg=NULL, updated_at=datetime('now') WHERE id=?""",
        (local_time, tzname, utc_iso, destino, caption, sid))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'status': 'scheduled', 'scheduled_at_utc': utc_iso,
                    'local_time': local_time, 'timezone': tzname})

@app.route('/api/slot/<int:sid>/unschedule', methods=['POST'])
def api_slot_unschedule(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    conn.execute("""UPDATE slots SET status='loaded', mode='manual', local_time=NULL, timezone=NULL,
        scheduled_at_utc=NULL, error_msg=NULL, updated_at=datetime('now')
        WHERE id=? AND status IN ('scheduled','failed')""", (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'status': 'loaded'})

@app.route('/api/slot/<int:sid>/publish-now', methods=['POST'])
def api_slot_publish_now(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    s = conn.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
    if not s or not s['media_filename']:
        conn.close(); return jsonify({'error': 'slot sin media'}), 400
    data = request.json or {}
    destino = data.get('destino', s['destino']); caption = data.get('caption', s['caption'] or '')
    conn.execute("UPDATE slots SET destino=?, caption=?, status='processing', mode='auto' WHERE id=?",
                 (destino, caption, sid))
    conn.commit()
    s = conn.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone(); conn.close()
    result = publish_slot(s)
    conn = get_db()
    if result.get('ok'):
        conn.execute("""UPDATE slots SET status='published', ig_post_id=?, ig_container_id=?,
            published_at=?, updated_at=datetime('now') WHERE id=?""",
            (result.get('ig_post_id', ''), result.get('container_id', ''), tz_utils.now_utc().isoformat(), sid))
    else:
        conn.execute("UPDATE slots SET status='failed', error_msg=? WHERE id=?", (result.get('error', 'err'), sid))
    conn.commit(); conn.close()
    return jsonify(result), (200 if result.get('ok') else 502)

@app.route('/api/slot/<int:sid>/download')
def api_slot_download(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    s = conn.execute("SELECT media_filename FROM slots WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not s or not s['media_filename']:
        return jsonify({'error': 'sin archivo'}), 404
    fp = os.path.join(MEDIA_DIR, s['media_filename'])
    if not os.path.exists(fp):
        return jsonify({'error': 'archivo no encontrado'}), 404
    return send_file(fp, as_attachment=True, download_name=s['media_filename'])

@app.route('/api/slot/<int:sid>', methods=['DELETE'])
def api_slot_delete(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    s = conn.execute("SELECT media_filename, status FROM slots WHERE id=?", (sid,)).fetchone()
    if not s:
        conn.close(); return jsonify({'error': 'no existe'}), 404
    warn = None
    if s['status'] == 'published':
        warn = 'No se borró de Instagram, solo del Drop'
    conn.execute("DELETE FROM slots WHERE id=?", (sid,))
    conn.commit(); conn.close()
    if s['media_filename']:
        try: os.remove(os.path.join(MEDIA_DIR, s['media_filename']))
        except Exception: pass
    return jsonify({'ok': True, 'warning': warn})

@app.route('/api/slot/<int:sid>/replace', methods=['POST'])
def api_slot_replace(sid):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'file requerido'}), 400
    conn = get_db()
    s = conn.execute("SELECT * FROM slots WHERE id=?", (sid,)).fetchone()
    conn.close()
    if not s:
        return jsonify({'error': 'no existe'}), 404
    old = s['media_filename']
    res, err = _save_upload(f, s['client_id'], s['ig_user_id'], s['slot_date'], s['position'], s['destino'])
    if err:
        return jsonify({'error': err}), 415
    if old and old != res['media_filename']:
        try: os.remove(os.path.join(MEDIA_DIR, old))
        except Exception: pass
    return jsonify(res)

# ── Cuentas IG + timezone ──

@app.route('/api/clients/<int:client_id>/timezone', methods=['POST'])
def api_client_tz(client_id):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    tzname = (request.json or {}).get('timezone', '')
    if not tz_utils.valid_tz(tzname):
        return jsonify({'error': 'timezone IANA inválida'}), 400
    conn = get_db()
    conn.execute("UPDATE client_ig_accounts SET timezone=?, updated_at=datetime('now') WHERE client_id=?", (tzname, client_id))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'timezone': tzname, 'label': tz_utils.offset_label(tzname)})

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
    d = request.json or {}
    if not d.get('client_id') or not d.get('ig_user_id'):
        return jsonify({'error': 'client_id e ig_user_id requeridos'}), 400
    tzname = d.get('timezone', 'America/Santiago')
    if not tz_utils.valid_tz(tzname):
        tzname = 'America/Santiago'
    conn = get_db()
    conn.execute("""INSERT INTO client_ig_accounts (client_id, client_name, ig_user_id, ig_username, token_key, timezone, updated_at)
        VALUES (?,?,?,?,?,?, datetime('now'))
        ON CONFLICT(client_id) DO UPDATE SET client_name=excluded.client_name, ig_user_id=excluded.ig_user_id,
          ig_username=excluded.ig_username, token_key=excluded.token_key, timezone=excluded.timezone, updated_at=datetime('now')""",
        (d['client_id'], d.get('client_name', ''), d['ig_user_id'], d.get('ig_username', ''),
         d.get('token_key', 'system'), tzname))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/discover-ig-accounts')
def api_discover_ig():
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    if not (META_TOKEN and META_BM_ID):
        return jsonify({'error': 'falta META_TOKEN o META_BM_ID'}), 400
    try:
        r = http_requests.get(f'{GRAPH}/{META_BM_ID}/instagram_accounts',
            params={'fields': 'id,username,name', 'limit': 100, 'access_token': META_TOKEN}, timeout=20)
        return jsonify(r.json().get('data', []))
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/limit/<int:client_id>')
def api_limit(client_id):
    if not require_auth():
        return jsonify({'error': 'no auth'}), 401
    c = client_row(client_id)
    if not c:
        return jsonify({'error': 'cliente sin cuenta IG'}), 400
    return jsonify(ig_publishing_limit(c['ig_user_id'], get_meta_token_for_client(client_id)) or {'error': 'no disponible'})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(OS_URL)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5055))
    app.run(debug=True, port=port, use_reloader=False)
