"""Pruebas de la publicación del panel admin (infra/admin-frontend-deploy.sh y admin_frontend_ayuda.py).

Sin AWS, sin red, sin credenciales: aws_fake_admin.py guarda el estado (función CloudFront, objetos del sitio,
invalidaciones), aws_fake_curl.py sirve el sitio desde FAKE_SITIO_DIR y aws_fake_git.py responde la guarda de git.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'admin-frontend-deploy.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'admin_frontend_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
FAKE_GIT = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_git.py')
REDIRECT = os.path.join(RAIZ, 'infra', 'redirect-www.js')
PAGINAS = ['admin-web/entrar.html', 'admin-web/resenas.html', 'assets/js/admin-comun.js',
           'assets/js/admin-entrar.js', 'assets/js/admin-panel.js']
MUTACIONES = ('cloudfront update-function', 'cloudfront publish-function', 'cloudfront create-invalidation', 's3 cp')


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


class AyudaFrontendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='frontend-ayuda-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = leer_texto(REDIRECT)
        self.sin_admin = ayuda.sin_bloque(self.repo)

    def escribir(self, nombre, texto):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write(texto)
        return ruta

    def test_repo_tiene_bloque_admin_completo(self):
        self.assertEqual(llamar_ayuda('bloque-admin', REDIRECT), '')
        for regla in ('/admin-web/resenas.html', '/admin-web/entrar.html', '/admin/api/'):
            self.assertIn(regla, self.repo)

    def test_funcion_igual_al_repo(self):
        self.assertEqual(llamar_ayuda('funcion', self.escribir('live.js', self.repo), REDIRECT), 'IGUAL')

    def test_funcion_sin_bloque_admin_se_puede_actualizar(self):
        self.assertEqual(llamar_ayuda('funcion', self.escribir('live.js', self.sin_admin), REDIRECT), 'ACTUALIZAR')

    def test_funcion_con_otro_cambio_no_se_toca(self):
        otra = self.sin_admin + '\n// cambio ajeno al panel\n'
        self.assertEqual(llamar_ayuda('funcion', self.escribir('live.js', otra), REDIRECT), 'DISTINTO')

    def test_bloque_admin_incompleto_falla(self):
        roto = self.repo.replace('/admin-web/entrar.html', '/otra.html')
        with self.assertRaises(ValueError):
            ayuda.sin_bloque(roto)

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
        self.sin_admin = ayuda.sin_bloque(self.repo)
        st = fake.estado_inicial()
        st['fn'] = {'etag': 'E-LIVE-0', 'codigo': self.sin_admin}
        self.guardar_estado(st)
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

    def guardar_estado(self, st):
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def estado_actual(self):
        with open(self.estado, encoding='utf-8') as f:
            return json.load(f)

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

    def test_primera_publicacion_actualiza_funcion_sube_paginas_y_pasa_el_smoke(self):
        r = self.correr()
        visible = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, visible)
        self.assertIn('CHECKPOINT F2', visible)
        self.assertIn('CHECKPOINT F3', visible)
        st = self.estado_actual()
        self.assertEqual(st['fn']['codigo'], self.repo, 'la función LIVE debe quedar igual al repo')
        self.assertEqual(sorted(st['sitio']), sorted(PAGINAS), 'solo las cinco claves del panel')
        for clave in PAGINAS:
            self.assertEqual(st['sitio'][clave], leer_texto(os.path.join(RAIZ, clave)), clave)
        self.assertEqual(st['invalidaciones'], [['/admin/entrar', '/admin/resenas', '/admin/resenas/*',
                                                  '/admin-web/*', '/assets/js/admin-*']])
        # Rollback: la función LIVE anterior (600), y ninguna página previa porque no existían.
        copias = self.copias()
        funcion = [n for n in copias if n.startswith('cloudfront-funcion-admin-LIVE.')]
        self.assertEqual(len(funcion), 1)
        self.assertEqual(copias[funcion[0]].decode('utf-8'), self.sin_admin)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.rb, 'rollback', funcion[0])).st_mode), 0o600)
        self.assertFalse(any(n.startswith('s3-previo-') for n in copias))
        self.assertNotIn('X-Amz-Signature', visible)

    def test_publicacion_repetida_no_vuelve_a_publicar_la_funcion(self):
        self.assertEqual(self.correr().returncode, 0)
        self.limpiar_log()
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('LIVE = repo: sin cambios', r.stdout)
        self.assertNotIn('cloudfront update-function', self.mutaciones())
        self.assertNotIn('cloudfront publish-function', self.mutaciones())

    def test_funcion_distinta_del_repo_aborta_con_4_sin_escribir_nada(self):
        def cambiar(st):
            st['fn']['codigo'] = self.sin_admin.replace('return request;', 'return request; // otro cambio', 1)
        st = self.estado_actual()
        cambiar(st)
        self.guardar_estado(st)
        self.limpiar_log()
        r = self.correr()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.mutaciones(), [])

    def test_arbol_sucio_no_escribe(self):
        r = self.correr({'FAKE_GIT_SUCIO': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.mutaciones(), [])

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
