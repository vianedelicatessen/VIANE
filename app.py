CAMBIOS EN app.py  (solo se agregan/cambian estas partes; lo demás NO se toca)
===========================================================================

PASO 1 - IMPORTS (arriba del todo)
----------------------------------
a) Cambia esta línea:
   from datetime import datetime, timezone
por:
   from datetime import datetime, timezone, timedelta

b) Justo debajo de "import secrets" agrega:
   import re
   import unicodedata


PASO 2 - TABLAS REQUERIDAS
--------------------------
Cambia esta línea:
REQUIRED_TABLES = {'products', 'reviews', 'orders', 'settings', 'ads', 'payments', 'media'}
por:
REQUIRED_TABLES = {'products', 'reviews', 'orders', 'settings', 'ads', 'payments', 'media', 'categories', 'sales'}


PASO 3 - TABLAS NUEVAS
----------------------
Dentro de la lista SCHEMA, pega esto JUSTO DESPUÉS de la línea
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


PASO 4 - MIGRACIÓN AUTOMÁTICA (se ejecuta una sola vez)
-------------------------------------------------------
En la función ensure_schema(), busca el final del bloque de productos de ejemplo:

                    for p in SAMPLE_PRODUCTS:
                        con.execute(
                            'INSERT INTO products(name,category,price,description,image,active,created_at) '
                            'VALUES(%s,%s,%s,%s,%s,%s,%s)', (*p, now()))

y pega esto justo debajo, con la MISMA sangría que el "if con.execute('SELECT COUNT(*)..." (16 espacios):

                try:  # categorias iniciales, ordenar productos y pasar pedidos ya entregados a ventas
                    with con.transaction():
                        for n in DEFAULT_CATEGORIES:
                            ensure_category(con, n)
                        reclassify_all(con)
                        for o in con.execute("SELECT * FROM orders WHERE status='Entregado'").fetchall():
                            save_sale(con, o, _dr_date(o['updated_at']))
                except Exception as exc:
                    print(f'[VIANE] Migracion de categorias/ventas: {exc!r}')


PASO 5 - FUNCIONES NUEVAS
-------------------------
Pega este bloque completo DESPUÉS de la función to_float(...) (sección "Utilidades"):

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


PASO 6 - REEMPLAZA la función public_data() completa por esta
-------------------------------------------------------------
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


PASO 7 - admin_data(): cambia UNA línea y agrega OTRA
-----------------------------------------------------
a) Cambia:
            'orders': rows(c, 'SELECT * FROM orders ORDER BY id DESC'),
por:
            'orders': rows(c, "SELECT * FROM orders WHERE status<>'Entregado' ORDER BY id DESC"),

b) Agrega justo debajo (misma sangría):
            'categories': rows(c, 'SELECT k.id,k.name,k.active,(SELECT COUNT(*) FROM products p '
                                  'WHERE p.category=k.name) AS total FROM categories k ORDER BY k.id'),


PASO 8 - PRODUCTOS: la categoría se decide sola
-----------------------------------------------
a) En create_product(), reemplaza:
    with db() as c:
        row = c.execute(
            'INSERT INTO products(name,category,price,description,image,active,created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, str(d.get('category', 'bandejas')), price, desc, image,
             1 if d.get('active', True) else 0, now())).fetchone()
por:
    with db() as c:
        category = resolve_category(c, name, d.get('category'))
        row = c.execute(
            'INSERT INTO products(name,category,price,description,image,active,created_at) '
            'VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id',
            (name, category, price, desc, image,
             1 if d.get('active', True) else 0, now())).fetchone()

b) En update_product(), reemplaza:
        c.execute('UPDATE products SET name=%s,category=%s,price=%s,description=%s,image=%s,active=%s WHERE id=%s',
                  (vals['name'], vals['category'], price, vals['description'], vals['image'],
                   int(bool(vals['active'])), pid))
por:
        category = resolve_category(c, vals['name'], vals['category'])
        c.execute('UPDATE products SET name=%s,category=%s,price=%s,description=%s,image=%s,active=%s WHERE id=%s',
                  (vals['name'], category, price, vals['description'], vals['image'],
                   int(bool(vals['active'])), pid))


PASO 9 - PEDIDOS: al marcar "Entregado" se crea la venta
--------------------------------------------------------
En update_order(), busca:
        if cur.rowcount == 0:
            return jsonify({'error': 'Pedido no encontrado'}), 404
y agrega justo debajo (misma sangría, dentro del "with db() as c:"):
        if status == 'Entregado':
            save_sale(c, c.execute('SELECT * FROM orders WHERE id=%s', (oid,)).fetchone())
        else:
            c.execute('DELETE FROM sales WHERE order_id=%s', (oid,))


PASO 10 - RUTAS NUEVAS
----------------------
Pega este bloque ANTES de la línea:   if __name__ == '__main__':

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
