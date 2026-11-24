"""
INVENTARIO y PÉRDIDAS — módulo independiente (Blueprint) para «Gastos y costos» y el panel.

Cómo se calcula la existencia (stock) de cada insumo — sin duplicar nada de lo que ya cargas:
    stock = compras de «Compras e insumos» (desde la fecha de inicio)
          + entradas / ajustes manuales
          − consumo automático de las RECETAS por cada venta activa (ventas borradas o reembolsadas no consumen)
          − salidas, mermas y devoluciones manuales
  • Inventario inicial: «Iniciar inventario» fija la fecha de inicio y el conteo físico de partida.
  • Valorización: costo promedio ponderado (el mismo que usa «Costo por bandeja»).
  • Lotes (FIFO) para detectar productos por vencer o vencidos (vida útil por insumo o fecha de vencimiento en la entrada).
  • Pérdidas: registro con categoría, evidencia, recuperado (seguro/reposición) y reportes; una pérdida de un insumo o de
    una bandeja descuenta el stock sola (movimiento «merma» ligado a la pérdida).
No modifica ninguna tabla ni ruta existente: solo lee supplies / recipe_items / sales / orders y escribe en inv_items,
inv_moves, losses y en la clave oculta _inventory_start de settings.
"""
import csv
import io
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, Response, jsonify, request, session

bp = Blueprint('inventario', __name__)
CTX = {'db': None, 'is_admin': lambda: False, 'skey': lambda s: str(s).strip().lower()}
DR = timezone(timedelta(hours=-4))
EPS = 1e-9
EXPIRY_WARN_DAYS = 7
MOVE_KINDS = ('entrada', 'salida', 'merma', 'devolucion', 'ajuste')
MOVE_LABELS = {'entrada': 'Entrada', 'salida': 'Salida / uso', 'merma': 'Merma', 'devolucion': 'Devolución a proveedor', 'ajuste': 'Ajuste de conteo'}
LOSS_CATEGORIES = {'merma': 'Merma / desperdicio', 'vencido': 'Producto vencido', 'danado': 'Dañado / mal estado',
                   'error_pedido': 'Error en el pedido', 'entrega_fallida': 'Entrega fallida / devolución', 'rotura': 'Rotura / accidente',
                   'robo': 'Robo / extravío', 'cancelacion': 'Cancelación con costo', 'otro': 'Otro'}

DDL = [
    '''CREATE TABLE IF NOT EXISTS public.inv_items(
        skey TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT 'und', category TEXT NOT NULL DEFAULT '',
        min_stock DOUBLE PRECISION NOT NULL DEFAULT 0, max_stock DOUBLE PRECISION NOT NULL DEFAULT 0, location TEXT NOT NULL DEFAULT '',
        supplier TEXT NOT NULL DEFAULT '', shelf_life_days INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
        notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS public.inv_moves(
        id BIGSERIAL PRIMARY KEY, skey TEXT NOT NULL, kind TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
        unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0, move_date TEXT NOT NULL, expires_on TEXT, note TEXT NOT NULL DEFAULT '',
        ref TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, deleted_at TEXT)''',
    'CREATE INDEX IF NOT EXISTS idx_inv_moves_item ON public.inv_moves(skey, move_date) WHERE deleted_at IS NULL',
    "CREATE INDEX IF NOT EXISTS idx_inv_moves_ref ON public.inv_moves(ref) WHERE ref <> ''",
    '''CREATE TABLE IF NOT EXISTS public.losses(
        id BIGSERIAL PRIMARY KEY, loss_date TEXT NOT NULL, category TEXT NOT NULL, concept TEXT NOT NULL,
        item_type TEXT NOT NULL DEFAULT 'otro', item_ref TEXT NOT NULL DEFAULT '', quantity DOUBLE PRECISION NOT NULL DEFAULT 1,
        unit_cost DOUBLE PRECISION NOT NULL DEFAULT 0, amount DOUBLE PRECISION NOT NULL, recovered DOUBLE PRECISION NOT NULL DEFAULT 0,
        deduct_stock INTEGER NOT NULL DEFAULT 0, responsible TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
        evidence TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT)''',
    'CREATE INDEX IF NOT EXISTS idx_losses_date ON public.losses(loss_date DESC) WHERE deleted_at IS NULL',
    'CREATE INDEX IF NOT EXISTS idx_losses_month ON public.losses((LEFT(loss_date, 7))) WHERE deleted_at IS NULL',
    'CREATE INDEX IF NOT EXISTS idx_losses_category ON public.losses(category) WHERE deleted_at IS NULL',
    'CREATE INDEX IF NOT EXISTS idx_losses_trash ON public.losses(deleted_at) WHERE deleted_at IS NOT NULL',
    'ALTER TABLE public.inv_items ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE public.inv_moves ENABLE ROW LEVEL SECURITY',
    'ALTER TABLE public.losses ENABLE ROW LEVEL SECURITY',
]


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def hoy():
    return datetime.now(DR).date()


