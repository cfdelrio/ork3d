"""Pruebas de la verificación de lectura de producción (infra/prod-lectura-v2.sh y prod_lectura_ayuda.py).

Sin AWS, sin red: el sitio es una copia de las páginas reales del repo (FAKE_SITIO_DIR) y el CLI de AWS es el
simulador. Única excepción conocida: el simulador no modela la redirección www → apex de CloudFront, así que ese
control falla a propósito en el camino feliz; la prueba lo declara.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'prod-lectura-v2.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'prod_lectura_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
PAGINAS_REPO = ['index.html', 'opiniones.html', 'sitemap.xml', 'opinar.html',
                'admin-web/entrar.html', 'admin-web/resenas.html']
ITEM = {'id': '0' * 32, 'rating': 5, 'comment': 'Muy buena', 'aspects': ['calidad'], 'display_name': 'Ana G.',
        'category': 'trofeos', 'product_label': 'Trofeo', 'verified': True,
        'photo_url': None, 'published_at': '2026-10-22T14:01:24Z'}


REQUEST = '6f1c8e2a-0b7d-4a4e-9a55-1f0d6c2e9b11'
LINEAS_NORMALES = [
    f'START RequestId: {REQUEST} Version: $LATEST\n',
    f'END RequestId: {REQUEST}\n',
    f'REPORT RequestId: {REQUEST}\tDuration: 12.31 ms\tBilled Duration: 13 ms\tMemory Size: 256 MB\tMax Memory Used: 61 MB\n',
    '{"ev": "moderacion", "review_id": "0123456789abcdef0123456789abcdef", "op": "aprobar", "estado": "publicada", '
    '"resultado": "ok"}\n',
]
VALOR_FALSO = 'valor-de-prueba-' + 'a' * 24   # no es un token: el control nunca debe imprimirlo


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


fake = cargar('aws_fake_admin', FAKE_AWS)
ayuda = cargar('prod_lectura_ayuda', AYUDA)


class HelpersLecturaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prod-lectura-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def archivo(self, nombre, texto):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(texto)
        return ruta

    def export(self, items):
        return self.archivo('publicas.json', json.dumps({'items': items}))

    def test_export_con_la_allowlist_exacta_pasa(self):
        self.assertTrue(ayuda.exportar(self.export([ITEM])).startswith('PASS'))

    def test_export_con_campo_extra_falla_y_nombra_el_campo(self):
        resultado = ayuda.exportar(self.export([{**ITEM, 'private_feedback': 'x'}]))
        self.assertTrue(resultado.startswith('FAIL'))
        self.assertIn('private_feedback', resultado)

    def test_export_con_correo_o_hash_falla(self):
        self.assertTrue(ayuda.exportar(self.export([{**ITEM, 'comment': 'escribime a ana@example.com'}])).startswith('FAIL'))
        self.assertTrue(ayuda.exportar(self.export([{**ITEM, 'comment': 'f' * 64}])).startswith('FAIL'))

    def test_export_sin_lista_items_falla(self):
        self.assertTrue(ayuda.exportar(self.archivo('x.json', '{"otra": 1}')).startswith('FAIL'))

    def test_fotos_lista_solo_las_que_tienen_url(self):
        urls = ayuda.fotos(self.export([ITEM, {**ITEM, 'photo_url': '/resenas/fotos/a.jpg'}]))
        self.assertEqual(urls, ['/resenas/fotos/a.jpg'])

    def test_robots_exacto(self):
        ok = self.archivo('a.html', '<meta name="robots" content="noindex, follow" />')
        self.assertTrue(ayuda.robots(ok, 'noindex, follow').startswith('PASS'))
        self.assertTrue(ayuda.robots(ok, 'noindex, nofollow').startswith('FAIL'))
        self.assertTrue(ayuda.robots(self.archivo('b.html', '<p>sin meta</p>'), 'noindex').startswith('FAIL'))

    def test_sin_enlace_detecta_todas_las_formas_de_href(self):
        for html in ('<a href="/opiniones">x</a>', "<a href='/opiniones'>x</a>", '<a href="opiniones">x</a>',
                     '<a href="/opiniones/">x</a>', '<a href="/opiniones?x=1">x</a>'):
            with self.subTest(html=html):
                self.assertTrue(ayuda.sin_enlace(self.archivo('h.html', html), '/opiniones').startswith('FAIL'))
        self.assertTrue(ayuda.sin_enlace(self.archivo('ok.html', '<a href="/opiniones-otra">x</a>'),
                                         '/opiniones').startswith('PASS'))

    def test_sitemap_del_repo_no_lista_opiniones(self):
        self.assertTrue(ayuda.sitemap_sin(os.path.join(RAIZ, 'sitemap.xml'), '/opiniones').startswith('PASS'))

    def test_flag_de_notificaciones(self):
        def cfg(valor):
            vars_ = {} if valor is None else {'RESENAS_NOTIFY_ENABLED': valor}
            return self.archivo('cfg.json', json.dumps({'Environment': {'Variables': vars_}}))
        self.assertTrue(ayuda.flag_notificaciones(cfg(None)).startswith('PASS'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('false')).startswith('PASS'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('true')).startswith('FAIL'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('TRUE')).startswith('FAIL'))

    def test_flag_con_estado_esperado_true_tras_la_activacion(self):
        def cfg(valor):
            return self.archivo('cfg.json', json.dumps({'Environment': {'Variables': {'RESENAS_NOTIFY_ENABLED': valor}}}))
        self.assertTrue(ayuda.flag_notificaciones(cfg('true'), 'true').startswith('PASS'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('false'), 'true').startswith('FAIL'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('false'), 'false').startswith('PASS'))
        self.assertTrue(ayuda.flag_notificaciones(cfg('true'), 'false').startswith('FAIL'))

    def test_el_flag_no_imprime_el_resto_de_la_configuracion(self):
        ruta = self.archivo('cfg.json', json.dumps({'Environment': {'Variables': {
            'RESENAS_NOTIFY_ENABLED': 'false', 'RESENAS_ADMIN_EMAIL': 'no-imprimir@example.com'}}}))
        self.assertNotIn('no-imprimir', ayuda.flag_notificaciones(ruta))


class LogsLimpiosTest(unittest.TestCase):
    """Checker de cookies en los logs: busca en el texto de cada evento; no cuenta los eventos devueltos."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prod-logs-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def logs(self, mensajes):
        ruta = os.path.join(self.tmp, 'logs.json')
        eventos = [{'timestamp': 1, 'message': m, 'eventId': str(i)} for i, m in enumerate(mensajes)]
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump({'events': eventos, 'searchedLogStreams': []}, f)
        return ruta

    def test_eventos_normales_pasan(self):
        self.assertEqual(ayuda.logs_limpios(self.logs(LINEAS_NORMALES)), 'PASS: 4 evento(s) revisados')

    def test_sin_eventos_pasa(self):
        self.assertEqual(ayuda.logs_limpios(self.logs([])), 'PASS: 0 evento(s) revisados')

    def test_fuga_real_falla_y_no_imprime_el_valor(self):
        fugas = [
            f'Cookie: ork3d_admin={VALOR_FALSO}',
            f'cookie: ork3d_admin={VALOR_FALSO}',
            f'Set-Cookie: ork3d_admin={VALOR_FALSO}; Path=/admin; HttpOnly; Secure; SameSite=Lax; Max-Age=28800',
            f'set-cookie: ORK3D_ADMIN={VALOR_FALSO}',
            f'{{"headers": {{"Set-Cookie": "ork3d_admin={VALOR_FALSO}"}}}}',
            'Cookie: otra=1',
            'x=1; Path=/admin; HttpOnly',
        ]
        for i, fuga in enumerate(fugas):
            with self.subTest(caso=i):
                resultado = ayuda.logs_limpios(self.logs([LINEAS_NORMALES[0], fuga]))
                self.assertTrue(resultado.startswith('FAIL: 1 de 2'), resultado)
                self.assertNotIn(VALOR_FALSO, resultado)

    def test_resumen_nombra_las_categorias_sin_el_valor(self):
        resultado = ayuda.logs_limpios(self.logs([f'Set-Cookie: ork3d_admin={VALOR_FALSO}; HttpOnly']))
        self.assertIn('nombre de la cookie de sesión (1)', resultado)
        self.assertIn('cabecera Set-Cookie (1)', resultado)
        self.assertIn('atributo HttpOnly de una cookie (1)', resultado)
        self.assertNotIn(VALOR_FALSO, resultado)

    def test_texto_documental_no_cuenta(self):
        documental = [
            'La cookie de sesión dura 8 horas.',
            'Set-Cookie se documenta en V2-ADMIN.md; HttpOnly también.',
            '{"ev": "sesion_cerrada", "cookies": 0, "resultado": "ok"}',
            'El atributo httponly no se loguea.',
            'HttpOnly',
            '    cookie = leer_cookie(evento)   # línea de código en un traceback',
        ]
        self.assertEqual(ayuda.logs_limpios(self.logs(documental)), 'PASS: 6 evento(s) revisados')

    def test_regresion_120_eventos_normales_sin_cookies_pasa(self):
        """El checker anterior daba FAIL con cualquier evento devuelto. Con 120 eventos normales debe dar PASS."""
        mensajes = []
        for i in range(40):
            rid = f'{i:08x}-0b7d-4a4e-9a55-1f0d6c2e9b11'
            mensajes += [f'START RequestId: {rid} Version: $LATEST\n', f'END RequestId: {rid}\n',
                         f'REPORT RequestId: {rid}\tDuration: 3.10 ms\tMax Memory Used: 61 MB\n']
        self.assertEqual(ayuda.logs_limpios(self.logs(mensajes)), 'PASS: 120 evento(s) revisados')

    def test_una_fuga_entre_120_eventos_se_cuenta_sobre_el_total(self):
        mensajes = [LINEAS_NORMALES[0]] * 120 + [f'Cookie: ork3d_admin={VALOR_FALSO}']
        self.assertTrue(ayuda.logs_limpios(self.logs(mensajes)).startswith('FAIL: 1 de 121'))

    def test_evento_sin_texto_o_salida_que_no_es_json_fallan(self):
        sin_texto = os.path.join(self.tmp, 'sin-texto.json')
        with open(sin_texto, 'w', encoding='utf-8') as f:
            json.dump({'events': [{'eventId': '1'}]}, f)
        self.assertTrue(ayuda.logs_limpios(sin_texto).startswith('FAIL'))
        no_json = os.path.join(self.tmp, 'no-json.json')
        with open(no_json, 'w', encoding='utf-8') as f:
            f.write('<html>')
        self.assertTrue(ayuda.logs_limpios(no_json).startswith('FAIL'))


