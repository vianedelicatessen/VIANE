"""
VIANE DELICATESSEN - modulos EXTRA y AISLADOS.

Este archivo se carga desde app.py con un try/except: si falla o se borra, el sitio sigue
funcionando exactamente igual que antes. Tiene sus propias tablas (se crean solas) y sus
propias rutas; no cambia ninguna ruta existente.

  1. AFILIADOS: solicitud con requisitos (mayor de edad y minimo 500 seguidores), terminos,
     firma digital y contrato sellado; correos (copia al negocio y gracias al solicitante);
     plazos 1-30 dias -> evaluacion extendida hasta 12 meses; cancelacion automatica a los
     90 dias sin ventas; aprobacion / rechazo / cancelacion desde el panel.
  2. REEMBOLSOS: al marcar un pedido como "Reembolsado" sale de la vista principal y queda un
     registro simple (nombre, telefono, fecha del pedido, monto).
  3. PAGOS A VENDEDORES: saldos (generado / pagado / pendiente), pagos en efectivo o
     transferencia, reporte por vendedor y rango de fechas y bloqueo si lo pagado en el mes
     supera los ingresos del mes.

Correo (opcional, variables de entorno en Vercel):
  SMTP_USER, SMTP_PASSWORD   (Gmail: usa una "contrasena de aplicacion")
  SMTP_HOST (smtp.gmail.com), SMTP_PORT (465), MAIL_FROM (= SMTP_USER)
  AFFILIATE_NOTIFY_EMAIL     (por defecto vianedelicatessen@gmail.com)
  PAYPAL_CLIENT_ID (o PAYPAL_CLIENTID), PAYPAL_CLIENT_SECRET   (credenciales REST de PayPal)
  PAYPAL_ENV = live | sandbox (por defecto live), SITE_URL (por defecto https://www.vianedelicatessen.com)
  PAYPAL_DOP_PER_USD         (tasa de respaldo; la tasa normal se cambia en Panel -> PayPal)
Si no estan configuradas, las solicitudes se guardan igual y el panel permite "Reenviar correos".
"""
import base64
import hashlib
import json
import html
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from flask import Response, jsonify, redirect, request
from xml.sax.saxutils import escape as xml_escape

# ---------------------------------------------------------------------------
# Reglas del programa de afiliados
# ---------------------------------------------------------------------------
MIN_AGE = 18
MIN_FOLLOWERS = 500
INACTIVITY_DAYS = 90          # 3 meses sin ventas -> cancelacion automatica
REVIEW_DAYS = 30              # plazo inicial de respuesta (1 a 30 dias)
EXTENDED_DAYS = 365           # si no hay respuesta: evaluacion extendida hasta 12 meses
TERMS_VERSION = '2026-10'
NOTIFY_EMAIL = os.environ.get('AFFILIATE_NOTIFY_EMAIL') or 'vianedelicatessen@gmail.com'
SITE_URL = (os.environ.get('SITE_URL') or 'https://www.vianedelicatessen.com').strip().rstrip('/')
PAID_STATES = ('Pago recibido', 'Pago confirmado', 'Reembolsado')
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

# ---------------------------------------------------------------------------
# Tablas propias (se crean solas la primera vez; no tocan las existentes)
# ---------------------------------------------------------------------------
DDL = [
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
    '''CREATE TABLE IF NOT EXISTS paypal_payments(
        id BIGSERIAL PRIMARY KEY, order_code TEXT NOT NULL, paypal_order_id TEXT UNIQUE NOT NULL,
        usd_amount DOUBLE PRECISION NOT NULL, rate DOUBLE PRECISION NOT NULL,
        status TEXT NOT NULL DEFAULT 'creado', capture_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, paid_at TEXT)''',
    'ALTER TABLE paypal_payments ENABLE ROW LEVEL SECURITY',
    '''CREATE TABLE IF NOT EXISTS order_notices(
        id BIGSERIAL PRIMARY KEY, order_code TEXT NOT NULL, kind TEXT NOT NULL, sent_at TEXT NOT NULL,
        UNIQUE(order_code, kind))''',
    'ALTER TABLE order_notices ENABLE ROW LEVEL SECURITY',
    'CREATE INDEX IF NOT EXISTS paypal_code_idx ON paypal_payments(order_code)',
]

H = {}
_ready = False
_lock = threading.Lock()
_last_expiry = 0.0


def ensure():
    """Crea las tablas de este modulo una vez por instancia (en su propia conexion)."""
    global _ready
    if _ready:
        return
    with _lock:
        if _ready:
            return
        H['ensure_schema']()
        with H['connect']() as con:
            con.execute('SELECT pg_advisory_xact_lock(%s)', (918273650,))
            for stmt in DDL:
                con.execute(stmt)
        _ready = True


@contextmanager
def xdb():
    ensure()
    with H['db']() as c:
        yield c


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def money(x):
    return f'RD$ {float(x or 0):,.2f}'


def num(v, default=None):
    if v is None or str(v).strip() == '':
        return default
    try:
        return float(str(v).replace(',', '.'))
    except ValueError:
        return None


def one_line(s, n=120):
    return re.sub(r'[\r\n\t]+', ' ', str(s or '')).strip()[:n]


def client_ip():
    xff = request.headers.get('X-Forwarded-For', '')
    return (xff.split(',')[0].strip() or request.remote_addr or '')[:60]


def parse_iso(s):
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def today_dr():
    return datetime.now(timezone(timedelta(hours=-4))).date()


def age_on(birth, today):
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


# ---------------------------------------------------------------------------
# Correo (SMTP)
# ---------------------------------------------------------------------------
def smtp_configured():
    return bool(os.environ.get('SMTP_USER') and os.environ.get('SMTP_PASSWORD'))


def mail_error(exc):
    """Texto corto y util (sin datos secretos) para saber por que fallo el correo."""
    n = type(exc).__name__
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'error: usuario o contraseña de aplicación incorrectos (Gmail pide una "contraseña de aplicación")'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'error: la dirección de destino fue rechazada'
    if isinstance(exc, (smtplib.SMTPConnectError, ConnectionError, TimeoutError, OSError)):
        return 'error: no se pudo conectar con el servidor de correo (' + n + ')'
    return 'error: ' + n


def send_mail(to, subject, text, attachments=()):
    """Devuelve (ok, estado). Nunca lanza: un fallo de correo no debe tumbar la solicitud."""
    if not smtp_configured():
        return False, 'sin configurar (faltan SMTP_USER y SMTP_PASSWORD en Vercel)'
    try:
        user, password = os.environ['SMTP_USER'].strip(), os.environ['SMTP_PASSWORD'].replace(' ', '')
        host = (os.environ.get('SMTP_HOST') or 'smtp.gmail.com').strip()
        port = int(os.environ.get('SMTP_PORT') or 465)
        msg = EmailMessage()
        msg['From'] = (os.environ.get('MAIL_FROM') or user).strip()
        msg['To'] = one_line(to, 200)
        msg['Subject'] = one_line(subject, 180)
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
        print(f'[VIANE] correo fallo: {type(exc).__name__}: {str(exc)[:120]}')
        return False, mail_error(exc)


