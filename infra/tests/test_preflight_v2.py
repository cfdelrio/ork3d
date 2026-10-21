"""Pruebas del preflight de V2 (infra/preflight_v2.py): guard de solo lectura, redacción y enmascarado, y una
corrida completa contra el simulador de la CLI (infra/tests/aws_fake_preflight.py).

Sin AWS, sin red y sin credenciales: el simulador responde con datos de muestra y registra cada llamada.
"""
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
PREFLIGHT = os.path.join(RAIZ, 'infra', 'preflight_v2.py')
FAKE = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_preflight.py')
SENTINELA = 'VALOR-SENTINELA-NO-DEBE-SALIR-7731'
# Valores falsos con la forma de los reales; armados por partes para que el escaneo del repo no los marque.
CUENTA = '9' * 12
CLAVE = 'AKIA' + 'FAKE' * 4
SECRETO = 'no-es-un-secreto-real-' * 2

spec = importlib.util.spec_from_file_location('preflight_v2', PREFLIGHT)
pf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pf)

# Todas las lecturas que el preflight hace, más las que la lista permite.
LECTURAS = [
    ('sts', 'get-caller-identity'),
    ('lambda', 'get-function-configuration'), ('lambda', 'get-function-url-config'),
    ('lambda', 'get-policy'), ('lambda', 'get-function-concurrency'), ('lambda', 'list-functions'),
    ('iam', 'get-role'), ('iam', 'list-attached-role-policies'), ('iam', 'list-role-policies'),
    ('iam', 'get-role-policy'),
    ('cloudfront', 'list-distributions'), ('cloudfront', 'get-distribution'),
    ('cloudfront', 'get-distribution-config'), ('cloudfront', 'get-cache-policy'),
    ('cloudfront', 'get-origin-request-policy'), ('cloudfront', 'get-function'),
    ('cloudfront', 'list-functions'), ('cloudfront', 'list-origin-access-controls'),
    ('cloudfront', 'list-origin-request-policies'),
    ('s3api', 'get-bucket-versioning'), ('s3api', 'get-bucket-encryption'),
    ('s3api', 'get-public-access-block'), ('s3api', 'get-bucket-lifecycle-configuration'),
    ('s3api', 'get-bucket-cors'), ('s3api', 'get-bucket-policy'), ('s3api', 'get-bucket-website'),
    ('s3api', 'list-objects-v2'), ('s3api', 'head-object'),
    ('logs', 'describe-log-groups'),
    ('sesv2', 'get-account'), ('sesv2', 'list-email-identities'), ('sesv2', 'get-email-identity'),
]
MUTACIONES = [
    ('lambda', 'update-function-configuration'), ('lambda', 'update-function-code'),
    ('lambda', 'create-function'), ('lambda', 'delete-function'), ('lambda', 'invoke'),
    ('lambda', 'add-permission'), ('lambda', 'remove-permission'), ('lambda', 'create-function-url-config'),
    ('lambda', 'publish-version'), ('lambda', 'put-function-concurrency'),
    ('cloudfront', 'update-distribution'), ('cloudfront', 'create-invalidation'),
    ('cloudfront', 'create-origin-access-control'), ('cloudfront', 'create-origin-request-policy'),
    ('cloudfront', 'publish-function'), ('cloudfront', 'update-function'), ('cloudfront', 'tag-resource'),
    ('s3api', 'put-object'), ('s3api', 'delete-object'), ('s3api', 'delete-objects'),
    ('s3api', 'put-bucket-policy'), ('s3api', 'delete-bucket-policy'),
    ('s3api', 'put-bucket-lifecycle-configuration'), ('s3api', 'put-bucket-versioning'),
    ('s3api', 'create-bucket'), ('s3api', 'delete-bucket'),
    ('iam', 'create-role'), ('iam', 'attach-role-policy'), ('iam', 'put-role-policy'),
    ('iam', 'update-assume-role-policy'), ('iam', 'delete-role-policy'), ('iam', 'detach-role-policy'),
    ('sesv2', 'create-email-identity'), ('sesv2', 'send-email'), ('sesv2', 'put-account-sending-attributes'),
    ('sesv2', 'delete-email-identity'), ('sesv2', 'create-configuration-set'),
    ('logs', 'put-retention-policy'), ('logs', 'create-log-group'), ('logs', 'delete-log-group'),
    ('logs', 'put-subscription-filter'),
]
# Lecturas que no están en la lista de solo lectura (el preflight no debe hacerlas nunca).
FUERA_DE_LISTA = [
    ('cloudfront', 'describe-function'), ('ec2', 'describe-instances'), ('secretsmanager', 'get-secret-value'),
    ('ssm', 'get-parameter'), ('ssm', 'get-parameters-by-path'), ('kms', 'decrypt'),
    ('sts', 'assume-role'), ('dynamodb', 'scan'), ('s3api', 'select-object-content'),
]
# Lecturas que devuelven contenido sensible, aunque su nombre encaje con get-* o list-*.
CONTENIDO = [
    ('s3api', 'get-object'), ('s3api', 'get-object-acl'), ('s3api', 'get-object-tagging'),
    ('lambda', 'get-function'), ('lambda', 'get-layer-version'), ('lambda', 'get-layer-version-by-arn'),
    ('iam', 'get-credential-report'),
    ('sesv2', 'get-contact-list'), ('sesv2', 'list-contacts'), ('sesv2', 'get-contact'),
    ('sesv2', 'list-suppressed-destinations'), ('sesv2', 'get-suppressed-destination'),
]


