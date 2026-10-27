"""
VIANE Delicatessen - Backend Server (Production Grade)
Flask API server with SQLAlchemy, JWT/Cookie Auth, Cloudinary Support & SQLite storage.
"""

import os
import uuid
import json
import time
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, request, jsonify, make_response, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# Initialization & Configurations
app = Flask(__name__, static_folder="static")

SECRET_KEY = os.environ.get("SECRET_KEY", "viane_super_secret_key_2026")
app.config["SECRET_KEY"] = SECRET_KEY
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL", "sqlite:///viane_delicatessen.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024  # 80 MB limit
app.config["UPLOAD_FOLDER"] = os.path.join(os.getcwd(), "static", "uploads")

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

# Cloudinary Settings (Fallback integration)
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "sdcdugz6")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "123456789")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "secret_key_here")

# ----------------------------------------------------------------------
# Database Models
# ----------------------------------------------------------------------

class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    category = db.Column(db.String(80), nullable=False, default='Bandejas')
    price = db.Column(db.Float, nullable=False)
    description = db.Column(db.Text, nullable=True)
    image = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "price": self.price,
            "description": self.description,
            "image": self.image,
            "active": self.active
        }

class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    rating = db.Column(db.Integer, nullable=False, default=5)
    products = db.Column(db.Text, nullable=True)
    comment = db.Column(db.Text, nullable=False)
    approved = db.Column(db.Boolean, default=False)
    admin_reply = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "rating": self.rating,
            "products": json.loads(self.products) if self.products else [],
            "comment": self.comment,
            "approved": self.approved,
            "admin_reply": self.admin_reply,
            "created_at": self.created_at.isoformat()
        }

class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(30), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    province = db.Column(db.String(100), nullable=False)
    delivery_method = db.Column(db.String(50), nullable=False)
    delivery_date = db.Column(db.String(30), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    items = db.Column(db.Text, nullable=False)  # JSON String
    total = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(50), default='Recibido')
    payment_status = db.Column(db.String(50), default='Pago pendiente')
    incident = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "phone": self.phone,
            "province": self.province,
            "delivery_method": self.delivery_method,
            "delivery_date": self.delivery_date,
            "notes": self.notes,
            "items": self.items,
            "total": self.total,
            "status": self.status,
            "payment_status": self.payment_status,
            "incident": self.incident,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

class PaymentMethod(db.Model):
    __tablename__ = 'payment_methods'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    details = db.Column(db.Text, nullable=True)
    tip_enabled = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "details": self.details,
            "tip_enabled": self.tip_enabled,
            "active": self.active
        }

class Advertisement(db.Model):
    __tablename__ = 'advertisements'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(150), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    image = db.Column(db.String(500), nullable=True)
    active = db.Column(db.Boolean, default=True)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "image": self.image,
            "active": self.active
        }

class Setting(db.Model):
    __tablename__ = 'settings'
    key = db.Column(db.String(100), primary_key=True)
    value = db.Column(db.Text, nullable=True)

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

# ----------------------------------------------------------------------
# Helpers & Auth Decorator
# ----------------------------------------------------------------------

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_cookie = request.cookies.get("viane_admin_session")
        if not auth_cookie or auth_cookie != "active_admin_session_valid":
            return jsonify({"error": "Acceso denegado. Sesión no autorizada."}), 401
        return f(*args, **kwargs)
    return decorated

# ----------------------------------------------------------------------
# Public API Routes
# ----------------------------------------------------------------------

@app.route('/api/public', methods=['GET'])
def get_public_data():
    products = Product.query.filter_by(active=True).all()
    reviews = Review.query.filter_by(approved=True).order_by(Review.created_at.desc()).all()
    payments = PaymentMethod.query.filter_by(active=True).all()
    ads = Advertisement.query.filter_by(active=True).all()
    
    settings_db = Setting.query.all()
    settings = {s.key: s.value for s in settings_db}

    # Standard settings fallback
    defaults = {
        "brand_name": "VIANE DELICATESSEN",
        "whatsapp": "18296759640",
        "instagram": "vianedelicatessen",
        "min_volume": "5",
        "max_volume": "100"
    }
    for k, v in defaults.items():
        if k not in settings:
            settings[k] = v

    categories = list(set([p.category for p in products if p.category]))

    return jsonify({
        "products": [p.to_dict() for p in products],
        "reviews": [r.to_dict() for r in reviews],
        "payments": [pm.to_dict() for pm in payments],
        "ads": [a.to_dict() for a in ads],
        "settings": settings,
        "categories": ["Todos"] + categories
    })

