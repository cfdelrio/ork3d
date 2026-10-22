"""Controles de cierre de V2 admin: magic link de un solo uso, concurrencia, sesión de 8 h, cookie, logout, CSRF
y logs. Mismo servicio real que test_admin_auth.py, sobre fakes. Sin AWS, sin red, sin reloj real.

Cada clase nombra el contrato que verifica (ver docs/resenas/V2-ADMIN.md §18). Correr desde api/resenas:
    python3 -m unittest test_seguridad_v2 -v
"""
import inspect
import json
import os
import re
import threading
import time
import unittest

import admin_app
from admin_fixtures import (CABECERAS_CSRF, CONTACTO, DESTINO, FEEDBACK, INVITACION, ORIGEN, PEDIDO, AdminCase,
                            AdminSesionCase, datos, sesion_de)
from nucleo import errores
from nucleo import registro as _registro
from nucleo import sesion as _sesion

ERROR_CODIGO = {'ok': False, 'error': 'codigo_invalido', 'mensaje': 'El enlace no es válido o venció.'}
ORIGEN_EXTRANO = 'https://evil.example'
RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COOKIE_SESION_RE = re.compile(r'^ork3d_admin=[A-Za-z0-9_-]{43}; HttpOnly; Secure; SameSite=Lax; Path=/admin; '
                              r'Max-Age=28800$')
COOKIE_CIERRE = 'ork3d_admin=; HttpOnly; Secure; SameSite=Lax; Path=/admin; Max-Age=0'


def hash_de(secreto):
    return _sesion.hash_secreto(secreto)


def fuente(ruta):
    with open(os.path.join(os.path.dirname(__file__), ruta), encoding='utf-8') as f:
        return f.read()


class MagicLinkTest(AdminCase):
    """solicitar → código → consumir → sesión → código usado. Un solo uso, replay sin efectos."""

    def emitir(self):
        self.assertEqual(self.pedir('POST', '/admin/api/sesion/solicitar', {})['statusCode'], 202)
        return self.ultimo_codigo()

    def confirmar(self, codigo):
        return self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo})

    def sesiones(self):
        return sorted(k for k in self.s3.objects if k.startswith('admin/sesiones/'))

    def test_primer_uso_crea_exactamente_una_sesion_y_marca_el_codigo(self):
        codigo = self.emitir()
        r = self.confirmar(codigo)
        self.assertEqual(r['statusCode'], 200)
        self.assertEqual(len(self.sesiones()), 1)
        usados = [k for k in self.s3.objects if k.startswith('admin/logins-usados/')]
        self.assertEqual(usados, [f'admin/logins-usados/{hash_de(codigo)}.json'])
        self.assertNotIn(codigo, self.s3.objects[usados[0]].decode('utf-8'))

    def test_replay_no_autentica_no_crea_sesion_y_responde_igual_que_un_codigo_falso(self):
        codigo = self.emitir()
        primera = self.confirmar(codigo)
        self.assertEqual(primera['statusCode'], 200)
        sesion_inicial = self.sesiones()
        self.reloj.avanzar(hours=1)
        replay = self.confirmar(codigo)
        falso = self.confirmar('z' * 43)
        self.assertEqual((replay['statusCode'], datos(replay)), (401, ERROR_CODIGO))
        self.assertEqual(replay['cookies'], [], 'el replay no entrega ninguna cookie')
        self.assertEqual(datos(replay), datos(falso), 'respuesta idéntica a la de un código inexistente')
        self.assertEqual(self.sesiones(), sesion_inicial, 'el replay no crea ni toca sesiones')

    def test_replay_no_prolonga_la_sesion_que_ya_existe(self):
        codigo = self.emitir()
        primera = self.confirmar(codigo)
        cookie = [f'ork3d_admin={sesion_de(primera["cookies"])}']
        expira = datos(primera)['expira']
        clave = f'admin/sesiones/{hash_de(sesion_de(primera["cookies"]))}.json'
        registro = self.s3.objects[clave]
        self.reloj.avanzar(hours=3)
        self.assertEqual(self.confirmar(codigo)['statusCode'], 401)
        self.assertEqual(self.s3.objects[clave], registro, 'el registro de la sesión no cambia')
        self.assertEqual(datos(self.pedir('GET', '/admin/api/sesion', cookies=cookie))['expira'], expira)

    def test_replay_con_sesion_ya_abierta_no_la_reemplaza(self):
        codigo = self.emitir()
        primera = self.confirmar(codigo)
        self.assertEqual(primera['statusCode'], 200)
        abierta = [f'ork3d_admin={sesion_de(primera["cookies"])}']
        self.assertEqual(self.confirmar(codigo)['statusCode'], 401)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=abierta)['statusCode'], 200)

    def test_codigo_usado_sigue_rechazado_aunque_su_login_venza(self):
        codigo = self.emitir()
        self.assertEqual(self.confirmar(codigo)['statusCode'], 200)
        self.reloj.avanzar(minutes=30)
        r = self.confirmar(codigo)
        self.assertEqual((r['statusCode'], datos(r)), (401, ERROR_CODIGO))

    def test_codigo_vencido_no_crea_sesion(self):
        codigo = self.emitir()
        self.reloj.avanzar(minutes=10)
        self.assertEqual((self.confirmar(codigo)['statusCode'], self.sesiones()), (401, []))


