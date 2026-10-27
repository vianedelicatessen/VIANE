"""
VIANE DELICATESSEN - backend Flask listo para Vercel.

- Base de datos: Postgres (Supabase) a traves de DATABASE_URL.
- Imagenes / audio / video subidos desde el navegador: se guardan en Supabase
  (tabla "media") y se muestran desde /media/<id>. No se pierden al actualizar
  ni al cerrar la web, y no necesitan ninguna llave extra.
- Sesion de administrador: SECRET_KEY, ADMIN_EMAIL, ADMIN_PASSWORD.
- Cloudinary es OPCIONAL: solo se usa para videos/audios grandes (/api/upload-signature).

Vercel NO conserva archivos locales entre peticiones, por eso no se usa
SQLite ni la carpeta static/uploads.
"""
import io
import os
import json
import hmac
import secrets
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

from pathlib import Path
from flask import Flask, request, jsonify, session, send_from_directory, send_file
from werkzeug.exceptions import HTTPException, NotFound, RequestEntityTooLarge
from werkzeug.utils import secure_filename

# Vercel busca esta variable "app" al nivel principal del archivo.
app = Flask(__name__)
TEMPLATES_DIR = Path(__file__).resolve().parent / 'templates'

try:  # solo para desarrollo local (archivo .env); en Vercel no hace nada
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _limpiar_cloudinary_url():
    """Arregla errores comunes al pegar CLOUDINARY_URL en Vercel: comillas, el texto
    'CLOUDINARY_URL=' delante, o los signos < > de los ejemplos."""
    v = (os.environ.get('CLOUDINARY_URL') or '').strip().strip('"').strip("'").strip()
    if not v:
        return
    if v.upper().startswith('CLOUDINARY_URL='):
        v = v.split('=', 1)[1]
    os.environ['CLOUDINARY_URL'] = v.strip().strip('"').strip("'").replace('<', '').replace('>', '').strip()


_limpiar_cloudinary_url()

# Si una de estas librerias falla, el sitio sigue abierto y /api/health dice cual es el problema
# (el detalle queda en los Logs de Vercel). Asi no aparece un 500 generico.
try:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.utils
except Exception as exc:
    cloudinary = None
    print(f'[VIANE] Cloudinary no disponible (es opcional): {exc!r}')

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception as exc:
    psycopg = None
    print(f'[VIANE] psycopg no disponible (revisa requirements.txt): {exc!r}')

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
IS_VERCEL = bool(os.environ.get('VERCEL') or os.environ.get('VERCEL_ENV'))

ALLOWED_IMAGES = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED_AUDIO_VIDEO = {'mp4', 'mp3', 'wav', 'ogg', 'm4a'}
ALLOWED_MEDIA = ALLOWED_IMAGES | ALLOWED_AUDIO_VIDEO
MEDIA_TYPES = {
    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp',
    'gif': 'image/gif', 'mp4': 'video/mp4', 'mp3': 'audio/mpeg', 'wav': 'audio/wav',
    'ogg': 'audio/ogg', 'm4a': 'audio/mp4',
}
STATUSES = ['Pago pendiente', 'Pago recibido', 'Pedido confirmado', 'En preparación',
            'En creación', 'Enviado', 'En camino', 'Entregado', 'Incidencia']

# Vercel limita el cuerpo de una peticion a 4.5 MB. Archivos mas grandes deben
# subirse directo a Cloudinary con /api/upload-signature (ver README).
MAX_SERVER_UPLOAD = 4 * 1024 * 1024

app.config.update(
    MAX_CONTENT_LENGTH=MAX_SERVER_UPLOAD,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=IS_VERCEL,  # HTTPS en Vercel
)

# La clave de sesion DEBE ser fija: en Vercel hay varias instancias y una clave
# aleatoria por instancia cerraria la sesion del admin constantemente.
_secret = os.environ.get('SECRET_KEY') or os.environ.get('VIANE_SECRET_KEY')
SECRET_KEY_OK = bool(_secret)
if not _secret:
    print('[VIANE] ADVERTENCIA: falta SECRET_KEY; la sesion del admin sera inestable.')
    _secret = secrets.token_hex(32)
app.secret_key = _secret

ADMIN_EMAIL = (os.environ.get('ADMIN_EMAIL') or '').strip().lower()
ADMIN_PASSWORD = (os.environ.get('ADMIN_PASSWORD') or '').strip()

# Cloudinary (opcional) lee CLOUDINARY_URL automaticamente. Respaldo con variables sueltas.
if cloudinary is not None:
    try:
        cloudinary.config(secure=True)
        if os.environ.get('CLOUDINARY_CLOUD_NAME') and not os.environ.get('CLOUDINARY_URL'):
            cloudinary.config(
                cloud_name=os.environ['CLOUDINARY_CLOUD_NAME'],
                api_key=os.environ.get('CLOUDINARY_API_KEY'),
                api_secret=os.environ.get('CLOUDINARY_API_SECRET'),
                secure=True,
            )
    except Exception as exc:
        print(f'[VIANE] Configuracion de Cloudinary invalida: {exc!r}')
        cloudinary = None


