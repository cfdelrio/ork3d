"""Protección del despliegue del sitio (deploy.sh) antes de integrar la rama a main.

Un push a main ejecuta .github/workflows/deploy.yml, que corre ./deploy.sh. Estas pruebas no usan AWS:

- Simulan el filtrado de `aws s3 sync` y de `aws s3 cp --recursive` con la semántica de la CLI de AWS (awscli 1.46.2,
  filters.py y syncstrategy/delete.py: las exclusiones se aplican también a los objetos del destino, y el texto de
  --delete dice que los archivos excluidos quedan fuera del borrado). La semántica se verificó leyendo ese código.
- Usan un inventario: los archivos del repo, más lo que escribe la aplicación en el bucket del sitio (resenas/), más
  el legado borrado del repo que podría seguir publicado.
- Auditan deploy.sh con esa simulación. La auditoría pasa sobre el script real y falla sobre cada mutación.
- Ejecutan deploy.sh con un `aws` falso para comprobar que DEPLOY_DRY_RUN=1 solo lista, y que el modo real invalida
  una sola vez y no usa --dryrun.
"""
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DEPLOY = os.path.join(RAIZ, 'deploy.sh')

# Panel admin: dueño infra/admin-ui-publicar.sh. El deploy del sitio no lo sube ni lo borra.
PROTEGIDOS_ADMIN = ['admin-web/entrar.html', 'admin-web/resenas.html', 'assets/css/admin.css',
                    'assets/js/admin-comun.js', 'assets/js/admin-entrar.js', 'assets/js/admin-panel.js']
# Export y fotos públicas: las escribe la aplicación en el bucket del sitio. No están en el repo.
GENERADOS = ['resenas/publicas.json',
             'resenas/fotos/0123456789abcdef0123456789abcdef.jpg',
             'resenas/fotos/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jpg']
# /opiniones: lanzamiento de la página pública y su CSS/JS.
OPINIONES = ['opiniones.html', 'assets/css/opiniones.css', 'assets/js/resenas-publicas.js']
# Raíz del sitio: lo que el deploy tiene que subir siempre.
SITIO_RAIZ = ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'error.html', 'opinar.html',
              'config.js', 'robots.txt', 'sitemap.xml']
# Objetos hipotéticos de código fuera del sitio: no deben existir en el bucket; si existieran, no se tocan.
CODIGO_FUERA = ['api/resenas/app.py', 'admin/resenas.sh', 'dev/serve.py', 'docs/resenas/README.md',
                'infra/setup.sh', '.github/workflows/deploy.yml']
# Legado: archivos del sitio borrados del repo en el historial de main (commits 2de8323, 98a3c95, 786cde9, b6c9fb2,
# 2d79e82 y 21b8258; instantánea a la fecha de este cambio). Sin referencias en el HEAD. Si siguen en producción,
# un --delete los borraría: es el único riesgo residual que se documenta, no se oculta.
LEGADO = [
    'assets/img/feature-torneo.svg',
    'assets/img/fotos/identidad-480.avif', 'assets/img/fotos/identidad-480.webp',
    'assets/img/fotos/identidad-640.avif', 'assets/img/fotos/identidad-640.webp',
    'assets/img/fotos/produccion-1199.avif', 'assets/img/fotos/produccion-1199.webp',
    'assets/img/fotos/produccion-640.avif', 'assets/img/fotos/produccion-640.webp',
    'assets/img/fotos/produccion-920.avif', 'assets/img/fotos/produccion-920.webp',
    'assets/img/fotos/produccion-960.avif', 'assets/img/fotos/produccion-960.webp',
    'assets/img/fotos/series-480.avif', 'assets/img/fotos/series-480.webp',
    'assets/img/fotos/series-699.avif', 'assets/img/fotos/series-699.webp',
    'assets/img/g-funcional.svg', 'assets/img/g-llaveros.svg', 'assets/img/g-logos.svg',
    'assets/img/g-medallas.svg', 'assets/img/g-merch.svg', 'assets/img/g-soportes.svg',
    'assets/img/g-trofeos.svg', 'assets/img/hero-trophies.svg', 'assets/img/printer.svg',
]
CACHE_SITEMAP = 'public, max-age=300, must-revalidate'

CLASES_PROTEGIDAS = {
    'generado (export y fotos públicas)': lambda k: k.startswith('resenas/'),
    'panel admin': lambda k: k in PROTEGIDOS_ADMIN or k.startswith('admin-web/'),
    '/opiniones': lambda k: k in OPINIONES,
    'sitio raíz': lambda k: k in SITIO_RAIZ,
    'código del repo': lambda k: (k.split('/')[0] in ('api', 'admin', 'dev', 'docs', 'infra', '.github')
                                  or k in ('deploy.sh', 'verify.sh', 'README.md', '.gitignore')
                                  or k.endswith(('.sh', '.md'))),
}


def leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def fuente_del_repo():
    """Archivos trackeados: lo que el checkout de CI contiene y el sync puede subir."""
    r = subprocess.run(['git', 'ls-files'], cwd=RAIZ, capture_output=True, text=True, check=True)
    return sorted(l for l in r.stdout.splitlines() if l)


def produccion_simulada(fuente):
    """Inventario de lo que podría haber en el bucket: el repo (lo que el último deploy subió), lo generado, el legado
    y los objetos de código hipotéticos."""
    return sorted(set(fuente) | set(GENERADOS) | set(LEGADO) | set(CODIGO_FUERA))


def codigo(texto):
    return '\n'.join(l for l in texto.splitlines() if not l.lstrip().startswith('#'))


def comandos(texto):
    """Comandos `aws s3 sync|cp` y `s3_sitio sync|cp` del script, con las continuaciones de línea unidas."""
    lineas = texto.splitlines()
    res, i = [], 0
    while i < len(lineas):
        s = lineas[i].strip()
        if re.match(r'^(aws s3|s3_sitio)\s+(sync|cp)\b', s):
            bloque = [s]
            while bloque[-1].endswith('\\') and i + 1 < len(lineas):
                i += 1
                bloque.append(lineas[i].strip())
            unido = ' '.join(b[:-1].rstrip() if b.endswith('\\') else b for b in bloque)
            tokens = shlex.split(unido)
            tokens = tokens[2:] if tokens[0] == 'aws' else tokens[1:]
            sub, origen, destino, resto = tokens[0], tokens[1], tokens[2], tokens[3:]
            recursivo, borrado, filtros, opciones = False, False, [], {}
            j = 0
            while j < len(resto):
                nombre = resto[j][2:]
                if nombre == 'recursive':
                    recursivo = True
                    j += 1
                elif nombre == 'delete':
                    borrado = True
                    j += 1
                elif nombre in ('exclude', 'include'):
                    filtros.append((nombre, resto[j + 1]))
                    j += 2
                else:
                    opciones[nombre] = resto[j + 1]
                    j += 2
            res.append({'sub': sub, 'origen': origen, 'destino': destino, 'recursivo': recursivo,
                        'delete': borrado, 'filtros': filtros, 'opciones': opciones, 'linea': i})
        i += 1
    return res


def clave_destino(destino):
    """'s3://${BUCKET}/assets/' -> 'assets/'; 's3://${BUCKET}' -> ''."""
    return destino.split('${BUCKET}', 1)[1].lstrip('/')


def incluido(clave, filtros):
    """Semántica de awscli filters.py: todo entra; cada regla que coincide fija el estado; gana la última."""
    estado = True
    for tipo, patron in filtros:
        if fnmatch.fnmatchcase(clave, patron):
            estado = tipo == 'include'
    return estado


def subidos(comando, fuente):
    """Claves del bucket que el comando sube desde el repo."""
    if comando['sub'] == 'sync':
        return [t for t in fuente if incluido(t, comando['filtros'])]
    if comando['recursivo']:
        origen = comando['origen'].rstrip('/') + '/'
        prefijo = clave_destino(comando['destino'])
        return [prefijo + t[len(origen):] for t in fuente
                if t.startswith(origen) and incluido(t[len(origen):], comando['filtros'])]
    return [clave_destino(comando['destino'])]


def borrables_si_hubiera_delete(comando, fuente, produccion):
    """Lo que `sync --delete` borraría: objetos del bucket sin archivo en el origen (filtrado) y no excluidos."""
    subidos_sync = set(subidos(comando, fuente))
    candidatos = [k for k in produccion if k not in subidos_sync]
    return [k for k in candidatos if incluido(k, comando['filtros'])]


