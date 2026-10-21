"""Contrato entre el CLI de moderación (admin/resenas.sh) y la Lambda (app.py).

Corre el CLI real contra el shim de AWS (admin/tests/aws_shim.py, que guarda archivos
en un directorio temporal) y usa la lógica real de app.py sobre esos mismos archivos.
Así se prueba que lo que escribe uno lo lee el otro: formato de tokens, claves de
reseñas y fotos, claims y export público.

Requiere bash, jq y openssl, igual que el CLI.
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import app
from fakes import FakeClientError

RAIZ_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CLI = os.path.join(RAIZ_REPO, 'admin', 'resenas.sh')
SHIM = os.path.join(RAIZ_REPO, 'admin', 'tests', 'aws_shim.py')
PRIVADO = 'bucket-privado-test'
SITIO = 'bucket-sitio-test'
JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 20_000


class ArchivosS3:
    """La interfaz de S3 que usa app.py, sobre los archivos que deja el shim."""

    def __init__(self, raiz):
        self.raiz = raiz

    def _ruta(self, bucket, key):
        return os.path.join(self.raiz, bucket, *key.split('/'))

    def put_object(self, Bucket, Key, Body=b'', ContentType=None, IfNoneMatch=None, **_):
        ruta = self._ruta(Bucket, Key)
        if IfNoneMatch == '*' and os.path.exists(ruta):
            raise FakeClientError('PreconditionFailed', 412, 'La clave ya existe.')
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, 'wb') as f:
            f.write(Body.encode('utf-8') if isinstance(Body, str) else Body)
        return {}

    def get_object(self, Bucket, Key, Range=None, **_):
        ruta = self._ruta(Bucket, Key)
        if not os.path.isfile(ruta):
            raise FakeClientError('NoSuchKey', 404, 'No existe.')
        with open(ruta, 'rb') as f:
            datos = f.read()
        if Range:
            inicio, fin = Range.split('=')[1].split('-')
            datos = datos[int(inicio):int(fin) + 1]
        return {'Body': io.BytesIO(datos), 'ContentLength': len(datos)}

    def head_object(self, Bucket, Key, **_):
        ruta = self._ruta(Bucket, Key)
        if not os.path.isfile(ruta):
            raise FakeClientError('404', 404, 'Not Found')
        return {'ContentLength': os.path.getsize(ruta)}

    def delete_object(self, Bucket, Key, **_):
        ruta = self._ruta(Bucket, Key)
        if os.path.isfile(ruta):
            os.remove(ruta)
        return {}

    def generate_presigned_post(self, Bucket, Key, Fields=None, Conditions=None, ExpiresIn=3600):
        campos = {**(Fields or {}), 'key': Key, 'policy': 'contrato'}
        return {'url': f'https://{Bucket}.s3.amazonaws.com/', 'fields': campos}


def payload(token, **cambios):
    p = {
        'token': token,
        'rating': 5,
        'comment': 'Llegó a tiempo y se ve muy bien.',
        'aspects': ['calidad'],
        'display_mode': 'inicial',
        'name': 'Ana Gómez',
        'permission_to_publish': True,
        'private_feedback': 'SECRETO-PRIVADO',
        'contact_requested': True,
        'contact': 'ana@example.com',
        'website': '',
    }
    p.update(cambios)
    return p


class ContratoCliEnLambda(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp(prefix='resenas-contrato-')
        self.addCleanup(shutil.rmtree, self.raiz, ignore_errors=True)
        self.s3 = ArchivosS3(self.raiz)
        self.svc = app.Service(self.s3, PRIVADO, 'https://ork3d.com',
                               google_review_url='https://g.page/r/ejemplo/review')

    def cli(self, *args):
        entorno = {
            **os.environ,
            'AWS_CMD': f'{sys.executable} {SHIM}',
            'SHIM_ROOT': self.raiz,
            'RESENAS_BUCKET': PRIVADO,
            'SITE_BUCKET': SITIO,
            'CF_DISTRIBUTION_ID': 'EJEMPLO123',
        }
        return subprocess.run(['bash', CLI, *args], env=entorno, capture_output=True, text=True)

    def cli_ok(self, *args):
        r = self.cli(*args)
        self.assertEqual(r.returncode, 0, f'resenas.sh {" ".join(args)} falló: {r.stderr}')
        return r.stdout

    def ruta(self, bucket, key):
        return os.path.join(self.raiz, bucket, *key.split('/'))

    def publicas(self):
        with open(self.ruta(SITIO, 'resenas/publicas.json'), encoding='utf-8') as f:
            return json.load(f)

    def crear_token(self):
        salida = self.cli_ok('token', 'crear', '--pedido', 'PED-1', '--producto', 'trofeo-acrilico',
                             '--categoria', 'trofeos', '--producto-label', 'Trofeo acrílico')
        m = re.search(r'/opinar/([A-Za-z0-9_-]{32})', salida)
        self.assertIsNotNone(m, f'el CLI no imprimió un enlace: {salida}')
        return m.group(1)

    def test_token_creado_por_el_cli_lo_valida_la_lambda(self):
        token = self.crear_token()
        status, body = self.svc.get_token(token)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'], body)
        self.assertEqual(body['producto'], 'Trofeo acrílico')
        self.assertEqual(body['categoria'], 'trofeos')

    def test_flujo_completo_publica_solo_lo_aprobado_y_sin_datos_privados(self):
        token = self.crear_token()
        status, body = self.svc.create_review(payload(token))
        self.assertEqual(status, 200, body)
        rid = body['id']
        self.assertTrue(os.path.isfile(self.ruta(PRIVADO, f'resenas/{rid}.json')))

        self.assertIn(rid, self.cli_ok('pendientes'))
        self.cli_ok('aprobar', rid)

        publicas = self.publicas()
        self.assertEqual(publicas['count'], 1)
        item = publicas['items'][0]
        self.assertEqual(item['id'], rid)
        self.assertEqual(item['display_name'], 'Ana G.')
        self.assertEqual(item['product_label'], 'Trofeo acrílico')
        texto = json.dumps(publicas, ensure_ascii=False)
        self.assertNotIn('ana@example.com', texto)
        self.assertNotIn('SECRETO-PRIVADO', texto)

        status, body = self.svc.create_review(payload(token, comment='otra vez'))
        self.assertEqual((status, body.get('motivo')), (409, 'usado'))

    def test_anonimo_se_publica_con_el_nombre_que_promete_el_formulario(self):
        token = self.crear_token()
        status, body = self.svc.create_review(payload(token, display_mode='anonimo', name='',
                                                      contact_requested=False))
        self.assertEqual(status, 200, body)
        self.cli_ok('aprobar', body['id'])
        self.assertEqual(self.publicas()['items'][0]['display_name'], 'Cliente de ORK3D')

    def test_foto_aprobada_se_copia_al_sitio_y_se_enlaza(self):
        token = self.crear_token()
        status, firma = self.svc.presign({'token': token, 'ext': 'jpg'})
        self.assertEqual(status, 200)
        self.s3.put_object(Bucket=PRIVADO, Key=firma['key'], Body=JPEG)  # lo que haría el navegador
        status, body = self.svc.create_review(payload(token, photo_key=firma['key']))
        self.assertEqual(status, 200, body)
        rid = body['id']
        self.cli_ok('aprobar', rid)
        item = self.publicas()['items'][0]
        self.assertEqual(item['photo_url'], f'/resenas/fotos/{rid}.jpg')
        self.assertTrue(os.path.isfile(self.ruta(SITIO, f'resenas/fotos/{rid}.jpg')))

    def test_revocar_desde_el_cli_bloquea_el_enlace(self):
        token = self.crear_token()
        self.cli_ok('token', 'revocar', token)
        self.assertEqual(self.svc.get_token(token)[1]['motivo'], 'revocado')

    def test_ocultar_saca_la_resena_del_sitio_sin_borrarla(self):
        token = self.crear_token()
        status, body = self.svc.create_review(payload(token))
        rid = body['id']
        self.cli_ok('aprobar', rid)
        self.assertEqual(self.publicas()['count'], 1)
        self.cli_ok('ocultar', rid)
        self.assertEqual(self.publicas()['count'], 0)
        self.assertTrue(os.path.isfile(self.ruta(PRIVADO, f'resenas/{rid}.json')))


if __name__ == '__main__':
    unittest.main()