def ahora():
    return datetime.now(DR).replace(microsecond=0).isoformat()


def num(v, default=0.0):
    try:
        return float(str(v).replace(',', '.')) if str(v).strip() != '' else default
    except (TypeError, ValueError):
        return default


def txt(v, n):
    return re.sub(r'\s+', ' ', str(v or '')).strip()[:n]


def fecha(v, default=None):
    v = str(v or '').strip()[:10]
    try:
        return datetime.strptime(v, '%Y-%m-%d').date().isoformat()
    except ValueError:
        return default


def admin_only(fn):
    @wraps(fn)
    def wrapped(*a, **k):
        if not CTX['is_admin']():
            return jsonify({'error': 'Acceso administrativo requerido'}), 401
        return fn(*a, **k)
    return wrapped


def body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else {}


def err(msg, code=400):
    return jsonify({'error': msg}), code


def csv_response(filename, header, rows_):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows_)
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename={filename}'})


def get_start(c):
    r = c.execute("SELECT value FROM settings WHERE key='_inventory_start'").fetchone()
    return fecha(r['value']) if r else None


# ---------------------------------------------------------------------------
# Cálculo de existencias (función pura: se prueba sin base de datos)
# ---------------------------------------------------------------------------
def consumo_por_recetas(sold, recipes):
    """[(fecha, skey, cantidad, codigo_pedido)] según lo vendido y la receta de cada producto."""
    import json
    out = []
    for s in sold:
        try:
            items = json.loads(s['items'] or '[]')
        except ValueError:
            continue
        cnt = {}
        for it in (items if isinstance(items, list) else []):
            try:
                pid = int(it.get('id'))
            except (TypeError, ValueError, AttributeError):
                continue
            cnt[pid] = cnt.get(pid, 0) + 1
        for pid, n in cnt.items():
            for skey, q in recipes.get(pid, ()):
                out.append((s['d'], skey, q * n, s['code']))
    return out


def calcular(items, buys, moves, cons, start, today):
    """Existencias, costo promedio, lotes (FIFO), estado y reposición sugerida de cada insumo."""
    cut30 = (today - timedelta(days=30)).isoformat()
    D, names = {}, {}

    def d(k):
        return D.setdefault(k, {'in': [], 'out': 0.0, 'qty_all': 0.0, 'cost_all': 0.0, 'last_buy': '', 'supplier': '', 'cons30': 0.0})
    for b in buys:
        q, cost = num(b['qty']), num(b['cost'])
        x = d(b['skey'])
        names.setdefault(b['skey'], b['name'])
        x['qty_all'] += q
        x['cost_all'] += cost
        f = b['bought_on'] or str(b['created_at'] or '')[:10]
        if f >= start:
            x['in'].append([f, q, cost / q if q else 0.0, None])
        if f >= x['last_buy']:
            x['last_buy'], x['supplier'] = f, (b['supplier'] or x['supplier'])
    for m in moves:
        if m['move_date'] < start:
            continue
        x, q = d(m['skey']), num(m['qty'])
        if q > 0:
            x['in'].append([m['move_date'], q, num(m['unit_cost']), m['expires_on'] or None])
            if num(m['unit_cost']) > 0 and m['kind'] == 'entrada':
                x['qty_all'] += q
                x['cost_all'] += q * num(m['unit_cost'])
        else:
            x['out'] += -q
    for f, k, q, _ in cons:
        if f >= start:
            x = d(k)
            x['out'] += q
            if f >= cut30:
                x['cons30'] += q
    out = {}
    for k in set(items) | set(D):
        it, x = items.get(k) or {}, D.get(k) or {'in': [], 'out': 0.0, 'qty_all': 0.0, 'cost_all': 0.0, 'last_buy': '', 'supplier': '', 'cons30': 0.0}
        avg = x['cost_all'] / x['qty_all'] if x['qty_all'] > EPS else 0.0
        entradas = sum(l[1] for l in x['in'])
        stock = entradas - x['out']
        shelf = int(it.get('shelf_life_days') or 0)
        pend, lots = x['out'], []
        for f, q, uc, exp in sorted(x['in'], key=lambda l: l[0]):   # FIFO: lo más viejo se gasta primero
            take = min(q, pend)
            pend -= take
            left = q - take
            if left > EPS:
                e = exp or ((datetime.strptime(f, '%Y-%m-%d').date() + timedelta(days=shelf)).isoformat() if shelf > 0 else None)
                lots.append({'date': f, 'qty': round(left, 3), 'expires_on': e})
        hoy_s, warn = today.isoformat(), (today + timedelta(days=EXPIRY_WARN_DAYS)).isoformat()
        vencido = sum(l['qty'] for l in lots if l['expires_on'] and l['expires_on'] < hoy_s)
        por_vencer = sum(l['qty'] for l in lots if l['expires_on'] and hoy_s <= l['expires_on'] <= warn)
        mn, mx = num(it.get('min_stock')), num(it.get('max_stock'))
        status = 'agotado' if stock <= EPS else ('vencido' if vencido > EPS else ('por_vencer' if por_vencer > EPS else ('bajo' if mn > 0 and stock < mn else ('exceso' if mx > 0 and stock > mx else 'ok'))))
        if stock <= EPS and vencido <= EPS:
            status = 'agotado'
        diario = x['cons30'] / 30.0
        objetivo = mx if mx > 0 else (mn * 2 if mn > 0 else 0.0)
        sug = max(objetivo - stock, 0.0) if (mn > 0 and stock < mn) or (stock <= EPS and objetivo > 0) else 0.0
        out[k] = {'skey': k, 'name': it.get('name') or names.get(k) or k, 'unit': it.get('unit') or 'und', 'category': it.get('category') or '',
                  'location': it.get('location') or '', 'supplier': it.get('supplier') or x['supplier'], 'min_stock': mn, 'max_stock': mx,
                  'shelf_life_days': shelf, 'active': int(it.get('active', 1)) if it else 1, 'notes': it.get('notes') or '',
                  'stock': round(stock, 3), 'unit_cost': round(avg, 4), 'value': round(max(stock, 0) * avg, 2), 'last_buy': x['last_buy'],
                  'coverage_days': round(stock / diario, 1) if diario > EPS and stock > EPS else None, 'status': status,
                  'expired_qty': round(vencido, 3), 'expiring_qty': round(por_vencer, 3), 'lots': lots,
                  'suggested': round(sug, 2), 'suggested_cost': round(sug * avg, 2), 'in_master': bool(it)}
    return out


