"""Pruebas del plan de infraestructura del panel admin V2 (infra/admin-setup.sh) y de sus plantillas.

Sin AWS, sin red y sin credenciales:
- El script se corre en una copia temporal de infra/, con un entorno limpio (sin variables AWS_*) y
  con `aws`, `curl` y `wget` falsos que registran cualquier llamada. Si alguno se ejecuta, la prueba falla.
- Las plantillas JSON se revisan con Python. La regla de redirección de CloudFront se ejecuta con node (vm).
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = 'infra/admin-setup.sh'
# Shim: registra la llamada y falla. Se usa para aws, curl y wget.
SHIM = '#!/bin/sh\necho "$(basename "$0") $*" >> "$STUB_LOG"\nexit 99\n'
EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}')
CUENTA_RE = re.compile(r'\b\d{12}\b')
CLAVE_RE = re.compile(r'AKIA[0-9A-Z]{16}')
MUTACION_DESTRUCTIVA = re.compile(r'delete-|--recursive|s3 rm|update-distribution')

# node: carga la regla de CloudFront en un contexto vm y la ejecuta sobre cada caso.
JS_PROGRAMA = r"""
const fs = require('fs');
const vm = require('vm');
const codigo = fs.readFileSync(process.argv[1], 'utf8');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(codigo, ctx);
const casos = JSON.parse(process.argv[2]);
const salida = casos.map((c) => {
  const request = { uri: c.uri, headers: { host: { value: c.host || 'ork3d.com' } }, querystring: {} };
  const r = ctx.handler({ request: request });
  return {
    uri: r.uri === undefined ? null : r.uri,
    statusCode: r.statusCode === undefined ? null : r.statusCode,
    location: r.headers && r.headers.location ? r.headers.location.value : null,
  };
});
process.stdout.write(JSON.stringify(salida));
"""


def leer(rel):
    with open(os.path.join(RAIZ, rel), encoding='utf-8') as f:
        return f.read()


def sentencias(rel):
    return json.loads(leer(rel))['Statement']


class PlanDryRun(unittest.TestCase):
    """Corre infra/admin-setup.sh en una copia temporal con un entorno limpio."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='admin-setup-')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.raiz = os.path.join(self.tmp, 'repo')
        shutil.copytree(os.path.join(RAIZ, 'infra'), os.path.join(self.raiz, 'infra'),
                        ignore=shutil.ignore_patterns('tests', '__pycache__', '.env'))
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        for comando in ('aws', 'curl', 'wget'):
            ruta = os.path.join(self.bin, comando)
            with open(ruta, 'w', encoding='utf-8') as f:
                f.write(SHIM)
            os.chmod(ruta, 0o755)
        self.log = os.path.join(self.tmp, 'llamadas.log')

    def entorno(self, **cambios):
        env = {
            'PATH': f'{self.bin}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin',
            'HOME': self.tmp,
            'STUB_LOG': self.log,
            'RESENAS_BUCKET': 'ork3d-resenas-privado-prueba',
            'BUCKET': 'ork3d.com',
            'DISTRIBUTION_ID': 'EPRUEBAABCDEF',
            'RESENAS_ADMIN_EMAIL': 'admin@ejemplo.invalid',
            'RESENAS_FROM_EMAIL': 'noreply@ejemplo.invalid',
        }
        env.update(cambios)
        return {k: v for k, v in env.items() if v is not None}

    def correr(self, **cambios):
        return subprocess.run(['bash', os.path.join(self.raiz, SCRIPT)], cwd=self.raiz,
                              env=self.entorno(**cambios), capture_output=True, text=True, timeout=60)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return f.read().splitlines()

    def test_dry_run_es_el_valor_por_defecto(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('Modo: DRY_RUN=1', r.stdout)

    def test_dry_run_0_queda_bloqueado_sin_llamar_a_nada(self):
        r = self.correr(DRY_RUN='0')
        self.assertEqual(r.returncode, 2)
        self.assertIn('no ejecuta AWS', r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_ninguna_herramienta_de_red_se_invoca(self):
        r = self.correr(DRY_RUN='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.llamadas(), [], 'el dry-run no debe invocar aws, curl ni wget')

    def test_cada_linea_con_aws_es_simulada_o_plan(self):
        r = self.correr()
        # Invocaciones de la CLI: "aws <servicio>". No cuentan rutas como /aws/lambda/ ni ARNs arn:aws:.
        for linea in r.stdout.splitlines():
            if re.search(r'(?<![\w/:])aws\s', linea):
                self.assertTrue(linea.startswith('  [dry-run] aws ') or linea.startswith('  PLAN aws '), linea)

    def test_sin_operaciones_destructivas_ni_update_real(self):
        r = self.correr()
        for linea in r.stdout.splitlines():
            if MUTACION_DESTRUCTIVA.search(linea):
                self.assertTrue(linea.startswith('  PLAN '), f'solo puede aparecer como plan: {linea}')
        self.assertNotIn('delete-', r.stdout)
        self.assertNotIn('--recursive', r.stdout)
        self.assertNotIn('s3 rm', r.stdout)

    def test_plan_completo_con_contadores(self):
        r = self.correr()
        for fragmento in ('create-role', 'put-role-policy', 'attach-role-policy', 'create-log-group',
                          'put-retention-policy', 'create-function ', 'function-active-v2',
                          'create-function-url-config', '--auth-type AWS_IAM', 'create-origin-access-control',
                          'cloudfront.amazonaws.com', 'update-distribution', 'put-bucket-lifecycle-configuration',
                          'update-function', 'publish-function', '--function-name ork3d-resenas-admin',
                          'python3.12', 'admin_app.handler', '--timeout 10', '--memory-size 256'):
            self.assertIn(fragmento, r.stdout, fragmento)
        self.assertIn('Resumen: 7 llamadas simuladas (dry-run), 8 comandos PLAN', r.stdout)
        self.assertIn('0 llamadas reales', r.stdout)

    def test_variables_de_correo_son_obligatorias_y_no_se_inventan(self):
        for variable in ('RESENAS_ADMIN_EMAIL', 'RESENAS_FROM_EMAIL'):
            r = self.correr(**{variable: None})
            self.assertNotEqual(r.returncode, 0, variable)
            self.assertIn(variable, r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_correo_con_formato_invalido_corta(self):
        r = self.correr(RESENAS_FROM_EMAIL='no-es-un-correo')
        self.assertEqual(r.returncode, 1)
        self.assertIn('RESENAS_FROM_EMAIL no tiene formato', r.stderr)

    def test_correos_se_muestran_enmascarados(self):
        r = self.correr()
        self.assertNotIn('admin@ejemplo.invalid', r.stdout)
        self.assertNotIn('noreply@ejemplo.invalid', r.stdout)
        self.assertIn('a***@ejemplo.invalid', r.stdout)
        self.assertIn('n***@ejemplo.invalid', r.stdout)

    def test_politica_y_entorno_renderizados_sin_placeholders(self):
        r = self.correr(KEEP_WORK='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r'KEEP_WORK=1: archivos renderizados conservados en (\S+)', r.stdout)
        self.assertIsNotNone(m, r.stdout[-400:])
        trabajo = m.group(1)
        self.addCleanup(shutil.rmtree, trabajo, ignore_errors=True)
        with open(os.path.join(trabajo, 'admin-permisos.json'), encoding='utf-8') as f:
            politica = f.read()
        self.assertNotRegex(politica, r'__[A-Z_]+__')
        self.assertIn('ses:FromAddress', politica)
        self.assertIn('noreply@ejemplo.invalid', politica)
        self.assertIn('admin@ejemplo.invalid', politica)
        self.assertIn('EPRUEBAABCDEF', politica)
        with open(os.path.join(trabajo, 'admin-env.json'), encoding='utf-8') as f:
            variables = json.load(f)['Variables']
        self.assertEqual(variables['RESENAS_ADMIN_EMAIL'], 'admin@ejemplo.invalid')
        self.assertEqual(variables['RESENAS_FROM_EMAIL'], 'noreply@ejemplo.invalid')
        self.assertEqual(variables['RESENAS_TOKEN_DAYS'], '60')
        self.assertEqual(variables['RESENAS_SESION_HORAS'], '8')
        self.assertEqual(variables['RESENAS_LOGIN_MINUTOS'], '10')
        self.assertEqual(variables['RESENAS_LOGIN_LIMITE'], '3')
        self.assertEqual(variables['RESENAS_LOGIN_VENTANA_MIN'], '15')
        self.assertEqual(self.llamadas(), [])


class PoliticasYLifecycle(unittest.TestCase):
    def test_politica_admin_sin_comodines_ni_s3_completo(self):
        for s in sentencias('infra/resenas/admin-lambda-permisos.json'):
            acciones = s['Action'] if isinstance(s['Action'], list) else [s['Action']]
            recursos = s['Resource'] if isinstance(s['Resource'], list) else [s['Resource']]
            self.assertNotIn('s3:*', acciones, s['Sid'])
            self.assertNotIn('*', acciones, s['Sid'])
            self.assertNotIn('*', recursos, s['Sid'])

    def test_politica_admin_alcance_por_bucket_y_prefijo(self):
        pol = sentencias('infra/resenas/admin-lambda-permisos.json')
        por_sid = {s['Sid']: s for s in pol}
        self.assertEqual(por_sid['InvalidarCacheDelSitio']['Resource'],
                         'arn:aws:cloudfront::__ACCOUNT_ID__:distribution/__DISTRIBUTION_ID__')
        self.assertEqual(por_sid['PublicarResenasEnElSitio']['Resource'], 'arn:aws:s3:::__SITE_BUCKET__/resenas/*')
        self.assertEqual(por_sid['ListarSoloResenasDelSitio']['Condition']['StringLike']['s3:prefix'], ['resenas/*'])
        # Sin condición en el listado del bucket privado: sin ella, S3 respondería AccessDenied en vez de NotFound.
        self.assertNotIn('Condition', por_sid['ListarBucketPrivadoParaDistinguirAusentes'])
        self.assertEqual(por_sid['ListarBucketPrivadoParaDistinguirAusentes']['Resource'],
                         'arn:aws:s3:::__RESENAS_BUCKET__')
        # Ninguna escritura ni borrado sobre pendientes/: el panel no toca fotos privadas.
        for s in pol:
            if 'PutObject' in str(s['Action']) or 'DeleteObject' in str(s['Action']):
                recursos = s['Resource'] if isinstance(s['Resource'], list) else [s['Resource']]
                for recurso in recursos:
                    self.assertNotIn('pendientes/', recurso, s['Sid'])

    def test_politica_admin_envio_de_correo_con_condiciones(self):
        por_sid = {s['Sid']: s for s in sentencias('infra/resenas/admin-lambda-permisos.json')}
        ses = por_sid['EnviarCorreoSoloEntreDireccionesConfiguradas']
        self.assertEqual(ses['Action'], 'ses:SendEmail')
        self.assertEqual(ses['Condition']['StringEquals']['ses:FromAddress'], '__RESENAS_FROM_EMAIL__')
        self.assertEqual(ses['Condition']['ForAllValues:StringEquals']['ses:Recipients'], '__RESENAS_ADMIN_EMAIL__')

    def test_politica_publica_incluye_ses_y_notificaciones_sin_tocar_v1(self):
        pub = sentencias('infra/resenas/lambda-permisos.json')
        sids = [s['Sid'] for s in pub]
        for v1 in ('LeerEstadoReseniasYFotos', 'EscribirClaimsResenasYFirmarFotos',
                   'LiberarClaimSiFallaLaEscritura', 'ListarBucketParaDistinguirAusentes'):
            self.assertIn(v1, sids)
        listas = [s for s in pub if s['Action'] == 's3:ListBucket']
        self.assertEqual(len(listas), 1)
        self.assertEqual(listas[0]['Resource'], 'arn:aws:s3:::__RESENAS_BUCKET__')
        por_sid = {s['Sid']: s for s in pub}
        avisos = por_sid['GuardarAvisosDeNotificacion']
        # Solo escritura create-only (If-None-Match): no hace falta leer los avisos.
        self.assertEqual(avisos['Action'], 's3:PutObject')
        self.assertEqual(avisos['Resource'], 'arn:aws:s3:::__RESENAS_BUCKET__/notificaciones/*')
        ses = por_sid['NotificarNuevaResenaAlModerador']
        self.assertEqual(ses['Action'], 'ses:SendEmail')
        self.assertEqual(ses['Condition']['StringEquals']['ses:FromAddress'], '__RESENAS_FROM_EMAIL__')
        self.assertEqual(ses['Condition']['ForAllValues:StringEquals']['ses:Recipients'], '__RESENAS_ADMIN_EMAIL__')
        # La Lambda pública no escribe en el bucket del sitio ni tiene s3:* en ningún lado.
        self.assertNotIn('__SITE_BUCKET__', json.dumps(pub))
        for s in pub:
            self.assertNotIn('s3:*', s['Action'] if isinstance(s['Action'], list) else [s['Action']])

    def test_lifecycle_admin_de_dos_dias_sin_tocar_las_reglas_v1(self):
        reglas = json.loads(leer('infra/resenas/bucket-lifecycle.json'))['Rules']
        por_id = {r['ID']: r for r in reglas}
        self.assertEqual(por_id['pendientes-vencen-a-los-2-dias']['Filter']['Prefix'], 'pendientes/')
        self.assertEqual(por_id['pendientes-vencen-a-los-2-dias']['Expiration']['Days'], 2)
        self.assertEqual(por_id['versiones-anteriores-30-dias']['NoncurrentVersionExpiration']['NoncurrentDays'], 30)
        admin = por_id['admin-vence-a-los-2-dias']
        self.assertEqual(admin['Status'], 'Enabled')
        self.assertEqual(admin['Filter']['Prefix'], 'admin/')
        self.assertEqual(admin['Expiration']['Days'], 2)
        self.assertFalse(any(r.get('Filter', {}).get('Prefix', '').startswith('notificaciones') for r in reglas))

    def test_plan_behavior_admin_api_sin_funcion_ni_cache(self):
        plan = json.loads(leer('infra/resenas/admin-cloudfront-behavior.json'))
        self.assertTrue(plan['_estado'].startswith('PLAN'))
        b = plan['behavior']
        self.assertEqual(b['PathPattern'], '/admin/api/*')
        self.assertEqual(b['FunctionAssociations']['Quantity'], 0)
        self.assertEqual(b['CachePolicyId'], '4135ea2d-6df8-44a3-9df3-4b5a84be39ad')
        self.assertEqual(b['OriginRequestPolicyId'], 'b689b0a8-53d0-40ab-baf2-68738e2966ac')
        self.assertEqual(b['AllowedMethods']['Quantity'], len(b['AllowedMethods']['Items']))
        self.assertIn('POST', b['AllowedMethods']['Items'])
        self.assertEqual(b['ViewerProtocolPolicy'], 'https-only')


class RedireccionCloudFront(unittest.TestCase):
    def test_reglas_admin_con_node_vm(self):
        self.assertIsNotNone(shutil.which('node'), 'node es necesario para ejecutar infra/redirect-www.js')
        ruta = os.path.join(RAIZ, 'infra', 'redirect-www.js')
        esperados = [
            ({'uri': '/admin/resenas'}, '/admin-web/resenas.html', None),
            ({'uri': '/admin/resenas/3f2a9c1e0b7d4a6f8e5c2b1a9d8e7f6a'}, '/admin-web/resenas.html', None),
            ({'uri': '/admin/entrar'}, '/admin-web/entrar.html', None),
            ({'uri': '/admin/api/resenas'}, '/admin/api/resenas', None),
            ({'uri': '/admin/api/resenas/3f2a9c1e0b7d4a6f8e5c2b1a9d8e7f6a/foto'},
             '/admin/api/resenas/3f2a9c1e0b7d4a6f8e5c2b1a9d8e7f6a/foto', None),
            ({'uri': '/admin/api/sesion/confirmar'}, '/admin/api/sesion/confirmar', None),
            ({'uri': '/opinar/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}, '/opinar.html', None),
            ({'uri': '/torneos'}, '/torneos.html', None),
            ({'uri': '/config.js'}, '/config.js', None),
        ]
        casos = [caso for caso, _, _ in esperados]
        casos.append({'uri': '/admin/resenas', 'host': 'www.ork3d.com'})
        r = subprocess.run(['node', '-e', JS_PROGRAMA, ruta, json.dumps(casos)],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        salida = json.loads(r.stdout)
        for (caso, uri_esperada, _), obtenido in zip(esperados, salida):
            self.assertEqual(obtenido['uri'], uri_esperada, caso['uri'])
        redireccion = salida[-1]
        self.assertEqual(redireccion['statusCode'], 301)
        self.assertEqual(redireccion['location'], 'https://ork3d.com/admin/resenas')


class EmpaquetadoYDeploy(unittest.TestCase):
    def test_zip_de_la_lambda_publica_incluye_el_nucleo_sin_romper_v1(self):
        texto = leer('infra/resenas-setup.sh')
        self.assertIn('zip -q -j "${WORK}/resenas.zip" app.py', texto)
        self.assertIn("zip -q -r \"${WORK}/resenas.zip\" nucleo -x '*/__pycache__/*'", texto)
        # Sin el núcleo la Lambda pública no arranca: la ausencia detiene el script (no es solo un aviso).
        self.assertIn('if [[ ! -d api/resenas/nucleo ]]; then', texto)
        self.assertIn('exit 1', texto.split('if [[ ! -d api/resenas/nucleo ]]; then', 1)[1].split('fi', 1)[0])

    def test_render_de_v1_sustituye_region_y_correos(self):
        texto = leer('infra/resenas-setup.sh')
        for placeholder in ('__AWS_REGION__', '__RESENAS_FROM_EMAIL__', '__RESENAS_ADMIN_EMAIL__'):
            self.assertIn(f's|{placeholder}|', texto)

    def test_deploy_yml_publica_cambios_de_admin_web(self):
        self.assertIn("- 'admin-web/**'", leer('.github/workflows/deploy.yml'))

    def test_sin_correos_cuentas_ni_claves_en_los_archivos_nuevos(self):
        for rel in ('infra/admin-setup.sh', 'infra/resenas/admin-lambda-permisos.json',
                    'infra/resenas/admin-cloudfront-behavior.json', 'infra/redirect-www.js',
                    'infra/resenas/lambda-permisos.json'):
            texto = leer(rel)
            self.assertIsNone(EMAIL_RE.search(texto), f'{rel}: correo literal')
            self.assertIsNone(CUENTA_RE.search(texto), f'{rel}: número de cuenta')
            self.assertIsNone(CLAVE_RE.search(texto), f'{rel}: clave de acceso')

    def test_admin_setup_es_ejecutable_y_falla_ante_errores(self):
        self.assertTrue(os.access(os.path.join(RAIZ, SCRIPT), os.X_OK))
        self.assertIn('set -euo pipefail', leer(SCRIPT))
        self.assertIn('DRY_RUN="${DRY_RUN:-1}"', leer(SCRIPT))


if __name__ == '__main__':
    unittest.main()
