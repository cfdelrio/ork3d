"""Pruebas del único script de infra que escribe en AWS: infra/smoke-if-match.sh.

Corre el smoke contra el simulador de S3 (infra/tests/aws_fake_s3.py): sin red, sin credenciales y sin bucket
real. Cubre el aborto sin ALLOW_SMOKE_WRITE, el flujo completo con limpieza, la violación de If-Match (ganan
ambos writers), un bucket ausente, una limpieza incompleta y las garantías estáticas del script.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SMOKE = os.path.join(RAIZ, 'infra', 'smoke-if-match.sh')
FAKE = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_s3.py')
PREFIJO = 'admin/smoke-if-match/'
CLAVE_RE = re.compile(r'^admin/smoke-if-match/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$')
PROHIBIDAS = ('tokens/', 'claims/', 'pendientes/', 'resenas/')
HEX32 = re.compile(r'\b[0-9a-f]{32}\b')
LIMPIEZA_OK = r'errores de borrado=0 · restantes=0 · key visible=0'


class SmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='smoke-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.out = os.path.join(self.tmp, 'salida')

    def correr(self, permitir=True, extra=None):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE, 'FAKE_STATE': self.estado,
               'FAKE_LOG': self.log, 'AWS_PROFILE': 'default', 'AWS_REGION': 'us-east-1', 'SMOKE_OUT': self.out}
        if permitir:
            env['ALLOW_SMOKE_WRITE'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', SMOKE], env=env, capture_output=True, text=True, timeout=300)

    def llamadas(self):
        with open(self.log, encoding='utf-8') as f:
            return [linea.split(' ') for linea in f.read().splitlines() if linea.strip()]

    def claves_tocadas(self):
        return {p[2] for p in self.llamadas() if p[0] == 's3api' and len(p) > 2 and p[2]}

    def estado_claves(self):
        with open(self.estado, encoding='utf-8') as f:
            return json.load(f)['keys']

    def log_del_smoke(self):
        ruta = glob.glob(os.path.join(self.out, 'smoke-if-match-*.log'))
        self.assertEqual(len(ruta), 1)
        with open(ruta[0], encoding='utf-8') as f:
            return f.read()

    def test_sin_allow_aborta_sin_llamar_a_aws(self):
        r = self.correr(permitir=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('ABORTA', r.stderr)
        self.assertFalse(os.path.exists(self.log))
        self.assertFalse(os.path.exists(self.estado))

    def test_flujo_completo_deja_el_bucket_limpio(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for esperado in ('1. crear {"version":1}: OK', 'rechazado con PreconditionFailed (HTTP 412',
                         'If-Match con el ETag vigente: OK', 'ganadores=1 · perdedores=1',
                         'código del perdedor=PreconditionFailed (HTTP 412',
                         'objeto final escrito por el writer', 'Pruebas 1 a 9: OK'):
            self.assertIn(esperado, r.stdout)
        self.assertRegex(r.stdout, LIMPIEZA_OK)
        self.assertEqual(self.estado_claves(), {})

        claves = self.claves_tocadas()
        self.assertEqual(len(claves), 1)
        clave = claves.pop()
        self.assertRegex(clave, CLAVE_RE)
        for prohibida in PROHIBIDAS:
            self.assertFalse(clave.startswith(prohibida))

    def test_no_imprime_etags_ni_ids_de_version(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(HEX32.search(r.stdout + r.stderr))
        self.assertIsNone(HEX32.search(self.log_del_smoke()))

    def test_si_ganan_ambos_writers_aborta_con_4_y_limpia(self):
        r = self.correr(extra={'FAKE_IGNORE_IFMATCH_BODIES': 'wa.json,wb.json'})
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn('ambos writers ganaron', r.stdout)
        self.assertRegex(r.stdout, LIMPIEZA_OK)
        self.assertEqual(self.estado_claves(), {})

    def test_sin_bucket_aborta_con_2_antes_de_escribir(self):
        r = self.correr(extra={'FAKE_NO_BUCKET': '1'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('no hay bucket privado configurado', r.stdout)
        self.assertEqual(self.claves_tocadas(), set())
        self.assertFalse(os.path.exists(self.estado))

    def test_limpieza_incompleta_sale_con_5(self):
        r = self.correr(extra={'FAKE_NO_DELETE': '1'})
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn('la limpieza no quedó completa', r.stdout)
        self.assertTrue(any(k.startswith(PREFIJO) for k in self.estado_claves()))


class SmokeEstaticoTest(unittest.TestCase):
    def setUp(self):
        with open(SMOKE, encoding='utf-8') as f:
            self.texto = f.read()

    def test_es_ejecutable(self):
        self.assertTrue(os.access(SMOKE, os.X_OK))

    def test_prefijo_fijo_y_sin_borrados_masivos(self):
        self.assertIn('PREFIJO="admin/smoke-if-match/"', self.texto)
        for prohibido in ('s3 rm', 'delete-objects', '--recursive', 's3 sync', 's3 mv'):
            self.assertNotIn(prohibido, self.texto)

    def test_la_comprobacion_de_allow_va_antes_de_cualquier_llamada_a_aws(self):
        self.assertLess(self.texto.index('ALLOW_SMOKE_WRITE'), self.texto.index('"$AWS" '))


if __name__ == '__main__':
    unittest.main()
