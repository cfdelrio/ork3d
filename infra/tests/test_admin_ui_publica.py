"""Pruebas del despliegue de los estáticos del panel admin (infra/admin-ui-publicar.sh, admin-ui-verify.sh,
admin-ui-comun.sh, admin_ui_ayuda.py, admin-ui-navegador.js y admin-ui-archivos.tsv).

Sin AWS, sin red y sin credenciales. Cada prueba de publicación o verificación trabaja en un clon git real, creado desde
un origen bare de este repo, con el tooling committeado y pusheado. Así las guardas (repo limpio, HEAD = origin,
contenido = 590ed7d, manifiesto = intersección del diff) se prueban con git de verdad. aws_fake_admin_ui.py guarda el
S3 y registra cada llamada; aws_fake_curl_admin_ui.py sirve el sitio desde un directorio que la prueba controla.
"""
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
INFRA = os.path.join(RAIZ, 'infra')
PUBLICAR = os.path.join(INFRA, 'admin-ui-publicar.sh')
VERIFICAR = os.path.join(INFRA, 'admin-ui-verify.sh')
COMUN = os.path.join(INFRA, 'admin-ui-comun.sh')
NAVEGADOR = os.path.join(INFRA, 'admin-ui-navegador.js')
AYUDA = os.path.join(INFRA, 'admin_ui_ayuda.py')
MANIFIESTO = os.path.join(INFRA, 'admin-ui-archivos.tsv')
FAKE_AWS = os.path.join(INFRA, 'tests', 'aws_fake_admin_ui.py')
FAKE_CURL = os.path.join(INFRA, 'tests', 'aws_fake_curl_admin_ui.py')

REVISION = '590ed7df180202390fda8a523b712b1c4e04c8cf'
BASE = 'e8052cde6e89a11595c15e7e13a299c51bc1ac7a'
RAMA = 'main'
SMOKE = '633729550934477689fafb8f576dd851'
ESTATICOS = ['admin-web/entrar.html', 'admin-web/resenas.html', 'assets/css/admin.css',
             'assets/js/admin-comun.js', 'assets/js/admin-panel.js']
TOOLING = ['infra/admin-ui-publicar.sh', 'infra/admin-ui-verify.sh', 'infra/admin-ui-comun.sh',
           'infra/admin_ui_ayuda.py', 'infra/admin-ui-archivos.tsv', 'infra/admin-ui-navegador.js']
RUTAS_ESPERADAS = sorted([
    '/admin-web/entrar.html', '/admin-web/resenas.html', '/admin/entrar', '/admin/entrar.html',
    '/admin/resenas', '/admin/resenas.html', '/admin/resenas/*', '/assets/css/admin.css',
    '/assets/js/admin-comun.js', '/assets/js/admin-panel.js'])
CT_HTML = 'text/html; charset=utf-8'
CT_JS = 'application/javascript; charset=utf-8'
CT_CSS = 'text/css; charset=utf-8'
NO_STORE = 'no-store, max-age=0'


def leer(ruta, modo='r'):
    with open(ruta, modo, **({} if 'b' in modo else {'encoding': 'utf-8'})) as f:
        return f.read()


def codigo_sin_comentarios(ruta):
    """Líneas de código: sin comentarios de shell, JS ni Python. Así los avisos "no usa --delete" no cuentan."""
    salida = []
    for linea in leer(ruta).splitlines():
        s = linea.strip()
        if s.startswith(('#', '//', '/*', '*', '"""')) or not s:
            continue
        salida.append(linea)
    return '\n'.join(salida)


def git(args, cwd, check=True):
    return subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, check=check)


def blob(revision, ruta):
    return subprocess.run(['git', 'show', f'{revision}:{ruta}'], cwd=RAIZ, capture_output=True, check=True).stdout


def sha(datos):
    import hashlib
    return hashlib.sha256(datos).hexdigest()


def ayuda(*args, entrada=None):
    return subprocess.run(['python3', AYUDA, *args], input=entrada, capture_output=True, text=True)