def cloudinary_ready():
    if cloudinary is None:
        return False
    try:
        cfg = cloudinary.config()
        return bool(cfg.cloud_name and cfg.api_key and cfg.api_secret)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Base de datos (Supabase / Postgres)
# ---------------------------------------------------------------------------
def _clean_dsn(url):
    """Limpia DATABASE_URL de Supabase para que nunca tumbe el sitio:
    - quita comillas, el texto 'DATABASE_URL=' y los corchetes/signos de los ejemplos
      (por ejemplo [YOUR-PASSWORD] o [servidor]);
    - codifica caracteres raros de la contrasena (@ # / : etc.);
    - quita parametros que libpq no entiende (pgbouncer=true, etc.) y exige SSL."""
    url = (url or '').strip().strip('"').strip("'").strip()
    if url.upper().startswith('DATABASE_URL='):
        url = url.split('=', 1)[1].strip().strip('"').strip("'").strip()
    if not url:
        return ''
    url = url.replace('[', '').replace(']', '').replace('<', '').replace('>', '')
    scheme, sep, rest = url.partition('://')
    if not sep:
        raise ValueError('la URL debe empezar con postgresql://')
    if scheme.lower() == 'postgres':
        scheme = 'postgresql'
    # La contrasena puede traer "@": el servidor empieza despues de la ULTIMA "@".
    userinfo, at, hostpart = rest.rpartition('@')
    if at:
        user, colon, password = userinfo.partition(':')
        userinfo = quote(unquote(user), safe='')
        if colon:
            userinfo += ':' + quote(unquote(password), safe='')
        userinfo += '@'
    else:
        userinfo = ''
    parts = urlsplit(f'{scheme}://{userinfo}{hostpart}')
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ('pgbouncer', 'connection_limit', 'schema')]
    keys = {k.lower() for k, _ in query}
    host = (parts.hostname or '').lower()
    if 'sslmode' not in keys and host not in ('localhost', '127.0.0.1'):
        query.append(('sslmode', 'require'))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


DSN_ERROR = ''
try:
    DSN = _clean_dsn(os.environ.get('DATABASE_URL'))
except Exception as exc:  # una URL mal escrita ya no tumba el sitio: /api/health lo avisa
    DSN = ''
    DSN_ERROR = 'DATABASE_URL tiene un formato invalido (copiala de nuevo desde Supabase).'
    print(f'[VIANE] {DSN_ERROR} Detalle: {type(exc).__name__}')


def connect():
    if psycopg is None:
        raise RuntimeError('La libreria psycopg no esta instalada (revisa requirements.txt).')
    if not DSN:
        raise RuntimeError(DSN_ERROR or 'Falta la variable de entorno DATABASE_URL.')
    # prepare_threshold=None: compatible con el pooler de Supabase (modo transaccion).
    return psycopg.connect(DSN, row_factory=dict_row, prepare_threshold=None, connect_timeout=10)


SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS products(
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'bandejas',
        price DOUBLE PRECISION NOT NULL, description TEXT NOT NULL, image TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS reviews(
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, rating INTEGER NOT NULL, products TEXT NOT NULL,
        comment TEXT NOT NULL, phone TEXT DEFAULT '', approved INTEGER NOT NULL DEFAULT 0,
        admin_reply TEXT DEFAULT '', created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS orders(
        id BIGSERIAL PRIMARY KEY, code TEXT UNIQUE NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
        province TEXT NOT NULL, delivery_date TEXT NOT NULL, items TEXT NOT NULL,
        total DOUBLE PRECISION NOT NULL, payment_status TEXT NOT NULL DEFAULT 'Pago pendiente',
        status TEXT NOT NULL DEFAULT 'Pago pendiente', notes TEXT DEFAULT '', incident TEXT DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ads(
        id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, image TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1)''',
    '''CREATE TABLE IF NOT EXISTS payments(
        id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL, details TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1, tip_enabled INTEGER NOT NULL DEFAULT 0)''',
    # Imagenes / audio / video subidos desde el navegador (hasta 4 MB cada uno).
    '''CREATE TABLE IF NOT EXISTS media(
        id BIGSERIAL PRIMARY KEY, filename TEXT NOT NULL, content_type TEXT NOT NULL,
        data BYTEA NOT NULL, size INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)''',
    # Seguridad: Supabase expone el esquema public por su API REST. Con RLS activado
    # y sin politicas, nadie puede leer estas tablas por esa via. Esta app se conecta
    # con el rol postgres (DATABASE_URL), que ignora RLS, asi que sigue funcionando.
    'ALTER TABLE products ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE reviews ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE orders ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE settings ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE ads ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE payments ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE media ENABLE ROW LEVEL SECURITY',
    '''CREATE TABLE IF NOT EXISTS categories(
        id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS sales(
        id BIGSERIAL PRIMARY KEY, order_id BIGINT UNIQUE, code TEXT NOT NULL DEFAULT '',
        customer TEXT NOT NULL, contact TEXT NOT NULL, purchase TEXT NOT NULL DEFAULT '',
        address TEXT NOT NULL DEFAULT '', amount DOUBLE PRECISION NOT NULL,
        sold_date TEXT NOT NULL, created_at TEXT NOT NULL)''',
    'ALTER TABLE categories ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE sales ENABLE ROW LEVEL SECURITY',
]
_schema_ready = False
_schema_lock = Lock()

DEFAULT_SETTINGS = {
    'brand_name': 'VIANE DELICATESSEN', 'logo': '', 'whatsapp': '18296759640',
    'instagram': 'vianedelicatessen', 'background_type': 'color', 'background_value': '#FFFDF5',
    'review_url': '', 'tips_message': '¡Gracias por apoyar a VIANE Delicatessen!',
}

SAMPLE_PRODUCTS = [
    ('Desayuno Sorpresa VIP Viane', 'desayunos', 3500,
     'Desayuno especial personalizado. Confirma disponibilidad y detalles por WhatsApp.', '', 0),
    ('Bandeja Romántica', 'bandejas', 4200, 'Bandeja premium personalizada para sorprender.', '', 0),
    ('Picaderas para Cumpleaños', 'picaderas', 4500, 'Variedad de bocadillos para compartir.', '', 0),
]


def now():
    return datetime.now(timezone.utc).isoformat()


REQUIRED_TABLES = {'products', 'reviews', 'orders', 'settings', 'ads', 'payments', 'media',
                   'categories', 'sales'}


def ensure_schema():
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with connect() as con:
            existing = {r['tablename'] for r in con.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()}
            if not REQUIRED_TABLES <= existing:
                con.execute('SELECT pg_advisory_xact_lock(%s)', (918273645,))
                for stmt in SCHEMA:
                    con.execute(stmt)
                for k, v in DEFAULT_SETTINGS.items():
                    con.execute('INSERT INTO settings(key,value) VALUES(%s,%s) ON CONFLICT (key) DO NOTHING', (k, v))
                if con.execute('SELECT COUNT(*) AS n FROM products').fetchone()['n'] == 0:
                    for p in SAMPLE_PRODUCTS:
                        con.execute(
                            'INSERT INTO products(name,category,price,description,image,active,created_at) '
                            'VALUES(%s,%s,%s,%s,%s,%s,%s)', (*p, now()))
                try:  # categorias iniciales, ordenar productos y pasar pedidos ya entregados a ventas
                    with con.transaction():
                        for n in DEFAULT_CATEGORIES:
                            ensure_category(con, n)
                        reclassify_all(con)
                        for o in con.execute("SELECT * FROM orders WHERE status='Entregado'").fetchall():
                            save_sale(con, o, _dr_date(o['updated_at']))
                except Exception as exc:
                    print(f'[VIANE] Migracion de categorias/ventas: {exc!r}')
        _schema_ready = True


@contextmanager
def db():
    ensure_schema()
    con = connect()
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def body():
    d = request.get_json(force=True, silent=True)
    return d if isinstance(d, dict) else {}


def setting_map(c):
    return {r['key']: r['value'] for r in c.execute('SELECT key,value FROM settings').fetchall()}


def rows(c, sql, params=()):
    return c.execute(sql, params).fetchall()


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not ADMIN_EMAIL or not session.get('admin') or session.get('email') != ADMIN_EMAIL:
            return jsonify({'error': 'Acceso administrativo requerido'}), 401
        return fn(*args, **kwargs)
    return wrapped


def allowed_file(filename, allowed):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed


def to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --- Categorias automaticas y ventas -----------------------------------------
DR_TZ = timezone(timedelta(hours=-4))  # Republica Dominicana (UTC-4, sin horario de verano)
DEFAULT_CATEGORIES = ['desayunos', 'bandejas', 'picaderas', 'bizcochos', 'mariscos', 'regalos', 'eventos']
# El orden es la prioridad: "Bandeja Desayuno" va a desayunos, no a bandejas.
CATEGORY_RULES = [
    ('desayunos', ('desayuno',)),
    ('picaderas', ('picadera',)),
    ('bizcochos', ('bizcocho', 'torta', 'cake')),
    ('mariscos', ('marisco', 'camaron', 'langosta')),
    ('bandejas', ('bandeja',)),
    ('regalos', ('regalo', 'ramo', 'arreglo', 'rosas')),
    ('eventos', ('evento', 'boda', 'compromiso', 'aniversario')),
]


def _norm(s):
    s = unicodedata.normalize('NFD', str(s or '').lower())
    return ''.join(ch for ch in s if unicodedata.category(ch) != 'Mn').strip()


def clean_cat(s):
    s = re.sub(r'[<>"\'`\\&]', '', str(s or '')).strip().lower()
    return re.sub(r'\s+', ' ', s)[:40]


def detect_category(text, custom=()):
    t = _norm(text)
    if not t:
        return None
    for cat, words in CATEGORY_RULES:
        if any(re.search(r'\b' + w, t) for w in words):
            return cat
    for cat in sorted(custom, key=len, reverse=True):  # categorias creadas desde el panel
        n = _norm(cat).rstrip('s')
        if cat != 'otros' and len(n) >= 3 and re.search(r'\b' + re.escape(n), t):
            return cat
    return None


def ensure_category(c, name):
    c.execute('INSERT INTO categories(name,active,created_at) VALUES(%s,1,%s) '
              'ON CONFLICT (name) DO NOTHING', (name, now()))


def category_names(c):
    return [r['name'] for r in c.execute('SELECT name FROM categories ORDER BY id').fetchall()]


def resolve_category(c, name, typed=''):
    names = category_names(c)
    cat = detect_category(name, names) or detect_category(typed, names) or clean_cat(typed) or 'otros'
    ensure_category(c, cat)
    return cat


def reclassify_all(c):
    names, changed = category_names(c), 0
    for p in c.execute('SELECT id,name,category FROM products').fetchall():
        cat = (detect_category(p['name'], names) or detect_category(p['category'], names)
               or clean_cat(p['category']) or 'otros')
        ensure_category(c, cat)
        if cat not in names:
            names.append(cat)
        if cat != p['category']:
            c.execute('UPDATE products SET category=%s WHERE id=%s', (cat, p['id']))
            changed += 1
    return changed


def _dr_date(iso=None):
    try:
        dt = datetime.fromisoformat(iso) if iso else datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DR_TZ).strftime('%Y-%m-%d')
    except Exception:
        return datetime.now(DR_TZ).strftime('%Y-%m-%d')


def items_text(raw):
    try:
        items = json.loads(raw or '[]')
    except Exception:
        return ''
    count = {}
    for i in (items if isinstance(items, list) else []):
        n = str(i.get('name', '')).strip() if isinstance(i, dict) else ''
        if n:
            count[n] = count.get(n, 0) + 1
    return ' · '.join(f'{q} x {n}' for n, q in count.items())


def save_sale(c, o, sold_date=None):
    c.execute(
        'INSERT INTO sales(order_id,code,customer,contact,purchase,address,amount,sold_date,created_at) '
        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING',
        (o['id'], o['code'], o['name'], o['phone'], items_text(o['items']), o['province'],
         o['total'], sold_date or _dr_date(), now()))


@app.after_request
def no_cache_api(resp):
    if request.path.startswith('/api/'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.errorhandler(RequestEntityTooLarge)
def too_large(_e):
    return jsonify({'error': 'El archivo es demasiado grande para subirlo por aquí (máximo 4 MB). '
                             'Reduce su tamaño o usa la subida directa.'}), 413


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        if request.path.startswith('/api/'):
            return jsonify({'error': e.description}), e.code
        return e
    app.logger.exception('Error no controlado')
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Error interno del servidor. Intenta de nuevo en un momento.'}), 500
    return 'Error interno del servidor', 500


# ---------------------------------------------------------------------------
# Paginas
# ---------------------------------------------------------------------------
@app.get('/')
def home():
    # Se sirve el archivo tal cual (sin Jinja), asi el JavaScript con llaves no se toca.
    return send_from_directory(TEMPLATES_DIR, 'index.html')


@app.get('/Bandeja360.html')
@app.get('/bandeja360')
def bandeja360():
    try:
        return send_from_directory(TEMPLATES_DIR, 'Bandeja360.html')
    except NotFound:
        return jsonify({'error': 'Página no encontrada'}), 404


@app.get('/media/<int:mid>')
def media(mid):
    """Muestra una imagen/audio/video guardado en Supabase (tabla media)."""
    with db() as c:
        row = c.execute('SELECT content_type,data FROM media WHERE id=%s', (mid,)).fetchone()
    if not row:
        return jsonify({'error': 'Archivo no encontrado'}), 404
    resp = send_file(io.BytesIO(bytes(row['data'])), mimetype=row['content_type'],
                     conditional=True, etag=f'viane-{mid}')
    # El id de un archivo nunca cambia de contenido: se puede guardar en cache un año.
    resp.headers['Cache-Control'] = 'public, max-age=31536000, s-maxage=31536000, immutable'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


@app.get('/api/health')
def health():
    """Diagnostico rapido del despliegue (solo true/false o textos cortos, nunca secretos)."""
    info = {
        'psycopg_instalado': psycopg is not None,
        'database_url': 'ok' if DSN else (DSN_ERROR or 'falta DATABASE_URL'),
        'database': False,
        'admin_configured': bool(ADMIN_EMAIL and ADMIN_PASSWORD),
        'secret_key': SECRET_KEY_OK,
        'cloudinary_opcional': cloudinary_ready(),
    }
    try:
        with db() as c:
            c.execute('SELECT 1')
        info['database'] = True
    except Exception:
        app.logger.exception('Health: fallo al conectar con la base de datos')
    info['ok'] = bool(info['database'] and info['admin_configured'] and info['secret_key'])
    return jsonify(info), (200 if info['ok'] else 503)


# ---------------------------------------------------------------------------
# API publica
# ---------------------------------------------------------------------------
@app.get('/api/public')
def public_data():
    with db() as c:
        products = rows(c, 'SELECT p.* FROM products p WHERE p.active=1 AND NOT EXISTS '
                           '(SELECT 1 FROM categories k WHERE k.name=p.category AND k.active=0) '
                           'ORDER BY p.id DESC')
        categories = [r['name'] for r in rows(
            c, 'SELECT k.name FROM categories k WHERE k.active=1 AND EXISTS '
               '(SELECT 1 FROM products p WHERE p.category=k.name AND p.active=1) ORDER BY k.id')]
        reviews = rows(c, 'SELECT id,name,rating,products,comment,admin_reply,created_at '
                          'FROM reviews WHERE approved=1 ORDER BY id DESC')
        ads = rows(c, 'SELECT * FROM ads WHERE active=1 ORDER BY id')
        payments = rows(c, 'SELECT id,title,url,details,tip_enabled FROM payments WHERE active=1 ORDER BY id')
        settings = setting_map(c)
    return jsonify({'products': products, 'categories': categories, 'reviews': reviews, 'ads': ads,
                    'payments': payments, 'settings': settings, 'statuses': STATUSES})


@app.post('/api/reviews')
def submit_review():
    d = body()
    name = str(d.get('name', '')).strip()[:100]
    comment = str(d.get('comment', '')).strip()[:2000]
    products = d.get('products', [])
    try:
        rating = int(d.get('rating', 0))
    except (TypeError, ValueError):
        rating = 0
    if (not name or not comment or rating not in range(1, 6)
            or not isinstance(products, list) or not 1 <= len(products) <= 3):
        return jsonify({'error': 'Completa nombre, comentario, calificación y de 1 a 3 productos'}), 400
    with db() as c:
        row = c.execute(
            'INSERT INTO reviews(name,rating,products,comment,phone,approved,created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, rating, json.dumps(products, ensure_ascii=False), comment,
             str(d.get('phone', ''))[:40], 0, now())).fetchone()
    return jsonify({'ok': True, 'id': row['id'],
                    'message': '¡Gracias! Tu reseña fue recibida y se publicará cuando sea revisada.'})


@app.post('/api/orders')
def create_order():
    d = body()
    items = d.get('items', [])
    name = str(d.get('name', '')).strip()[:100]
    phone = str(d.get('phone', '')).strip()[:40]
    if not name or not phone or not isinstance(items, list) or not items:
        return jsonify({'error': 'Completa los datos y agrega productos'}), 400
    items = items[:100]
    ids = []
    for i in items:
        if isinstance(i, dict) and str(i.get('id', '')).isdigit():
            ids.append(int(i['id']))
    if not ids:
        return jsonify({'error': 'Productos inválidos'}), 400
    with db() as c:
        found = rows(c, 'SELECT * FROM products WHERE active=1 AND id = ANY(%s)', (list(set(ids)),))
        byid = {r['id']: r for r in found}
        clean, total = [], 0.0
        for pid in ids:  # cada elemento del carrito cuenta una vez (igual que antes)
            p = byid.get(pid)
            if p:
                clean.append({'id': pid, 'name': p['name'], 'price': p['price']})
                total += p['price']
        if not clean:
            return jsonify({'error': 'No hay productos válidos'}), 400
        code = 'VIA-' + secrets.token_hex(4).upper()
        c.execute(
            'INSERT INTO orders(code,name,phone,province,delivery_date,items,total,payment_status,status,'
            'notes,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (code, name, phone, str(d.get('province', 'Puerto Plata'))[:100],
             str(d.get('delivery_date', ''))[:100], json.dumps(clean, ensure_ascii=False), total,
             'Pago pendiente', 'Pago pendiente', str(d.get('notes', ''))[:2000], now(), now()))
        s = setting_map(c)
    return jsonify({'ok': True, 'code': code, 'total': total, 'review_url': s.get('review_url', ''),
                    'whatsapp': s.get('whatsapp', '18296759640')})


@app.get('/api/track/<code>')
def track_order(code):
    with db() as c:
        row = c.execute('SELECT code,status,payment_status,delivery_date,updated_at,incident '
                        'FROM orders WHERE code=%s', (code.strip().upper(),)).fetchone()
    if not row:
        return jsonify({'error': 'No encontramos un pedido con ese código. Revisa e inténtalo otra vez.'}), 404
    return jsonify(row)


# ---------------------------------------------------------------------------
# Administrador
# ---------------------------------------------------------------------------
@app.post('/api/login')
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = request.form
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()

    if not ADMIN_EMAIL or not ADMIN_PASSWORD:
        return jsonify({'error': 'El administrador debe configurar ADMIN_EMAIL y ADMIN_PASSWORD en el servidor.'}), 503
    ok_email = hmac.compare_digest(email.encode('utf-8'), ADMIN_EMAIL.encode('utf-8'))
    ok_pass = hmac.compare_digest(password.encode('utf-8'), ADMIN_PASSWORD.encode('utf-8'))
    if not (ok_email and ok_pass):
        return jsonify({'error': 'Correo o contraseña incorrectos'}), 401

    session.clear()
    session['admin'] = True
    session['email'] = ADMIN_EMAIL
    return jsonify({'ok': True, 'email': ADMIN_EMAIL})


@app.post('/api/logout')
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.get('/api/admin/data')
@admin_required
def admin_data():
    with db() as c:
        return jsonify({
            'products': rows(c, 'SELECT * FROM products ORDER BY id DESC'),
            'reviews': rows(c, 'SELECT * FROM reviews ORDER BY id DESC'),
            'orders': rows(c, "SELECT * FROM orders WHERE status<>'Entregado' ORDER BY id DESC"),
            'categories': rows(c, 'SELECT k.id,k.name,k.active,(SELECT COUNT(*) FROM products p '
                                  'WHERE p.category=k.name) AS total FROM categories k ORDER BY k.id'),
            'ads': rows(c, 'SELECT * FROM ads ORDER BY id DESC'),
            'payments': rows(c, 'SELECT * FROM payments ORDER BY id DESC'),
            'settings': setting_map(c),
            'statuses': STATUSES,
        })


# --- Subida de archivos (se guardan en Supabase) ------------------------------
@app.post('/api/upload')
@admin_required
def upload():
    """Guarda un archivo (hasta 4 MB) en Supabase. Devuelve {'url', 'filename'}.
    La url (/media/<id>) es la que se guarda en el producto, logo o anuncio."""
    f = request.files.get('file')
    kind = request.form.get('kind', 'image')
    if not f or not f.filename:
        return jsonify({'error': 'Selecciona un archivo'}), 400
    allowed = ALLOWED_MEDIA if kind == 'media' else ALLOWED_IMAGES
    if not allowed_file(f.filename, allowed):
        return jsonify({'error': 'Tipo de archivo no permitido'}), 400

    ext = f.filename.rsplit('.', 1)[1].lower()
    data = f.read()
    if not data:
        return jsonify({'error': 'El archivo está vacío'}), 400
    if len(data) > MAX_SERVER_UPLOAD:
        return too_large(None)
    name = (secure_filename(f.filename.rsplit('.', 1)[0])[:60] or 'archivo') + '.' + ext

    with db() as c:
        row = c.execute(
            'INSERT INTO media(filename,content_type,data,size,created_at) VALUES(%s,%s,%s,%s,%s) RETURNING id',
            (name, MEDIA_TYPES[ext], data, len(data), now())).fetchone()
    return jsonify({'url': f"/media/{row['id']}", 'filename': name})


@app.post('/api/upload-signature')
@admin_required
def upload_signature():
    """OPCIONAL (necesita Cloudinary): firma para subir archivos GRANDES (video/audio) directo
    del navegador a Cloudinary, sin pasar por Vercel (limite 4.5 MB)."""
    if not cloudinary_ready():
        return jsonify({'error': 'Cloudinary no está configurado (revisa CLOUDINARY_URL en Vercel).'}), 503

    resource_type = body().get('resource_type', 'image')
    if resource_type not in ('image', 'video'):
        return jsonify({'error': 'resource_type inválido'}), 400

    cfg = cloudinary.config()

    # Ordenamos los parámetros alfabéticamente para evitar conflictos en la firma (folder antes que timestamp)
    params = {
        'folder': 'viane',
        'timestamp': int(datetime.now(timezone.utc).timestamp())
    }

    signature = cloudinary.utils.api_sign_request(params, cfg.api_secret)

    return jsonify({
        'upload_url': f'https://api.cloudinary.com/v1_1/{cfg.cloud_name}/{resource_type}/upload',
        'api_key': cfg.api_key,
        'timestamp': params['timestamp'],
        'folder': params['folder'],
        'signature': signature,
    })


# --- Productos ----------------------------------------------------------------
@app.post('/api/products')
@admin_required
def create_product():
    d = body()
    name = str(d.get('name', '')).strip()
    desc = str(d.get('description', '')).strip()
    image = str(d.get('image', '')).strip()
    price = to_float(d.get('price'))
    if price is None:
        return jsonify({'error': 'Precio inválido'}), 400
    if not name or not desc or not image or price < 0:
        return jsonify({'error': 'Nombre, imagen, precio y descripción son obligatorios'}), 400
    with db() as c:
        category = resolve_category(c, name, d.get('category'))
        row = c.execute(
            'INSERT INTO products(name,category,price,description,image,active,created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, category, price, desc, image,
             1 if d.get('active', True) else 0, now())).fetchone()
    return jsonify({'id': row['id'], 'ok': True})


@app.put('/api/products/<int:pid>')
@admin_required
def update_product(pid):
    d = body()
    with db() as c:
        old = c.execute('SELECT * FROM products WHERE id=%s', (pid,)).fetchone()
        if not old:
            return jsonify({'error': 'Producto no encontrado'}), 404
        vals = {k: d.get(k, old[k]) for k in ['name', 'category', 'price', 'description', 'image', 'active']}
        price = to_float(vals['price'])
        if price is None or price < 0:
            return jsonify({'error': 'Precio inválido'}), 400
        if not vals['name'] or not vals['description'] or not vals['image']:
            return jsonify({'error': 'Faltan campos requeridos'}), 400
        category = resolve_category(c, vals['name'], vals['category'])
        c.execute('UPDATE products SET name=%s,category=%s,price=%s,description=%s,image=%s,active=%s WHERE id=%s',
                  (vals['name'], category, price, vals['description'], vals['image'],
                   int(bool(vals['active'])), pid))
    return jsonify({'ok': True})


@app.delete('/api/products/<int:pid>')
@admin_required
def delete_product(pid):
    with db() as c:
        c.execute('DELETE FROM products WHERE id=%s', (pid,))
    return jsonify({'ok': True})


# --- Reseñas (moderacion) -------------------------------------------------------
@app.patch('/api/reviews/<int:rid>')
@admin_required
def moderate_review(rid):
    d = body()
    with db() as c:
        c.execute('UPDATE reviews SET approved=%s,admin_reply=%s WHERE id=%s',
                  (int(bool(d.get('approved', False))), str(d.get('admin_reply', ''))[:1000], rid))
    return jsonify({'ok': True})


@app.delete('/api/reviews/<int:rid>')
@admin_required
def delete_review(rid):
    with db() as c:
        c.execute('DELETE FROM reviews WHERE id=%s', (rid,))
    return jsonify({'ok': True})


# --- Pedidos ----------------------------------------------------------------------
@app.patch('/api/orders/<int:oid>')
@admin_required
def update_order(oid):
    d = body()
    status = d.get('status', 'Pago pendiente')
    payment = str(d.get('payment_status', 'Pago pendiente'))[:100]
    if status not in STATUSES:
        return jsonify({'error': 'Estado no válido'}), 400
    notes = str(d['notes'])[:2000] if 'notes' in d else None  # None = conservar las notas del cliente
    with db() as c:
        cur = c.execute(
            'UPDATE orders SET status=%s,payment_status=%s,notes=COALESCE(%s,notes),incident=%s,updated_at=%s '
            'WHERE id=%s',
            (status, payment, notes, str(d.get('incident', ''))[:1000], now(), oid))
        if cur.rowcount == 0:
            return jsonify({'error': 'Pedido no encontrado'}), 404
        # Entregado -> pasa a Ventas; cualquier otro estado -> se quita de Ventas.
        if status == 'Entregado':
            save_sale(c, c.execute('SELECT * FROM orders WHERE id=%s', (oid,)).fetchone())
        else:
            c.execute('DELETE FROM sales WHERE order_id=%s', (oid,))
    return jsonify({'ok': True})


# --- Ajustes ----------------------------------------------------------------------
@app.put('/api/settings')
@admin_required
def update_settings():
    d = body()
    allowed = {'brand_name', 'logo', 'whatsapp', 'instagram', 'background_type',
               'background_value', 'review_url', 'tips_message'}
    with db() as c:
        for k, v in d.items():
            if k in allowed:
                c.execute('INSERT INTO settings(key,value) VALUES(%s,%s) '
                          'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (k, str(v)[:4000]))
    return jsonify({'ok': True})


# --- Anuncios -----------------------------------------------------------------------
@app.post('/api/ads')
@admin_required
def add_ad():
    d = body()
    if not d.get('title') or not d.get('url'):
        return jsonify({'error': 'Título y enlace son obligatorios'}), 400
    with db() as c:
        row = c.execute('INSERT INTO ads(title,url,image,active) VALUES(%s,%s,%s,%s) RETURNING id',
                        (str(d['title']), str(d['url']), str(d.get('image', '')),
                         int(bool(d.get('active', True))))).fetchone()
    return jsonify({'ok': True, 'id': row['id']})


@app.put('/api/ads/<int:aid>')
@admin_required
def update_ad(aid):
    d = body()
    with db() as c:
        c.execute('UPDATE ads SET title=%s,url=%s,image=%s,active=%s WHERE id=%s',
                  (str(d.get('title', '')), str(d.get('url', '')), str(d.get('image', '')),
                   int(bool(d.get('active', True))), aid))
    return jsonify({'ok': True})


@app.delete('/api/ads/<int:aid>')
@admin_required
def delete_ad(aid):
    with db() as c:
        c.execute('DELETE FROM ads WHERE id=%s', (aid,))
    return jsonify({'ok': True})


# --- Metodos de pago ------------------------------------------------------------------
@app.post('/api/payments')
@admin_required
def add_payment():
    d = body()
    if not d.get('title') or not d.get('url'):
        return jsonify({'error': 'Nombre y enlace de pago son obligatorios'}), 400
    with db() as c:
        row = c.execute(
            'INSERT INTO payments(title,url,details,active,tip_enabled) VALUES(%s,%s,%s,%s,%s) RETURNING id',
            (str(d['title']), str(d['url']), str(d.get('details', '')),
             int(bool(d.get('active', True))), int(bool(d.get('tip_enabled', False))))).fetchone()
    return jsonify({'ok': True, 'id': row['id']})


@app.put('/api/payments/<int:pid>')
@admin_required
def update_payment(pid):
    d = body()
    with db() as c:
        c.execute('UPDATE payments SET title=%s,url=%s,details=%s,active=%s,tip_enabled=%s WHERE id=%s',
                  (str(d.get('title', '')), str(d.get('url', '')), str(d.get('details', '')),
                   int(bool(d.get('active', True))), int(bool(d.get('tip_enabled', False))), pid))
    return jsonify({'ok': True})


@app.delete('/api/payments/<int:pid>')
@admin_required
def delete_payment(pid):
    with db() as c:
        c.execute('DELETE FROM payments WHERE id=%s', (pid,))
    return jsonify({'ok': True})


# --- Categorias ---------------------------------------------------------------
@app.post('/api/categories')
@admin_required
def add_category():
    name = clean_cat(body().get('name'))
    if not name:
        return jsonify({'error': 'Escribe el nombre de la categoría'}), 400
    with db() as c:
        c.execute('INSERT INTO categories(name,active,created_at) VALUES(%s,1,%s) '
                  'ON CONFLICT (name) DO UPDATE SET active=1', (name, now()))
    return jsonify({'ok': True})


@app.put('/api/categories/<int:cid>')
@admin_required
def update_category(cid):
    with db() as c:
        c.execute('UPDATE categories SET active=%s WHERE id=%s',
                  (int(bool(body().get('active', True))), cid))
    return jsonify({'ok': True})


@app.delete('/api/categories/<int:cid>')
@admin_required
def delete_category(cid):
    with db() as c:
        row = c.execute('SELECT name FROM categories WHERE id=%s', (cid,)).fetchone()
        if not row:
            return jsonify({'error': 'Categoría no encontrada'}), 404
        if row['name'] != 'otros':
            ensure_category(c, 'otros')
            c.execute('UPDATE products SET category=%s WHERE category=%s', ('otros', row['name']))
        elif c.execute('SELECT 1 FROM products WHERE category=%s', ('otros',)).fetchone():
            return jsonify({'error': 'No puedes eliminar "otros" mientras tenga productos'}), 400
        c.execute('DELETE FROM categories WHERE id=%s', (cid,))
    return jsonify({'ok': True})


@app.post('/api/categories/reclassify')
@admin_required
def reclassify():
    with db() as c:
        changed = reclassify_all(c)
    return jsonify({'ok': True, 'changed': changed})


# --- Ventas (pedidos entregados) -----------------------------------------------
@app.get('/api/sales')
@admin_required
def list_sales():
    sql = ('SELECT id,order_id,code,customer,contact,purchase,address,amount,sold_date '
           'FROM sales WHERE 1=1')
    params = []
    for arg, op in (('from', '>='), ('to', '<=')):
        v = (request.args.get(arg) or '').strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            sql += f' AND sold_date {op} %s'
            params.append(v)
    with db() as c:
        data = rows(c, sql + ' ORDER BY sold_date DESC, id DESC', tuple(params))
    return jsonify({'sales': data, 'count': len(data), 'total': sum(r['amount'] for r in data)})


@app.delete('/api/sales/<int:sid>')
@admin_required
def reopen_sale(sid):
    """Deshace una venta por error: el pedido vuelve a la lista de Pedidos como 'En camino'."""
    with db() as c:
        row = c.execute('SELECT order_id FROM sales WHERE id=%s', (sid,)).fetchone()
        if not row:
            return jsonify({'error': 'Venta no encontrada'}), 404
        c.execute('DELETE FROM sales WHERE id=%s', (sid,))
        if row['order_id']:
            c.execute("UPDATE orders SET status='En camino', updated_at=%s WHERE id=%s",
                      (now(), row['order_id']))
    return jsonify({'ok': True})


if __name__ == '__main__':
    # Desarrollo local: python app.py  ->  http://localhost:5000
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='0.0.0.0', port=5000)