# ---------------------------------------------------------------------------
# Contrato firmado (HTML autonomo, se imprime o se guarda como PDF desde el navegador)
# ---------------------------------------------------------------------------
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
    """Copia del contrato al negocio + gracias al solicitante. Devuelve (estado_negocio, estado_usuario)."""
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
        f"Hemos recibido tu solicitud y tu contrato firmado. Tu afiliación está PENDIENTE DE APROBACIÓN: por "
        f"favor espera nuestra respuesta, todavía no puedes usar un código de afiliado.\n\n"
        f"Estaremos revisando tu perfil y te daremos una respuesta en un plazo de 1 a 30 días. En caso de no "
        f"recibir ninguna notificación dentro de este plazo inicial, el proceso de evaluación se extenderá por "
        f"un período de 30 días a 12 meses mientras validamos la disponibilidad y el cumplimiento de nuestros "
        f"criterios.\n\n"
        f"Sello de tu contrato: {seal}\nAdjuntamos una copia para tus registros.\n\n"
        f"VIANE Delicatessen · WhatsApp 829-675-9640"
    )
    ok_u, st_u = send_mail(a['email'], 'Gracias por tu solicitud de afiliación — VIANE Delicatessen', user_text, atts)
    return st_a, st_u


# ---------------------------------------------------------------------------
# Reembolsos (gancho de pedidos)
# ---------------------------------------------------------------------------
def refund_hook(c, oid, status, payment):
    ensure()
    if status == 'Reembolsado' or payment == 'Reembolsado':
        o = c.execute('SELECT id,name,phone,total,created_at FROM orders WHERE id=%s', (oid,)).fetchone()
        if not o:
            return
        order_date = H['dr_date'](o['created_at'])
        c.execute('INSERT INTO refunds(order_id,name,phone,order_date,amount,created_at) '
                  'VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (order_id) DO NOTHING',
                  (o['id'], o['name'], o['phone'], order_date, float(o['total']), H['now']()))
        c.execute('DELETE FROM sales WHERE order_id=%s', (oid,))  # un reembolso no cuenta como venta
    else:
        c.execute('DELETE FROM refunds WHERE order_id=%s', (oid,))  # se corrigio el estado


# ---------------------------------------------------------------------------
# PayPal: SDK v6 en el navegador + Orders API v2 en el servidor
# (PayPal NO acepta pesos dominicanos: se cobra en US$ con la tasa que pongas en Panel -> PayPal)
# ---------------------------------------------------------------------------
PP_TOKEN = {'v': '', 'exp': 0.0}


def env_first(*names):
    for n in names:
        v = (os.environ.get(n) or '').strip()
        if v:
            return v
    return ''


def pp_conf():
    sandbox = env_first('PAYPAL_ENV').lower() == 'sandbox'
    return {
        'id': env_first('PAYPAL_CLIENT_ID', 'PAYPAL_CLIENTID'),
        'secret': env_first('PAYPAL_CLIENT_SECRET', 'PAYPAL_SECRET'),
        'env': 'sandbox' if sandbox else 'live',
        'api': 'https://api-m.sandbox.paypal.com' if sandbox else 'https://api-m.paypal.com',
        'sdk': ('https://www.sandbox.paypal.com/web-sdk/v6/core' if sandbox
                else 'https://www.paypal.com/web-sdk/v6/core'),
    }


def pp_has_keys():
    cf = pp_conf()
    return bool(cf['id'] and cf['secret'])


def pp_http(method, url, headers, data=None):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            raw, code = r.read().decode('utf-8', 'replace'), r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode('utf-8', 'replace'), e.code
    try:
        return code, json.loads(raw or '{}')
    except ValueError:
        return code, {'raw': raw[:200]}


def pp_token():
    """Token de acceso de PayPal (se reutiliza hasta que caduca). Devuelve (token, error)."""
    if PP_TOKEN['v'] and time.time() < PP_TOKEN['exp']:
        return PP_TOKEN['v'], ''
    cf = pp_conf()
    if not (cf['id'] and cf['secret']):
        return '', 'Faltan PAYPAL_CLIENT_ID y PAYPAL_CLIENT_SECRET en Vercel.'
    auth = base64.b64encode(f"{cf['id']}:{cf['secret']}".encode()).decode()
    try:
        code, res = pp_http('POST', cf['api'] + '/v1/oauth2/token',
                            {'Authorization': 'Basic ' + auth, 'Accept': 'application/json',
                             'Content-Type': 'application/x-www-form-urlencoded'},
                            b'grant_type=client_credentials')
    except Exception as exc:
        return '', 'No se pudo conectar con PayPal (' + type(exc).__name__ + ').'
    if code == 200 and res.get('access_token'):
        PP_TOKEN['v'] = res['access_token']
        PP_TOKEN['exp'] = time.time() + max(60, int(res.get('expires_in', 300)) - 120)
        return PP_TOKEN['v'], ''
    if code in (400, 401):
        return '', ('PayPal rechazó las credenciales: revisa PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET y que '
                    'PAYPAL_ENV (' + cf['env'] + ') sea el modo de esas credenciales.')
    return '', f'PayPal respondió con el error {code}.'


def pp_api(method, path, payload=None):
    """Llamada autenticada a la API de PayPal. Devuelve (codigo_http, json); codigo 0 = no se pudo llamar."""
    tok, err = pp_token()
    if err:
        return 0, {'error': err}
    headers = {'Authorization': 'Bearer ' + tok, 'Accept': 'application/json',
               'Content-Type': 'application/json', 'PayPal-Request-Id': secrets.token_hex(12)}
    data = None if method == 'GET' else json.dumps(payload if payload is not None else {}).encode('utf-8')
    try:
        return pp_http(method, pp_conf()['api'] + path, headers, data)
    except Exception as exc:
        return 0, {'error': 'No se pudo conectar con PayPal (' + type(exc).__name__ + ').'}


def pp_rate(c):
    """Pesos dominicanos por 1 US$: primero la que pones en el panel, luego la variable de respaldo."""
    row = c.execute("SELECT value FROM settings WHERE key='paypal_rate'").fetchone()
    for v in ((row or {}).get('value'), os.environ.get('PAYPAL_DOP_PER_USD')):
        x = num(v)
        if x and x > 0:
            return float(x)
    return 0.0


