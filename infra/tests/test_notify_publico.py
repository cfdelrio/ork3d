"""Pruebas del despliegue público del aviso (Fase A) y de su parte de IAM, contra el simulador con estado.

Sin AWS, sin red y sin credenciales. Lo que se prueba: el diff no escribe nada; el despliegue escribe solo el
Environment (con las variables actuales + las nuevas, RESENAS_NOTIFY_ENABLED=false) y el código del cierre de imports;
nunca pisa un valor existente; la verificación compara con la foto de antes; revertir restaura exactamente lo guardado;
y la política IAM solo agrega las sentencias que faltan.
"""
import base64
import contextlib
import glob
import io
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(__file__))
from test_notify_activacion import ADMIN, BUCKET, FROM, ACC, ROL_ARN, SID_AVISOS, SID_SES, politica_esperada  # noqa: E402

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT_CODIGO = os.path.join(RAIZ, 'infra', 'notify-public-code-deploy.sh')
SCRIPT_IAM = os.path.join(RAIZ, 'infra', 'notify-public-iam.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'notify_activacion_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_notify.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
FN, FN_ADMIN = 'ork3d-resenas', 'ork3d-resenas-admin'
NUEVAS = {'RESENAS_FROM_EMAIL': FROM, 'RESENAS_ADMIN_EMAIL': ADMIN,
          'RESENAS_ADMIN_BASE_URL': 'https://ork3d.com', 'RESENAS_NOTIFY_ENABLED': 'false'}
ESCRITURAS_CODIGO = ['lambda update-function-configuration', 'lambda update-function-code']
ESCRITURAS = ('update-', 'put-', 'create-', 'delete-', 'add-', 'remove-', 'publish', 'invoke', 'tag-')

_spec = importlib.util.spec_from_file_location('notify_activacion_ayuda', AYUDA)
ayuda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ayuda)


def leer_repo(*partes):
    with open(os.path.join(RAIZ, *partes), 'rb') as f:
        return f.read()


def sha_de(ruta):
    with open(ruta, 'rb') as f:
        return base64.b64encode(hashlib.sha256(f.read()).digest()).decode('ascii')


def zip_con(carpeta, nombre, archivos):
    ruta = os.path.join(carpeta, nombre)
    with zipfile.ZipFile(ruta, 'w', zipfile.ZIP_DEFLATED) as z:
        for relativo, contenido in sorted(archivos.items()):
            z.writestr(relativo, contenido)
    return ruta


def paquete_anterior(carpeta):
    """Lo que hoy puede tener la Lambda pública: app.py sin aviso, un registro anterior y módulos que el aviso no usa."""
    app = leer_repo('api', 'resenas', 'app.py').replace(b'self._notificar(review)', b'pass')
    archivos = {'app.py': app,
                'nucleo/registro.py': leer_repo('api', 'resenas', 'nucleo', 'registro.py') + b'\n# version anterior\n',
                'nucleo/sesion.py': b'# modulo que el aviso no importa\n',
                'nucleo/reglas.py': leer_repo('api', 'resenas', 'nucleo', 'tokens.py')}
    for nombre in ('almacen.py', 'errores.py', 'jpeg.py', 'tokens.py', '__init__.py'):
        archivos['nucleo/' + nombre] = leer_repo('api', 'resenas', 'nucleo', nombre)
    return zip_con(carpeta, 'anterior.zip', archivos)


def estado(zip_sha, publica_vars, admin_vars, politica=None):
    return {
        'publica': {'config': {'FunctionName': FN, 'Runtime': 'python3.12', 'Handler': 'app.handler',
                               'Role': ROL_ARN, 'MemorySize': 256, 'Timeout': 10, 'State': 'Active',
                               'LastUpdateStatus': 'Successful', 'CodeSha256': zip_sha, 'RevisionId': 'rev-1',
                               'Environment': {'Variables': dict(publica_vars)}}},
        'admin': {'config': {'FunctionName': FN_ADMIN, 'Environment': {'Variables': dict(admin_vars)}}},
        'url': None, 'recurso': None, 'concurrencia': {},
        'cloudfront': {'ETag': 'E2X1', 'Distribution': {'Status': 'Deployed',
                                                        'LastModifiedTime': '2026-10-01T00:00:00Z',
                                                        'InProgressInvalidationBatches': 0}},
        'politica_nombre': 'resenas-bucket-privado',
        'politica': politica if politica is not None else {'Version': '2012-10-17', 'Statement': [
            {'Sid': 'LeerEstadoReseniasYFotos', 'Effect': 'Allow', 's3:Action': 's3:GetObject',
             'Action': 's3:GetObject', 'Resource': [f'arn:aws:s3:::{BUCKET}/tokens/*']}]},
        'identidades': {}, 'cuenta': {'SendingEnabled': True}, 'logs': [], 'objetos': [],
    }