class ConcurrenciaLoginTest(AdminCase):
    """Dos o más consumos a la vez del mismo código: exactamente uno gana."""

    def carrera(self, n, consumir):
        codigo = self.svc.logins.emitir()
        barrera = threading.Barrier(n)
        resultados = [None] * n

        def trabajo(i):
            barrera.wait()
            resultados[i] = consumir(codigo)

        hilos = [threading.Thread(target=trabajo, args=(i,)) for i in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=30)
        self.assertFalse(any(h.is_alive() for h in hilos), 'un hilo no terminó')
        return resultados

    def consumo_real(self, codigo):
        try:
            self.svc.logins.consumir(codigo)
            return 'ganador'
        except errores.ErrorCodigo as exc:
            return str(exc.args[0])

    def test_dos_consumos_simultaneos_solo_uno_gana(self):
        for prueba in range(20):
            with self.subTest(prueba=prueba):
                resultados = self.carrera(2, self.consumo_real)
                self.assertEqual(sorted(resultados), ['ganador', 'usado'])

    def test_diez_consumos_simultaneos_exactamente_uno_gana(self):
        for prueba in range(5):
            with self.subTest(prueba=prueba):
                resultados = self.carrera(10, self.consumo_real)
                self.assertEqual(resultados.count('ganador'), 1)
                self.assertEqual(resultados.count('usado'), 9)

    def test_por_http_dos_confirmaciones_simultaneas_una_sola_sesion(self):
        codigo = self.svc.logins.emitir()
        barrera = threading.Barrier(2)
        respuestas = []

        def trabajo():
            barrera.wait()
            respuestas.append(self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo}))

        hilos = [threading.Thread(target=trabajo) for _ in range(2)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join(timeout=30)
        self.assertEqual(sorted(r['statusCode'] for r in respuestas), [200, 401])
        sesiones = [k for k in self.s3.objects if k.startswith('admin/sesiones/')]
        self.assertEqual(len(sesiones), 1)

    def test_control_negativo_leer_comprobar_escribir_deja_dos_ganadores(self):
        """Prueba la propia prueba: el patrón prohibido tiene que producir dos ganadores con este arnés."""
        class LeerComprobarEscribir(_sesion.Logins):
            def consumir(self, codigo):
                h = _sesion.hash_secreto(codigo)
                registro, _ = self.almacen.leer_json(self.bucket, f'admin/logins/{h}.json')
                usado, _ = self.almacen.leer_json(self.bucket, f'admin/logins-usados/{h}.json')
                if registro is None or usado is not None:
                    raise errores.ErrorCodigo('usado')
                time.sleep(0.05)  # ventana entre comprobar y escribir
                self.almacen.escribir_json(self.bucket, f'admin/logins-usados/{h}.json', {'v': 1})
                return True

        ingenuo = LeerComprobarEscribir(self.svc.logins.almacen, self.svc.logins.bucket,
                                        self.svc.logins.ahora, self.svc.logins.minutos)

        def consumo_ingenuo(codigo):
            try:
                ingenuo.consumir(codigo)
                return 'ganador'
            except errores.ErrorCodigo as exc:
                return str(exc.args[0])

        dobles = sum(1 for _ in range(3) if self.carrera(2, consumo_ingenuo).count('ganador') == 2)
        self.assertGreater(dobles, 0, 'el arnés no detecta la carrera: la prueba de concurrencia no vale')

    def test_el_consumo_decide_con_escritura_condicional(self):
        codigo_fuente = inspect.getsource(_sesion.Logins.consumir)
        inicio = codigo_fuente.index('admin/logins-usados/')
        tramo = codigo_fuente[inicio:codigo_fuente.index('except', inicio)]
        self.assertIn('solo_si_no_existe=True', tramo, 'la marca de uso solo se crea si no existe')


class SesionVentanaTest(AdminSesionCase):
    """Sesión de 8 h desde su creación: válida antes, inválida al vencer, sin renovación automática."""

    def get(self, cookies=None):
        return self.pedir('GET', '/admin/api/sesion', cookies=self.cookies if cookies is None else cookies)

    def test_recien_creada_es_valida(self):
        self.assertEqual(self.get()['statusCode'], 200)

    def test_antes_de_vencer_es_valida(self):
        self.reloj.avanzar(hours=7, minutes=59, seconds=59)
        self.assertEqual(self.get()['statusCode'], 200)

    def test_justo_al_vencer_es_invalida(self):
        self.reloj.avanzar(hours=8)
        self.assertEqual(self.get()['statusCode'], 401)
        self.reloj.avanzar(seconds=1)
        self.assertEqual(self.get()['statusCode'], 401)

    def test_no_se_renueva_con_lecturas(self):
        expira = datos(self.get())['expira']
        clave = f'admin/sesiones/{hash_de(sesion_de(self.cookies))}.json'
        registro = self.s3.objects[clave]
        for tramo in (1, 2, 4):  # 1 h, 3 h y 7 h desde la creación: siempre dentro de las 8 h
            self.reloj.avanzar(hours=tramo)
            self.assertEqual(datos(self.get())['expira'], expira)
        self.assertEqual(self.s3.objects[clave], registro, 'leer no reescribe la sesión')

    def test_sesion_inexistente_responde_401(self):
        self.assertEqual(self.get([f'ork3d_admin={"Q" * 43}'])['statusCode'], 401)

    def test_cookies_malformadas_responden_401_sin_error_interno(self):
        valores = ['', ' ', 'x' * 5000, 'ñandú', '../../admin/sesiones/x', 'a;b', 'Bearer abc', '%00', '"quoted"']
        escrituras = dict(self.s3.objects)
        for valor in valores:
            with self.subTest(valor=valor[:20]):
                r = self.get([f'ork3d_admin={valor}'])
                self.assertEqual((r['statusCode'], datos(r)['error']), (401, 'no_autenticado'))
        self.assertEqual(self.s3.objects, escrituras, 'las cookies malformadas no escriben nada')


class LogoutTest(AdminSesionCase):
    """Logout: invalida en el servidor y expira la cookie con los mismos atributos."""

    def test_cerrar_borra_el_registro_de_la_sesion_en_el_servidor(self):
        antes = [k for k in self.s3.objects if k.startswith('admin/sesiones/')]
        self.assertEqual(self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)['statusCode'], 200)
        self.assertEqual(len(antes), 1)
        self.assertEqual([k for k in self.s3.objects if k.startswith('admin/sesiones/')], [])

    def test_cookie_de_cierre_exacta(self):
        r = self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        self.assertEqual(r['cookies'], [COOKIE_CIERRE])

    def test_cookie_vieja_no_sirve_despues_de_cerrar(self):
        self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        self.assertEqual(self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)['statusCode'], 401)

    def test_doble_cierre_responde_401_sin_cookies_y_sin_escrituras(self):
        self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        antes = dict(self.s3.objects)
        r = self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        self.assertEqual((r['statusCode'], r['cookies']), (401, []))
        self.assertEqual(self.s3.objects, antes)

    def test_cerrar_sin_sesion_responde_401(self):
        self.assertEqual(self.pedir('POST', '/admin/api/sesion/cerrar')['statusCode'], 401)

    def test_cerrar_sin_csrf_no_borra_la_sesion(self):
        antes = dict(self.s3.objects)
        r = self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies,
                       cabeceras={'origin': ORIGEN_EXTRANO, 'content-type': 'application/json'})
        self.assertEqual((r['statusCode'], r['cookies']), (400, []))
        self.assertEqual(self.s3.objects, antes)


