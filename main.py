import os
import hmac
import hashlib
import time
import sqlite3
import threading
import uuid
import requests as http_requests
from datetime import datetime, timedelta
from flask import Flask, request, redirect, session, jsonify, send_from_directory, send_file
from apscheduler.schedulers.background import BackgroundScheduler
import anthropic
import atexit

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'blend-drop-secret-2026')

UNITY_URL = os.environ.get('UNITY_URL', 'https://positive-appreciation-production.up.railway.app')
OS_URL = os.environ.get('OS_URL', 'https://socialfood-os-production.up.railway.app')
AUTH_SECRET = 'blendsf-auth-2026'

DB_PATH = os.environ.get('DB_PATH', '/data/blend_drop.db')
META_TOKEN = os.environ.get('META_TOKEN', '')
META_BM_ID = os.environ.get('META_BM_ID', '')
ANTHROPIC_API_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
DROP_URL = os.environ.get('DROP_URL', 'https://blend-drop-production.up.railway.app')
MEDIA_DIR = '/tmp/drop_media'
os.makedirs(MEDIA_DIR, exist_ok=True)

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

# ── BD ──

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pieza_id INTEGER NOT NULL,
            client_id INTEGER NOT NULL,
            ig_user_id TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            ig_container_id TEXT,
            error_msg TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS client_ig_accounts (
            client_id INTEGER PRIMARY KEY,
            client_name TEXT,
            ig_user_id TEXT NOT NULL,
            ig_username TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
    print("[DROP] init_db OK")

init_db()

# ── Media pública ──

@app.route('/media/<filename>')
def serve_media(filename):
    """Sirve archivos temporales para que Meta API pueda accederlos."""
    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(filepath):
        return 'Not found', 404
    return send_file(filepath)

# ── Drive download ──

def download_drive_file(drive_file_id, token):
    """Descarga archivo de Drive vía Unity proxy y lo guarda en MEDIA_DIR."""
    try:
        r = http_requests.get(
            f'{UNITY_URL}/api/drive/file/{drive_file_id}',
            params={'token': token},
            timeout=30,
            stream=True
        )
        if not r.ok:
            return None
        ext = 'jpg'
        ct = r.headers.get('content-type', '')
        if 'mp4' in ct or 'video' in ct:
            ext = 'mp4'
        elif 'png' in ct:
            ext = 'png'
        filename = f"{uuid.uuid4().hex}.{ext}"
        filepath = os.path.join(MEDIA_DIR, filename)
        with open(filepath, 'wb') as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        return filename
    except Exception as e:
        print(f"[DROP] Error descargando Drive: {e}")
        return None

# ── Meta API publish ──

def publish_piece(pieza_id, ig_user_id, copy_text, token):
    """Crea container Meta y publica. Retorna {'ok': True} o {'ok': False, 'error': str}."""
    # 1. Obtener pieza
    try:
        r = http_requests.get(f'{UNITY_URL}/api/pieza-detail/{pieza_id}',
            params={'token': token}, timeout=10)
        pieza = r.json()
    except Exception as e:
        return {'ok': False, 'error': f'Unity error: {e}'}

    drive_file_id = pieza.get('drive_file_id')
    tipo = pieza.get('tipo_pedido', 'grilla')
    caption = copy_text or pieza.get('copy', '')

    if not drive_file_id:
        return {'ok': False, 'error': 'Sin archivo Drive en la pieza'}

    # 2. Descargar archivo y obtener URL pública
    filename = download_drive_file(drive_file_id, token)
    if not filename:
        return {'ok': False, 'error': 'No se pudo descargar archivo de Drive'}

    media_url = f"{DROP_URL}/media/{filename}"
    is_video = filename.endswith('.mp4')

    # 3. Crear container Meta
    try:
        if tipo == 'reel':
            params = {
                'access_token': META_TOKEN,
                'caption': caption,
                'media_type': 'REELS',
                'video_url': media_url,
                'share_to_feed': 'true'
            }
        else:  # grilla / carrusel (imagen principal por ahora; carrusel multi en v2.1)
            params = {
                'access_token': META_TOKEN,
                'caption': caption,
                'image_url': media_url,
            }

        r = http_requests.post(
            f'https://graph.facebook.com/v21.0/{ig_user_id}/media',
            params=params, timeout=60
        )
        container_data = r.json()
        container_id = container_data.get('id')
        if not container_id:
            return {'ok': False, 'error': f'Meta container error: {container_data}'}

        # 4. Esperar que el container esté listo (para videos)
        if is_video:
            for _ in range(12):
                time.sleep(5)
                status_r = http_requests.get(
                    f'https://graph.facebook.com/v21.0/{container_id}',
                    params={'fields': 'status_code', 'access_token': META_TOKEN}
                )
                if status_r.json().get('status_code') == 'FINISHED':
                    break

        # 5. Publicar
        pub_r = http_requests.post(
            f'https://graph.facebook.com/v21.0/{ig_user_id}/media_publish',
            params={'creation_id': container_id, 'access_token': META_TOKEN},
            timeout=30
        )
        pub_data = pub_r.json()
        if pub_data.get('id'):
            try:
                os.remove(os.path.join(MEDIA_DIR, filename))
            except Exception:
                pass
            return {'ok': True, 'ig_post_id': pub_data['id'], 'container_id': container_id}
        else:
            return {'ok': False, 'error': f'Meta publish error: {pub_data}'}

    except Exception as e:
        return {'ok': False, 'error': str(e)}

# ── Cron ──

def cron_publish():
    """Ejecuta cada 5 min. Publica posts cuyo scheduled_at está en los próximos 20 min."""
    now = datetime.utcnow()
    window_end = now + timedelta(minutes=20)

    conn = get_db()
    posts = conn.execute("""
        SELECT * FROM scheduled_posts
        WHERE status='pending'
        AND scheduled_at <= ?
        AND scheduled_at >= ?
    """, (window_end.isoformat(), now.isoformat())).fetchall()
    conn.close()

    for post in posts:
        post_id = post['id']
        print(f"[DROP CRON] Publicando post {post_id} pieza {post['pieza_id']}")

        conn = get_db()
        conn.execute("UPDATE scheduled_posts SET status='processing' WHERE id=?", (post_id,))
        conn.commit()
        conn.close()

        token = generate_token('drop-cron', 'admin')
        result = publish_piece(post['pieza_id'], post['ig_user_id'], '', token)

        conn = get_db()
        if result.get('ok'):
            conn.execute("""
                UPDATE scheduled_posts
                SET status='published', ig_container_id=?
                WHERE id=?
            """, (result.get('container_id', ''), post_id))
            try:
                http_requests.post(f'{UNITY_URL}/api/drop/publicar',
                    json={'token': token, 'pieza_id': post['pieza_id']}, timeout=10)
            except Exception:
                pass
        else:
            conn.execute("""
                UPDATE scheduled_posts
                SET status='failed', error_msg=?
                WHERE id=?
            """, (result.get('error', 'Unknown'), post_id))
        conn.commit()
        conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(cron_publish, 'interval', minutes=5, id='cron_publish')
scheduler.start()
atexit.register(lambda: scheduler.shutdown(wait=False))
print("[DROP] APScheduler started")

# ── Routes ──

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
        return redirect(OS_URL)
    return send_from_directory('.', 'app.html')

@app.route('/api/hoy')
def api_hoy():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    fecha = request.args.get('fecha', '')
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar',
            params={'fecha': fecha, 'token': token}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/semana')
def api_semana():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    fecha = request.args.get('fecha', '')
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar-semana',
            params={'fecha': fecha, 'token': token}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/clientes')
def api_clientes():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/list-clients',
            params={'token': token}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/mes')
def api_mes():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    mes = request.args.get('mes', '')
    client_id = request.args.get('client_id', '')
    token = generate_token(session['user'], session['role'])
    params = {'mes': mes, 'token': token}
    if client_id:
        params['client_id'] = client_id
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar-mes',
            params=params, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/deshacer', methods=['POST'])
def api_deshacer():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.post(f'{UNITY_URL}/api/drop/deshacer',
            json={'token': token, 'pieza_id': pieza_id}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/publicar', methods=['POST'])
def api_publicar():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.post(f'{UNITY_URL}/api/drop/publicar',
            json={'token': token, 'pieza_id': pieza_id}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

# ── Drop v2 endpoints ──

@app.route('/api/schedule', methods=['POST'])
def api_schedule():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    client_id = data.get('client_id')
    scheduled_at = data.get('scheduled_at')  # ISO format: "2026-04-06T18:00"
    copy_text = data.get('copy', '')

    if not all([pieza_id, client_id, scheduled_at]):
        return jsonify({'error': 'pieza_id, client_id y scheduled_at requeridos'}), 400

    # Guardar copy en Unity
    token = generate_token(session['user'], session['role'])
    try:
        http_requests.put(
            f'{UNITY_URL}/api/piezas/{pieza_id}/copy',
            json={'copy': copy_text},
            params={'token': token},
            timeout=10
        )
    except Exception:
        pass  # No bloquear si Unity falla

    # Obtener ig_user_id del cliente
    conn = get_db()
    ig_row = conn.execute(
        "SELECT ig_user_id FROM client_ig_accounts WHERE client_id=?", (client_id,)
    ).fetchone()
    conn.close()

    if not ig_row:
        return jsonify({'error': f'No hay cuenta IG configurada para cliente {client_id}. Configura en /api/ig-accounts'}), 400

    conn = get_db()
    conn.execute("""
        INSERT INTO scheduled_posts (pieza_id, client_id, ig_user_id, scheduled_at)
        VALUES (?, ?, ?, ?)
    """, (pieza_id, client_id, ig_row['ig_user_id'], scheduled_at))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'scheduled_at': scheduled_at})

@app.route('/api/publicar-ahora', methods=['POST'])
def api_publicar_ahora():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    client_id = data.get('client_id')
    copy_text = data.get('copy', '')

    if not all([pieza_id, client_id]):
        return jsonify({'error': 'pieza_id y client_id requeridos'}), 400

    token = generate_token(session['user'], session['role'])
    try:
        http_requests.put(
            f'{UNITY_URL}/api/piezas/{pieza_id}/copy',
            json={'copy': copy_text},
            params={'token': token},
            timeout=10
        )
    except Exception:
        pass

    conn = get_db()
    ig_row = conn.execute(
        "SELECT ig_user_id FROM client_ig_accounts WHERE client_id=?", (client_id,)
    ).fetchone()
    conn.close()

    if not ig_row:
        return jsonify({'error': 'No hay cuenta IG configurada para este cliente'}), 400

    result = publish_piece(pieza_id, ig_row['ig_user_id'], copy_text, token)
    if result.get('ok'):
        try:
            http_requests.post(f'{UNITY_URL}/api/drop/publicar',
                json={'token': token, 'pieza_id': pieza_id}, timeout=10)
        except Exception:
            pass
    return jsonify(result), 200 if result.get('ok') else 502

@app.route('/api/scheduled')
def api_scheduled():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM scheduled_posts
        WHERE status IN ('pending','failed')
        ORDER BY scheduled_at
    """).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/historia/done', methods=['POST'])
def api_historia_done():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.post(f'{UNITY_URL}/api/drop/publicar',
            json={'token': token, 'pieza_id': pieza_id}, timeout=10)
        return jsonify(r.json()), r.status_code
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/generate-copy', methods=['POST'])
def api_generate_copy():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    pieza_id = data.get('pieza_id')
    if not pieza_id:
        return jsonify({'error': 'pieza_id requerido'}), 400

    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/pieza-detail/{pieza_id}',
            params={'token': token}, timeout=10)
        pieza = r.json() if r.ok else {}
    except Exception:
        pieza = {}

    if not pieza or pieza.get('error'):
        return jsonify({'error': 'No se pudo obtener la pieza desde Unity'}), 502

    cliente = pieza.get('cliente', {})
    sticker_map = {
        'encuesta': 'Encuesta (pregunta + 2 opciones)',
        'slide': 'Slide (frase para deslizar)',
        'box': 'Box (pregunta abierta)',
        'emoji': 'Emoji slider',
        'ubicacion': 'Ubicación',
        'reserva': 'Enlace a reserva',
        'no_usa': 'Sin sticker'
    }
    sticker_tipo = sticker_map.get(pieza.get('stickers', 'no_usa'), 'Sin sticker')

    prompt = f"""Usa la skill blend-copywriter para generar copy de Instagram.

Cliente: {cliente.get('nombre', '')}
- voz_marca: {cliente.get('voz_marca', '')}
- pilar: {pieza.get('pilar', '')}
- restricciones: {cliente.get('restricciones', 'ninguna')}
- horarios: {cliente.get('horarios', '')}
- delivery: {'Sí' if cliente.get('delivery') else 'No'}
- atributo_superior: {cliente.get('atributo_superior', '')}
- url_web: {cliente.get('url_web', '')}

Pieza:
- titulo: {pieza.get('titulo', '')}
- descripcion: {pieza.get('descripcion', '')}
- tipo_pedido: {pieza.get('tipo_pedido', 'grilla')}
- stickers: {pieza.get('stickers', 'no_usa')} ({sticker_tipo})
- pilar: {pieza.get('pilar', '')}

Genera Opción Principal + Opción Alternativa siguiendo las reglas de la skill."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=1024,
            messages=[{'role': 'user', 'content': prompt}]
        )
        return jsonify({'copy': msg.content[0].text})
    except Exception as e:
        return jsonify({'error': str(e)}), 502

@app.route('/api/ig-accounts', methods=['GET'])
def api_ig_accounts_get():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    conn = get_db()
    rows = conn.execute("SELECT * FROM client_ig_accounts ORDER BY client_name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/ig-accounts', methods=['POST'])
def api_ig_accounts_save():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    data = request.json or {}
    if not data.get('client_id') or not data.get('ig_user_id'):
        return jsonify({'error': 'client_id e ig_user_id requeridos'}), 400
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO client_ig_accounts (client_id, client_name, ig_user_id, ig_username, updated_at)
        VALUES (?, ?, ?, ?, datetime('now'))
    """, (data['client_id'], data.get('client_name', ''), data['ig_user_id'], data.get('ig_username', '')))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(OS_URL)

if __name__ == '__main__':
    app.run(debug=True, port=5005)
