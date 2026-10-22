"""Routing real de la API admin con eventos de Lambda Function URL payload v2.

Lambda entrega el path completo en rawPath y en requestContext.http.path (no reescribe /admin/api). Estas pruebas
construyen el evento con esa forma y lo pasan por el mismo adaptador que usa handler():
evento_a_entrada -> ServicioAdmin.atender -> a_respuesta_http. Sin AWS: SES y S3 son dobles.

Lo que se demuestra:
- GET /admin/api/sesion sin cookie llega a la app y responde 401 con el formato de la app.
- POST /admin/api/sesion/solicitar sin Origin llega a _solicitar y _csrf responde 400 antes de tocar SES.
- POST válido llega a _solicitar y envía exactamente un correo (FakeSES).
- Rutas desconocidas responden 404 de la app (con error no_encontrado) y nunca envían correo.
"""
import hashlib

from admin_app import a_respuesta_http, evento_a_entrada
from admin_fixtures import CABECERAS_CSRF, DESTINO, AdminCase

HASH_CUERPO_VACIO = hashlib.sha256(b'{}').hexdigest()
CABECERAS_SIN_ORIGEN = {'content-type': 'application/json', 'x-admin-request': '1',
                        'x-amz-content-sha256': HASH_CUERPO_VACIO}
CABECERAS_VALIDAS = dict(CABECERAS_CSRF, **{'x-amz-content-sha256': HASH_CUERPO_VACIO})


def evento_v2(metodo, ruta, *, cabeceras=None, cuerpo=None, cookies=None, qs=''):
    """Evento de Lambda Function URL, payload format 2.0 (forma de AWS, no del adaptador)."""
    evento = {
        'version': '2.0',
        'routeKey': '$default',
        'rawPath': ruta,
        'rawQueryString': qs,
        'cookies': cookies or [],
        'headers': cabeceras or {},
        'requestContext': {
            'accountId': 'anonymous',
            'apiId': 'fixture',
            'domainName': 'fixture.lambda-url.us-east-1.on.aws',
            'domainPrefix': 'fixture',
            'http': {'method': metodo, 'path': ruta, 'protocol': 'HTTP/1.1',
                     'sourceIp': '203.0.113.10', 'userAgent': 'fixture'},
            'requestId': 'fixture-request',
            'routeKey': '$default',
            'stage': '$default',
            'time': '01/Jan/2026:00:00:00 +0000',
            'timeEpoch': 1767225600000,
        },
        'isBase64Encoded': False,
    }
    if cuerpo is not None:
        evento['body'] = cuerpo
    return evento


class RoutingV2Test(AdminCase):
    def llamar(self, evento):
        return a_respuesta_http(self.svc.atender(*evento_a_entrada(evento)))

    def solicitudes_de_correo(self):
        return len(self.ses.enviados)

    def test_get_sesion_sin_cookie_llega_a_la_app_y_da_401(self):
        r = self.llamar(evento_v2('GET', '/admin/api/sesion'))
        self.assertEqual(r['statusCode'], 401)
        self.assertIn('no_autenticado', r['body'])
        self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_post_solicitar_sin_origin_llega_a_csrf_y_da_400_sin_correo(self):
        r = self.llamar(evento_v2('POST', '/admin/api/sesion/solicitar',
                                  cabeceras=CABECERAS_SIN_ORIGEN, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 400)
        self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_post_solicitar_valido_llega_a_solicitar_y_envia_un_correo(self):
        r = self.llamar(evento_v2('POST', '/admin/api/sesion/solicitar',
                                  cabeceras=CABECERAS_VALIDAS, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 202)
        self.assertEqual(self.solicitudes_de_correo(), 1)
        self.assertEqual(self.ses.enviados[0]['to'], DESTINO)

    def test_post_solicitar_valido_con_query_string_no_cambia_la_ruta(self):
        r = self.llamar(evento_v2('POST', '/admin/api/sesion/solicitar', cabeceras=CABECERAS_VALIDAS,
                                  cuerpo='{}', qs='x=1'))
        self.assertEqual(r['statusCode'], 202)
        self.assertEqual(self.solicitudes_de_correo(), 1)

    def test_ruta_extra_da_404_de_la_app_y_nunca_envia(self):
        for cabeceras in (CABECERAS_SIN_ORIGEN, CABECERAS_VALIDAS):
            with self.subTest(origen='con' if cabeceras is CABECERAS_VALIDAS else 'sin'):
                r = self.llamar(evento_v2('POST', '/admin/api/sesion/solicitar/extra',
                                          cabeceras=cabeceras, cuerpo='{}'))
                self.assertEqual(r['statusCode'], 404)
                self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_prefijo_parecido_da_404_y_nunca_envia(self):
        r = self.llamar(evento_v2('POST', '/admin/api2/sesion/solicitar', cabeceras=CABECERAS_VALIDAS, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 404)
        self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_metodo_get_sobre_solicitar_da_405_y_nunca_envia(self):
        r = self.llamar(evento_v2('GET', '/admin/api/sesion/solicitar', cabeceras=CABECERAS_VALIDAS))
        self.assertEqual(r['statusCode'], 405)
        self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_post_ruta_desconocida_da_404_y_nunca_envia(self):
        r = self.llamar(evento_v2('POST', '/admin/api/no-existe', cabeceras=CABECERAS_VALIDAS, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 404)
        self.assertEqual(self.solicitudes_de_correo(), 0)

    def test_404_de_la_app_tiene_formato_reconocible(self):
        # Un 404 de la app trae JSON con ok=false y error=no_encontrado. Un 404 de CloudFront o de la Function URL
        # no tiene ese formato: así se distingue en el smoke.
        r = self.llamar(evento_v2('POST', '/admin/api/no-existe', cabeceras=CABECERAS_VALIDAS, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 404)
        self.assertIn('"ok": false', r['body'])
        self.assertIn('"error": "no_encontrado"', r['body'])

    def test_400_csrf_tiene_formato_de_la_app(self):
        r = self.llamar(evento_v2('POST', '/admin/api/sesion/solicitar', cabeceras=CABECERAS_SIN_ORIGEN, cuerpo='{}'))
        self.assertEqual(r['statusCode'], 400)
        self.assertIn('"ok": false', r['body'])

    def test_rawpath_y_requestcontext_coinciden_en_el_evento_de_aws(self):
        evento = evento_v2('POST', '/admin/api/sesion/solicitar', cabeceras=CABECERAS_VALIDAS, cuerpo='{}')
        self.assertEqual(evento['rawPath'], evento['requestContext']['http']['path'])
        metodo, ruta, _, _, _, _ = evento_a_entrada(evento)
        self.assertEqual((metodo, ruta), ('POST', '/admin/api/sesion/solicitar'))
