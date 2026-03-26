import os
import hmac
import hashlib
import time
import requests as http_requests
from flask import Flask, request, redirect, session, jsonify, send_from_directory

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET', 'blend-drop-secret-2026')

UNITY_URL = os.environ.get('UNITY_URL', 'https://positive-appreciation-production.up.railway.app')
OS_URL = os.environ.get('OS_URL', 'https://socialfood-os-production.up.railway.app')
AUTH_SECRET = 'blendsf-auth-2026'

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

@app.route('/api/cliente-piezas')
def api_cliente_piezas():
    if 'user' not in session:
        return jsonify({'error': 'no auth'}), 401
    client_id = request.args.get('client_id', '')
    mes = request.args.get('mes', '')
    token = generate_token(session['user'], session['role'])
    try:
        r = http_requests.get(f'{UNITY_URL}/api/piezas-publicar-cliente',
            params={'client_id': client_id, 'mes': mes, 'token': token}, timeout=10)
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

@app.route('/logout')
def logout():
    session.clear()
    return redirect(OS_URL)

if __name__ == '__main__':
    app.run(debug=True, port=5005)