def auditar(texto, fuente, produccion):
    """Problemas de deploy.sh. Lista vacía = el script no borra nada y protege los prefijos del sitio."""
    problemas = []
    cod = codigo(texto)
    cmds = comandos(texto)
    # Token del comando, no texto crudo: un mensaje de echo que mencione --delete no cuenta.
    if any(c['delete'] for c in cmds):
        problemas.append('sync con --delete: puede borrar objetos del bucket')
    if re.search(r'\bs3 rm\b|\bs3_sitio rm\b|delete-object|s3api delete', cod):
        problemas.append('hay un comando de borrado en S3')
    syncs = [c for c in cmds if c['sub'] == 'sync']
    if len(syncs) != 1:
        problemas.append(f'se esperaba un solo sync, hay {len(syncs)}')
    if syncs:
        for nombre, predicado in CLASES_PROTEGIDAS.items():
            afectados = [k for k in borrables_si_hubiera_delete(syncs[0], fuente, produccion) if predicado(k)]
            if afectados:
                problemas.append(f'con --delete se borrarían objetos de {nombre}: {afectados[:3]}')
    todos = set()
    for c in cmds:
        todos.update(subidos(c, fuente))
    for nombre, predicado in CLASES_PROTEGIDAS.items():
        if nombre in ('generado (export y fotos públicas)', 'panel admin', 'código del repo'):
            subidos_mal = sorted(k for k in todos if predicado(k))
            if subidos_mal:
                problemas.append(f'el deploy del sitio sube {nombre}: {subidos_mal[:3]}')
    faltan = sorted(k for k in OPINIONES + SITIO_RAIZ if k in fuente and k not in todos)
    if faltan:
        problemas.append(f'no se suben archivos del sitio: {faltan}')
    sitemap = [c for c in cmds if c['destino'].endswith('sitemap.xml')]
    if not sitemap or sitemap[0]['opciones'].get('cache-control') != CACHE_SITEMAP:
        problemas.append('sitemap.xml sin el cache-control de /opiniones')
    return problemas


def riesgo_residual(texto, fuente, produccion):
    """Objetos que se borrarían SOLO si se reintroduce --delete. Con el script real, no hay borrados."""
    syncs = [c for c in comandos(texto) if c['sub'] == 'sync']
    return sorted(borrables_si_hubiera_delete(syncs[0], fuente, produccion)) if syncs else []


def reemplazar_una_vez(texto, viejo, nuevo):
    assert texto.count(viejo) == 1, f'el texto a mutar debe aparecer una sola vez: {viejo!r}'
    return texto.replace(viejo, nuevo)


class SimulacionTest(unittest.TestCase):
    """La simulación reproduce la semántica de la CLI sobre casos concretos."""

    def test_exclusion_protege_un_objeto_del_destino(self):
        filtros = [('exclude', 'resenas/*')]
        self.assertFalse(incluido('resenas/publicas.json', filtros))
        self.assertTrue(incluido('index.html', filtros))

    def test_la_ultima_regla_gana_como_en_awscli(self):
        filtros = [('exclude', '*'), ('include', '*.avif')]
        self.assertTrue(incluido('assets/img/x.avif', filtros))
        self.assertFalse(incluido('assets/img/x.webp', filtros))

    def test_parser_lee_sync_con_sus_exclusiones_y_sin_delete(self):
        cmd = [c for c in comandos(leer(DEPLOY)) if c['sub'] == 'sync'][0]
        self.assertFalse(cmd['delete'])
        self.assertIn(('exclude', 'resenas/*'), cmd['filtros'])
        self.assertIn(('exclude', 'admin-web/*'), cmd['filtros'])