def kardex(skey, buys, moves, cons, start, shelf=0):
    """Libro de movimientos de UN insumo, con saldo corrido desde la fecha de inicio."""
    ev = []
    for b in buys:
        f = b['bought_on'] or str(b['created_at'] or '')[:10]
        if b['skey'] == skey and f >= start:
            ev.append((f, 0, 'Compra', num(b['qty']), (num(b['cost']) / num(b['qty'])) if num(b['qty']) else 0.0, 'Compras e insumos' + (f" · {b['supplier']}" if b['supplier'] else '')))
    for m in moves:
        if m['skey'] == skey and m['move_date'] >= start:
            ev.append((m['move_date'], 1, MOVE_LABELS.get(m['kind'], m['kind']), num(m['qty']), num(m['unit_cost']), (m['note'] or '') + (f" [{m['ref']}]" if m['ref'] else ''), m['id']))
    for f, k, q, code in cons:
        if k == skey and f >= start:
            ev.append((f, 2, 'Consumo por venta', -q, 0.0, f'Pedido {code}'))
    saldo, out = 0.0, []
    for e in sorted(ev, key=lambda e: (e[0], e[1])):
        saldo += e[3]
        out.append({'date': e[0], 'type': e[2], 'qty': round(e[3], 3), 'unit_cost': round(e[4], 4), 'balance': round(saldo, 3), 'note': e[5], 'move_id': e[6] if len(e) > 6 else None})
    return out


def cargar(c, start):
    items = {r['skey']: dict(r) for r in c.execute('SELECT * FROM inv_items').fetchall()}
    buys = c.execute('SELECT id,skey,name,qty,cost,supplier,bought_on,created_at FROM supplies ORDER BY id').fetchall()
    moves = c.execute('SELECT id,skey,kind,qty,unit_cost,move_date,expires_on,note,ref,created_by FROM inv_moves WHERE deleted_at IS NULL ORDER BY move_date,id').fetchall()
    recipes = {}
    for r in c.execute('SELECT product_id,skey,qty FROM recipe_items').fetchall():
        recipes.setdefault(r['product_id'], []).append((r['skey'], num(r['qty'])))
    sold = c.execute('SELECT s.sold_date AS d,o.code,o.items FROM sales s JOIN orders o ON o.id=s.order_id '
                     'WHERE s.deleted_at IS NULL AND s.sold_date >= %s ORDER BY s.sold_date', (start,)).fetchall()
    return items, buys, moves, consumo_por_recetas(sold, recipes), recipes


def estado(c):
    start = get_start(c)
    base = start or '0000-01-01'
    items, buys, moves, cons, recipes = cargar(c, base)
    return start, calcular(items, buys, moves, cons, base, hoy()), (items, buys, moves, cons, recipes)


