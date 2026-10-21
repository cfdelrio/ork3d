"""Tests de privacidad (V2): logs sin secretos ni datos personales, allowlist del export, sin
correos fijos en el código, y reglas de seguridad del frontend admin (sin innerHTML, sin
almacenamiento persistente, sin secretos en JS).

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import glob
import hashlib
import json
import os
import re
import unittest

import app
from admin_fixtures import (BASE, CONTACTO, DESTINO, FEEDBACK, JPEG, ORIGEN, PEDIDO, PRIVADO, REMITENTE,
                            AdminSesionCase, datos, sesion_de)
from nucleo import publicacion as _publicacion
from nucleo import registro as _registro

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
COMENTARIO = 'Comentario-unico-del-cliente-XYZ'
FEEDBACK_CLIENTE = 'Feedback-privado-del-cliente-QWE'
NOMBRE = 'Ana Gómez Pérez'
CAMPOS_LOG_V2 = {'review_id', 'op', 'status', 'estado', 'resultado', 'error', 'ruta', 'intento', 'foto', 'count'}
EXPORT_ALLOWLIST = {'id', 'rating', 'comment', 'aspects', 'display_name', 'category', 'product_label',
                    'verified', 'photo_url', 'published_at'}


def leer_fuentes(patron):
    """Contenido de los archivos del repo que coinciden con `patron` (ruta relativa a la raíz)."""
    fuentes = {}
    for ruta in sorted(glob.glob(os.path.join(RAIZ, patron), recursive=True)):
        with open(ruta, encoding='utf-8') as f:
            fuentes[os.path.relpath(ruta, RAIZ)] = f.read()
    return fuentes


def sin_comentarios_js(fuente):
    """Quita comentarios /* */ y // para que una mención en un comentario no cuente como uso."""
    fuente = re.sub(r'/\*.*?\*/', '', fuente, flags=re.S)
    return re.sub(r'(?m)^\s*//.*$', '', fuente)


class LogsSanitizadosTest(unittest.TestCase):
    def test_campos_permitidos_son_exactamente_los_del_contrato(self):
        self.assertEqual(_registro.CAMPOS, CAMPOS_LOG_V2)

    def test_evento_descarta_campos_que_no_estan_en_la_allowlist(self):
        registro = _registro.evento('ejemplo', review_id='a' * 32, comment='hola', token='Qx7' * 10,
                                    contact=CONTACTO, private_feedback=FEEDBACK)
        self.assertEqual(set(registro) - {'ev', 'ts'}, {'review_id'})
        self.assertEqual(registro['review_id'], 'a' * 32)

    def test_valor_que_no_es_identificador_se_reemplaza(self):
        registro = _registro.evento('ejemplo', error='Ana Gómez <ana@example.com>', op='aprobar')
        self.assertEqual(registro['error'], '[omitido]')
        self.assertEqual(registro['op'], 'aprobar')

    def test_ruta_plantilla_oculta_ids_y_tokens(self):
        token = 'Qx7_mLp2vR9tZkW4aB8cD1eF3gH5jK6n'
        self.assertEqual(_registro.ruta_plantilla(f'/tokens/{token}'), '/tokens/{token}')
        self.assertEqual(_registro.ruta_plantilla(f'/admin/api/resenas/{"a" * 32}/aprobar'),
                         '/admin/api/resenas/{id}/aprobar')
        self.assertEqual(_registro.ruta_plantilla('/opinar/' + 'b' * 32), '/opinar/{id}')

    def test_ningun_archivo_del_nucleo_ni_las_lambdas_registra_nombres_de_campos_sensibles(self):
        for ruta, fuente in leer_fuentes('api/resenas/*.py').items() | leer_fuentes('api/resenas/nucleo/*.py').items():
            if os.path.basename(ruta).startswith('test_'):
                continue
            for linea in fuente.splitlines():
                if re.search(r'_log\(|_registro\.evento\(|log\.info\(', linea):
                    for sensible in ['token_hash', 'th=', 'comment=', 'private_feedback=', 'contact=', 'email=']:
                        self.assertNotIn(sensible, linea, f'{ruta}: {linea.strip()}')


class SinCorreosFijosTest(unittest.TestCase):
    def test_el_codigo_de_produccion_no_tiene_direcciones_de_correo(self):
        patron = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
        fuentes = {**leer_fuentes('api/resenas/*.py'), **leer_fuentes('api/resenas/nucleo/*.py')}
        soporte = {'fakes.py', 'admin_fixtures.py'}  # dobles y fixtures de test, no código de producción
        for ruta, fuente in fuentes.items():
            if os.path.basename(ruta).startswith('test_') or os.path.basename(ruta) in soporte:
                continue
            self.assertIsNone(patron.search(fuente), f'{ruta} contiene una dirección de correo')