class DeployRealTest(unittest.TestCase):
    """Auditoría del deploy.sh real contra el inventario de producción simulado."""

    @classmethod
    def setUpClass(cls):
        cls.texto = leer(DEPLOY)
        cls.fuente = fuente_del_repo()
        cls.produccion = produccion_simulada(cls.fuente)

    def test_la_auditoria_pasa_sobre_el_script_real(self):
        self.assertEqual(auditar(self.texto, self.fuente, self.produccion), [])

    def test_con_el_script_real_no_hay_ningun_borrado_posible_en_ninguna_clase(self):
        syncs = [c for c in comandos(self.texto) if c['sub'] == 'sync']
        self.assertEqual(len(syncs), 1)
        # El sync borra solo si tiene --delete. Sin él, el conjunto de borrados reales es vacío para todo el inventario,
        # incluido el legado. (Con --delete, se calcularía lo que borraría.)
        borrados_reales = borrables_si_hubiera_delete(syncs[0], self.fuente, self.produccion) \
            if syncs[0]['delete'] else []
        self.assertEqual(borrados_reales, [])
        self.assertEqual(riesgo_residual(self.texto, self.fuente, self.produccion), sorted(LEGADO))

    def test_generados_export_y_fotos_no_se_suben_ni_se_borran(self):
        sync = [c for c in comandos(self.texto) if c['sub'] == 'sync'][0]
        for clave in GENERADOS:
            with self.subTest(clave=clave):
                self.assertFalse(incluido(clave, sync['filtros']), 'excluido por filtro')
                self.assertNotIn(clave, subidos(sync, self.fuente))
                self.assertNotIn(clave, riesgo_residual(self.texto, self.fuente, self.produccion))

    def test_panel_admin_no_se_sube_ni_se_borra_con_el_deploy_del_sitio(self):
        todos = set()
        for c in comandos(self.texto):
            todos.update(subidos(c, self.fuente))
        for clave in PROTEGIDOS_ADMIN:
            with self.subTest(clave=clave):
                self.assertNotIn(clave, todos)
                self.assertNotIn(clave, riesgo_residual(self.texto, self.fuente, self.produccion))

    def test_cp_recursivo_de_assets_excluye_el_css_y_js_admin(self):
        cp = [c for c in comandos(self.texto) if c['sub'] == 'cp' and c['recursivo'] and c['origen'] == 'assets/'][0]
        subidos_assets = subidos(cp, self.fuente)
        self.assertNotIn('assets/css/admin.css', subidos_assets)
        self.assertNotIn('assets/js/admin-panel.js', subidos_assets)
        self.assertIn('assets/css/opiniones.css', subidos_assets)
        self.assertIn('assets/js/resenas-publicas.js', subidos_assets)

    def test_opiniones_y_raiz_del_sitio_se_suben(self):
        todos = set()
        for c in comandos(self.texto):
            todos.update(subidos(c, self.fuente))
        for clave in OPINIONES + SITIO_RAIZ:
            with self.subTest(clave=clave):
                self.assertIn(clave, todos)

    def test_sitemap_conserva_el_cache_del_lanzamiento(self):
        sitemap = [c for c in comandos(self.texto) if c['destino'].endswith('sitemap.xml')]
        self.assertEqual(len(sitemap), 1)
        self.assertEqual(sitemap[0]['opciones']['cache-control'], CACHE_SITEMAP)
        self.assertEqual(sitemap[0]['opciones']['content-type'], 'application/xml; charset=utf-8')

    def test_codigo_del_repo_no_se_sube_al_bucket(self):
        todos = set()
        for c in comandos(self.texto):
            todos.update(subidos(c, self.fuente))
        self.assertEqual(sorted(k for k in todos if CLASES_PROTEGIDAS['código del repo'](k)), [])

    def test_reintroducir_delete_solo_arriesgaria_el_legado(self):
        """Si alguien vuelve a poner --delete, las exclusiones siguen protegiendo generados, admin, /opiniones y raíz.
        Lo único borrable es el legado de LEGADO, y lo documenta esta prueba."""
        con_delete = reemplazar_una_vez(self.texto, 's3_sitio sync . "s3://${BUCKET}" \\\n',
                                        's3_sitio sync . "s3://${BUCKET}" \\\n  --delete \\\n')
        self.assertIn('sync con --delete', auditar(con_delete, self.fuente, self.produccion)[0])
        residual = riesgo_residual(con_delete, self.fuente, self.produccion)
        self.assertEqual(residual, sorted(LEGADO))


class MutacionesTest(unittest.TestCase):
    """Cada mutación rompe una exclusión o una regla. La auditoría tiene que detectarla con su motivo."""

    @classmethod
    def setUpClass(cls):
        cls.texto = leer(DEPLOY)
        cls.fuente = fuente_del_repo()
        cls.produccion = produccion_simulada(cls.fuente)

    def detecta(self, mutado, fragmento):
        problemas = auditar(mutado, self.fuente, self.produccion)
        self.assertTrue(any(fragmento in p for p in problemas),
                        f'no se detectó {fragmento!r}; problemas: {problemas}')

    def test_quitar_la_exclusion_de_resenas_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto, '  --exclude "resenas/*" \\\n', ''), 'generado')

    def test_quitar_la_exclusion_de_admin_web_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto, '  --exclude "admin-web/*" \\\n', ''), 'panel admin')

    def test_quitar_la_exclusion_de_css_admin_del_cp_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto, '  --exclude "css/admin.css" \\\n', ''), 'panel admin')

    def test_reintroducir_delete_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto, 's3_sitio sync . "s3://${BUCKET}" \\\n',
                                        's3_sitio sync . "s3://${BUCKET}" \\\n  --delete \\\n'),
                     'sync con --delete')

    def test_quitar_la_exclusion_de_api_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto, '  --exclude "api/*" \\\n', ''), 'código del repo')

    def test_dejar_de_subir_opiniones_se_detecta(self):
        # Se quita de las dos vías por las que /opiniones llega al bucket: la exclusión del sync y la subida explícita.
        # Excluirla solo del sync no basta: la subida explícita la sigue publicando.
        mutado = reemplazar_una_vez(self.texto, '  --exclude "dev/*" \\\n',
                                    '  --exclude "dev/*" \\\n  --exclude "opiniones.html" \\\n')
        mutado = reemplazar_una_vez(mutado, 's3_sitio cp opiniones.html "s3://${BUCKET}/opiniones.html"',
                                    's3_sitio cp opiniones.html "s3://${BUCKET}/opiniones-viejo.html"')
        self.detecta(mutado, 'no se suben archivos del sitio')
        self.assertIn('opiniones.html', ' '.join(auditar(mutado, self.fuente, self.produccion)))

    def test_quitar_el_cache_del_sitemap_se_detecta(self):
        self.detecta(reemplazar_una_vez(self.texto,
                                        '  --cache-control "public, max-age=300, must-revalidate" \\\n'
                                        '  --content-type "application/xml; charset=utf-8" \\\n',
                                        '  --cache-control "public, max-age=300" \\\n'
                                        '  --content-type "application/xml; charset=utf-8" \\\n'),
                     'sitemap.xml sin el cache-control')


