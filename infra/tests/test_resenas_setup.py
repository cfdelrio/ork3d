"""Pruebas del script de infraestructura de reseñas (infra/resenas-setup.sh) y de sus políticas.

Sin AWS, sin red y sin credenciales:
- Pruebas estáticas: leen el script y las plantillas JSON.
- Pruebas de ejecución: corren el script real, copiado a un directorio temporal, con un comando
  `aws` falso que registra cada llamada y responde valores fijos.

ORK3D_ROOT permite correr las mismas pruebas contra otra copia del repo (por ejemplo, una versión
anterior) para comprobar que fallan con el código viejo.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
ORIGEN = os.environ.get('ORK3D_ROOT', RAIZ)
SCRIPT = 'infra/resenas-setup.sh'
MUTACION = re.compile(
    r'^(s3api (create|put)|iam (create|put|attach)|lambda (create|update|put|add)|logs (create|put)|cloudfront (update|publish))')

# Comando `aws` falso: registra la llamada y responde valores fijos. No hace red ni usa credenciales.
AWS_FALSO = r'''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
s = " ".join(args)
q = "--query" in args
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as registro:
    registro.write(s + "\n")


def salir(texto="", codigo=0):
    if texto:
        sys.stdout.write(texto + "\n")
    sys.exit(codigo)


def fallar(texto):
    sys.stderr.write(texto + "\n")
    sys.exit(254)


if args[:1] == ["--version"]:
    salir("aws-cli/2.17.0 falso")
if s.startswith("sts get-caller-identity"):
    salir("cuenta-de-prueba")
if s.startswith("s3api head-bucket"):
    if "--bucket ork3d.com" in s:
        salir()
    fallar("Not Found")
if s.startswith("cloudfront get-distribution-config"):
    salir("ork3d.com")
if s.startswith("cloudfront describe-function"):
    salir("ETAG-PRUEBA" if "ETag" in s else "ork3d-redirect-www")
if s.startswith("iam get-role"):
    if q:
        salir("arn:aws:iam::cuenta-de-prueba:role/ork3d-resenas-lambda")
    fallar("NoSuchEntity")
if s.startswith("lambda get-function "):
    fallar("ResourceNotFoundException")
if s.startswith("lambda get-function-url-config"):
    if q:
        salir("http://127.0.0.1:9/")
    fallar("ResourceNotFoundException")
if s.startswith("lambda get-account-settings"):
    salir(os.environ.get("STUB_CUENTA", '{"UnreservedConcurrentExecutions": 100, "ConcurrentExecutions": 1000}'))
if s.startswith("lambda get-function-concurrency"):
    salir(os.environ.get("STUB_RESERVADA") or "None")
if s.startswith("lambda get-policy"):
    politica = os.environ.get("STUB_POLITICA", "")
    if politica:
        salir(politica)
    fallar("ResourceNotFoundException: no policy")
if "add-permission help" in s:
    salir("usage: aws lambda add-permission [--function-url-auth-type <value>] [--invoked-via-function-url]")
if s.startswith("logs describe-log-groups"):
    salir("/aws/lambda/ork3d-resenas" if os.environ.get("STUB_LOGGROUP") == "1" else "")
salir()
'''


def leer(rel):
    with open(os.path.join(ORIGEN, rel), encoding='utf-8') as f:
        return f.read()


def sentencias(rel):
    return json.loads(leer(rel))['Statement']


def mutaciones(lineas):
    # `aws lambda add-permission help` es una consulta de ayuda, no una mutación.
    return [linea for linea in lineas if MUTACION.match(linea) and not linea.endswith(' help')]


def permisos_creados(lineas):
    # Un permiso real siempre lleva --statement-id; la ayuda del chequeo previo no.
    return [linea for linea in lineas if linea.startswith('lambda add-permission') and '--statement-id' in linea]


class Repo:
    """Copia mínima del repo (script, plantillas, redirect y Lambda) en un directorio temporal."""

    def __init__(self, caso):
        self.dir = tempfile.mkdtemp(prefix='resenas-infra-')
        caso.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        for rel in [SCRIPT, 'infra/redirect-www.js', 'api/resenas/app.py']:
            destino = os.path.join(self.dir, rel)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            shutil.copy(os.path.join(ORIGEN, rel), destino)
        shutil.copytree(os.path.join(ORIGEN, 'infra', 'resenas'), os.path.join(self.dir, 'infra', 'resenas'))
        # El script empaqueta el núcleo V2 con la Lambda pública y se detiene si falta: la copia lo incluye.
        shutil.copytree(os.path.join(ORIGEN, 'api', 'resenas', 'nucleo'), os.path.join(self.dir, 'api', 'resenas', 'nucleo'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.bin = os.path.join(self.dir, 'bin')
        os.makedirs(self.bin)
        aws = os.path.join(self.bin, 'aws')
        with open(aws, 'w', encoding='utf-8') as f:
            f.write(AWS_FALSO)
        os.chmod(aws, 0o755)
        self.registro = os.path.join(self.dir, 'llamadas.log')

    def ejecutar(self, dry_run, entrada='', **extra):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith('AWS_') and k not in ('BUCKET', 'DISTRIBUTION_ID', 'RESENAS_BUCKET')}
        env.update({
            'PATH': self.bin + os.pathsep + os.environ.get('PATH', ''),
            'STUB_LOG': self.registro,
            'BUCKET': 'ork3d.com',
            'DISTRIBUTION_ID': 'E1PRUEBAABCDEF',
            'AWS_REGION': 'us-east-1',
            'IAM_PROPAGATION_SECONDS': '0',
            'DRY_RUN': '1' if dry_run else '0',
        })
        env.update(extra)
        return subprocess.run(['bash', os.path.join(self.dir, SCRIPT)], cwd=self.dir, env=env,
                              input=entrada, capture_output=True, text=True, timeout=120)

    def llamadas(self):
        if not os.path.exists(self.registro):
            return []
        with open(self.registro, encoding='utf-8') as f:
            return [linea.strip() for linea in f if linea.strip()]


class PoliticasYScriptEstaticos(unittest.TestCase):
    def test_lambda_permisos_incluye_listbucket_solo_sobre_el_bucket_de_resenas(self):
        listas = [s for s in sentencias('infra/resenas/lambda-permisos.json') if s['Action'] == 's3:ListBucket']
        self.assertEqual(len(listas), 1, 'debe haber exactamente una sentencia s3:ListBucket')
        self.assertEqual(listas[0]['Effect'], 'Allow')
        self.assertEqual(listas[0]['Resource'], 'arn:aws:s3:::__RESENAS_BUCKET__')
        self.assertNotIn('__SITE_BUCKET__', json.dumps(listas[0]))

    def test_sin_comodines_ni_s3_full_access(self):
        for rel in ['infra/resenas/lambda-permisos.json', 'infra/resenas/admin-permisos.json']:
            for s in sentencias(rel):
                acciones = s['Action'] if isinstance(s['Action'], list) else [s['Action']]
                recursos = s['Resource'] if isinstance(s['Resource'], list) else [s['Resource']]
                self.assertNotIn('s3:*', acciones, f'{rel}: {s.get("Sid")}')
                self.assertNotIn('*', recursos, f'{rel}: {s.get("Sid")}')
        self.assertNotIn('AmazonS3FullAccess', leer(SCRIPT))

    def test_script_declara_los_dos_permisos_de_function_url(self):
        texto = leer(SCRIPT)
        self.assertIn('--statement-id FunctionURLAllowPublicAccess', texto)
        self.assertIn('--action lambda:InvokeFunctionUrl', texto)
        self.assertIn('--function-url-auth-type NONE', texto)
        self.assertIn('--statement-id FunctionURLAllowInvokeViaUrl', texto)
        self.assertIn('--action lambda:InvokeFunction ', texto)

    def test_segundo_permiso_usa_invoked_via_function_url(self):
        texto = leer(SCRIPT)
        i = texto.index('--statement-id FunctionURLAllowInvokeViaUrl')
        bloque = texto[i:i + 200]
        self.assertIn('--action lambda:InvokeFunction ', bloque)
        self.assertIn('--invoked-via-function-url', bloque)

    def test_log_group_se_crea_antes_de_la_lambda_con_retencion_30(self):
        texto = leer(SCRIPT)
        crear_log = texto.index('logs create-log-group')
        retencion = texto.index('logs put-retention-policy')
        crear_lambda = texto.index('lambda create-function --function-name')
        self.assertLess(crear_log, crear_lambda)
        self.assertLess(crear_log, retencion)
        self.assertLess(retencion, crear_lambda)
        self.assertIn('--retention-in-days 30', texto)

    def test_concurrencia_consulta_el_margen_antes_de_reservar(self):
        texto = leer(SCRIPT)
        self.assertIn('get-account-settings', texto)
        self.assertLess(texto.index('get-account-settings'), texto.index('put-function-concurrency'))
        self.assertIn('Reserved concurrency omitida: límite de cuenta insuficiente.', texto)

    def test_no_hay_operaciones_destructivas(self):
        texto = leer(SCRIPT)
        for prohibido in ['delete-', '--recursive', 's3 rm', 'update-distribution', 'detach-role-policy', 'rm -rf /']:
            self.assertNotIn(prohibido, texto)

    def test_dry_run_es_el_valor_por_defecto(self):
        self.assertIn('DRY_RUN="${DRY_RUN:-1}"', leer(SCRIPT))


class EjecucionConAwsFalso(unittest.TestCase):
    def test_dry_run_no_llama_a_aws(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(repo.llamadas(), [])
        self.assertIn('DRY_RUN=1: no se llama a AWS', r.stdout)
        self.assertGreaterEqual(r.stdout.count('[dry-run] aws '), 15)
        self.assertNotIn('  $ aws ', r.stdout)

    def test_modo_real_crea_los_dos_permisos_de_function_url(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        permisos = permisos_creados(repo.llamadas())
        self.assertEqual(len(permisos), 2, permisos)
        self.assertTrue(any('--statement-id FunctionURLAllowPublicAccess' in p
                            and '--function-url-auth-type NONE' in p for p in permisos), permisos)
        self.assertTrue(any('--statement-id FunctionURLAllowInvokeViaUrl' in p
                            and 'lambda:InvokeFunction' in p
                            and '--invoked-via-function-url' in p for p in permisos), permisos)

    def test_permisos_ya_existentes_no_se_vuelven_a_crear(self):
        arn = 'arn:aws:lambda:us-east-1:cuenta-de-prueba:function:ork3d-resenas'
        politica = json.dumps({'Version': '2012-10-17', 'Statement': [
            {'Sid': 'manual-1', 'Effect': 'Allow', 'Principal': '*', 'Action': 'lambda:InvokeFunctionUrl',
             'Resource': arn, 'Condition': {'StringEquals': {'lambda:FunctionUrlAuthType': 'NONE'}}},
            {'Sid': 'manual-2', 'Effect': 'Allow', 'Principal': '*', 'Action': 'lambda:InvokeFunction',
             'Resource': arn, 'Condition': {'Bool': {'lambda:InvokedViaFunctionUrl': 'true'}}},
        ]})
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n', STUB_POLITICA=politica)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(permisos_creados(repo.llamadas()), [])
        self.assertIn('ya existe', r.stdout)

    def test_concurrencia_sin_margen_no_aborta_ni_toca_la_funcion(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n',
                          STUB_CUENTA='{"UnreservedConcurrentExecutions": 10, "ConcurrentExecutions": 10}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Reserved concurrency omitida: límite de cuenta insuficiente.', r.stdout)
        self.assertEqual([l for l in repo.llamadas() if 'put-function-concurrency' in l], [])
        self.assertIn('Listo. Próximos pasos', r.stdout)

    def test_concurrencia_con_margen_la_configura_solo_para_la_funcion(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n',
                          STUB_CUENTA='{"UnreservedConcurrentExecutions": 100, "ConcurrentExecutions": 1000}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('lambda put-function-concurrency --function-name ork3d-resenas '
                      '--reserved-concurrent-executions 5', repo.llamadas())

    def test_log_group_se_crea_antes_de_la_lambda_con_retencion_30(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n', STUB_LOGGROUP='')
        self.assertEqual(r.returncode, 0, r.stderr)
        ll = repo.llamadas()
        i_crear = ll.index('logs create-log-group --log-group-name /aws/lambda/ork3d-resenas')
        i_retencion = ll.index('logs put-retention-policy --log-group-name /aws/lambda/ork3d-resenas '
                               '--retention-in-days 30')
        i_lambda = next(i for i, l in enumerate(ll) if l.startswith('lambda create-function --function-name ork3d-resenas'))
        self.assertLess(i_crear, i_retencion)
        self.assertLess(i_retencion, i_lambda)

    def test_log_group_existente_no_se_recrea(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n', STUB_LOGGROUP='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        ll = repo.llamadas()
        self.assertEqual([l for l in ll if l.startswith('logs create-log-group')], [])
        self.assertIn('logs put-retention-policy --log-group-name /aws/lambda/ork3d-resenas '
                      '--retention-in-days 30', ll)

    def test_cancelar_no_hace_mutaciones(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='no\n')
        self.assertEqual(r.returncode, 1)
        self.assertIn('Cancelado. No se modificó AWS.', r.stdout)
        self.assertEqual(mutaciones(repo.llamadas()), [])

    def test_modo_real_no_hace_operaciones_destructivas(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        for linea in repo.llamadas():
            self.assertNotIn('delete', linea)
            self.assertNotIn('--recursive', linea)

    def test_modo_real_verifica_la_api_y_el_preflight_cors(self):
        repo = Repo(self)
        r = repo.ejecutar(dry_run=False, entrada='CREAR\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('GET /tokens/<falso>', r.stdout)
        self.assertIn('OPTIONS /resenas', r.stdout)


if __name__ == '__main__':
    unittest.main()