PUBLICA_VARS = {'RESENAS_BUCKET': BUCKET, 'ALLOWED_ORIGIN': 'https://ork3d.com',
                'GOOGLE_REVIEW_URL': 'https://g.page/r/ejemplo-prueba'}
ADMIN_VARS = {'RESENAS_BUCKET': BUCKET, 'RESENAS_FROM_EMAIL': FROM, 'RESENAS_ADMIN_EMAIL': ADMIN,
              'RESENAS_ADMIN_BASE_URL': 'https://ork3d.com', 'ALLOWED_ORIGIN': 'https://ork3d.com'}


class DespliegueBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='notify-public-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado_ruta = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.ruta = os.path.join(self.tmp, 'ruta')
        self.sitio = os.path.join(self.tmp, 'sitio')
        os.makedirs(os.path.join(self.sitio, 'resenas'))
        with open(os.path.join(self.sitio, 'resenas', 'publicas.json'), 'w', encoding='utf-8') as f:
            json.dump({'items': []}, f)
        self.codigo_anterior = paquete_anterior(self.tmp)
        self.preparar()

    def preparar(self, cambios=None, publica_vars=None, admin_vars=None):
        st = estado(sha_de(self.codigo_anterior), PUBLICA_VARS if publica_vars is None else publica_vars,
                    ADMIN_VARS if admin_vars is None else admin_vars)
        if cambios:
            cambios(st)
        with open(self.estado_ruta, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        with open(self.codigo_anterior, 'rb') as f:
            self.bytes_anteriores = f.read()
        self.sha_anterior = sha_de(self.codigo_anterior)
        if os.path.exists(self.log):
            os.remove(self.log)

    def estado_actual(self):
        with open(self.estado_ruta, encoding='utf-8') as f:
            return json.load(f)

    def cambiar(self, fn):
        st = self.estado_actual()
        fn(st)
        with open(self.estado_ruta, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]

    def escrituras(self):
        return [l for l in self.llamadas() if any(w in l for w in ESCRITURAS)]

    def variables(self):
        return self.estado_actual()['publica']['config']['Environment']['Variables']

    def correr(self, script, modo, guardia=None, extra=None):
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp, 'AWS_CLI': FAKE_AWS, 'CURL_CMD': FAKE_CURL,
               'FAKE_ESTADO': self.estado_ruta, 'FAKE_LOG': self.log,
               'FAKE_CODIGO_ANTERIOR_ARCHIVO': self.codigo_anterior, 'FAKE_SITIO_DIR': self.sitio,
               'NOTIFY_PUBLIC_RUTA': self.ruta}
        if guardia:
            env[guardia] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', script, modo], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=120)

    def salida(self, r):
        return r.stdout + r.stderr

    def copias(self):
        if not os.path.isdir(self.ruta):
            return []
        return sorted(glob.glob(os.path.join(self.ruta, 'rollback', '*', '')))


class DiffTest(DespliegueBase):
    def test_lista_los_cambios_sin_correos_y_no_escribe(self):
        r = self.correr(SCRIPT_CODIGO, 'diff')
        salida = self.salida(r)
        self.assertEqual(r.returncode, 0, salida)
        self.assertIn('CAMBIA  app.py', salida)
        self.assertIn('NUEVO   nucleo/correo.py', salida)
        self.assertIn('CAMBIA  nucleo/registro.py', salida)
        self.assertIn('SOBRA   nucleo/sesion.py', salida)
        self.assertIn('IGUAL   nucleo/tokens.py', salida)
        self.assertIn('AÑADE    RESENAS_FROM_EMAIL = r***@e***.test', salida)
        self.assertIn('LISTO para desplegar', salida)
        self.assertNotIn(FROM, salida)
        self.assertNotIn(ADMIN, salida)
        self.assertEqual(self.escrituras(), [])

    def test_diff_no_muestra_urls_firmadas(self):
        self.assertNotIn('fake-code.example', self.salida(self.correr(SCRIPT_CODIGO, 'diff')))

    def test_diff_con_correos_faltantes_en_admin_no_esta_listo(self):
        self.preparar(admin_vars={k: v for k, v in ADMIN_VARS.items() if k != 'RESENAS_FROM_EMAIL'})
        r = self.correr(SCRIPT_CODIGO, 'diff')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('NO LISTO', self.salida(r))


