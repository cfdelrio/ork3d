"""Servidor de desarrollo de ORK3D: sitio estático + API de reseñas con S3 falso.

Uso (desde la raíz del repo):
    python3 dev/serve.py [puerto]          # por defecto 8787, solo 127.0.0.1

Imita a CloudFront: /opinar/<token> → opinar.html, /pagina → pagina.html y
omite lo que el deploy no publica (api/, dev/, admin/, infra/, docs/).
La API /api/resenas/* usa api/resenas/app.py con un S3 en memoria; la subida de
fotos (presigned POST) la recibe /s3-fake/<bucket> y la valida la misma política.
Los tokens de prueba se generan al arrancar y se listan en GET /dev/tokens.
Nada de esto se despliega: dev/ está excluido del deploy.
"""
import datetime as dt
import email.policy
import json
import mimetypes
import os
import secrets
import sys
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api', 'resenas'))

import app  # noqa: E402
from fakes import FakeClientError, FakeS3  # noqa: E402

NO_PUBLICO = {'api', 'dev', 'admin', 'infra', 'docs', '.git', '.github'}
TIPOS = {
    '.avif': 'image/avif',
    '.webp': 'image/webp',
    '.svg': 'image/svg+xml',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
}

S3 = None
TOKENS = {}
PUERTO = 8787


class DevS3(FakeS3):
    """Igual que FakeS3, pero la URL firmada apunta a este servidor."""

    def generate_presigned_post(self, Bucket, Key, **kw):
        post = super().generate_presigned_post(Bucket, Key, **kw)
        post['url'] = f'http://127.0.0.1:{PUERTO}/s3-fake/{Bucket}'
        return post


