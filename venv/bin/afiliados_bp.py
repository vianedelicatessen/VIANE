"""
PORTAL DE AFILIADOS — módulo 100 % independiente (Blueprint).

No modifica rutas, tablas ni funciones existentes: usa sus propias rutas (/afiliados/...), sus propias plantillas
(templates/afiliados/) y solo LEE las tablas affiliates / resellers / orders / sales; solo escribe en las suyas
(users, affiliate_withdrawals) y, al marcar un retiro como pagado, registra el pago en reseller_payouts para que
el saldo del panel administrativo siga cuadrando.

Seguridad:
  * Solo puede crear cuenta quien ya figure en `affiliates` con estado 'aprobado' (y su WhatsApp coincida con el de
    su solicitud). Si no: "Debes solicitar ser afiliado primero".
  * Todo lo que ve un afiliado se consulta por su user_id (sesión), nunca por parámetros enviados por el navegador.
  * Cuentas con is_active = FALSE no pueden entrar.
Se activa desde app.py con:  afiliados_bp.init_afiliados(app, db=db, migrations=MIGRATIONS, is_admin=is_admin_session)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

import infra

bp = Blueprint('afiliados', __name__, url_prefix='/afiliados', template_folder='templates')
CTX = {'db': None, 'rdb': None, 'is_admin': lambda: False}

PENDING_MSG = 'Tu cuenta está pendiente de aprobación por la administración.'
NOT_AFFILIATE_MSG = 'Debes solicitar ser afiliado primero'
NO_FUNDS_MSG = '🚨 No tienes fondos disponibles. ¡Motívate a seguir publicando más imágenes con tu código de afiliado!'
WITHDRAW_OK_MSG = 'Tus fondos serán transferidos dentro de las próximas 72 horas laborables.'
METODOS = ('Transferencia bancaria', 'PayPal', 'Efectivo / otro')
NOTIFY_TO = (os.environ.get('NOTIFY_EMAIL') or 'vianedelicatessen@gmail.com').strip()
SITE_URL = (os.environ.get('SITE_URL') or 'https://www.vianedelicatessen.com').strip().rstrip('/')

# ---------------------------------------------------------------------------
# SQL (también está en el bloque para pegar en el editor SQL de Supabase)
# ---------------------------------------------------------------------------
DDL = [
    '''CREATE TABLE IF NOT EXISTS public.users(
        id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL, full_name TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '', activated_at TEXT)''',
    "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'cliente'",
    "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE",
    "ALTER TABLE public.users ADD COLUMN IF NOT EXISTS avatar TEXT NOT NULL DEFAULT ''",
    'CREATE UNIQUE INDEX IF NOT EXISTS users_email_uidx ON public.users (lower(email))',
    'ALTER TABLE public.affiliates ADD COLUMN IF NOT EXISTS user_id BIGINT',
    'CREATE UNIQUE INDEX IF NOT EXISTS affiliates_user_uidx ON public.affiliates(user_id) WHERE user_id IS NOT NULL',
    '''CREATE TABLE IF NOT EXISTS public.affiliate_withdrawals(
        id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL, affiliate_id BIGINT NOT NULL,
        reseller_id BIGINT NOT NULL, amount DOUBLE PRECISION NOT NULL, method TEXT NOT NULL,
        bank TEXT NOT NULL DEFAULT '', account TEXT NOT NULL, holder TEXT NOT NULL,
        cedula TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pendiente',
        created_at TEXT NOT NULL, processed_at TEXT, note TEXT NOT NULL DEFAULT '')''',
    'CREATE INDEX IF NOT EXISTS aw_user_idx ON public.affiliate_withdrawals(user_id)',
    'CREATE INDEX IF NOT EXISTS aw_status_idx ON public.affiliate_withdrawals(status)',
    'CREATE INDEX IF NOT EXISTS aw_reseller_idx ON public.affiliate_withdrawals(reseller_id)',
    'ALTER TABLE public.affiliate_withdrawals ENABLE ROW LEVEL SECURITY',
    '''CREATE TABLE IF NOT EXISTS public.affiliate_templates(
        id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
        sort INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)''',
    'ALTER TABLE public.affiliate_templates ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE public.affiliates ADD COLUMN IF NOT EXISTS last_inactivity_mail_at TEXT',
    '''CREATE TABLE IF NOT EXISTS public.affiliate_invites(
        id BIGSERIAL PRIMARY KEY, affiliate_id BIGINT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL, used_at TEXT, created_at TEXT NOT NULL)''',
    'CREATE INDEX IF NOT EXISTS affiliate_invites_aff_idx ON public.affiliate_invites(affiliate_id)',
    'ALTER TABLE public.affiliate_invites ENABLE ROW LEVEL SECURITY',
    '''CREATE TABLE IF NOT EXISTS public.affiliate_sessions(
        id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, refresh_hash TEXT NOT NULL, prev_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL, rotated_at TEXT NOT NULL, revoked_at TEXT,
        ip TEXT NOT NULL DEFAULT '', ua TEXT NOT NULL DEFAULT '')''',
    'CREATE INDEX IF NOT EXISTS idx_afi_sessions_user ON public.affiliate_sessions(user_id)',
    'CREATE INDEX IF NOT EXISTS idx_afi_sessions_exp ON public.affiliate_sessions(expires_at)',
    'ALTER TABLE public.affiliate_sessions ENABLE ROW LEVEL SECURITY',
]

# ---------------------------------------------------------------------------
# Escala de comisiones (según ventas del MES) y coronas
# ---------------------------------------------------------------------------
NIVELES = [
    {'key': 'bronce', 'nombre': 'Corona Bronce', 'desde': 1, 'hasta': 25, 'pct': 3.0, 'extra': '5 % si mantienes ventas el mes siguiente',
     'regalos': 'Sin regalos', 'emoji': '🥉', 'color': '#CD7F32'},
    {'key': 'plata', 'nombre': 'Corona Plata', 'desde': 26, 'hasta': 50, 'pct': 8.0, 'extra': '',
     'regalos': 'Regalo cada 45 días', 'emoji': '🥈', 'color': '#B4BCC4'},
    {'key': 'oro', 'nombre': 'Corona Oro', 'desde': 51, 'hasta': 75, 'pct': 10.0, 'extra': '',
     'regalos': 'Regalo mensual', 'emoji': '🥇', 'color': '#D4AF37'},
    {'key': 'esmeralda', 'nombre': 'Corona Esmeralda', 'desde': 76, 'hasta': 150, 'pct': 15.0, 'extra': '',
     'regalos': 'Regalo cada 21 días', 'emoji': '💚👑', 'color': '#1FA971'},
    {'key': 'diamante', 'nombre': 'Corona Diamante', 'desde': 151, 'hasta': 250, 'pct': 20.0, 'extra': '',
     'regalos': 'Regalo cada 15 días', 'emoji': '💎👑', 'color': '#3FB8F0'},
    {'key': 'imperial', 'nombre': 'Corona Imperial', 'desde': 251, 'hasta': 500, 'pct': 25.0, 'extra': '',
     'regalos': 'Regalo cada 12 días', 'emoji': '🔥👑', 'color': '#E5471B'},
    {'key': 'rey', 'nombre': 'Corona Viane Delicatessen Rey', 'desde': 501, 'hasta': None, 'pct': 30.0, 'extra': '',
     'regalos': 'Regalo cada 7 días', 'emoji': '👑✨', 'color': '#C9A227'},
]
NIVEL_INICIO = {'key': 'inicio', 'nombre': 'Aún sin corona', 'desde': 0, 'hasta': 0, 'pct': 3.0, 'extra': '',
                'regalos': 'Sin regalos', 'emoji': '✨', 'color': '#B9B2A8'}

DR = timezone(timedelta(hours=-4))  # hora de República Dominicana


def ahora():
    return datetime.now(DR).replace(microsecond=0).isoformat()


def meses():
    """(mes actual, mes anterior) como 'YYYY-MM'."""
    hoy = datetime.now(DR)
    y, m = hoy.year, hoy.month - 1
    if m == 0:
        y, m = y - 1, 12
    return hoy.strftime('%Y-%m'), f'{y:04d}-{m:02d}'


def nivel_para(ventas, previas):
    """Nivel y % de comisión según las ventas del mes (y si mantuvo ventas el mes anterior)."""
    if ventas <= 0:
        n = dict(NIVEL_INICIO)
    else:
        n = dict(next(x for x in NIVELES if x['desde'] <= ventas and (x['hasta'] is None or ventas <= x['hasta'])))
    if n['key'] in ('inicio', 'bronce') and previas >= 1:
        n['pct'] = 5.0  # Bronce: 5 % si se mantiene el mes siguiente
    return n


def siguiente_nivel(ventas):
    for x in NIVELES:
        if x['desde'] > ventas:
            return {'nombre': x['nombre'], 'emoji': x['emoji'], 'desde': x['desde'], 'faltan': x['desde'] - ventas, 'pct': x['pct']}
    return None


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
_FAILS = {}


def _ip():
    return (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()) or '?'


def _blocked(ip):
    now = time.time()
    _FAILS[ip] = [t for t in _FAILS.get(ip, []) if now - t < 900]
    return len(_FAILS[ip]) >= 8


def _fail(ip):
    _FAILS.setdefault(ip, []).append(time.time())


def _json():
    d = request.get_json(silent=True)  # solo JSON: un formulario de otro sitio no puede enviarlo
    return d if isinstance(d, dict) else None


def _txt(v, n):
    return re.sub(r'\s+', ' ', str(v or '')).strip()[:n]


def usuario_actual():
    """Afiliado con sesión válida y cuenta activa (o None): access token (cookie o Bearer) o, de respaldo, la sesión clásica."""
    token = _token_actual()
    claims = jwt_decode(token) if token else None
    with CTX['db']() as c:
        if claims:   # el token es válido, pero se confirma en la base: sesión no revocada ni vencida y cuenta activa
            u = c.execute("SELECT u.id,u.email,u.full_name,u.is_active,u.avatar FROM users u JOIN affiliate_sessions s ON s.user_id=u.id "
                          "WHERE u.id=%s AND u.role='afiliado' AND s.id=%s AND s.revoked_at IS NULL AND s.expires_at > %s",
                          (claims.get('sub'), claims.get('sid'), infra.utc())).fetchone()
        elif session.get('uid') and session.get('role') == 'afiliado':
            u = c.execute("SELECT id,email,full_name,is_active,avatar FROM users WHERE id=%s AND role='afiliado'", (session['uid'],)).fetchone()
        else:
            return None
    return u if u and u['is_active'] else None


def solo_afiliado(fn):
    @wraps(fn)
    def wrapped(*a, **k):
        u = usuario_actual()
        if not u:
            return jsonify({'error': 'Inicia sesión para continuar.'}), 401
        return fn(u, *a, **k)
    return wrapped


def solo_admin(fn):
    @wraps(fn)
    def wrapped(*a, **k):
        if not CTX['is_admin']():
            return jsonify({'error': 'Acceso administrativo requerido'}), 401
        return fn(*a, **k)
    return wrapped


def metricas(c, rid):
    """Ventas del mes, comisiones y saldo de UN vendedor (rid sale del user_id de la sesión)."""
    mes, prev = meses()

    def n(m):
        return int(c.execute('SELECT COUNT(*) AS v FROM orders o JOIN sales s ON s.order_id=o.id WHERE o.reseller_id=%s '
                             'AND s.deleted_at IS NULL AND LEFT(s.sold_date,7)=%s', (rid, m)).fetchone()['v'])

    def suma(sql, *p):
        return float(c.execute(sql, p).fetchone()['v'])
    ganado = suma('SELECT COALESCE(SUM(o.commission),0) AS v FROM orders o JOIN sales s ON s.order_id=o.id '
                  'WHERE o.reseller_id=%s AND s.deleted_at IS NULL', rid)
    delmes = suma('SELECT COALESCE(SUM(o.commission),0) AS v FROM orders o JOIN sales s ON s.order_id=o.id '
                  'WHERE o.reseller_id=%s AND s.deleted_at IS NULL AND LEFT(s.sold_date,7)=%s', rid, mes)
    pagado = suma('SELECT COALESCE(SUM(amount),0) AS v FROM reseller_payouts WHERE reseller_id=%s', rid)
    proceso = suma("SELECT COALESCE(SUM(amount),0) AS v FROM affiliate_withdrawals WHERE reseller_id=%s AND status='pendiente'", rid)
    ventas, previas = n(mes), n(prev)
    return {'mes': mes, 'ventas_mes': ventas, 'ventas_previas': previas, 'ganado': round(ganado, 2),
            'comision_mes': round(delmes, 2), 'pagado': round(pagado, 2), 'en_proceso': round(proceso, 2),
            'disponible': round(max(ganado - pagado - proceso, 0.0), 2)}


def codigo_nuevo(c):
    alfabeto = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    while True:
        code = 'AFI-' + ''.join(secrets.choice(alfabeto) for _ in range(6))
        if not c.execute('SELECT 1 FROM resellers WHERE code=%s', (code,)).fetchone():
            return code


def asegurar_codigo(c, aff):
    """Si el afiliado aprobado aún no tiene código de descuento, se le genera uno."""
    if aff['reseller_id']:
        return aff['reseller_id']
    row = c.execute('INSERT INTO resellers(name,code,active,created_at,discount_pct,commission_pct) '
                    'VALUES(%s,%s,1,%s,%s,%s) RETURNING id',
                    (aff['full_name'], codigo_nuevo(c), ahora(), 3.0, NIVEL_INICIO['pct'])).fetchone()
    c.execute('UPDATE affiliates SET reseller_id=%s WHERE id=%s', (row['id'], aff['id']))
    return row['id']


def sincronizar(c, rid):
    """Actualiza el % de comisión del código del afiliado según su nivel actual."""
    if not c.execute("SELECT 1 FROM affiliates WHERE reseller_id=%s AND status='aprobado' AND rights_revoked=0", (rid,)).fetchone():
        return None  # vendedores creados a mano por el administrador no se tocan
    m = metricas(c, rid)
    nv = nivel_para(m['ventas_mes'], m['ventas_previas'])
    c.execute('UPDATE resellers SET commission_pct=%s WHERE id=%s AND (commission_pct IS NULL OR commission_pct<>%s)',
              (nv['pct'], rid, nv['pct']))
    return nv


@bp.before_app_request
def _al_crear_pedido():
    """Antes de crear un pedido con código de afiliado, se deja su % de comisión al nivel vigente."""
    if request.method != 'POST' or request.path != '/api/orders':
        return
    d = request.get_json(silent=True)
    code = str(d.get('reseller_code') or '').strip().upper() if isinstance(d, dict) else ''
    if not code:
        return
    try:
        with CTX['db']() as c:
            r = c.execute('SELECT id FROM resellers WHERE code=%s', (code,)).fetchone()
            if r:
                sincronizar(c, r['id'])
    except Exception:
        pass  # nunca debe impedir un pedido


@bp.after_request
def _sin_cache(resp):
    if request.endpoint and not request.endpoint.endswith('comisiones'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


# ---------------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------------
@bp.get('/')
def inicio():
    return redirect('/afiliados/panel' if usuario_actual() else '/afiliados/login')


@bp.get('/login')
def pagina_login():
    return render_template('afiliados/login.html')


@bp.get('/panel')
def pagina_panel():
    if not (request.cookies.get(CK_ACCESS) or usuario_actual()):
        return redirect('/afiliados/login')
    return render_template('afiliados/panel.html')


@bp.get('/comisiones')
def pagina_comisiones():
    return render_template('afiliados/comisiones.html', niveles=NIVELES)


@bp.get('/admin/retiros')
def pagina_admin_retiros():
    if not CTX['is_admin']():
        return redirect('/#admin')
    return render_template('afiliados/admin_retiros.html')


# ---------------------------------------------------------------------------
# Sesión con tokens: ACCESS TOKEN (JWT HS256, corto) + REFRESH TOKEN (opaco, rotatorio) — sesión total de 7 días
#   • El access token va en una cookie httpOnly (o en "Authorization: Bearer ..." para apps/clientes API) y se
#     revalida en la base en cada petición: cerrar sesión o desactivar a la persona lo anula al instante.
#   • El refresh token solo se guarda como HASH. Cada renovación lo ROTA; si alguien reutiliza uno viejo, se cierra
#     toda la sesión (detección de robo). La sesión nunca dura más de 7 días desde el inicio de sesión.
#   AFI_ACCESS_TTL (segundos, 900 = 15 min por defecto) · AFI_REFRESH_DAYS (7) · JWT_SECRET (si no, SECRET_KEY)
# ---------------------------------------------------------------------------
ACCESS_TTL = int(os.environ.get('AFI_ACCESS_TTL') or 900)
REFRESH_DAYS = int(os.environ.get('AFI_REFRESH_DAYS') or 7)
REFRESH_GRACE = 10  # segundos: dos pestañas que renuevan a la vez no se consideran robo
CK_ACCESS, CK_REFRESH = 'afi_at', 'afi_rt'
REFRESH_PATH = '/afiliados/api/refresh'


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b'=').decode()


def _unb64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def _jwt_key():
    return (os.environ.get('JWT_SECRET') or str(current_app.config.get('SECRET_KEY') or '')).encode('utf-8')


def _firma(texto):
    return _b64(hmac.new(_jwt_key(), texto.encode('ascii'), hashlib.sha256).digest())


def jwt_encode(claims):
    head = _b64(json.dumps({'alg': 'HS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    body = _b64(json.dumps(claims, separators=(',', ':')).encode())
    return f'{head}.{body}.{_firma(head + "." + body)}'


def jwt_decode(token, check_exp=True):
    """Claims del access token, o None si la firma, el tipo o la vigencia no son válidos."""
    try:
        head, body, sig = str(token).split('.')
        if json.loads(_unb64(head)).get('alg') != 'HS256' or not hmac.compare_digest(sig, _firma(head + '.' + body)):
            return None
        claims = json.loads(_unb64(body))
        if claims.get('typ') != 'access' or (check_exp and int(claims.get('exp', 0)) < int(time.time())):
            return None
        return claims
    except Exception:
        return None


def _h(texto):
    return hashlib.sha256(str(texto).encode('utf-8')).hexdigest()


def _ts(iso):
    return datetime.strptime(iso, '%Y-%m-%dT%H:%M:%S').replace(tzinfo=timezone.utc)


def _crear_sesion(c, uid):
    sid, secret, exp = uuid.uuid4().hex, secrets.token_urlsafe(48), infra.utc(REFRESH_DAYS * 86400)
    c.execute('INSERT INTO affiliate_sessions(id,user_id,refresh_hash,created_at,expires_at,rotated_at,ip,ua) '
              'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
              (sid, uid, _h(secret), infra.utc(), exp, infra.utc(), _ip()[:60], request.headers.get('User-Agent', '')[:160]))
    return sid, secret, exp


def _tokens(uid, sid, secret, exp_iso):
    now = int(time.time())
    return {'token_type': 'Bearer', 'expires_in': ACCESS_TTL,
            'access_token': jwt_encode({'sub': uid, 'sid': sid, 'typ': 'access', 'iat': now, 'exp': now + ACCESS_TTL, 'jti': secrets.token_hex(6)}),
            'refresh_token': f'{sid}.{secret}' if secret else None,
            'refresh_expires_in': max(int((_ts(exp_iso) - datetime.now(timezone.utc)).total_seconds()), 0)}


def _poner_cookies(resp, t):
    secure = request.is_secure or request.headers.get('X-Forwarded-Proto', '') == 'https'
    # la cookie del access token vive lo que la sesión: así /afiliados/panel puede abrirse y renovarse sola
    resp.set_cookie(CK_ACCESS, t['access_token'], max_age=t['refresh_expires_in'], httponly=True, secure=secure, samesite='Lax', path='/afiliados')
    if t['refresh_token']:   # solo viaja al endpoint de renovación
        resp.set_cookie(CK_REFRESH, t['refresh_token'], max_age=t['refresh_expires_in'], httponly=True, secure=secure, samesite='Lax', path=REFRESH_PATH)
    return resp


def _borrar_cookies(resp):
    resp.delete_cookie(CK_ACCESS, path='/afiliados')
    resp.delete_cookie(CK_REFRESH, path=REFRESH_PATH)
    return resp


def respuesta_sesion(uid, quiere_tokens=False, **extra):
    """Abre la sesión de 7 días y responde. Navegador: tokens solo en cookies httpOnly. Apps/API: "tokens": true los devuelve en el JSON."""
    with CTX['db']() as c:
        sid, secret, exp = _crear_sesion(c, uid)
    t = _tokens(uid, sid, secret, exp)
    body = {'ok': True, 'redirect': '/afiliados/panel', 'token_type': 'Bearer', 'expires_in': t['expires_in'],
            'refresh_expires_in': t['refresh_expires_in'], **extra}
    if quiere_tokens:
        body.update(access_token=t['access_token'], refresh_token=t['refresh_token'])
    return _poner_cookies(jsonify(body), t)


def adjuntar_sesion(resp, uid):
    """Para el login general de la tienda (app.py): añade las cookies de sesión de 7 días a su respuesta."""
    with CTX['db']() as c:
        sid, secret, exp = _crear_sesion(c, uid)
    return _poner_cookies(resp, _tokens(uid, sid, secret, exp))


def _token_actual():
    h = request.headers.get('Authorization', '')
    return h[7:].strip() if h.startswith('Bearer ') else request.cookies.get(CK_ACCESS, '')


def cerrar_sesion_actual():
    """Revoca la sesión del access token presente (aunque ya haya vencido)."""
    claims = jwt_decode(_token_actual(), check_exp=False)
    if claims:
        with CTX['db']() as c:
            c.execute('UPDATE affiliate_sessions SET revoked_at=%s WHERE id=%s AND revoked_at IS NULL', (infra.utc(), claims.get('sid')))


@bp.post('/api/refresh')
def api_refresh():
    """Canjea el refresh token (cookie httpOnly o {"refresh_token": "..."}) por un access token nuevo y rota el refresh."""
    d = _json()
    if d is None:
        return jsonify({'error': 'Solicitud no válida.'}), 400
    ip = _ip()
    if _blocked(ip):
        return jsonify({'error': 'Demasiados intentos. Espera 15 minutos.'}), 429
    sid, _, secret = (request.cookies.get(CK_REFRESH) or str(d.get('refresh_token') or '')).partition('.')

    def rechazo(msg='Sesión vencida. Inicia sesión de nuevo.'):
        return _borrar_cookies(jsonify({'error': msg})), 401
    if not sid or not secret:
        _fail(ip)
        return rechazo()
    with CTX['db']() as c:
        s = c.execute('SELECT id,user_id,refresh_hash,prev_hash,expires_at,rotated_at,revoked_at FROM affiliate_sessions WHERE id=%s', (sid,)).fetchone()
        if not s or s['revoked_at'] or s['expires_at'] <= infra.utc():
            _fail(ip)
            return rechazo()
        u = c.execute("SELECT is_active FROM users WHERE id=%s AND role='afiliado'", (s['user_id'],)).fetchone()
        if not u or not u['is_active']:
            return rechazo('Tu cuenta no está activa.')
        h, nuevo = _h(secret), None
        if hmac.compare_digest(h, s['refresh_hash']):
            nuevo = secrets.token_urlsafe(48)   # rotación: el token usado deja de servir
            c.execute('UPDATE affiliate_sessions SET refresh_hash=%s, prev_hash=%s, rotated_at=%s WHERE id=%s', (_h(nuevo), s['refresh_hash'], infra.utc(), sid))
        elif s['prev_hash'] and hmac.compare_digest(h, s['prev_hash']):
            if (datetime.now(timezone.utc) - _ts(s['rotated_at'])).total_seconds() > REFRESH_GRACE:
                c.execute('UPDATE affiliate_sessions SET revoked_at=%s WHERE id=%s', (infra.utc(), sid))   # token viejo reutilizado: posible robo
                _fail(ip)
                return rechazo('Sesión cerrada por seguridad. Inicia sesión de nuevo.')
            # dentro de la gracia (otra pestaña renovó hace segundos): solo se da un access nuevo, el refresh vigente ya lo tiene el navegador
        else:
            _fail(ip)
            return rechazo()
    t = _tokens(s['user_id'], sid, nuevo, s['expires_at'])
    body = {'ok': True, 'token_type': 'Bearer', 'expires_in': t['expires_in'], 'refresh_expires_in': t['refresh_expires_in']}
    if d.get('tokens'):
        body.update(access_token=t['access_token'], refresh_token=t['refresh_token'])
    resp = jsonify(body)
    if nuevo is None:   # sin refresh nuevo: solo se renueva la cookie del access token
        secure = request.is_secure or request.headers.get('X-Forwarded-Proto', '') == 'https'
        resp.set_cookie(CK_ACCESS, t['access_token'], max_age=t['refresh_expires_in'], httponly=True, secure=secure, samesite='Lax', path='/afiliados')
        return resp
    return _poner_cookies(resp, t)


@bp.post('/api/logout-all')
@solo_afiliado
def api_logout_all(u):
    """Cierra la sesión en TODOS los dispositivos."""
    with CTX['db']() as c:
        c.execute('UPDATE affiliate_sessions SET revoked_at=%s WHERE user_id=%s AND revoked_at IS NULL', (infra.utc(), u['id']))
    session.clear()
    return _borrar_cookies(jsonify({'ok': True}))


# ---------------------------------------------------------------------------
# Registro / login / salida (aislados)
# ---------------------------------------------------------------------------
@bp.post('/api/registro')
def api_registro():
    d = _json()
    if d is None:
        return jsonify({'error': 'Solicitud no válida.'}), 400
    ip = _ip()
    if _blocked(ip):
        return jsonify({'error': 'Demasiados intentos. Espera 15 minutos.'}), 429
    email = _txt(d.get('email'), 120).lower()
    phone = re.sub(r'\D', '', str(d.get('phone') or ''))[-10:]
    pwd = str(d.get('password') or '')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'error': 'Escribe un correo válido.'}), 400
    if len(pwd) < 8:
        return jsonify({'error': 'La contraseña debe tener al menos 8 caracteres.'}), 400
    with CTX['db']() as c:
        aff = c.execute("SELECT id,full_name,phone,user_id,reseller_id FROM affiliates WHERE LOWER(email)=%s "
                        "AND status='aprobado' AND rights_revoked=0 ORDER BY id DESC LIMIT 1", (email,)).fetchone()
        if not aff:  # no existe o no está aprobado
            _fail(ip)
            return jsonify({'error': NOT_AFFILIATE_MSG, 'solicitar': True}), 403
        if re.sub(r'\D', '', aff['phone'] or '')[-10:] != phone:  # evita que otra persona reclame tu correo
            _fail(ip)
            return jsonify({'error': 'El WhatsApp no coincide con el de tu solicitud de afiliado.'}), 400
        if aff['user_id'] or c.execute('SELECT 1 FROM users WHERE lower(email)=%s', (email,)).fetchone():
            return jsonify({'error': 'Ya existe una cuenta con ese correo. Inicia sesión.'}), 400
        uid = c.execute("INSERT INTO users(email,full_name,password_hash,role,is_active,created_by,created_at,activated_at) "
                        "VALUES(%s,%s,%s,'afiliado',TRUE,'registro-afiliado',%s,%s) RETURNING id",
                        (email, aff['full_name'], generate_password_hash(pwd), ahora(), ahora())).fetchone()['id']
        c.execute('UPDATE affiliates SET user_id=%s WHERE id=%s', (uid, aff['id']))
        asegurar_codigo(c, aff)
    session.clear()
    return respuesta_sesion(uid, bool(d.get('tokens')))


@bp.post('/api/login')
def api_login():
    d = _json()
    if d is None:
        return jsonify({'error': 'Solicitud no válida.'}), 400
    ip = _ip()
    if _blocked(ip):
        return jsonify({'error': 'Demasiados intentos. Espera 15 minutos.'}), 429
    email = _txt(d.get('email'), 120).lower()
    pwd = str(d.get('password') or '')
    with CTX['db']() as c:
        u = c.execute("SELECT id,email,password_hash,is_active FROM users WHERE lower(email)=%s AND role='afiliado'", (email,)).fetchone()
    if not u or not u['password_hash'] or not check_password_hash(u['password_hash'], pwd):
        _fail(ip)
        return jsonify({'error': 'Correo o contraseña incorrectos. ¿Aún no tienes cuenta? Créala en «Crear cuenta».'}), 401
    if not u['is_active']:
        return jsonify({'error': PENDING_MSG, 'pending': True}), 403
    _FAILS.pop(ip, None)
    session.clear()
    return respuesta_sesion(u['id'], bool(d.get('tokens')))


@bp.post('/api/logout')
def api_logout():
    cerrar_sesion_actual()   # el access token (aunque no haya vencido) y el refresh dejan de servir ya
    session.clear()
    return _borrar_cookies(jsonify({'ok': True}))


# ---------------------------------------------------------------------------
# Panel del afiliado (todo filtrado por su user_id)
# ---------------------------------------------------------------------------
@bp.get('/api/panel')
@solo_afiliado
def api_panel(u):
    with CTX['db']() as c:
        aff = c.execute('SELECT id,full_name,status,rights_revoked,reseller_id FROM affiliates WHERE user_id=%s '
                        'ORDER BY id DESC LIMIT 1', (u['id'],)).fetchone()
        activo = bool(aff and aff['status'] == 'aprobado' and not aff['rights_revoked'])
        base = {'usuario': {'nombre': u['full_name'], 'email': u['email'], 'avatar': u['avatar']},
                'niveles': NIVELES, 'activo': activo}
        if not activo:
            return jsonify({**base, 'mensaje': 'Tu afiliación no está activa. Escríbenos por WhatsApp si crees que es un error.'})
        rid = asegurar_codigo(c, aff)
        nv = sincronizar(c, rid) or NIVEL_INICIO
        m = metricas(c, rid)
        r = c.execute('SELECT code,discount_pct FROM resellers WHERE id=%s', (rid,)).fetchone()
        retiros = c.execute('SELECT id,amount,method,status,created_at,processed_at FROM affiliate_withdrawals '
                            'WHERE user_id=%s ORDER BY id DESC LIMIT 10', (u['id'],)).fetchall()
    return jsonify({**base, 'codigo': r['code'], 'descuento_pct': r['discount_pct'], 'nivel': nv,
                    'siguiente': siguiente_nivel(m['ventas_mes']), **m, 'retiros': retiros, 'metodos': METODOS})


@bp.post('/api/avatar')
@solo_afiliado
def api_avatar(u):
    d = _json() or {}
    img = str(d.get('avatar') or '')
    if not re.fullmatch(r'data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+', img) or len(img) > 200_000:
        return jsonify({'error': 'La foto no es válida o es demasiado pesada.'}), 400
    with CTX['db']() as c:
        c.execute('UPDATE users SET avatar=%s WHERE id=%s', (img, u['id']))
    return jsonify({'ok': True})


@bp.post('/api/retiro')
@solo_afiliado
def api_retiro(u):
    d = _json() or {}
    with CTX['db']() as c:
        c.execute('SELECT pg_advisory_xact_lock(%s)', (7_300_000 + int(u['id']),))  # evita dobles solicitudes simultáneas
        aff = c.execute("SELECT id,reseller_id FROM affiliates WHERE user_id=%s AND status='aprobado' AND rights_revoked=0 "
                        'ORDER BY id DESC LIMIT 1', (u['id'],)).fetchone()
        if not aff or not aff['reseller_id']:
            return jsonify({'error': 'Tu afiliación no está activa.'}), 403
        m = metricas(c, aff['reseller_id'])
        if m['disponible'] <= 0:
            return jsonify({'error': NO_FUNDS_MSG, 'sin_fondos': True}), 400
        metodo, cuenta, titular = _txt(d.get('method'), 40), _txt(d.get('account'), 120), _txt(d.get('holder'), 100)
        if metodo not in METODOS or len(cuenta) < 3 or len(titular) < 3:
            return jsonify({'error': 'Completa el método, la cuenta y el nombre del titular.'}), 400
        c.execute('INSERT INTO affiliate_withdrawals(user_id,affiliate_id,reseller_id,amount,method,bank,account,holder,cedula,status,created_at) '
                  "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'pendiente',%s)",
                  (u['id'], aff['id'], aff['reseller_id'], m['disponible'], metodo, _txt(d.get('bank'), 80), cuenta,
                   titular, _txt(d.get('cedula'), 30), ahora()))
        infra.enqueue_mail(c, NOTIFY_TO, f"Solicitud de retiro: {u['full_name']} — RD$ {m['disponible']:,.2f}",
                           f"{u['full_name']} ({u['email']}) solicitó retirar RD$ {m['disponible']:,.2f} por {metodo}.\n"
                           f"Revísalo en {SITE_URL}/afiliados/admin/retiros")
    return jsonify({'ok': True, 'mensaje': WITHDRAW_OK_MSG, 'monto': m['disponible']})


# ---------------------------------------------------------------------------
# Retiros: vista del administrador (página propia, no toca el panel actual)
# ---------------------------------------------------------------------------
@bp.get('/api/admin/retiros')
@solo_admin
def api_admin_retiros():
    with CTX['db']() as c:
        data = c.execute("SELECT w.id,w.amount,w.method,w.bank,w.account,w.holder,w.cedula,w.status,w.created_at,w.processed_at,"
                         "COALESCE(a.full_name,'') AS afiliado FROM affiliate_withdrawals w LEFT JOIN affiliates a ON a.id=w.affiliate_id "
                         "ORDER BY (w.status='pendiente') DESC, w.id DESC LIMIT 200").fetchall()
    return jsonify({'retiros': data})


@bp.post('/api/admin/retiros/<int:wid>/pagar')
@solo_admin
def api_admin_pagar(wid):
    with CTX['db']() as c:
        w = c.execute("SELECT * FROM affiliate_withdrawals WHERE id=%s AND status='pendiente'", (wid,)).fetchone()
        if not w:
            return jsonify({'error': 'La solicitud no existe o ya fue procesada.'}), 404
        r = c.execute('SELECT name FROM resellers WHERE id=%s', (w['reseller_id'],)).fetchone()
        c.execute('INSERT INTO reseller_payouts(reseller_id,reseller_name,amount,method,account,holder,cedula,paid_on,created_at) '
                  'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (w['reseller_id'], r['name'] if r else '', w['amount'], w['method'], w['account'], w['holder'],
                   w['cedula'], datetime.now(DR).date().isoformat(), ahora()))
        c.execute("UPDATE affiliate_withdrawals SET status='pagado', processed_at=%s WHERE id=%s", (ahora(), wid))
    return jsonify({'ok': True})


@bp.post('/api/admin/retiros/<int:wid>/rechazar')
@solo_admin
def api_admin_rechazar(wid):
    d = _json() or {}
    with CTX['db']() as c:
        cur = c.execute("UPDATE affiliate_withdrawals SET status='rechazado', processed_at=%s, note=%s WHERE id=%s AND status='pendiente'",
                        (ahora(), _txt(d.get('note'), 300), wid))
        if cur.rowcount != 1:
            return jsonify({'error': 'La solicitud no existe o ya fue procesada.'}), 404
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Catálogo en vivo para afiliados + plantillas de mensajes + recordatorios por inactividad
# ---------------------------------------------------------------------------
DEFAULT_TEMPLATE_BODY = (
    "Entra a: *www.vianedelicatessen.com*\n"
    "🎁 ¡Usa este código todas las veces que quieras!\n"
    "Aplica *```{codigo_afiliado}```* en tus compras y obtén un descuento especial cada vez. "
    "¡Compártelo con quien quieras! ✨\n"
    "*www.vianedelicatessen.com*"
)
PLACEHOLDER = '{codigo_afiliado}'
DIAS_INACTIVIDAD = 7
MAX_CORREOS_POR_CORRIDA = 25  # evita pasarse del tiempo de la función serverless


def _abs(u):
    u = str(u or '')
    return SITE_URL + u if u.startswith('/') else u


def _catalogo():
    """Lista de productos (réplica de lectura). Se cachea 30 s y se invalida sola cuando el panel cambia algo."""
    def load():
        with CTX['rdb']() as c:
            data = c.execute('SELECT p.id,p.name,p.description,p.price,p.image,p.category,p.created_at FROM products p '
                             'WHERE p.active=1 AND NOT EXISTS (SELECT 1 FROM categories k WHERE k.name=p.category AND k.active=0) '
                             'ORDER BY p.id DESC LIMIT 200').fetchall()
        hoy, items = datetime.now(DR), []
        for p in data:
            try:
                nuevo = (hoy - datetime.fromisoformat(str(p['created_at']))).days < 3
            except ValueError:
                nuevo = False
            items.append({'id': p['id'], 'nombre': p['name'], 'descripcion': p['description'], 'precio': float(p['price']),
                          'categoria': p['category'], 'imagen': _abs(p['image']), 'url': f"{SITE_URL}/?p={p['id']}", 'nuevo': nuevo})
        return items
    return infra.cache.cached('data', 'afi_catalogo', 30, load)


@bp.get('/api/productos')
@solo_afiliado
def api_productos(u):
    """Catálogo vivo: un producto recién publicado aparece al instante (el panel del admin invalida la caché).
    El navegador pregunta cada ~20 s con ?v=<versión> y solo recibe la lista si algo cambió."""
    items = _catalogo()
    version = hashlib.sha1(json.dumps([(i['id'], i['nombre'], i['precio'], i['imagen']) for i in items]).encode()).hexdigest()[:12]
    if request.args.get('v') == version:
        return jsonify({'cambio': False, 'version': version})
    return jsonify({'cambio': True, 'version': version, 'productos': items})


@bp.get('/api/plantillas')
@solo_afiliado
def api_plantillas(u):
    def load():
        with CTX['rdb']() as c:
            return c.execute('SELECT id,title,body FROM affiliate_templates WHERE active=1 ORDER BY sort,id').fetchall()
    data = infra.cache.cached('data', 'afi_plantillas', 60, load) or [{'id': 0, 'title': 'Mensaje estándar', 'body': DEFAULT_TEMPLATE_BODY}]
    return jsonify({'plantillas': data})


def _plantilla(d):
    title, body = _txt(d.get('title'), 80), str(d.get('body') or '').strip()[:1500]
    if len(title) < 2:
        return None, 'Escribe un título para la plantilla.'
    if PLACEHOLDER not in body:
        return None, 'El texto debe incluir ' + PLACEHOLDER + ' para insertar el código de cada afiliado.'
    return (title, body), None


@bp.get('/api/admin/plantillas')
@solo_admin
def api_admin_plantillas():
    with CTX['db']() as c:
        data = c.execute('SELECT id,title,body,active,sort FROM affiliate_templates ORDER BY sort,id').fetchall()
    return jsonify({'plantillas': data, 'por_defecto': DEFAULT_TEMPLATE_BODY})


@bp.post('/api/admin/plantillas')
@solo_admin
def api_admin_plantilla_crear():
    d = _json() or {}
    val, err = _plantilla(d)
    if err:
        return jsonify({'error': err}), 400
    with CTX['db']() as c:
        row = c.execute('INSERT INTO affiliate_templates(title,body,active,sort,created_at) VALUES(%s,%s,%s,%s,%s) RETURNING id',
                        (val[0], val[1], 1 if d.get('active', True) else 0, int(d.get('sort') or 0), ahora())).fetchone()
    return jsonify({'ok': True, 'id': row['id']})


@bp.put('/api/admin/plantillas/<int:tid>')
@solo_admin
def api_admin_plantilla_editar(tid):
    d = _json() or {}
    val, err = _plantilla(d)
    if err:
        return jsonify({'error': err}), 400
    with CTX['db']() as c:
        cur = c.execute('UPDATE affiliate_templates SET title=%s,body=%s,active=%s,sort=%s WHERE id=%s',
                        (val[0], val[1], 1 if d.get('active', True) else 0, int(d.get('sort') or 0), tid))
        if cur.rowcount != 1:
            return jsonify({'error': 'Plantilla no encontrada.'}), 404
    return jsonify({'ok': True})


@bp.delete('/api/admin/plantillas/<int:tid>')
@solo_admin
def api_admin_plantilla_borrar(tid):
    with CTX['db']() as c:
        c.execute('DELETE FROM affiliate_templates WHERE id=%s', (tid,))
    return jsonify({'ok': True})


# ---- Recordatorio motivacional por inactividad (ideal para un Cron Job diario). Los correos van a la cola (infra.py) ----
def dias_inactivo(ultima_venta, aprobado_en, hoy):
    """Días desde la última venta (o desde su aprobación si nunca vendió). None si no hay ninguna fecha."""
    fechas = []
    for v in (ultima_venta, aprobado_en):
        try:
            fechas.append(datetime.fromisoformat(str(v)[:10]).date())
        except (ValueError, TypeError):
            pass
    return (hoy - max(fechas)).days if fechas else None


def texto_inactividad(nombre, codigo, dias):
    escala = '\n'.join(f"  {n['emoji']} {n['nombre']}: {('de ' + str(n['desde']) + ' a ' + str(n['hasta'])) if n['hasta'] else 'más de 500'} ventas al mes → {n['pct']:g} % de comisión · {n['regalos']}"
                       for n in NIVELES)
    return (f"¡Hola {(nombre or '').split(' ')[0] or 'afiliado/a'}! 👋\n\n"
            f"Hace {dias} días que no registramos ventas con tu código de afiliado y queremos verte brillar. ✨\n\n"
            f"🎁 Tu código: {codigo or '(escríbenos y te lo activamos)'}\n"
            "Quien compra con él recibe un descuento especial, y tú ganas comisión por cada venta.\n\n"
            f"Tus beneficios, según las ventas del mes:\n{escala}\n\n"
            "💡 Hoy mismo puedes copiar la imagen de un producto con tu mensaje listo y compartirla en tus redes:\n"
            f"{SITE_URL}/afiliados/panel\n\n"
            "¡Cada publicación cuenta! Gracias por ser parte de VIANE DELICATESSEN.")


@bp.route('/api/cron/inactividad', methods=['GET', 'POST'])
def api_cron_inactividad():
    """Protegido: lo llama Vercel Cron con 'Authorization: Bearer <CRON_SECRET>' (o un administrador con sesión).
    ?dry=1 solo muestra a quién se enviaría, sin enviar nada."""
    secret = os.environ.get('CRON_SECRET', '').strip()
    auth = request.headers.get('Authorization', '')
    if not ((secret and hmac.compare_digest(auth.encode('utf-8'), ('Bearer ' + secret).encode('utf-8'))) or CTX['is_admin']()):
        return jsonify({'error': 'No autorizado'}), 401
    dry = request.args.get('dry') == '1'
    hoy = datetime.now(DR).date()
    enviados, candidatos = [], []
    with CTX['db']() as c:
        filas = c.execute(
            "SELECT a.id,a.full_name,a.email,a.decided_at,a.last_inactivity_mail_at,r.code,"
            "(SELECT MAX(s.sold_date) FROM sales s JOIN orders o ON o.id=s.order_id "
            " WHERE o.reseller_id=a.reseller_id AND s.deleted_at IS NULL) AS ultima_venta "
            "FROM affiliates a LEFT JOIN resellers r ON r.id=a.reseller_id "
            "WHERE a.status='aprobado' AND a.rights_revoked=0").fetchall()
        for a in filas:
            dias = dias_inactivo(a['ultima_venta'], a['decided_at'], hoy)
            if dias is None or dias <= DIAS_INACTIVIDAD:
                continue
            previo = dias_inactivo(None, a['last_inactivity_mail_at'], hoy)
            if previo is not None and previo < DIAS_INACTIVIDAD:  # ya se le escribió esta semana
                continue
            candidatos.append(a)
        for a in candidatos[:MAX_CORREOS_POR_CORRIDA]:
            if dry:
                enviados.append(a['email'])
                continue
            dias = dias_inactivo(a['ultima_venta'], a['decided_at'], hoy)
            if infra.enqueue_mail(c, a['email'], '¡Te extrañamos! Tu código de afiliado te espera 🎁',
                                  texto_inactividad(a['full_name'], a['code'], dias)) in ('en cola', 'enviado'):
                c.execute('UPDATE affiliates SET last_inactivity_mail_at=%s WHERE id=%s', (ahora(), a['id']))
                enviados.append(a['email'])
    return jsonify({'ok': True, 'simulacro': dry, 'revisados': len(filas), 'con_inactividad': len(candidatos),
                    'enviados' if not dry else 'se_enviaria_a': enviados})


# ---------------------------------------------------------------------------
# Acceso por enlace de correo: al aprobar a un afiliado le llega un enlace personal (7 días, un solo uso)
# donde crea su contraseña y entra. También sirve para recuperar el acceso.
# ---------------------------------------------------------------------------
INVITE_HORAS = 168


def _h(token):
    return hashlib.sha256(str(token).encode('utf-8')).hexdigest()


def crear_invitacion(c, aff_id):
    """Crea el enlace personal (el anterior deja de servir). Guarda solo el hash del token."""
    c.execute('UPDATE affiliate_invites SET used_at=%s WHERE affiliate_id=%s AND used_at IS NULL', (ahora(), aff_id))
    token = secrets.token_urlsafe(32)
    c.execute('INSERT INTO affiliate_invites(affiliate_id,token_hash,expires_at,created_at) VALUES(%s,%s,%s,%s)',
              (aff_id, _h(token), (datetime.now(DR) + timedelta(hours=INVITE_HORAS)).isoformat(), ahora()))
    return f'{SITE_URL}/afiliados/activar?t={token}'


def enlace_de_acceso(aff_id):
    """Lo usa el correo de aprobación (viane_extras). Devuelve la URL personal de acceso."""
    with CTX['db']() as c:
        return crear_invitacion(c, aff_id)


def _invitacion(c, token):
    """(invitación, error). Valida que exista, no esté usada ni vencida y que la afiliación siga aprobada."""
    inv = c.execute('SELECT i.id,i.expires_at,i.used_at,a.id AS aff_id,a.email,a.full_name,a.user_id,a.status,a.rights_revoked '
                    'FROM affiliate_invites i JOIN affiliates a ON a.id=i.affiliate_id WHERE i.token_hash=%s',
                    (_h(token),)).fetchone() if token else None
    if not inv or inv['used_at']:
        return None, 'Este enlace ya se usó o no es válido. Pide uno nuevo.'
    try:
        vencido = datetime.fromisoformat(str(inv['expires_at'])) < datetime.now(DR)
    except ValueError:
        vencido = True
    if vencido:
        return None, 'Este enlace venció. Pide uno nuevo.'
    if inv['status'] != 'aprobado' or inv['rights_revoked']:
        return None, 'Tu afiliación no está activa.'
    return inv, None


@bp.get('/activar')
def pagina_activar():
    return render_template('afiliados/activar.html')


@bp.get('/api/activar/validar')
def api_activar_validar():
    with CTX['db']() as c:
        inv, err = _invitacion(c, request.args.get('t', ''))
    if err:
        return jsonify({'error': err}), 400
    mail = str(inv['email'])
    return jsonify({'ok': True, 'nombre': inv['full_name'], 'correo': mail[:2] + '•••' + mail[mail.find('@'):]})


@bp.post('/api/activar')
def api_activar():
    d = _json()
    if d is None:
        return jsonify({'error': 'Solicitud no válida.'}), 400
    ip = _ip()
    if _blocked(ip):
        return jsonify({'error': 'Demasiados intentos. Espera 15 minutos.'}), 429
    pwd = str(d.get('password') or '')
    if len(pwd) < 8:
        return jsonify({'error': 'La contraseña debe tener al menos 8 caracteres.'}), 400
    with CTX['db']() as c:
        inv, err = _invitacion(c, str(d.get('token') or ''))
        if err:
            _fail(ip)
            return jsonify({'error': err}), 400
        email = str(inv['email']).strip().lower()
        existente = c.execute('SELECT id,role FROM users WHERE lower(email)=%s', (email,)).fetchone()
        if existente and existente['role'] != 'afiliado':
            return jsonify({'error': 'Este correo pertenece a otro tipo de cuenta. Escríbenos por WhatsApp.'}), 400
        if existente:
            uid = existente['id']
            c.execute('UPDATE users SET password_hash=%s, is_active=TRUE WHERE id=%s', (generate_password_hash(pwd), uid))
        else:
            uid = c.execute("INSERT INTO users(email,full_name,password_hash,role,is_active,created_by,created_at,activated_at) "
                            "VALUES(%s,%s,%s,'afiliado',TRUE,'enlace-de-aprobacion',%s,%s) RETURNING id",
                            (email, inv['full_name'], generate_password_hash(pwd), ahora(), ahora())).fetchone()['id']
        c.execute('UPDATE affiliates SET user_id=%s WHERE id=%s', (uid, inv['aff_id']))
        c.execute('UPDATE affiliate_invites SET used_at=%s WHERE id=%s', (ahora(), inv['id']))
        aff = c.execute('SELECT id,full_name,reseller_id FROM affiliates WHERE id=%s', (inv['aff_id'],)).fetchone()
        asegurar_codigo(c, aff)
    _STAFF_CACHE_CLEAR(uid)
    session.clear()
    return respuesta_sesion(uid, bool(d.get('tokens')))


def _STAFF_CACHE_CLEAR(uid):
    try:
        from app import _STAFF_CACHE
        _STAFF_CACHE.pop(uid, None)
    except Exception:
        pass


@bp.post('/api/recuperar')
def api_recuperar():
    """Envía un enlace nuevo al correo de un afiliado APROBADO (primer ingreso u olvido de contraseña)."""
    d = _json()
    if d is None:
        return jsonify({'error': 'Solicitud no válida.'}), 400
    ip = _ip()
    if _blocked(ip):
        return jsonify({'error': 'Demasiados intentos. Espera 15 minutos.'}), 429
    email = _txt(d.get('email'), 120).lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
        return jsonify({'error': 'Escribe un correo válido.'}), 400
    _fail(ip)  # cuenta como intento: limita el envío masivo de correos
    with CTX['db']() as c:
        aff = c.execute("SELECT id,full_name FROM affiliates WHERE LOWER(email)=%s AND status='aprobado' AND rights_revoked=0 "
                        'ORDER BY id DESC LIMIT 1', (email,)).fetchone()
        if not aff:
            return jsonify({'error': NOT_AFFILIATE_MSG, 'solicitar': True}), 403
        link = crear_invitacion(c, aff['id'])
        st = infra.enqueue_mail(c, email, 'Tu acceso al portal de afiliados — VIANE DELICATESSEN',
                                f"Hola {(aff['full_name'] or '').split(' ')[0] or 'afiliado/a'},\n\n"
                                f"Entra aquí para crear tu contraseña y abrir tu portal de afiliado (el enlace sirve una vez y vence en 7 días):\n{link}\n\n"
                                "Si no lo pediste tú, ignora este mensaje.\n\nVIANE DELICATESSEN")
    if st not in ('en cola', 'enviado'):
        return jsonify({'error': 'No pudimos enviar el correo ahora. Escríbenos por WhatsApp al 829-675-9640.'}), 503
    return jsonify({'ok': True, 'mensaje': 'Te enviamos un enlace de acceso. Revisa tu correo (y la carpeta de spam).'})


def init_afiliados(app, db, migrations, is_admin, rdb=None):
    """Registra el módulo. Solo añade rutas nuevas y sentencias CREATE/ALTER ... IF NOT EXISTS."""
    CTX['db'], CTX['rdb'], CTX['is_admin'] = db, rdb or db, is_admin
    for stmt in DDL:
        if stmt not in migrations:
            migrations.append(stmt)
    app.register_blueprint(bp)
