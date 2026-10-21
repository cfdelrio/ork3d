"""Paridad entre el CLI de moderación (admin/resenas.sh) y la web V2 sobre el MISMO almacén.

Reutiliza el harness de test_contrato (CLI real contra admin/tests/aws_shim.py, que guarda
los objetos como archivos). Se agrega ETag, If-Match y listado para que la API admin corra
sobre ese mismo almacén. Lo que se prueba: lo que escribe una herramienta lo lee la otra,
con los mismos campos, las mismas claves de export y las mismas transiciones.

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
from urllib.parse import parse_qs

import admin_app
import app
import test_contrato as tc
from admin_fixtures import BASE, CABECERAS_CSRF, DESTINO, JPEG, ORIGEN, REMITENTE, Reloj
from fakes import FakeClientError, FakeCloudFront, FakeS3, FakeSES

PRIVADO = tc.PRIVADO
SITIO = tc.SITIO
CAMPOS_TOKEN = {'v', 'token_hash', 'created_at', 'expires_at', 'order_ref', 'product_slug', 'product_label',
                'category', 'verified', 'revoked', 'revoked_at'}
ALLOWLIST = {'id', 'rating', 'comment', 'aspects', 'display_name', 'category', 'product_label', 'verified',
             'photo_url', 'published_at'}
ENLACE_RE = re.compile(r'/opinar/([A-Za-z0-9_-]{32})')


class ArchivosS3V2(tc.ArchivosS3):
    """Shim de archivos con lo que usa la API admin: ETag, If-Match y listado paginado."""

    def get_object(self, Bucket, Key, Range=None, **_):
        respuesta = super().get_object(Bucket=Bucket, Key=Key, Range=Range)
        datos = respuesta['Body'].getvalue()
        respuesta['Body'] = io.BytesIO(datos)
        respuesta['ETag'] = FakeS3.etag(datos)
        return respuesta

    def put_object(self, Bucket, Key, Body=b'', ContentType=None, IfNoneMatch=None, IfMatch=None,
                   CacheControl=None, **_):
        ruta = self._ruta(Bucket, Key)
        if IfMatch is not None:
            if not os.path.isfile(ruta):
                raise FakeClientError('PreconditionFailed', 412, 'If-Match: la clave no existe')
            with open(ruta, 'rb') as f:
                if FakeS3.etag(f.read()) != IfMatch:
                    raise FakeClientError('PreconditionFailed', 412, 'If-Match: el ETag no coincide')
        super().put_object(Bucket=Bucket, Key=Key, Body=Body, ContentType=ContentType, IfNoneMatch=IfNoneMatch)
        with open(ruta, 'rb') as f:
            return {'ETag': FakeS3.etag(f.read())}

    def list_objects_v2(self, Bucket, Prefix='', ContinuationToken=None, **_):
        base = os.path.join(self.raiz, Bucket)
        claves = []
        for dirpath, _, nombres in os.walk(base):
            for nombre in nombres:
                clave = os.path.relpath(os.path.join(dirpath, nombre), base).replace(os.sep, '/')
                if clave.startswith(Prefix or ''):
                    claves.append(clave)
        claves.sort()
        return {'Contents': [{'Key': k} for k in claves], 'KeyCount': len(claves), 'IsTruncated': False}


class Base(unittest.TestCase):
    def setUp(self):
        self.raiz = tempfile.mkdtemp(prefix='resenas-paridad-')
        self.addCleanup(shutil.rmtree, self.raiz, ignore_errors=True)
        self.s3 = ArchivosS3V2(self.raiz)
        self.ses = FakeSES()
        self.cf = FakeCloudFront()
        self.reloj = Reloj()
        self.publica = app.Service(self.s3, PRIVADO, ORIGEN, google_review_url=None, correo=self.ses,
                                   base_url=BASE, origen_correo=REMITENTE, destino_correo=DESTINO)
        previo = app._SVC
        app._SVC = self.publica
        self.addCleanup(setattr, app, '_SVC', previo)
        self.admin_svc = admin_app.ServicioAdmin(
            self.s3, bucket_privado=PRIVADO, bucket_sitio=SITIO, origen_permitido=ORIGEN, base_url=BASE,
            correo_svc=self.ses, correo_origen=REMITENTE, correo_destino=DESTINO, invalidador=self.cf,
            ahora=self.reloj, espera=lambda segundos: None)
        self.cookies = self.abrir_sesion()

    # ── Web ───────────────────────────────────────────────────────────────
    def admin(self, metodo, ruta, cuerpo=None, qs=''):
        cabeceras = dict(CABECERAS_CSRF) if metodo == 'POST' else {}
        datos = json.dumps(cuerpo).encode('utf-8') if cuerpo is not None else b''
        r = self.admin_svc.atender(metodo, ruta, parse_qs(qs), cabeceras, self.cookies, datos)
        return r['status'], r['cuerpo']

    def abrir_sesion(self):
        self.admin_svc.atender('POST', '/admin/api/sesion/solicitar', {}, dict(CABECERAS_CSRF), [], b'')
        codigo = re.search(r'#c=([A-Za-z0-9_-]{43})', self.ses.enviados[-1]['text']).group(1)
        r = self.admin_svc.atender('POST', '/admin/api/sesion/confirmar', {}, dict(CABECERAS_CSRF), [],
                                   json.dumps({'codigo': codigo}).encode('utf-8'))
        return [f"ork3d_admin={r['cookies'][0].split(';')[0].split('=', 1)[1]}"]

    def invitacion_web(self):
        status, cuerpo = self.admin('POST', '/admin/api/invitaciones', {
            'order_ref': tc.PEDIDO, 'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
            'category': 'trofeos', 'dias': 30})
        self.assertEqual(status, 201, cuerpo)
        return ENLACE_RE.search(cuerpo['enlace']).group(1)

    def resena_publica(self, token, con_foto=False):
        clave = None
        if con_foto:
            status, firma = self.publica.presign({'token': token, 'ext': 'jpg'})
            self.assertEqual(status, 200, firma)
            clave = firma['key']
            self.s3.put_object(Bucket=PRIVADO, Key=clave, Body=JPEG)
        status, cuerpo = self.publica.create_review(tc.payload(token, photo_key=clave))
        self.assertEqual(status, 200, cuerpo)
        return cuerpo['id']

    # ── CLI ───────────────────────────────────────────────────────────────
    def cli(self, *args):
        entorno = {**os.environ, 'AWS_CMD': f'{sys.executable} {tc.SHIM}', 'SHIM_ROOT': self.raiz,
                   'RESENAS_BUCKET': PRIVADO, 'SITE_BUCKET': SITIO, 'CF_DISTRIBUTION_ID': 'EJEMPLO123'}
        return subprocess.run(['bash', tc.CLI, *args], env=entorno, capture_output=True, text=True)

    def cli_ok(self, *args):
        r = self.cli(*args)
        self.assertEqual(r.returncode, 0, f'resenas.sh {" ".join(args)} falló: {r.stderr}')
        return r.stdout

    def token_cli(self):
        salida = self.cli_ok('token', 'crear', '--pedido', tc.PEDIDO, '--producto', 'trofeo-acrilico',
                             '--categoria', 'trofeos', '--producto-label', 'Trofeo acrílico')
        return ENLACE_RE.search(salida).group(1)

    # ── Lectura ───────────────────────────────────────────────────────────
    def leer(self, bucket, clave):
        with open(self.s3._ruta(bucket, clave), encoding='utf-8') as f:
            return json.load(f)

    def existe(self, bucket, clave):
        return os.path.isfile(self.s3._ruta(bucket, clave))

    def export(self):
        return self.leer(SITIO, 'resenas/publicas.json')


class TokensParidadTest(Base):
    def test_registro_de_token_tiene_los_mismos_campos_en_cli_y_web(self):
        token_cli, token_web = self.token_cli(), self.invitacion_web()
        reg_cli = self.leer(PRIVADO, f'tokens/{app.token_hash(token_cli)}.json')
        reg_web = self.leer(PRIVADO, f'tokens/{app.token_hash(token_web)}.json')
        self.assertEqual(set(reg_cli), CAMPOS_TOKEN)
        self.assertEqual(set(reg_web), CAMPOS_TOKEN)
        for reg in [reg_cli, reg_web]:
            self.assertEqual((reg['verified'], reg['revoked'], reg['revoked_at']), (False, False, None))
            self.assertEqual(reg['order_ref'], tc.PEDIDO)

    def test_token_de_cada_herramienta_lo_acepta_la_api_publica(self):
        for token in [self.token_cli(), self.invitacion_web()]:
            status, cuerpo = self.publica.get_token(token)
            self.assertEqual((status, cuerpo['ok']), (200, True))
            self.assertEqual(cuerpo['producto'], 'Trofeo acrílico')

    def test_los_dos_enlaces_tienen_el_mismo_formato(self):
        self.assertEqual(len(self.token_cli()), 32)
        self.assertEqual(len(self.invitacion_web()), 32)


class ModeracionParidadTest(Base):
    def test_la_resena_aprobada_por_la_web_la_ve_el_cli_y_no_figura_pendiente(self):
        rid = self.resena_publica(self.token_cli(), con_foto=True)
        self.assertEqual(self.admin('POST', f'/admin/api/resenas/{rid}/aprobar')[0], 200)
        self.cli_ok('ver', rid)
        self.assertEqual(self.leer(PRIVADO, f'resenas/{rid}.json')['status'], 'approved')
        self.assertNotIn(rid, self.cli_ok('pendientes'))

    def test_la_resena_aprobada_por_el_cli_aparece_en_la_bandeja_web(self):
        rid = self.resena_publica(self.token_cli(), con_foto=True)
        self.cli_ok('aprobar', rid)
        status, cuerpo = self.admin('GET', '/admin/api/resenas', qs='estado=approved')
        self.assertEqual(status, 200)
        self.assertEqual([f['id'] for f in cuerpo['resenas']], [rid])
        self.assertEqual(self.admin('GET', f'/admin/api/resenas/{rid}')[1]['resena']['status'], 'approved')

    def test_campos_que_escribe_la_web_al_aprobar_son_los_del_cli(self):
        rid_cli = self.resena_publica(self.token_cli(), con_foto=True)
        rid_web = self.resena_publica(self.token_cli(), con_foto=True)
        pendiente_cli = self.leer(PRIVADO, f'resenas/{rid_cli}.json')
        pendiente_web = self.leer(PRIVADO, f'resenas/{rid_web}.json')
        self.cli_ok('aprobar', rid_cli)
        self.admin('POST', f'/admin/api/resenas/{rid_web}/aprobar')
        despues_cli = self.leer(PRIVADO, f'resenas/{rid_cli}.json')
        despues_web = self.leer(PRIVADO, f'resenas/{rid_web}.json')
        self.assertEqual(set(despues_web), set(pendiente_web))  # la web no agrega claves nuevas
        self.assertEqual(set(despues_cli), set(despues_web))
        cambios_web = {k for k in pendiente_web if pendiente_web[k] != despues_web[k]}
        cambios_cli = {k for k in pendiente_cli if pendiente_cli[k] != despues_cli[k]}
        self.assertEqual(cambios_web, {'status', 'moderated_at', 'published_at', 'photo_public_key'})
        self.assertEqual(cambios_web, cambios_cli)

    def test_export_de_web_y_cli_tienen_las_mismas_claves_y_formato(self):
        rid_cli = self.resena_publica(self.token_cli(), con_foto=True)
        rid_web = self.resena_publica(self.token_cli(), con_foto=True)
        self.cli_ok('aprobar', rid_cli)
        self.admin('POST', f'/admin/api/resenas/{rid_web}/aprobar')
        export_web = self.export()
        self.cli_ok('exportar')
        export_cli = self.export()
        self.assertEqual((export_web['count'], export_cli['count']), (2, 2))
        for export in [export_web, export_cli]:
            self.assertEqual({i['id'] for i in export['items']}, {rid_cli, rid_web})
            for item in export['items']:
                self.assertEqual(set(item), ALLOWLIST)
        for rid in [rid_cli, rid_web]:
            por_web = next(i for i in export_web['items'] if i['id'] == rid)
            por_cli = next(i for i in export_cli['items'] if i['id'] == rid)
            self.assertEqual(por_web['photo_url'], f'/resenas/fotos/{rid}.jpg')
            self.assertEqual(por_web['photo_url'], por_cli['photo_url'])
            self.assertEqual(por_web['display_name'], por_cli['display_name'])

    def test_ocultar_con_cli_y_restaurar_con_web_conserva_published_at_y_foto(self):
        rid = self.resena_publica(self.token_cli(), con_foto=True)
        self.cli_ok('aprobar', rid)
        publicado = self.leer(PRIVADO, f'resenas/{rid}.json')['published_at']
        self.cli_ok('ocultar', rid)
        self.assertFalse(self.existe(SITIO, f'resenas/fotos/{rid}.jpg'))
        self.assertEqual(self.export()['items'], [])
        status, cuerpo = self.admin('POST', f'/admin/api/resenas/{rid}/restaurar')
        self.assertEqual((status, cuerpo['estado']), (200, 'approved'))
        self.assertEqual(self.leer(PRIVADO, f'resenas/{rid}.json')['published_at'], publicado)
        self.assertTrue(self.existe(SITIO, f'resenas/fotos/{rid}.jpg'))
        self.assertEqual([i['id'] for i in self.export()['items']], [rid])

    def test_rechazar_aprobada_con_web_la_saca_del_export_igual_que_el_cli(self):
        rid = self.resena_publica(self.token_cli(), con_foto=True)
        self.admin('POST', f'/admin/api/resenas/{rid}/aprobar')
        status, _ = self.admin('POST', f'/admin/api/resenas/{rid}/rechazar')
        self.assertEqual(status, 200)
        self.assertEqual(self.leer(PRIVADO, f'resenas/{rid}.json')['status'], 'rejected')
        self.assertIsNone(self.leer(PRIVADO, f'resenas/{rid}.json')['photo_public_key'])
        self.assertFalse(self.existe(SITIO, f'resenas/fotos/{rid}.jpg'))
        self.assertEqual(self.export()['items'], [])
        self.cli_ok('ver', rid)

    def test_el_cli_sigue_funcionando_despues_de_moderar_desde_la_web(self):
        rid = self.resena_publica(self.token_cli(), con_foto=True)
        self.admin('POST', f'/admin/api/resenas/{rid}/aprobar')
        self.admin('POST', f'/admin/api/resenas/{rid}/ocultar')
        self.cli_ok('ver', rid)
        self.cli_ok('exportar')
        self.assertEqual(self.export()['items'], [])
        self.cli_ok('restaurar', rid)
        self.assertEqual([i['id'] for i in self.export()['items']], [rid])


if __name__ == '__main__':
    unittest.main()