class GuardTest(unittest.TestCase):
    def test_lecturas_de_la_lista_pasan(self):
        for servicio, operacion in LECTURAS:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertTrue(pf.permitida(servicio, operacion))

    def test_mutaciones_se_bloquean(self):
        for servicio, operacion in MUTACIONES:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertFalse(pf.permitida(servicio, operacion))

    def test_lecturas_fuera_de_lista_se_bloquean(self):
        for servicio, operacion in FUERA_DE_LISTA:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertFalse(pf.permitida(servicio, operacion))

    def test_contenido_sensible_se_bloquea(self):
        for servicio, operacion in CONTENIDO:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertFalse(pf.permitida(servicio, operacion))

    def test_cada_llamada_literal_del_preflight_esta_permitida(self):
        with open(PREFLIGHT, encoding='utf-8') as f:
            fuente = f.read()
        llamadas = set(re.findall(r"\bro\(\s*'([a-z0-9]+)',\s*'([a-z0-9-]+)'", fuente))
        self.assertGreater(len(llamadas), 20)
        for servicio, operacion in sorted(llamadas):
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertTrue(pf.permitida(servicio, operacion))

    def test_ro_no_llama_a_la_cli_si_la_operacion_no_esta_permitida(self):
        with mock.patch.object(pf.subprocess, 'run') as run:
            with self.assertRaises(pf.OperacionNoPermitida):
                pf.ro('cloudfront', 'describe-function', '--name', 'x')
            run.assert_not_called()

    def test_paso_no_traga_una_operacion_no_permitida(self):
        tmp = tempfile.mkdtemp(prefix='preflight-guard-')
        self.addCleanup(shutil.rmtree, tmp, True)
        inf = pf.Informe(tmp)

        def intento(_inf):
            pf.ro('iam', 'create-role', '--role-name', 'x')

        with mock.patch.object(pf.subprocess, 'run') as run:
            with self.assertRaises(pf.OperacionNoPermitida):
                pf.paso(inf, 'prueba', intento)
            run.assert_not_called()

    def test_ro_pasa_perfil_y_region_y_solo_lee(self):
        entorno = {'AWS_PROFILE': 'perfil-x', 'AWS_REGION': 'us-east-1', 'AWS_CLI': 'aws-simulado'}
        with mock.patch.dict(os.environ, entorno, clear=True), mock.patch.object(pf.subprocess, 'run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='{}', stderr='')
            pf.ro('sts', 'get-caller-identity')
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[:3], ['aws-simulado', 'sts', 'get-caller-identity'])
        self.assertEqual(run.call_args[1]['env']['AWS_PROFILE'], 'perfil-x')
        self.assertEqual(run.call_args[1]['env']['AWS_REGION'], 'us-east-1')

    def test_perfil_y_region_por_defecto(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pf.perfil(), 'default')
            self.assertEqual(pf.region(), 'us-east-1')