class Entorno(unittest.TestCase):
    """Origen bare + clon con el tooling pusheado + envoltorios de aws y curl + estado S3 y sitio controlados."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='admin-ui-prueba-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origen = os.path.join(self.tmp, 'origin.git')
        subprocess.run(['git', 'clone', '--bare', '-q', RAIZ, origen], check=True, capture_output=True)
        self.clon = os.path.join(self.tmp, 'clon')
        subprocess.run(['git', 'clone', '-q', origen, self.clon], check=True, capture_output=True)
        git(['config', 'user.email', 'prueba@example.test'], self.clon)
        git(['config', 'user.name', 'prueba'], self.clon)
        for ruta in TOOLING:
            destino = os.path.join(self.clon, ruta)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copyfile(os.path.join(RAIZ, ruta), destino)
            if ruta.endswith(('.sh', '.py', '.js')):
                os.chmod(destino, 0o755)
        self.commit_y_push('prueba: tooling de admin-ui')

        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        for nombre, fake in (('aws', FAKE_AWS), ('curl', FAKE_CURL)):
            envoltorio = os.path.join(self.bin, nombre)
            with open(envoltorio, 'w', encoding='utf-8') as f:
                f.write(f'#!/bin/sh\nexec python3 "{fake}" "$@"\n')
            os.chmod(envoltorio, 0o755)
        # Una sola raíz: los fakes guardan el bucket en <estado>/s3/<clave>, y la prueba lo siembra y lee en el mismo sitio.
        self.estado = os.path.join(self.tmp, 'estado')
        self.s3 = os.path.join(self.estado, 's3')
        self.sitio = os.path.join(self.tmp, 'sitio')
        self.rutas = os.path.join(self.tmp, 'rutas')
        self.log_aws = os.path.join(self.tmp, 'aws.log')
        self.log_curl = os.path.join(self.tmp, 'curl.log')
        os.makedirs(self.s3)

    def commit_y_push(self, mensaje, push=True):
        git(['add', 'infra'], self.clon)
        if git(['status', '--porcelain'], self.clon).stdout.strip():
            git(['commit', '-q', '-m', mensaje], self.clon)
            if push:
                git(['push', '-q', 'origin', RAMA], self.clon)

    def entorno(self, extra=None):
        env = dict(os.environ)
        env.update({
            'AWS_CLI': os.path.join(self.bin, 'aws'),
            'CURL_CMD': os.path.join(self.bin, 'curl'),
            'FAKE_ADMIN_UI_ESTADO': self.estado,
            'FAKE_ADMIN_UI_LOG': self.log_aws,
            'FAKE_ADMIN_UI_CURL_LOG': self.log_curl,
            'FAKE_ADMIN_UI_SITIO': self.sitio,
            'ADMIN_UI_RUTA': self.rutas,
            'ADMIN_UI_ESPERA': '0',
            'PLAYWRIGHT_PATH': '/nonexistent/playwright',
            'SMOKE_NOTIFY_ID': SMOKE,
        })
        env.pop('ALLOW_ADMIN_UI_PUBLISH', None)
        env.update(extra or {})
        return env

    def correr(self, script, *args, extra=None):
        return subprocess.run(['bash', os.path.join(self.clon, 'infra', os.path.basename(script)), *args],
                              env=self.entorno(extra), cwd=self.clon, capture_output=True, text=True, timeout=600)

    def llamadas_aws(self):
        if not os.path.exists(self.log_aws):
            return []
        return [json.loads(linea) for linea in leer(self.log_aws).splitlines() if linea.strip()]

    def invalidaciones(self):
        return [r for c in self.llamadas_aws() if c.get('op') == 'invalidacion' for r in c['rutas']]

    def puts(self):
        claves = []
        for c in self.llamadas_aws():
            if c.get('op') == 's3api put-object':
                args = c['args']
                claves.append(args[args.index('--key') + 1])
        return claves

    def sembrar_produccion(self):
        """Estado previo: páginas y JS viejos, admin.css ausente, admin-entrar.js igual que en 590ed7d."""
        antiguo = b'<!DOCTYPE html><title>viejo</title>'
        self.poner('admin-web/entrar.html', antiguo, CT_HTML, 'no-cache')
        self.poner('admin-web/resenas.html', antiguo, CT_HTML, 'no-cache', {'version': 'vieja'})
        self.poner('assets/js/admin-comun.js', b'/* viejo */', CT_JS, 'public, max-age=300')
        self.poner('assets/js/admin-panel.js', b'/* viejo */', CT_JS, 'public, max-age=300')
        self.poner('assets/js/admin-entrar.js', blob(REVISION, 'assets/js/admin-entrar.js'), CT_JS, NO_STORE,
                   {'origen': 'prueba'})

    def poner(self, clave, cuerpo, ct, cc, metadata=None):
        ruta = os.path.join(self.s3, clave)
        os.makedirs(os.path.dirname(ruta), exist_ok=True)
        with open(ruta, 'wb') as f:
            f.write(cuerpo)
        meta = {'ContentType': ct, 'CacheControl': cc}
        if metadata:
            meta['Metadata'] = metadata
        with open(ruta + '.json', 'w', encoding='utf-8') as f:
            json.dump(meta, f)

    def leer_objeto(self, clave):
        ruta = os.path.join(self.s3, clave)
        if not os.path.exists(ruta):
            return None, None
        with open(ruta + '.json', encoding='utf-8') as f:
            return leer(ruta, 'rb'), json.load(f)

    def construir_sitio(self, cambios=None, origen=REVISION, css=True):
        """Sitio de producción simulado. origen: commit cuyo contenido se sirve (590ed7d por defecto, BASE para el
        baseline actual de producción). css=False: el sitio no tiene assets/css/admin.css (como hoy en producción)."""
        if os.path.exists(self.sitio):
            shutil.rmtree(self.sitio)
        os.makedirs(os.path.join(self.sitio, 'admin-web'))
        os.makedirs(os.path.join(self.sitio, 'assets', 'css'))
        os.makedirs(os.path.join(self.sitio, 'assets', 'js'))
        with open(os.path.join(self.sitio, 'index.html'), 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html><html><head><title>ORK3D</title></head><body>'
                    '<a href="/opiniones">Opiniones</a></body></html>')
        with open(os.path.join(self.sitio, 'opiniones.html'), 'w', encoding='utf-8') as f:
            f.write('<!DOCTYPE html><html><head><title>Opiniones de clientes | ORK3D</title>'
                    '<meta name="description" content="Opiniones de clientes de ORK3D">'
                    '<link rel="canonical" href="https://ork3d.com/opiniones"></head><body></body></html>')
        with open(os.path.join(self.sitio, 'sitemap.xml'), 'w', encoding='utf-8') as f:
            f.write('<urlset><url><loc>https://ork3d.com/opiniones</loc></url></urlset>')
        for ruta in ESTATICOS:
            if ruta == 'assets/css/admin.css' and not css:
                continue
            datos = blob(origen, ruta)
            if cambios and ruta in cambios:
                datos = cambios[ruta]
            with open(os.path.join(self.sitio, ruta), 'wb') as f:
                f.write(datos)
        with open(os.path.join(self.sitio, 'assets', 'js', 'admin-entrar.js'), 'wb') as f:
            f.write(blob(origen, 'assets/js/admin-entrar.js'))


class ManifiestoTest(unittest.TestCase):
    def test_manifiesto_exacto_con_cinco_archivos(self):
        filas = [linea.split('\t') for linea in leer(MANIFIESTO).splitlines()
                 if linea.strip() and not linea.startswith('#')]
        self.assertEqual(sorted(f[0] for f in filas), sorted(ESTATICOS))
        for archivo, clave, ct, cc, rutas in filas:
            self.assertEqual(clave, archivo, 'la clave es el archivo local')
            self.assertEqual(cc, NO_STORE, f'{archivo}: mismo Cache-Control que el panel ya publicado')
            self.assertTrue(rutas.startswith('/'))
        tipos = {f[0]: f[2] for f in filas}
        self.assertEqual(tipos['admin-web/resenas.html'], CT_HTML)
        self.assertEqual(tipos['assets/css/admin.css'], CT_CSS)
        self.assertEqual(tipos['assets/js/admin-panel.js'], CT_JS)

    def test_manifiesto_es_la_interseccion_del_diff_y_los_estaticos(self):
        diff = git(['diff', '--name-only', BASE, REVISION], RAIZ).stdout
        esperado = subprocess.run(['python3', AYUDA, 'estaticos-admin'], input=diff, capture_output=True,
                                  text=True, check=True).stdout.split()
        declarado = [linea.split('\t')[0] for linea in leer(MANIFIESTO).splitlines()
                     if linea.strip() and not linea.startswith('#')]
        self.assertEqual(sorted(esperado), sorted(declarado))
        self.assertEqual(sorted(declarado), sorted(ESTATICOS))

    def test_admin_entrar_js_no_entra_porque_no_cambio(self):
        self.assertNotIn('assets/js/admin-entrar.js', git(['diff', '--name-only', BASE, REVISION], RAIZ).stdout.split())
        self.assertEqual(blob(BASE, 'assets/js/admin-entrar.js'), blob(REVISION, 'assets/js/admin-entrar.js'))
        self.assertNotIn('assets/js/admin-entrar.js', leer(MANIFIESTO))

    def test_rutas_a_invalidar_son_las_esperadas_y_acotadas(self):
        salida = ayuda('rutas', MANIFIESTO)
        self.assertEqual(salida.returncode, 0, salida.stderr)
        self.assertEqual(salida.stdout.split(), RUTAS_ESPERADAS)
        self.assertNotIn('/*', salida.stdout.split())

    def test_validar_manifiesto_rechaza_comodin_global_columnas_claves_y_repetidos(self):
        d = tempfile.mkdtemp(prefix='admin-ui-man-')
        self.addCleanup(shutil.rmtree, d, True)
        for contenido, motivo in (
                ('a.html\ta.html\ttext/html\tno-store\t/*\n', 'comodín'),
                ('a.html\ta.html\ttext/html\tno-store\n', 'columnas'),
                ('a.html\tb.html\ttext/html\tno-store\t/a.html\n', 'clave distinta'),
                ('a.html\ta.html\ttext/html\tno-store\t/a.html\na.html\ta.html\ttext/html\tno-store\t/a.html\n',
                 'repetido')):
            ruta = os.path.join(d, 'm.tsv')
            with open(ruta, 'w', encoding='utf-8') as f:
                f.write(contenido)
            self.assertNotEqual(ayuda('validar-manifiesto', ruta).returncode, 0, motivo)


class ScriptsTest(unittest.TestCase):
    """Análisis estático del tooling: solo los comandos permitidos, sin sync, sin --delete, sin directorios."""

    def test_no_hay_sync_ni_delete_ni_recursivos_ni_directorios_completos(self):
        for ruta in (PUBLICAR, VERIFICAR, COMUN, NAVEGADOR):
            codigo = codigo_sin_comentarios(ruta)
            for patron in (r'\bs3 sync\b', r'--delete\b', r'\bs3 rm\b', r'--recursive\b', r'\bs3 cp\b',
                           r'\bs3 mb\b', r'\bs3 rb\b', r'\brb\b'):
                self.assertIsNone(re.search(patron, codigo), f'{os.path.basename(ruta)} contiene {patron}')

    def test_solo_comandos_de_s3_y_cloudfront_permitidos(self):
        permitidos = {'s3api head-object', 's3api get-object', 's3api put-object', 's3api delete-object',
                      'cloudfront create-invalidation', 'cloudfront wait'}
        usados = set()
        for ruta in (PUBLICAR, VERIFICAR, COMUN):
            for servicio, operacion in re.findall(r'"\$AWS"\s+(\S+)\s+(\S+)', codigo_sin_comentarios(ruta)):
                usados.add(f'{servicio} {operacion}')
        self.assertTrue(usados <= permitidos, f'comandos no permitidos: {sorted(usados - permitidos)}')
        self.assertIn('s3api put-object', usados)
        self.assertIn('cloudfront create-invalidation', usados)

    def test_delete_object_solo_en_revertir(self):
        codigo = codigo_sin_comentarios(PUBLICAR)
        bloque = re.search(r'\nrevertir\(\) \{\n(.*?)\n\}\n', codigo, re.S)
        self.assertIsNotNone(bloque, 'revertir() no encontrado')
        self.assertIn('s3api delete-object', bloque.group(1))
        fuera = codigo.replace(bloque.group(1), '')
        self.assertNotIn('delete-object', fuera)

    def test_verificacion_no_escribe_ni_usa_metodos_que_no_sean_get(self):
        codigo = codigo_sin_comentarios(VERIFICAR)
        for patron in (r'\bput-object\b', r'\bdelete-object\b', r'create-invalidation', r'/solicitar',
                       r'/sesion/cerrar', r'\bupdate-', r'\bpublish-'):
            self.assertIsNone(re.search(patron, codigo), f'admin-ui-verify.sh contiene {patron}')
        # Las llamadas a curl son solo GET: ninguna con método, cuerpo ni cabeceras de escritura.
        llamadas_curl = [l for l in codigo.splitlines() if '"$CURL"' in l]
        self.assertTrue(llamadas_curl, 'la verificación hace GET con curl')
        for linea in llamadas_curl:
            for marca in (' -X ', '--request', '--data', ' -d ', '-H ', '--json'):
                self.assertNotIn(marca, linea, f'curl con {marca!r}: {linea.strip()}')
        self.assertNotIn('"$AWS"', codigo, 'la verificación no llama a AWS directamente (solo prod-lectura-v2.sh)')

    def test_navegador_solo_get_al_mismo_origen_y_sin_clics(self):
        codigo = codigo_sin_comentarios(NAVEGADOR)
        self.assertIn("peticion.method() === 'GET'", codigo)
        self.assertIn('return ruta.abort();', codigo)
        for patron in (r'\.click\(', r'\.dblclick\(', r'\.fill\(', r'\.press\(', r'\.check\(', r'\.submit\(',
                       r'\.tap\(', r'\.dispatchEvent\(', r'keyboard\.', r'\bfetch\('):
            self.assertIsNone(re.search(patron, codigo), f'el navegador contiene {patron}')


class AyudaTest(unittest.TestCase):
    """Chequeos de CSP, noindex, estilos, scripts y innerHTML sobre el repo real y sobre casos sintéticos."""

    def test_panel_del_repo_cumple_csp_noindex_sin_estilos_sin_scripts_y_sin_innerhtml(self):
        for html in ('admin-web/resenas.html', 'admin-web/entrar.html'):
            ruta = os.path.join(RAIZ, html)
            for orden in (['csp'], ['noindex'], ['sin-estilos'], ['sin-scripts-inline']):
                r = ayuda(*orden, ruta)
                self.assertEqual(r.returncode, 0, f'{html} {orden}: {r.stdout}')
        js = [os.path.join(RAIZ, f'assets/js/{n}') for n in ('admin-comun.js', 'admin-panel.js', 'admin-entrar.js')]
        r = ayuda('sin-innerhtml', *js)
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_ayuda_detecta_estilo_inline_script_inline_e_innerhtml(self):
        d = tempfile.mkdtemp(prefix='admin-ui-ayuda-')
        self.addCleanup(shutil.rmtree, d, True)
        html = os.path.join(d, 'x.html')
        with open(html, 'w', encoding='utf-8') as f:
            f.write('<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; '
                    'style-src \'self\'; object-src \'none\'"><meta name="robots" content="noindex, nofollow">'
                    '<p style="color:red">x</p><script>alert(1)</script>')
        self.assertNotEqual(ayuda('sin-estilos', html).returncode, 0)
        self.assertNotEqual(ayuda('sin-scripts-inline', html).returncode, 0)
        self.assertEqual(ayuda('csp', html).returncode, 0)
        js = os.path.join(d, 'x.js')
        with open(js, 'w', encoding='utf-8') as f:
            f.write('el.innerHTML = datos;')
        self.assertNotEqual(ayuda('sin-innerhtml', js).returncode, 0)

    def test_csp_sin_style_src_self_falla(self):
        d = tempfile.mkdtemp(prefix='admin-ui-csp-')
        self.addCleanup(shutil.rmtree, d, True)
        html = os.path.join(d, 'y.html')
        with open(html, 'w', encoding='utf-8') as f:
            f.write('<meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'">')
        self.assertNotEqual(ayuda('csp', html).returncode, 0)


class PublicarTest(Entorno):
    def test_diff_feliz_no_escribe_nada(self):
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for archivo in ESTATICOS:
            self.assertIn(archivo, r.stdout)
        self.assertIn('estado:', r.stdout)
        self.assertIn('Diff listo: no se escribió nada', r.stdout)
        self.assertIn('repo: rama main', r.stdout, 'la guarda compara contra main, no contra la feature branch')
        ops = {c.get('op') for c in self.llamadas_aws()}
        self.assertTrue(ops <= {'s3api head-object', 's3api get-object'}, f'diff llamó a {ops}')
        self.assertFalse(os.path.exists(self.rutas), 'diff no crea la carpeta de copias')

    def test_diff_rechaza_repo_con_cambios_sin_commitear(self):
        with open(os.path.join(self.clon, 'assets/css/admin.css'), 'a', encoding='utf-8') as f:
            f.write('/* sin commitear */\n')
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('sin commitear', r.stderr)
        self.assertEqual(self.llamadas_aws(), [], 'no se llama a AWS con el repo sucio')

    def test_diff_rechaza_head_distinto_de_origin(self):
        with open(os.path.join(self.clon, 'infra/admin-ui-archivos.tsv'), 'a', encoding='utf-8') as f:
            f.write('# comentario local\n')
        git(['commit', '-q', '-am', 'local sin push'], self.clon)
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('no coincide con origin', r.stderr)
        self.assertEqual(self.llamadas_aws(), [])

    def test_diff_rechaza_contenido_distinto_del_revisado(self):
        with open(os.path.join(self.clon, 'assets/js/admin-panel.js'), 'a', encoding='utf-8') as f:
            f.write('\n// cambio no revisado\n')
        git(['commit', '-q', '-am', 'cambio no revisado'], self.clon)
        git(['push', '-q', 'origin', RAMA], self.clon)
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('no es el de 590ed7d', r.stderr)

    def test_diff_rechaza_archivo_del_manifiesto_ausente(self):
        git(['rm', '-q', 'assets/css/admin.css'], self.clon)
        git(['commit', '-q', '-m', 'quita admin.css'], self.clon)
        git(['push', '-q', 'origin', RAMA], self.clon)
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('falta el archivo local assets/css/admin.css', r.stderr)

    def test_diff_rechaza_manifiesto_que_no_es_la_interseccion(self):
        ruta = os.path.join(self.clon, 'infra/admin-ui-archivos.tsv')
        lineas = [l for l in leer(ruta).splitlines(True) if 'admin-comun.js' not in l]
        with open(ruta, 'w', encoding='utf-8') as f:
            f.writelines(lineas)
        git(['commit', '-q', '-am', 'manifiesto recortado'], self.clon)
        git(['push', '-q', 'origin', RAMA], self.clon)
        r = self.correr(PUBLICAR, 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('no es la intersección', r.stderr)

    def test_publicar_sin_allow_sale_con_3_sin_tocar_aws(self):
        r = self.correr(PUBLICAR, 'publicar')
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('ALLOW_ADMIN_UI_PUBLISH=1', r.stderr)
        self.assertEqual(self.llamadas_aws(), [])
        r = self.correr(PUBLICAR, 'revertir')
        self.assertEqual(r.returncode, 3, r.stderr)

    def test_publicar_guarda_copia_600_sube_solo_cinco_y_invalida_acotado(self):
        self.sembrar_produccion()
        r = self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sorted(self.puts()), sorted(ESTATICOS), 'solo los 5 del manifiesto, sin admin-entrar.js')
        self.assertNotIn('assets/js/admin-entrar.js', self.puts())
        self.assertEqual(sorted(set(self.invalidaciones())), RUTAS_ESPERADAS)
        self.assertNotIn('/*', self.invalidaciones())

        for archivo in ESTATICOS:
            cuerpo, meta = self.leer_objeto(archivo)
            self.assertEqual(cuerpo, blob(REVISION, archivo), f'{archivo}: bytes = 590ed7d')
        _, meta = self.leer_objeto('assets/css/admin.css')
        self.assertEqual((meta['ContentType'], meta['CacheControl']), (CT_CSS, NO_STORE))
        _, meta = self.leer_objeto('admin-web/resenas.html')
        self.assertEqual((meta['ContentType'], meta['CacheControl']), (CT_HTML, NO_STORE))
        cuerpo_entrar, meta_entrar = self.leer_objeto('assets/js/admin-entrar.js')
        self.assertEqual(cuerpo_entrar, blob(REVISION, 'assets/js/admin-entrar.js'))
        self.assertEqual(meta_entrar.get('Metadata'), {'origen': 'prueba'}, 'admin-entrar.js no se tocó')

        copias = os.listdir(os.path.join(self.rutas, 'copias'))
        self.assertEqual(len(copias), 1)
        copia = os.path.join(self.rutas, 'copias', copias[0])
        self.assertIn(copia, r.stdout, 'la ruta de la copia se muestra')
        self.assertEqual(stat.S_IMODE(os.stat(copia).st_mode), 0o700)
        for dirpath, dirnames, filenames in os.walk(copia):
            for nombre in filenames:
                self.assertEqual(stat.S_IMODE(os.stat(os.path.join(dirpath, nombre)).st_mode), 0o600, nombre)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.rutas, 'ULTIMA-COPIA')).st_mode), 0o600)

        entradas = sorted(os.listdir(os.path.join(copia, 'entradas')))
        self.assertEqual(len(entradas), 5)
        with open(os.path.join(copia, 'entradas', '03.json'), encoding='utf-8') as f:
            self.assertFalse(json.load(f)['existia'], 'admin.css no existía: revertir lo quita')
        with open(os.path.join(copia, 'entradas', '02.json'), encoding='utf-8') as f:
            entrada = json.load(f)
        self.assertTrue(entrada['existia'])
        self.assertEqual(entrada['metadata'], {'version': 'vieja'}, 'metadata de la copia preservada')
        self.assertEqual(entrada['content_type'], CT_HTML)
        self.assertEqual(leer(os.path.join(copia, 'objetos', 'assets/js/admin-panel.js'), 'rb'), b'/* viejo */')

    def test_revertir_restaura_bytes_cabeceras_y_metadata_exactos(self):
        self.sembrar_produccion()
        antes = {a: self.leer_objeto(a) for a in ESTATICOS[:2] + ['assets/js/admin-comun.js',
                                                                 'assets/js/admin-panel.js']}
        self.assertEqual(self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'}).returncode, 0)
        llamadas_previas = len(self.llamadas_aws())
        r = self.correr(PUBLICAR, 'revertir', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for archivo, (cuerpo, meta) in antes.items():
            cuerpo_ahora, meta_ahora = self.leer_objeto(archivo)
            self.assertEqual(cuerpo_ahora, cuerpo, f'{archivo}: bytes de antes')
            self.assertEqual(meta_ahora, meta, f'{archivo}: cabeceras y metadata de antes')
        self.assertEqual(self.leer_objeto('assets/css/admin.css'), (None, None),
                         'admin.css no existía antes: revertir lo quita')
        nuevas = self.llamadas_aws()[llamadas_previas:]
        borrados = [c['args'][c['args'].index('--key') + 1] for c in nuevas if c.get('op') == 's3api delete-object']
        self.assertEqual(borrados, ['assets/css/admin.css'], 'solo se borra el objeto que no existía')
        self.assertEqual(sorted(set(self.invalidaciones())), RUTAS_ESPERADAS)
        self.assertIn('Ningún dato, reseña, Lambda ni configuración de CloudFront cambió', r.stdout)

    def test_publicar_dos_veces_conserva_las_copias_y_apunta_a_la_ultima(self):
        self.sembrar_produccion()
        self.assertEqual(self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'}).returncode, 0)
        self.assertEqual(self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'}).returncode, 0)
        copias = sorted(os.listdir(os.path.join(self.rutas, 'copias')))
        self.assertEqual(len(copias), 2, 'las copias anteriores no se borran')
        with open(os.path.join(self.rutas, 'ULTIMA-COPIA'), encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), os.path.join(self.rutas, 'copias', copias[-1]))
        primera = os.path.join(self.rutas, 'copias', copias[0], 'objetos', 'assets/js/admin-panel.js')
        self.assertEqual(leer(primera, 'rb'), b'/* viejo */', 'la copia más antigua sigue intacta')

    def test_publicacion_parcial_informa_la_copia_y_como_revertir(self):
        self.sembrar_produccion()
        r = self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1',
                                                      'FAKE_ADMIN_UI_FALLA_PUT': 'assets/js/admin-panel.js'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('Publicación parcial', r.stderr)
        self.assertIn('revertir', r.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.rutas, 'ULTIMA-COPIA')), 'la copia queda registrada')
        self.assertEqual(self.leer_objeto('assets/js/admin-panel.js')[0], b'/* viejo */', 'no se escribió a medias')

    def test_revertir_sin_copia_falla_sin_tocar_aws(self):
        r = self.correr(PUBLICAR, 'revertir', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('no hay copia para revertir', r.stderr)
        self.assertEqual(self.llamadas_aws(), [])

    def test_publicar_con_repo_sucio_no_escribe_aunque_haya_allow(self):
        with open(os.path.join(self.clon, 'assets/css/admin.css'), 'a', encoding='utf-8') as f:
            f.write('x')
        r = self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'})
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual([c for c in self.llamadas_aws() if c.get('op') != 's3api head-object'], [])
        self.assertFalse(os.path.exists(os.path.join(self.rutas, 'ULTIMA-COPIA')))


class VerificarTest(Entorno):
    def setUp(self):
        super().setUp()
        # Baseline de producción hoy: HTML y JS de e8052cd, y sin assets/css/admin.css (nunca se publicó).
        self.construir_sitio(origen=BASE, css=False)

    def test_pre_es_solo_lectura_y_pasa_en_el_estado_esperado(self):
        r = self.correr(VERIFICAR, 'pre')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('KNOWN GAP  assets/css/admin.css?v=1 no existe en producción; será creado por este deploy', r.stdout)
        self.assertIn('PASS  lectura de producción completa', r.stdout)
        self.assertIn('ninguna reseña de prueba aparece en el export', r.stdout)
        self.assertIn('PASS  /admin/resenas responde 200', r.stdout)
        self.assertIn('PASS  API admin sin cookie responde 401 no_autenticado', r.stdout)
        peticiones = [json.loads(l) for l in leer(self.log_curl).splitlines() if l.strip()]
        self.assertTrue(peticiones)
        self.assertEqual({p['metodo'] for p in peticiones}, {'GET'}, 'pre solo hace GET')
        ops = {c.get('op') for c in self.llamadas_aws()}
        lecturas = {'lambda get-function-configuration', 'logs filter-log-events', 's3api head-object'}
        self.assertTrue(ops <= lecturas, f'pre llamó a {ops}')
        self.assertIn('s3api head-object', ops, 'pre confirma con head-object que admin.css no existe')
        self.assertNotIn('s3api put-object', ops)
        self.assertNotIn('s3api delete-object', ops)
        self.assertEqual(self.invalidaciones(), [], 'pre no invalida nada')

    def test_pre_exige_smoke_notify_id(self):
        r = self.correr(VERIFICAR, 'pre', extra={'SMOKE_NOTIFY_ID': ''})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('falta SMOKE_NOTIFY_ID', r.stderr)

    def test_pre_detecta_resena_de_prueba_en_el_export(self):
        export = json.dumps({'items': [{'id': SMOKE, 'rating': 5, 'comment': 'x', 'aspects': [], 'display_name': 'x',
                                        'category': 'x', 'product_label': 'x', 'verified': False,
                                        'photo_url': None, 'published_at': None}]})
        r = self.correr(VERIFICAR, 'pre', extra={'FAKE_ADMIN_UI_EXPORT': export})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL  ninguna reseña de prueba aparece en el export', r.stdout)

    def test_post_feliz_corresponde_a_590ed7d_byte_a_byte(self):
        self.sembrar_produccion()
        self.assertEqual(self.correr(PUBLICAR, 'publicar', extra={'ALLOW_ADMIN_UI_PUBLISH': '1'}).returncode, 0)
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('PASS  /admin/resenas = admin-web/resenas.html en ' + REVISION, r.stdout)
        self.assertRegex(r.stdout, r'PASS  /assets/css/admin\.css\?v=2: Content-Type text/css')
        self.assertRegex(r.stdout, r'PASS  /assets/js/admin-panel\.js\?v=2: Content-Type application/javascript')
        self.assertRegex(r.stdout, r'PASS  /admin/entrar = admin-web/entrar\.html en ' + REVISION)
        self.assertNotIn('FAIL', r.stdout)
        self.assertIn('Verificación post: todo PASS.', r.stdout)

    def test_post_detecta_html_de_la_version_anterior(self):
        self.construir_sitio({'admin-web/resenas.html': blob(BASE, 'admin-web/resenas.html')})
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL  /admin/resenas: código 200', r.stdout)
        self.assertIn('distinto del', r.stdout)

    def test_post_detecta_estilo_inline_en_el_html(self):
        html = blob(REVISION, 'admin-web/resenas.html').replace(
            b'<h1 class="adm__titulo"', b'<h1 style="color:red" class="adm__titulo"', 1)
        self.construir_sitio({'admin-web/resenas.html': html})
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL  sin atributos style=', r.stdout)

    def test_post_detecta_innerhtml_en_el_js_servido(self):
        js = blob(REVISION, 'assets/js/admin-panel.js') + b'\nel.innerHTML = datos;\n'
        self.construir_sitio({'assets/js/admin-panel.js': js})
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('FAIL  sin innerHTML ni HTML crudo en', r.stdout)

    def test_post_detecta_referencia_sin_cache_bust(self):
        html = blob(REVISION, 'admin-web/resenas.html').replace(b'admin.css?v=2', b'admin.css?v=1')
        self.construir_sitio({'admin-web/resenas.html': html})
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn('versión ?v=2 (cache bust)', r.stdout)

    def test_navegador_sin_playwright_sale_con_3_y_no_es_un_fallo(self):
        r = subprocess.run(['node', NAVEGADOR], env=self.entorno(), capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('INFO  navegador omitido: Playwright no está disponible', r.stdout)
        r = self.correr(VERIFICAR, 'pre')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('INFO  navegador omitido', r.stdout)


class HuecoCssTest(Entorno):
    """El único hueco permitido en pre: /assets/css/admin.css?v=1 → 404. Cada condición tiene su caso negativo."""

    def setUp(self):
        super().setUp()
        self.construir_sitio(origen=BASE, css=False)

    def pre(self, extra=None):
        return self.correr(VERIFICAR, 'pre', extra=extra)

    def assertSinHueco(self, r):
        self.assertNotIn('KNOWN GAP', r.stdout, 'el hueco no se acepta en este caso')

    def test_hueco_aceptado_se_informa_explicito_y_no_como_pass_silencioso(self):
        r = self.pre()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('KNOWN GAP  assets/css/admin.css?v=1 no existe en producción; será creado por este deploy', r.stdout)
        self.assertIn('todo PASS, con 1 KNOWN GAP explícito', r.stdout)
        self.assertNotRegex(r.stdout, r'(?m)^FAIL')

    def test_css_que_existe_y_responde_200_no_es_hueco(self):
        self.poner('assets/css/admin.css', blob(REVISION, 'assets/css/admin.css'), CT_CSS, NO_STORE)
        r = self.pre()
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('responde 200: el archivo existe, no hay hueco', r.stdout)

    def test_rechaza_si_head_object_da_error_distinto_de_404(self):
        r = self.pre(extra={'FAKE_ADMIN_UI_ERROR_HEAD': 'assets/css/admin.css'})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  head-object assets/css/admin.css no es concluyente', r.stdout)
        self.assertIn('(403)', r.stdout)

    def test_rechaza_si_existe_en_s3_pero_la_ruta_responde_404(self):
        self.poner('assets/css/admin.css', blob(REVISION, 'assets/css/admin.css'), CT_CSS, NO_STORE)
        r = self.pre(extra={'FAKE_ADMIN_UI_CURL_FORZAR': '/assets/css/admin.css=404'})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  /assets/css/admin.css?v=1 responde 404 pero assets/css/admin.css existe en S3', r.stdout)

    def test_rechaza_si_admin_css_responde_500_aunque_no_exista(self):
        r = self.pre(extra={'FAKE_ADMIN_UI_CURL_FORZAR': '/assets/css/admin.css=500'})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('responde 500: sin archivo en S3 solo se acepta 404', r.stdout)

    def test_rechaza_si_falta_cualquier_otro_asset_del_panel(self):
        for ruta in ('assets/js/admin-comun.js', 'assets/js/admin-panel.js', 'assets/js/admin-entrar.js'):
            with self.subTest(ruta=ruta):
                self.construir_sitio(origen=BASE, css=False)
                os.remove(os.path.join(self.sitio, ruta))
                r = self.pre()
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertSinHueco(r)
                self.assertRegex(r.stdout, rf'FAIL  /{re.escape(ruta)}\?v=1 responde 200 — código 404')

    def test_rechaza_si_el_html_pide_una_version_inesperada_de_un_asset(self):
        html = blob(BASE, 'admin-web/resenas.html').replace(b'admin-panel.js?v=1', b'admin-panel.js?v=3')
        self.construir_sitio(cambios={'admin-web/resenas.html': html}, origen=BASE, css=False)
        r = self.pre()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  /assets/js/admin-panel.js?v=3: versión inesperada en el HTML actual', r.stdout)

    def test_rechaza_si_el_css_se_pide_con_otra_version(self):
        html = blob(BASE, 'admin-web/resenas.html').replace(b'admin.css?v=1', b'admin.css?v=2')
        self.construir_sitio(cambios={'admin-web/resenas.html': html}, origen=BASE, css=False)
        r = self.pre()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  /assets/css/admin.css?v=2: versión inesperada en el HTML actual', r.stdout)

    def test_rechaza_si_el_manifiesto_no_contiene_el_css(self):
        ruta = os.path.join(self.clon, 'infra', 'admin-ui-archivos.tsv')
        with open(ruta, encoding='utf-8') as f:
            lineas = [l for l in f.readlines() if 'assets/css/admin.css' not in l]
        with open(ruta, 'w', encoding='utf-8') as f:
            f.writelines(lineas)
        r = self.pre()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  el manifiesto contiene assets/css/admin.css exactamente una vez', r.stdout)
        self.assertIn('el manifiesto no contiene assets/css/admin.css exactamente una vez', r.stdout)

    def test_rechaza_si_el_css_local_no_es_el_blob_revisado(self):
        with open(os.path.join(self.clon, 'assets', 'css', 'admin.css'), 'a', encoding='utf-8') as f:
            f.write('/* local, no revisado */\n')
        r = self.pre()
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn(f'FAIL  assets/css/admin.css: contenido local distinto de {REVISION}', r.stdout)
        self.assertIn('el CSS local no es el de ' + REVISION, r.stdout)

    def test_el_hueco_no_tapa_otra_regresion_del_preflight(self):
        export = json.dumps({'items': [{'id': SMOKE, 'rating': 5, 'comment': 'x', 'aspects': [], 'display_name': 'x',
                                        'category': 'x', 'product_label': 'x', 'verified': False,
                                        'photo_url': None, 'published_at': None}]})
        r = self.pre(extra={'FAKE_ADMIN_UI_EXPORT': export})
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('hueco no aceptado mientras haya otros FAIL', r.stdout)

    def test_post_nunca_acepta_el_hueco(self):
        self.construir_sitio(origen=REVISION, css=False)
        r = self.correr(VERIFICAR, 'post')
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertSinHueco(r)
        self.assertIn('FAIL  /assets/css/admin.css?v=2 responde 200 — código 404', r.stdout)


if __name__ == '__main__':
    unittest.main()