def pp_finish(token):
    """Captura el pago de PayPal y marca el pedido como pagado. Devuelve (estado, codigo_pedido)."""
    with xdb() as c:
        row = c.execute('SELECT * FROM paypal_payments WHERE paypal_order_id=%s', (token,)).fetchone()
    if not row:
        return 'error', ''
    code = row['order_code']
    if row['status'] == 'paid':
        return 'ok', code
    status, res = pp_api('POST', f'/v2/checkout/orders/{token}/capture', {})
    if status == 422 and any(d.get('issue') == 'ORDER_ALREADY_CAPTURED' for d in (res.get('details') or [])):
        status, res = pp_api('GET', f'/v2/checkout/orders/{token}')
    cap = {}
    try:
        cap = res['purchase_units'][0]['payments']['captures'][0]
    except (KeyError, IndexError, TypeError):
        pass
    amt = cap.get('amount') or {}
    try:
        same_amount = abs(float(amt.get('value') or 0) - float(row['usd_amount'])) < 0.011
    except ValueError:
        same_amount = False
    paid = (res.get('status') == 'COMPLETED' and cap.get('status') == 'COMPLETED'
            and amt.get('currency_code') == 'USD' and same_amount)
    state = 'ok' if paid else ('pending' if cap.get('status') == 'PENDING' else 'error')
    with xdb() as c:
        if paid:
            c.execute("UPDATE paypal_payments SET status='paid', capture_id=%s, paid_at=%s WHERE paypal_order_id=%s",
                      (str(cap.get('id') or '')[:60], H['now'](), token))
            c.execute("UPDATE orders SET payment_status='Pago recibido', "
                      "status=CASE WHEN status='Pago pendiente' THEN 'Pago recibido' ELSE status END, "
                      "updated_at=%s WHERE code=%s", (H['now'](), code))
        else:
            c.execute('UPDATE paypal_payments SET status=%s WHERE paypal_order_id=%s', (state, token))
    if not paid:
        print(f'[VIANE] PayPal no confirmo el pago ({state}): http {status} {str(res)[:200]}')
    return state, code


# ---------------------------------------------------------------------------
# Textos de la tienda (borradores editables desde Panel -> Tienda; revisalos con un abogado)
# ---------------------------------------------------------------------------
LEGAL_DEFAULT = {
    'terms': """TÉRMINOS Y CONDICIONES — VIANE Delicatessen

1. Quiénes somos. VIANE Delicatessen elabora y vende bandejas decorativas, desayunos, picaderas y regalos personalizados en República Dominicana. Contacto: WhatsApp 829-675-9640.

2. Pedidos. Todos los pedidos deben realizarse con un mínimo de 24 horas de anticipación. Un pedido queda confirmado cuando recibimos el pago o lo acordamos contigo por WhatsApp.

3. Precios. Los precios se muestran en pesos dominicanos (RD$). El envío se suma según la zona elegida. Podemos corregir errores evidentes de precio antes de confirmar tu pedido.

4. Formas de pago. Aceptamos PayPal (el cobro se hace en dólares al cambio indicado al momento de pagar), transferencia bancaria y efectivo. El pedido se prepara cuando el pago está confirmado.

5. Entrega. La fecha y la zona las eliges al hacer el pedido. Debe haber una persona disponible para recibirlo; si no se puede entregar por causas del cliente, podemos cobrar un nuevo envío.

6. Productos personalizados y perecederos. Por su naturaleza no admiten cambio ni devolución, salvo error nuestro o daño ocurrido durante la entrega (ver «Envíos y devoluciones»).

7. Imágenes. Las fotos son referenciales. El diseño final puede variar levemente según la disponibilidad de insumos, manteniendo la calidad y el valor del pedido.

8. Códigos de descuento y afiliados. Los códigos son personales, no acumulables y pueden desactivarse en cualquier momento. El programa de afiliados se rige por su contrato firmado.

9. Cambios. Podemos actualizar estos términos; aplican los vigentes al momento de tu pedido.

10. Ley aplicable. Estos términos se rigen por las leyes de la República Dominicana.""",
    'privacy': """POLÍTICA DE PRIVACIDAD — VIANE Delicatessen

1. Datos que recopilamos. Nombre, teléfono o WhatsApp, dirección de entrega, correo electrónico (opcional) y los detalles de tu pedido. Si solicitas ser afiliado, también los datos de tu solicitud y tu firma.

2. Para qué los usamos. Preparar y entregar tu pedido, contactarte sobre él, confirmar pagos, atender reclamos y, si dejaste tu correo, pedirte una reseña cuando tu pedido sea entregado.

3. Con quién los compartimos. Solo con quienes hacen posible el servicio: la plataforma de pago (PayPal, si eliges ese método), el servicio de correo y los proveedores técnicos del sitio. No vendemos tus datos.

4. Cookies y publicidad. El sitio puede mostrar anuncios de Google y usar herramientas de analítica que emplean cookies. Puedes administrar tu consentimiento en el aviso que aparece en el sitio y en la configuración de tu navegador.

5. Conservación. Guardamos la información mientras sea necesaria para atender tu pedido y cumplir obligaciones legales o contables.

6. Tus derechos. Puedes pedirnos acceder a tus datos, corregirlos o eliminarlos escribiéndonos por WhatsApp al 829-675-9640.

7. Seguridad. Usamos conexiones cifradas y acceso restringido a la información. Ningún sistema es 100% infalible, pero trabajamos para protegerla.""",
    'returns': """ENVÍOS Y DEVOLUCIONES — VIANE Delicatessen

ENVÍOS
• Puedes recoger tu pedido en el local o recibirlo en Puerto Plata (ciudad), Santiago o Santo Domingo. El costo de cada zona se muestra en la página y al crear tu pedido.
• Entregamos en la fecha que eliges. Los pedidos requieren un mínimo de 24 horas de anticipación.
• Si te damos un envío gratis desde cierto monto, se aplica automáticamente al llegar a ese monto.

REVISA TU PEDIDO AL RECIBIRLO
Por tratarse de productos frescos y personalizados, revisa tu pedido en el momento de la entrega.

DEVOLUCIONES Y SOLUCIONES
• Si hay un error nuestro, falta algún producto o llega dañado, escríbenos por WhatsApp (829-675-9640) el mismo día con fotos y tu código de pedido.
• Lo resolvemos con una reposición o con el reembolso de lo afectado, según el caso.
• No aceptamos devoluciones de productos perecederos o personalizados por cambio de opinión.

REEMBOLSOS
Los reembolsos se hacen por el mismo medio de pago: a tu cuenta de PayPal o por transferencia, normalmente en 3 a 5 días hábiles.""",
    'size_guide': """GUÍA DE TAMAÑOS

¿Para cuántas personas es tu pedido? Cuéntanos cuántas personas serán y la ocasión (cumpleaños, boda, aniversario, sorpresa...) y te recomendamos el tamaño ideal.

• Detalle individual o para 1 a 2 personas.
• Bandejas para compartir en familia.
• Bandejas para eventos: cumpleaños de 25 personas en adelante.

Si tienes dudas, escríbenos por WhatsApp al 829-675-9640 y armamos la opción perfecta para ti.""",
}
LEGAL_KEYS = {'terms': 'legal_terms', 'privacy': 'legal_privacy', 'returns': 'legal_returns', 'size_guide': 'size_guide'}


def effective_legal(c):
    """Texto vigente: el que escribas en el panel o, si lo dejas vacio, el borrador de arriba."""
    rows_ = c.execute('SELECT key,value FROM settings WHERE key = ANY(%s)', (list(LEGAL_KEYS.values()),)).fetchall()
    cur = {r['key']: r['value'] for r in rows_}
    return {k: (cur.get(v) or '').strip() or LEGAL_DEFAULT[k] for k, v in LEGAL_KEYS.items()}