class DryRunTest(unittest.TestCase):
    """Ejecuta deploy.sh (copiado a un directorio temporal) con un aws falso que registra cada llamada."""

    FAKE = '''#!/usr/bin/env python3
import json
import os
import sys
with open(os.environ["FAKE_AWS_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["cloudfront", "create-invalidation"]:
    print("IFAKE123")
sys.exit(0)
'''

    def correr(self, dry):
        d = tempfile.mkdtemp(prefix='deploy-dry-')
        self.addCleanup(shutil.rmtree, d, True)
        shutil.copy(DEPLOY, os.path.join(d, 'deploy.sh'))
        aws = os.path.join(d, 'bin', 'aws')
        os.makedirs(os.path.dirname(aws))
        with open(aws, 'w', encoding='utf-8') as f:
            f.write(self.FAKE)
        os.chmod(aws, 0o755)
        log = os.path.join(d, 'llamadas.jsonl')
        env = dict(os.environ, PATH=os.path.dirname(aws) + os.pathsep + os.environ.get('PATH', ''),
                   BUCKET='ork3d.com', DISTRIBUTION_ID='EFAKE', FAKE_AWS_LOG=log,
                   DEPLOY_DRY_RUN='1' if dry else '0')
        r = subprocess.run(['bash', os.path.join(d, 'deploy.sh')], env=env, cwd=d, capture_output=True,
                           text=True, timeout=120)
        llamadas = []
        if os.path.exists(log):
            with open(log, encoding='utf-8') as f:
                llamadas = [json.loads(l) for l in f.read().splitlines() if l.strip()]
        return r, llamadas

    def test_dry_run_solo_lista_y_no_invalida(self):
        r, llamadas = self.correr(dry=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('DRY RUN', r.stdout)
        self.assertTrue(llamadas, 'el dry run debe listar operaciones')
        for llamada in llamadas:
            self.assertEqual(llamada[0], 's3', f'solo llamadas s3 en dry run: {llamada}')
            self.assertIn('--dryrun', llamada, f'toda llamada s3 lleva --dryrun: {llamada}')
        self.assertFalse([l for l in llamadas if 'cloudfront' in l], 'el dry run no invalida CloudFront')

    def test_modo_real_no_usa_dryrun_e_invalida_una_vez_sobre_raiz(self):
        r, llamadas = self.correr(dry=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for llamada in llamadas:
            if llamada[0] == 's3':
                self.assertNotIn('--dryrun', llamada)
        invalidaciones = [l for l in llamadas if l[:2] == ['cloudfront', 'create-invalidation']]
        self.assertEqual(len(invalidaciones), 1)
        self.assertEqual(invalidaciones[0][invalidaciones[0].index('--paths') + 1], '/*')

    def test_dry_run_y_modo_real_planean_las_mismas_operaciones(self):
        _, dry = self.correr(dry=True)
        _, real = self.correr(dry=False)
        quitar = lambda l: [x for x in l if x != '--dryrun']
        plan_dry = [quitar(l) for l in dry]
        plan_real = [l for l in real if l[:2] != ['cloudfront', 'create-invalidation']]
        self.assertEqual(plan_dry, plan_real)

    def test_ninguna_llamada_usa_borrado(self):
        _, real = self.correr(dry=False)
        for llamada in real:
            self.assertNotIn('--delete', llamada)
            self.assertNotIn('rm', llamada[:3])


if __name__ == '__main__':
    unittest.main()