class CookieContratoTest(unittest.TestCase):
    """Atributos exactos de la cookie de sesión y de su cierre. Solo admin_app la emite."""

    def test_cookie_de_sesion_exacta(self):
        cookie = _sesion.cookie_sesion('S' * 43, 8)
        self.assertEqual(cookie, 'ork3d_admin=' + 'S' * 43 + '; HttpOnly; Secure; SameSite=Lax; Path=/admin; Max-Age=28800')
        self.assertRegex(cookie, COOKIE_SESION_RE)

    def test_nombre_de_la_cookie_es_el_del_codigo(self):
        self.assertEqual(_sesion.COOKIE, 'ork3d_admin')

    def test_sin_domain_ni_expires(self):
        for cookie in (_sesion.cookie_sesion('S' * 43, 8), _sesion.cookie_expirada()):
            self.assertNotIn('Domain', cookie)
            self.assertNotIn('Expires', cookie)

    def test_cierre_tiene_los_mismos_atributos_que_la_sesion(self):
        nombres = lambda cookie: sorted(p.strip().split('=', 1)[0] for p in cookie.split(';')[1:])
        sesion, cierre = _sesion.cookie_sesion('S' * 43, 8), _sesion.cookie_expirada()
        self.assertEqual(nombres(cierre), nombres(sesion))
        self.assertIn('Path=/admin', cierre)
        self.assertIn('SameSite=Lax', cierre)
        self.assertIn('Max-Age=0', cierre)

    def test_samesite_es_lax_y_nunca_strict_ni_none(self):
        self.assertIn('SameSite=Lax', fuente('nucleo/sesion.py'), 'el atributo vive en nucleo/sesion.py')
        for ruta in ('nucleo/sesion.py', 'admin_app.py'):
            self.assertNotRegex(fuente(ruta), r'SameSite=(Strict|None)')

    def test_solo_admin_app_emite_la_cookie_de_sesion(self):
        admin = fuente('admin_app.py')
        self.assertEqual(admin.count('_sesion.cookie_sesion('), 1, 'una sola emisión: en confirmar')
        self.assertEqual(admin.count('_sesion.cookie_expirada('), 1, 'una sola expiración: en cerrar')

    def test_la_api_publica_no_lee_ni_emite_la_cookie_admin(self):
        publica = fuente('app.py')
        self.assertNotIn('ork3d_admin', publica)
        self.assertNotIn('Set-Cookie', publica)
        self.assertNotIn('cookie_sesion', publica)