class RedaccionTest(unittest.TestCase):
    def test_redactar_quita_claves_secretos_y_cuentas(self):
        texto = (f'An error occurred (InvalidClientTokenId): access_key={CLAVE} '
                 f'secret={SECRETO} cuenta {CUENTA}')
        salida = pf.redactar(texto)
        for sensible in (CLAVE, SECRETO, CUENTA):
            self.assertNotIn(sensible, salida)
        self.assertIn('****' + CUENTA[-4:], salida)

    def test_correo_y_cuenta_enmascarados(self):
        self.assertEqual(pf.correo_enmascarado('ana.moderadora@example.com'), 'a***@e***.com')
        self.assertEqual(pf.enmascarar(f'cuenta {CUENTA}'), f'cuenta ****{CUENTA[-4:]}')

    def test_bucket_en_arn_de_s3_enmascarado(self):
        self.assertEqual(pf.enmascarar_recursos('arn:aws:s3:::bucket-privado-fake-1234/*'),
                         'arn:aws:s3:::buck…/*')

    def test_sin_valores_oculta_los_valores_y_no_toca_el_original(self):
        cfg = {'FunctionName': 'x', 'Environment': {'Variables': {'GOOGLE_REVIEW_URL': 'https://g.page/r/secreto'}}}
        copia = pf.sin_valores(cfg)
        self.assertEqual(copia['Environment']['Variables'], {'GOOGLE_REVIEW_URL': '<valor omitido>'})
        self.assertEqual(cfg['Environment']['Variables']['GOOGLE_REVIEW_URL'], 'https://g.page/r/secreto')


class CorridaSimuladaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='preflight-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.out = os.path.join(self.tmp, 'salida')

    def correr(self, extra=None):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE, 'FAKE_LOG': self.log,
               'AWS_PROFILE': 'default', 'AWS_REGION': 'us-east-1'}
        env.update(extra or {})
        return subprocess.run([sys.executable, PREFLIGHT, self.out], env=env, capture_output=True, text=True,
                              timeout=120)

    def llamadas(self):
        with open(self.log, encoding='utf-8') as f:
            return [tuple(linea.split()[:2]) for linea in f if linea.strip()]

    def archivos_de_salida(self):
        for raiz, _, nombres in os.walk(self.out):
            for nombre in nombres:
                yield os.path.join(raiz, nombre)

    def test_corrida_completa_solo_lee_y_no_filtra_nada(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(os.path.join(self.out, 'resumen.txt'), encoding='utf-8') as f:
            visible = r.stdout + r.stderr + f.read()
        for prohibido in (SENTINELA, CUENTA, 'ana.moderadora', CLAVE, SECRETO, 'g.page'):
            self.assertNotIn(prohibido, visible)
        for ruta in self.archivos_de_salida():
            with open(ruta, encoding='utf-8', errors='replace') as f:
                contenido = f.read()
            for prohibido in (SENTINELA, 'ana.moderadora', CLAVE, SECRETO, 'g.page'):
                self.assertNotIn(prohibido, contenido, os.path.basename(ruta))

        llamadas = self.llamadas()
        self.assertGreater(len(llamadas), 30)
        for servicio, operacion in sorted(set(llamadas)):
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertTrue(pf.permitida(servicio, operacion))
        for esperada in (('sts', 'get-caller-identity'), ('cloudfront', 'list-functions'),
                         ('cloudfront', 'get-function'), ('s3api', 'list-objects-v2')):
            self.assertIn(esperada, llamadas)

    def test_archivos_de_salida_con_permisos_600(self):
        self.correr()
        for ruta in self.archivos_de_salida():
            with self.subTest(archivo=os.path.basename(ruta)):
                self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600)

    def test_si_falla_la_autenticacion_sale_con_1_y_no_muestra_secretos(self):
        r = self.correr({'FAKE_STS_FAIL': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no se pudo autenticar', r.stderr)
        for prohibido in (CLAVE, SECRETO, CUENTA):
            self.assertNotIn(prohibido, r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [('sts', 'get-caller-identity')])


if __name__ == '__main__':
    unittest.main()
