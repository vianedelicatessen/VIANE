import os
import cloudinary
import cloudinary.uploader
import cloudinary.api
import secrets
from flask import Flask, request, jsonify, render_template
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# Configuración de Cloudinary (Asegúrate de tener tus credenciales configuradas)
# cloudinary.config(
#     cloud_name = os.environ.get('CLOUDINARY_CLOUD_NAME'),
#     api_key = os.environ.get('CLOUDINARY_API_KEY'),
#     api_secret = os.environ.get('CLOUDINARY_API_SECRET')
# )

# Función para obtener la conexión a Supabase (PostgreSQL)
def db():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise ValueError("DATABASE_URL no está configurada en las variables de entorno.")
    # Supabase requiere conexiones SSL
    conn = psycopg2.connect(database_url, sslmode="require")
    return conn

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/products', methods=['GET'])
def get_products():
    try:
        conn = db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT * FROM productos")
        productos = cursor.fetchall()
        cursor.close()
        conn.close()
        return jsonify(productos)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.post('/api/login')

def login():

    data = request.get_json(silent=True) or request.form

    email = str(data.get('email', '')).strip().lower()

    password = str(data.get('password', '')).strip()



    if not ADMIN_PASSWORD:

        return jsonify({'error':'El administrador debe configurar la contraseña en el servidor.'}),503

    if email != ADMIN_EMAIL or not secrets.compare_digest(password.encode('utf-8'), ADMIN_PASSWORD.strip().encode('utf-8')):

        return jsonify({'error':'Correo o contraseña incorrectos'}),401



    session.clear()

    session['admin'] = True

    session['email'] = ADMIN_EMAIL

    return jsonify({'ok': True, 'email': ADMIN_EMAIL})



@app.route('/api/products', methods=['POST'])
def create_product():
    # Nota: Asegúrate de adaptar esta lógica según recibas los datos (FormData o JSON)
    name = request.form.get('name')
    category = request.form.get('category')
    price = request.form.get('price')
    description = request.form.get('description')
    image = request.form.get('image')
    
    try:
        price = float(price) if price else 0.0
    except ValueError:
        return jsonify({'error': 'Precio inválido'}), 400

    if not name or not price:
        return jsonify({'error': 'Nombre y precio son obligatorios'}), 400

    try:
        conn = db()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO productos (name, category, price, description, image, is_active)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (name, category, price, description, image, True)
        )
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'message': 'Producto creado con éxito'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/products/<int:id>', methods=['DELETE'])
def delete_product(id):
    try:
        conn = db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM productos WHERE id = %s", (id,))
        conn.commit()
        cursor.close()
        conn.close()
        return jsonify({'message': 'Producto eliminado con éxito'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