# ---------------------------------------------------------------------------
# INVENTARIO — rutas
# ---------------------------------------------------------------------------
@bp.get('/api/inventory')
@admin_only
def inv_resumen():
    with CTX['db']() as c:
        start, data, _ = estado(c)
    lista = sorted(data.values(), key=lambda x: (x['name'] or '').lower())
    activos = [x for x in lista if x['active']]
    prio = {'agotado': 0, 'vencido': 1, 'por_vencer': 2, 'bajo': 3}
    alertas = sorted([x for x in activos if x['status'] in prio], key=lambda x: (prio[x['status']], x['name']))
    compras = [x for x in activos if x['suggested'] > 0]
    return jsonify({'started': bool(start), 'start': start,
                    'kpis': {'items': len(activos), 'value': round(sum(x['value'] for x in activos), 2),
                             'out': sum(1 for x in activos if x['status'] == 'agotado'), 'low': sum(1 for x in activos if x['status'] == 'bajo'),
                             'expiring': sum(1 for x in activos if x['expiring_qty'] > EPS), 'expired': sum(1 for x in activos if x['expired_qty'] > EPS),
                             'shopping_cost': round(sum(x['suggested_cost'] for x in compras), 2)},
                    'items': lista, 'alerts': [{'skey': x['skey'], 'name': x['name'], 'status': x['status'], 'stock': x['stock'], 'unit': x['unit'], 'min_stock': x['min_stock'],
                                                'expired_qty': x['expired_qty'], 'expiring_qty': x['expiring_qty']} for x in alertas],
                    'shopping': [{'skey': x['skey'], 'name': x['name'], 'unit': x['unit'], 'stock': x['stock'], 'min_stock': x['min_stock'], 'suggested': x['suggested'],
                                  'unit_cost': x['unit_cost'], 'cost': x['suggested_cost'], 'supplier': x['supplier']} for x in compras],
                    'categories': sorted({x['category'] for x in lista if x['category']}), 'kinds': MOVE_LABELS})


@bp.get('/api/inventory/kardex/<skey>')
@admin_only
def inv_kardex(skey):
    with CTX['db']() as c:
        start, data, (items, buys, moves, cons, _) = estado(c)
    if skey not in data:
        return err('Insumo no encontrado.', 404)
    return jsonify({'item': data[skey], 'start': start, 'lines': kardex(skey, buys, moves, cons, start or '0000-01-01')[::-1][:300]})


def _stock_de(c, skey):
    _, data, _ = estado(c)
    return (data.get(skey) or {}).get('stock', 0.0), (data.get(skey) or {}).get('unit_cost', 0.0)


@bp.post('/api/inventory/items')
@admin_only
def inv_item_crear():
    d = body()
    name = txt(d.get('name'), 80)
    if len(name) < 2:
        return err('Escribe el nombre del insumo.')
    k = CTX['skey'](name)
    with CTX['db']() as c:
        if c.execute('SELECT 1 FROM inv_items WHERE skey=%s', (k,)).fetchone():
            return err('Ya existe un insumo con ese nombre.')
        c.execute('INSERT INTO inv_items(skey,name,unit,category,min_stock,max_stock,location,supplier,shelf_life_days,notes,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                  (k, name, txt(d.get('unit'), 12) or 'und', txt(d.get('category'), 40), max(num(d.get('min_stock')), 0), max(num(d.get('max_stock')), 0),
                   txt(d.get('location'), 60), txt(d.get('supplier'), 80), max(int(num(d.get('shelf_life_days'))), 0), txt(d.get('notes'), 300), ahora()))
    return jsonify({'ok': True, 'skey': k})


@bp.put('/api/inventory/items/<skey>')
@admin_only
def inv_item_guardar(skey):
    d = body()
    with CTX['db']() as c:
        ex = c.execute('SELECT name FROM inv_items WHERE skey=%s', (skey,)).fetchone()
        nombre = txt(d.get('name'), 80) or (ex['name'] if ex else skey)
        vals = (nombre, txt(d.get('unit'), 12) or 'und', txt(d.get('category'), 40), max(num(d.get('min_stock')), 0), max(num(d.get('max_stock')), 0),
                txt(d.get('location'), 60), txt(d.get('supplier'), 80), max(int(num(d.get('shelf_life_days'))), 0), 0 if str(d.get('active', 1)) in ('0', 'False', 'false') else 1, txt(d.get('notes'), 300))
        if ex:
            c.execute('UPDATE inv_items SET name=%s,unit=%s,category=%s,min_stock=%s,max_stock=%s,location=%s,supplier=%s,shelf_life_days=%s,active=%s,notes=%s WHERE skey=%s', vals + (skey,))
        else:
            c.execute('INSERT INTO inv_items(skey,name,unit,category,min_stock,max_stock,location,supplier,shelf_life_days,active,notes,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)', (skey,) + vals + (ahora(),))
    return jsonify({'ok': True})


