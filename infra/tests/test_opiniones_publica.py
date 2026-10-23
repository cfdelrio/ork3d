"""Pruebas del lanzamiento público de /opiniones: HTML, sitemap, enlaces, export, scripts y publicación acotada.

Sin AWS, sin red y sin credenciales: aws_fake_sitio.py simula S3 y CloudFront; un git falso responde el estado del repo.
"""
import base64
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PUBLICAR = os.path.join(RAIZ, 'infra', 'opiniones-publicar.sh')
VERIFICAR = os.path.join(RAIZ, 'infra', 'opiniones-verify.sh')
NAVEGADOR = os.path.join(RAIZ, 'infra', 'opiniones-navegador.js')
MANIFIESTO = os.path.join(RAIZ, 'infra', 'opiniones-archivos.tsv')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_sitio.py')
ESPERADOS = {'opiniones.html', 'index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html',
             'sitemap.xml', 'assets/css/opiniones.css', 'assets/js/resenas-publicas.js'}
PRIVADOS = ('order_ref', 'contact', 'private_feedback', 'token_hash', 'permission_to_publish', 'claimed_at')
PATRON_PRIVADO = re.compile(r'\b(' + '|'.join(PRIVADOS) + r')\b')   # palabra completa: 'contacto' en prosa no cuenta


def leer(*partes):
    with open(os.path.join(RAIZ, *partes), encoding='utf-8') as f:
        return f.read()


def manifiesto():
    filas = []
    with open(MANIFIESTO, encoding='utf-8') as f:
        for linea in f:
            if linea.startswith('#') or not linea.strip():
                continue
            archivo, clave, ct, cc, rutas = linea.rstrip('\n').split('\t')
            filas.append({'archivo': archivo, 'clave': clave, 'ct': ct, 'cc': cc, 'rutas': rutas.split(',')})
    return filas


class HtmlPaginaTest(unittest.TestCase):
    def setUp(self):
        self.html = leer('opiniones.html')

    def test_indexable_sin_meta_robots_como_el_resto_del_sitio(self):
        self.assertNotIn('noindex', self.html)
        self.assertNotRegex(self.html, r'<meta name="robots"')

    def test_canonical_exacto_sin_querystring(self):
        self.assertIn('<link rel="canonical" href="https://ork3d.com/opiniones" />', self.html)
        self.assertNotRegex(self.html, r'canonical[^>]*\?')

    def test_title_description_y_og(self):
        self.assertIn('<title>Opiniones de clientes | ORK3D</title>', self.html)
        desc = re.search(r'<meta name="description" content="([^"]*)"', self.html).group(1)
        self.assertTrue(80 <= len(desc) <= 170, len(desc))
        self.assertIn('og:title" content="Opiniones de clientes | ORK3D"', self.html)
        self.assertIn('og:url" content="https://ork3d.com/opiniones"', self.html)
        self.assertIn('og:image" content="https://ork3d.com/assets/img/og-cover.svg"', self.html)

    def test_un_h1_y_landmarks(self):
        self.assertEqual(len(re.findall(r'<h1[ >]', self.html)), 1)
        self.assertIn('<html lang="es">', self.html)
        for etiqueta in ('<header class="nav"', '<main class="opiniones" id="contenido"', '<footer class="footer">',
                         '<nav class="nav__links"'):
            self.assertIn(etiqueta, self.html)

    def test_skip_link_al_contenido(self):
        self.assertIn('href="#contenido"', self.html)
        self.assertIn('id="contenido"', self.html)

    def test_sin_datos_estructurados_ni_schema(self):
        # Decisión registrada en docs/resenas/OPINIONES-PUBLICA.md: no se agregan Product/AggregateRating/Review.
        self.assertNotIn('application/ld+json', self.html)
        self.assertNotIn('schema.org', self.html)

    def test_estado_inicial_visible_y_noscript(self):
        self.assertIn('role="status"', self.html)
        self.assertIn('Cargando opiniones', self.html)
        self.assertIn('<noscript>', self.html)

    def test_cta_en_la_cabecera_no_debajo_de_la_lista(self):
        lista = self.html.index('id="resenas-lista"')
        self.assertLess(self.html.index('data-wa-source="opiniones_cabecera"'), lista)
        self.assertNotIn('opiniones__cierre', self.html)

    def test_sin_campos_privados_en_la_pagina(self):
        self.assertIsNone(PATRON_PRIVADO.search(self.html))


