"""Pruebas de la publicación del panel admin (infra/admin-frontend-deploy.sh y admin_frontend_ayuda.py).

Modelo: la función LIVE nunca se reemplaza por el repo. Se publica LIVE + bloque admin (la regla del panel insertada
antes del rewrite de extensionless). La propuesta se verifica: quitar el bloque tiene que dar LIVE byte a byte.

Sin AWS, sin red, sin credenciales: aws_fake_admin.py guarda el estado (función CloudFront, objetos del sitio,
invalidaciones), aws_fake_curl.py sirve el sitio desde FAKE_SITIO_DIR y aws_fake_git.py responde la guarda de git.
Las reglas de la función se ejecutan con node (cf_function_harness.js), igual que el runtime cloudfront-js.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'admin-frontend-deploy.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'admin_frontend_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
FAKE_GIT = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_git.py')
HARNESS = os.path.join(RAIZ, 'infra', 'tests', 'cf_function_harness.js')
REDIRECT = os.path.join(RAIZ, 'infra', 'redirect-www.js')
PAGINAS = ['admin-web/entrar.html', 'admin-web/resenas.html', 'assets/js/admin-comun.js',
           'assets/js/admin-entrar.js', 'assets/js/admin-panel.js']
MUTACIONES = ('cloudfront update-function', 'cloudfront publish-function', 'cloudfront create-invalidation', 's3 cp')
REGLA_PUBLICA_SOLO_LIVE = """  // Regla pública previa (solo en LIVE): /legado redirige al inicio.
  if (uri === '/legado') {
    request.uri = '/index.html';
    return request;
  }

