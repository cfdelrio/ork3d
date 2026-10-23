"""Cierre del tooling de Reviews V2 sobre main (producción validada). Sin AWS ni red.

- La rama de feature no aparece en código ejecutable, tests ni runbooks. Solo queda en el registro de auditoría.
- Las guardas comparan contra main.
- El tooling de /opiniones funciona desde main: el diff pasa la guarda sin escribir, y publicar y pre exigen su marca.
- El estado vigente de producción se documenta con sus dos variables explícitas.

Los clones de prueba se crean desde un origen bare de este repo con los scripts del árbol de trabajo, para que las
pruebas validen lo que se va a commitear y no el HEAD anterior.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
FEATURE = 'claude/ork3d-landing-page-b2fQr'
# El registro de auditoría conserva la referencia: describe el merge que ya ocurrió. Este archivo la nombra a propósito.
EXCEPCIONES = {'docs/resenas/MERGE-MAIN.md', 'infra/tests/test_cierre_main.py'}
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin_ui.py')
TOOLING_OPINIONES = ['infra/opiniones-publicar.sh', 'infra/opiniones-verify.sh', 'infra/opiniones-archivos.tsv',
                     'infra/prod-lectura-v2.sh', 'infra/prod_lectura_ayuda.py']


def leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def archivos_del_repo():
    r = subprocess.run(['git', 'ls-files'], cwd=RAIZ, capture_output=True, text=True, check=True)
    return [l for l in r.stdout.splitlines() if l]


class ReferenciasDeRamaTest(unittest.TestCase):
    def test_la_rama_de_feature_solo_aparece_en_el_registro_de_auditoria(self):
        culpables = []
        for rel in archivos_del_repo():
            if rel in EXCEPCIONES:
                continue
            try:
                texto = leer(os.path.join(RAIZ, rel))
            except (UnicodeDecodeError, OSError):
                continue
            if FEATURE in texto:
                culpables.append(rel)
        self.assertEqual(culpables, [], 'referencias a la rama de feature fuera del registro de auditoría')

    def test_las_guardas_ejecutables_comparan_contra_main(self):
        patrones = {
            'infra/admin-ui-comun.sh': r'^RAMA="([^"]+)"',
            'infra/opiniones-publicar.sh': r'^RAMA="([^"]+)"',
            'infra/admin-frontend-deploy.sh': r'^RAMA_ESPERADA="([^"]+)"',
            'infra/admin-deploy.sh': r'^RAMA_ESPERADA="([^"]+)"',
        }
        for rel, patron in patrones.items():
            with self.subTest(guarda=rel):
                m = re.search(patron, leer(os.path.join(RAIZ, rel)), re.M)
                self.assertIsNotNone(m, f'{rel}: no se encontró la constante de rama')
                self.assertEqual(m.group(1), 'main')

    def test_el_estado_vigente_se_documenta_con_sus_dos_variables(self):
        ruta = os.path.join(RAIZ, 'docs', 'resenas', 'ESTADO-PRODUCCION.md')
        self.assertTrue(os.path.exists(ruta), 'falta docs/resenas/ESTADO-PRODUCCION.md')
        texto = leer(ruta)
        self.assertIn('PROD_LECTURA_OPINIONES=lanzada PROD_LECTURA_NOTIFY_ESPERADO=true', texto)
        self.assertIn('main', texto)


class TooingOpinionesDesdeMainTest(unittest.TestCase):
    """Los scripts de /opiniones se prueban en un clon de main con los archivos del árbol de trabajo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='cierre-main-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        origen = os.path.join(self.tmp, 'origin.git')
        subprocess.run(['git', 'clone', '--bare', '-q', RAIZ, origen], check=True, capture_output=True)
        # El origen de prueba es main, sin importar la rama que esté activa en el checkout de desarrollo.
        subprocess.run(['git', '--git-dir', origen, 'symbolic-ref', 'HEAD', 'refs/heads/main'], check=True,
                       capture_output=True)
        self.clon = os.path.join(self.tmp, 'clon')
        subprocess.run(['git', 'clone', '-q', origen, self.clon], check=True, capture_output=True)
        self.rama = subprocess.run(['git', '-C', self.clon, 'rev-parse', '--abbrev-ref', 'HEAD'],
                                   capture_output=True, text=True, check=True).stdout.strip()
        for rel in TOOLING_OPINIONES:
            destino = os.path.join(self.clon, rel)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copyfile(os.path.join(RAIZ, rel), destino)
        for cmd in (['config', 'user.email', 'prueba@example.test'], ['config', 'user.name', 'prueba']):
            subprocess.run(['git', '-C', self.clon, *cmd], check=True, capture_output=True)
        subprocess.run(['git', '-C', self.clon, 'add', 'infra'], check=True, capture_output=True)
        if subprocess.run(['git', '-C', self.clon, 'status', '--porcelain'], capture_output=True,
                          text=True).stdout.strip():
            subprocess.run(['git', '-C', self.clon, 'commit', '-q', '-m', 'prueba: tooling de opiniones'],
                           check=True, capture_output=True)
            subprocess.run(['git', '-C', self.clon, 'push', '-q', 'origin', self.rama], check=True,
                           capture_output=True)
        self.aws_bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.aws_bin)
        envoltorio = os.path.join(self.aws_bin, 'aws')
        with open(envoltorio, 'w', encoding='utf-8') as f:
            f.write(f'#!/bin/sh\nexec python3 "{FAKE_AWS}" "$@"\n')
        os.chmod(envoltorio, 0o755)
        self.estado = os.path.join(self.tmp, 'estado')
        os.makedirs(os.path.join(self.estado, 's3'))
        self.log = os.path.join(self.tmp, 'aws.log')

    def entorno(self, extra=None):
        env = dict(os.environ, AWS_CLI=os.path.join(self.aws_bin, 'aws'), FAKE_ADMIN_UI_ESTADO=self.estado,
                   FAKE_ADMIN_UI_LOG=self.log)
        env.pop('ALLOW_OPINIONES_PUBLISH', None)
        env.pop('OPINIONES_REPUBLICAR', None)
        env.update(extra or {})
        return env

    def correr(self, script, *args, extra=None):
        return subprocess.run(['bash', os.path.join(self.clon, 'infra', script), *args], env=self.entorno(extra),
                              cwd=self.clon, capture_output=True, text=True, timeout=300)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        return [json.loads(l) for l in leer(self.log).splitlines() if l.strip()]

    def test_la_rama_del_clon_es_main(self):
        self.assertEqual(self.rama, 'main')

    def test_diff_desde_main_pasa_la_guarda_y_no_escribe(self):
        r = self.correr('opiniones-publicar.sh', 'diff')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('(igual a origin/main)', r.stdout)
        ops = {' '.join(c.get('op', '').split()[:2]) for c in self.llamadas()}
        self.assertTrue(ops <= {'s3api head-object', 's3api get-object'}, f'diff llamó a {ops}')

    def test_diff_se_niega_con_el_repo_sucio(self):
        with open(os.path.join(self.clon, 'infra', 'opiniones-archivos.tsv'), 'a', encoding='utf-8') as f:
            f.write('# cambio sin commitear\n')
        r = self.correr('opiniones-publicar.sh', 'diff')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertIn('cambios sin commitear', r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_publicar_sin_la_marca_de_republicar_sale_con_3_sin_llamar_a_aws(self):
        r = self.correr('opiniones-publicar.sh', 'publicar', extra={'ALLOW_OPINIONES_PUBLISH': '1'})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn('OPINIONES_REPUBLICAR=1', r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_publicar_sin_allow_sigue_saliendo_con_3(self):
        r = self.correr('opiniones-publicar.sh', 'publicar', extra={'OPINIONES_REPUBLICAR': '1'})
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_verify_pre_exige_la_marca_historica_y_no_hace_peticiones(self):
        r = self.correr('opiniones-verify.sh', 'pre', extra={
            'SMOKE_NOTIFY_ID': '0123456789abcdef0123456789abcdef', 'CURL_CMD': '/bin/false', 'AWS_CLI': '/bin/false'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('OPINIONES_PRE_HISTORICO=1', r.stderr)
        self.assertIn('usar post', r.stderr)


if __name__ == '__main__':
    unittest.main()
