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

Novedades de esta version (esquema v3, se migra solo, sin perder datos):
- Seguimiento de pedidos tolerante: acepta minusculas, espacios, guiones raros o sin el
  prefijo "VIA-", y distingue "pedido no existe" de "error de conexion".
- El costo de envio se calcula en el servidor y se suma al total del pedido.
- Los pedidos guardan direccion, metodo de entrega y sector.
- Categoria "personalizado": solo aparece en "Todos" y en su propia categoria.
- Pedidos "Entregado" o "Terminado" pasan solos a Ventas; /api/sales devuelve
  desglose de montos y porcentaje de ventas por sector.
- Reproductor flotante: ajustes music_url, music_title y music_kind.
- Categorias gestionables desde el panel (crear, renombrar, ocultar, eliminar).
- Vendedores (esquema v4): codigo multiuso, descuento al cliente y comision del vendedor.
- Esquema v5: % de descuento y de comision EDITABLES por vendedor, ventas con borrado
  logico (papelera de 12 meses, reactivables) y control de costos (insumos, recetas, ganancia neta).
"""
import io
import os
import json
import hmac
import secrets
import re
import unicodedata
import hashlib
import base64
import html
import time
import smtplib
import ssl
from email.message import EmailMessage
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from functools import wraps
from threading import Lock
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

from pathlib import Path
from flask import Flask, request, jsonify, session, send_from_directory, send_file, Response
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
            'En creación', 'Enviado', 'En camino', 'Entregado', 'Terminado', 'Incidencia']
# Los pedidos con alguno de estos estados salen de "Pedidos" y pasan a "Ventas".
SALE_STATUSES = ('Entregado', 'Terminado')

# Metodos de entrega. Los textos (label) deben coincidir con las opciones del <select>
# de index.html. El costo se calcula AQUI (no se confia en el navegador).
DELIVERY = {
    'pickup': {'label': 'Pasar a recoger en el local', 'sector': 'Recoger en local', 'cost': 0},
    'delivery_pp': {'label': 'Delivery en Pto. Plata (Ciudad)', 'sector': 'Puerto Plata', 'cost': 250},
    'delivery_santiago': {'label': 'Envío a Santiago', 'sector': 'Santiago', 'cost': 600},
    'delivery_capital': {'label': 'Envío a Santo Domingo', 'sector': 'Santo Domingo', 'cost': 700},
}

# Vendedores (revendedores) con codigo de descuento multiuso.
RESELLER_DISCOUNT_PCT = 3      # (por defecto; cada codigo puede tener el suyo) % que se le descuenta al cliente (sobre los productos, no sobre el envio)
RESELLER_COMMISSION_PCT = 15   # (por defecto; cada vendedor puede tener el suyo) % de ganancia del vendedor sobre el valor de la venta (productos ya con descuento)
# ---------------------------------------------------------------------------
# Reglas del programa de afiliados
# ---------------------------------------------------------------------------
MIN_AGE = 18
MIN_FOLLOWERS = 500
INACTIVITY_DAYS = 90          # 3 meses sin ventas -> cancelacion automatica
REVIEW_DAYS = 30              # plazo inicial de respuesta (1 a 30 dias)
EXTENDED_DAYS = 365           # si no hay respuesta: evaluacion extendida hasta 12 meses
TERMS_VERSION = '2026-10'
NOTIFY_EMAIL = os.environ.get('AFFILIATE_NOTIFY_EMAIL') or 'Vianedelicatenssen@gmail.com'
PLATFORMS = ['Instagram', 'TikTok', 'Facebook', 'YouTube', 'X (Twitter)', 'Otra']

TERMS_TEXT = f"""CONTRATO Y TÉRMINOS DEL PROGRAMA DE AFILIADOS — VIANE DELICATESSEN
Versión {TERMS_VERSION}

REQUISITOS PARA EL PROGRAMA DE AFILIADOS
1. Ser mayor de edad (según las leyes locales aplicables).
2. Contar con un mínimo de {MIN_FOLLOWERS} seguidores en tus plataformas o redes sociales vinculadas.

INACTIVIDAD
3. Si en un período de 3 meses consecutivos ({INACTIVITY_DAYS} días) el afiliado no registra ninguna venta, su suscripción de afiliado se cancelará automáticamente, junto con todos los derechos reservados vinculados a la misma.

COMISIONES
4. Las comisiones generadas son estrictamente y únicamente proporcionadas por nuestra empresa.

DERECHO DE TERMINACIÓN
5. Nos reservamos el derecho de cancelar cualquier suscripción de afiliado en el momento que consideremos apropiado, ya sea por infracción de las normas o por motivos operativos o legales que nos obliguen a ello.

RESPUESTA Y PLAZOS
6. Al enviar tu solicitud, te daremos una respuesta en un plazo de 1 a 30 días. En caso de no recibir ninguna notificación dentro de este plazo inicial, el proceso de evaluación se extenderá por un período de 30 días a 12 meses mientras validamos la disponibilidad y el cumplimiento de nuestros criterios.