class ExportAllowlistTest(unittest.TestCase):
    def test_ningun_item_del_export_trae_claves_fuera_de_la_allowlist(self):
        self.assertEqual(set(_publicacion.ALLOWLIST_EXPORT), EXPORT_ALLOWLIST)


class FlujoCompletoSinSecretosEnLogs(AdminSesionCase):
    """Recorre el flujo entero y revisa cada línea de log que se emitió."""

    def token_por_invitacion(self):
        enlace = datos(self.invitacion(self.cookies))['enlace']
        return enlace.rsplit('/', 1)[1]

    def test_ningun_log_contiene_secretos_ni_datos_personales(self):
        token = self.token_por_invitacion()
        th = hashlib.sha256(token.encode('utf-8')).hexdigest()
        publica = app.Service(self.s3, PRIVADO, ORIGEN, base_url=BASE, origen_correo=REMITENTE,
                              destino_correo=DESTINO, correo=self.ses)
        with self.assertLogs('resenas', level='INFO') as registrado:
            # El cliente sube la foto y deja la reseña (dispara el aviso al moderador).
            _, firma = publica.presign({'token': token, 'ext': 'jpg'})
            self.s3.objects[firma['key']] = JPEG
            status, body = publica.create_review({
                'token': token, 'rating': 5, 'comment': COMENTARIO, 'aspects': ['calidad'],
                'display_mode': 'nombre', 'name': NOMBRE, 'permission_to_publish': True,
                'private_feedback': FEEDBACK_CLIENTE, 'contact_requested': True, 'contact': CONTACTO,
                'website': '', 'photo_key': firma['key']})
            self.assertEqual(status, 200)
            rid = body['id']
            # Moderación, export y reenvío de aviso.
            self.assertEqual(self.pedir('POST', f'/admin/api/resenas/{rid}/aprobar', cookies=self.cookies)['statusCode'], 200)
            self.assertEqual(self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)['statusCode'], 200)
            # Acceso: un código equivocado, luego el bueno.
            self.assertEqual(self.pedir('POST', '/admin/api/sesion/solicitar', {})['statusCode'], 202)
            codigo = self.ultimo_codigo()
            self.assertEqual(self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': 'x' * 43})['statusCode'], 401)
            r = self.pedir('POST', '/admin/api/sesion/confirmar', {'codigo': codigo})
            self.assertEqual(r['statusCode'], 200)
            cookies = [r['cookies'][0].split(';')[0]]
            self.ses.fallar_proximo()
            otra = self.pedir('POST', '/admin/api/invitaciones', {'order_ref': 'PEDIDO-2', 'product_slug': 'x-1',
                                                              'product_label': 'Otro', 'category': 'trofeos'},
                              cookies=cookies)
            self.assertEqual(otra['statusCode'], 201)
            # Falla de SES en un reenvío de aviso y de invalidación de CloudFront.
            pendiente = self.sembrar_resena(status='pending')
            self.ses.fallar_proximo()
            self.assertEqual(self.pedir('POST', f'/admin/api/resenas/{pendiente}/reenviar-aviso', cookies=cookies)['statusCode'], 503)
            self.cf.fallar_proximo()
            self.assertEqual(self.pedir('POST', '/admin/api/exportar', cookies=cookies)['statusCode'], 503)
            # Error interno con datos en el mensaje: no debe salir.
            self.s3.fail_on('get_object', RuntimeError(f'filtra {PEDIDO} {NOMBRE}'), prefix='resenas/')
            self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}', cookies=cookies)['statusCode'], 500)
            self.assertEqual(self.pedir('POST', '/admin/api/sesion/cerrar', cookies=cookies)['statusCode'], 200)

        salida = '\n'.join(registrado.output)
        secreto_sesion = sesion_de(cookies)
        prohibidos = [token, token[:12], th, th[:12], COMENTARIO, FEEDBACK_CLIENTE, CONTACTO, DESTINO,
                      REMITENTE, PEDIDO, NOMBRE, 'Gómez', codigo, codigo[:12], secreto_sesion,
                      secreto_sesion[:12], 'ork3d_admin=', 'Set-Cookie', 'X-Amz', 'https://', 'http://']
        for prohibido in prohibidos:
            self.assertNotIn(prohibido, salida, f'los logs contienen {prohibido[:12]!r}...')

        eventos = [json.loads(linea.split(':', 2)[2]) for linea in registrado.output]
        nombres = {e['ev'] for e in eventos}
        for esperado in ['resena_creada', 'moderacion', 'export', 'login_solicitado', 'login_rechazado',
                         'login_confirmado', 'aviso_fallido', 'error_interno']:
            self.assertIn(esperado, nombres)
        plantillas = [e['ruta'] for e in eventos if e['ev'] == 'error_interno']
        self.assertEqual(plantillas, ['/admin/api/resenas/{id}'])

    def test_export_publicado_tiene_exactamente_la_allowlist(self):
        rid = self.sembrar_resena(status='approved', published_at='2026-10-19T09:00:00Z')
        self.assertEqual(self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)['statusCode'], 200)
        export = json.loads(self.s3.objects['resenas/publicas.json'])
        self.assertEqual([i['id'] for i in export['items']], [rid])
        for item in export['items']:
            self.assertEqual(set(item), EXPORT_ALLOWLIST)
        texto = json.dumps(export)
        for prohibido in [PEDIDO, FEEDBACK, CONTACTO, 'token_hash']:
            self.assertNotIn(prohibido, texto)