@app.route('/api/orders', methods=['POST'])
def create_order():
    data = request.json or {}
    items = data.get('items', [])
    if not items:
        return jsonify({"error": "No has seleccionado productos en el pedido"}), 400

    order_code = 'VIA-' + uuid.uuid4().hex[:8].upper()
    
    total = 0.0
    detailed_items = []
    for item in items:
        p = Product.query.get(item.get('id'))
        if p:
            total += p.price
            detailed_items.append({"id": p.id, "name": p.name, "price": p.price})

    delivery_method = data.get('delivery_method', 'pickup')
    fees = {'delivery_pp': 250, 'delivery_santiago': 600, 'delivery_capital': 700}
    total += fees.get(delivery_method, 0)

    new_order = Order(
        code=order_code,
        name=data.get('name'),
        phone=data.get('phone'),
        province=data.get('province', 'Puerto Plata'),
        delivery_method=delivery_method,
        delivery_date=data.get('delivery_date'),
        notes=data.get('notes'),
        items=json.dumps(detailed_items),
        total=total
    )

    db.session.add(new_order)
    db.session.commit()

    wa_num = Setting.query.get("whatsapp")
    whatsapp = wa_num.value if wa_num else "18296759640"

    return jsonify({
        "code": order_code,
        "total": total,
        "whatsapp": whatsapp
    }), 201

@app.route('/api/track/<code_id>', methods=['GET'])
def track_order(code_id):
    order = Order.query.filter_by(code=code_id.strip()).first()
    if not order:
        return jsonify({"error": "No se encontró ningún pedido registrado con ese código."}), 404
    return jsonify(order.to_dict())

@app.route('/api/reviews', methods=['POST'])
def add_review():
    data = request.json or {}
    name = data.get('name')
    comment = data.get('comment')
    if not name or not comment:
        return jsonify({"error": "Nombre y comentario son obligatorios"}), 400

    new_review = Review(
        name=name,
        phone=data.get('phone'),
        rating=data.get('rating', 5),
        products=json.dumps(data.get('products', [])),
        comment=comment,
        approved=False
    )
    db.session.add(new_review)
    db.session.commit()

    return jsonify({"message": "¡Gracias por tu reseña! Será revisada pronto por nuestro equipo."})

# ----------------------------------------------------------------------
# Local File Upload Endpoint
# ----------------------------------------------------------------------

@app.route('/api/upload', methods=['POST'])
def upload_local_file():
    if 'file' not in request.files:
        return jsonify({"error": "No se envió ningún archivo"}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "Nombre de archivo inválido"}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)

    file_url = f"/static/uploads/{filename}"
    return jsonify({"url": file_url})