class EnlacesYSitemapTest(unittest.TestCase):
    def test_home_torneos_y_regalos_enlazan_en_el_footer(self):
        for archivo in ('index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'opiniones.html'):
            with self.subTest(archivo=archivo):
                texto = leer(archivo)
                footer = texto[texto.index('<div class="footer__links">'):]
                footer = footer[:footer.index('</div>')]
                self.assertIn('<a href="/opiniones">Opiniones</a>', footer)

    def test_sitemap_lista_opiniones_sin_lastmod_inventado(self):
        arbol = ET.parse(os.path.join(RAIZ, 'sitemap.xml')).getroot()
        ns = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        urls = {u.find('s:loc', ns).text: u for u in arbol.findall('s:url', ns)}
        self.assertIn('https://ork3d.com/opiniones', urls)
        entrada = urls['https://ork3d.com/opiniones']
        self.assertIsNone(entrada.find('s:lastmod', ns))
        self.assertEqual(entrada.find('s:changefreq', ns).text, 'weekly')
        self.assertEqual(entrada.find('s:priority', ns).text, '0.7')
        self.assertIn('https://ork3d.com/', urls)


class CodigoPublicoTest(unittest.TestCase):
    def test_js_solo_usa_texto_plano_y_no_html_dinamico(self):
        js = leer('assets', 'js', 'resenas-publicas.js')
        self.assertNotIn('innerHTML', js)
        self.assertNotIn('insertAdjacentHTML', js)
        self.assertNotIn('eval(', js)
        self.assertIn('textContent', js)

    def test_js_y_css_no_piden_campos_privados(self):
        for archivo in (('assets', 'js', 'resenas-publicas.js'), ('assets', 'css', 'opiniones.css')):
            self.assertIsNone(PATRON_PRIVADO.search(leer(*archivo)), archivo)

    def test_presupuesto_de_peso(self):
        for archivo in (('assets', 'css', 'opiniones.css'), ('assets', 'js', 'resenas-publicas.js'), ('opiniones.html',)):
            self.assertLess(os.path.getsize(os.path.join(RAIZ, *archivo)), 16 * 1024, archivo)

    def test_no_se_agregaron_dependencias(self):
        html = leer('opiniones.html')
        self.assertEqual(sorted(re.findall(r'<script src="([^"]+)"', html)),
                         sorted(['/config.js', '/assets/js/main.js?v=2', '/assets/js/resenas-publicas.js?v=2']))
        self.assertNotRegex(html, r'<script(?![^>]*src=)')


class ManifiestoTest(unittest.TestCase):
    def test_exactamente_los_archivos_del_lanzamiento(self):
        self.assertEqual({m['archivo'] for m in manifiesto()}, ESPERADOS)

    def test_nada_de_datos_ni_backend_ni_admin(self):
        for m in manifiesto():
            for prohibido in ('resenas/', 'api/', 'admin', 'infra/', 'dev/', 'docs/'):
                self.assertFalse(m['archivo'].startswith(prohibido), m['archivo'])

    def test_todos_los_archivos_existen_y_tienen_tipo_y_cache(self):
        for m in manifiesto():
            self.assertTrue(os.path.isfile(os.path.join(RAIZ, m['archivo'])), m['archivo'])
            self.assertTrue(m['ct'].startswith(('text/', 'application/')), m['ct'])
            self.assertTrue(m['cc'].startswith('public, max-age='), m['cc'])

    def test_cada_html_invalida_su_ruta_limpia_y_su_html(self):
        for m in manifiesto():
            if m['archivo'].endswith('.html'):
                limpia = m['archivo'][:-5] if m['archivo'] != 'index.html' else '/'
                rutas = m['rutas']
                if m['archivo'] == 'index.html':
                    self.assertEqual(rutas, ['/', '/index.html'])
                else:
                    self.assertIn('/' + limpia, rutas)
                    self.assertIn('/' + m['archivo'], rutas)


class ScriptsTest(unittest.TestCase):
    def setUp(self):
        self.publicar = leer('infra', 'opiniones-publicar.sh')
        self.verificar = leer('infra', 'opiniones-verify.sh')
        self.navegador = leer('infra', 'opiniones-navegador.js')

    def codigo(self, texto):
        return '\n'.join(l for l in texto.splitlines() if not l.lstrip().startswith('#'))

    def test_publicar_no_hace_sync_ni_borrados_ni_scripts_peligrosos(self):
        codigo = self.codigo(self.publicar)
        for prohibido in ('s3 sync', '--delete', 'setup.sh', 'resenas-setup', 'deploy.sh', 'update-distribution',
                          'update-function', 'put-role-policy', 'create-function', 's3 rm'):
            self.assertNotIn(prohibido, codigo, prohibido)

    def test_ninguna_escritura_apunta_a_resenas(self):
        self.assertIsNone(re.search(r's3://\$BUCKET/resenas', self.codigo(self.publicar)))
        self.assertIsNone(re.search(r's3 cp[^\n]*resenas/', self.codigo(self.publicar)))

    def test_publicar_solo_usa_las_operaciones_necesarias(self):
        usadas = set(re.findall(r'"\$AWS" ([a-z0-9-]+ [a-z0-9-]+)', self.publicar))
        self.assertEqual(usadas, {'s3api head-object', 's3 cp', 'cloudfront create-invalidation',
                                  'cloudfront wait'}, usadas)

    def test_verificar_es_solo_lectura(self):
        codigo = self.codigo(self.verificar)
        for escritura in ('-X POST', '-X PUT', 'update-', 'put-', 'create-', 'delete-', 's3 cp', 'publicar'):
            self.assertNotIn(escritura, codigo, escritura)

    def test_verificar_exige_el_id_de_la_reseña_de_prueba(self):
        r = subprocess.run(['bash', VERIFICAR, 'post'], env={'PATH': os.environ.get('PATH', '')},
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('SMOKE_NOTIFY_ID', r.stderr)

    def test_navegador_solo_lee(self):
        self.assertNotIn("method: 'POST'", self.navegador)
        self.assertNotIn('route(', self.navegador)
        self.assertNotIn('fill(', self.navegador)
        self.assertNotIn('click(', self.navegador)


class ExportPublicoTest(unittest.TestCase):
    """La lógica real del export (api/resenas/nucleo/publicacion.py): solo aprobadas, orden, allowlist."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.join(RAIZ, 'api', 'resenas'))
        from nucleo import publicacion
        cls.pub = publicacion

    def resena(self, n, **cambios):
        base = {'id': f'{n:032x}', 'rating': 5, 'comment': f'texto {n}', 'aspects': ['calidad'],
                'display_name': f'Cliente {n}', 'category': 'trofeos', 'product_label': 'Trofeo',
                'verified': True, 'published_at': f'2026-10-{n:02d}T10:00:00Z', 'status': 'approved',
                'permission_to_publish': True, 'order_ref': 'PEDIDO-X', 'contact': 'x@example.test',
                'private_feedback': 'FB', 'token_hash': 'h' * 64}
        base.update(cambios)
        return base

    def test_solo_aprobadas_con_permiso_y_sin_campos_privados(self):
        resenas = [self.resena(1), self.resena(2, status='hidden'), self.resena(3, status='rejected'),
                   self.resena(4, status='pending'), self.resena(5, permission_to_publish=False)]
        export = self.pub.construir_export(resenas, '2026-10-22T12:00:00Z')
        self.assertEqual([i['id'] for i in export['items']], [f'{1:032x}'])
        self.assertEqual(export['count'], 1)
        self.assertEqual(set(export['items'][0]), set(self.pub.ALLOWLIST_EXPORT))
        for privado in PRIVADOS:
            self.assertNotIn(privado, json.dumps(export))

    def test_ocultas_y_rechazadas_quedan_fuera_aunque_sean_smoke(self):
        smoke = ['a' * 32, 'b' * 32]
        resenas = [self.resena(1, id=smoke[0], status='hidden'), self.resena(2, id=smoke[1], status='rejected'),
                   self.resena(3)]
        ids = {i['id'] for i in self.pub.construir_export(resenas, 'x')['items']}
        self.assertNotIn(smoke[0], ids)
        self.assertNotIn(smoke[1], ids)

    def test_orden_mas_recientes_primero_sin_ranking_por_rating(self):
        resenas = [self.resena(1, rating=5), self.resena(5, rating=1), self.resena(3, rating=5)]
        fechas = [i['published_at'] for i in self.pub.construir_export(resenas, 'x')['items']]
        self.assertEqual(fechas, sorted(fechas, reverse=True))

    def test_foto_publica_solo_si_hay_clave_publica(self):
        con = self.pub.construir_export([self.resena(1, photo_public_key='resenas/fotos/x.jpg')], 'x')['items'][0]
        sin = self.pub.construir_export([self.resena(2)], 'x')['items'][0]
        self.assertEqual(con['photo_url'], f'/resenas/fotos/{1:032x}.jpg')
        self.assertIsNone(sin['photo_url'])


class PublicacionFakeTest(unittest.TestCase):
    """publicar y revertir contra el simulador: sube solo el manifiesto, guarda y restaura lo anterior."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='opiniones-pub-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.ruta = os.path.join(self.tmp, 'ruta')
        self.git_head = os.path.join(self.tmp, 'git-head')
        self.git = os.path.join(self.tmp, 'git')
        with open(self.git, 'w', encoding='utf-8') as f:
            f.write('#!/bin/bash\n'
                    'case "$*" in\n'
                    '  *status*) [ -n "$FAKE_GIT_SUCIO" ] && echo " M algo" ;;\n'
                    '  *"rev-parse origin/"*) echo "$FAKE_GIT_ORIGIN" ;;\n'
                    '  *"rev-parse HEAD"*) echo "$FAKE_GIT_HEAD" ;;\n'
                    'esac\n')
        os.chmod(self.git, 0o755)
        self.previo = {}
        objetos = {}
        for m in manifiesto():
            contenido = f'version anterior de {m["archivo"]}\n'.encode('utf-8')
            objetos[m['clave']] = {'b64': base64.b64encode(contenido).decode('ascii'),
                                   'ct': 'text/html; charset=utf-8', 'cc': 'public, max-age=60'}
            self.previo[m['clave']] = contenido
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump({'objetos': objetos, 'invalidaciones': []}, f)

    def correr(self, modo, extra=None, guardia=True):
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp, 'AWS_CLI': FAKE_AWS, 'GIT_CMD': self.git,
               'FAKE_ESTADO': self.estado, 'FAKE_LOG': self.log, 'OPINIONES_RUTA': self.ruta,
               'FAKE_GIT_HEAD': 'abc123', 'FAKE_GIT_ORIGIN': 'abc123'}
        if guardia and modo in ('publicar', 'revertir'):
            env['ALLOW_OPINIONES_PUBLISH'] = '1'
        if guardia and modo == 'publicar':
            # Estas pruebas publican a propósito sobre /opiniones ya lanzada. La marca explícita de republicación tiene
            # sus propias pruebas en infra/tests/test_cierre_main.py.
            env['OPINIONES_REPUBLICAR'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', PUBLICAR, modo], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=120)

    def estado_actual(self):
        with open(self.estado, encoding='utf-8') as f:
            return json.load(f)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]

    def subidas(self):
        return [l.split(' ', 3)[3] for l in self.llamadas() if l.startswith('s3 cp subida ')]

    def test_diff_lista_los_archivos_y_no_escribe(self):
        r = self.correr('diff', guardia=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for m in manifiesto():
            self.assertIn(m['archivo'], r.stdout)
        self.assertNotIn('subida', ' '.join(self.llamadas()))
        self.assertNotIn('create-invalidation', ' '.join(self.llamadas()))

    def test_publicar_sin_guardia_no_llama_a_aws(self):
        r = self.correr('publicar', guardia=False)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_repo_con_cambios_sin_commitear_no_publica(self):
        r = self.correr('publicar', extra={'FAKE_GIT_SUCIO': '1'})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn('cambios sin commitear', r.stderr)
        self.assertEqual(self.subidas(), [])

    def test_head_distinto_de_origin_no_publica(self):
        r = self.correr('publicar', extra={'FAKE_GIT_ORIGIN': 'otro999'})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.subidas(), [])

    def test_publicar_sube_exactamente_el_manifiesto_con_sus_cabeceras(self):
        r = self.correr('publicar')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        claves = [m['clave'] for m in manifiesto()]
        self.assertEqual(self.subidas(), claves)
        objetos = self.estado_actual()['objetos']
        for m in manifiesto():
            with open(os.path.join(RAIZ, m['archivo']), 'rb') as f:
                self.assertEqual(base64.b64decode(objetos[m['clave']]['b64']), f.read(), m['archivo'])
            self.assertEqual(objetos[m['clave']]['ct'], m['ct'])
            self.assertEqual(objetos[m['clave']]['cc'], m['cc'])

    def test_invalida_solo_las_rutas_del_manifiesto(self):
        self.correr('publicar')
        invalidacion = self.estado_actual()['invalidaciones'][-1]
        esperadas = sorted({ruta for m in manifiesto() for ruta in m['rutas']})
        self.assertEqual(sorted(invalidacion['rutas']), esperadas)
        self.assertEqual(invalidacion['dist'], 'ECOW0H6A48HVP')
        self.assertFalse(any(ruta.startswith('/resenas') for ruta in invalidacion['rutas']))

    def test_no_toca_nada_fuera_del_manifiesto(self):
        self.correr('publicar')
        claves = {m['clave'] for m in manifiesto()}
        for linea in self.llamadas():
            if linea.startswith('s3 cp subida '):
                self.assertIn(linea.split(' ', 3)[3], claves)

    def test_guarda_copia_privada_con_objetos_y_cabeceras_anteriores(self):
        self.correr('publicar')
        (copia,) = glob.glob(os.path.join(self.ruta, 'rollback', '*', ''))
        cab = open(os.path.join(copia, 'cabeceras.tsv'), encoding='utf-8').read().splitlines()
        self.assertEqual(len(cab), 7)
        for linea in cab:
            clave, ct, cc, existia = linea.split('\t')
            self.assertEqual(existia, 'si')
            self.assertTrue(ct)
            ruta = os.path.join(copia, 'objetos', clave)
            with open(ruta, 'rb') as f:
                self.assertEqual(f.read(), self.previo[clave])
            self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(copia, 'cabeceras.tsv')).st_mode), 0o600)

    def test_revertir_restaura_bytes_y_cabeceras_exactas(self):
        self.correr('publicar')
        r = self.correr('revertir')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        objetos = self.estado_actual()['objetos']
        for clave, previo in self.previo.items():
            self.assertEqual(base64.b64decode(objetos[clave]['b64']), previo, clave)
            self.assertEqual(objetos[clave]['ct'], 'text/html; charset=utf-8')
            self.assertEqual(objetos[clave]['cc'], 'public, max-age=60')
        self.assertEqual(len(self.estado_actual()['invalidaciones']), 2)

    def test_revertir_sin_guardia_no_escribe(self):
        self.correr('publicar')
        antes = len(self.llamadas())
        r = self.correr('revertir', guardia=False)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(len(self.llamadas()), antes)

    def test_revertir_sin_copia_falla_sin_escribir(self):
        r = self.correr('revertir')
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual(self.subidas(), [])

    def test_la_salida_no_muestra_contenido_ni_urls_firmadas(self):
        r = self.correr('publicar')
        salida = r.stdout + r.stderr
        self.assertNotIn('version anterior', salida)
        self.assertNotIn('X-Amz', salida)


if __name__ == '__main__':
    unittest.main()
