"""Pruebas de infra/smoke-ses.sh contra el simulador de SES (infra/tests/aws_fake_ses.py).

Sin red, sin credenciales y sin envíos reales: el simulador registra cada llamada y copia la petición de envío.
Las direcciones usan el dominio reservado .test, que no existe.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SMOKE = os.path.join(RAIZ, 'infra', 'smoke-ses.sh')
FAKE = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_ses.py')
REMITENTE = 'remitente-prueba@ejemplo.test'
DESTINO = 'moderacion-prueba@ejemplo.test'
ASUNTO = 'ORK3D · prueba técnica SES V2'
CUERPO = 'Prueba técnica del sistema de reseñas ORK3D. No requiere ninguna acción.'
PERMITIDAS = {('sts', 'get-caller-identity'), ('sesv2', 'get-account'), ('sesv2', 'get-email-identity'),
              ('ses', 'send-email')}
HEX32 = re.compile(r'\b[0-9a-f]{32}\b')
IDENTIDADES_OK = [
    {'IdentityType': 'EMAIL_ADDRESS', 'IdentityName': REMITENTE, 'VerificationStatus': 'SUCCESS',
     'VerifiedForSendingStatus': True},
    {'IdentityType': 'EMAIL_ADDRESS', 'IdentityName': DESTINO, 'VerificationStatus': 'SUCCESS',
     'VerifiedForSendingStatus': True},
]


class SmokeSesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='smoke-ses-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.copia = os.path.join(self.tmp, 'envio.json')

    def correr(self, extra=None, *, remitente=REMITENTE, destino=DESTINO, permitir=True):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE, 'FAKE_LOG': self.log,
               'FAKE_ENVIO_COPIA': self.copia, 'AWS_PROFILE': 'default', 'AWS_REGION': 'us-east-1'}
        if permitir:
            env['ALLOW_SES_SEND'] = '1'
        if remitente is not None:
            env['RESENAS_FROM_EMAIL'] = remitente
        if destino is not None:
            env['RESENAS_ADMIN_EMAIL'] = destino
        env.update(extra or {})
        return subprocess.run(['bash', SMOKE], env=env, capture_output=True, text=True, timeout=120)

    def llamadas(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding='utf-8') as f:
            return [tuple(linea.split()[:2]) for linea in f if linea.strip()]

    def envios(self):
        return [llamada for llamada in self.llamadas() if llamada == ('ses', 'send-email')]

    def identidades(self, *identidades):
        return {'FAKE_IDENTIDADES': json.dumps(list(identidades))}

    def test_sin_allow_aborta_sin_llamar_a_aws(self):
        for valor in (None, '0', ''):
            with self.subTest(valor=valor):
                extra = {} if valor is None else {'ALLOW_SES_SEND': valor}
                r = self.correr(extra, permitir=False)
                self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
                self.assertIn('ABORTA', r.stderr)
                self.assertEqual(self.llamadas(), [])

    def test_sin_remitente_aborta_con_2_antes_de_aws(self):
        r = self.correr(remitente=None)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('falta RESENAS_FROM_EMAIL', r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_sin_destinatario_aborta_con_2_antes_de_aws(self):
        r = self.correr(destino=None)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('falta RESENAS_ADMIN_EMAIL', r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_formato_invalido_aborta_con_2_sin_mostrar_el_valor(self):
        r = self.correr(remitente='no-es-un-correo')
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('RESENAS_FROM_EMAIL no tiene formato de correo', r.stderr)
        self.assertNotIn('no-es-un-correo', r.stdout + r.stderr)
        self.assertEqual(self.llamadas(), [])

    def test_identidad_no_verificada_no_envia(self):
        pendiente = dict(IDENTIDADES_OK[0], VerificationStatus='PENDING')
        r = self.correr(self.identidades(pendiente, IDENTIDADES_OK[1]))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('remitente: no utilizable: verificación PENDING', r.stdout)
        self.assertEqual(self.envios(), [])

    def test_destinatario_no_habilitado_para_enviar_no_envia(self):
        sin_envio = dict(IDENTIDADES_OK[1], VerifiedForSendingStatus=False)
        r = self.correr(self.identidades(IDENTIDADES_OK[0], sin_envio))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('destinatario: no utilizable: no está habilitada para enviar', r.stdout)
        self.assertEqual(self.envios(), [])

    def test_destinatario_cubierto_solo_por_dominio_no_envia(self):
        # En el sandbox el destinatario debe ser una identidad EMAIL_ADDRESS verificada.
        dominio = {'IdentityType': 'DOMAIN', 'IdentityName': 'ejemplo.test', 'VerificationStatus': 'SUCCESS',
                   'VerifiedForSendingStatus': True, 'DkimStatus': 'SUCCESS'}
        r = self.correr(self.identidades(IDENTIDADES_OK[0], dominio))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no es una identidad EMAIL_ADDRESS de SES', r.stderr)
        self.assertEqual(self.envios(), [])

    def test_cuenta_con_envio_deshabilitado_no_envia(self):
        r = self.correr({'FAKE_ENVIO_DESHABILITADO': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('envío habilitado=no', r.stdout)
        self.assertEqual(self.envios(), [])

    def test_autenticacion_fallida_no_envia(self):
        r = self.correr({'FAKE_STS_FAIL': '1'})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('no se pudo autenticar', r.stderr)
        self.assertEqual(self.envios(), [])

    def test_sandbox_se_informa(self):
        r = self.correr()
        self.assertIn('sandbox=sí', r.stdout)
        r_prod = self.correr({'FAKE_PRODUCCION': '1'})
        self.assertIn('sandbox=no', r_prod.stdout)

    def test_envio_exitoso_envia_exactamente_un_correo_con_el_contenido_pedido(self):
        r = self.correr(self.identidades(*IDENTIDADES_OK))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.envios()), 1)
        with open(self.copia, encoding='utf-8') as f:
            pedido = json.load(f)
        self.assertEqual(pedido['Source'], REMITENTE)
        self.assertEqual(pedido['Destination'], {'ToAddresses': [DESTINO]})
        self.assertEqual(pedido['Message']['Subject'], {'Data': ASUNTO, 'Charset': 'UTF-8'})
        self.assertEqual(pedido['Message']['Body'], {'Text': {'Data': CUERPO, 'Charset': 'UTF-8'}})
        self.assertNotIn('Html', pedido['Message']['Body'])

    def test_envio_exitoso_muestra_messageid_truncado(self):
        r = self.correr(self.identidades(*IDENTIDADES_OK))
        self.assertRegex(r.stdout, r'MessageId: [0-9a-f]{8}… \(truncado\)')
        self.assertIsNone(HEX32.search(r.stdout + r.stderr))

    def test_envio_fallido_se_reporta_y_no_reintenta(self):
        r = self.correr(dict(self.identidades(*IDENTIDADES_OK), FAKE_SEND_FAIL='1'))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('código MessageRejected', r.stderr)
        self.assertIn('No se reintenta', r.stderr)
        self.assertEqual(len(self.envios()), 1)

    def test_no_muestra_las_direcciones_completas(self):
        r = self.correr(self.identidades(*IDENTIDADES_OK))
        visible = r.stdout + r.stderr
        self.assertNotIn(REMITENTE, visible)
        self.assertNotIn(DESTINO, visible)
        self.assertIn('r***@e***.test', visible)
        self.assertIn('m***@e***.test', visible)

    def test_ningun_cambio_de_configuracion_ni_escritura(self):
        self.correr(self.identidades(*IDENTIDADES_OK))
        self.correr(dict(self.identidades(*IDENTIDADES_OK), FAKE_SEND_FAIL='1'))
        llamadas = self.llamadas()
        self.assertTrue(llamadas)
        for servicio, operacion in llamadas:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertIn((servicio, operacion), PERMITIDAS)
                self.assertNotIn(servicio, ('s3', 's3api'))
                self.assertFalse(re.match(r'(put|create|update|delete|set|tag|untag|verify)-', operacion))


class SmokeSesEstaticoTest(unittest.TestCase):
    def setUp(self):
        with open(SMOKE, encoding='utf-8') as f:
            self.texto = f.read()

    def test_es_ejecutable(self):
        self.assertTrue(os.access(SMOKE, os.X_OK))

    def test_la_guarda_de_allow_va_antes_de_cualquier_llamada_a_aws(self):
        self.assertLess(self.texto.index('ALLOW_SES_SEND'), self.texto.index('"$AWS" '))

    def test_un_unico_send_email_y_ninguna_escritura(self):
        self.assertEqual(self.texto.count('"$AWS" ses send-email'), 1)
        llamadas = re.findall(r'"\$AWS" (\S+) (\S+)', self.texto)
        self.assertTrue(llamadas)
        for servicio, operacion in llamadas:
            with self.subTest(op=f'{servicio} {operacion}'):
                self.assertIn((servicio, operacion), PERMITIDAS)
                self.assertFalse(re.match(r'(put|create|update|delete|set|tag|untag|verify)-', operacion))


if __name__ == '__main__':
    unittest.main()