@bp.post('/api/inventory/moves')
@admin_only
def inv_mover():
    d = body()
    kind, skey = str(d.get('kind') or ''), str(d.get('skey') or '').strip()
    if kind not in MOVE_KINDS or not skey:
        return err('Elige el insumo y el tipo de movimiento.')
    q = num(d.get('qty'))
    if abs(q) <= EPS:
        return err('La cantidad debe ser distinta de cero.')
    if kind != 'ajuste' and q < 0:
        return err('Escribe la cantidad en positivo; el tipo de movimiento define si suma o resta.')
    f = fecha(d.get('move_date'), hoy().isoformat())
    if f > (hoy() + timedelta(days=1)).isoformat():
        return err('La fecha no puede estar en el futuro.')
    signed = q if kind in ('entrada', 'ajuste') else -q
    with CTX['db']() as c:
        start = get_start(c)
        if not start:
            return err('Primero inicia el inventario (conteo inicial).')
        stock, avg = _stock_de(c, skey)
        if signed < 0 and stock + signed < -EPS and not d.get('force'):
            return err(f'No hay suficiente existencia: hay {stock:g} y intentas sacar {-signed:g}. Revisa el conteo o confirma para forzar.', 409)
        uc = max(num(d.get('unit_cost')), 0) or (avg if signed < 0 else 0)
        exp = fecha(d.get('expires_on')) if kind == 'entrada' else None
        row = c.execute('INSERT INTO inv_moves(skey,kind,qty,unit_cost,move_date,expires_on,note,ref,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                        (skey, kind, signed, uc, f, exp, txt(d.get('note'), 200), '', session.get('email', ''), ahora())).fetchone()
    return jsonify({'ok': True, 'id': row['id']})


@bp.delete('/api/inventory/moves/<int:mid>')
@admin_only
def inv_mover_borrar(mid):
    with CTX['db']() as c:
        m = c.execute('SELECT ref FROM inv_moves WHERE id=%s AND deleted_at IS NULL', (mid,)).fetchone()
        if not m:
            return err('Movimiento no encontrado.', 404)
        if m['ref']:
            return err('Este movimiento viene de una pérdida: elimínala desde «Pérdidas».')
        c.execute('UPDATE inv_moves SET deleted_at=%s WHERE id=%s', (ahora(), mid))
    return jsonify({'ok': True})


def _conteo(c, counts, note, kind_note):
    start, data, _ = estado(c)
    ajustes, dif = 0, 0.0
    for r in counts:
        k = str(r.get('skey') or '').strip()
        if not k or str(r.get('counted', '')).strip() == '':
            continue
        contado = max(num(r.get('counted')), 0.0)
        actual = (data.get(k) or {}).get('stock', 0.0)
        delta = contado - actual
        if abs(delta) > 0.0005:
            uc = (data.get(k) or {}).get('unit_cost', 0.0)
            c.execute('INSERT INTO inv_moves(skey,kind,qty,unit_cost,move_date,note,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                      (k, 'ajuste', round(delta, 4), uc, hoy().isoformat(), kind_note + (f' — {note}' if note else ''), session.get('email', ''), ahora()))
            ajustes += 1
            dif += delta * uc
    return ajustes, round(dif, 2)


@bp.post('/api/inventory/start')
@admin_only
def inv_iniciar():
    """Fija la fecha de inicio y registra el conteo físico de partida (lo contado = existencia inicial)."""
    d = body()
    f = fecha(d.get('date'), hoy().isoformat())
    with CTX['db']() as c:
        if get_start(c):
            return err('El inventario ya está iniciado. Usa «Conteo físico» para corregir existencias.')
        c.execute("INSERT INTO settings(key,value) VALUES('_inventory_start',%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (f,))
        n = 0
        for r in (d.get('counts') or []):
            k, q = str(r.get('skey') or '').strip(), max(num(r.get('counted')), 0.0)
            if k and q > 0:
                _, avg = 0, 0.0
                c.execute('INSERT INTO inv_moves(skey,kind,qty,unit_cost,move_date,note,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)',
                          (k, 'ajuste', q, 0, f, 'Inventario inicial', session.get('email', ''), ahora()))
                n += 1
    return jsonify({'ok': True, 'start': f, 'items_contados': n})


@bp.post('/api/inventory/count')
@admin_only
def inv_conteo():
    """Conteo físico: compara lo contado con la existencia del sistema y crea los ajustes de la diferencia."""
    d = body()
    with CTX['db']() as c:
        if not get_start(c):
            return err('Primero inicia el inventario.')
        n, dif = _conteo(c, d.get('counts') or [], txt(d.get('note'), 120), 'Conteo físico')
    return jsonify({'ok': True, 'ajustes': n, 'diferencia_valor': dif})


@bp.get('/api/inventory/export.csv')
@admin_only
def inv_export():
    with CTX['db']() as c:
        _, data, _ = estado(c)
    rows_ = [[x['name'], x['category'], x['unit'], x['stock'], x['min_stock'], x['max_stock'], x['unit_cost'], x['value'], x['status'], x['location'], x['supplier'], x['last_buy']]
             for x in sorted(data.values(), key=lambda x: x['name'].lower())]
    return csv_response(f'inventario_{hoy().isoformat()}.csv', ['Insumo', 'Categoría', 'Unidad', 'Existencia', 'Mínimo', 'Máximo', 'Costo unitario', 'Valor', 'Estado', 'Ubicación', 'Proveedor', 'Última compra'], rows_)


@bp.get('/api/inventory/shopping.csv')
@admin_only
def inv_compras_csv():
    with CTX['db']() as c:
        _, data, _ = estado(c)
    rows_ = [[x['name'], x['unit'], x['stock'], x['min_stock'], x['suggested'], x['unit_cost'], x['suggested_cost'], x['supplier']] for x in sorted(data.values(), key=lambda x: x['name'].lower()) if x['suggested'] > 0 and x['active']]
    return csv_response(f'lista_de_compras_{hoy().isoformat()}.csv', ['Insumo', 'Unidad', 'Existencia', 'Mínimo', 'Comprar', 'Costo unitario', 'Costo estimado', 'Proveedor'], rows_)


# ---------------------------------------------------------------------------
# PÉRDIDAS
# ---------------------------------------------------------------------------
def _sync_stock(c, lid, loss, activo):
    """Mantiene los movimientos de merma ligados a la pérdida (se recrean al editar y se quitan al borrar)."""
    c.execute('DELETE FROM inv_moves WHERE ref=%s', (f'loss:{lid}',))
    if not activo or not loss['deduct_stock'] or loss['quantity'] <= 0 or not get_start(c):
        return 0
    n = 0
    if loss['item_type'] == 'insumo' and loss['item_ref']:
        pares = [(loss['item_ref'], loss['quantity'])]
    elif loss['item_type'] == 'producto' and str(loss['item_ref']).isdigit():
        pares = [(r['skey'], num(r['qty']) * loss['quantity']) for r in c.execute('SELECT skey,qty FROM recipe_items WHERE product_id=%s', (int(loss['item_ref']),)).fetchall()]
    else:
        return 0
    _, data, _ = estado(c)
    for k, q in pares:
        if q > EPS:
            c.execute('INSERT INTO inv_moves(skey,kind,qty,unit_cost,move_date,note,ref,created_by,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                      (k, 'merma', -q, (data.get(k) or {}).get('unit_cost', 0.0), loss['loss_date'], 'Pérdida: ' + loss['concept'][:80], f'loss:{lid}', session.get('email', ''), ahora()))
            n += 1
    return n


def _costo_unitario(c, tipo, ref):
    if tipo == 'insumo' and ref:
        r = c.execute('SELECT SUM(cost) AS c, SUM(qty) AS q FROM supplies WHERE skey=%s', (ref,)).fetchone()
        return (num(r['c']) / num(r['q'])) if r and num(r['q']) else 0.0
    if tipo == 'producto' and str(ref).isdigit():
        r = c.execute('SELECT ri.qty AS qty, (SELECT SUM(s.cost)/NULLIF(SUM(s.qty),0) FROM supplies s WHERE s.skey=ri.skey) AS uc FROM recipe_items ri WHERE ri.product_id=%s', (int(ref),)).fetchall()
        return sum(num(x['qty']) * num(x['uc']) for x in r)
    return 0.0


def _validar_perdida(c, d):
    f = fecha(d.get('loss_date'))
    if not f:
        return None, 'La fecha no es válida.'
    if f > (hoy() + timedelta(days=1)).isoformat():
        return None, 'La fecha no puede estar en el futuro.'
    cat = str(d.get('category') or '')
    if cat not in LOSS_CATEGORIES:
        return None, 'Elige una categoría.'
    concepto = txt(d.get('concept'), 120)
    if len(concepto) < 3:
        return None, 'Describe qué se perdió.'
    tipo = str(d.get('item_type') or 'otro')
    tipo = tipo if tipo in ('insumo', 'producto', 'otro') else 'otro'
    ref = txt(d.get('item_ref'), 80) if tipo != 'otro' else ''
    cant = num(d.get('quantity'), 1.0)
    if cant <= 0:
        return None, 'La cantidad debe ser mayor que cero.'
    uc = num(d.get('unit_cost'), 0.0) or _costo_unitario(c, tipo, ref)
    monto = num(d.get('amount'), 0.0) or round(cant * uc, 2)
    if monto <= 0:
        return None, 'El monto de la pérdida debe ser mayor que cero.'
    rec = max(num(d.get('recovered'), 0.0), 0.0)
    if rec > monto + EPS:
        return None, 'Lo recuperado no puede superar la pérdida.'
    return {'loss_date': f, 'category': cat, 'concept': concepto, 'item_type': tipo, 'item_ref': ref, 'quantity': cant, 'unit_cost': round(uc, 4), 'amount': round(monto, 2),
            'recovered': round(rec, 2), 'deduct_stock': 1 if d.get('deduct_stock') and tipo != 'otro' else 0, 'responsible': txt(d.get('responsible'), 60),
            'notes': txt(d.get('notes'), 400), 'evidence': txt(d.get('evidence'), 300)}, None


COLS = 'loss_date,category,concept,item_type,item_ref,quantity,unit_cost,amount,recovered,deduct_stock,responsible,notes,evidence'


def _filtro(args):
    w, p = ['deleted_at IS NOT NULL' if args.get('deleted') == '1' else 'deleted_at IS NULL'], []
    if fecha(args.get('from')):
        w.append('loss_date >= %s'); p.append(fecha(args.get('from')))
    if fecha(args.get('to')):
        w.append('loss_date <= %s'); p.append(fecha(args.get('to')))
    if args.get('category') in LOSS_CATEGORIES:
        w.append('category = %s'); p.append(args['category'])
    q = txt(args.get('q'), 60)
    if q:
        w.append('(LOWER(concept) LIKE %s OR LOWER(responsible) LIKE %s OR LOWER(notes) LIKE %s)'); p += [f'%{q.lower()}%'] * 3
    return ' AND '.join(w), p


@bp.get('/api/losses')
@admin_only
def perdidas_lista():
    where, p = _filtro(request.args)
    size = min(max(int(num(request.args.get('size'), 25)), 5), 200)
    page = max(int(num(request.args.get('page'), 1)), 1)
    with CTX['db']() as c:
        t = c.execute(f'SELECT COUNT(*) AS n, COALESCE(SUM(amount),0) AS a, COALESCE(SUM(recovered),0) AS r FROM losses WHERE {where}', p).fetchone()
        data = c.execute(f'SELECT id,{COLS},created_by,deleted_at FROM losses WHERE {where} ORDER BY loss_date DESC, id DESC LIMIT %s OFFSET %s', p + [size, (page - 1) * size]).fetchall()
    n = int(t['n'])
    return jsonify({'items': data, 'count': n, 'page': page, 'pages': max((n + size - 1) // size, 1), 'sum_amount': round(num(t['a']), 2),
                    'sum_recovered': round(num(t['r']), 2), 'sum_net': round(num(t['a']) - num(t['r']), 2), 'categories': LOSS_CATEGORIES})


@bp.get('/api/losses/summary')
@admin_only
def perdidas_resumen():
    meses = min(max(int(num(request.args.get('months'), 12)), 3), 24)
    h = hoy()
    y, m = h.year, h.month
    keys = []
    for _ in range(meses):
        keys.append(f'{y:04d}-{m:02d}')
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    keys.reverse()
    desde = keys[0] + '-01'
    with CTX['db']() as c:
        mens = {r['m']: r for r in c.execute('SELECT LEFT(loss_date,7) AS m, SUM(amount) AS a, SUM(recovered) AS r, COUNT(*) AS n FROM losses WHERE deleted_at IS NULL AND loss_date >= %s GROUP BY 1', (desde,)).fetchall()}
        cats = c.execute('SELECT category, SUM(amount-recovered) AS net, COUNT(*) AS n FROM losses WHERE deleted_at IS NULL AND LEFT(loss_date,7)=%s GROUP BY category ORDER BY net DESC', (keys[-1],)).fetchall()
        top = c.execute('SELECT concept, SUM(amount-recovered) AS net, COUNT(*) AS n FROM losses WHERE deleted_at IS NULL AND loss_date >= %s GROUP BY concept ORDER BY net DESC LIMIT 5', (desde,)).fetchall()
        ventas = {r['m']: num(r['a']) for r in c.execute('SELECT LEFT(sold_date,7) AS m, SUM(amount) AS a FROM sales WHERE deleted_at IS NULL AND sold_date >= %s GROUP BY 1', (desde,)).fetchall()}
        year = c.execute('SELECT COALESCE(SUM(amount-recovered),0) AS net FROM losses WHERE deleted_at IS NULL AND LEFT(loss_date,4)=%s', (str(h.year),)).fetchone()
    serie = [{'month': k, 'amount': round(num((mens.get(k) or {}).get('a')), 2), 'recovered': round(num((mens.get(k) or {}).get('r')), 2),
              'net': round(num((mens.get(k) or {}).get('a')) - num((mens.get(k) or {}).get('r')), 2), 'count': int((mens.get(k) or {}).get('n') or 0), 'sales': round(ventas.get(k, 0.0), 2)} for k in keys]
    cur, prev = serie[-1], serie[-2]
    var = round((cur['net'] - prev['net']) / prev['net'] * 100, 1) if prev['net'] > 0 else None
    return jsonify({'month': keys[-1], 'current': cur, 'previous': prev, 'variation_pct': var,
                    'pct_of_sales': round(cur['net'] / cur['sales'] * 100, 2) if cur['sales'] > 0 else None, 'year_net': round(num(year['net']), 2),
                    'series': serie, 'by_category': [{'category': r['category'], 'label': LOSS_CATEGORIES.get(r['category'], r['category']), 'net': round(num(r['net']), 2), 'count': int(r['n'])} for r in cats],
                    'top': [{'concept': r['concept'], 'net': round(num(r['net']), 2), 'count': int(r['n'])} for r in top]})


@bp.post('/api/losses')
@admin_only
def perdida_crear():
    with CTX['db']() as c:
        v, e = _validar_perdida(c, body())
        if e:
            return err(e)
        row = c.execute(f'INSERT INTO losses({COLS},created_by,created_at,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id',
                        tuple(v[k] for k in COLS.split(',')) + (session.get('email', ''), ahora(), ahora())).fetchone()
        mov = _sync_stock(c, row['id'], v, True)
    return jsonify({'ok': True, 'id': row['id'], 'stock_descontado': mov})


@bp.put('/api/losses/<int:lid>')
@admin_only
def perdida_editar(lid):
    with CTX['db']() as c:
        if not c.execute('SELECT 1 FROM losses WHERE id=%s AND deleted_at IS NULL', (lid,)).fetchone():
            return err('Pérdida no encontrada.', 404)
        v, e = _validar_perdida(c, body())
        if e:
            return err(e)
        c.execute('UPDATE losses SET ' + ','.join(f'{k}=%s' for k in COLS.split(',')) + ',updated_at=%s WHERE id=%s', tuple(v[k] for k in COLS.split(',')) + (ahora(), lid))
        mov = _sync_stock(c, lid, v, True)
    return jsonify({'ok': True, 'stock_descontado': mov})


@bp.delete('/api/losses/<int:lid>')
@admin_only
def perdida_borrar(lid):
    with CTX['db']() as c:
        r = c.execute('SELECT * FROM losses WHERE id=%s AND deleted_at IS NULL', (lid,)).fetchone()
        if not r:
            return err('Pérdida no encontrada.', 404)
        c.execute('UPDATE losses SET deleted_at=%s, updated_at=%s WHERE id=%s', (ahora(), ahora(), lid))
        _sync_stock(c, lid, dict(r), False)   # el stock descontado se devuelve
    return jsonify({'ok': True})


@bp.post('/api/losses/<int:lid>/restore')
@admin_only
def perdida_restaurar(lid):
    with CTX['db']() as c:
        r = c.execute('SELECT * FROM losses WHERE id=%s AND deleted_at IS NOT NULL', (lid,)).fetchone()
        if not r:
            return err('Pérdida no encontrada.', 404)
        c.execute('UPDATE losses SET deleted_at=NULL, updated_at=%s WHERE id=%s', (ahora(), lid))
        _sync_stock(c, lid, dict(r), True)
    return jsonify({'ok': True})


@bp.get('/api/losses/export.csv')
@admin_only
def perdidas_csv():
    where, p = _filtro(request.args)
    with CTX['db']() as c:
        data = c.execute(f'SELECT {COLS} FROM losses WHERE {where} ORDER BY loss_date DESC, id DESC LIMIT 5000', p).fetchall()
    return csv_response(f'perdidas_{hoy().isoformat()}.csv', ['Fecha', 'Categoría', 'Concepto', 'Tipo', 'Referencia', 'Cantidad', 'Costo unitario', 'Pérdida', 'Recuperado', 'Pérdida neta', 'Responsable', 'Notas'],
                        [[r['loss_date'], LOSS_CATEGORIES.get(r['category'], r['category']), r['concept'], r['item_type'], r['item_ref'], r['quantity'], r['unit_cost'], r['amount'], r['recovered'],
                          round(num(r['amount']) - num(r['recovered']), 2), r['responsible'], r['notes']] for r in data])


def init_inventario(app, db, migrations, is_admin, skey=None):
    CTX['db'], CTX['is_admin'] = db, is_admin
    if skey:
        CTX['skey'] = skey
    for stmt in DDL:
        if stmt not in migrations:
            migrations.append(stmt)
    app.register_blueprint(bp)