class CsrfMatrizTest(AdminSesionCase):
    """Regresión del contrato CSRF: 400 por forma de la petición (CloudFront reescribe los 403), 401 sin sesión."""

    def nueva_sesion(self):
        return [f'ork3d_admin={self.svc.sesiones.crear()[0]}']

    def rechazos(self):
        csrf = dict(CABECERAS_CSRF)
        return {
            'origin_ausente': ({k: v for k, v in csrf.items() if k != 'origin'}, 400, 'origen'),
            'origin_distinto': ({**csrf, 'origin': ORIGEN_EXTRANO}, 400, 'origen'),
            'x_admin_ausente': ({k: v for k, v in csrf.items() if k != 'x-admin-request'}, 400, 'csrf'),
            'x_admin_otro_valor': ({**csrf, 'x-admin-request': '0'}, 400, 'csrf'),
            'content_type_texto': ({**csrf, 'content-type': 'text/plain'}, 400, 'content_type'),
        }

    def rutas_con_sesion(self):
        rid = self.sembrar_resena(status='pending')
        return [
            ('POST', '/admin/api/invitaciones', INVITACION),
            ('POST', '/admin/api/exportar', None),
            ('POST', f'/admin/api/resenas/{rid}/aprobar', None),
            ('POST', f'/admin/api/resenas/{rid}/rechazar', None),
            ('POST', f'/admin/api/resenas/{rid}/reenviar-aviso', None),
        ]

    def test_mutaciones_con_sesion_rechazan_la_forma_invalida_con_400_sin_efectos(self):
        for metodo, ruta, cuerpo in self.rutas_con_sesion():
            for nombre, (cabeceras, estado, motivo) in self.rechazos().items():
                with self.subTest(ruta=ruta, caso=nombre):
                    cookies = self.nueva_sesion()
                    antes = dict(self.s3.objects)
                    r = self.pedir(metodo, ruta, cuerpo, cookies=cookies, cabeceras=cabeceras)
                    self.assertEqual((r['statusCode'], datos(r)['error']), (estado, motivo))
                    self.assertEqual(self.s3.objects, antes, 'un rechazo CSRF no escribe nada')

    def test_sin_sesion_responde_401_aunque_la_forma_sea_valida(self):
        for metodo, ruta, cuerpo in self.rutas_con_sesion():
            with self.subTest(ruta=ruta):
                r = self.pedir(metodo, ruta, cuerpo)
                self.assertEqual((r['statusCode'], datos(r)['error']), (401, 'no_autenticado'))

    def test_con_sesion_y_forma_valida_la_mutacion_pasa_el_csrf(self):
        for metodo, ruta, cuerpo in self.rutas_con_sesion():
            with self.subTest(ruta=ruta):
                r = self.pedir(metodo, ruta, cuerpo, cookies=self.nueva_sesion())
                self.assertNotIn(r['statusCode'], (400, 401, 403))

    def test_pre_sesion_exigen_origen_y_forma(self):
        for ruta, cuerpo in (('/admin/api/sesion/solicitar', {}), ('/admin/api/sesion/confirmar', {'codigo': 'x'})):
            for nombre, (cabeceras, estado, motivo) in self.rechazos().items():
                with self.subTest(ruta=ruta, caso=nombre):
                    r = self.pedir('POST', ruta, cuerpo, cabeceras=cabeceras)
                    self.assertEqual((r['statusCode'], datos(r)['error']), (estado, motivo))

    def test_ningun_rechazo_de_csrf_responde_403(self):
        codigos = set()
        for metodo, ruta, cuerpo in self.rutas_con_sesion():
            for cabeceras, _, _ in self.rechazos().values():
                codigos.add(self.pedir(metodo, ruta, cuerpo, cookies=self.nueva_sesion(),
                                       cabeceras=cabeceras)['statusCode'])
        self.assertNotIn(403, codigos)