def review_hook(c, oid, status, payment):
    """Cuando un pedido pasa a Entregado/Terminado y el cliente dejo su correo, se le pide una resena (una sola vez)."""
    if status not in ('Entregado', 'Terminado') or payment == 'Reembolsado' or not smtp_configured():
        return
    ensure()
    o = c.execute('SELECT code,name,customer_email FROM orders WHERE id=%s', (oid,)).fetchone()
    if not o or not o.get('customer_email'):
        return
    cur = c.execute('INSERT INTO order_notices(order_code,kind,sent_at) VALUES(%s,%s,%s) '
                    'ON CONFLICT (order_code,kind) DO NOTHING', (o['code'], 'review', H['now']()))
    if cur.rowcount != 1:  # ya se le pidio antes
        return
    row = c.execute("SELECT value FROM settings WHERE key='review_url'").fetchone()
    link = row['value'] if row and str(row['value']).lower().startswith('http') else SITE_URL + '/#reviews'
    first = (o['name'] or '').split(' ')[0] or 'hola'
    ok, _ = send_mail(o['customer_email'], '¿Cómo te fue con tu pedido? — VIANE Delicatessen',
                      f"Hola {first},\n\n¡Gracias por tu compra! Esperamos que hayas disfrutado tu pedido {o['code']}.\n\n"
                      f"¿Nos cuentas cómo te fue? Tu opinión ayuda a otras personas y nos ayuda a mejorar:\n{link}\n\n"
                      f"Si algo no salió como esperabas, responde este correo o escríbenos por WhatsApp al 829-675-9640.\n\n"
                      f"VIANE Delicatessen")
    if not ok:  # si no salio, que se pueda intentar de nuevo
        c.execute('DELETE FROM order_notices WHERE order_code=%s AND kind=%s', (o['code'], 'review'))


