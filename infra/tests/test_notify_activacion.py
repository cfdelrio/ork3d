"""Pruebas de infra/notify-activacion.sh y de infra/notify_activacion_ayuda.py contra el simulador con estado.

Sin AWS, sin red y sin credenciales: aws_fake_notify.py guarda la configuración de la Lambda pública en un JSON;
aws_fake_curl.py sirve el código desplegado. Lo que se prueba: el aviso se activa con UNA escritura que cambia solo
RESENAS_NOTIFY_ENABLED; la comprobación previa bloquea lo que no está listo; revertir restaura exactamente lo
guardado; la observación cuenta sin mostrar textos; y nada imprime correos.
"""
import base64
import glob
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
import uuid
import zipfile

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'notify-activacion.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'notify_activacion_ayuda.py')
PLANTILLA = os.path.join(RAIZ, 'infra', 'resenas', 'lambda-permisos.json')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_notify.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
FN = 'ork3d-resenas'
ACC = '9' * 12
ROL_ARN = f'arn:aws:iam::{ACC}:role/ork3d-resenas-lambda'
BUCKET = 'bucket-privado-fake-1234'
FROM = 'remitente-prueba@ejemplo.test'
ADMIN = 'moderacion-prueba@ejemplo.test'
SID_SES = 'NotificarNuevaResenaAlModerador'
SID_AVISOS = 'GuardarAvisosDeNotificacion'
RESENA = '0123456789abcdef0123456789abcdef'
IDENTIDAD = {'IdentityType': 'EMAIL_ADDRESS', 'VerificationStatus': 'SUCCESS', 'VerifiedForSendingStatus': True}
ESCRITURAS = ('update-', 'put-', 'create-', 'delete-', 'add-', 'remove-', 'publish', 'invoke', 'tag-')

_spec = importlib.util.spec_from_file_location('notify_activacion_ayuda', AYUDA)
ayuda = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ayuda)


def variables(flag=None):
    v = {'RESENAS_BUCKET': BUCKET, 'ALLOWED_ORIGIN': 'https://ork3d.com',
         'GOOGLE_REVIEW_URL': 'https://g.page/r/ejemplo-prueba', 'RESENAS_ADMIN_BASE_URL': 'https://ork3d.com',
         'RESENAS_FROM_EMAIL': FROM, 'RESENAS_ADMIN_EMAIL': ADMIN}
    if flag is not None:
        v['RESENAS_NOTIFY_ENABLED'] = flag
    return v


def politica_esperada():
    """Lo que lambda-permisos.json renderizado debe dar para el rol de la Lambda pública (escrito a mano)."""
    return {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'LeerEstadoReseniasYFotos', 'Effect': 'Allow', 'Action': 's3:GetObject',
         'Resource': [f'arn:aws:s3:::{BUCKET}/tokens/*']},
        {'Sid': SID_AVISOS, 'Effect': 'Allow', 'Action': 's3:PutObject',
         'Resource': f'arn:aws:s3:::{BUCKET}/notificaciones/*'},
        {'Sid': SID_SES, 'Effect': 'Allow', 'Action': 'ses:SendEmail',
         'Resource': f'arn:aws:ses:us-east-1:{ACC}:identity/{FROM}',
         'Condition': {'StringEquals': {'ses:FromAddress': FROM},
                       'ForAllValues:StringEquals': {'ses:Recipients': ADMIN}}}]}


def leer_repo(*partes):
    with open(os.path.join(RAIZ, *partes), 'rb') as f:
        return f.read()


def paquete(carpeta, modificados=None):
    """Zip con app.py y nucleo/*.py, como resenas-setup.sh: el código del repo, salvo lo que se cambie."""
    modificados = modificados or {}
    rutas = {'app.py': os.path.join(RAIZ, 'api', 'resenas', 'app.py')}
    for ruta in sorted(glob.glob(os.path.join(RAIZ, 'api', 'resenas', 'nucleo', '*.py'))):
        rutas['nucleo/' + os.path.basename(ruta)] = ruta
    destino = os.path.join(carpeta, f'paquete-{uuid.uuid4().hex}.zip')
    with zipfile.ZipFile(destino, 'w', zipfile.ZIP_DEFLATED) as z:
        for nombre, ruta in sorted(rutas.items()):
            if nombre in modificados:
                contenido = modificados[nombre]
            else:
                with open(ruta, 'rb') as f:
                    contenido = f.read()
            z.writestr(nombre, contenido)
    with open(destino, 'rb') as f:
        sha = base64.b64encode(hashlib.sha256(f.read()).digest()).decode('ascii')
    return destino, sha


