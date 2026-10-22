import os
import re
import json
import secrets
import sqlite3
import cloudinary
import cloudinary.uploader
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, session, render_template, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
application = Flask(__name__)

BASE = Path(__file__).resolve().parent
UPLOAD_DIR = BASE / 'static' / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Detecta si estamos en Vercel para usar /tmp (única ruta con permisos de escritura)
if os.environ.get('VERCEL'):
    DB_PATH = Path('/tmp/viane.sqlite3')
else:
    DB_PATH = BASE / 'viane.sqlite3'

ALLOWED_IMAGES = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
ALLOWED_MEDIA = ALLOWED_IMAGES | {'mp4', 'mp3', 'wav', 'ogg', 'm4a'}
STATUSES = ['Pago pendiente', 'Pago recibido', 'Pedido confirmado', 'En preparación']
app.secret_key = os.environ.get('VIANE_SECRET_KEY') or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', MAX_CONTENT_LENGTH=16 * 1024 * 1024)

@app.before_request
def ensure_db_initialized():
    if not hasattr(app, '_db_initialized'):
        init_db()
        app._db_initialized = True

# Credenciales de Administrador configuradas
ADMIN_EMAIL = os.environ.get('VIANE_ADMIN_EMAIL', 'vianedelicatessen@gmail.com').strip().lower()
ADMIN_PASSWORD = os.environ.get('VIANE_ADMIN_PASSWORD', 'erisson09@')

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con