class FrontendAdminTest(unittest.TestCase):
    """Reglas del frontend admin que se pueden verificar leyendo el código."""

    def archivos_js(self):
        archivos = {ruta: sin_comentarios_js(f) for ruta, f in leer_fuentes('assets/js/admin-*.js').items()}
        self.assertTrue(archivos, 'no hay JavaScript admin para revisar')
        return archivos

    def test_js_admin_no_usa_html_dinamico_ni_evaluacion(self):
        prohibidos = [r'\.innerHTML\b', r'\.outerHTML\b', r'insertAdjacentHTML', r'document\.write',
                      r'\beval\s*\(', r'new\s+Function\s*\(', r'setTimeout\s*\(\s*[\'"`]']
        for ruta, fuente in self.archivos_js().items():
            for patron in prohibidos:
                self.assertIsNone(re.search(patron, fuente), f'{ruta} usa {patron}')

    def test_js_admin_no_guarda_nada_en_el_navegador(self):
        for ruta, fuente in self.archivos_js().items():
            for patron in [r'localStorage', r'sessionStorage', r'indexedDB', r'document\.cookie']:
                self.assertIsNone(re.search(patron, fuente), f'{ruta} usa {patron}')

    def test_frontend_admin_no_tiene_secretos_ni_urls_de_aws(self):
        patrones = [r'AKIA[0-9A-Z]{16}', r'aws_secret', r'X-Amz-', r'amazonaws\.com', r'arn:aws',
                    r'BEGIN (RSA )?PRIVATE KEY', r'sk_live_', r'Bearer\s']
        fuentes = {**self.archivos_js(), **leer_fuentes('admin-web/*.html'), **leer_fuentes('assets/css/admin.css')}
        self.assertTrue(fuentes)
        for ruta, fuente in fuentes.items():
            for patron in patrones:
                self.assertIsNone(re.search(patron, fuente), f'{ruta} contiene {patron}')

    def test_admin_web_no_trae_datos_de_reseñas(self):
        paginas = leer_fuentes('admin-web/*.html')
        self.assertTrue(paginas, 'no hay páginas admin para revisar')
        for ruta, fuente in paginas.items():
            for prohibido in [PEDIDO, CONTACTO, FEEDBACK, 'Muy buena compra']:
                self.assertNotIn(prohibido, fuente, f'{ruta} trae datos estáticos')

    def test_fetch_solo_en_el_helper_compartido_y_los_post_firman_el_body(self):
        # Todo el tráfico del panel pasa por llamar() de admin-comun.js: ahí se serializa el body una vez,
        # se calcula su SHA-256 (x-amz-content-sha256) y se envía ese mismo string.
        js = self.archivos_js()
        comun = js['assets/js/admin-comun.js']
        self.assertEqual(len(re.findall(r'\bfetch\(', comun)), 1)
        self.assertEqual(len(re.findall(r'JSON\.stringify\(', comun)), 1, 'el body se serializa una sola vez')
        self.assertIn("digest('SHA-256'", comun)
        self.assertIn("'x-amz-content-sha256'", comun)
        self.assertIn('opciones.body = cuerpoTexto;', comun)
        for ruta, fuente in js.items():
            if ruta == 'assets/js/admin-comun.js':
                continue
            self.assertIsNone(re.search(r'\bfetch\(', fuente), f'{ruta} hace fetch por fuera del helper')
            self.assertIsNone(re.search(r'JSON\.stringify\(', fuente), f'{ruta} serializa un body por fuera del helper')

    def test_js_admin_no_construye_urls_absolutas_fuera_del_sitio(self):
        for ruta, fuente in self.archivos_js().items():
            for url in re.findall(r"""['"`]https?://[^'"`\s]+""", fuente):
                self.fail(f'{ruta} tiene una URL absoluta: {url}')


if __name__ == '__main__':
    unittest.main()