def _iso(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def sembrar(s3):
    """Crea tokens de prueba: varios ok (uno por caso E2E) y uno de cada estado no válido."""
    ahora = dt.datetime.now(dt.timezone.utc)

    def registro(**cambios):
        base = {
            'v': 1,
            'created_at': _iso(ahora),
            'expires_at': _iso(ahora + dt.timedelta(days=60)),
            'revoked': False,
            'product_slug': 'trofeo-acrilico',
            'product_label': 'Trofeo acrílico',
            'category': 'trofeos',
            'verified': True,
        }
        base.update(cambios)
        return base

    casos = {
        'ok1': registro(), 'ok2': registro(), 'ok3': registro(), 'ok4': registro(),
        'ok5': registro(), 'ok6': registro(),
        'usado': registro(),
        'vencido': registro(expires_at=_iso(ahora - dt.timedelta(days=1))),
        'revocado': registro(revoked=True),
    }
    tokens = {}
    for nombre, reg in casos.items():
        tok = secrets.token_urlsafe(24)  # 32 caracteres, igual que el CLI de admin
        tokens[nombre] = tok
        s3.objects[f'tokens/{app.token_hash(tok)}.json'] = json.dumps(reg).encode('utf-8')
    s3.objects[f'claims/{app.token_hash(tokens["usado"])}.json'] = b'{}'
    return tokens


class Handler(BaseHTTPRequestHandler):
    server_version = 'ORK3D-dev'

    def log_message(self, formato, *args):  # silencioso: el E2E mira la consola del navegador
        pass

    def do_GET(self):
        self._despachar('GET')

    def do_POST(self):
        self._despachar('POST')

    def do_OPTIONS(self):
        self._despachar('OPTIONS')

    def _despachar(self, metodo):
        ruta = urlsplit(self.path).path
        largo = int(self.headers.get('Content-Length') or 0)
        cuerpo = self.rfile.read(largo) if largo else b''
        if ruta.startswith('/api/resenas'):
            return self._api(metodo, ruta[len('/api/resenas'):] or '/', cuerpo)
        if ruta.startswith('/s3-fake/') and metodo == 'POST':
            return self._s3_fake(ruta, cuerpo)
        if ruta == '/dev/tokens':
            return self._json(200, TOKENS)
        if ruta == '/dev/state':
            return self._json(200, self._estado())
        if ruta == '/config.js':
            return self._config()
        return self._estatico(ruta)

    # ── API de reseñas (el mismo handler que la Lambda) ──────────────────
    def _api(self, metodo, ruta, cuerpo):
        evento = {
            'requestContext': {'http': {'method': metodo}},
            'rawPath': ruta,
            'body': cuerpo.decode('utf-8', 'replace'),
        }
        r = app.handler(evento)
        self.send_response(r['statusCode'])
        for clave, valor in r['headers'].items():
            self.send_header(clave, valor)
        datos = r['body'].encode('utf-8')
        self.send_header('Content-Length', str(len(datos)))
        self.end_headers()
        if metodo != 'OPTIONS':
            self.wfile.write(datos)

    # ── Subida firmada a S3 (multipart del navegador) ────────────────────
    def _s3_fake(self, ruta, cuerpo):
        bucket = ruta.split('/')[2]
        ctype = self.headers.get('Content-Type', '')
        msg = BytesParser(policy=email.policy.HTTP).parsebytes(
            b'Content-Type: ' + ctype.encode('utf-8') + b'\r\n\r\n' + cuerpo)
        campos, archivo = {}, None
        for parte in msg.iter_parts():
            nombre = parte.get_param('name', header='content-disposition')
            datos = parte.get_payload(decode=True)
            if nombre == 'file':
                archivo = datos
            else:
                campos[nombre] = datos.decode('utf-8')
        if archivo is None or 'key' not in campos:
            return self._texto(400, 'MalformedPOSTRequest')
        try:
            S3.browser_post(campos['key'], archivo, campos.get('Content-Type', ''))
        except FakeClientError as exc:
            estado = exc.response['ResponseMetadata']['HTTPStatusCode']
            return self._texto(estado, exc.response['Error']['Code'])
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.send_header('x-dev-bucket', bucket)
        self.end_headers()

    # ── Vistas de depuración ─────────────────────────────────────────────
    def _estado(self):
        resenas = []
        for clave in sorted(k for k in S3.objects if k.startswith('resenas/')):
            r = json.loads(S3.objects[clave])
            if r.get('photo_key'):
                cabecera = S3.objects[r['photo_key']][:3].hex()
            else:
                cabecera = None
            resenas.append({**r, 'foto_cabecera': cabecera})
        claims = sorted(k for k in S3.objects if k.startswith('claims/'))
        return {'resenas': resenas, 'claims': len(claims),
                'pendientes': sorted(k for k in S3.objects if k.startswith('pendientes/'))}

    def _config(self):
        with open(os.path.join(ROOT, 'config.js'), encoding='utf-8') as f:
            base = f.read()
        extra = ("\n// Solo dev: API en el mismo origen y una reseña de Google de prueba.\n"
                 "window.ORK3D_CONFIG.RESENAS_API_URL = '/api/resenas';\n"
                 "window.ORK3D_CONFIG.GOOGLE_REVIEW_URL = 'https://g.page/r/dev-ork3d/review';\n")
        self._bytes(200, TIPOS['.js'], (base + extra).encode('utf-8'))

    # ── Archivos estáticos (con las reglas de CloudFront) ────────────────
    def _estatico(self, ruta):
        if ruta.startswith('/opinar/'):
            ruta = '/opinar.html'
        elif ruta == '/':
            ruta = '/index.html'
        elif not os.path.splitext(ruta)[1]:
            ruta = ruta.rstrip('/') + '.html'
        partes = ruta.lstrip('/').split('/')
        if partes[0] in NO_PUBLICO:
            return self._texto(404, 'No encontrado')
        archivo = os.path.realpath(os.path.join(ROOT, *partes))
        if not archivo.startswith(ROOT + os.sep) or not os.path.isfile(archivo):
            return self._texto(404, 'No encontrado')
        tipo = TIPOS.get(os.path.splitext(archivo)[1]) or mimetypes.guess_type(archivo)[0] or 'application/octet-stream'
        with open(archivo, 'rb') as f:
            self._bytes(200, tipo, f.read())

    # ── Respuestas ───────────────────────────────────────────────────────
    def _bytes(self, estado, tipo, datos):
        self.send_response(estado)
        self.send_header('Content-Type', tipo)
        self.send_header('Content-Length', str(len(datos)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(datos)

    def _json(self, estado, obj):
        self._bytes(estado, 'application/json; charset=utf-8', json.dumps(obj, ensure_ascii=False).encode('utf-8'))

    def _texto(self, estado, texto):
        self._bytes(estado, 'text/plain; charset=utf-8', texto.encode('utf-8'))


def main():
    global S3, TOKENS, PUERTO
    PUERTO = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    S3 = DevS3()
    TOKENS = sembrar(S3)
    app._SVC = app.Service(S3, 'dev-resenas', f'http://127.0.0.1:{PUERTO}',
                           google_review_url='https://g.page/r/dev-ork3d/review')
    servidor = ThreadingHTTPServer(('127.0.0.1', PUERTO), Handler)
    print(f'ORK3D dev en http://127.0.0.1:{PUERTO}', flush=True)
    servidor.serve_forever()


if __name__ == '__main__':
    main()