class LogsSinSecretosTest(AdminSesionCase):
    """Un flujo completo no deja en el registro códigos, tokens, cookies, correos ni datos de la reseña."""

    def test_flujo_completo_no_loguea_secretos_ni_datos_personales(self):
        with self.assertLogs('resenas', level='INFO') as cm:
            self.pedir('POST', '/admin/api/sesion/solicitar', {})
            codigo = self.ultimo_codigo()
            self.assertEqual(self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo})['statusCode'], 200)
            self.assertEqual(self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo})['statusCode'], 401)
            r = self.invitacion(self.cookies)
            token = re.search(r'/opinar/([A-Za-z0-9_-]{20,})', r['body'] or '')
            self.assertIsNotNone(token, 'la invitación debe devolver el enlace para comprobar que no se loguea')
            rid = self.sembrar_resena(status='pending', foto=True)
            self.pedir('POST', f'/admin/api/resenas/{rid}/aprobar', cookies=self.cookies)
            self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
            self.pedir('POST', '/admin/api/sesion/cerrar', cookies=self.cookies)
        salida = '\n'.join(cm.output)
        secretos = [codigo, token.group(1), sesion_de(self.cookies), DESTINO, CONTACTO, FEEDBACK, PEDIDO]
        for secreto in secretos:
            self.assertNotIn(secreto, salida)
        self.assertNotIn('ork3d_admin', salida)
        self.assertNotIn('Set-Cookie', salida)
        self.assertNotIn('#c=', salida)
        for linea in salida.splitlines():
            evento = json.loads(linea.split(':resenas:', 1)[1])
            self.assertLessEqual(set(evento) - {'ev', 'ts'}, _registro.CAMPOS)


class SeguridadPublicaTest(unittest.TestCase):
    """Reglas de seguridad del frontend y del paso del código: se verifican en el código fuente y en Chromium
    (dev/e2e/admin.e2e.js, caso de login: cookie HttpOnly, Secure, SameSite=Lax, Path=/admin, 8 h; fragmento fuera de
    la URL antes de la primera petición)."""

    def test_el_frontend_de_login_no_guarda_nada_en_el_navegador(self):
        codigo = fuente(os.path.join('..', '..', 'assets', 'js', 'admin-entrar.js'))
        self.assertNotIn('localStorage', codigo)
        self.assertNotIn('sessionStorage', codigo)
        self.assertNotIn('document.cookie', codigo)

    def test_el_fragmento_se_borra_antes_de_cualquier_peticion(self):
        codigo = fuente(os.path.join('..', '..', 'assets', 'js', 'admin-entrar.js'))
        self.assertLess(codigo.index('codigo = leerCodigo()'), codigo.index('replaceState'))
        self.assertLess(codigo.index('replaceState'), codigo.index("A.llamar('POST', '/sesion/confirmar'"))

    def test_el_codigo_no_se_escribe_en_el_dom(self):
        codigo = fuente(os.path.join('..', '..', 'assets', 'js', 'admin-entrar.js'))
        self.assertNotIn('innerHTML', codigo)
        self.assertNotIn('textContent = codigo', codigo)
        self.assertNotIn('.value = codigo', codigo)


if __name__ == '__main__':
    unittest.main()