class ScriptSoloLecturaTest(unittest.TestCase):
    def setUp(self):
        with open(SCRIPT, encoding='utf-8') as f:
            self.texto = f.read()

    def test_los_logs_se_leen_completos_sin_filtro_ni_limite(self):
        codigo = '\n'.join(l for l in self.texto.splitlines() if not l.lstrip().startswith('#'))   # sin comentarios
        self.assertIn('filter-log-events', codigo)
        for opcion in ('--filter-pattern', '--max-items', '--starting-token', '--page-size'):
            self.assertNotIn(opcion, codigo)

    def test_solo_peticiones_get(self):
        self.assertNotRegex(self.texto, r'-X\s+(POST|PUT|PATCH|DELETE)')
        self.assertNotIn('--data', self.texto)

    def test_ninguna_operacion_de_escritura_de_aws(self):
        for operacion in ('put-', 'create-', 'update-', 'delete-', 'publish', 'invalidat', 'add-permission',
                          'attach-', 'remove-', 'modify-'):
            self.assertNotIn(operacion, self.texto)
        self.assertNotRegex(self.texto, r's3 (cp|mv|rm|sync)\b')

    def test_la_unica_lectura_de_aws_es_la_configuracion_de_la_lambda_publica(self):
        self.assertEqual(re.findall(r'"\$AWS" ([a-z0-9-]+ [a-z0-9-]+)', self.texto),
                         ['lambda get-function-configuration', 'logs filter-log-events'])