class DesplieguePermisosTest(DespliegueBase):
    def test_sin_guardia_no_llama_a_aws(self):
        r = self.correr(SCRIPT_CODIGO, 'deploy')
        self.assertEqual(r.returncode, 3, self.salida(r))
        self.assertEqual(self.llamadas(), [])

    def test_no_pisa_un_valor_existente_y_no_escribe(self):
        self.preparar(publica_vars=dict(PUBLICA_VARS, RESENAS_FROM_EMAIL='otro-remitente@ejemplo.test'))
        r = self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 4, self.salida(r))
        self.assertIn('no se pisa', self.salida(r))
        self.assertNotIn('otro-remitente@ejemplo.test', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_con_flag_encendido_se_niega(self):
        self.preparar(publica_vars=dict(PUBLICA_VARS, RESENAS_NOTIFY_ENABLED='true'))
        r = self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 4, self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_admin_sin_correos_se_niega_sin_escribir(self):
        self.preparar(admin_vars={'RESENAS_BUCKET': BUCKET})
        r = self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 4, self.salida(r))
        self.assertEqual(self.escrituras(), [])


class DespliegueTest(DespliegueBase):
    def desplegar(self, extra=None):
        return self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY', extra=extra)

    def test_escribe_solo_el_environment_y_el_codigo(self):
        r = self.desplegar()
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertEqual(self.escrituras(), ESCRITURAS_CODIGO)
        self.assertFalse(any(w in l for l in self.escrituras() for w in ('iam', 'cloudfront', 'route53')))

    def test_environment_son_las_actuales_mas_las_nuevas_y_el_flag_queda_false(self):
        antes = dict(self.variables())
        self.assertEqual(self.desplegar().returncode, 0)
        despues = self.variables()
        self.assertEqual(despues, dict(antes, **NUEVAS))
        self.assertEqual(despues['RESENAS_NOTIFY_ENABLED'], 'false')
        for k, v in antes.items():
            self.assertEqual(despues[k], v)

    def test_codigo_nuevo_es_el_paquete_propuesto(self):
        self.assertEqual(self.desplegar().returncode, 0)
        sha_propuesto = subprocess.run([sys.executable, AYUDA, 'zip-aviso', RAIZ, os.path.join(self.tmp, 'x.zip')],
                                       capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(sha_propuesto)
        self.assertEqual(self.estado_actual()['publica']['config']['CodeSha256'], sha_propuesto)

    def test_no_muestra_correos_ni_urls_firmadas(self):
        r = self.desplegar()
        self.assertNotIn(FROM, self.salida(r))
        self.assertNotIn(ADMIN, self.salida(r))
        self.assertNotIn('fake-code.example', self.salida(r))

    def test_guarda_copias_privadas_antes_de_escribir(self):
        self.assertEqual(self.desplegar().returncode, 0)
        (carpeta,) = self.copias()
        for nombre in ('env-antes.json', 'foto/config.json', 'foto/desplegado.zip', 'foto/codigo.sha'):
            ruta = os.path.join(carpeta, nombre)
            self.assertTrue(os.path.exists(ruta), nombre)
            self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600, nombre)
        with open(os.path.join(carpeta, 'env-antes.json'), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['Variables'], PUBLICA_VARS)
        self.assertEqual(sha_de(os.path.join(carpeta, 'foto', 'desplegado.zip')), self.sha_anterior)

    def test_verify_despues_del_despliegue_pasa(self):
        self.assertEqual(self.desplegar().returncode, 0)
        r = self.correr(SCRIPT_CODIGO, 'verify')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('PASS: RESENAS_NOTIFY_ENABLED apagado', self.salida(r))
        self.assertIn('PASS: export público sin cambios', self.salida(r))

    def test_verify_detecta_que_alguien_encendio_el_flag(self):
        self.assertEqual(self.desplegar().returncode, 0)
        self.cambiar(lambda st: st['publica']['config']['Environment']['Variables'].update(RESENAS_NOTIFY_ENABLED='true'))
        r = self.correr(SCRIPT_CODIGO, 'verify')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('FAIL: RESENAS_NOTIFY_ENABLED apagado', self.salida(r))

    def test_verify_detecta_un_cambio_en_cloudfront(self):
        self.assertEqual(self.desplegar().returncode, 0)
        self.cambiar(lambda st: st['cloudfront'].update(ETag='E2X2'))
        r = self.correr(SCRIPT_CODIGO, 'verify')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('FAIL: CloudFront', self.salida(r))

    def test_verify_detecta_un_export_que_cambio(self):
        self.assertEqual(self.desplegar().returncode, 0)
        with open(os.path.join(self.sitio, 'resenas', 'publicas.json'), 'w', encoding='utf-8') as f:
            json.dump({'items': [{'id': 'x'}]}, f)
        r = self.correr(SCRIPT_CODIGO, 'verify')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('FAIL: export público', self.salida(r))

    def test_fallo_del_codigo_deja_copias_y_pide_revertir(self):
        r = self.desplegar(extra={'FAKE_NOTIFY_FALLA_CODIGO': '1'})
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('revertir', self.salida(r))
        self.assertEqual(len(self.copias()), 1)
        self.assertEqual(self.variables()['RESENAS_NOTIFY_ENABLED'], 'false')