FIRMA
7. Al firmar declaras que los datos entregados son verdaderos, que cumples los requisitos y que aceptas estos términos. Tu firma electrónica y el sello de este contrato dejan constancia de tu aceptación.
"""

RESPONSE_MESSAGE = (
    "¡Hemos recibido tu solicitud de afiliación! 🚀\n"
    "Estaremos revisando tu perfil y te daremos una respuesta en un plazo de 1 a 30 días. "
    "En caso de no recibir ninguna notificación dentro de este plazo inicial, el proceso de evaluación "
    "se extenderá por un período de 30 días a 12 meses mientras validamos la disponibilidad y el "
    "cumplimiento de nuestros criterios."
)

# Ventas eliminadas (borrado logico): se conservan este numero de dias y se pueden reactivar.
TRASH_DAYS = 365

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
    '''CREATE TABLE IF NOT EXISTS affiliates(
        id BIGSERIAL PRIMARY KEY, full_name TEXT NOT NULL, email TEXT NOT NULL,
        phone TEXT NOT NULL DEFAULT '', birth_date TEXT NOT NULL, platform TEXT NOT NULL DEFAULT '',
        profile TEXT NOT NULL DEFAULT '', followers BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pendiente', created_at TEXT NOT NULL,
        decided_at TEXT, decision_note TEXT NOT NULL DEFAULT '', reseller_id BIGINT,
        rights_revoked INTEGER NOT NULL DEFAULT 0, cancelled_at TEXT,
        cancel_reason TEXT NOT NULL DEFAULT '', terms_version TEXT NOT NULL DEFAULT '',
        terms_text TEXT NOT NULL DEFAULT '', signature_name TEXT NOT NULL DEFAULT '',
        signature_png TEXT NOT NULL DEFAULT '', contract_hash TEXT NOT NULL DEFAULT '',
        signed_at TEXT, signed_ip TEXT NOT NULL DEFAULT '',
        mail_admin TEXT NOT NULL DEFAULT '', mail_user TEXT NOT NULL DEFAULT '')''',
    'ALTER TABLE affiliates ENABLE ROW LEVEL SECURITY',
    'CREATE INDEX IF NOT EXISTS affiliates_status_idx ON affiliates(status)',
    'CREATE INDEX IF NOT EXISTS affiliates_email_idx ON affiliates(email)',
    '''CREATE TABLE IF NOT EXISTS refunds(
        id BIGSERIAL PRIMARY KEY, order_id BIGINT UNIQUE NOT NULL, name TEXT NOT NULL,
        phone TEXT NOT NULL, order_date TEXT NOT NULL, amount DOUBLE PRECISION NOT NULL,
        created_at TEXT NOT NULL)''',
    'ALTER TABLE refunds ENABLE ROW LEVEL SECURITY',
    '''CREATE TABLE IF NOT EXISTS reseller_payouts(
        id BIGSERIAL PRIMARY KEY, reseller_id BIGINT NOT NULL, reseller_name TEXT NOT NULL,
        amount DOUBLE PRECISION NOT NULL, method TEXT NOT NULL, account TEXT NOT NULL DEFAULT '',
        holder TEXT NOT NULL DEFAULT '', cedula TEXT NOT NULL DEFAULT '', paid_on TEXT NOT NULL,
        created_at TEXT NOT NULL)''',
    'ALTER TABLE reseller_payouts ENABLE ROW LEVEL SECURITY',
    'CREATE INDEX IF NOT EXISTS payouts_reseller_idx ON reseller_payouts(reseller_id)',
    'CREATE INDEX IF NOT EXISTS payouts_paid_on_idx ON reseller_payouts(paid_on)',

]

# Migracion v3: columnas nuevas. Se aplican UNA sola vez (marca "_schema_version" en settings)
# y son seguras de repetir (IF NOT EXISTS), asi que no se pierde ningun dato existente.
SCHEMA_VERSION = '6'
MIGRATIONS = [
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS delivery_method TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS sector TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS address TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS shipping DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS sector TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS shipping DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS subtotal DOUBLE PRECISION NOT NULL DEFAULT 0",
    # v4: vendedores y codigos de descuento
    '''CREATE TABLE IF NOT EXISTS resellers(
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)''',
    "ALTER TABLE resellers ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS reseller_id BIGINT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS reseller_name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS reseller_code TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS discount DOUBLE PRECISION NOT NULL DEFAULT 0",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS commission DOUBLE PRECISION NOT NULL DEFAULT 0",
    # v5: porcentajes por vendedor, papelera de ventas y control de costos
    f"ALTER TABLE resellers ADD COLUMN IF NOT EXISTS discount_pct DOUBLE PRECISION NOT NULL DEFAULT {RESELLER_DISCOUNT_PCT}",
    f"ALTER TABLE resellers ADD COLUMN IF NOT EXISTS commission_pct DOUBLE PRECISION NOT NULL DEFAULT {RESELLER_COMMISSION_PCT}",
    "ALTER TABLE sales ADD COLUMN IF NOT EXISTS deleted_at TEXT",
    '''CREATE TABLE IF NOT EXISTS supplies(
        id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, skey TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL DEFAULT 1, cost DOUBLE PRECISION NOT NULL DEFAULT 0,
        sale_price DOUBLE PRECISION NOT NULL DEFAULT 0, supplier TEXT NOT NULL DEFAULT '',
        bought_on TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''',
    "ALTER TABLE supplies ENABLE ROW LEVEL SECURITY",
    "CREATE INDEX IF NOT EXISTS supplies_skey_idx ON supplies(skey)",
    '''CREATE TABLE IF NOT EXISTS recipe_items(
        id BIGSERIAL PRIMARY KEY, product_id BIGINT NOT NULL, skey TEXT NOT NULL,
        qty DOUBLE PRECISION NOT NULL)''',
    "ALTER TABLE recipe_items ENABLE ROW LEVEL SECURITY",
    "CREATE INDEX IF NOT EXISTS recipe_items_pid_idx ON recipe_items(product_id)",
    # v6: columnas para solicitudes de afiliados (formulario simple) y reembolsos manuales
    "ALTER TABLE affiliates ADD COLUMN IF NOT EXISTS socials TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pendiente'",
    "ALTER TABLE refunds ADD COLUMN IF NOT EXISTS payment_method TEXT NOT NULL DEFAULT ''",
]

_schema_ready = False
_schema_lock = Lock()

DEFAULT_SETTINGS = {
    'brand_name': 'VIANE DELICATESSEN', 'logo': '', 'whatsapp': '18296759640',
    'instagram': 'vianedelicatessen', 'background_type': 'color', 'background_value': '#FFFDF5',
    'review_url': '', 'tips_message': '¡Gracias por apoyar a VIANE Delicatessen!',
    'music_url': '', 'music_title': '', 'music_kind': 'audio',
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


def migrate(con):
    """Aplica la migracion v3 una sola vez. Usa un candado para que dos instancias
    de Vercel arrancando a la vez no choquen."""
    def current():
        row = con.execute("SELECT value FROM settings WHERE key='_schema_version'").fetchone()
        return row['value'] if row else None

    if current() == SCHEMA_VERSION:
        return
    con.execute('SELECT pg_advisory_xact_lock(%s)', (918273646,))
    if current() == SCHEMA_VERSION:  # otra instancia la termino mientras esperabamos
        return
    for stmt in MIGRATIONS:
        con.execute(stmt)
    try:  # categoria "personalizado" y pedidos ya entregados -> ventas (no duplica nada)
        with con.transaction():
            ensure_category(con, PROTECTED_CATEGORY)
            for o in con.execute('SELECT * FROM orders WHERE status = ANY(%s)',
                                 (list(SALE_STATUSES),)).fetchall():
                save_sale(con, o, _dr_date(o['updated_at']))
    except Exception as exc:
        print(f'[VIANE] Migracion v3 (categoria/ventas): {exc!r}')
    con.execute("INSERT INTO settings(key,value) VALUES('_schema_version',%s) "
                "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (SCHEMA_VERSION,))


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
                try:  # categorias iniciales y ordenar los productos que ya existan
                    with con.transaction():
                        for n in DEFAULT_CATEGORIES:
                            ensure_category(con, n)
                        reclassify_all(con)
                except Exception as exc:
                    print(f'[VIANE] Migracion de categorias: {exc!r}')
            migrate(con)  # columnas nuevas (direccion, sector, envio...) y ventas
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


def public_settings(c):
    """Ajustes visibles al navegador: sin las claves internas (empiezan con _)."""
    return {k: v for k, v in setting_map(c).items() if not k.startswith('_')}


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


def safe_media_url(v):
    """Solo se acepta un archivo propio (/media/<id>) o un enlace https (Cloudinary)."""
    v = str(v or '').strip()[:1000]
    return v if (v.startswith('/media/') or v.lower().startswith('https://')) else ''


# --- Categorias automaticas y ventas -----------------------------------------
DR_TZ = timezone(timedelta(hours=-4))  # Republica Dominicana (UTC-4, sin horario de verano)
DEFAULT_CATEGORIES = ['desayunos', 'bandejas', 'picaderas', 'bizcochos', 'mariscos', 'regalos',
                      'eventos', 'personalizado']
# "personalizado" es una categoria protegida: solo se asigna a mano (nunca por palabras del
# nombre) y nunca se reclasifica. Sus productos salen en "Todos" y en "Personalizado" solamente.
PROTECTED_CATEGORY = 'personalizado'
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


# Categorias que el sistema asigna solo (por palabras del nombre) o que tienen reglas propias:
# se pueden ocultar o eliminar, pero NO renombrar (si no, volverian a crearse con el nombre viejo).
LOCKED_CATEGORIES = {c for c, _ in CATEGORY_RULES} | {PROTECTED_CATEGORY, 'otros'}


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
        if cat in ('otros', PROTECTED_CATEGORY):
            continue
        n = _norm(cat).rstrip('s')
        if len(n) >= 3 and re.search(r'\b' + re.escape(n), t):
            return cat
    return None


def ensure_category(c, name):
    c.execute('INSERT INTO categories(name,active,created_at) VALUES(%s,1,%s) '
              'ON CONFLICT (name) DO NOTHING', (name, now()))


def category_names(c):
    return [r['name'] for r in c.execute('SELECT name FROM categories ORDER BY id').fetchall()]


def resolve_category(c, name, typed=''):
    # Regla de visibilidad: si se elige "personalizado", se respeta SIEMPRE (aunque el
    # nombre diga "Desayuno" o "Bandeja") para que no se cuele en otras secciones.
    if clean_cat(typed) == PROTECTED_CATEGORY:
        ensure_category(c, PROTECTED_CATEGORY)
        return PROTECTED_CATEGORY
    names = category_names(c)
    cat = detect_category(name, names) or detect_category(typed, names) or clean_cat(typed) or 'otros'
    ensure_category(c, cat)
    return cat


def reclassify_all(c):
    names, changed = category_names(c), 0
    for p in c.execute('SELECT id,name,category FROM products').fetchall():
        if p['category'] == PROTECTED_CATEGORY:  # los personalizados no se mueven nunca
            ensure_category(c, PROTECTED_CATEGORY)
            continue
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


# --- Entrega, sector y codigo de seguimiento ---------------------------------
def sector_from(method='', text=''):
    """Sector de entrega. Pedidos nuevos: sale del metodo. Pedidos viejos: del texto de
    'province' (por ejemplo 'Envío a Santiago')."""
    if method in DELIVERY:
        return DELIVERY[method]['sector']
    t = _norm(text)
    if 'recoger' in t:
        return 'Recoger en local'
    if 'santiago' in t:
        return 'Santiago'
    if 'santo domingo' in t or 'capital' in t:
        return 'Santo Domingo'
    if 'plata' in t:
        return 'Puerto Plata'
    return str(text or '').strip()[:60] or 'Sin sector'


def method_from(method='', province=''):
    """Metodo de entrega valido. Si el navegador no lo manda (pagina vieja en cache),
    se deduce del texto de la provincia; si no se entiende, se usa 'recoger en local'."""
    if method in DELIVERY:
        return method
    sector = sector_from('', province)
    for key, info in DELIVERY.items():
        if info['sector'] == sector:
            return key
    return 'pickup'


def normalize_code(raw):
    """Convierte lo que escribe el cliente en la parte unica del codigo.
    'via-49478bb8', ' VIA 49478BB8 ', 'VIA‑49478BB8' (guion raro) y '49478BB8' dan lo mismo.
    El codigo solo usa 0-9 y A-F, por eso O -> 0 e I/L -> 1 son correcciones seguras."""
    s = unicodedata.normalize('NFKC', str(raw or '')).upper()
    s = re.sub(r'[^A-Z0-9]', '', s)  # quita espacios, guiones de cualquier tipo, invisibles...
    if s.startswith('VIA'):
        s = s[3:]
    return s.translate(str.maketrans('OIL', '011'))


def clean_reseller_code(v):
    """Codigo de vendedor: mayusculas, solo letras, numeros, guion y guion bajo (max. 20)."""
    s = unicodedata.normalize('NFKC', str(v or '')).upper()
    return re.sub(r'[^A-Z0-9_-]', '', s)[:20]


def find_reseller(c, raw):
    """Vendedor ACTIVO con ese codigo, o None."""
    code = clean_reseller_code(raw)
    if not code:
        return None
    row = c.execute('SELECT id,name,code,active,discount_pct,commission_pct FROM resellers WHERE code=%s',
                    (code,)).fetchone()
    return row if row and row['active'] else None


def reseller_pcts(r):
    """(% de descuento al cliente, % de comision del vendedor) de ese vendedor."""
    d = r.get('discount_pct') if r else None
    m = r.get('commission_pct') if r else None
    return (RESELLER_DISCOUNT_PCT if d is None else float(d),
            RESELLER_COMMISSION_PCT if m is None else float(m))


def parse_pct(v, default):
    """Porcentaje entre 0 y 100. Vacio -> valor por defecto. Invalido -> ValueError."""
    if v is None or str(v).strip() == '':
        return default
    x = to_float(str(v).replace(',', '.'))
    if x is None or not 0 <= x <= 100:
        raise ValueError('porcentaje')
    return round(x, 2)


def num(v, default=None):
    if v is None or str(v).strip() == '':
        return default
    return to_float(str(v).replace(',', '.'))

# ---------------------------------------------------------------------------
# Correo (SMTP) y Contratos de Afiliados
# ---------------------------------------------------------------------------
def smtp_configured():
    return bool(os.environ.get('SMTP_USER') and os.environ.get('SMTP_PASSWORD'))

def send_mail(to, subject, text, attachments=()):
    if not smtp_configured():
        return False, 'sin configurar'
    try:
        user, password = os.environ['SMTP_USER'], os.environ['SMTP_PASSWORD']
        host = os.environ.get('SMTP_HOST') or 'smtp.gmail.com'
        port = int(os.environ.get('SMTP_PORT') or 465)
        msg = EmailMessage()
        msg['From'] = os.environ.get('MAIL_FROM') or user
        msg['To'] = str(to or '')[:200]
        msg['Subject'] = str(subject or '')[:180]
        msg.set_content(text)
        for filename, data, mime in attachments:
            maintype, subtype = mime.split('/', 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        ctx = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=12, context=ctx) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=12) as s:
                s.starttls(context=ctx)
                s.login(user, password)
                s.send_message(msg)
        return True, 'enviado'
    except Exception as exc:
        print(f'[VIANE] correo fallo: {type(exc).__name__}')
        return False, 'error: ' + type(exc).__name__

def contract_html(a):
    e = html.escape
    sig_img = ''
    if a.get('signature_png'):
        sig_img = f'<img alt="Firma" src="{e(a["signature_png"], quote=True)}" style="max-width:360px;height:auto">'
    signed = (a.get('signed_at') or '')[:19].replace('T', ' ')
    rows_ = [
        ('Nombre completo', a.get('full_name', '')), ('Correo', a.get('email', '')),
        ('Teléfono / WhatsApp', a.get('phone', '')), ('Fecha de nacimiento', a.get('birth_date', '')),
        ('Red principal', a.get('platform', '')), ('Perfil', a.get('profile', '')),
        ('Seguidores declarados', f"{int(a.get('followers') or 0):,}"),
    ]
    data = ''.join(f'<tr><th>{e(k)}</th><td>{e(str(v))}</td></tr>' for k, v in rows_)
    return f'''<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Contrato de afiliado #{a['id']}</title>
<style>body{{font-family:Georgia,serif;max-width:760px;margin:30px auto;padding:0 16px;color:#222;line-height:1.6}}
h1{{font-size:20px;color:#7A0C16}}table{{border-collapse:collapse;width:100%;margin:14px 0}}th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px}}
th{{background:#f7f1e3;width:36%}}pre{{white-space:pre-wrap;font-family:inherit;font-size:14px;background:#fffdf5;border:1px solid #eee;padding:14px;border-radius:8px}}
.seal{{border:2px solid #9A7B1C;border-radius:10px;padding:12px;margin-top:18px;font-size:13px;word-break:break-all}}</style></head><body>
<h1>VIANE DELICATESSEN — Contrato de afiliado #{a['id']}</h1>
<table>{data}</table>
<pre>{e(a.get('terms_text') or TERMS_TEXT)}</pre>
<p><b>Firmado por:</b> {e(a.get('signature_name') or '')}<br>{sig_img}</p>
<div class="seal"><b>Contrato sellado</b><br>Fecha y hora (UTC): {e(signed)}<br>IP: {e(a.get('signed_ip') or '')}<br>
Versión de términos: {e(a.get('terms_version') or '')}<br>Sello (SHA-256): {e(a.get('contract_hash') or '')}</div>
</body></html>'''

def seal_hash(a, sig_sha):
    parts = [a['terms_version'], hashlib.sha256(a['terms_text'].encode('utf-8')).hexdigest(), a['full_name'],
             a['email'], a['birth_date'], str(a['followers']), a['platform'], a['profile'],
             a['signature_name'], sig_sha, a['signed_at'], a['signed_ip']]
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()

def png_bytes(data_url):
    m = re.fullmatch(r'data:image/png;base64,([A-Za-z0-9+/=]+)', data_url or '')
    if not m:
        return b''
    try:
        return base64.b64decode(m.group(1))
    except Exception:
        return b''

def send_application_mails(a):
    doc = contract_html(a).encode('utf-8')
    png = png_bytes(a.get('signature_png'))
    atts = [(f'contrato-afiliado-{a["id"]}.html', doc, 'text/html')]
    if png:
        atts.append((f'firma-{a["id"]}.png', png, 'image/png'))
    seal = (a['contract_hash'] or '')[:12].upper()
    admin_text = (
        f"Nueva solicitud de afiliado FIRMADA (contrato sellado)\n\n"
        f"Nombre: {a['full_name']}\nCorreo: {a['email']}\nTeléfono: {a['phone']}\n"
        f"Fecha de nacimiento: {a['birth_date']}\nRed principal: {a['platform']} — {a['profile']}\n"
        f"Seguidores declarados: {int(a['followers']):,}\nFirma (nombre): {a['signature_name']}\n"
        f"Fecha de firma (UTC): {(a['signed_at'] or '')[:19]}\nSello del contrato: {seal}\n"
        f"Versión de términos: {a['terms_version']}\n\n"
        f"Estado: pendiente. Plazo de respuesta: 1 a 30 días; si no hay respuesta, la evaluación se "
        f"extiende de 30 días a 12 meses.\nSe adjunta la copia del contrato firmado."
    )
    ok_a, st_a = send_mail(NOTIFY_EMAIL, f"Nueva solicitud de afiliado: {a['full_name']}", admin_text, atts)
    user_text = (
        f"Hola {a['full_name']},\n\n"
        f"¡Gracias por solicitar entrar al esquema de afiliados de VIANE Delicatessen!\n\n"
        f"Hemos recibido tu solicitud y tu contrato firmado. Estaremos revisando tu perfil y te daremos una "
        f"respuesta en un plazo de 1 a 30 días. En caso de no recibir ninguna notificación dentro de este "
        f"plazo inicial, el proceso de evaluación se extenderá por un período de 30 días a 12 meses mientras "
        f"validamos la disponibilidad y el cumplimiento de nuestros criterios.\n\n"
        f"Sello de tu contrato: {seal}\nAdjuntamos una copia para tus registros.\n\n"
        f"VIANE Delicatessen · WhatsApp 829-675-9640"
    )
    ok_u, st_u = send_mail(a['email'], 'Gracias por tu solicitud de afiliación — VIANE Delicatessen', user_text, atts)
    return st_a, st_u
def notify_new_affiliate(a):
    """Aviso simple por correo para el formulario corto de afiliados (sin contrato firmado)."""
    text = (
        f"Nueva solicitud de afiliado (formulario simple)\n\n"
        f"Nombre: {a['full_name']}\nTeléfono: {a['phone']}\nCorreo: {a.get('email', '')}\n"
        f"Redes: {a.get('socials', '')}\nPerfil: {a.get('profile', '')}\n\nEstado: pendiente."
    )
    send_mail(NOTIFY_EMAIL, f"Nueva solicitud de afiliado: {a['full_name']}", text)
  
def refund_hook(c, oid, status, payment):
    if status == 'Reembolsado' or payment == 'Reembolsado':
        o = c.execute('SELECT id,name,phone,total,created_at FROM orders WHERE id=%s', (oid,)).fetchone()
        if not o:
            return
        order_date = _dr_date(o['created_at'])
        c.execute('INSERT INTO refunds(order_id,name,phone,order_date,amount,created_at) '
                  'VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING',
                  (o['id'], o['name'], o['phone'], order_date, float(o['total']), now()))
        c.execute('DELETE FROM sales WHERE order_id=%s', (oid,))
    else:
        c.execute('DELETE FROM refunds WHERE order_id=%s', (oid,))


def supply_key(name):
    """Clave para agrupar insumos iguales ('Uvas ' y 'uvas' son el mismo insumo)."""
    return re.sub(r'\s+', ' ', _norm(name))[:80]


def clean_supply(r):
    """Valida una fila de insumo. Devuelve (name, skey, qty, cost, sale_price, supplier, bought_on)."""
    if not isinstance(r, dict):
        raise ValueError('fila inválida')
    name = str(r.get('name') or '').strip()[:80]
    if not name:
        raise ValueError('escribe el nombre del insumo')
    qty, cost, price = num(r.get('qty'), 1.0), num(r.get('cost'), 0.0), num(r.get('sale_price'), 0.0)
    if qty is None or qty <= 0:
        raise ValueError('la cantidad debe ser mayor que 0')
    if cost is None or cost < 0 or price is None or price < 0:
        raise ValueError('costo y precio de venta deben ser números válidos')
    supplier = str(r.get('supplier') or '').strip()[:80]
    bought = str(r.get('bought_on') or '').strip()
    if bought and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', bought):
        raise ValueError('fecha de compra inválida')
    return name, supply_key(name), qty, cost, price, supplier, bought


def product_costs(c):
    """Costo de produccion por producto = suma(cantidad usada x costo promedio unitario del insumo).
    El promedio sale de TODAS las compras de ese insumo (costo total / cantidad total)."""
    data = c.execute(
        'SELECT ri.product_id, SUM(ri.qty * u.unit) AS cost FROM recipe_items ri '
        'JOIN (SELECT skey, SUM(cost)/NULLIF(SUM(qty),0) AS unit FROM supplies GROUP BY skey) u '
        'ON u.skey=ri.skey GROUP BY ri.product_id').fetchall()
    return {r['product_id']: float(r['cost']) for r in data if r['cost']}


def order_cost(raw, pcosts):
    """(precio de lista, costo de produccion, costo_incompleto) de los productos de un pedido."""
    try:
        items = json.loads(raw or '[]')
    except Exception:
        items = []
    if not isinstance(items, list):
        items = []
    gross = cost = 0.0
    missing = not items
    for it in items:
        if not isinstance(it, dict):
            continue
        gross += to_float(it.get('price')) or 0.0
        pc = pcosts.get(it.get('id'))
        if pc is None:
            missing = True
        else:
            cost += pc
    return gross, cost, missing


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


def product_lines(raw):
    """Productos de un pedido con cantidad y precio unitario (para la vista de ventas)."""
    try:
        items = json.loads(raw or '[]')
    except Exception:
        return []
    agg = {}
    for i in (items if isinstance(items, list) else []):
        if not isinstance(i, dict):
            continue
        n = str(i.get('name', '')).strip()
        if not n:
            continue
        key = (n, to_float(i.get('price')) or 0.0)
        agg[key] = agg.get(key, 0) + 1
    return [{'name': n, 'qty': q, 'price': p} for (n, p), q in agg.items()]


def save_sale(c, o, sold_date=None):
    method = o.get('delivery_method') or ''
    sector = o.get('sector') or sector_from(method, o.get('province'))
    shipping = float(o.get('shipping') or 0)
    total = float(o['total'])
    c.execute(
        'INSERT INTO sales(order_id,code,customer,contact,purchase,address,amount,sold_date,created_at,'
        'sector,shipping,subtotal) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) '
        'ON CONFLICT (order_id) DO NOTHING',
        (o['id'], o['code'], o['name'], o['phone'], items_text(o['items']), o.get('address') or '',
         total, sold_date or _dr_date(), now(), sector, shipping, total - shipping))


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
        # Botones de la barra del catalogo: todas las categorias visibles (aunque esten vacias).
        category_list = [r['name'] for r in rows(c, 'SELECT name FROM categories WHERE active=1 ORDER BY id')]
        reviews = rows(c, 'SELECT id,name,rating,products,comment,admin_reply,created_at '
                          'FROM reviews WHERE approved=1 ORDER BY id DESC')
        ads = rows(c, 'SELECT * FROM ads WHERE active=1 ORDER BY id')
        payments = rows(c, 'SELECT id,title,url,details,tip_enabled FROM payments WHERE active=1 ORDER BY id')
        settings = public_settings(c)
    return jsonify({'products': products, 'categories': categories, 'category_list': category_list,
                    'reviews': reviews, 'ads': ads,
                    'payments': payments, 'settings': settings, 'statuses': STATUSES,
                    'delivery': DELIVERY, 'reseller_discount': RESELLER_DISCOUNT_PCT})


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

    # El envio lo decide el servidor: el navegador solo dice QUE metodo eligio.
    method = method_from(str(d.get('delivery_method', '')).strip(), d.get('province', ''))
    delivery = DELIVERY[method]
    address = str(d.get('address', '')).strip()[:300]
    raw_code = str(d.get('reseller_code', '')).strip()

    with db() as c:
        reseller = None
        if raw_code:  # el servidor valida el codigo otra vez: el descuento nunca depende del navegador
            reseller = find_reseller(c, raw_code)
            if not reseller:
                return jsonify({'error': 'El código de vendedor no es válido o está inactivo. '
                                         'Corrígelo o bórralo para continuar.'}), 400
        found = rows(c, 'SELECT * FROM products WHERE active=1 AND id = ANY(%s)', (list(set(ids)),))
        byid = {r['id']: r for r in found}
        clean, subtotal = [], 0.0
        for pid in ids:  # cada elemento del carrito cuenta una vez (igual que antes)
            p = byid.get(pid)
            if p:
                clean.append({'id': pid, 'name': p['name'], 'price': p['price']})
                subtotal += p['price']
        if not clean:
            return jsonify({'error': 'No hay productos válidos'}), 400
        shipping = float(delivery['cost'])
        # Descuento al cliente y ganancia del vendedor (ver RESELLER_* arriba).
        disc_pct, comm_pct = reseller_pcts(reseller)  # porcentajes propios de ese vendedor
        discount = round(subtotal * disc_pct / 100, 2) if reseller else 0.0
        net = subtotal - discount
        commission = round(net * comm_pct / 100, 2) if reseller else 0.0
        total = net + shipping
        code = 'VIA-' + secrets.token_hex(4).upper()
        c.execute(
            'INSERT INTO orders(code,name,phone,province,delivery_date,items,total,payment_status,status,'
            'notes,created_at,updated_at,delivery_method,sector,address,shipping,'
            'reseller_id,reseller_name,reseller_code,discount,commission) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (code, name, phone, delivery['label'], str(d.get('delivery_date', ''))[:100],
             json.dumps(clean, ensure_ascii=False), total, 'Pago pendiente', 'Pago pendiente',
             str(d.get('notes', ''))[:2000], now(), now(), method, delivery['sector'], address, shipping,
             reseller['id'] if reseller else None, reseller['name'] if reseller else '',
             reseller['code'] if reseller else '', discount, commission))
        s = setting_map(c)
    return jsonify({'ok': True, 'code': code, 'subtotal': subtotal, 'discount': discount,
                    'reseller_code': reseller['code'] if reseller else '',
                    'shipping': shipping, 'total': total,
                    'delivery_label': delivery['label'], 'sector': delivery['sector'],
                    'review_url': s.get('review_url', ''), 'whatsapp': s.get('whatsapp', '18296759640')})


@app.post('/api/resellers/validate')
def validate_reseller():
    """Publico: comprueba un codigo y devuelve SOLO su % de descuento (no el nombre del vendedor)."""
    code = clean_reseller_code(body().get('code'))
    if not code:
        return jsonify({'valid': False, 'error': 'Escribe el código'}), 404
    with db() as c:
        r = find_reseller(c, code)
    if not r:
        return jsonify({'valid': False, 'error': 'Código no válido o inactivo'}), 404
    return jsonify({'valid': True, 'code': r['code'], 'discount_pct': reseller_pcts(r)[0]})


@app.get('/api/track/<code>')
def track_order(code):
    key = normalize_code(code)
    if len(key) < 4:
        return jsonify({'error': 'Escribe el código completo de tu pedido (ejemplo: VIA-A1B2C3D4).'}), 404
    cols = 'code,status,payment_status,delivery_date,updated_at,incident'
    try:
        with db() as c:
            # 1) Busqueda exacta (usa el indice de la columna code).
            row = c.execute(f'SELECT {cols} FROM orders WHERE code=%s', ('VIA-' + key,)).fetchone()
            # 2) Respaldo: compara sin guiones ni espacios y sin importar mayusculas, por si el
            #    codigo guardado trae algun caracter distinto al que escribe el cliente.
            if not row:
                row = c.execute(
                    f"SELECT {cols} FROM orders WHERE REGEXP_REPLACE(UPPER(code),'[^A-Z0-9]','','g')=%s "
                    'ORDER BY id DESC LIMIT 1', ('VIA' + key,)).fetchone()
    except Exception:
        # Un fallo de conexion YA NO se disfraza de "pedido no existe".
        app.logger.exception('Seguimiento: fallo al consultar la base de datos')
        return jsonify({'error': 'No pudimos consultar el pedido en este momento (problema de conexión). '
                                 'Intenta de nuevo en un minuto.'}), 503
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
        cats = rows(c, 'SELECT k.id,k.name,k.active,(SELECT COUNT(*) FROM products p '
                       'WHERE p.category=k.name) AS total FROM categories k ORDER BY k.id')
        for k in cats:
            k['locked'] = k['name'] in LOCKED_CATEGORIES
        return jsonify({
            'products': rows(c, 'SELECT * FROM products ORDER BY id DESC'),
            'reviews': rows(c, 'SELECT * FROM reviews ORDER BY id DESC'),
            # Entregado / Terminado ya no se listan aqui: viven en Ventas.
            'orders': rows(c, 'SELECT * FROM orders WHERE NOT (status = ANY(%s)) ORDER BY id DESC',
                           (list(SALE_STATUSES),)),
            'categories': cats,
            'ads': rows(c, 'SELECT * FROM ads ORDER BY id DESC'),
            'payments': rows(c, 'SELECT * FROM payments ORDER BY id DESC'),
            'resellers': rows(c, 'SELECT r.id,r.name,r.code,r.active,r.created_at,r.discount_pct,r.commission_pct,'
                                 '(SELECT COUNT(*) FROM orders o WHERE o.reseller_id=r.id) AS orders_count,'
                                 # cobrable: pedidos cuya venta existe y NO esta eliminada
                                 '(SELECT COALESCE(SUM(o.commission),0) FROM orders o '
                                 ' WHERE o.reseller_id=r.id AND EXISTS (SELECT 1 FROM sales s '
                                 '  WHERE s.order_id=o.id AND s.deleted_at IS NULL)) AS earned,'
                                 '(SELECT COALESCE(SUM(o.commission),0) FROM orders o '
                                 ' WHERE o.reseller_id=r.id AND NOT (o.status = ANY(%s))) AS pending '
                                 'FROM resellers r ORDER BY r.id DESC',
                              (list(SALE_STATUSES),)),
            'reseller_discount': RESELLER_DISCOUNT_PCT,
            'reseller_commission': RESELLER_COMMISSION_PCT,
            'settings': public_settings(c),
            'statuses': STATUSES,
        })


# --- Subida de archivos (se guardan en Supabase) ------------------------------
@app.post('/api/upload')
@admin_required
def upload():
    """Guarda un archivo (hasta 4 MB) en Supabase. Devuelve {'url', 'filename', 'type'}.
    La url (/media/<id>) es la que se guarda en el producto, logo, anuncio o reproductor."""
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
    return jsonify({'url': f"/media/{row['id']}", 'filename': name, 'type': MEDIA_TYPES[ext]})


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
        c.execute('DELETE FROM recipe_items WHERE product_id=%s', (pid,))
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
        # Entregado / Terminado -> pasa solo a Ventas; cualquier otro estado -> se quita de Ventas.
        if status in SALE_STATUSES:
            save_sale(c, c.execute('SELECT * FROM orders WHERE id=%s', (oid,)).fetchone())
        else:
            c.execute('DELETE FROM sales WHERE order_id=%s', (oid,))
    return jsonify({'ok': True, 'moved_to_sales': status in SALE_STATUSES})


# --- Ajustes ----------------------------------------------------------------------
@app.put('/api/settings')
@admin_required
def update_settings():
    d = body()
    allowed = {'brand_name', 'logo', 'whatsapp', 'instagram', 'background_type',
               'background_value', 'review_url', 'tips_message',
               'music_url', 'music_title', 'music_kind'}
    with db() as c:
        for k, v in d.items():
            if k not in allowed:
                continue
            v = '' if v is None else str(v)
            if k == 'music_url':
                v = safe_media_url(v)
            elif k == 'music_kind':
                v = v if v in ('audio', 'video') else 'audio'
            c.execute('INSERT INTO settings(key,value) VALUES(%s,%s) '
                      'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', (k, v[:4000]))
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
    """Renombra y/o muestra-oculta una categoria. Solo cambia lo que viene en la peticion:
    {'name': 'nuevo'} renombra (y mueve sus productos), {'active': false} la oculta."""
    d = body()
    with db() as c:
        row = c.execute('SELECT name FROM categories WHERE id=%s', (cid,)).fetchone()
        if not row:
            return jsonify({'error': 'Categoría no encontrada'}), 404
        if 'name' in d:
            old, new = row['name'], clean_cat(d.get('name'))
            if not new:
                return jsonify({'error': 'Escribe el nombre de la categoría'}), 400
            if new != old:
                if old in LOCKED_CATEGORIES:
                    return jsonify({'error': 'Esta categoría se asigna automáticamente por el nombre del '
                                             'producto y no se puede renombrar. Puedes ocultarla.'}), 400
                if c.execute('SELECT 1 FROM categories WHERE name=%s AND id<>%s', (new, cid)).fetchone():
                    return jsonify({'error': 'Ya existe una categoría con ese nombre'}), 400
                c.execute('UPDATE categories SET name=%s WHERE id=%s', (new, cid))
                c.execute('UPDATE products SET category=%s WHERE category=%s', (new, old))
        if 'active' in d:
            c.execute('UPDATE categories SET active=%s WHERE id=%s', (int(bool(d['active'])), cid))
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


# --- Vendedores (revendedores) ----------------------------------------------------
@app.post('/api/resellers')
@admin_required
def add_reseller():
    d = body()
    name = str(d.get('name', '')).strip()[:80]
    if not name:
        return jsonify({'error': 'Escribe el nombre del vendedor'}), 400
    raw = str(d.get('code') or '').strip()
    code = clean_reseller_code(raw) if raw else 'VND-' + secrets.token_hex(3).upper()
    if len(code) < 3:
        return jsonify({'error': 'El código debe tener al menos 3 caracteres (letras, números o guion)'}), 400
    try:
        disc = parse_pct(d.get('discount_pct'), RESELLER_DISCOUNT_PCT)
        comm = parse_pct(d.get('commission_pct'), RESELLER_COMMISSION_PCT)
    except ValueError:
        return jsonify({'error': 'Los porcentajes deben estar entre 0 y 100'}), 400
    with db() as c:
        if c.execute('SELECT 1 FROM resellers WHERE code=%s', (code,)).fetchone():
            return jsonify({'error': 'Ese código ya existe. Elige otro.'}), 400
        row = c.execute('INSERT INTO resellers(name,code,active,created_at,discount_pct,commission_pct) '
                        'VALUES(%s,%s,1,%s,%s,%s) RETURNING id', (name, code, now(), disc, comm)).fetchone()
    return jsonify({'ok': True, 'id': row['id'], 'code': code})


@app.put('/api/resellers/<int:rid>')
@admin_required
def update_reseller(rid):
    """Cambia solo lo que viene en la peticion: name, code, active, discount_pct y/o commission_pct."""
    d = body()
    with db() as c:
        if not c.execute('SELECT 1 FROM resellers WHERE id=%s', (rid,)).fetchone():
            return jsonify({'error': 'Vendedor no encontrado'}), 404
        sets, vals = [], []
        if 'name' in d:
            name = str(d['name']).strip()[:80]
            if not name:
                return jsonify({'error': 'Escribe el nombre del vendedor'}), 400
            sets.append('name=%s'); vals.append(name)
        if 'code' in d:
            code = clean_reseller_code(d['code'])
            if len(code) < 3:
                return jsonify({'error': 'El código debe tener al menos 3 caracteres (letras, números o guion)'}), 400
            if c.execute('SELECT 1 FROM resellers WHERE code=%s AND id<>%s', (code, rid)).fetchone():
                return jsonify({'error': 'Ese código ya existe. Elige otro.'}), 400
            sets.append('code=%s'); vals.append(code)
        for field in ('discount_pct', 'commission_pct'):
            if field in d:
                try:
                    pct = parse_pct(d[field], None)
                except ValueError:
                    pct = None
                if pct is None:
                    return jsonify({'error': 'Los porcentajes deben estar entre 0 y 100'}), 400
                sets.append(f'{field}=%s'); vals.append(pct)
        if 'active' in d:
            sets.append('active=%s'); vals.append(int(bool(d['active'])))
        if sets:
            c.execute(f"UPDATE resellers SET {','.join(sets)} WHERE id=%s", (*vals, rid))
    return jsonify({'ok': True})


@app.delete('/api/resellers/<int:rid>')
@admin_required
def delete_reseller(rid):
    # Los pedidos ya hechos conservan el nombre y el codigo del vendedor (copia en el pedido).
    with db() as c:
        c.execute('DELETE FROM resellers WHERE id=%s', (rid,))
    return jsonify({'ok': True})


# --- Control de costos: insumos (compras) y recetas por bandeja -------------------
@app.get('/api/costs')
@admin_required
def costs_data():
    """Todo lo de la pagina de Costos en UNA sola peticion (ligera: sin imagenes)."""
    with db() as c:
        supplies = rows(c, 'SELECT id,name,qty,cost,sale_price,supplier,bought_on FROM supplies '
                           'ORDER BY id DESC LIMIT 500')
        ingredients = rows(c, 'SELECT MIN(name) AS name, skey, SUM(qty) AS qty, SUM(cost) AS cost '
                              'FROM supplies GROUP BY skey ORDER BY MIN(name)')
        products = rows(c, 'SELECT id,name,price FROM products ORDER BY name')
        recipes = rows(c, 'SELECT product_id,skey,qty FROM recipe_items ORDER BY id')
    return jsonify({'supplies': supplies, 'ingredients': ingredients, 'products': products,
                    'recipes': recipes})


@app.post('/api/supplies/bulk')
@admin_required
def add_supplies():
    """Agrega hasta 20 insumos de una vez (todo o nada)."""
    items = body().get('rows')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'No hay filas para guardar'}), 400
    if len(items) > 20:
        return jsonify({'error': 'Máximo 20 insumos por vez'}), 400
    clean = []
    for n, r in enumerate(items, 1):
        try:
            clean.append(clean_supply(r))
        except ValueError as e:
            label = r.get('row') if isinstance(r, dict) and r.get('row') else n
            return jsonify({'error': f'Fila {label}: {e}'}), 400
    with db() as c:
        for t in clean:
            c.execute('INSERT INTO supplies(name,skey,qty,cost,sale_price,supplier,bought_on,created_at) '
                      'VALUES(%s,%s,%s,%s,%s,%s,%s,%s)', (*t, now()))
    return jsonify({'ok': True, 'count': len(clean)})


@app.put('/api/supplies/<int:sid>')
@admin_required
def update_supply(sid):
    try:
        t = clean_supply(body())
    except ValueError as e:
        return jsonify({'error': str(e).capitalize()}), 400
    with db() as c:
        old = c.execute('SELECT skey FROM supplies WHERE id=%s', (sid,)).fetchone()
        if not old:
            return jsonify({'error': 'Insumo no encontrado'}), 404
        c.execute('UPDATE supplies SET name=%s,skey=%s,qty=%s,cost=%s,sale_price=%s,supplier=%s,bought_on=%s '
                  'WHERE id=%s', (*t, sid))
        # Si se renombro el unico registro de ese insumo, las recetas lo siguen.
        if old['skey'] != t[1] and not c.execute('SELECT 1 FROM supplies WHERE skey=%s', (old['skey'],)).fetchone():
            c.execute('UPDATE recipe_items SET skey=%s WHERE skey=%s', (t[1], old['skey']))
    return jsonify({'ok': True})


@app.delete('/api/supplies/<int:sid>')
@admin_required
def delete_supply(sid):
    with db() as c:
        c.execute('DELETE FROM supplies WHERE id=%s', (sid,))
    return jsonify({'ok': True})


@app.put('/api/recipes/<int:pid>')
@admin_required
def save_recipe(pid):
    """Reemplaza la receta (insumos y cantidades usadas) de una bandeja."""
    items = body().get('items')
    if not isinstance(items, list) or len(items) > 40:
        return jsonify({'error': 'Receta inválida (máximo 40 insumos)'}), 400
    clean = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        key, q = str(it.get('skey') or '').strip()[:80], num(it.get('qty'))
        if key and q is not None and q > 0:
            clean[key] = clean.get(key, 0.0) + q
    with db() as c:
        if not c.execute('SELECT 1 FROM products WHERE id=%s', (pid,)).fetchone():
            return jsonify({'error': 'Producto no encontrado'}), 404
        valid = {r['skey'] for r in rows(c, 'SELECT DISTINCT skey FROM supplies WHERE skey = ANY(%s)',
                                         (list(clean),))}
        c.execute('DELETE FROM recipe_items WHERE product_id=%s', (pid,))
        for key, q in clean.items():
            if key in valid:
                c.execute('INSERT INTO recipe_items(product_id,skey,qty) VALUES(%s,%s,%s)', (pid, key, q))
    return jsonify({'ok': True})


# --- Ventas (pedidos entregados o terminados) -----------------------------------
@app.get('/api/sales')
@admin_required
def list_sales():
    """Ventas con filtro por fechas (?from=AAAA-MM-DD&to=AAAA-MM-DD).
    Por defecto devuelve las ventas ACTIVAS; con ?deleted=1 devuelve las eliminadas (papelera).
    Las eliminadas no cuentan en ningun total y se borran de verdad despues de TRASH_DAYS."""
    want_trash = request.args.get('deleted') == '1'
    date_sql, params = '', []
    for arg, op in (('from', '>='), ('to', '<=')):
        v = (request.args.get(arg) or '').strip()
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
            date_sql += f' AND s.sold_date {op} %s'
            params.append(v)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRASH_DAYS)).isoformat()
    sql = ('SELECT s.id,s.order_id,s.code,s.customer,s.contact,s.purchase,s.address,s.amount,'
           's.sold_date,s.sector,s.shipping,s.subtotal,s.deleted_at,o.items,o.delivery_method,o.province,'
           'o.discount,o.reseller_name,o.commission '
           'FROM sales s LEFT JOIN orders o ON o.id=s.order_id '
           f"WHERE s.deleted_at IS {'NOT NULL' if want_trash else 'NULL'}")
    with db() as c:
        c.execute('DELETE FROM sales WHERE deleted_at IS NOT NULL AND deleted_at < %s', (cutoff,))
        data = rows(c, sql + date_sql + ' ORDER BY s.sold_date DESC, s.id DESC', tuple(params))
        trash_count = len(data) if want_trash else c.execute(
            'SELECT COUNT(*) AS n FROM sales s WHERE s.deleted_at IS NOT NULL' + date_sql,
            tuple(params)).fetchone()['n']
        pcosts = product_costs(c)

    sales, sectors = [], {}
    tot = {'total': 0.0, 'subtotal': 0.0, 'shipping': 0.0, 'discount': 0.0, 'commission': 0.0,
           'cost': 0.0, 'profit': 0.0}
    cost_missing_count = 0
    for r in data:
        amount = float(r['amount'] or 0)
        shipping = float(r['shipping'] or 0)
        subtotal = float(r['subtotal'] or 0)
        if not subtotal and not shipping:  # ventas anteriores a la v3: el monto era solo productos
            subtotal = amount
        discount = float(r['discount'] or 0)
        commission = float(r['commission'] or 0)
        sector = r['sector'] or sector_from(r['delivery_method'], r['province'] or r['address'])
        # En ventas viejas "address" guardaba el texto del metodo de entrega, no una direccion.
        address = r['address'] if r['sector'] else ''
        lines = product_lines(r['items']) or [{'name': r['purchase'] or 'Pedido', 'qty': 1, 'price': None}]
        # Ganancia neta = precio de lista - costo de produccion - descuento (cupon) - comision del vendedor
        gross, cost, missing = order_cost(r['items'], pcosts)
        if not r['items']:
            gross = subtotal + discount
        profit = gross - discount - cost - commission
        cost_missing_count += 1 if missing else 0

        info = sectors.setdefault(sector, {'sector': sector, 'count': 0, 'amount': 0.0})
        info['count'] += 1
        info['amount'] += amount
        for k, v in (('total', amount), ('subtotal', subtotal), ('shipping', shipping),
                     ('discount', discount), ('commission', commission), ('cost', cost), ('profit', profit)):
            tot[k] += v
        sales.append({
            'id': r['id'], 'order_id': r['order_id'], 'code': r['code'], 'customer': r['customer'],
            'contact': r['contact'], 'sector': sector, 'address': address, 'amount': amount,
            'subtotal': subtotal, 'shipping': shipping, 'sold_date': r['sold_date'], 'lines': lines,
            'discount': discount, 'reseller': r['reseller_name'] or '', 'commission': commission,
            'cost': round(cost, 2), 'profit': round(profit, 2), 'cost_missing': missing,
            'deleted_at': (r['deleted_at'] or '')[:10],
        })

    by_sector = sorted(sectors.values(), key=lambda s: s['amount'], reverse=True)
    for s in by_sector:
        s['amount'] = round(s['amount'], 2)
        s['percent'] = round(s['amount'] / tot['total'] * 100, 1) if tot['total'] else 0.0
    count = len(sales)
    return jsonify({
        'sales': sales, 'count': count, 'total': round(tot['total'], 2),
        'subtotal': round(tot['subtotal'], 2), 'shipping': round(tot['shipping'], 2),
        'average': round(tot['total'] / count, 2) if count else 0.0, 'by_sector': by_sector,
        'discount': round(tot['discount'], 2), 'commission': round(tot['commission'], 2),
        'cost': round(tot['cost'], 2), 'profit': round(tot['profit'], 2),
        'cost_missing_count': cost_missing_count, 'trash_count': trash_count,
    })


@app.post('/api/sales/<int:sid>/void')
@admin_required
def void_sale(sid):
    """Borrado logico: la venta deja de sumarse en las ventas generales pero se conserva
    (TRASH_DAYS dias) y se puede reactivar."""
    with db() as c:
        cur = c.execute('UPDATE sales SET deleted_at=%s WHERE id=%s AND deleted_at IS NULL', (now(), sid))
        if cur.rowcount == 0:
            return jsonify({'error': 'Venta no encontrada o ya eliminada'}), 404
    return jsonify({'ok': True})


@app.post('/api/sales/<int:sid>/restore')
@admin_required
def restore_sale(sid):
    with db() as c:
        cur = c.execute('UPDATE sales SET deleted_at=NULL WHERE id=%s AND deleted_at IS NOT NULL', (sid,))
        if cur.rowcount == 0:
            return jsonify({'error': 'Venta no encontrada en las eliminadas'}), 404
    return jsonify({'ok': True})


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
# --- Afiliados (formulario simple) -------------------------------------------------
@app.post('/api/affiliates')
def submit_affiliate():
    d = body()
    full_name = str(d.get('full_name', '')).strip()[:120]
    phone = str(d.get('phone', '')).strip()[:40]
    email = str(d.get('email', '')).strip().lower()[:160]
    profile_text = str(d.get('profile_text', '')).strip()[:2000]
    socials = d.get('socials', '')
    if isinstance(socials, list):
        socials = ','.join(str(s).strip() for s in socials if str(s).strip())
    else:
        socials = str(socials).strip()
    socials = socials[:500]
    if not full_name or not phone:
        return jsonify({'success': False, 'error': 'Nombre y teléfono son obligatorios'}), 400

    with db() as c:
        existing = None
        if email:
            existing = c.execute('SELECT id FROM affiliates WHERE email=%s', (email,)).fetchone()
        if not existing and phone:
            existing = c.execute('SELECT id FROM affiliates WHERE phone=%s', (phone,)).fetchone()
        if existing:
            aff_id = existing['id']
            c.execute('UPDATE affiliates SET full_name=%s,phone=%s,email=%s,profile=%s,socials=%s,'
                      "status='pendiente' WHERE id=%s",
                      (full_name, phone, email, profile_text, socials, aff_id))
        else:
            contract_hash = hashlib.sha256(f'{full_name}-{phone}-{time.time()}'.encode('utf-8')).hexdigest()
            row = c.execute(
                'INSERT INTO affiliates(full_name,email,phone,birth_date,platform,profile,socials,followers,'
                'status,created_at,contract_hash,signed_at) '
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'pendiente',%s,%s,%s) RETURNING id",
                (full_name, email, phone, '', '', profile_text, socials, 0, now(), contract_hash, now())
            ).fetchone()
            aff_id = row['id']
        affiliate = c.execute('SELECT * FROM affiliates WHERE id=%s', (aff_id,)).fetchone()

    try:
        if affiliate:
            notify_new_affiliate(affiliate)
    except Exception as exc:
        print(f'[VIANE] correo de afiliado fallo: {exc!r}')

    return jsonify({'success': True, 'id': aff_id})


@app.get('/api/affiliates')
@admin_required
def list_affiliates():
    with db() as c:
        data = rows(c, 'SELECT * FROM affiliates ORDER BY created_at DESC')
    return jsonify({'success': True, 'affiliates': data})


@app.post('/api/affiliates/<int:aff_id>/status')
@admin_required
def update_affiliate_status(aff_id):
    d = body()
    new_status = str(d.get('status', '')).strip()
    if new_status not in ('aprobado', 'rechazado', 'pendiente'):
        return jsonify({'success': False, 'error': 'Estado inválido'}), 400
    with db() as c:
        cur = c.execute('UPDATE affiliates SET status=%s,decided_at=%s WHERE id=%s',
                        (new_status, now(), aff_id))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'Afiliado no encontrado'}), 404
    return jsonify({'success': True})


# --- Reembolsos (solicitud manual) --------------------------------------------------
@app.post('/api/refunds')
def request_refund():
    """El cliente/admin pide un reembolso para una venta ya registrada (sales.id)."""
    d = body()
    sale_id = num(d.get('order_id'))
    reason = str(d.get('reason', '')).strip()[:1000]
    if not sale_id or not reason:
        return jsonify({'success': False, 'error': 'Faltan datos'}), 400
    with db() as c:
        sale = c.execute('SELECT order_id FROM sales WHERE id=%s', (int(sale_id),)).fetchone()
        if not sale or not sale['order_id']:
            return jsonify({'success': False, 'error': 'Pedido no encontrado'}), 404
        order = c.execute('SELECT id,name,phone,total,created_at FROM orders WHERE id=%s',
                          (sale['order_id'],)).fetchone()
        if not order:
            return jsonify({'success': False, 'error': 'Pedido no encontrado'}), 404
        c.execute(
            'INSERT INTO refunds(order_id,name,phone,order_date,amount,reason,status,created_at) '
            "VALUES(%s,%s,%s,%s,%s,%s,'pendiente',%s) "
            'ON CONFLICT (order_id) DO UPDATE SET reason=EXCLUDED.reason,status=EXCLUDED.status',
            (order['id'], order['name'], order['phone'], _dr_date(order['created_at']),
             float(order['total']), reason, now()))
    return jsonify({'success': True})


@app.get('/api/refunds')
@admin_required
def list_refunds():
    with db() as c:
        data = rows(c, 'SELECT r.*, o.name AS customer, o.total FROM refunds r '
                       'LEFT JOIN orders o ON o.id=r.order_id ORDER BY r.created_at DESC')
    return jsonify({'success': True, 'refunds': data})


@app.post('/api/refunds/<int:ref_id>/status')
@admin_required
def update_refund_status(ref_id):
    d = body()
    status = str(d.get('status', '')).strip()
    if status not in ('reembolsado', 'rechazado', 'pendiente'):
        return jsonify({'success': False, 'error': 'Estado inválido'}), 400
    with db() as c:
        cur = c.execute('UPDATE refunds SET status=%s WHERE id=%s', (status, ref_id))
        if cur.rowcount == 0:
            return jsonify({'success': False, 'error': 'Reembolso no encontrado'}), 404
    return jsonify({'success': True})


# --- Pagos a vendedores (payouts) ----------------------------------------------------
@app.get('/api/payouts')
@admin_required
def list_payouts():
    with db() as c:
        data = rows(c, 'SELECT * FROM reseller_payouts ORDER BY created_at DESC')
    return jsonify({'success': True, 'payouts': data})


@app.post('/api/payouts')
@admin_required
def add_payout():
    d = body()
    reseller_id = num(d.get('reseller_id'))
    amount = num(d.get('amount'))
    if not reseller_id or not amount or amount <= 0:
        return jsonify({'success': False, 'error': 'Datos de pago inválidos'}), 400
    with db() as c:
        r = c.execute('SELECT id,name FROM resellers WHERE id=%s', (int(reseller_id),)).fetchone()
        if not r:
            return jsonify({'success': False, 'error': 'Vendedor no encontrado'}), 404
        c.execute(
            'INSERT INTO reseller_payouts(reseller_id,reseller_name,amount,method,account,holder,cedula,'
            'paid_on,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
            (r['id'], r['name'], amount, str(d.get('method', '')).strip()[:40],
             str(d.get('account', '')).strip()[:80], str(d.get('holder', '')).strip()[:120],
             str(d.get('cedula', '')).strip()[:40], _dr_date(), now()))
    return jsonify({'success': True})


if __name__ == '__main__':
    # Desarrollo local: python app.py  ->  http://localhost:5000
    app.run(debug=os.environ.get('FLASK_DEBUG') == '1', host='0.0.0.0', port=5000)
