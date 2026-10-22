"""Tests de autenticación de la API admin (V2): magic link, código de un solo uso, sesión,
cookie, CSRF, rate limit y ausencia de efectos en los GET.

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import re
import unittest

from admin_fixtures import (BASE, CABECERAS_CSRF, DESTINO, INVITACION, ORIGEN, AdminCase,
                            AdminSesionCase, datos, sesion_de)
from fakes import FakeClientError, FakeS3
from nucleo import sesion as _sesion

ERROR_CODIGO = {'ok': False, 'error': 'codigo_invalido', 'mensaje': 'El enlace no es válido o venció.'}
ORIGEN_EXTRANO = 'https://evil.example'
VALIDO = object()  # marca: usar el código que llegó por correo en este test


class S3ConLimiteRoto(FakeS3):
    """El conteo del rate limit nunca se puede escribir: conflicto persistente."""

    def put_object(self, Bucket, Key, **kwargs):
        if Key == 'admin/limites/acceso.json':
            raise FakeClientError('PreconditionFailed', 412, 'conflicto persistente')
        return super().put_object(Bucket=Bucket, Key=Key, **kwargs)


class SolicitarTest(AdminCase):
    def solicitar(self, cuerpo=None, **kwargs):
        return self.pedir('POST', '/admin/api/sesion/solicitar', cuerpo, **kwargs)

    def test_responde_202_uniforme_y_envia_al_destinatario_fijo(self):
        r = self.solicitar({})
        self.assertEqual(r['statusCode'], 202)
        self.assertEqual(datos(r), {'ok': True})
        self.assertEqual(len(self.ses.enviados), 1)
        self.assertEqual(self.ses.enviados[0]['to'], DESTINO)
        self.assertEqual(self.ses.enviados[0]['subject'], 'Acceso al panel de reseñas ORK3D')

    def test_cuerpo_vacio_tambien_es_valido(self):
        self.assertEqual(self.solicitar(None)['statusCode'], 202)

    def test_el_navegador_no_puede_elegir_destinatario(self):
        for cuerpo in [{'email': 'otro@ejemplo.invalid'}, {'destinatario': 'x@ejemplo.invalid'}]:
            with self.subTest(cuerpo=cuerpo):
                r = self.solicitar(cuerpo)
                self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'validacion'))
        self.assertEqual(self.ses.enviados, [])

    def test_enlace_lleva_el_codigo_en_el_fragmento(self):
        self.solicitar({})
        enlace = re.search(r'(https://\S+)', self.ses.enviados[0]['text']).group(1)
        self.assertTrue(enlace.startswith(f'{BASE}/admin/entrar#c='))
        self.assertNotIn('?', enlace)
        self.assertRegex(enlace, r'#c=[A-Za-z0-9_-]{43}$')

    def test_codigo_tiene_32_bytes_de_entropia(self):
        self.solicitar({})
        self.assertEqual(len(self.ultimo_codigo()), 43)  # token_urlsafe(32)

    def test_codigo_se_guarda_solo_como_hash(self):
        self.solicitar({})
        codigo = self.ultimo_codigo()
        claves = [k for k in self.s3.objects if k.startswith('admin/logins/')]
        self.assertEqual(claves, [f'admin/logins/{_sesion.hash_secreto(codigo)}.json'])
        for clave, contenido in self.s3.objects.items():
            self.assertNotIn(codigo, clave)
            self.assertNotIn(codigo, contenido.decode('utf-8'))

    def test_maximo_tres_solicitudes_cada_quince_minutos(self):
        estados = [self.solicitar({})['statusCode'] for _ in range(4)]
        self.assertEqual(estados, [202, 202, 202, 202])  # la respuesta es igual para todas
        self.assertEqual(len(self.ses.enviados), 3)       # la cuarta no envía nada

    def test_la_ventana_se_reinicia_despues_de_quince_minutos(self):
        for _ in range(3):
            self.solicitar({})
        self.reloj.avanzar(minutes=15)
        self.solicitar({})
        self.assertEqual(len(self.ses.enviados), 4)

    def test_limite_falla_cerrado_si_no_se_puede_registrar_el_conteo(self):
        self.svc = self.servicio(S3ConLimiteRoto())
        r = self.solicitar({})
        self.assertEqual(r['statusCode'], 202)
        self.assertEqual(self.ses.enviados, [])

    def test_fallo_de_ses_responde_igual_y_no_revela_el_error(self):
        self.ses.fallar_proximo()
        r = self.solicitar({})
        self.assertEqual((r['statusCode'], datos(r)), (202, {'ok': True}))
        self.assertEqual(self.ses.enviados, [])

    def test_sin_csrf_responde_400_y_no_cuenta_ni_envia(self):
        r = self.solicitar({}, cabeceras={'content-type': 'application/json'})
        self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'origen'))
        self.assertEqual(self.ses.enviados, [])
        self.assertNotIn('admin/limites/acceso.json', self.s3.objects)


class ConfirmarTest(AdminCase):
    def setUp(self):
        super().setUp()
        self.solicitar_acceso()
        self.codigo = self.ultimo_codigo()

    def solicitar_acceso(self):
        self.assertEqual(self.pedir('POST', '/admin/api/sesion/solicitar', {})['statusCode'], 202)

    def confirmar(self, codigo=VALIDO, **kwargs):
        valor = self.codigo if codigo is VALIDO else codigo
        return self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': valor}, **kwargs)

    def test_codigo_valido_abre_sesion_con_cookie_segura(self):
        r = self.confirmar()
        self.assertEqual(r['statusCode'], 200)
        (cookie,) = r['cookies']
        self.assertRegex(cookie, r'^ork3d_admin=[A-Za-z0-9_-]{43}; HttpOnly; Secure; SameSite=Lax; '
                                 r'Path=/admin; Max-Age=28800$')

    def test_sesion_se_guarda_solo_como_hash(self):
        r = self.confirmar()
        secreto = sesion_de([r['cookies'][0]])
        self.assertIn(f'admin/sesiones/{_sesion.hash_secreto(secreto)}.json', self.s3.objects)
        for clave, contenido in self.s3.objects.items():
            self.assertNotIn(secreto, clave)
            self.assertNotIn(secreto, contenido.decode('utf-8'))

    def test_codigo_es_de_un_solo_uso(self):
        self.assertEqual(self.confirmar()['statusCode'], 200)
        r = self.confirmar()
        self.assertEqual((r['statusCode'], datos(r)), (401, ERROR_CODIGO))

    def test_codigo_sigue_valido_a_los_nueve_minutos_y_medio(self):
        self.reloj.avanzar(minutes=9, seconds=30)
        self.assertEqual(self.confirmar()['statusCode'], 200)

    def test_codigo_vence_a_los_diez_minutos(self):
        self.reloj.avanzar(minutes=10)
        r = self.confirmar()
        self.assertEqual((r['statusCode'], datos(r)), (401, ERROR_CODIGO))

    def test_codigos_que_no_sirven_responden_todos_igual(self):
        respuestas = []
        for codigo in ['x' * 43, 'corto', None]:
            r = self.confirmar(codigo)
            respuestas.append((r['statusCode'], datos(r)))
        self.assertEqual(self.confirmar()['statusCode'], 200)  # el válido, usado ahora
        r = self.confirmar()
        respuestas.append((r['statusCode'], datos(r)))
        self.assertEqual(respuestas, [(401, ERROR_CODIGO)] * 4)

    def test_get_al_endpoint_de_confirmacion_no_consume_el_codigo(self):
        # Un escáner de correo hace GET al enlace: el endpoint solo admite POST.
        self.assertEqual(self.pedir('GET', '/admin/api/sesion/confirmar')['statusCode'], 405)
        self.assertEqual(self.confirmar()['statusCode'], 200)

    def test_confirmar_sin_csrf_responde_400_y_no_consume(self):
        r = self.confirmar(cabeceras={'content-type': 'application/json'})
        self.assertEqual(r['statusCode'], 400)
        self.assertEqual(self.confirmar()['statusCode'], 200)

    def test_confirmar_sin_cuerpo_responde_401_sin_detalle(self):
        r = self.pedir('POST', '/admin/api/sesion/confirmar', {})
        self.assertEqual((r['statusCode'], datos(r)), (401, ERROR_CODIGO))


class SesionTest(AdminSesionCase):
    def test_sesion_vence_a_las_ocho_horas_desde_su_creacion(self):
        self.reloj.avanzar(hours=7, minutes=59)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 200)
        self.reloj.avanzar(minutes=1)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 401)

    def test_sin_cookie_o_con_cookie_falsa_responde_401(self):
        self.assertEqual(self.pedir('GET', '/admin/api/sesion')['statusCode'], 401)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=['ork3d_admin=falsa'])['statusCode'], 401)

    def test_cookie_puede_venir_en_el_header_cookie(self):
        r = self.pedir('GET', '/admin/api/sesion', cabeceras={'cookie': f'otra=1; {self.cookies[0]}'})
        self.assertEqual(r['statusCode'], 200)

    def test_cerrar_invalida_la_sesion_y_expira_la_cookie(self):
        r = self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        self.assertEqual(r['statusCode'], 200)
        (cookie,) = r['cookies']
        self.assertIn('Max-Age=0', cookie)
        self.assertIn('HttpOnly; Secure; SameSite=Lax; Path=/admin', cookie)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 401)

    def test_cerrar_sin_csrf_no_cierra_la_sesion(self):
        r = self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies,
                       cabeceras={'origin': ORIGEN_EXTRANO, 'content-type': 'application/json'})
        self.assertEqual(r['statusCode'], 400)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 200)


class CsrfTest(AdminSesionCase):
    def invitar(self, cabeceras=None, cookies=None):
        return self.pedir('POST', '/admin/api/invitaciones', INVITACION,
                          cookies=self.cookies if cookies is None else cookies, cabeceras=cabeceras)

    def test_mutacion_sin_sesion_responde_401(self):
        self.assertEqual(self.invitar(cookies=[])['statusCode'], 401)

    def test_origen_ausente_o_distinto_responde_400(self):
        for origen in [None, ORIGEN_EXTRANO, 'https://ork3d.com.evil.example', 'http://ork3d.com', 'https://ORK3D.com', ORIGEN + '/']:
            with self.subTest(origen=origen):
                cabeceras = {k: v for k, v in CABECERAS_CSRF.items() if k != 'origin'}
                if origen is not None:
                    cabeceras['origin'] = origen
                r = self.invitar(cabeceras)
                self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'origen'))

    def test_content_type_distinto_de_json_responde_400(self):
        for tipo in ['text/plain', 'application/x-www-form-urlencoded', 'multipart/form-data']:
            with self.subTest(tipo=tipo):
                r = self.invitar({**CABECERAS_CSRF, 'content-type': tipo})
                self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'content_type'))

    def test_content_type_json_con_charset_se_acepta(self):
        r = self.invitar({**CABECERAS_CSRF, 'content-type': 'application/json; charset=utf-8'})
        self.assertEqual(r['statusCode'], 201)

    def test_falta_o_es_incorrecta_la_cabecera_x_admin_request_responde_400(self):
        sin = {k: v for k, v in CABECERAS_CSRF.items() if k != 'x-admin-request'}
        for cabeceras in [sin, {**CABECERAS_CSRF, 'x-admin-request': '0'}]:
            with self.subTest(cabeceras=cabeceras):
                r = self.invitar(cabeceras)
                self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'csrf'))

    def test_csrf_fallido_no_toca_el_almacen(self):
        antes = dict(self.s3.objects)
        self.invitar({**CABECERAS_CSRF, 'origin': ORIGEN_EXTRANO})
        self.assertEqual(self.s3.objects, antes)

    def test_metodo_incorrecto_responde_405(self):
        self.assertEqual(self.pedir('GET', '/admin/api/sesion/solicitar')['statusCode'], 405)
        self.assertEqual(self.pedir('GET', '/admin/api/invitaciones', cookies=self.cookies)['statusCode'], 405)
        rid = self.sembrar_resena()
        self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}/aprobar', cookies=self.cookies)['statusCode'], 405)

    def test_ruta_inexistente_responde_404(self):
        self.assertEqual(self.pedir('GET', '/admin/api/no-existe', cookies=self.cookies)['statusCode'], 404)
        self.assertEqual(self.pedir('GET', '/otra/ruta', cookies=self.cookies)['statusCode'], 404)


class SinEfectosEnLecturaTest(AdminSesionCase):
    def test_los_gets_no_escriben_ni_borran_nada(self):
        rid = self.sembrar_resena(status='approved', published_at='2026-10-19T09:00:00Z', foto=True)
        self.s3.objects[f'resenas/fotos/{rid}.jpg'] = b'x'
        escrituras, borrados, correos = len(self.s3.escrituras), len(self.s3.borrados), len(self.ses.enviados)
        antes = dict(self.s3.objects)
        lecturas = [
            ('/admin/api/sesion', ''),
            ('/admin/api/resenas', 'estado=pending'),
            ('/admin/api/resenas', ''),
            (f'/admin/api/resenas/{rid}', ''),
            (f'/admin/api/resenas/{rid}/foto', ''),
        ]
        for ruta, qs in lecturas:
            with self.subTest(ruta=ruta, qs=qs):
                self.assertEqual(self.pedir('GET', ruta, cookies=self.cookies, qs=qs)['statusCode'], 200)
        self.assertEqual(self.s3.objects, antes)
        self.assertEqual(self.s3.escrituras[escrituras:], [])
        self.assertEqual(self.s3.borrados[borrados:], [])
        self.assertEqual(len(self.ses.enviados), correos)

    def test_lectura_de_sesion_vencida_no_borra_la_sesion(self):
        secreto = sesion_de(self.cookies)
        self.reloj.avanzar(hours=9)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 401)
        self.assertIn(f'admin/sesiones/{_sesion.hash_secreto(secreto)}.json', self.s3.objects)


class RegistrosCorruptosTest(AdminCase):
    """Un registro de sesión o de login que no se puede leer no es válido: la respuesta es 401, nunca 500."""

    def test_sesion_sin_vencimiento_responde_401(self):
        secreto = 'A' * 43
        self.s3.objects[f'admin/sesiones/{_sesion.hash_secreto(secreto)}.json'] = b'{"v": 1}'
        r = self.pedir('GET', '/admin/api/sesion', cookies=[f'ork3d_admin={secreto}'])
        self.assertEqual((r['statusCode'], datos(r)['error']), (401, 'no_autenticado'))

    def test_sesion_que_no_es_json_responde_401(self):
        secreto = 'B' * 43
        self.s3.objects[f'admin/sesiones/{_sesion.hash_secreto(secreto)}.json'] = b'no-es-json'
        r = self.pedir('GET', '/admin/api/sesion', cookies=[f'ork3d_admin={secreto}'])
        self.assertEqual((r['statusCode'], datos(r)['error']), (401, 'no_autenticado'))

    def test_login_corrupto_responde_como_codigo_invalido(self):
        self.assertEqual(self.pedir('POST', '/admin/api/sesion/solicitar', {})['statusCode'], 202)
        codigo = self.ultimo_codigo()
        self.s3.objects[f'admin/logins/{_sesion.hash_secreto(codigo)}.json'] = b'{"v": 1}'
        r = self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo})
        self.assertEqual((r['statusCode'], datos(r)), (401, ERROR_CODIGO))
        self.assertFalse([k for k in self.s3.objects if k.startswith('admin/sesiones/')])


if __name__ == '__main__':
    unittest.main()