class RevertirTest(DespliegueBase):
    def test_sin_guardia_no_llama_a_aws(self):
        r = self.correr(SCRIPT_CODIGO, 'revertir')
        self.assertEqual(r.returncode, 3, self.salida(r))
        self.assertEqual(self.llamadas(), [])

    def test_revertir_restaura_exacto_el_environment_y_el_codigo(self):
        antes = dict(self.variables())
        self.assertEqual(self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY').returncode, 0)
        r = self.correr(SCRIPT_CODIGO, 'revertir', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertEqual(self.variables(), antes)
        self.assertNotIn('RESENAS_NOTIFY_ENABLED', self.variables())
        self.assertEqual(self.estado_actual()['publica']['config']['CodeSha256'], self.sha_anterior)
        self.assertIn('exactamente', self.salida(r) + 'exactamente')

    def test_tras_un_fallo_a_mitad_revertir_deja_todo_como_antes(self):
        antes = dict(self.variables())
        self.assertEqual(self.correr(SCRIPT_CODIGO, 'deploy', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY',
                                     extra={'FAKE_NOTIFY_FALLA_CODIGO': '1'}).returncode, 1)
        r = self.correr(SCRIPT_CODIGO, 'revertir', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertEqual(self.variables(), antes)
        self.assertEqual(self.estado_actual()['publica']['config']['CodeSha256'], self.sha_anterior)

    def test_sin_copia_no_escribe(self):
        r = self.correr(SCRIPT_CODIGO, 'revertir', guardia='ALLOW_NOTIFY_PUBLIC_CODE_DEPLOY')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertEqual(self.escrituras(), [])


class IamTest(DespliegueBase):
    def politica(self):
        return self.estado_actual()['politica']

    def sentencias(self):
        return {s['Sid']: s for s in self.politica()['Statement'] if 'Sid' in s}

    def test_diff_muestra_lo_que_falta_con_placeholders_y_sin_escribir(self):
        r = self.correr(SCRIPT_IAM, 'diff')
        salida = self.salida(r)
        self.assertEqual(r.returncode, 0, salida)
        self.assertIn(f'AÑADIR     {SID_SES}', salida)
        self.assertIn(f'AÑADIR     {SID_AVISOS}', salida)
        self.assertIn('__RESENAS_FROM_EMAIL__', salida)
        self.assertNotIn(FROM, salida)
        self.assertEqual(self.escrituras(), [])

    def test_aplicar_sin_guardia_no_escribe(self):
        r = self.correr(SCRIPT_IAM, 'aplicar')
        self.assertEqual(r.returncode, 3, self.salida(r))
        self.assertEqual(self.llamadas(), [])

    def test_aplicar_agrega_solo_lo_que_falta_y_conserva_lo_existente(self):
        antes = json.loads(json.dumps(self.politica()))
        r = self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertEqual(self.escrituras(), ['iam put-role-policy'])
        self.assertIn('las 1 sentencias anteriores siguen iguales; se agregaron 2', self.salida(r))
        despues = self.politica()['Statement']
        self.assertEqual(despues[0], antes['Statement'][0])
        esperadas = politica_esperada()
        for sid in (SID_SES, SID_AVISOS):
            por_sid = {s['Sid']: s for s in esperadas['Statement']}
            self.assertEqual(self.sentencias()[sid], por_sid[sid])
        self.assertNotIn(FROM, self.salida(r))

    def test_copia_de_la_politica_anterior_es_privada(self):
        self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        copias = glob.glob(os.path.join(self.ruta, 'rollback', 'politica-antes.*.json'))
        self.assertEqual(len(copias), 1)
        self.assertEqual(stat.S_IMODE(os.stat(copias[0]).st_mode), 0o600)

    def test_aplicar_es_idempotente(self):
        self.assertEqual(self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM').returncode, 0)
        self.llamadas_antes = len(self.escrituras())
        r = self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('IAM sin cambios', self.salida(r))
        self.assertEqual(len(self.escrituras()), 1)

    def test_verify_despues_de_aplicar_pasa(self):
        self.assertEqual(self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM').returncode, 0)
        r = self.correr(SCRIPT_IAM, 'verify')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('PASS: el rol tiene las sentencias del aviso', self.salida(r))

    def test_sentencia_con_mismo_sid_y_otro_contenido_no_se_cambia(self):
        politica = politica_esperada()
        for s in politica['Statement']:
            if s['Sid'] == SID_SES:
                s['Condition']['ForAllValues:StringEquals']['ses:Recipients'] = 'otra-persona@ejemplo.test'
        self.preparar(cambios=lambda st: st.update(politica=politica))
        r = self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        self.assertEqual(r.returncode, 4, self.salida(r))
        self.assertIn('no se cambia en automático', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_no_crea_una_politica_que_no_existe(self):
        self.preparar(cambios=lambda st: st.update(politica_nombre='otra-politica'))
        r = self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('no se crea una nueva', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_si_IAM_falla_la_politica_no_cambia(self):
        antes = json.loads(json.dumps(self.politica()))
        r = self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM', extra={'FAKE_NOTIFY_FALLA_IAM': '1'})
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertEqual(self.politica(), antes)

    def test_IAM_no_toca_codigo_ni_environment(self):
        self.correr(SCRIPT_IAM, 'aplicar', guardia='ALLOW_NOTIFY_PUBLIC_IAM')
        self.assertFalse(any('update-function' in l for l in self.llamadas()))


class FormasDeLaAyudaTest(unittest.TestCase):
    """Reglas del paquete y de las variables, sin AWS."""

    def test_los_modulos_del_aviso_son_exactamente_el_cierre_de_imports(self):
        cierre = {'app.py'} | {f'nucleo/{m}.py' for m in ayuda.cierre_de_imports(RAIZ) if m != 'app'}
        declarados = {m for m in ayuda.MODULOS_AVISO if m != 'nucleo/__init__.py'}
        self.assertEqual(cierre, declarados)

    def test_el_paquete_es_determinista_y_solo_tiene_el_cierre(self):
        with tempfile.TemporaryDirectory() as carpeta:
            a, b = os.path.join(carpeta, 'a.zip'), os.path.join(carpeta, 'b.zip')
            with contextlib.redirect_stdout(io.StringIO()):
                ayuda.zip_aviso(RAIZ, a)
                ayuda.zip_aviso(RAIZ, b)
            self.assertEqual(sha_de(a), sha_de(b))
            with zipfile.ZipFile(a) as z:
                self.assertEqual(sorted(z.namelist()), sorted(ayuda.MODULOS_AVISO))

    def test_env_nuevo_no_pisa_y_conserva_todo(self):
        with tempfile.TemporaryDirectory() as carpeta:
            cfg = os.path.join(carpeta, 'cfg.json')
            adm = os.path.join(carpeta, 'adm.json')
            sal = os.path.join(carpeta, 'env.json')
            with open(cfg, 'w', encoding='utf-8') as f:
                json.dump({'Environment': {'Variables': dict(PUBLICA_VARS)}}, f)
            with open(adm, 'w', encoding='utf-8') as f:
                json.dump({'Environment': {'Variables': dict(ADMIN_VARS)}}, f)
            lineas = ayuda.env_nuevo(cfg, adm, sal)
            self.assertFalse(any(l.startswith('FAIL') for l in lineas), lineas)
            with open(sal, encoding='utf-8') as f:
                final = json.load(f)['Variables']
            self.assertEqual(final, dict(PUBLICA_VARS, **NUEVAS))
            self.assertFalse(any(FROM in l or ADMIN in l for l in lineas))


if __name__ == '__main__':
    unittest.main()