def init_db():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'bandejas',price REAL NOT NULL,description TEXT NOT NULL,image TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,rating INTEGER NOT NULL,products TEXT NOT NULL,comment TEXT NOT NULL,phone TEXT DEFAULT '',approved INTEGER NOT NULL DEFAULT 0,admin_reply TEXT DEFAULT '',created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,code TEXT UNIQUE NOT NULL,name TEXT NOT NULL,phone TEXT NOT NULL,province TEXT NOT NULL,delivery_date TEXT NOT NULL,items TEXT NOT NULL,total REAL NOT NULL,payment_status TEXT NOT NULL DEFAULT 'Pago pendiente',status TEXT NOT NULL DEFAULT 'Pago pendiente',notes TEXT DEFAULT '',incident TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ads(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,url TEXT NOT NULL,image TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,url TEXT NOT NULL,details TEXT DEFAULT '',active INTEGER NOT NULL DEFAULT 1,tip_enabled INTEGER NOT NULL DEFAULT 0);
        ''')
        defaults = {
            'brand_name':'VIANE DELICATESSEN','logo':'','whatsapp':'18296759640','instagram':'vianedelicatessen',
            'background_type':'color','background_value':'#FFFDF5','review_url':'','tips_message':'¡Gracias por apoyar a VIANE Delicatessen!'
        }
        for k, v in defaults.items():
            c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
        
        count = c.execute('SELECT COUNT(*) FROM products').fetchone()[0]
        if not count:
            samples = [
                ('Desayuno Sorpresa VIP Viane','desayunos',3500,'Desayuno especial personalizado. Confirma disponibilidad y detalles por WhatsApp.','',0),
                ('Bandeja Romántica','bandejas',4200,'Bandeja premium personalizada para sorprender.','',0),
                ('Picaderas para Cumpleaños','picaderas',4500,'Variedad de bocadillos para compartir.','',0)
            ]
            for p in samples:
                c.execute('INSERT INTO products(name,category,price,description,image,active,created_at) VALUES(?,?,?,?,?,?,?)', (*p, datetime.now(timezone.utc).isoformat()))


def now(): return datetime.now(timezone.utc).isoformat()
def asdict(row): return dict(row) if row else None
def setting_map():
    with db() as c: return {r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}
def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin') or session.get('email') != ADMIN_EMAIL:
            return jsonify({'error':'Acceso administrativo requerido'}),401
        return fn(*args, **kwargs)
    return wrapped
def allowed_file(filename, allowed): return '.' in filename and filename.rsplit('.',1)[1].lower() in allowed

@app.get('/')
def home(): return render_template('index.html')
@app.get('/uploads/<path:filename>')
def uploads(filename): return send_from_directory(UPLOAD_DIR, filename)

@app.get('/api/public')
def public_data():
    with db() as c:
        products=[dict(r) for r in c.execute('SELECT * FROM products WHERE active=1 ORDER BY id DESC')]
        reviews=[dict(r) for r in c.execute('SELECT id,name,rating,products,comment,admin_reply,created_at FROM reviews WHERE approved=1 ORDER BY id DESC')]
        ads=[dict(r) for r in c.execute('SELECT * FROM ads WHERE active=1')]
        payments=[dict(r) for r in c.execute('SELECT id,title,url,details,tip_enabled FROM payments WHERE active=1')]
    return jsonify({'products':products,'reviews':reviews,'ads':ads,'payments':payments,'settings':setting_map(),'statuses':STATUSES})

@app.post('/api/login')
def login():
    data = request.get_json(silent=True) or request.form
    email = str(data.get('email', '')).strip().lower()
    password = str(data.get('password', '')).strip()

    if not ADMIN_PASSWORD:
        return jsonify({'error':'El administrador debe configurar la contraseña en el servidor.'}),503
    if email != ADMIN_EMAIL.lower() or not secrets.compare_digest(password, ADMIN_PASSWORD.strip()):
        return jsonify({'error':'Correo o contraseña incorrectos'}),401
    
    session.clear()
    session['admin'] = True
    session['email'] = ADMIN_EMAIL
    return jsonify({'ok': True, 'email': ADMIN_EMAIL})

@app.post('/api/logout')
def logout(): session.clear(); return jsonify({'ok':True})
    
@app.route('/Bandeja360.html')
def bandeja_360():
    return render_template('Bandeja360.html')


@app.get('/api/admin/data')
@admin_required
def admin_data():
    with db() as c:
        return jsonify({
            'products':[dict(r) for r in c.execute('SELECT * FROM products ORDER BY id DESC')],
            'reviews':[dict(r) for r in c.execute('SELECT * FROM reviews ORDER BY id DESC')],
            'orders':[dict(r) for r in c.execute('SELECT * FROM orders ORDER BY id DESC')],
            'ads':[dict(r) for r in c.execute('SELECT * FROM ads ORDER BY id DESC')],
            'payments':[dict(r) for r in c.execute('SELECT * FROM payments ORDER BY id DESC')],
            'settings':setting_map(),
            'statuses':STATUSES
        })

@app.post('/api/upload')
@admin_required
def upload():
    f=request.files.get('file'); kind=request.form.get('kind','image')
    if not f or not f.filename: return jsonify({'error':'Selecciona un archivo'}),400
    allowed=ALLOWED_MEDIA if kind=='media' else ALLOWED_IMAGES
   upload_result = 
cloudinary.uploader.upload(f) 
filename =upload_result.get("secure_url")
return jsonify({"url": filename,
                "filiname": filename})

@app.post('/api/products')
@admin_required
def create_product():
    d=request.get_json(force=True); name=str(d.get('name','')).strip(); desc=str(d.get('description','')).strip(); image=str(d.get('image','')).strip()
    try: price=float(d.get('price'))
    except: return jsonify({'error':'Precio inválido'}),400
    if not name or not desc or not image or price<0: return jsonify({'error':'Nombre, imagen, precio y descripción son obligatorios'}),400
    with db() as c:
        cur=c.execute('INSERT INTO products(name,category,price,description,image,active,created_at) VALUES(?,?,?,?,?,?,?)',(name,str(d.get('category','bandejas')),price,desc,image,1 if d.get('active',True) else 0,now()))
        return jsonify({'id':cur.lastrowid,'ok':True})

@app.put('/api/products/<int:pid>')
@admin_required
def update_product(pid):
    d=request.get_json(force=True)
    with db() as c:
        if not c.execute('SELECT id FROM products WHERE id=?',(pid,)).fetchone(): return jsonify({'error':'Producto no encontrado'}),404
        old=dict(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
        vals={k:d.get(k,old[k]) for k in ['name','category','price','description','image','active']}
        if not vals['name'] or not vals['description'] or not vals['image']: return jsonify({'error':'Faltan campos requeridos'}),400
        c.execute('UPDATE products SET name=?,category=?,price=?,description=?,image=?,active=? WHERE id=?',(vals['name'],vals['category'],float(vals['price']),vals['description'],vals['image'],int(bool(vals['active'])),pid))
    return jsonify({'ok':True})

@app.delete('/api/products/<int:pid>')
@admin_required
def delete_product(pid):
    with db() as c: c.execute('DELETE FROM products WHERE id=?',(pid,))
    return jsonify({'ok':True})

@app.post('/api/reviews')
def submit_review():
    d=request.get_json(force=True); name=str(d.get('name','')).strip(); comment=str(d.get('comment','')).strip(); products=d.get('products',[]); rating=int(d.get('rating',0))
    if not name or not comment or rating not in range(1,6) or not isinstance(products,list) or not 1<=len(products)<=3: return jsonify({'error':'Completa nombre, comentario, calificación y de 1 a 3 productos'}),400
    with db() as c:
        cur=c.execute('INSERT INTO reviews(name,rating,products,comment,phone,approved,created_at) VALUES(?,?,?,?,?,?,?)',(name,rating,json.dumps(products,ensure_ascii=False),comment,str(d.get('phone',''))[:40],0,now()))
        return jsonify({'ok':True,'id':cur.lastrowid,'message':'¡Gracias! Tu reseña fue recibida y se publicará cuando sea revisada.'})

@app.patch('/api/reviews/<int:rid>')
@admin_required
def moderate_review(rid):
    d=request.get_json(force=True)
    with db() as c:
        c.execute('UPDATE reviews SET approved=?,admin_reply=? WHERE id=?',(int(bool(d.get('approved',False))),str(d.get('admin_reply',''))[:1000],rid))
    return jsonify({'ok':True})

@app.delete('/api/reviews/<int:rid>')
@admin_required
def delete_review(rid):
    with db() as c: c.execute('DELETE FROM reviews WHERE id=?',(rid,))
    return jsonify({'ok':True})

@app.post('/api/orders')
def create_order():
    d=request.get_json(force=True); items=d.get('items',[])
    if not str(d.get('name','')).strip() or not str(d.get('phone','')).strip() or not items: return jsonify({'error':'Completa los datos y agrega productos'}),400
    with db() as c:
        ids=[int(i.get('id')) for i in items if str(i.get('id','')).isdigit()]
        if not ids: return jsonify({'error':'Productos inválidos'}),400
        q=','.join('?' for _ in ids); rows=c.execute(f'SELECT * FROM products WHERE active=1 AND id IN ({q})',ids).fetchall(); byid={r['id']:r for r in rows}
        clean=[]; total=0
        for item in items:
            try: pid=int(item['id'])
            except: continue
            if pid in byid:
                p=byid[pid]; clean.append({'id':pid,'name':p['name'],'price':p['price']}); total+=p['price']
        if not clean: return jsonify({'error':'No hay productos válidos'}),400
        code='VIA-'+secrets.token_hex(4).upper()
        c.execute('INSERT INTO orders(code,name,phone,province,delivery_date,items,total,payment_status,status,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(code,str(d['name']).strip(),str(d['phone']).strip(),str(d.get('province','Puerto Plata')),str(d.get('delivery_date','')),json.dumps(clean,ensure_ascii=False),total,'Pago pendiente','Pago pendiente',str(d.get('notes',''))[:2000],now(),now()))
    s=setting_map(); review_url=s.get('review_url','')
    return jsonify({'ok':True,'code':code,'total':total,'review_url':review_url,'whatsapp':s.get('whatsapp','18296759640')})

@app.get('/api/track/<code>')
def track_order(code):
    with db() as c: row=c.execute('SELECT code,status,payment_status,delivery_date,updated_at,incident FROM orders WHERE code=?',(code.upper(),)).fetchone()
    if not row: return jsonify({'error':'No encontramos un pedido con ese código. Revisa e inténtalo otra vez.'}),404
    return jsonify(dict(row))

@app.patch('/api/orders/<int:oid>')
@admin_required
def update_order(oid):
    d=request.get_json(force=True); status=d.get('status','Pago pendiente'); payment=d.get('payment_status','Pago pendiente')
    if status not in STATUSES: return jsonify({'error':'Estado no válido'}),400
    with db() as c:
        c.execute('UPDATE orders SET status=?,payment_status=?,notes=?,incident=?,updated_at=? WHERE id=?',(status,payment,str(d.get('notes',''))[:2000],str(d.get('incident',''))[:1000],now(),oid))
    return jsonify({'ok':True})

@app.put('/api/settings')
@admin_required
def update_settings():
    d=request.get_json(force=True)
    allowed={'brand_name','logo','whatsapp','instagram','background_type','background_value','review_url','tips_message'}
    with db() as c:
        for k,v in d.items():
            if k in allowed: c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,str(v)[:4000]))
    return jsonify({'ok':True})

@app.route('/api/ads',methods=['POST'])
@admin_required
def add_ad():
    d=request.get_json(force=True)
    if not d.get('title') or not d.get('url'): return jsonify({'error':'Título y enlace son obligatorios'}),400
    with db() as c: cur=c.execute('INSERT INTO ads(title,url,image,active) VALUES(?,?,?,?)',(d['title'],d['url'],d.get('image',''),int(bool(d.get('active',True)))))
    return jsonify({'ok':True,'id':cur.lastrowid})

@app.put('/api/ads/<int:aid>')
@admin_required
def update_ad(aid):
    d=request.get_json(force=True)
    with db() as c: c.execute('UPDATE ads SET title=?,url=?,image=?,active=? WHERE id=?',(d.get('title',''),d.get('url',''),d.get('image',''),int(bool(d.get('active',True))),aid))
    return jsonify({'ok':True})

@app.delete('/api/ads/<int:aid>')
@admin_required
def delete_ad(aid):
    with db() as c: c.execute('DELETE FROM ads WHERE id=?',(aid,))
    return jsonify({'ok':True})

@app.route('/api/payments',methods=['POST'])
@admin_required
def add_payment():
    d=request.get_json(force=True)
    if not d.get('title') or not d.get('url'): return jsonify({'error':'Nombre y enlace de pago son obligatorios'}),400
    with db() as c: cur=c.execute('INSERT INTO payments(title,url,details,active,tip_enabled) VALUES(?,?,?,?,?)',(d['title'],d['url'],d.get('details',''),int(bool(d.get('active',True))),int(bool(d.get('tip_enabled',False)))))
    return jsonify({'ok':True,'id':cur.lastrowid})

@app.put('/api/payments/<int:pid>')
@admin_required
def update_payment(pid):
    d=request.get_json(force=True)
    with db() as c: c.execute('UPDATE payments SET title=?,url=?,details=?,active=?,tip_enabled=? WHERE id=?',(d.get('title',''),d.get('url',''),d.get('details',''),int(bool(d.get('active',True))),int(bool(d.get('tip_enabled',False))),pid))
    return jsonify({'ok':True})

@app.delete('/api/payments/<int:pid>')
@admin_required
def delete_payment(pid):
    with db() as c: c.execute('DELETE FROM payments WHERE id=?',(pid,))
    return jsonify({'ok':True})

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
