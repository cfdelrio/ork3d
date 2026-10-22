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
        llamadas = set(re.findall(r"\b(?:ro|leer)\(\s*'([a-z0-9]+)',\s*'([a-z0-9-]+)'", fuente))
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

    def test_iam_muestra_rol_politicas_y_recursos_enmascarados(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Rol: ork3d-resenas-lambda', r.stdout)
        self.assertIn('Políticas administradas adjuntas: AWSLambdaBasicExecutionRole', r.stdout)
        self.assertIn('Políticas inline: resenas-bucket-privado', r.stdout)
        self.assertIn('arn:aws:s3:::buck…/*', r.stdout)
        self.assertNotIn('arn:aws:s3:::bucket-privado', r.stdout)

    def test_prefijos_solo_informan_si_o_no(self):
        r = self.correr()
        lineas = [linea for linea in r.stdout.splitlines() if linea.startswith('  prefijo ')]
        self.assertEqual(len(lineas), 6, r.stdout)
        for linea in lineas:
            self.assertRegex(linea, r'^  prefijo (tokens|claims|resenas|pendientes|admin|notificaciones)/: (sí|no)$')

    def test_bucket_privado_se_inspecciona_aunque_la_copia_no_tenga_el_valor(self):
        # Caso A: la configuración en memoria trae RESENAS_BUCKET; la copia de rollback no.
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with open(self.log, encoding='utf-8') as f:
            self.assertIn('s3api get-bucket-versioning bucket=bucket-privado-fake-1234', f.read())
        with open(os.path.join(self.out, 'rollback', 'lambda-publica-configuracion.json'), encoding='utf-8') as f:
            copia = f.read()
        self.assertIn('<valor omitido>', copia)
        self.assertNotIn('bucket-privado-fake-1234', copia)

    def test_json_invalido_en_un_paso_se_reporta_con_comando_y_no_frena_el_bucket(self):
        # Caso A con fallo real: concurrencia devuelve algo que no es JSON. S3 e IAM deben seguir.
        r = self.correr({'FAKE_CONCURRENCIA_JSON_INVALIDO': '1'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('ERROR: lambda get-function-concurrency rc=0 tipo=json_invalido', r.stdout)
        self.assertNotIn('Expecting value', r.stdout + r.stderr)
        self.assertIn('Rol: ork3d-resenas-lambda', r.stdout)
        self.assertIn('Bucket privado: bucket-privado-fake-1234', r.stdout)
        self.assertIn('## 9. SES', r.stdout)

    def test_concurrencia_con_stdout_vacio_es_sin_concurrencia_y_no_error(self):
        # Caso D: rc 0 con stdout vacío es el contrato de "sin concurrencia reservada".
        r = self.correr({'FAKE_CONCURRENCIA_VACIA': '1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Concurrencia reservada: ninguna', r.stdout)
        self.assertNotIn('ERROR', r.stdout)

    def test_listas_iam_vacias_no_rompen_el_preflight(self):
        # Caso B: las listas de IAM responden vacío. Se trata como lista vacía.
        r = self.correr({'FAKE_IAM_LISTAS_VACIAS': '1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('Políticas administradas adjuntas: ninguna', r.stdout)
        self.assertIn('Políticas inline: ninguna', r.stdout)
        self.assertNotIn('ERROR', r.stdout)

    def test_rc_distinto_de_cero_en_iam_es_error_de_ese_paso(self):
        # Caso C: AccessDenied en get-role. Error atribuido a la operación; los pasos siguientes siguen.
        r = self.correr({'FAKE_IAM_RC': '1'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('ERROR: iam get-role rc=255 tipo=AccessDenied', r.stdout)
        self.assertNotIn('Expecting value', r.stdout + r.stderr)
        self.assertIn('## 6. CloudFront', r.stdout)


class ContratoDeLecturaTest(unittest.TestCase):
    """Cada respuesta de la CLI tiene su contrato. Nada se parsea sin mirar antes el rc y el stdout."""

    def cli(self, rc, out='', err=''):
        return mock.patch.object(pf.subprocess, 'run', return_value=mock.Mock(returncode=rc, stdout=out, stderr=err))

    def test_rc_distinto_de_cero_da_servicio_operacion_rc_y_tipo_sin_secretos(self):
        err = f'An error occurred (AccessDenied) when calling the GetRole operation: acceso con access_key={CLAVE}'
        with self.cli(255, err=err):
            with self.assertRaises(pf.ErrorAws) as ctx:
                pf.leer('iam', 'get-role', '--role-name', 'x')
        exc = ctx.exception
        self.assertEqual((exc.servicio, exc.operacion, exc.rc, exc.tipo), ('iam', 'get-role', 255, 'AccessDenied'))
        self.assertNotIn(CLAVE, str(exc))

    def test_stdout_vacio_sin_contrato_de_vacio_es_error(self):
        with self.cli(0, out='\n'):
            with self.assertRaises(pf.ErrorAws) as ctx:
                pf.leer('sesv2', 'get-account')
        self.assertEqual(ctx.exception.tipo, 'stdout_vacio')

    def test_stdout_vacio_con_contrato_de_lista_vacia_devuelve_vacio(self):
        with self.cli(0, out=''):
            self.assertEqual(pf.leer('iam', 'list-role-policies', '--role-name', 'x', vacio_ok=True), {})

    def test_salida_que_no_es_json_no_se_muestra(self):
        with self.cli(0, out='respuesta sin formato'):
            with self.assertRaises(pf.ErrorAws) as ctx:
                pf.leer('lambda', 'get-function-concurrency', '--function-name', 'x')
        self.assertEqual(ctx.exception.tipo, 'json_invalido')
        self.assertNotIn('respuesta sin formato', str(ctx.exception))

    def test_error_de_ausencia_esperado_devuelve_none(self):
        err = 'An error occurred (ResourceNotFoundException) when calling the GetPolicy operation: no existe'
        with self.cli(255, err=err):
            self.assertIsNone(pf.leer('lambda', 'get-policy', '--function-name', 'x',
                                      ausente=('ResourceNotFoundException',)))

    def test_error_no_esperado_no_se_oculta_como_ausencia(self):
        err = 'An error occurred (AccessDenied) when calling the GetPolicy operation: no autorizado'
        with self.cli(255, err=err):
            with self.assertRaises(pf.ErrorAws):
                pf.leer('lambda', 'get-policy', '--function-name', 'x', ausente=('ResourceNotFoundException',))

    def test_paso_registra_el_error_con_su_contexto_y_sigue(self):
        tmp = tempfile.mkdtemp(prefix='preflight-contrato-')
        self.addCleanup(shutil.rmtree, tmp, True)
        inf = pf.Informe(tmp)

        def fallo(_inf):
            pf.leer('iam', 'get-role', '--role-name', 'x')

        with self.cli(255, err='An error occurred (AccessDenied) when calling the GetRole operation: no'):
            self.assertIsNone(pf.paso(inf, 'IAM', fallo))
        self.assertTrue(any('ERROR: iam get-role rc=255 tipo=AccessDenied' in linea for linea in inf.lineas))
        self.assertEqual(len(inf.errores), 1)

    def test_ningun_json_loads_recibe_una_salida_de_la_cli_directamente(self):
        # Regla estática: el único parseo de salidas pasa por json_de(), después de mirar rc y stdout.
        with open(PREFLIGHT, encoding='utf-8') as f:
            fuente = f.read()
        self.assertIsNone(re.search(r'json\.loads\([^)]*\.out\b', fuente))
        self.assertEqual(len(re.findall(r'json\.loads\(', fuente)), 2)  # json_de() y la copia de sin_valores()


if __name__ == '__main__':
    unittest.main()
