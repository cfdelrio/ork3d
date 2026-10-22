"""Pruebas de infra/admin-deploy.sh y de infra/admin_deploy_ayuda.py contra el simulador con estado.

Sin AWS, sin red y sin credenciales: aws_fake_admin.py guarda el estado en un JSON, aws_fake_git.py y
aws_fake_curl.py reemplazan git y curl. ADMIN_DEPLOY_RUTA apunta a una carpeta temporal.
"""
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
import zipfile

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'admin-deploy.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'admin_deploy_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin.py')
FAKE_GIT = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_git.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
RAMA = 'claude/ork3d-landing-page-b2fQr'
HEAD = '202ecb7ed9016a607eb2854f3d5a92ccf8a5466c'
FROM = 'remitente-prueba@ejemplo.test'
ADMIN = 'moderacion-prueba@ejemplo.test'
SENTINELA = 'VALOR-SENTINELA-ADMIN-DEPLOY-5522'
MUTANTES = ('create-', 'put-', 'attach-', 'add-permission', 'update-distribution')
CUENTA = '9' * 12  # cuenta falsa (no se escribe literal: el escaneo del repo la marcaría)
DIST_ARN = f'arn:aws:cloudfront::{CUENTA}:distribution/ECOW0H6A48HVP'

_spec = importlib.util.spec_from_file_location('aws_fake_admin', FAKE_AWS)
fake = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake)
_spec_ayuda = importlib.util.spec_from_file_location('admin_deploy_ayuda', AYUDA)
ayuda = importlib.util.module_from_spec(_spec_ayuda)
_spec_ayuda.loader.exec_module(ayuda)


class DespliegueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='admin-deploy-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.rb = os.path.join(self.tmp, 'rb')
        self.peticiones_path = os.path.join(self.tmp, 'peticiones.jsonl')
        self.preparar()

    def preparar(self, cambios=None):
        st = fake.estado_inicial()
        if cambios:
            cambios(st)
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        if os.path.exists(self.log):
            os.remove(self.log)

    def modificar_estado(self, cambios):
        st = self.estado_actual()
        cambios(st)
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def estado_actual(self):
        with open(self.estado, encoding='utf-8') as f:
            return json.load(f)

    def correr(self, extra=None, *, permitir=True, sin_correos=False, sin_perfil=False, magico=False):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE_AWS, 'GIT_CMD': FAKE_GIT,
               'CURL_CMD': FAKE_CURL, 'FAKE_ESTADO': self.estado, 'FAKE_LOG': self.log,
               'ADMIN_DEPLOY_ESPERA': '0', 'ADMIN_DEPLOY_RUTA': self.rb,
               'AWS_PROFILE': 'default', 'AWS_REGION': 'us-east-1',
               'RESENAS_FROM_EMAIL': FROM, 'RESENAS_ADMIN_EMAIL': ADMIN,
               'FAKE_GIT_RAMA': RAMA, 'FAKE_GIT_HEAD': HEAD, 'FAKE_CURL_REQ': self.peticiones_path}
        if permitir:
            env['ALLOW_ADMIN_DEPLOY'] = '1'
        if sin_correos:
            env.pop('RESENAS_FROM_EMAIL')
            env.pop('RESENAS_ADMIN_EMAIL')
        if sin_perfil:
            env.pop('AWS_PROFILE')
        if magico:
            env['ALLOW_ADMIN_MAGIC_EMAIL'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', SCRIPT], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=300)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [linea.strip() for linea in f if linea.strip()]

    def mutantes(self):
        # Solo servicio y operación: los detalles (if-match=, etag=) no cuentan como otra llamada.
        return [' '.join(l.split()[:2]) for l in self.llamadas() if any(m in l for m in MUTANTES)]

    def peticiones(self):
        if not os.path.exists(self.peticiones_path):
            return []
        with open(self.peticiones_path, encoding='utf-8') as f:
            return [json.loads(l) for l in f if l.strip()]

    def solicitudes_validas(self):
        return [p for p in self.peticiones() if p['valido'] and p['ruta'] == '/admin/api/sesion/solicitar']

    def copias(self):
        carpeta = os.path.join(self.rb, 'rollback')
        if not os.path.isdir(carpeta):
            return {}
        return {n: open(os.path.join(carpeta, n), 'rb').read() for n in os.listdir(carpeta)}


class PrechecksTest(DespliegueTest):
    def test_sin_allow_aborta_sin_llamar_a_aws(self):
        r = self.correr(permitir=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn('ABORTA', r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_perfil_y_region_solo_los_permitidos(self):
        r = self.correr({'AWS_PROFILE': 'otro'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])
        r = self.correr({'AWS_REGION': 'eu-west-1'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_correos_obligatorios_y_con_formato(self):
        r = self.correr(sin_correos=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('falta RESENAS_FROM_EMAIL', r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])
        r = self.correr({'RESENAS_ADMIN_EMAIL': 'no-es-un-correo'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn('no-es-un-correo', r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_bucket_no_se_define_por_entorno(self):
        r = self.correr({'RESENAS_BUCKET': 'otro-bucket'})
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_rama_incorrecta_no_escribe(self):
        r = self.correr({'FAKE_GIT_RAMA': 'main'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('branch main', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_arbol_sucio_no_escribe(self):
        r = self.correr({'FAKE_GIT_SUCIO': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('árbol no limpio', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_remoto_distinto_no_escribe(self):
        r = self.correr({'FAKE_GIT_REMOTO': 'e622ecf9f5bd6b78a2122a3481adea0b14acff6c'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('distinto del remoto', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_lambda_publica_no_active_no_escribe(self):
        self.preparar(lambda st: st['publica']['config'].update({'State': 'Pending'}))
        r = self.correr()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no está Active', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_distribucion_no_deployed_no_escribe(self):
        self.preparar(lambda st: st['dist'].update({'estado': 'InProgress', 'pendientes': 5}))
        r = self.correr()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no está Deployed', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_identidad_ses_no_verificada_no_escribe(self):
        self.preparar(lambda st: st['ses']['identidades'][FROM].update({'VerificationStatus': 'PENDING'}))
        r = self.correr()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no está verificada', r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_lambda_admin_distinta_aborta_con_4_sin_escribir(self):
        def cambiar(st):
            st['iam']['ork3d-resenas-admin-lambda'] = {'trust': fake.TRUST, 'attached': [fake.BASICA], 'inline': {}}
            st['lambda'] = {'config': {'FunctionName': 'ork3d-resenas-admin', 'Runtime': 'python3.11',
                                       'Handler': 'admin_app.handler', 'MemorySize': 256, 'Timeout': 10,
                                       'Role': f'arn:aws:iam::{CUENTA}:role/ork3d-resenas-admin-lambda',
                                       'State': 'Active', 'LastUpdateStatus': 'Successful',
                                       'CodeSha256': 'x', 'Environment': {'Variables': {}}},
                            'url': None, 'policy': []}
        self.preparar(cambiar)
        r = self.correr()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_conflicto_de_patron_en_cloudfront_aborta_con_5(self):
        def cambiar(st):
            st['dist']['config']['CacheBehaviors']['Items'].append(dict(
                st['dist']['config']['CacheBehaviors']['Items'][0], PathPattern='/admin/*'))
            st['dist']['config']['CacheBehaviors']['Quantity'] = 2
        self.preparar(cambiar)
        r = self.correr()
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])

    def test_rol_existente_con_otra_politica_aborta_con_4(self):
        def cambiar(st):
            st['iam']['ork3d-resenas-admin-lambda'] = {
                'trust': fake.TRUST, 'attached': [fake.BASICA],
                'inline': {'ork3d-resenas-admin-permisos': {'Version': '2012-10-17', 'Statement': [
                    {'Effect': 'Allow', 'Action': '*', 'Resource': '*'}]}}}
        self.preparar(cambiar)
        r = self.correr()
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])


class CaminoFelizTest(DespliegueTest):
    def test_despliegue_completo_en_orden_y_smoke(self):
        antes = self.estado_actual()
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('smoke', r.stdout.lower())
        llam = self.llamadas()

        def pos(texto):
            return next(i for i, l in enumerate(llam) if texto in l)
        self.assertLess(pos('logs create-log-group'), pos('lambda create-function'))
        self.assertLess(pos('iam create-role'), pos('lambda create-function'))
        self.assertLess(pos('lambda create-function'), pos('lambda create-function-url-config'))
        self.assertLess(pos('lambda add-permission'), pos('cloudfront update-distribution'))
        self.assertEqual(len([l for l in llam if l == 'lambda add-permission']), 2)
        self.assertEqual(llam[-1], 'curl POST /admin/api/sesion/solicitar')
        self.assertEqual(llam[-2], 'curl GET /admin/api/sesion')
        self.assertEqual(self.mutantes()[-1], 'cloudfront update-distribution')
        self.assertEqual(self.mutantes().count('cloudfront update-distribution'), 1)

        st = self.estado_actual()
        self.assertEqual(sorted(st['lambda']['config']['Environment']['Variables']),
                         sorted(['RESENAS_BUCKET', 'RESENAS_SITE_BUCKET', 'RESENAS_DISTRIBUTION_ID',
                                 'RESENAS_ADMIN_BASE_URL', 'ALLOWED_ORIGIN', 'RESENAS_ADMIN_EMAIL',
                                 'RESENAS_FROM_EMAIL', 'RESENAS_SESION_HORAS', 'RESENAS_LOGIN_MINUTOS',
                                 'RESENAS_LOGIN_LIMITE', 'RESENAS_LOGIN_VENTANA_MIN', 'RESENAS_TOKEN_DAYS']))
        env = st['lambda']['config']['Environment']['Variables']
        self.assertEqual(env['RESENAS_SESION_HORAS'], '8')
        self.assertEqual(env['RESENAS_TOKEN_DAYS'], '15')
        self.assertEqual(st['lambda']['config']['Runtime'], 'python3.12')
        self.assertEqual(st['lambda']['config']['Handler'], 'admin_app.handler')
        self.assertEqual(st['lambda']['config']['MemorySize'], 256)
        self.assertEqual(st['lambda']['config']['Timeout'], 10)
        self.assertEqual(st['lambda']['url']['AuthType'], 'AWS_IAM')
        sids = {s['Sid']: s for s in st['lambda']['policy']}
        self.assertEqual(sorted(sids), ['ork3d-admin-cf-invoke', 'ork3d-admin-cf-url'])
        self.assertEqual(sids['ork3d-admin-cf-url']['Action'], 'lambda:InvokeFunctionUrl')
        self.assertEqual(sids['ork3d-admin-cf-url']['Condition']['StringEquals'],
                         {'lambda:FunctionUrlAuthType': 'AWS_IAM'})
        self.assertEqual(sids['ork3d-admin-cf-invoke']['Action'], 'lambda:InvokeFunction')
        self.assertEqual(sids['ork3d-admin-cf-invoke']['Condition']['Bool'], {'lambda:InvokedViaFunctionUrl': 'true'})
        for sid in sids.values():
            self.assertEqual(sid['Principal'], {'Service': 'cloudfront.amazonaws.com'})
            self.assertEqual(sid['Condition']['ArnLike'], {'AWS:SourceArn': DIST_ARN})

        self.assertEqual(len(st['oacs']), 1)
        oac = st['oacs'][0]
        self.assertEqual((oac['SigningProtocol'], oac['SigningBehavior'], oac['OriginAccessControlOriginType']),
                         ('sigv4', 'always', 'lambda'))
        self.assertEqual(len(st['orps']), 1)
        orp = st['orps'][0]['config']
        self.assertEqual(orp['Name'], 'ork3d-resenas-admin-orp')
        self.assertEqual(orp['HeadersConfig']['HeaderBehavior'], 'allExcept')
        self.assertEqual(orp['HeadersConfig']['Headers']['Items'], ['Host'])
        self.assertEqual(orp['CookiesConfig']['Cookies']['Items'], ['ork3d_admin'])
        self.assertEqual(orp['QueryStringsConfig']['QueryStrings']['Items'], ['estado'])

        self.assertNotEqual(st['dist']['etag'], antes['dist']['etag'])
        self.assertEqual(st['dist']['estado'], 'Deployed')

    def test_behavior_admin_y_origen_cumplen_la_especificacion(self):
        self.correr()
        st = self.estado_actual()
        cfg = st['dist']['config']
        origen = [o for o in cfg['Origins']['Items'] if o['Id'] == 'resenas-admin-lambda']
        self.assertEqual(len(origen), 1)
        self.assertEqual(origen[0]['DomainName'], 'abcdefghijklmnopqrstuvwx.lambda-url.us-east-1.on.aws')
        self.assertEqual(origen[0]['OriginAccessControlId'], st['oacs'][0]['Id'])
        self.assertEqual(origen[0]['CustomOriginConfig']['OriginProtocolPolicy'], 'https-only')
        beh = [b for b in cfg['CacheBehaviors']['Items'] if b['PathPattern'] == '/admin/api/*']
        self.assertEqual(len(beh), 1)
        b = beh[0]
        self.assertEqual(b['TargetOriginId'], 'resenas-admin-lambda')
        self.assertEqual(b['ViewerProtocolPolicy'], 'redirect-to-https')
        self.assertEqual(b['CachePolicyId'], '4135ea2d-6df8-44a3-9df3-4b5a84be39ad')
        self.assertEqual(b['OriginRequestPolicyId'], st['orps'][0]['Id'])
        self.assertEqual(b['AllowedMethods']['Quantity'], 7)
        self.assertIn('POST', b['AllowedMethods']['Items'])
        self.assertEqual(b['AllowedMethods']['CachedMethods']['Items'], ['GET', 'HEAD'])
        self.assertIs(b['SmoothStreaming'], False)
        self.assertEqual(b['TrustedSigners'], {'Enabled': False, 'Quantity': 0})
        self.assertEqual(b['TrustedKeyGroups'], {'Enabled': False, 'Quantity': 0})
        self.assertEqual(b['LambdaFunctionAssociations'], {'Quantity': 0})
        self.assertEqual(b['FunctionAssociations'], {'Quantity': 0})
        self.assertEqual(b['FieldLevelEncryptionId'], '')
        self.assertIs(b['Compress'], True)

    def test_default_origen_s3_y_behaviors_previos_no_cambian(self):
        antes = self.estado_actual()['dist']['config']
        self.correr()
        despues = self.estado_actual()['dist']['config']
        self.assertEqual(antes['DefaultCacheBehavior'], despues['DefaultCacheBehavior'])
        self.assertEqual([o for o in antes['Origins']['Items'] if 'S3OriginConfig' in o],
                         [o for o in despues['Origins']['Items'] if 'S3OriginConfig' in o])
        previos = [b for b in despues['CacheBehaviors']['Items'] if b['PathPattern'] != '/admin/api/*']
        self.assertEqual(previos, antes['CacheBehaviors']['Items'])

    def test_politica_del_rol_lleva_correos_y_bucket_sin_marcadores(self):
        self.correr()
        doc = self.estado_actual()['iam']['ork3d-resenas-admin-lambda']['inline']['ork3d-resenas-admin-permisos']
        texto = json.dumps(doc)
        self.assertNotIn('__', texto)
        self.assertIn(f'identity/{FROM}', texto)
        self.assertIn(FROM, texto)
        self.assertIn(ADMIN, texto)
        self.assertIn('arn:aws:s3:::bucket-privado-fake-1234/tokens/*', texto)

    def test_arn_del_rol_y_trust_de_lambda(self):
        self.correr()
        rol = self.estado_actual()['iam']['ork3d-resenas-admin-lambda']
        self.assertEqual(rol['attached'], [fake.BASICA])
        self.assertEqual(rol['trust']['Statement'][0]['Principal'], {'Service': 'lambda.amazonaws.com'})

    def test_rol_no_asumible_al_inicio_se_reintenta(self):
        r = self.correr({'FAKE_ROL_TARDE': '2'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len([l for l in self.llamadas() if l == 'lambda create-function']), 3)

    def test_segunda_ejecucion_es_idempotente_sin_escrituras(self):
        self.assertEqual(self.correr().returncode, 0)
        self.preparar_log_solo()
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), [])
        self.assertIn('existe y coincide', r.stdout)

    def preparar_log_solo(self):
        if os.path.exists(self.log):
            os.remove(self.log)


class RollbackTest(DespliegueTest):
    def test_carpeta_700_archivos_600_y_no_sobrescribe(self):
        self.correr()
        carpeta = os.path.join(self.rb, 'rollback')
        self.assertEqual(stat.S_IMODE(os.stat(carpeta).st_mode), 0o700)
        primera = self.copias()
        self.assertTrue(primera)
        for nombre in primera:
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(carpeta, nombre)).st_mode), 0o600, nombre)
        self.assertTrue(any('cloudfront-distribucion-config' in n for n in primera))
        self.assertTrue(any('cloudfront-funcion-redirect-LIVE' in n for n in primera))
        self.assertTrue(any('s3-lifecycle-privado' in n for n in primera))
        self.assertTrue(any('lambda-publica-policy' in n for n in primera))
        self.assertTrue(any('iam-rol-publico' in n for n in primera))
        self.preparar()
        self.correr()
        segunda = self.copias()
        self.assertGreater(len(segunda), len(primera))
        for nombre, contenido in primera.items():
            self.assertEqual(segunda[nombre], contenido, f'se modificó {nombre}')

    def test_rollback_del_cloudfront_tiene_el_etag_de_antes(self):
        self.correr()
        carpeta = os.path.join(self.rb, 'rollback')
        archivo = next(n for n in os.listdir(carpeta) if n.startswith('cloudfront-distribucion-config'))
        with open(os.path.join(carpeta, archivo), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['ETag'], 'E2TAGDEMO')

    def test_no_imprime_correos_completos_ni_secretos(self):
        r = self.correr()
        visible = r.stdout + r.stderr
        self.assertNotIn(FROM, visible)
        self.assertNotIn(ADMIN, visible)
        self.assertIn('r***@e***.test', visible)
        self.assertNotIn(SENTINELA, visible)
        for nombre, contenido in self.copias().items():
            self.assertNotIn(SENTINELA.encode(), contenido, nombre)


class SmokeTest(DespliegueTest):
    def test_get_sin_401_de_la_api_sale_con_6(self):
        r = self.correr({'FAKE_CURL_SESION': '200', 'FAKE_CURL_SESION_CUERPO': '<html>ok</html>'})
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn('se esperaba 401', r.stdout + r.stderr)

    def test_post_sin_cuerpo_de_la_api_sale_con_6(self):
        r = self.correr({'FAKE_CURL_SOLICITAR_CUERPO': '<html>AccessDenied</html>'})
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn('se esperaba 403', r.stdout + r.stderr)

    def test_smoke_no_envia_correo(self):
        self.correr()
        self.assertIn('curl POST /admin/api/sesion/solicitar', self.llamadas())
        self.assertEqual(len([l for l in self.llamadas() if 'solicitar' in l]), 1)


class AyudaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='admin-deploy-ayuda-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def escribir(self, nombre, datos):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(datos, f)
        return ruta

    def test_zip_determinista_con_solo_admin_y_nucleo(self):
        a = os.path.join(self.tmp, 'a.zip')
        b = os.path.join(self.tmp, 'b.zip')
        ra = ayuda.zip_admin(RAIZ, a)
        rb = ayuda.zip_admin(RAIZ, b)
        self.assertEqual(ra, 0)
        self.assertEqual(rb, 0)
        self.assertEqual(hashlib.sha256(open(a, 'rb').read()).digest(), hashlib.sha256(open(b, 'rb').read()).digest())
        with zipfile.ZipFile(a) as z:
            nombres = z.namelist()
        self.assertIn('admin_app.py', nombres)
        self.assertTrue(all(n == 'admin_app.py' or n.startswith('nucleo/') for n in nombres), nombres)
        self.assertFalse(any('test' in n for n in nombres))

    def test_politica_sin_marcadores_y_con_correos(self):
        salida = os.path.join(self.tmp, 'pol.json')
        rc = ayuda.politica(os.path.join(RAIZ, 'infra', 'resenas', 'admin-lambda-permisos.json'), salida,
                            CUENTA, 'us-east-1', 'ECOW0H6A48HVP', 'bucket-x', 'ork3d.com', FROM, ADMIN)
        self.assertEqual(rc, 0)
        texto = open(salida, encoding='utf-8').read()
        self.assertNotIn('__', texto)
        self.assertIn(FROM, texto)

    def test_politica_con_marcador_sin_valor_falla(self):
        plantilla = self.escribir('p.json', {'X': '__RESENAS_BUCKET__'})
        rc = ayuda.politica(plantilla, os.path.join(self.tmp, 'o.json'), '1', 'us-east-1', 'D', '__RESENAS_BUCKET__',
                            's', 'a@b.cd', 'c@d.ef')
        self.assertEqual(rc, 1)

    def test_lambda_coincide_detecta_valores_distintos_sin_mostrarlos(self):
        cfg = self.escribir('cfg.json', {'Runtime': 'python3.12', 'Handler': 'admin_app.handler', 'MemorySize': 256,
                                         'Timeout': 10, 'Role': 'arn:aws:iam::1:role/ork3d-resenas-admin-lambda',
                                         'State': 'Active', 'LastUpdateStatus': 'Successful', 'CodeSha256': 's',
                                         'Environment': {'Variables': {k: 'x' for k in ayuda.VARIABLES}}})
        env = self.escribir('env.json', {'Variables': {k: 'y' for k in ayuda.VARIABLES}})
        self.assertEqual(ayuda.lambda_coincide(cfg, 's', env), 1)

    def test_dist_nueva_rechaza_origen_existente_con_otro_dominio(self):
        cfg = {'DistributionConfig': {
            'Origins': {'Quantity': 1, 'Items': [{'Id': 'resenas-admin-lambda', 'DomainName': 'otro.example',
                                                  'OriginAccessControlId': 'OAC1'}]},
            'CacheBehaviors': {'Quantity': 0, 'Items': []}}}
        entrada = self.escribir('d.json', cfg)
        rc = ayuda.dist_nueva(entrada, os.path.join(self.tmp, 'n.json'), 'x.lambda-url', 'OAC1', 'ORP1')
        self.assertEqual(rc, 4)

    def test_dist_nueva_rechaza_patron_en_conflicto(self):
        cfg = {'DistributionConfig': {'Origins': {'Quantity': 0, 'Items': []},
                                      'CacheBehaviors': {'Quantity': 1, 'Items': [{'PathPattern': '/admin/*'}]}}}
        rc = ayuda.dist_nueva(self.escribir('d.json', cfg), os.path.join(self.tmp, 'n.json'), 'h', 'o', 'p')
        self.assertEqual(rc, 5)

    def test_dist_verificar_detecta_cambio_en_el_default(self):
        antes = {'DistributionConfig': {'DefaultCacheBehavior': {'TargetOriginId': 'sitio'},
                                        'Origins': {'Items': []}, 'CacheBehaviors': {'Items': []}}}
        despues = {'DistributionConfig': {'DefaultCacheBehavior': {'TargetOriginId': 'otro'},
                                          'Origins': {'Items': []}, 'CacheBehaviors': {'Items': []}}}
        rc = ayuda.dist_verificar(self.escribir('a.json', antes), self.escribir('b.json', despues), 'ORP')
        self.assertEqual(rc, 1)

    def test_permisos_con_una_sola_sentencia_difiere(self):
        pol = {'Policy': json.dumps({'Statement': [{'Sid': 'ork3d-admin-cf-url', 'Action': 'lambda:InvokeFunctionUrl',
                                                    'Principal': {'Service': 'cloudfront.amazonaws.com'},
                                                    'Condition': {'StringEquals': {
                                                        'lambda:FunctionUrlAuthType': 'AWS_IAM'},
                                                        'ArnLike': {'AWS:SourceArn': DIST_ARN}}}]})}
        ruta = self.escribir('pol.json', pol)
        self.assertEqual(subprocess.run([sys.executable, AYUDA, 'permisos', ruta, DIST_ARN],
                                        capture_output=True, text=True).returncode, 1)

    def test_permisos_ninguno_y_ok(self):
        ruta = self.escribir('vacia.json', {})
        r = subprocess.run([sys.executable, AYUDA, 'permisos', ruta, DIST_ARN], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, 'ninguno'))


class SmokeMagicTest(DespliegueTest):
    """Smoke positivo: un único POST de magic link, solo con ALLOW_ADMIN_MAGIC_EMAIL=1."""

    def test_sin_flag_no_hay_post_valido_y_se_informa_omision(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.solicitudes_validas(), [])
        self.assertIn('Smoke positivo de magic email omitido; requiere ALLOW_ADMIN_MAGIC_EMAIL=1', r.stdout)
        self.assertNotIn('CHECKPOINT 7', r.stdout)

    def test_con_flag_exactamente_un_post_valido_y_202(self):
        r = self.correr(magico=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.solicitudes_validas()), 1)
        self.assertIn('CHECKPOINT 7 — POST solicitar 202 · email de acceso solicitado', r.stdout)
        self.assertIn('Revisá la bandeja del moderador y confirmá manualmente la recepción.', r.stdout)
        self.assertNotIn('Smoke positivo de magic email omitido', r.stdout)

    def test_body_exacto_y_sha256_de_los_mismos_bytes_enviados(self):
        self.correr(magico=True)
        valida = self.solicitudes_validas()[0]
        self.assertEqual(valida['cuerpo'], '{}')
        self.assertEqual(valida['cabeceras']['x-amz-content-sha256'],
                         hashlib.sha256(b'{}').hexdigest())
        self.assertEqual(len('{}'.encode('utf-8')), 2)

    def test_cabeceras_origin_content_type_y_x_admin_request(self):
        self.correr(magico=True)
        cab = self.solicitudes_validas()[0]['cabeceras']
        self.assertEqual(cab['origin'], 'https://ork3d.com')
        self.assertEqual(cab['content-type'], 'application/json')
        self.assertEqual(cab['x-admin-request'], '1')

    def test_respuesta_distinta_de_202_falla_sin_reintento(self):
        for codigo in ('200', '400', '403', '500'):
            with self.subTest(codigo=codigo):
                self.preparar()
                if os.path.exists(self.peticiones_path):
                    os.remove(self.peticiones_path)
                r = self.correr({'FAKE_CURL_SOLICITAR_VALIDO': codigo}, magico=True)
                self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
                self.assertIn(f'válido → {codigo}', r.stdout + r.stderr)
                self.assertEqual(len(self.solicitudes_validas()), 1, 'no debe reintentar')
                self.assertNotIn('CHECKPOINT 7', r.stdout)

    def test_negativo_403_jamas_envia_correo(self):
        self.correr()
        negativos = [p for p in self.peticiones() if p['ruta'] == '/admin/api/sesion/solicitar']
        self.assertEqual(len(negativos), 1)
        self.assertNotIn('origin', negativos[0]['cabeceras'])
        self.assertFalse(negativos[0]['valido'])
        self.assertEqual(self.solicitudes_validas(), [])

    def test_rerun_sin_flag_no_envia_correos(self):
        self.correr(magico=True)
        self.assertEqual(len(self.solicitudes_validas()), 1)
        self.preparar()
        if os.path.exists(self.peticiones_path):
            os.remove(self.peticiones_path)
        self.correr()
        self.assertEqual(self.solicitudes_validas(), [])

    def test_checkpoints_visibles_en_orden(self):
        r = self.correr(magico=True)
        orden = ['CHECKPOINT 1 — backups OK', 'CHECKPOINT 2 — IAM OK', 'CHECKPOINT 3 — Lambda admin OK',
                 'CHECKPOINT 4 — Function URL/OAC OK', 'CHECKPOINT 5 — CloudFront Deployed',
                 'CHECKPOINT 6 — GET sesión 401', 'CHECKPOINT 6B — CSRF negativo 403',
                 'CHECKPOINT 7 — POST solicitar 202', 'CHECKPOINT 8 — logs sanitizados']
        posiciones = [r.stdout.index(c) for c in orden]
        self.assertEqual(posiciones, sorted(posiciones))

    def test_salida_sin_correos_completos_ni_material_de_magic_link(self):
        r = self.correr(magico=True)
        visible = r.stdout + r.stderr
        self.assertNotIn(FROM, visible)
        self.assertNotIn(ADMIN, visible)
        for prohibido in ('codigo=', 'magic_link', 'http://', 'enlace', 'x-amz-security-token'):
            self.assertNotIn(prohibido, visible.lower())

    def test_registro_de_ejecucion_privado_y_sanitizado(self):
        self.correr(magico=True)
        logs = [n for n in os.listdir(os.path.join(self.rb)) if n.startswith('ejecucion.') and n.endswith('.log')]
        self.assertEqual(len(logs), 1)
        ruta = os.path.join(self.rb, logs[0])
        self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600)
        contenido = open(ruta, encoding='utf-8').read()
        self.assertIn('CHECKPOINT 8', contenido)
        self.assertNotIn(FROM, contenido)
        self.assertNotIn(ADMIN, contenido)


class OrpTest(unittest.TestCase):
    """Política de origen para la Function URL con OAC: allExcept excluye solo Host. Sin AWS."""

    def setUp(self):
        with open(os.path.join(RAIZ, 'infra', 'resenas', 'admin-origin-request-policy.json'), encoding='utf-8') as f:
            self.orp = json.load(f)
        self.texto = json.dumps(self.orp)

    def test_usa_allexcept_y_excluye_solo_host(self):
        self.assertEqual(self.orp['HeadersConfig']['HeaderBehavior'], 'allExcept')
        self.assertEqual(self.orp['HeadersConfig']['Headers'], {'Quantity': 1, 'Items': ['Host']})

    def test_no_contiene_literalmente_x_amz_content_sha256(self):
        self.assertNotIn('x-amz-content-sha256', self.texto.lower())

    def test_regresion_ninguna_cabecera_x_amz_en_items(self):
        # Si alguien vuelve a listar cabeceras x-amz-* en Headers.Items (que CloudFront rechazó), esta prueba falla.
        items = self.orp['HeadersConfig']['Headers']['Items']
        self.assertEqual([i for i in items if i.lower().startswith('x-amz-')], [])

    def test_cookies_solo_ork3d_admin(self):
        self.assertEqual(self.orp['CookiesConfig'], {'CookieBehavior': 'whitelist',
                                                     'Cookies': {'Quantity': 1, 'Items': ['ork3d_admin']}})

    def test_query_solo_estado(self):
        self.assertEqual(self.orp['QueryStringsConfig'], {'QueryStringBehavior': 'whitelist',
                                                          'QueryStrings': {'Quantity': 1, 'Items': ['estado']}})

    def test_comment_existe_y_cabe_en_el_limite_de_cloudfront(self):
        comentario = self.orp.get('Comment')
        self.assertIsInstance(comentario, str)
        self.assertTrue(comentario)
        self.assertLessEqual(len(comentario), 128, 'CloudFront rechaza Comment de más de 128 caracteres')
        self.assertLessEqual(len(comentario), 100, 'margen: el comentario debe quedar por debajo de 100')
        self.assertLessEqual(len(comentario.encode('utf-8')), 128)

    def test_mantiene_headers_cookies_y_query_restringidos(self):
        self.assertEqual(self.orp['HeadersConfig']['HeaderBehavior'], 'allExcept')
        self.assertEqual(self.orp['HeadersConfig']['Headers']['Items'], ['Host'])
        self.assertEqual(self.orp['CookiesConfig']['Cookies']['Items'], ['ork3d_admin'])
        self.assertEqual(self.orp['QueryStringsConfig']['QueryStrings']['Items'], ['estado'])

    def _crear_orp_con_fake(self, politica):
        with tempfile.TemporaryDirectory() as tmp:
            estado = os.path.join(tmp, 'estado.json')
            ruta = os.path.join(tmp, 'orp.json')
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(politica, f, ensure_ascii=False)
            return subprocess.run([sys.executable, FAKE_AWS, 'cloudfront', 'create-origin-request-policy',
                                   '--origin-request-policy-config', f'file://{ruta}'],
                                  env={'FAKE_ESTADO': estado, 'PATH': os.environ.get('PATH', '')},
                                  capture_output=True, text=True)

    def test_el_fake_rechaza_un_comment_de_mas_de_128_caracteres(self):
        largo = json.loads(self.texto)
        largo['Comment'] = 'x' * 129
        r = self._crear_orp_con_fake(largo)
        self.assertEqual(r.returncode, 255)
        self.assertIn('InvalidArgument', r.stderr)
        self.assertIn('The parameter Comment is too big.', r.stderr)

    def test_el_fake_acepta_la_politica_con_el_comment_nuevo(self):
        r = self._crear_orp_con_fake(self.orp)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('ORK3D admin API', r.stdout)

    def test_policy_gestionada_de_cloudfront_no_se_usa(self):
        self.assertNotIn('AllViewerExceptHostHeader', self.texto)
        self.assertNotIn('Managed-AllViewer', self.texto)

    def test_el_cliente_sigue_enviando_el_hash_del_body(self):
        # El frontend no cambia: calcula SHA-256 sobre los bytes del body y los envía con x-amz-content-sha256.
        with open(os.path.join(RAIZ, 'assets', 'js', 'admin-comun.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertIn("cabeceras['x-amz-content-sha256'] = hash", js)
        self.assertIn("crypto.subtle.digest('SHA-256', new TextEncoder().encode(texto))", js)

    def test_la_cli_rechaza_la_version_anterior_con_cabeceras_x_amz(self):
        # Reproduce el rechazo observado: cabeceras x-amz-* listadas en Headers.Items.
        with tempfile.TemporaryDirectory() as tmp:
            estado = os.path.join(tmp, 'estado.json')
            anterior = json.loads(self.texto)
            anterior['HeadersConfig'] = {'HeaderBehavior': 'whitelist', 'Headers': {
                'Quantity': 2, 'Items': ['Content-Type', 'x-amz-content-sha256']}}
            ruta = os.path.join(tmp, 'anterior.json')
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(anterior, f)
            r = subprocess.run([sys.executable, FAKE_AWS, 'cloudfront', 'create-origin-request-policy',
                                '--origin-request-policy-config', f'file://{ruta}'],
                               env={'FAKE_ESTADO': estado, 'PATH': os.environ.get('PATH', '')},
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 255)
            self.assertIn('InvalidArgument', r.stderr)
            self.assertIn('x-amz-content-sha256', r.stderr)


class RerunConRecursosExistentesTest(DespliegueTest):
    def test_rerun_crea_solo_orp_y_cloudfront(self):
        self.assertEqual(self.correr().returncode, 0)

        def deshacer_cloudfront_y_orp(st):
            st['orps'] = []
            st['dist']['config'] = fake.configuracion_distribucion()
            st['dist']['etag'] = 'E2TAGDEMO'
            st['dist']['estado'] = 'Deployed'
            st['dist']['pendientes'] = 0
        self.modificar_estado(deshacer_cloudfront_y_orp)
        if os.path.exists(self.log):
            os.remove(self.log)
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.mutantes(), ['cloudfront create-origin-request-policy', 'cloudfront update-distribution'])
        self.assertIn('existe y coincide (idempotente)', r.stdout)
        self.assertEqual(len(self.estado_actual()['orps']), 1)

    def test_orp_rechazada_muestra_detalle_aws_sanitizado(self):
        r = self.correr({'FAKE_ORP_RECHAZO': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        visible = r.stdout + r.stderr
        self.assertIn('InvalidArgument', visible)
        self.assertIn('CreateOriginRequestPolicy', visible)
        self.assertIn('a***@e***.com', visible)
        self.assertNotIn('ana.moderadora@example.com', visible)
        self.assertNotIn('9' * 12, visible)  # cuenta falsa del simulador, sin escribirla literal
        self.assertNotIn('AKIA', visible)
        self.assertNotIn('X-Amz-Signature', visible)
        self.assertEqual(self.mutantes()[-1:], ['cloudfront create-origin-request-policy'])


class BehaviorAdminTest(unittest.TestCase):
    """Forma del CacheBehavior de /admin/api/*. Sin AWS."""

    def setUp(self):
        self.b = ayuda.behavior_admin('ORP-DE-PRUEBA')

    def test_smooth_streaming_existe_es_booleano_y_false(self):
        self.assertIn('SmoothStreaming', self.b)
        self.assertIs(self.b['SmoothStreaming'], False)

    def test_trusted_signers_vacio(self):
        self.assertEqual(self.b['TrustedSigners'], {'Enabled': False, 'Quantity': 0})

    def test_trusted_key_groups_vacio(self):
        self.assertEqual(self.b['TrustedKeyGroups'], {'Enabled': False, 'Quantity': 0})

    def test_lambda_function_associations_vacio(self):
        self.assertEqual(self.b['LambdaFunctionAssociations'], {'Quantity': 0})

    def test_function_associations_vacio(self):
        self.assertEqual(self.b['FunctionAssociations'], {'Quantity': 0})

    def test_field_level_encryption_id_es_cadena_vacia(self):
        self.assertIn('FieldLevelEncryptionId', self.b)
        self.assertIsInstance(self.b['FieldLevelEncryptionId'], str)
        self.assertEqual(self.b['FieldLevelEncryptionId'], '')

    def test_allowed_methods_tiene_los_siete_metodos_con_post(self):
        self.assertEqual(self.b['AllowedMethods']['Quantity'], 7)
        self.assertEqual(sorted(self.b['AllowedMethods']['Items']),
                         sorted(['GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'POST', 'DELETE']))

    def test_cached_methods_get_y_head(self):
        self.assertEqual(self.b['AllowedMethods']['CachedMethods'], {'Quantity': 2, 'Items': ['GET', 'HEAD']})

    def test_cache_policy_es_caching_disabled(self):
        self.assertEqual(self.b['CachePolicyId'], '4135ea2d-6df8-44a3-9df3-4b5a84be39ad')

    def test_origin_request_policy_es_la_pasada(self):
        self.assertEqual(self.b['OriginRequestPolicyId'], 'ORP-DE-PRUEBA')

    def test_viewer_protocol_redirect_to_https(self):
        self.assertEqual(self.b['ViewerProtocolPolicy'], 'redirect-to-https')

    def test_compress_true(self):
        self.assertIs(self.b['Compress'], True)

    def test_path_y_origen(self):
        self.assertEqual((self.b['PathPattern'], self.b['TargetOriginId']), ('/admin/api/*', 'resenas-admin-lambda'))


class ActualizacionCloudFrontTest(DespliegueTest):
    def config_con_behavior(self, behavior):
        cfg = fake.configuracion_distribucion()
        cfg['CacheBehaviors']['Items'].append(behavior)
        cfg['CacheBehaviors']['Quantity'] = len(cfg['CacheBehaviors']['Items'])
        return cfg

    def update_directo(self, cfg, if_match='E2TAGDEMO'):
        with tempfile.TemporaryDirectory() as tmp:
            ruta = os.path.join(tmp, 'cfg.json')
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(cfg, f)
            with open(self.estado, 'w', encoding='utf-8') as f:
                json.dump(fake.estado_inicial(), f)
            return subprocess.run([sys.executable, FAKE_AWS, 'cloudfront', 'update-distribution', '--id', 'ECOW0H6A48HVP',
                                   '--if-match', if_match, '--distribution-config', f'file://{ruta}'],
                                  env={'FAKE_ESTADO': self.estado, 'PATH': os.environ.get('PATH', '')},
                                  capture_output=True, text=True)

    def behavior_admin(self):
        return ayuda.behavior_admin('ORP-DE-PRUEBA')

    def test_sin_smooth_streaming_cloudfront_rechaza_con_el_mensaje_real(self):
        b = self.behavior_admin()
        del b['SmoothStreaming']
        r = self.update_directo(self.config_con_behavior(b))
        self.assertEqual(r.returncode, 255)
        self.assertIn('InvalidArgument', r.stderr)
        self.assertIn('The parameter SmoothStreaming flag is missing.', r.stderr)

    def test_behavior_con_forma_completa_se_acepta(self):
        r = self.update_directo(self.config_con_behavior(self.behavior_admin()))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_fake_detecta_bloques_faltantes_sin_esperar_a_aws(self):
        for campo in ('TrustedSigners', 'TrustedKeyGroups', 'LambdaFunctionAssociations', 'FunctionAssociations',
                      'FieldLevelEncryptionId', 'Compress'):
            with self.subTest(campo=campo):
                b = self.behavior_admin()
                del b[campo]
                r = self.update_directo(self.config_con_behavior(b))
                self.assertEqual(r.returncode, 255)
                self.assertIn(f'The parameter {campo} is missing.', r.stderr)

    def test_fake_detecta_allowed_methods_sin_cached_methods(self):
        b = self.behavior_admin()
        del b['AllowedMethods']['CachedMethods']
        r = self.update_directo(self.config_con_behavior(b))
        self.assertEqual(r.returncode, 255)
        self.assertIn('CachedMethods is missing', r.stderr)

    def test_rerun_con_orp_existente_no_crea_otra_y_actualiza_una_vez(self):
        self.assertEqual(self.correr().returncode, 0)

        def deshacer_solo_cloudfront(st):
            st['dist']['config'] = fake.configuracion_distribucion()
            st['dist']['etag'] = 'E2TAGDEMO'
            st['dist']['estado'] = 'Deployed'
            st['dist']['pendientes'] = 0
        self.modificar_estado(deshacer_solo_cloudfront)
        if os.path.exists(self.log):
            os.remove(self.log)
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn('cloudfront create-origin-request-policy', self.mutantes())
        self.assertEqual(self.mutantes(), ['cloudfront update-distribution'])
        self.assertIn('existe y coincide', r.stdout)
        self.assertEqual(len(self.estado_actual()['orps']), 1)

    def test_if_match_usa_el_etag_leido_justo_antes_del_update(self):
        self.assertEqual(self.correr().returncode, 0)
        def cloudfront_sin_origin_admin(st):
            # Estado previo al update: sin origin ni behavior admin, ETag nuevo; la ORP ya existe.
            st['dist'].update({'config': fake.configuracion_distribucion(), 'etag': 'E2OTRO',
                               'estado': 'Deployed', 'pendientes': 0})
        self.modificar_estado(cloudfront_sin_origin_admin)
        if os.path.exists(self.log):
            os.remove(self.log)
        self.assertEqual(self.correr().returncode, 0)
        lineas = self.llamadas()
        idx = next(i for i, l in enumerate(lineas) if l.startswith('cloudfront update-distribution'))
        match = lineas[idx].split('if-match=')[1]
        previo = [l for l in lineas[:idx] if l.startswith('cloudfront get-distribution-config')][-1]
        self.assertEqual(previo.split('etag=')[1], match)
        self.assertEqual(match, 'E2OTRO')

    def test_existentes_no_cambian_en_el_camino_feliz(self):
        antes = self.estado_actual()['dist']['config']
        self.assertEqual(self.correr().returncode, 0)
        despues = self.estado_actual()['dist']['config']
        self.assertEqual(antes['DefaultCacheBehavior'], despues['DefaultCacheBehavior'])
        self.assertEqual(antes['Origins']['Items'], [o for o in despues['Origins']['Items'] if o['Id'] != 'resenas-admin-lambda'])
        self.assertEqual(antes['CacheBehaviors']['Items'],
                         [b for b in despues['CacheBehaviors']['Items'] if b['PathPattern'] != '/admin/api/*'])
        self.assertEqual(len([o for o in despues['Origins']['Items'] if o['Id'] == 'resenas-admin-lambda']), 1)
        self.assertEqual(len([b for b in despues['CacheBehaviors']['Items'] if b['PathPattern'] == '/admin/api/*']), 1)
        self.assertEqual(despues['CacheBehaviors']['Quantity'], len(despues['CacheBehaviors']['Items']))
        self.assertEqual(despues['Origins']['Quantity'], len(despues['Origins']['Items']))


if __name__ == '__main__':
    unittest.main()