"""


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


fake = cargar('aws_fake_admin', FAKE_AWS)
ayuda = cargar('admin_frontend_ayuda', AYUDA)


def leer_json(ruta):
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def leer_texto(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def llamar_ayuda(*args):
    salida = io.StringIO()
    with contextlib.redirect_stdout(salida):
        ayuda.main(list(args))
    return salida.getvalue().strip()


def fixtures_live(repo):
    """LIVE de prueba: el repo sin el bloque admin, y ese mismo LIVE con una regla pública que el repo no tiene."""
    sin = ayuda.sin_bloque(repo)
    extra = sin.replace(ayuda.ANCLA_COMENTARIO, REGLA_PUBLICA_SOLO_LIVE + ayuda.ANCLA_COMENTARIO, 1)
    return sin, extra


class AyudaFrontendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='frontend-ayuda-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = leer_texto(REDIRECT)
        self.sin_admin, self.extra = fixtures_live(self.repo)

    def escribir(self, nombre, texto):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(texto)
        return ruta

    def test_repo_tiene_bloque_admin_completo(self):
        self.assertEqual(llamar_ayuda('bloque-admin', REDIRECT), '')
        for regla in ('/admin-web/resenas.html', '/admin-web/entrar.html', '/admin/api/'):
            self.assertIn(regla, self.repo)

    def test_estado_de_live_frente_al_repo(self):
        self.assertEqual(ayuda.estado(self.escribir('a.js', self.sin_admin), REDIRECT), 'SIN_BLOQUE')
        self.assertEqual(ayuda.estado(self.escribir('b.js', self.repo), REDIRECT), 'PUBLICADA')
        self.assertEqual(ayuda.estado(self.escribir('c.js', self.extra), REDIRECT), 'SIN_BLOQUE')
        sin_ancla = self.sin_admin.replace(ayuda.ANCLA_COMENTARIO, '  // otra cosa', 1)
        sin_ancla = sin_ancla.replace("  if (uri !== '/' && uri.indexOf('.') === -1)", '  if (false)', 1)
        self.assertTrue(ayuda.estado(self.escribir('d.js', sin_ancla), REDIRECT).startswith('INCOMPATIBLE'))
        con_admin_propio = self.sin_admin.replace('  // Extensionless path',
                                                  "  if (uri === '/admin/otro') { return request; }\n  // Extensionless path", 1)
        self.assertTrue(ayuda.estado(self.escribir('e.js', con_admin_propio), REDIRECT).startswith('INCOMPATIBLE'))

    def test_inyectar_sobre_live_sin_bloque_reproduce_el_repo(self):
        salida = os.path.join(self.tmp, 'prop.js')
        ayuda.inyectar(self.escribir('live.js', self.sin_admin), REDIRECT, salida)
        self.assertEqual(leer_texto(salida), self.repo)

    def test_inyectar_no_quita_nada_de_live(self):
        salida = os.path.join(self.tmp, 'prop.js')
        ayuda.inyectar(self.escribir('live.js', self.extra), REDIRECT, salida)
        propuesta = leer_texto(salida)
        self.assertEqual(propuesta.replace(ayuda.bloque_admin(self.repo), '', 1), self.extra)
        self.assertIn("if (uri === '/legado')", propuesta)

    def test_verificar_acepta_solo_la_insercion_del_bloque(self):
        salida = os.path.join(self.tmp, 'prop.js')
        ayuda.inyectar(self.escribir('live.js', self.extra), REDIRECT, salida)
        self.assertEqual(ayuda.verificar(self.escribir('live2.js', self.extra), salida, REDIRECT), 0)

    def test_verificar_rechaza_una_propuesta_que_quita_una_linea_de_live(self):
        salida = os.path.join(self.tmp, 'prop.js')
        ayuda.inyectar(self.escribir('live.js', self.extra), REDIRECT, salida)
        quitada = leer_texto(salida).replace("    request.uri = '/index.html';\n", '', 1)
        propuesta = self.escribir('mala.js', quitada)
        with self.assertRaises(ValueError):
            ayuda.verificar(self.escribir('live3.js', self.extra), propuesta, REDIRECT)

    def test_verificar_rechaza_el_bloque_duplicado(self):
        bloque = ayuda.bloque_admin(self.repo)
        doble = self.repo.replace(bloque, bloque + bloque, 1)
        with self.assertRaises(ValueError):
            ayuda.verificar(self.escribir('live4.js', self.sin_admin), self.escribir('doble.js', doble), REDIRECT)

    def test_sin_comentarios_respeta_las_urls_dentro_de_cadenas(self):
        texto = "var x = 'https://ork3d.com'; // comentario\n/* bloque */ var y = \"//no es comentario\";\n"
        limpio = ayuda.sin_comentarios(texto)
        self.assertIn("'https://ork3d.com'", limpio)
        self.assertIn('"//no es comentario"', limpio)
        self.assertNotIn('// comentario', limpio)
        self.assertNotIn('bloque', limpio)

    def test_clasifica_funcional_formato_drift_y_admin(self):
        repo_sin = ayuda.sin_bloque(self.repo)
        self.assertEqual([h['categoria'] for h in ayuda.clasificar_diferencias(self.extra, repo_sin)], ['funcional'])
        formato = repo_sin.replace('  // Extensionless path', '  // Extensionless  path (otro comentario)', 1)
        self.assertEqual([h['categoria'] for h in ayuda.clasificar_diferencias(formato, repo_sin)], ['formato'])
        cambiado = repo_sin.replace("request.uri = '/opinar.html';", "request.uri = '/otro.html';", 1)
        self.assertEqual([h['categoria'] for h in ayuda.clasificar_diferencias(cambiado, repo_sin)], ['drift'])
        self.assertEqual([h['categoria'] for h in ayuda.clasificar_diferencias(self.repo, repo_sin)], ['admin'])

    def test_historial_sin_git_disponible_no_rompe(self):
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            ayuda.historial(self.escribir('live.js', self.sin_admin), RAIZ, FAKE_GIT)
        self.assertIn('historial de git no disponible', salida.getvalue())

    def test_config_funcion_solo_acepta_runtimes_conocidos(self):
        meta = self.escribir('meta.json', json.dumps({'FunctionSummary': {'FunctionConfig': {
            'Comment': 'c', 'Runtime': 'cloudfront-js-2.0'}}}))
        salida = os.path.join(self.tmp, 'cfg.json')
        llamar_ayuda('config-funcion', meta, salida)
        self.assertEqual(leer_json(salida), {'Comment': 'c', 'Runtime': 'cloudfront-js-2.0'})
        malo = self.escribir('malo.json', json.dumps({'FunctionSummary': {'FunctionConfig': {'Runtime': 'node'}}}))
        with self.assertRaises(SystemExit):
            llamar_ayuda('config-funcion', malo, os.path.join(self.tmp, 'otra.json'))

    def test_invalidacion_solo_rutas_del_panel(self):
        salida = os.path.join(self.tmp, 'inv.json')
        llamar_ayuda('invalidacion', salida, 'REF-1')
        lote = leer_json(salida)
        self.assertEqual(lote['CallerReference'], 'REF-1')
        self.assertIn('/admin/entrar', lote['Paths']['Items'])
        self.assertFalse(any(r in ('/*', '/index.html', '/opiniones*') for r in lote['Paths']['Items']))


class ReglasDeLaFuncionTest(unittest.TestCase):
    """Ejecuta el handler real (node) con LIVE, con la propuesta y con el repo. Fuera de /admin nada cambia."""

    CASOS_URI = ['/', '/index.html', '/torneos', '/torneos/', '/torneos.html', '/opinar', '/opinar/', '/opinar.html',
                 '/opinar/abc123', '/opiniones', '/opiniones.html', '/regalos-empresariales-fin-de-ano',
                 '/assets/css/estilos.css', '/robots.txt', '/sitemap.xml', '/legado', '/no-existe', '/no-existe.html',
                 '/admin', '/admin/', '/admin/api/sesion', '/admin/api/sesion/solicitar', '/admin/xyz',
                 '/admin/entrar', '/admin/entrar/', '/admin/resenas', '/admin/resenas/123']
    HOSTS = ['ork3d.com', 'www.ork3d.com']

    @classmethod
    def setUpClass(cls):
        cls.repo = leer_texto(REDIRECT)
        cls.sin_admin, cls.extra = fixtures_live(cls.repo)
        cls.tmp = tempfile.mkdtemp(prefix='frontend-node-')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def ruta(self, nombre, texto):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(texto)
        return ruta

    def ejecutar(self, funciones, casos):
        entrada = json.dumps({'funciones': funciones, 'casos': casos})
        r = subprocess.run(['node', HARNESS], input=entrada, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def casos(self, qs=None):
        return [{'host': h, 'uri': u, 'qs': qs or {}} for h in self.HOSTS for u in self.CASOS_URI]

    def propuesta(self, live_texto):
        origen = self.ruta(f'live-{uuid.uuid4().hex}.js', live_texto)
        salida = os.path.join(self.tmp, f'prop-{uuid.uuid4().hex}.js')
        ayuda.inyectar(origen, REDIRECT, salida)
        return leer_texto(salida), salida

    def test_propuesta_sobre_live_sin_bloque_es_el_repo_y_se_comporta_igual(self):
        live = self.ruta('live.js', self.sin_admin)
        _, prop = self.propuesta(self.sin_admin)
        resultados = self.ejecutar([live, prop, REDIRECT], self.casos())
        self.assertEqual(resultados[1], resultados[2], 'la propuesta equivale al repo')

    def test_fuera_de_admin_la_propuesta_responde_igual_que_live(self):
        for etiqueta, live_texto in (('base', self.sin_admin), ('con regla propia', self.extra)):
            with self.subTest(live=etiqueta):
                live = self.ruta(f'live-{etiqueta.replace(" ", "-")}.js', live_texto)
                _, prop = self.propuesta(live_texto)
                casos = self.casos()
                resultados = self.ejecutar([live, prop], casos)
                for caso, antes, despues in zip(casos, resultados[0], resultados[1]):
                    if not caso['uri'].startswith('/admin'):
                        self.assertEqual(despues, antes, caso)

    def test_regla_publica_propia_de_live_se_conserva(self):
        _, prop = self.propuesta(self.extra)
        live = self.ruta('live-extra.js', self.extra)
        casos = [{'host': 'ork3d.com', 'uri': '/legado', 'qs': {}}]
        antes, despues = self.ejecutar([live, prop], casos)
        self.assertEqual(antes[0]['uri'], '/index.html')
        self.assertEqual(despues[0]['uri'], '/index.html')

    def test_admin_se_reescribe_a_la_pagina_correcta(self):
        _, prop = self.propuesta(self.sin_admin)
        live = self.ruta('live.js', self.sin_admin)
        casos = [{'host': 'ork3d.com', 'uri': u, 'qs': {}} for u in
                 ('/admin/entrar', '/admin/resenas', '/admin/resenas/123', '/admin/api/sesion')]
        antes, despues = self.ejecutar([live, prop], casos)
        self.assertEqual([r['uri'] for r in despues],
                         ['/admin-web/entrar.html', '/admin-web/resenas.html', '/admin-web/resenas.html',
                          '/admin/api/sesion'])
        # Sin el bloque, LIVE rompía el panel y la API: el extensionless las convertía en .html.
        self.assertEqual([r['uri'] for r in antes][-1], '/admin/api/sesion.html')

    def test_www_y_query_strings_no_cambian(self):
        _, prop = self.propuesta(self.sin_admin)
        live = self.ruta('live.js', self.sin_admin)
        qs = {'estado': {'value': 'pendiente'}, 'pagina': {'value': '2'}}
        casos = self.casos(qs)
        resultados = self.ejecutar([live, prop], casos)
        for caso, antes, despues in zip(casos, resultados[0], resultados[1]):
            if not caso['uri'].startswith('/admin'):
                self.assertEqual(despues, antes, caso)
        redirecciones = [r for c, r in zip(casos, resultados[1]) if c['host'] == 'www.ork3d.com' and c['uri'] == '/torneos']
        self.assertTrue(redirecciones)
        for r in redirecciones:
            self.assertEqual(r['statusCode'], 301)
            self.assertTrue(r['headers']['location']['value'].startswith('https://ork3d.com/torneos'))


class FrontendDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='frontend-deploy-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.curl_req = os.path.join(self.tmp, 'curl.jsonl')
        self.rb = os.path.join(self.tmp, 'rb')
        self.sitio = os.path.join(self.tmp, 'sitio')
        self.repo = leer_texto(REDIRECT)
        self.sin_admin, self.extra = fixtures_live(self.repo)
        self.fijar_live(self.sin_admin)
        # Lo que CloudFront serviría tras publicar: las mismas páginas del repo.
        for relativa in PAGINAS + ['index.html', 'opiniones.html']:
            destino = os.path.join(self.sitio, relativa)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            origen = os.path.join(RAIZ, relativa)
            if os.path.exists(origen):
                shutil.copyfile(origen, destino)
            else:
                with open(destino, 'w', encoding='utf-8') as f:
                    f.write('<meta name="robots" content="noindex, follow">')

    def fijar_live(self, codigo):
        st = fake.estado_inicial()
        st['fn'] = {'etag': 'E-LIVE-0', 'codigo': codigo}
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def estado_actual(self):
        return leer_json(self.estado)

    def correr(self, extra=None, *, permitir=True):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE_AWS, 'CURL_CMD': FAKE_CURL, 'GIT_CMD': FAKE_GIT,
               'FAKE_ESTADO': self.estado, 'FAKE_LOG': self.log, 'FAKE_CURL_REQ': self.curl_req,
               'FAKE_SITIO_DIR': self.sitio, 'ADMIN_DEPLOY_RUTA': self.rb, 'FRONT_REINTENTOS': '1',
               'FRONT_ESPERA': '0', 'FAKE_GIT_RAMA': 'claude/ork3d-landing-page-b2fQr'}
        if permitir:
            env['ALLOW_ADMIN_FRONTEND'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', SCRIPT], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=300)

    def mutaciones(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            lineas = [l.strip() for l in f if l.strip()]
        return [' '.join(l.split()[:2]) for l in lineas if any(l.startswith(m) for m in MUTACIONES)]

    def limpiar_log(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def copias(self):
        carpeta = os.path.join(self.rb, 'rollback')
        if not os.path.isdir(carpeta):
            return {}
        contenidos = {}
        for n in os.listdir(carpeta):
            with open(os.path.join(carpeta, n), 'rb') as f:
                contenidos[n] = f.read()
        return contenidos

    def test_sin_flag_aborta_con_3_sin_tocar_aws_ni_red(self):
        r = self.correr(permitir=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.log))
        self.assertFalse(os.path.exists(self.curl_req))

    def test_primera_publicacion_publica_live_mas_bloque_y_pasa_el_smoke(self):
        r = self.correr()
        visible = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, visible)
        self.assertIn('CHECKPOINT F2', visible)
        self.assertIn('CHECKPOINT F3', visible)
        self.assertIn('LIVE + bloque admin', visible)
        st = self.estado_actual()
        self.assertEqual(st['fn']['codigo'], self.repo, 'LIVE sin bloque + bloque = repo en este fixture')
        self.assertEqual(sorted(st['sitio']), sorted(PAGINAS), 'solo las cinco claves del panel')
        for clave in PAGINAS:
            self.assertEqual(st['sitio'][clave], leer_texto(os.path.join(RAIZ, clave)), clave)
        self.assertEqual(st['invalidaciones'], [['/admin/entrar', '/admin/resenas', '/admin/resenas/*',
                                                  '/admin-web/*', '/assets/js/admin-*']])
        copias = self.copias()
        funcion = [n for n in copias if n.startswith('cloudfront-funcion-admin-LIVE.')]
        self.assertEqual(len(funcion), 1)
        self.assertEqual(copias[funcion[0]].decode('utf-8'), self.sin_admin)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.rb, 'rollback', funcion[0])).st_mode), 0o600)
        propuesta = [n for n in copias if n.startswith('cloudfront-funcion-admin-propuesta.')]
        self.assertEqual(len(propuesta), 1)
        self.assertFalse(any(n.startswith('s3-previo-') for n in copias))
        self.assertNotIn('X-Amz-Signature', visible)

    def test_live_con_regla_publica_propia_se_conserva_al_publicar(self):
        self.fijar_live(self.extra)
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        codigo = self.estado_actual()['fn']['codigo']
        self.assertIn("if (uri === '/legado')", codigo, 'la regla pública de LIVE sigue')
        self.assertIn('// Panel admin V2.', codigo, 'y el bloque admin está')
        self.assertEqual(codigo.replace(ayuda.bloque_admin(self.repo), '', 1), self.extra)
        self.assertIn('funcional pública preexistente (solo en LIVE: se conserva): 1', r.stdout)

    def test_live_sin_punto_de_insercion_aborta_con_4_sin_escribir(self):
        self.fijar_live(self.sin_admin.replace("  if (uri !== '/' && uri.indexOf('.') === -1)", '  if (false)', 1)
                        .replace('  // Extensionless path', '  // otra cosa', 1))
        r = self.correr()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn('no admite el bloque admin', r.stdout + r.stderr)
        self.assertEqual(self.mutaciones(), [])

    def test_publicacion_repetida_no_escribe_la_funcion(self):
        self.assertEqual(self.correr().returncode, 0)
        self.limpiar_log()
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('ya contiene el bloque admin', r.stdout)
        self.assertNotIn('cloudfront update-function', self.mutaciones())
        self.assertNotIn('cloudfront publish-function', self.mutaciones())

    def test_arbol_sucio_no_escribe(self):
        r = self.correr({'FAKE_GIT_SUCIO': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.mutaciones(), [])

    def test_diagnostico_solo_lee_y_deja_el_diff_completo(self):
        self.fijar_live(self.extra)
        r = self.correr({'FRONT_SOLO_DIAGNOSTICO': '1'}, permitir=False)
        visible = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, visible)
        self.assertIn('diferencias LIVE vs repo', visible)
        self.assertIn('Diagnóstico de solo lectura', visible)
        self.assertEqual(self.mutaciones(), [])
        diff = [n for n in self.copias() if n.startswith('cloudfront-funcion-admin-diferencias.')]
        self.assertEqual(len(diff), 1)
        self.assertIn('== Semántico', self.copias()[diff[0]].decode('utf-8'))

    def test_smoke_sin_pagina_publicada_sale_con_6(self):
        os.remove(os.path.join(self.sitio, 'admin-web', 'entrar.html'))
        r = self.correr()
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn('/admin/entrar', r.stdout + r.stderr)
        self.assertNotIn('CHECKPOINT F3', r.stdout)

    def test_smoke_comprueba_no_store_y_la_api_401(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('no-store', r.stdout)
        self.assertIn('/admin/api/sesion: 401 de la API', r.stdout)
        self.assertIn('/: 200', r.stdout)


if __name__ == '__main__':
    unittest.main()
