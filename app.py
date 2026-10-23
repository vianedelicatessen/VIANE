import os
import json
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, session, render_template, send_from_directory

app = Flask(__name__)

BASE = Path(__file__).resolve().parent

# En Vercel usamos /tmp/uploads (única ruta con permisos de escritura), localmente usamos static/uploads
if os.environ.get('VERCEL'):
    UPLOAD_DIR = Path('/tmp/uploads')
else:
    UPLOAD_DIR = BASE / 'static' / 'uploads'

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app.secret_key = os.environ.get('VANE_SECRET_KEY') or secrets.token_hex(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

ADMIN_EMAIL = os.environ.get('VANE_ADMIN_EMAIL', 'vianedelicatessen@gmail.com')
ADMIN_PASSWORD = os.environ.get('VANE_ADMIN_PASSWORD')

@app.before_request
def ensure_db_initialized():
    if not hasattr(g, '_db_initialized'):
        db_init()
        g._db_initialized = True

def db_init():
    with sqlite3.connect(BASE / 'database.db') as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS productos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                subtitle TEXT,
                description TEXT,
                price TEXT NOT NULL,
                image TEXT,
                category TEXT NOT NULL
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS recuentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                subtitle TEXT,
                description TEXT,
                price TEXT NOT NULL,
                image TEXT,
                category TEXT NOT NULL
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT UNIQUE NOT NULL,
                value TEXT
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS pagos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                metodo TEXT NOT NULL,
                whatsapp TEXT NOT NULL,
                instagram TEXT NOT NULL,
                background_color TEXT,
                table_background_color TEXT,
                title_color TEXT,
                msg_message TEXT
            )
        ''')
        
        # Valores por defecto para settings
        defaults = [
            ('brand_name', 'Viané Delicatessen'),
            ('whatsapp', '8290000000'),
            ('instagram', 'vianedelicatessen'),
            ('background_color', '#fff'),
            ('table_background_color', '#fafafa'),
            ('title_color', '#333'),
            ('msg_message', 'Hola, quiero ordenar este producto.')
        ]
        for k, v in defaults:
            c.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (k, v))
            
        conn.commit()

@app.route('/api/products', methods=['GET'])
def get_products():
    with sqlite3.connect(BASE / 'database.db') as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT id, title, subtitle, description, price, image, category FROM productos')
        products = [dict(row) for row in c.fetchall()]
    return jsonify(products)

@app.route('/api/settings', methods=['GET'])
def get_settings():
    with sqlite3.connect(BASE / 'database.db') as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute('SELECT key, value FROM settings')
        settings = {row['key']: row['value'] for row in c.fetchall()}
    return jsonify(settings)

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_DIR, filename)

if __name__ == '__main__':
    app.run(debug=True)
