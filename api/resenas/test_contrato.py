"""Contrato entre el CLI de moderación (admin/resenas.sh) y la API (app.py).

Corre el CLI real contra el shim de AWS (admin/tests/aws_shim.py, que guarda los
objetos como archivos en un directorio temporal) y la API real por su punto de
entrada, app.handler, sobre esos mismos archivos. Nada del contrato se simula:
si CLI y API no coinciden en formato de IDs, claves o campos, el test falla.

Requiere bash, jq y openssl, igual que el CLI.
"""
import datetime as dt
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
from fakes import FakeClientError, jpeg

RAIZ_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CLI = os.path.join(RAIZ_REPO, 'admin', 'resenas.sh')
SHIM = os.path.join(RAIZ_REPO, 'admin', 'tests', 'aws_shim.py')
PRIVADO = 'bucket-privado-test'
SITIO = 'bucket-sitio-test'
PEDIDO = 'PEDIDO-SECRETO-777'
JPEG = jpeg()
ID_RE = re.compile(r'^[a-f0-9]{32}$')


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


class Base(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp(prefix='resenas-contrato-')
        self.addCleanup(shutil.rmtree, self.raiz, ignore_errors=True)
        self.s3 = ArchivosS3(self.raiz)
        self.svc = app.Service(self.s3, PRIVADO, 'https://ork3d.com',
                               google_review_url='https://g.page/r/ejemplo/review')
        previo = app._SVC
        app._SVC = self.svc
        self.addCleanup(setattr, app, '_SVC', previo)

    # ── CLI ───────────────────────────────────────────────────────────────
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

    def crear_token(self):
        salida = self.cli_ok('token', 'crear', '--pedido', PEDIDO, '--producto', 'trofeo-acrilico',
                             '--categoria', 'trofeos', '--producto-label', 'Trofeo acrílico')
        m = re.search(r'/opinar/([A-Za-z0-9_-]{32})', salida)
        self.assertIsNotNone(m, f'el CLI no imprimió un enlace: {salida}')
        return m.group(1)

    # ── Archivos ──────────────────────────────────────────────────────────
    def ruta(self, bucket, key):
        return os.path.join(self.raiz, bucket, *key.split('/'))

    def leer(self, bucket, key):
        with open(self.ruta(bucket, key), encoding='utf-8') as f:
            return json.load(f)

    def publicas(self):
        if not os.path.isfile(self.ruta(SITIO, 'resenas/publicas.json')):
            return {'count': 0, 'items': []}
        return self.leer(SITIO, 'resenas/publicas.json')

    # ── API (punto de entrada real) ───────────────────────────────────────
    def api(self, metodo, ruta, cuerpo=None):
        evento = {'requestContext': {'http': {'method': metodo}}, 'rawPath': ruta}
        if cuerpo is not None:
            evento['body'] = json.dumps(cuerpo, ensure_ascii=False)
        r = app.handler(evento)
        return r['statusCode'], (json.loads(r['body']) if r['body'] else None)

    def enviar_resena(self, token, con_foto=False, **cambios):
        clave = None
        if con_foto:
            status, firma = self.api('POST', '/presign', {'token': token, 'ext': 'jpg'})
            self.assertEqual(status, 200, firma)
            clave = firma['key']
            self.s3.put_object(Bucket=PRIVADO, Key=clave, Body=JPEG)  # lo que haría el navegador
        status, body = self.api('POST', '/resenas', payload(token, photo_key=clave, **cambios))
        self.assertEqual(status, 200, body)
        self.assertRegex(body['id'], ID_RE, 'la Lambda debe generar IDs de 32 caracteres hex')
        return body['id'], clave

    def resena_pendiente(self, **cambios):
        token = self.crear_token()
        rid, _ = self.enviar_resena(token, **cambios)
        return rid

    def assertSinDatosPrivados(self, publica, token, clave_foto=None):
        texto = json.dumps(publica, ensure_ascii=False)
        prohibidos = ['ana@example.com', 'SECRETO-PRIVADO', PEDIDO, 'Gómez', token,
                      app.token_hash(token), 'pendientes/', 'order_ref', 'contact', 'private_feedback']
        if clave_foto:
            prohibidos.append(clave_foto)
        for secreto in prohibidos:
            self.assertNotIn(secreto, texto, f'el export público expone {secreto!r}')


class FlujoCompleto(Base):
    def test_cli_crea_token_lambda_recibe_resena_cli_aprueba_export_publico(self):
        token = self.crear_token()                                      # CLI genera el enlace
        status, body = self.api('GET', f'/tokens/{token}')              # Lambda acepta el token
        self.assertEqual((status, body['ok']), (200, True))
        self.assertEqual(body['producto'], 'Trofeo acrílico')

        rid, clave = self.enviar_resena(token, con_foto=True)           # el cliente envía con foto

        self.assertIn(rid, self.cli_ok('pendientes'))                   # CLI ve la reseña pendiente
        self.cli_ok('ver', rid)
        self.cli_ok('aprobar', rid)                                     # CLI aprueba

        publica = self.publicas()                                       # export público
        self.assertEqual(publica['count'], 1)
        item = publica['items'][0]
        self.assertEqual(item['id'], rid)
        self.assertEqual(item['display_name'], 'Ana G.')
        self.assertEqual(item['photo_url'], f'/resenas/fotos/{rid}.jpg')
        self.assertTrue(os.path.isfile(self.ruta(SITIO, f'resenas/fotos/{rid}.jpg')))
        self.assertSinDatosPrivados(publica, token, clave)

        privada = self.leer(PRIVADO, f'resenas/{rid}.json')             # la copia privada sí conserva todo
        self.assertEqual(privada['contact'], 'ana@example.com')
        self.assertEqual(privada['status'], 'approved')

        status, body = self.api('POST', '/resenas', payload(token, comment='otra vez'))
        self.assertEqual((status, body['motivo']), (409, 'usado'))      # el enlace ya no sirve


class OperacionesCli(Base):
    def test_listar_y_revocar_tokens(self):
        token = self.crear_token()
        listado = self.cli_ok('token', 'listar')
        self.assertIn('trofeo-acrilico', listado)
        self.assertNotIn(token, listado, 'listar no debe imprimir el token')
        self.cli_ok('token', 'revocar', token)
        self.assertEqual(self.api('GET', f'/tokens/{token}')[1]['motivo'], 'revocado')

    def test_ver_muestra_la_resena_con_id_de_32(self):
        rid = self.resena_pendiente()
        self.assertIn(rid, self.cli_ok('ver', rid))

    def test_rechazar_no_publica_y_queda_registrado(self):
        rid = self.resena_pendiente()
        self.cli_ok('rechazar', rid)
        self.assertEqual(self.leer(PRIVADO, f'resenas/{rid}.json')['status'], 'rejected')
        self.assertNotIn(rid, json.dumps(self.publicas()))

    def test_ocultar_y_restaurar_con_export_consistente(self):
        rid = self.resena_pendiente()
        self.cli_ok('aprobar', rid)
        self.assertEqual(self.publicas()['count'], 1)
        self.cli_ok('ocultar', rid)
        self.assertEqual(self.publicas()['count'], 0)
        self.assertTrue(os.path.isfile(self.ruta(PRIVADO, f'resenas/{rid}.json')))
        self.cli_ok('restaurar', rid)
        self.assertEqual(self.publicas()['count'], 1)

    def test_verificar_reseña_aprobada(self):
        rid = self.resena_pendiente()
        self.cli_ok('aprobar', rid)
        self.cli_ok('verificar', rid)

    def test_exportar_regenera_el_archivo_publico(self):
        rid = self.resena_pendiente()
        self.cli_ok('aprobar', rid)
        os.remove(self.ruta(SITIO, 'resenas/publicas.json'))
        self.cli_ok('exportar')
        self.assertEqual(self.publicas()['items'][0]['id'], rid)

    def test_borrar_solo_foto_conserva_el_texto(self):
        # Según el CLI: `borrar ID --foto` quita las fotos (privada y pública) y deja la reseña.
        token = self.crear_token()
        rid, clave = self.enviar_resena(token, con_foto=True)
        self.cli_ok('aprobar', rid)
        self.assertTrue(os.path.isfile(self.ruta(SITIO, f'resenas/fotos/{rid}.jpg')))
        self.cli_ok('borrar', rid, '--foto', '--yes')
        self.assertFalse(os.path.isfile(self.ruta(SITIO, f'resenas/fotos/{rid}.jpg')))
        self.assertFalse(os.path.isfile(self.ruta(PRIVADO, clave)))
        privada = self.leer(PRIVADO, f'resenas/{rid}.json')
        self.assertIsNone(privada['photo_key'])
        self.assertIsNone(privada['photo_public_key'])
        self.assertIsNone(self.publicas()['items'][0]['photo_url'])

    def test_borrar_completa_quita_texto_y_fotos(self):
        token = self.crear_token()
        rid, clave = self.enviar_resena(token, con_foto=True)
        self.cli_ok('aprobar', rid)
        self.cli_ok('borrar', rid, '--yes')
        self.assertFalse(os.path.isfile(self.ruta(PRIVADO, f'resenas/{rid}.json')))
        self.assertFalse(os.path.isfile(self.ruta(PRIVADO, clave)))
        self.assertFalse(os.path.isfile(self.ruta(SITIO, f'resenas/fotos/{rid}.jpg')))
        self.assertEqual(self.publicas()['count'], 0)

    def test_mantenimiento_solo_borra_rechazadas_de_mas_de_90_dias(self):
        vieja = self.resena_pendiente()
        self.cli_ok('rechazar', vieja)
        nueva = self.resena_pendiente()
        self.cli_ok('rechazar', nueva)
        oculta = self.resena_pendiente()
        self.cli_ok('aprobar', oculta)
        self.cli_ok('ocultar', oculta)
        hace_120 = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=120)).strftime('%Y-%m-%dT%H:%M:%SZ')
        for rid in (vieja, oculta):
            ruta = self.ruta(PRIVADO, f'resenas/{rid}.json')
            datos = self.leer(PRIVADO, f'resenas/{rid}.json')
            datos['created_at'] = hace_120
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(datos, f)
        self.assertIn(vieja, self.cli_ok('mantenimiento', '--dry-run'))
        self.cli_ok('mantenimiento', '--yes')
        self.assertFalse(os.path.isfile(self.ruta(PRIVADO, f'resenas/{vieja}.json')))
        self.assertTrue(os.path.isfile(self.ruta(PRIVADO, f'resenas/{nueva}.json')))
        self.assertTrue(os.path.isfile(self.ruta(PRIVADO, f'resenas/{oculta}.json')))

    def test_backup_copia_el_bucket_privado(self):
        self.crear_token()
        destino = tempfile.mkdtemp(prefix='resenas-backup-')
        self.addCleanup(shutil.rmtree, destino, ignore_errors=True)
        self.cli_ok('backup', destino)
        copias = [f for _, _, archivos in os.walk(destino) for f in archivos]
        self.assertTrue(any(f.endswith('.json') for f in copias), copias)

    def test_ids_que_no_son_de_32_se_rechazan(self):
        r = self.cli('aprobar', 'a' * 24)
        self.assertNotEqual(r.returncode, 0)


if __name__ == '__main__':
    unittest.main()