def estado(zip_sha, flag=None, politica=None, identidades=None, cuenta=True):
    return {
        'publica': {'config': {'FunctionName': FN, 'Runtime': 'python3.12', 'Handler': 'app.handler',
                               'Role': ROL_ARN, 'MemorySize': 256, 'Timeout': 10, 'State': 'Active',
                               'LastUpdateStatus': 'Successful', 'CodeSha256': zip_sha, 'RevisionId': 'rev-1',
                               'Environment': {'Variables': variables(flag)}}},
        'politica_nombre': 'resenas-bucket-privado',
        'politica': politica if politica is not None else politica_esperada(),
        'identidades': identidades if identidades is not None else {FROM: dict(IDENTIDAD), ADMIN: dict(IDENTIDAD)},
        'cuenta': {'SendingEnabled': cuenta},
        'logs': [],
        'objetos': [],
    }


class NotifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='notify-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado_ruta = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.notify_ruta = os.path.join(self.tmp, 'notify')
        self.preparar()

    def preparar(self, cambios=None, modificados=None, flag=None, politica=None, identidades=None, cuenta=True):
        self.zip_ruta, self.zip_sha = paquete(self.tmp, modificados)
        st = estado(self.zip_sha, flag=flag, politica=politica, identidades=identidades, cuenta=cuenta)
        if cambios:
            cambios(st)
        with open(self.estado_ruta, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        if os.path.exists(self.log):
            os.remove(self.log)

    def estado_actual(self):
        with open(self.estado_ruta, encoding='utf-8') as f:
            return json.load(f)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [l.strip() for l in f if l.strip()]

    def escrituras(self):
        return [l for l in self.llamadas() if any(w in l for w in ESCRITURAS)]

    def variables_actuales(self):
        return self.estado_actual()['publica']['config']['Environment']['Variables']

    def correr(self, modo, extra=None, con_guardia=False):
        env = {'PATH': os.environ.get('PATH', ''), 'HOME': self.tmp, 'AWS_CLI': FAKE_AWS, 'CURL_CMD': FAKE_CURL,
               'FAKE_ESTADO': self.estado_ruta, 'FAKE_LOG': self.log,
               'FAKE_CODIGO_ANTERIOR_ARCHIVO': self.zip_ruta, 'NOTIFY_RUTA': self.notify_ruta}
        if con_guardia:
            env['ALLOW_NOTIFY_ENABLE'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', SCRIPT, modo], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=120)

    def salida(self, r):
        return r.stdout + r.stderr


class PreflightTest(NotifyCase):
    def test_listo_para_activar_y_no_escribe_nada(self):
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('LISTO PARA ACTIVAR', self.salida(r))
        self.assertEqual(self.escrituras(), [])
        self.assertIn('lambda get-function-configuration', self.llamadas())

    def test_no_muestra_correos_ni_direccion_de_descarga(self):
        r = self.correr('preflight')
        visible = self.salida(r)
        self.assertNotIn(FROM, visible)
        self.assertNotIn(ADMIN, visible)
        self.assertNotIn('fake-code.example', visible)
        self.assertIn('r***@', visible)   # el correo sale enmascarado

    def test_codigo_sin_el_aviso_bloquea_y_pide_desplegar(self):
        self.preparar(modificados={'app.py': leer_repo('api', 'resenas', 'app.py')
                                   .replace(b'self._notificar(review)', b'pass')})
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('no trae el aviso', self.salida(r))
        self.assertIn('NO LISTO', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_correo_py_distinto_de_head_bloquea(self):
        original = leer_repo('api', 'resenas', 'nucleo', 'correo.py')
        self.preparar(modificados={'nucleo/correo.py': original + b'\n# cambio de prueba\n'})
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('nucleo/correo.py difiere de HEAD', self.salida(r))

    def test_registro_distinto_bloquea_porque_correo_lo_importa(self):
        original = leer_repo('api', 'resenas', 'nucleo', 'registro.py')
        self.preparar(modificados={'nucleo/registro.py': original + b'\n# version anterior de prueba\n'})
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('nucleo/registro.py difiere de HEAD', self.salida(r))

    def test_modulo_fuera_del_cierre_solo_se_informa(self):
        self.preparar(modificados={'nucleo/sesion.py': b'# modulo que el aviso no importa\n'})
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('INFO: nucleo/sesion.py está en el paquete pero el aviso no lo importa', self.salida(r))

    def test_politica_sin_sentencia_ses_bloquea(self):
        politica = politica_esperada()
        politica['Statement'] = [s for s in politica['Statement'] if s['Sid'] != SID_SES]
        self.preparar(politica=politica)
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn(SID_SES, self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_politica_con_otro_destinatario_bloquea(self):
        politica = politica_esperada()
        ses = [s for s in politica['Statement'] if s['Sid'] == SID_SES][0]
        ses['Condition']['ForAllValues:StringEquals']['ses:Recipients'] = 'otra-persona-prueba@ejemplo.test'
        self.preparar(politica=politica)
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('no tiene exactas las sentencias', self.salida(r))

    def test_identidad_sin_verificar_bloquea(self):
        identidades = {FROM: dict(IDENTIDAD), ADMIN: dict(IDENTIDAD, VerificationStatus='PENDING')}
        self.preparar(identidades=identidades)
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('es EMAIL_ADDRESS verificada', self.salida(r))

    def test_ses_con_envio_deshabilitado_bloquea(self):
        self.preparar(cuenta=False)
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('SES de la cuenta habilitado', self.salida(r))

    def test_flag_ya_activa_no_vuelve_a_escribir(self):
        self.preparar(flag='true')
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('YA ACTIVO', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_flag_con_valor_raro_bloquea(self):
        self.preparar(flag='si')
        r = self.correr('preflight')
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('valor no reconocido', self.salida(r))


class ActivarTest(NotifyCase):
    def test_sin_guardia_no_llama_a_aws(self):
        r = self.correr('activar')
        self.assertEqual(r.returncode, 3, self.salida(r))
        self.assertIn('ABORTA', self.salida(r))
        self.assertEqual(self.llamadas(), [])

    def test_activar_escribe_una_sola_vez_y_solo_la_flag(self):
        antes = dict(self.variables_actuales())
        r = self.correr('activar', con_guardia=True)
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('Aviso activado', self.salida(r))
        self.assertEqual(self.escrituras(), ['lambda update-function-configuration'])
        despues = self.variables_actuales()
        self.assertEqual(despues['RESENAS_NOTIFY_ENABLED'], 'true')
        self.assertEqual({k: v for k, v in despues.items() if k != 'RESENAS_NOTIFY_ENABLED'}, antes)

    def test_la_copia_para_revertir_es_privada_y_tiene_lo_de_antes(self):
        self.correr('activar', con_guardia=True)
        carpeta = os.path.join(self.notify_ruta, 'rollback')
        copias = [n for n in os.listdir(carpeta) if n.startswith('notify-antes.')]
        self.assertEqual(len(copias), 1)
        ruta = os.path.join(carpeta, copias[0])
        self.assertEqual(stat.S_IMODE(os.stat(ruta).st_mode), 0o600)
        with open(ruta, encoding='utf-8') as f:
            guardadas = json.load(f)['Variables']
        self.assertNotIn('RESENAS_NOTIFY_ENABLED', guardadas)
        self.assertEqual(guardadas, {k: v for k, v in self.variables_actuales().items()
                                     if k != 'RESENAS_NOTIFY_ENABLED'})

    def test_activar_no_muestra_correos(self):
        r = self.correr('activar', con_guardia=True)
        self.assertNotIn(FROM, self.salida(r))
        self.assertNotIn(ADMIN, self.salida(r))

    def test_revision_movida_se_rechaza_sin_cambiar_nada(self):
        antes = dict(self.variables_actuales())
        r = self.correr('activar', extra={'FAKE_NOTIFY_MUEVE_REVISION': '1'}, con_guardia=True)
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('la Lambda no cambió', self.salida(r))
        self.assertEqual(self.variables_actuales(), antes)

    def test_fallo_de_la_escritura_no_cambia_nada(self):
        antes = dict(self.variables_actuales())
        r = self.correr('activar', extra={'FAKE_NOTIFY_FALLA_UPDATE': '1'}, con_guardia=True)
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertEqual(self.variables_actuales(), antes)

    def test_no_activa_si_preflight_no_esta_listo(self):
        politica = politica_esperada()
        politica['Statement'] = []
        self.preparar(politica=politica)
        r = self.correr('activar', con_guardia=True)
        self.assertEqual(r.returncode, 4, self.salida(r))
        self.assertIn('no se escribió nada', self.salida(r))
        self.assertEqual(self.escrituras(), [])

    def test_repetir_activar_no_escribe_otra_vez(self):
        self.assertEqual(self.correr('activar', con_guardia=True).returncode, 0)
        r = self.correr('activar', con_guardia=True)
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('YA ACTIVO', self.salida(r))
        self.assertEqual(len(self.escrituras()), 1)   # el log acumula: solo la primera escritura


class RevertirTest(NotifyCase):
    def test_sin_guardia_no_llama_a_aws(self):
        r = self.correr('revertir')
        self.assertEqual(r.returncode, 3, self.salida(r))
        self.assertEqual(self.llamadas(), [])

    def test_revertir_restaura_exactamente_lo_guardado(self):
        original = dict(self.variables_actuales())
        self.assertEqual(self.correr('activar', con_guardia=True).returncode, 0)
        r = self.correr('revertir', con_guardia=True)
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn('exactamente como antes', self.salida(r))
        self.assertEqual(self.variables_actuales(), original)
        self.assertNotIn('RESENAS_NOTIFY_ENABLED', self.variables_actuales())

    def test_sin_copia_no_escribe(self):
        r = self.correr('revertir', con_guardia=True)
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertIn('no hay copia', self.salida(r))
        self.assertEqual(self.escrituras(), [])


class ObservarTest(NotifyCase):
    def mensajes(self):
        return [
            'START RequestId: 6f1c8e2a-0b7d-4a4e-9a55-1f0d6c2e9b11 Version: $LATEST',
            f'2026-10-22T10:00:00Z\t1\t{{"ev": "resena_creada", "ts": "2026-10-22T10:00:00Z", "review_id": "{RESENA}", "foto": false}}',
            f'2026-10-22T10:00:01Z\t1\t{{"ev": "aviso_enviado", "ts": "2026-10-22T10:00:01Z", "review_id": "{RESENA}"}}',
            f'2026-10-22T10:05:00Z\t2\t{{"ev": "aviso_fallido", "ts": "2026-10-22T10:05:00Z", "review_id": "{RESENA}", "error": "ClientError"}}',
            f'2026-10-22T10:06:00Z\t2\t{{"ev": "aviso_fallido", "ts": "2026-10-22T10:06:00Z", "review_id": "{RESENA}", "error": "EndpointConnectionError"}}',
            'END RequestId: 6f1c8e2a-0b7d-4a4e-9a55-1f0d6c2e9b11',
        ]

    def test_cuenta_eventos_sin_mostrar_textos(self):
        self.preparar(cambios=lambda st: st.update({'logs': self.mensajes()}))
        r = self.correr('observar')
        visible = self.salida(r)
        self.assertEqual(r.returncode, 0, visible)
        self.assertIn('INFO: resena_creada: 1', visible)
        self.assertIn('INFO: aviso_enviado: 1', visible)
        self.assertIn('INFO: aviso_fallido: 2', visible)
        self.assertIn('aviso_fallido por error ClientError: 1', visible)
        self.assertIn('aviso_fallido por error EndpointConnectionError: 1', visible)
        self.assertNotIn(RESENA, visible)
        self.assertNotIn('START RequestId', visible)

    def test_review_id_con_aviso_registrado_pasa(self):
        self.preparar(cambios=lambda st: st.update({'objetos': [f'notificaciones/{RESENA}.json']}))
        r = self.correr('observar', extra={'REVIEW_ID': RESENA})
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn(f'PASS  notificaciones/{RESENA}.json existe', self.salida(r))

    def test_review_id_sin_aviso_es_informativo(self):
        r = self.correr('observar', extra={'REVIEW_ID': RESENA})
        self.assertEqual(r.returncode, 0, self.salida(r))
        self.assertIn(f'notificaciones/{RESENA}.json no existe todavía', self.salida(r))

    def test_review_id_con_formato_invalido_no_consulta_nada(self):
        r = self.correr('observar', extra={'REVIEW_ID': '../otra'})
        self.assertEqual(r.returncode, 1, self.salida(r))
        self.assertNotIn('s3api head-object', self.llamadas())


class ScriptTest(unittest.TestCase):
    """Estático: el script solo tiene una escritura en AWS y no invoca los scripts de despliegue."""

    def setUp(self):
        with open(SCRIPT, encoding='utf-8') as f:
            self.texto = f.read()
        self.codigo = '\n'.join(l for l in self.texto.splitlines() if not l.lstrip().startswith('#'))

    def test_una_sola_escritura_y_es_la_configuracion(self):
        self.assertEqual(self.codigo.count('lambda update-function-configuration'), 1)
        for operacion in ('update-function-code', 'put-', 'create-', 'delete-', 'add-permission', 'publish',
                          'invoke', 'update-distribution'):
            self.assertNotIn(operacion, self.codigo)

    def test_no_llama_a_los_scripts_de_despliegue(self):
        for script in ('resenas-setup.sh', 'setup.sh', 'admin-deploy.sh', 'admin-frontend-deploy.sh'):
            self.assertNotIn(script, self.codigo)

    def test_flag_fijo_y_funcion_correcta(self):
        self.assertIn('FN="ork3d-resenas"', self.texto)
        self.assertNotIn('ork3d-resenas-admin', self.codigo)


class AyudaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='notify-ayuda-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def archivo(self, nombre, doc):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(doc, f)
        return ruta

    def config(self, flag=None):
        return self.archivo('config.json', {'Environment': {'Variables': variables(flag)}, 'Runtime': 'python3.12'})

    def test_plantilla_renderizada_coincide_con_las_sentencias_esperadas(self):
        esperadas = ayuda.sentencias_esperadas(self.config(), PLANTILLA, ACC)
        por_sid = {s['Sid']: s for s in politica_esperada()['Statement']}
        self.assertEqual(esperadas[SID_SES], por_sid[SID_SES])
        self.assertEqual(esperadas[SID_AVISOS], por_sid[SID_AVISOS])

    def test_estado_de_la_flag(self):
        for valor, esperado in ((None, 'apagado'), ('false', 'apagado'), ('FALSE', 'apagado'),
                                ('true', 'activo'), ('True', 'activo'), ('1', 'desconocido'), ('', 'desconocido')):
            with self.subTest(valor=valor):
                self.assertEqual(ayuda.estado_flag(self.config(valor)), esperado)

    def test_plan_activar_solo_cambia_la_flag(self):
        salida = os.path.join(self.tmp, 'env.json')
        ayuda.plan_activar(self.config(), salida)
        with open(salida, encoding='utf-8') as f:
            nuevo = json.load(f)['Variables']
        self.assertEqual(nuevo, dict(variables(), RESENAS_NOTIFY_ENABLED='true'))
        self.assertEqual(stat.S_IMODE(os.stat(salida).st_mode), 0o600)

    def test_comparar_activar_detecta_otra_variable_sin_mostrar_valores(self):
        antes = self.config()
        despues_doc = {'Environment': {'Variables': dict(variables(), RESENAS_NOTIFY_ENABLED='true',
                                                         ALLOWED_ORIGIN='https://otro.example')},
                       'Runtime': 'python3.12', 'LastUpdateStatus': 'Successful'}
        despues = self.archivo('despues.json', despues_doc)
        lineas = ayuda.comparar(antes, despues, 'activar')
        self.assertTrue(any(l.startswith('FAIL') and 'ALLOWED_ORIGIN' in l for l in lineas), lineas)
        self.assertFalse(any('https://otro.example' in l for l in lineas))

    def test_comparar_revertir_exige_igualdad_exacta(self):
        guardado = self.archivo('copia.json', {'Variables': variables()})
        igual = self.config()
        self.assertTrue(ayuda.comparar(guardado, igual, 'revertir')[0].startswith('PASS'))
        distinto = self.config('true')
        self.assertTrue(ayuda.comparar(guardado, distinto, 'revertir')[0].startswith('FAIL'))

    def test_logs_ignora_lineas_que_no_son_json_y_textos_sin_ev(self):
        ruta = self.archivo('logs.json', {'events': [
            {'message': 'texto suelto sin llaves'},
            {'message': '{"ev": "otro_evento"}'},
            {'message': '{"ev": "aviso_enviado", "review_id": "x"}'},
            {'eventId': 'sin-mensaje'},
        ]})
        lineas = ayuda.logs(ruta)
        self.assertIn('INFO: eventos en la ventana: 4', lineas)
        self.assertIn('INFO: aviso_enviado: 1', lineas)
        self.assertIn('INFO: aviso_fallido: 0', lineas)

    def test_codigo_sin_hook_da_fail_y_no_informa_contenido(self):
        zip_ruta, _ = paquete(self.tmp, {'app.py': b'print("sin aviso")\n'})
        lineas = ayuda.codigo(zip_ruta, RAIZ)
        self.assertTrue(lineas[0].startswith('FAIL'), lineas)
        self.assertNotIn('sin aviso', '\n'.join(lineas))


if __name__ == '__main__':
    unittest.main()