# ---------------------------------------------------------------------------
# Registro de rutas
# ---------------------------------------------------------------------------
def register(app, **h):
    H.update(h)
    admin_required, body, rows, now = h['admin_required'], h['body'], h['rows'], h['now']
    parse_pct, norm = h['parse_pct'], h['norm']
    DEF_DISC, DEF_COMM = h['reseller_defaults']
    h['order_hooks'].append(refund_hook)
    h['order_hooks'].append(review_hook)
    mig = h.get('migrations')
    if mig is not None:  # las tablas de este modulo se crean junto con las migraciones del sitio
        for stmt in DDL:
            if stmt not in mig:
                mig.append(stmt)

    @app.post('/api/affiliates/test-mail')
    @admin_required
    def affiliate_test_mail():
        """Envia un correo de prueba al correo del negocio y dice exactamente que falla si no llega."""
        ok, st = send_mail(NOTIFY_EMAIL, 'Prueba de correo — VIANE Delicatessen',
                           'Si lees esto, el correo del sistema de afiliados está bien configurado.')
        return jsonify({'ok': ok, 'status': st, 'to': NOTIFY_EMAIL})

    # ---------- inactividad: cancelacion automatica a los 90 dias sin ventas ----------
    def cancel_affiliate(c, aid, rid, reason):
        c.execute("UPDATE affiliates SET status='cancelado', rights_revoked=1, cancelled_at=%s, cancel_reason=%s "
                  "WHERE id=%s", (now(), reason, aid))
        if rid:
            c.execute('UPDATE resellers SET active=0 WHERE id=%s', (rid,))

    def expire_inactive(c):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=INACTIVITY_DAYS)).isoformat()
        n = 0
        for a in c.execute("SELECT id,reseller_id,decided_at FROM affiliates WHERE status='aprobado'").fetchall():
            last = None
            if a['reseller_id']:
                last = c.execute(
                    "SELECT MAX(created_at) AS m FROM orders WHERE reseller_id=%s "
                    "AND status <> 'Reembolsado' AND payment_status <> 'Reembolsado'",
                    (a['reseller_id'],)).fetchone()['m']
            stamps = [x for x in (a['decided_at'], last) if x]
            if stamps and max(stamps) < cutoff:
                cancel_affiliate(c, a['id'], a['reseller_id'],
                                 f'Inactividad: {INACTIVITY_DAYS} días consecutivos sin ventas (cancelación automática)')
                n += 1
        return n

    @app.before_request
    def _affiliate_expiry():
        """Antes de validar un codigo o crear un pedido se revisa (como mucho cada 5 min) si algun
        afiliado lleva 90 dias sin ventas, para que su codigo ya no funcione."""
        global _last_expiry
        if request.method == 'POST' and request.path in ('/api/resellers/validate', '/api/orders'):
            if time.time() - _last_expiry > 300:
                _last_expiry = time.time()
                try:
                    with xdb() as c:
                        expire_inactive(c)
                except Exception as exc:
                    print(f'[VIANE] revision de inactividad fallo: {exc!r}')

    # ---------- AFILIADOS: publico ----------
    @app.get('/api/affiliates/terms')
    def affiliate_terms():
        return jsonify({'version': TERMS_VERSION, 'text': TERMS_TEXT, 'min_age': MIN_AGE,
                        'min_followers': MIN_FOLLOWERS, 'platforms': PLATFORMS, 'message': RESPONSE_MESSAGE})

    @app.post('/api/affiliates')
    def affiliate_submit():
        d = body()
        if str(d.get('website') or '').strip():  # campo trampa para robots: se ignora en silencio
            return jsonify({'ok': True, 'message': RESPONSE_MESSAGE})
        name = re.sub(r'\s+', ' ', one_line(d.get('full_name'), 100))
        email = one_line(d.get('email'), 120).lower()
        phone = one_line(d.get('phone'), 40)
        platform = one_line(d.get('platform'), 40)
        profile = one_line(d.get('profile'), 200)
        if len(name) < 5 or ' ' not in name:
            return jsonify({'error': 'Escribe tu nombre completo.'}), 400
        if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email):
            return jsonify({'error': 'Escribe un correo válido.'}), 400
        if len(re.sub(r'\D', '', phone)) < 7:
            return jsonify({'error': 'Escribe tu teléfono o WhatsApp.'}), 400
        if platform not in PLATFORMS or not profile:
            return jsonify({'error': 'Indica tu red social principal y el enlace o @usuario de tu perfil.'}), 400
        # --- Requisito 1: mayor de edad ---
        try:
            birth = datetime.strptime(str(d.get('birth_date') or ''), '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'error': 'Indica tu fecha de nacimiento.'}), 400
        today = today_dr()
        if birth > today or birth.year < today.year - 120:
            return jsonify({'error': 'La fecha de nacimiento no es válida.'}), 400
        if age_on(birth, today) < MIN_AGE:
            return jsonify({'error': f'Debes ser mayor de edad ({MIN_AGE} años o más) para ser afiliado.'}), 400
        # --- Requisito 2: minimo 500 seguidores ---
        try:
            followers = int(re.sub(r'[.,\s]', '', str(d.get('followers') or '')))
        except ValueError:
            return jsonify({'error': 'Indica cuántos seguidores tienes (solo números).'}), 400
        if followers < MIN_FOLLOWERS:
            return jsonify({'error': f'Necesitas un mínimo de {MIN_FOLLOWERS} seguidores para solicitar la afiliación.'}), 400
        if followers > 5_000_000_000:
            return jsonify({'error': 'Revisa la cantidad de seguidores.'}), 400
        # --- Terminos y firma ---
        if d.get('accepted') is not True:
            return jsonify({'error': 'Debes aceptar los términos y condiciones para continuar.'}), 400
        sig_name = one_line(d.get('signature_name'), 100)
        if re.sub(r'\s+', ' ', norm(sig_name)) != re.sub(r'\s+', ' ', norm(name)):  # ignora tildes, mayúsculas y espacios dobles
            return jsonify({'error': 'Escribe tu nombre completo, igual que en la solicitud, como firma.'}), 400
        sig_png = str(d.get('signature_png') or '')
        raw_png = png_bytes(sig_png)
        if len(sig_png) > 250_000 or len(sig_png) < 1200 or not raw_png.startswith(b'\x89PNG'):
            return jsonify({'error': 'Dibuja tu firma en el recuadro.'}), 400

        signed_at = now()
        a = {'terms_version': TERMS_VERSION, 'terms_text': TERMS_TEXT, 'full_name': name, 'email': email,
             'birth_date': birth.isoformat(), 'followers': followers, 'platform': platform, 'profile': profile,
             'signature_name': sig_name, 'signed_at': signed_at, 'signed_ip': client_ip(), 'phone': phone,
             'signature_png': sig_png}
        a['contract_hash'] = seal_hash(a, hashlib.sha256(raw_png).hexdigest())
        with xdb() as c:
            if c.execute("SELECT 1 FROM affiliates WHERE LOWER(email)=%s AND status IN ('pendiente','aprobado')",
                         (email,)).fetchone():
                return jsonify({'error': 'Ya existe una solicitud activa con este correo.'}), 400
            row = c.execute(
                'INSERT INTO affiliates(full_name,email,phone,birth_date,platform,profile,followers,created_at,'
                'terms_version,terms_text,signature_name,signature_png,contract_hash,signed_at,signed_ip) '
                'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                (name, email, phone, a['birth_date'], platform, profile, followers, signed_at, TERMS_VERSION,
                 TERMS_TEXT, sig_name, sig_png, a['contract_hash'], signed_at, a['signed_ip'])).fetchone()
        a['id'] = row['id']
        st_admin, st_user = send_application_mails(a)  # fuera de la transaccion: un fallo no pierde la solicitud
        try:
            with xdb() as c:
                c.execute('UPDATE affiliates SET mail_admin=%s, mail_user=%s WHERE id=%s',
                          (st_admin, st_user, a['id']))
        except Exception as exc:
            print(f'[VIANE] no se pudo guardar el estado del correo: {exc!r}')
        return jsonify({'ok': True, 'message': RESPONSE_MESSAGE, 'seal': a['contract_hash'][:12].upper(),
                        'mail_user': st_user == 'enviado'})

    # ---------- AFILIADOS: panel ----------
    def phase_info(a):
        created = parse_iso(a['created_at'])
        if a['status'] != 'pendiente' or not created:
            return '', ''
        n = datetime.now(timezone.utc)
        d30, d365 = created + timedelta(days=REVIEW_DAYS), created + timedelta(days=EXTENDED_DAYS)
        if n <= d30:
            return 'inicial', d30.date().isoformat()
        if n <= d365:
            return 'extendida', d365.date().isoformat()
        return 'vencida', d365.date().isoformat()

    @app.get('/api/affiliates')
    @admin_required
    def affiliates_list():
        with xdb() as c:
            expire_inactive(c)
            data = rows(c, 'SELECT a.id,a.full_name,a.email,a.phone,a.birth_date,a.platform,a.profile,a.followers,'
                           'a.status,a.created_at,a.decided_at,a.cancelled_at,a.cancel_reason,a.decision_note,'
                           'a.rights_revoked,a.reseller_id,a.contract_hash,a.mail_admin,a.mail_user,'
                           'r.code AS code,r.discount_pct,r.commission_pct FROM affiliates a '
                           'LEFT JOIN resellers r ON r.id=a.reseller_id ORDER BY a.id DESC LIMIT 300')
        today = today_dr()
        for a in data:
            ph, dl = phase_info(a)
            a['phase'], a['deadline'] = ph, dl
            try:
                a['age'] = age_on(datetime.strptime(a['birth_date'], '%Y-%m-%d').date(), today)
            except ValueError:
                a['age'] = None
            a['contract_hash'] = (a['contract_hash'] or '')[:12].upper()
        return jsonify({'affiliates': data, 'mail_configured': smtp_configured(), 'notify_email': NOTIFY_EMAIL})

    def load_affiliate(c, aid):
        return c.execute('SELECT * FROM affiliates WHERE id=%s', (aid,)).fetchone()

    @app.post('/api/affiliates/<int:aid>/approve')
    @admin_required
    def affiliate_approve(aid):
        d = body()
        try:
            disc = parse_pct(d.get('discount_pct'), DEF_DISC)
            comm = parse_pct(d.get('commission_pct'), DEF_COMM)
        except ValueError:
            return jsonify({'error': 'Los porcentajes deben estar entre 0 y 100'}), 400
        with xdb() as c:
            a = load_affiliate(c, aid)
            if not a:
                return jsonify({'error': 'Solicitud no encontrada'}), 404
            if a['status'] != 'pendiente':
                return jsonify({'error': 'Solo se pueden aprobar solicitudes pendientes'}), 400
            while True:
                code = 'AFI-' + secrets.token_hex(3).upper()
                if not c.execute('SELECT 1 FROM resellers WHERE code=%s', (code,)).fetchone():
                    break
            r = c.execute('INSERT INTO resellers(name,code,active,created_at,discount_pct,commission_pct) '
                          'VALUES(%s,%s,1,%s,%s,%s) RETURNING id', (a['full_name'], code, now(), disc, comm)).fetchone()
            c.execute("UPDATE affiliates SET status='aprobado', decided_at=%s, reseller_id=%s WHERE id=%s",
                      (now(), r['id'], aid))
        ok, st = send_mail(
            a['email'], '¡Tu afiliación a VIANE Delicatessen fue aprobada!',
            f"Hola {a['full_name']},\n\n¡Felicidades! Tu solicitud de afiliación fue aprobada.\n\n"
            f"Tu código de afiliado: {code}\nTus clientes reciben {disc:g}% de descuento al usarlo y tú ganas "
            f"{comm:g}% de comisión sobre el valor de cada venta.\n\nRecuerda: si pasan {INACTIVITY_DAYS} días "
            f"consecutivos sin ventas, la afiliación se cancela automáticamente (ver contrato firmado).\n\n"
            f"VIANE Delicatessen · WhatsApp 829-675-9640")
        return jsonify({'ok': True, 'code': code, 'mail': st})

    @app.post('/api/affiliates/<int:aid>/reject')
    @admin_required
    def affiliate_reject(aid):
        note = one_line(body().get('note'), 300)
        with xdb() as c:
            a = load_affiliate(c, aid)
            if not a:
                return jsonify({'error': 'Solicitud no encontrada'}), 404
            if a['status'] != 'pendiente':
                return jsonify({'error': 'Solo se pueden rechazar solicitudes pendientes'}), 400
            c.execute("UPDATE affiliates SET status='rechazado', decided_at=%s, decision_note=%s WHERE id=%s",
                      (now(), note, aid))
        ok, st = send_mail(
            a['email'], 'Sobre tu solicitud de afiliación — VIANE Delicatessen',
            f"Hola {a['full_name']},\n\nGracias por tu interés en el programa de afiliados. En esta ocasión no "
            f"podremos continuar con tu solicitud.{(' ' + note) if note else ''}\n\nVIANE Delicatessen")
        return jsonify({'ok': True, 'mail': st})

    @app.post('/api/affiliates/<int:aid>/cancel')
    @admin_required
    def affiliate_cancel(aid):
        reason = one_line(body().get('reason'), 300)
        if not reason:
            return jsonify({'error': 'Escribe el motivo de la cancelación (infracción o motivo legal/operativo).'}), 400
        with xdb() as c:
            a = load_affiliate(c, aid)
            if not a:
                return jsonify({'error': 'Afiliado no encontrado'}), 404
            if a['status'] not in ('aprobado', 'pendiente'):
                return jsonify({'error': 'Esta afiliación ya está cerrada'}), 400
            cancel_affiliate(c, aid, a['reseller_id'], reason)
        return jsonify({'ok': True})

    @app.post('/api/affiliates/<int:aid>/resend')
    @admin_required
    def affiliate_resend(aid):
        with xdb() as c:
            a = load_affiliate(c, aid)
        if not a:
            return jsonify({'error': 'Solicitud no encontrada'}), 404
        st_admin, st_user = send_application_mails(a)
        with xdb() as c:
            c.execute('UPDATE affiliates SET mail_admin=%s, mail_user=%s WHERE id=%s', (st_admin, st_user, aid))
        return jsonify({'ok': st_admin == 'enviado' and st_user == 'enviado', 'mail_admin': st_admin,
                        'mail_user': st_user})

    @app.get('/api/affiliates/<int:aid>/contract')
    @admin_required
    def affiliate_contract(aid):
        with xdb() as c:
            a = load_affiliate(c, aid)
        if not a:
            return jsonify({'error': 'Solicitud no encontrada'}), 404
        return Response(contract_html(a), mimetype='text/html')

    # ---------- PAYPAL ----------
    @app.get('/api/paypal/config')
    def paypal_config():
        """Datos publicos para el boton «Pagar aquí» (el client id de PayPal es publico por diseño)."""
        cf = pp_conf()
        out = {'enabled': False, 'client_id': '', 'sdk_url': cf['sdk'], 'currency': 'USD', 'rate': 0}
        if pp_has_keys():
            try:
                with xdb() as c:
                    out['rate'] = pp_rate(c)
            except Exception as exc:
                print(f'[VIANE] PayPal config: {exc!r}')
            out['enabled'] = out['rate'] > 0
            out['client_id'] = cf['id'] if out['enabled'] else ''
        return jsonify(out)

    @app.post('/api/paypal/create')
    def paypal_create():
        """Crea la orden de PayPal con el total REAL del pedido guardado en la base de datos."""
        code = str(body().get('code') or '').strip().upper()
        if not pp_has_keys():
            return jsonify({'error': 'El pago con PayPal todavía no está disponible.'}), 503
        with xdb() as c:
            o = c.execute('SELECT code,total,payment_status FROM orders WHERE code=%s', (code,)).fetchone()
            if not o:
                return jsonify({'error': 'No encontramos ese pedido.'}), 404
            if o['payment_status'] in PAID_STATES:
                return jsonify({'error': 'Este pedido ya está pagado.'}), 400
            rate = pp_rate(c)
        if rate <= 0:
            return jsonify({'error': 'PayPal aún no tiene la tasa del dólar configurada.'}), 503
        usd = round(float(o['total']) / rate, 2)
        if usd < 0.01:
            return jsonify({'error': 'El monto del pedido es demasiado pequeño para PayPal.'}), 400
        status, res = pp_api('POST', '/v2/checkout/orders', {
            'intent': 'CAPTURE',
            'purchase_units': [{
                'reference_id': code, 'custom_id': code,
                'description': f'Pedido {code} - VIANE Delicatessen',
                'amount': {'currency_code': 'USD', 'value': f'{usd:.2f}'},
            }],
            'payment_source': {'paypal': {'experience_context': {
                'brand_name': 'VIANE Delicatessen', 'locale': 'es-ES', 'user_action': 'PAY_NOW',
                'shipping_preference': 'NO_SHIPPING',
                'return_url': SITE_URL + '/api/paypal/return',
                'cancel_url': f'{SITE_URL}/?paypal=cancel&code={code}',
            }}},
        })
        order_id = res.get('id') if status in (200, 201) else ''
        if not order_id:
            print(f'[VIANE] PayPal no creo la orden: http {status} {str(res)[:300]}')
            return jsonify({'error': res.get('error') or 'PayPal no pudo crear el pago. Intenta de nuevo.'}), 502
        link = next((l.get('href') for l in (res.get('links') or []) if l.get('rel') in ('payer-action', 'approve')), '')
        with xdb() as c:
            c.execute('INSERT INTO paypal_payments(order_code,paypal_order_id,usd_amount,rate,status,created_at) '
                      'VALUES(%s,%s,%s,%s,%s,%s)', (code, order_id, usd, rate, 'creado', now()))
        return jsonify({'ok': True, 'orderId': order_id, 'approve_url': link, 'usd': usd})

    @app.post('/api/paypal/capture')
    def paypal_capture():
        """El navegador avisa que el cliente aprobó el pago; el servidor lo captura y lo verifica."""
        token = re.sub(r'[^A-Za-z0-9_-]', '', str(body().get('orderId') or ''))[:64]
        if not token:
            return jsonify({'error': 'Pago inválido.'}), 400
        state, code = pp_finish(token)
        return jsonify({'ok': state in ('ok', 'pending'), 'state': state, 'code': code})

    @app.get('/api/paypal/return')
    def paypal_return():
        """Respaldo: si el cliente paga por redireccion (sin ventana emergente) vuelve aqui."""
        token = re.sub(r'[^A-Za-z0-9_-]', '', request.args.get('token', ''))[:64]
        state, code = pp_finish(token) if token else ('error', '')
        return redirect(f'{SITE_URL}/?paypal={state}&code={code}#track')

    @app.get('/api/paypal/test')
    @admin_required
    def paypal_test():
        """Diagnostico para el panel: credenciales, modo, conexion real con PayPal y tasa."""
        cf = pp_conf()
        with xdb() as c:
            rate = pp_rate(c)
        out = {'configured': pp_has_keys(), 'env': cf['env'], 'rate': rate, 'site_url': SITE_URL,
               'connection': None, 'message': ''}
        if out['configured']:
            PP_TOKEN['exp'] = 0.0  # fuerza una prueba real
            tok, err = pp_token()
            out['connection'], out['message'] = bool(tok), err
        return jsonify(out)

    # ---------- TIENDA: textos legales, analitica, Google Shopping y SEO ----------
    @app.get('/api/store/legal')
    def store_legal():
        with xdb() as c:
            return jsonify(effective_legal(c))

    @app.get('/api/analytics')
    @admin_required
    def analytics():
        """Ventas por mes (12 meses), productos mas vendidos y clientes recurrentes (solo ventas activas)."""
        with xdb() as c:
            data = rows(c, 'SELECT s.amount,s.sold_date,s.contact,o.items FROM sales s '
                           'LEFT JOIN orders o ON o.id=s.order_id WHERE s.deleted_at IS NULL '
                           'ORDER BY s.sold_date DESC LIMIT 5000')
        today = today_dr()
        keys, y, m = [], today.year, today.month
        for _ in range(12):
            keys.append(f'{y:04d}-{m:02d}')
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        months = {k: {'month': k, 'amount': 0.0, 'count': 0} for k in reversed(keys)}
        prods, phones, total = {}, {}, 0.0
        for r in data:
            amt = float(r['amount'] or 0)
            total += amt
            mk = (r['sold_date'] or '')[:7]
            if mk in months:
                months[mk]['amount'] += amt
                months[mk]['count'] += 1
            ph = re.sub(r'\D', '', r['contact'] or '')[-10:]
            if ph:
                phones[ph] = phones.get(ph, 0) + 1
            try:
                items = json.loads(r['items'] or '[]')
            except ValueError:
                items = []
            for it in (items if isinstance(items, list) else []):
                if isinstance(it, dict) and it.get('name'):
                    p = prods.setdefault(str(it['name']), {'name': str(it['name']), 'qty': 0, 'revenue': 0.0})
                    p['qty'] += 1
                    p['revenue'] += float(it.get('price') or 0)
        top = sorted(prods.values(), key=lambda p: (-p['qty'], -p['revenue']))[:10]
        for p in top:
            p['revenue'] = round(p['revenue'], 2)
        n = len(data)
        return jsonify({'months': [{**v, 'amount': round(v['amount'], 2)} for v in months.values()], 'top': top,
                        'sales': n, 'revenue': round(total, 2), 'average': round(total / n, 2) if n else 0.0,
                        'customers': len(phones), 'repeat': sum(1 for v in phones.values() if v > 1)})

    @app.get('/feed/google.xml')
    def google_feed():
        """Catalogo para Google Merchant Center (Google Shopping / fichas gratuitas)."""
        with xdb() as c:
            items = rows(c, 'SELECT p.id,p.name,p.description,p.price,p.image,p.category FROM products p '
                            'WHERE p.active=1 AND NOT EXISTS (SELECT 1 FROM categories k WHERE k.name=p.category '
                            'AND k.active=0) ORDER BY p.id')
        e = xml_escape
        out = ['<?xml version="1.0" encoding="UTF-8"?>',
               '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0"><channel>',
               '<title>Viane Delicatessen</title>', f'<link>{SITE_URL}/</link>',
               '<description>Bandejas decorativas, desayunos, picaderas y regalos</description>']
        for p in items:
            img = p['image'] or ''
            img = SITE_URL + img if img.startswith('/') else img
            if not img.lower().startswith('https://') or float(p['price'] or 0) <= 0:
                continue  # Google exige imagen y precio
            out.append(
                '<item>'
                f'<g:id>VIA-{p["id"]}</g:id><title>{e(str(p["name"])[:150])}</title>'
                f'<description>{e(str(p["description"] or p["name"])[:4900])}</description>'
                f'<link>{SITE_URL}/?p={p["id"]}</link><g:image_link>{e(img)}</g:image_link>'
                f'<g:availability>in stock</g:availability><g:price>{float(p["price"]):.2f} DOP</g:price>'
                '<g:condition>new</g:condition><g:brand>Viane Delicatessen</g:brand>'
                f'<g:identifier_exists>no</g:identifier_exists><g:product_type>{e(str(p["category"]))}</g:product_type>'
                '</item>')
        out.append('</channel></rss>')
        resp = Response('\n'.join(out), mimetype='application/xml')
        resp.headers['Cache-Control'] = 'public, max-age=3600'
        return resp

    @app.get('/robots.txt')
    def robots_txt():
        return Response(f'User-agent: *\nAllow: /\nDisallow: /api/\n\nSitemap: {SITE_URL}/sitemap.xml\n', mimetype='text/plain')

    @app.get('/sitemap.xml')
    def sitemap_xml():
        today = today_dr().isoformat()
        return Response('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        f'<url><loc>{SITE_URL}/</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq></url></urlset>',
                        mimetype='application/xml')

    # ---------- REEMBOLSOS ----------
    @app.get('/api/refunds')
    @admin_required
    def refunds_list():
        with xdb() as c:
            data = rows(c, 'SELECT order_id,name,phone,order_date,amount FROM refunds '
                           'ORDER BY id DESC LIMIT 500')
        return jsonify({'refunds': data})

    @app.post('/api/refunds/<int:oid>/undo')
    @admin_required
    def refund_undo(oid):
        """Deshace un reembolso marcado por error: el pedido vuelve a la lista de Pedidos."""
        with xdb() as c:
            if not c.execute('SELECT 1 FROM refunds WHERE order_id=%s', (oid,)).fetchone():
                return jsonify({'error': 'Reembolso no encontrado'}), 404
            c.execute('DELETE FROM refunds WHERE order_id=%s', (oid,))
            c.execute("UPDATE orders SET status='Pago pendiente', payment_status='Pago pendiente', updated_at=%s "
                      "WHERE id=%s", (now(), oid))
        return jsonify({'ok': True})

    # ---------- PAGOS A VENDEDORES ----------
    def earned_for(c, rid):
        return float(c.execute(
            'SELECT COALESCE(SUM(o.commission),0) AS v FROM orders o JOIN sales s ON s.order_id=o.id '
            'WHERE o.reseller_id=%s AND s.deleted_at IS NULL', (rid,)).fetchone()['v'])

    def paid_for(c, rid):
        return float(c.execute('SELECT COALESCE(SUM(amount),0) AS v FROM reseller_payouts WHERE reseller_id=%s',
                               (rid,)).fetchone()['v'])

    def month_figures(c, month):
        revenue = float(c.execute("SELECT COALESCE(SUM(amount),0) AS v FROM sales "
                                  "WHERE deleted_at IS NULL AND LEFT(sold_date,7)=%s", (month,)).fetchone()['v'])
        paid = float(c.execute('SELECT COALESCE(SUM(amount),0) AS v FROM reseller_payouts '
                               'WHERE LEFT(paid_on,7)=%s', (month,)).fetchone()['v'])
        return revenue, paid

    @app.get('/api/payouts/balance/<int:rid>')
    @admin_required
    def payout_balance(rid):
        with xdb() as c:
            r = c.execute('SELECT id,name FROM resellers WHERE id=%s', (rid,)).fetchone()
            if not r:
                return jsonify({'error': 'Vendedor no encontrado'}), 404
            earned, paid = earned_for(c, rid), paid_for(c, rid)
            month = today_dr().isoformat()[:7]
            revenue, month_paid = month_figures(c, month)
        return jsonify({'name': r['name'], 'earned': round(earned, 2), 'paid': round(paid, 2),
                        'pending': round(max(earned - paid, 0), 2), 'month': month,
                        'month_revenue': round(revenue, 2), 'month_paid': round(month_paid, 2)})

    @app.post('/api/payouts')
    @admin_required
    def payout_create():
        d = body()
        try:
            rid = int(d.get('reseller_id'))
        except (TypeError, ValueError):
            return jsonify({'error': 'Vendedor inválido'}), 400
        amount = num(d.get('amount'))
        if amount is None or amount <= 0:
            return jsonify({'error': 'Escribe un monto a pagar mayor que 0.'}), 400
        amount = round(amount, 2)
        method = str(d.get('method') or '')
        if method not in ('efectivo', 'transferencia'):
            return jsonify({'error': 'Elige si el pago es en efectivo o por transferencia.'}), 400
        account = one_line(d.get('account'), 40)
        holder = one_line(d.get('holder'), 100)
        cedula = one_line(d.get('cedula'), 30)
        if method == 'transferencia' and (len(account) < 4 or len(holder) < 3 or len(re.sub(r'\W', '', cedula)) < 6):
            return jsonify({'error': 'Para una transferencia completa: número de cuenta, nombre del titular y cédula.'}), 400
        paid_on = str(d.get('paid_on') or '').strip() or today_dr().isoformat()
        try:
            date.fromisoformat(paid_on)
        except ValueError:
            return jsonify({'error': 'Fecha de pago inválida.'}), 400
        with xdb() as c:
            r = c.execute('SELECT id,name FROM resellers WHERE id=%s', (rid,)).fetchone()
            if not r:
                return jsonify({'error': 'Vendedor no encontrado'}), 404
            earned, paid = earned_for(c, rid), paid_for(c, rid)
            pending = round(earned - paid, 2)
            if amount > pending + 0.005:
                return jsonify({'error': f'El monto ({money(amount)}) supera lo pendiente de {r["name"]} '
                                         f'({money(max(pending, 0))}).'}), 400
            # Seguridad: lo pagado en el mes no puede superar los ingresos recaudados del mes.
            revenue, month_paid = month_figures(c, paid_on[:7])
            if month_paid + amount > revenue + 0.005:
                return jsonify({'error': f'Pago bloqueado: los pagos del mes {paid_on[:7]} '
                                         f'({money(month_paid + amount)}) superarían los ingresos recaudados '
                                         f'del mes ({money(revenue)}).'}), 400
            c.execute('INSERT INTO reseller_payouts(reseller_id,reseller_name,amount,method,account,holder,cedula,'
                      'paid_on,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (rid, r['name'], amount, method, account, holder, cedula, paid_on, now()))
        return jsonify({'ok': True, 'pending_after': round(max(pending - amount, 0), 2)})

    @app.get('/api/payouts/report')
    @admin_required
    def payout_report():
        """Pagado y pendiente por vendedor, filtrable por vendedor y por rango de fechas."""
        rid = request.args.get('reseller_id', '').strip()
        rid = int(rid) if rid.isdigit() else None
        frm, to = (request.args.get('from') or '').strip(), (request.args.get('to') or '').strip()
        frm = frm if re.fullmatch(r'\d{4}-\d{2}-\d{2}', frm) else ''
        to = to if re.fullmatch(r'\d{4}-\d{2}-\d{2}', to) else ''

        def where(col_date, col_rid):
            sql, par = '', []
            if rid:
                sql += f' AND {col_rid}=%s'; par.append(rid)
            if frm:
                sql += f' AND {col_date}>=%s'; par.append(frm)
            if to:
                sql += f' AND {col_date}<=%s'; par.append(to)
            return sql, tuple(par)

        with xdb() as c:
            w, p = where('s.sold_date', 'o.reseller_id')
            gen = {r['rid']: float(r['v']) for r in rows(
                c, 'SELECT o.reseller_id AS rid, COALESCE(SUM(o.commission),0) AS v FROM orders o '
                   'JOIN sales s ON s.order_id=o.id WHERE s.deleted_at IS NULL AND o.reseller_id IS NOT NULL'
                   + w + ' GROUP BY o.reseller_id', p)}
            w, p = where('paid_on', 'reseller_id')
            paid = {r['rid']: float(r['v']) for r in rows(
                c, 'SELECT reseller_id AS rid, COALESCE(SUM(amount),0) AS v FROM reseller_payouts WHERE 1=1'
                   + w + ' GROUP BY reseller_id', p)}
            payments = rows(c, 'SELECT id,reseller_id,reseller_name,amount,method,account,holder,cedula,paid_on '
                               'FROM reseller_payouts WHERE 1=1' + w + ' ORDER BY paid_on DESC, id DESC LIMIT 200', p)
            all_gen = {r['rid']: float(r['v']) for r in rows(
                c, 'SELECT o.reseller_id AS rid, COALESCE(SUM(o.commission),0) AS v FROM orders o '
                   'JOIN sales s ON s.order_id=o.id WHERE s.deleted_at IS NULL AND o.reseller_id IS NOT NULL '
                   'GROUP BY o.reseller_id')}
            all_paid = {r['rid']: float(r['v']) for r in rows(
                c, 'SELECT reseller_id AS rid, COALESCE(SUM(amount),0) AS v FROM reseller_payouts '
                   'GROUP BY reseller_id')}
            names = {r['id']: r['name'] for r in rows(c, 'SELECT id,name FROM resellers')}
        for pm in payments:
            names.setdefault(pm['reseller_id'], pm['reseller_name'])
        ids = set(gen) | set(paid) | ({rid} if rid else set())
        out, tg, tp = [], 0.0, 0.0
        for i in sorted(ids, key=lambda k: names.get(k, '')):
            g, pd = gen.get(i, 0.0), paid.get(i, 0.0)
            tg, tp = tg + g, tp + pd
            out.append({'reseller_id': i, 'name': names.get(i, f'Vendedor #{i}'), 'generated': round(g, 2),
                        'paid': round(pd, 2), 'pending': round(g - pd, 2),
                        'balance': round(all_gen.get(i, 0.0) - all_paid.get(i, 0.0), 2)})
        return jsonify({'rows': out, 'payments': payments,
                        'totals': {'generated': round(tg, 2), 'paid': round(tp, 2), 'pending': round(tg - tp, 2)}})