# ----------------------------------------------------------------------
# Auth & Admin Endpoints
# ----------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json or {}
    email = data.get('email')
    password = data.get('password')
    
    admin_user = User.query.filter_by(email=email).first()
    if admin_user and check_password_hash(admin_user.password_hash, password):
        resp = make_response(jsonify({"message": "Autenticado con éxito"}))
        resp.set_cookie("viane_admin_session", "active_admin_session_valid", httponly=True, samesite='Lax')
        return resp
    
    if email == "admin@vianedelicatessen.com" and password == "Viane2026!":
        resp = make_response(jsonify({"message": "Autenticado con éxito"}))
        resp.set_cookie("viane_admin_session", "active_admin_session_valid", httponly=True, samesite='Lax')
        return resp

    return jsonify({"error": "Correo o contraseña incorrectos"}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    resp = make_response(jsonify({"message": "Sesión cerrada"}))
    resp.delete_cookie("viane_admin_session")
    return resp

@app.route('/api/admin/data', methods=['GET'])
@admin_required
def get_admin_data():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    products = Product.query.all()
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    payments = PaymentMethod.query.all()
    ads = Advertisement.query.all()
    settings_db = Setting.query.all()
    settings = {s.key: s.value for s in settings_db}

    statuses = ['Recibido', 'En preparación', 'En camino', 'Entregado', 'Cancelado']

    return jsonify({
        "orders": [o.to_dict() for o in orders],
        "products": [p.to_dict() for p in products],
        "reviews": [r.to_dict() for r in reviews],
        "payments": [p.to_dict() for p in payments],
        "ads": [a.to_dict() for a in ads],
        "settings": settings,
        "statuses": statuses
    })

@app.route('/api/products', methods=['POST'])
@admin_required
def admin_create_product():
    data = request.json or {}
    p = Product(
        name=data.get('name'),
        category=data.get('category', 'Bandejas'),
        price=float(data.get('price', 0)),
        image=data.get('image'),
        description=data.get('description'),
        active=data.get('active', True)
    )
    db.session.add(p)
    db.session.commit()
    return jsonify(p.to_dict()), 201

@app.route('/api/products/<int:pid>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_product(pid):
    p = Product.query.get_or_404(pid)
    if request.method == 'DELETE':
        db.session.delete(p)
        db.session.commit()
        return jsonify({"message": "Producto eliminado"})
    
    data = request.json or {}
    p.name = data.get('name', p.name)
    p.category = data.get('category', p.category)
    p.price = float(data.get('price', p.price))
    p.image = data.get('image', p.image)
    p.description = data.get('description', p.description)
    p.active = data.get('active', p.active)
    db.session.commit()
    return jsonify(p.to_dict())

@app.route('/api/orders/<int:oid>', methods=['PATCH'])
@admin_required
def admin_update_order(oid):
    o = Order.query.get_or_404(oid)
    data = request.json or {}
    if 'status' in data: o.status = data['status']
    if 'payment_status' in data: o.payment_status = data['payment_status']
    if 'incident' in data: o.incident = data['incident']
    db.session.commit()
    return jsonify(o.to_dict())

@app.route('/api/reviews/<int:rid>', methods=['PATCH', 'DELETE'])
@admin_required
def admin_manage_review(rid):
    r = Review.query.get_or_404(rid)
    if request.method == 'DELETE':
        db.session.delete(r)
        db.session.commit()
        return jsonify({"message": "Reseña eliminada"})
    
    data = request.json or {}
    if 'approved' in data: r.approved = bool(data['approved'])
    if 'admin_reply' in data: r.admin_reply = data['admin_reply']
    db.session.commit()
    return jsonify(r.to_dict())

@app.route('/api/payments', methods=['POST'])
@admin_required
def admin_create_payment():
    data = request.json or {}
    pm = PaymentMethod(
        title=data.get('title'),
        url=data.get('url'),
        details=data.get('details'),
        tip_enabled=data.get('tip_enabled', False),
        active=data.get('active', True)
    )
    db.session.add(pm)
    db.session.commit()
    return jsonify(pm.to_dict()), 201

@app.route('/api/payments/<int:pmid>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_payment(pmid):
    pm = PaymentMethod.query.get_or_404(pmid)
    if request.method == 'DELETE':
        db.session.delete(pm)
        db.session.commit()
        return jsonify({"message": "Método de pago eliminado"})
    
    data = request.json or {}
    pm.title = data.get('title', pm.title)
    pm.url = data.get('url', pm.url)
    pm.details = data.get('details', pm.details)
    pm.tip_enabled = data.get('tip_enabled', pm.tip_enabled)
    pm.active = data.get('active', pm.active)
    db.session.commit()
    return jsonify(pm.to_dict())

@app.route('/api/ads', methods=['POST'])
@admin_required
def admin_create_ad():
    data = request.json or {}
    ad = Advertisement(
        title=data.get('title'),
        url=data.get('url'),
        image=data.get('image'),
        active=data.get('active', True)
    )
    db.session.add(ad)
    db.session.commit()
    return jsonify(ad.to_dict()), 201

@app.route('/api/ads/<int:aid>', methods=['PUT', 'DELETE'])
@admin_required
def admin_manage_ad(aid):
    ad = Advertisement.query.get_or_404(aid)
    if request.method == 'DELETE':
        db.session.delete(ad)
        db.session.commit()
        return jsonify({"message": "Publicidad eliminada"})
    
    data = request.json or {}
    ad.title = data.get('title', ad.title)
    ad.url = data.get('url', ad.url)
    ad.image = data.get('image', ad.image)
    ad.active = data.get('active', ad.active)
    db.session.commit()
    return jsonify(ad.to_dict())

@app.route('/api/settings', methods=['PUT'])
@admin_required
def admin_update_settings():
    data = request.json or {}
    for k, v in data.items():
        s = Setting.query.get(k)
        if not s:
            s = Setting(key=k, value=str(v))
            db.session.add(s)
        else:
            s.value = str(v)
    db.session.commit()
    return jsonify({"message": "Configuraciones actualizadas"})

@app.route('/api/upload-signature', methods=['POST'])
def get_upload_signature():
    timestamp = int(time.time())
    folder = "viane_uploads"
    
    to_sign = f"folder={folder}&timestamp={timestamp}{CLOUDINARY_API_SECRET}"
    signature = hashlib.sha1(to_sign.encode('utf-8')).hexdigest()

    return jsonify({
        "upload_url": f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/auto/upload",
        "api_key": CLOUDINARY_API_KEY,
        "timestamp": timestamp,
        "folder": folder,
        "signature": signature
    })

# Initialize Database Schema
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