class PruebaEndToEndTest(unittest.TestCase):
    """El script completo contra una copia del sitio real del repo y del simulador de AWS y de curl."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='prod-lectura-e2e-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sitio = os.path.join(self.tmp, 'sitio')
        for rel in PAGINAS_REPO:
            destino = os.path.join(self.sitio, rel)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copyfile(os.path.join(RAIZ, rel), destino)
        self.publicar([ITEM, {**ITEM, 'id': '1' * 32, 'photo_url': '/resenas/fotos/' + '1' * 32 + '.jpg'}])
        os.makedirs(os.path.join(self.sitio, 'resenas', 'fotos'), exist_ok=True)
        with open(os.path.join(self.sitio, 'resenas', 'fotos', '1' * 32 + '.jpg'), 'wb') as f:
            f.write(b'\xff\xd8\xff\xe0jpeg-de-prueba')
        self.estado = os.path.join(self.tmp, 'estado.json')
        st = fake.estado_inicial()
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        self.peticiones = os.path.join(self.tmp, 'peticiones.jsonl')

    def publicar(self, items):
        carpeta = os.path.join(self.sitio, 'resenas')
        os.makedirs(carpeta, exist_ok=True)
        with open(os.path.join(carpeta, 'publicas.json'), 'w', encoding='utf-8') as f:
            json.dump({'items': items}, f)

    def correr(self, estado=None, extra=None):
        if estado is not None:
            with open(self.estado, 'w', encoding='utf-8') as f:
                json.dump(estado, f)
        env = {'PATH': os.environ.get('PATH', ''), 'CURL_CMD': FAKE_CURL, 'AWS_CLI': FAKE_AWS,
               'FAKE_SITIO_DIR': self.sitio, 'FAKE_ESTADO': self.estado, 'FAKE_CURL_REQ': self.peticiones}
        env.update(extra or {})
        return subprocess.run(['bash', SCRIPT], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=120)

    def peticiones_hechas(self):
        if not os.path.exists(self.peticiones):
            return 0
        with open(self.peticiones, encoding='utf-8') as f:
            return len(f.readlines())

    def lineas(self, r):
        return [l for l in (r.stdout + r.stderr).splitlines() if l[:4] in ('PASS', 'FAIL')]

    def test_camino_feliz_todos_pasan_salvo_el_redirect_www_del_simulador(self):
        r = self.correr()
        fallos = [l for l in self.lineas(r) if l.startswith('FAIL')]
        self.assertEqual(len(fallos), 1, fallos)
        self.assertIn('www redirige al apex', fallos[0])
        pasan = ' | '.join(self.lineas(r))
        for control in ('home responde 200', '/opiniones accesible directamente', '/opiniones noindex, follow',
                        'home no enlaza /opiniones', 'sitemap no lista /opiniones',
                        '/opinar/<token> responde la página pública', '/admin/resenas responde 200',
                        '/admin/entrar responde 200', 'API admin sin cookie responde 401',
                        'export público: solo la allowlist', 'foto pública 1', 'RESENAS_NOTIFY_ENABLED apagado',
                        'logs de /aws/lambda/ork3d-resenas-admin sin cookies', 'logs de /aws/lambda/ork3d-resenas sin'):
            self.assertIn(control, pasan)
        self.assertEqual(r.returncode, 1, 'un FAIL deja el código de salida en 1')

    def test_solo_envia_peticiones_get(self):
        self.correr()
        with open(self.peticiones, encoding='utf-8') as f:
            metodos = {json.loads(l)['metodo'] for l in f if l.strip()}
        self.assertEqual(metodos, {'GET'})

    def test_export_con_dato_privado_falla_el_control(self):
        self.publicar([{**ITEM, 'private_feedback': 'interno'}])
        self.assertIn('FAIL  export público: solo la allowlist', self.correr().stdout)

    def test_fuga_en_los_logs_falla_el_control_sin_mostrar_el_valor(self):
        fuga = os.path.join(self.tmp, 'fuga.json')
        with open(fuga, 'w', encoding='utf-8') as f:
            json.dump([LINEAS_NORMALES[0], f'Set-Cookie: ork3d_admin={VALOR_FALSO}; HttpOnly'], f)
        r = self.correr(extra={'FAKE_LOGS_EVENTOS': fuga})
        self.assertIn('FAIL  logs de /aws/lambda/ork3d-resenas-admin sin cookies ni Set-Cookie (24 h)', r.stdout)
        self.assertIn('FAIL  logs de /aws/lambda/ork3d-resenas sin cookies ni Set-Cookie (24 h)', r.stdout)
        self.assertNotIn(VALOR_FALSO, r.stdout + r.stderr)

    def test_notificaciones_encendidas_fallan_el_control(self):
        st = fake.estado_inicial()
        st['publica']['config']['Environment']['Variables']['RESENAS_NOTIFY_ENABLED'] = 'true'
        self.assertIn('FAIL  RESENAS_NOTIFY_ENABLED apagado', self.correr(st).stdout)

    def test_con_la_activacion_esperada_el_control_pasa_y_sin_ella_falla(self):
        st = fake.estado_inicial()
        st['publica']['config']['Environment']['Variables']['RESENAS_NOTIFY_ENABLED'] = 'true'
        self.assertIn('PASS  RESENAS_NOTIFY_ENABLED encendido', self.correr(st, extra={'PROD_LECTURA_NOTIFY_ESPERADO': 'true'}).stdout)
        self.assertIn('FAIL  RESENAS_NOTIFY_ENABLED encendido', self.correr(fake.estado_inicial(), extra={'PROD_LECTURA_NOTIFY_ESPERADO': 'true'}).stdout)

    def test_estado_esperado_invalido_no_ejecuta_los_controles(self):
        r = self.correr(extra={'PROD_LECTURA_NOTIFY_ESPERADO': 'si'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.peticiones_hechas(), 0)

    def test_opiniones_enlazada_desde_home_falla_el_control(self):
        with open(os.path.join(self.sitio, 'index.html'), 'a', encoding='utf-8') as f:
            f.write('<a href="/opiniones">Ver opiniones</a>')
        self.assertIn('FAIL  home no enlaza /opiniones', self.correr().stdout)

    def test_opiniones_con_robots_distinto_falla_el_control(self):
        with open(os.path.join(self.sitio, 'opiniones.html'), 'w', encoding='utf-8') as f:
            f.write('<meta name="robots" content="index, follow" />')
        self.assertIn('FAIL  /opiniones noindex, follow', self.correr().stdout)


if __name__ == '__main__':
    unittest.main()
