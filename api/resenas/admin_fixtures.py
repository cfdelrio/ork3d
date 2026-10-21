"""Utilidades compartidas por los tests de la API admin (V2). No contiene tests.

Un ServicioAdmin real sobre FakeS3, FakeSES y FakeCloudFront, con reloj inyectado. Cada
petición pasa por admin_app.evento_a_entrada y admin_app.a_respuesta_http, igual que en
la Lambda, así que los tests también cubren el adaptador.
"""
import datetime as dt
import hashlib
import json
import re
import unittest
import uuid

import admin_app
from fakes import FakeCloudFront, FakeS3, FakeSES, jpeg

ORIGEN = 'https://ork3d.com'
BASE = 'https://ork3d.com'
DESTINO = 'moderacion@ejemplo.invalid'
REMITENTE = 'avisos@ejemplo.invalid'
PRIVADO = 'bucket-privado-test'
SITIO = 'bucket-sitio-test'
PEDIDO = 'PEDIDO-SECRETO-777'
CONTACTO = 'ana@example.com'
FEEDBACK = 'Dato interno de moderación'
CABECERAS_CSRF = {'origin': ORIGEN, 'content-type': 'application/json', 'x-admin-request': '1'}
JPEG = jpeg()
INICIO = dt.datetime(2026, 10, 21, 12, 0, 0, tzinfo=dt.timezone.utc)
CAMPOS_TOKEN = {'v', 'token_hash', 'created_at', 'expires_at', 'order_ref', 'product_slug',
                'product_label', 'category', 'verified', 'revoked', 'revoked_at'}
INVITACION = {'order_ref': PEDIDO, 'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
              'category': 'trofeos', 'dias': 30}


def iso(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def datos(respuesta):
    """Cuerpo JSON de una respuesta de la Lambda (None si no tiene cuerpo)."""
    if respuesta['isBase64Encoded'] or not respuesta['body']:
        return None
    return json.loads(respuesta['body'])


class Reloj:
    """Reloj inyectable: llamarlo devuelve el instante actual; avanzar() lo mueve."""

    def __init__(self, inicio=INICIO):
        self.t = inicio

    def __call__(self):
        return self.t

    def avanzar(self, **kwargs):
        self.t += dt.timedelta(**kwargs)


def sesion_de(cookies):
    """Secreto de la cookie de sesión, con o sin atributos (Set-Cookie). Para comprobar que nunca se guarda en claro."""
    return cookies[0].split(';')[0].split('=', 1)[1]


class AdminCase(unittest.TestCase):
    """Servicio admin nuevo en cada test, con S3, correo y CloudFront falsos."""

    def setUp(self):
        self.s3 = FakeS3()
        self.ses = FakeSES()
        self.cf = FakeCloudFront()
        self.reloj = Reloj()
        self.svc = self.servicio(self.s3)

    def servicio(self, s3, espera=lambda segundos: None):
        return admin_app.ServicioAdmin(
            s3, bucket_privado=PRIVADO, bucket_sitio=SITIO, origen_permitido=ORIGEN,
            base_url=BASE, correo_svc=self.ses, correo_origen=REMITENTE, correo_destino=DESTINO,
            invalidador=self.cf, ahora=self.reloj, espera=espera)

    def pedir(self, metodo, ruta, cuerpo=None, *, cookies=None, cabeceras=None, qs=''):
        """Petición completa por el adaptador. Las mutaciones llevan cabeceras CSRF válidas por defecto."""
        if cabeceras is None:
            cabeceras = dict(CABECERAS_CSRF) if metodo == 'POST' else {}
        evento = {'requestContext': {'http': {'method': metodo}}, 'rawPath': ruta,
                  'rawQueryString': qs, 'headers': cabeceras, 'cookies': cookies or []}
        if cuerpo is not None:
            evento['body'] = cuerpo if isinstance(cuerpo, str) else json.dumps(cuerpo, ensure_ascii=False)
        return admin_app.a_respuesta_http(self.svc.atender(*admin_app.evento_a_entrada(evento)))

    def ultimo_codigo(self):
        """Código de acceso que llegó en el último correo de login (el fragmento del enlace)."""
        m = re.search(r'#c=([A-Za-z0-9_-]{43})', self.ses.enviados[-1]['text'])
        self.assertIsNotNone(m, 'el correo de acceso no trae el código en el fragmento')
        return m.group(1)

    def login(self):
        """Solicita acceso, toma el código del correo y confirma. Devuelve la lista de cookies."""
        r = self.pedir('POST', '/admin/api/sesion/solicitar', {})
        self.assertEqual(r['statusCode'], 202)
        r = self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': self.ultimo_codigo()})
        self.assertEqual(r['statusCode'], 200, r['body'])
        valor = r['cookies'][0].split(';')[0].split('=', 1)[1]
        return [f'ork3d_admin={valor}']

    def sembrar_resena(self, rid=None, *, status='pending', rating=5,
                       created_at='2026-10-20T10:00:00Z', published_at=None, permiso=True,
                       foto=False, aviso=False, **cambios):
        """Escribe una reseña como la deja la Lambda pública (mismos campos que V1), con su token."""
        rid = rid or uuid.uuid4().hex
        th = hashlib.sha256(f'token-de-{rid}'.encode('utf-8')).hexdigest()
        self.s3.objects.setdefault(f'tokens/{th}.json', json.dumps({
            'v': 1, 'token_hash': th, 'created_at': created_at, 'expires_at': '2027-01-01T00:00:00Z',
            'order_ref': PEDIDO, 'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
            'category': 'trofeos', 'verified': True, 'revoked': False, 'revoked_at': None,
        }).encode('utf-8'))
        resena = {
            'v': 1, 'id': rid, 'token_hash': th, 'status': status, 'created_at': created_at,
            'moderated_at': None, 'published_at': published_at, 'rating': rating,
            'comment': 'Muy buena compra.', 'aspects': ['calidad'], 'display_mode': 'nombre',
            'display_name': 'Ana G.', 'permission_to_publish': permiso,
            'photo_key': f'pendientes/{th}/{uuid.uuid4().hex}.jpg' if foto else None,
            'photo_public_key': None, 'private_feedback': FEEDBACK,
            'contact_requested': True, 'contact': CONTACTO,
            'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
            'category': 'trofeos', 'verified': True,
            **cambios,
        }
        self.s3.objects[f'resenas/{rid}.json'] = json.dumps(resena, ensure_ascii=False).encode('utf-8')
        if foto:
            self.s3.objects[resena['photo_key']] = JPEG
        if aviso:
            self.s3.objects[f'notificaciones/{rid}.json'] = json.dumps(
                {'v': 1, 'enviado': iso(INICIO)}).encode('utf-8')
        return rid

    def resena(self, rid):
        return json.loads(self.s3.objects[f'resenas/{rid}.json'])

    def invitacion(self, cookies, **cambios):
        return self.pedir('POST', '/admin/api/invitaciones', {**INVITACION, **cambios}, cookies=cookies)


class AdminSesionCase(AdminCase):
    """Igual que AdminCase, con una sesión abierta en self.cookies. El buzón arranca vacío."""

    def setUp(self):
        super().setUp()
        self.cookies = self.login()
        self.ses.enviados.clear()  # el correo de acceso del login no es parte del test
