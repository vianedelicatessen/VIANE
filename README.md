# VIANE Delicatessen — sitio con panel administrativo

Este proyecto adapta el concepto visual del HTML compartido y agrega un backend Flask con base de datos SQLite. No está publicado en internet por el simple hecho de descargarlo.

## Funciones incluidas
- Catálogo público conectado a SQLite; creación, edición, publicación/ocultación y eliminación de productos.
- Subida de logo, imágenes, fondos y multimedia (MP4/audio) desde el servidor.
- Reseñas con selección de 1 a 3 productos, moderación y respuesta desde el panel.
- Pedidos con código único `VIA-XXXXXXXX`, consulta pública de estado y edición de estado/pago/incidencias desde el panel.
- Configuración de marca, WhatsApp, Instagram, fondo y enlace de reseñas.
- Gestión de enlaces publicitarios y métodos de pago; los métodos marcados para propinas aparecen tras enviar una reseña.
- Autenticación de panel mediante sesión del servidor y contraseña configurada en variables de entorno.

## Instalación local
1. Instala Python 3.10 o superior.
2. En la carpeta del proyecto ejecuta `python -m venv .venv` y activa el entorno.
3. Ejecuta `pip install -r requirements.txt`.
4. Configura las variables de entorno indicadas en `.env.example` (no publiques una contraseña en el código). En Linux/macOS, por ejemplo:
   ```bash
   export VIANE_ADMIN_EMAIL='flaquitabella875@gmail.com'
   export VIANE_ADMIN_PASSWORD='TU_CONTRASENA_LARGA_Y_UNICA'
   export VIANE_SECRET_KEY='UN_SECRETO_ALEATORIO_LARGO'
   python app.py
   ```
5. Abre `http://127.0.0.1:5000`.

En Windows PowerShell, configura las variables con `$env:VIANE_ADMIN_PASSWORD='...'` y `$env:VIANE_SECRET_KEY='...'` antes de `python app.py`.

## Antes de publicar
- Despliega en un hosting que soporte Python/Flask y HTTPS. Para una tienda real, configura almacenamiento persistente para la base de datos y los archivos subidos; algunos hostings borran archivos locales al redeplegar.
- Cambia `VIANE_SECRET_KEY` y usa una contraseña robusta. No uses el servidor de desarrollo de Flask en producción.
- SQLite funciona para un proyecto pequeño en un solo servidor. Para tráfico mayor, usa PostgreSQL y almacenamiento de objetos persistente.
- El formulario de pedidos genera el código de seguimiento y prepara un mensaje de WhatsApp. **No puede enviar mensajes de WhatsApp automáticamente por sí solo**: eso requiere WhatsApp Business Platform/Cloud API, una cuenta comercial, número verificado y configuración de plantillas/webhooks. Actualmente el cliente debe pulsar “Continuar por WhatsApp”.
- La reseña se invita desde la confirmación del pedido, pero el envío automático al cliente requiere WhatsApp Business API. Puedes establecer el enlace público en Panel → Logo y diseño → Enlace público de reseñas una vez tengas dominio publicado.
- Los enlaces de pago son botones/enlaces configurables; el proyecto no procesa ni verifica pagos automáticamente. Para confirmación automática se necesita API oficial del proveedor de pago y credenciales autorizadas.
- Las imágenes se validan por extensión, pero una regla técnica no puede garantizar que cada archivo sea realmente HD/4K. El administrador debe subir imágenes de calidad adecuada.
- Música/audio y vídeo se pueden subir, pero la reproducción automática de audio suele estar bloqueada por los navegadores hasta que el usuario interactúe.
- Para una instalación pública con seguridad reforzada, añade protección CSRF, límites de intentos de inicio de sesión, validación MIME por contenido, escaneo de archivos, copias de seguridad y monitorización.

## Notas
El archivo `viane.sqlite3` se crea automáticamente al iniciar. La página utiliza una base de datos del servidor, no `localStorage`, por lo que los cambios son compartidos por los visitantes conectados al mismo despliegue.
