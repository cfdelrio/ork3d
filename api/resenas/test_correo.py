"""Tests de correo (V2): aviso de reseña nueva, correo de acceso, cliente SES y hook de la Lambda
pública. Sin SES real: FakeSES y un cliente boto3 falso que registra la llamada.

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import datetime as dt
import hashlib
import json
import unittest

import app
from admin_fixtures import BASE, CONTACTO, DESTINO, FEEDBACK, INICIO, ORIGEN, PEDIDO, PRIVADO, REMITENTE
from fakes import FakeS3, FakeSES
from nucleo import correo as _correo
from nucleo.almacen import Almacen

RID = 'c' * 32
TOKEN = 'Qx7_mLp2vR9tZkW4aB8cD1eF3gH5jK6n'
TH = hashlib.sha256(TOKEN.encode('utf-8')).hexdigest()


def resena(**cambios):
    base = {'id': RID, 'rating': 4, 'comment': 'Muy buena compra, llegó a tiempo.',
            'display_name': 'Ana G.', 'product_label': 'Trofeo acrílico', 'category': 'trofeos',
            'photo_key': None, 'order_ref': PEDIDO, 'private_feedback': FEEDBACK,
            'contact': CONTACTO, 'contact_requested': True, 'token_hash': TH, 'status': 'pending'}
    base.update(cambios)
    return base


class EstrellasYAsuntoTest(unittest.TestCase):
    def test_asunto_exacto_con_cinco_estrellas(self):
        self.assertEqual(_correo.asunto_resena(5), 'Nueva reseña ORK3D · ★★★★★')

    def test_estrellas_segun_el_rating(self):
        self.assertEqual(_correo.asunto_resena(4), 'Nueva reseña ORK3D · ★★★★☆')
        self.assertEqual(_correo.estrellas(1), '★☆☆☆☆')

    def test_rating_fuera_de_rango_se_acota(self):
        self.assertEqual(_correo.estrellas(0), '★☆☆☆☆')
        self.assertEqual(_correo.estrellas(9), '★★★★★')


class ContenidoAvisoTest(unittest.TestCase):
    def aviso(self, **cambios):
        return _correo.construir_resena(resena(**cambios), BASE, REMITENTE, DESTINO)

    def test_destinatario_y_remitente_vienen_del_argumento(self):
        c = self.aviso()
        self.assertEqual((c.origen, c.destino), (REMITENTE, DESTINO))

    def test_solo_contiene_la_allowlist(self):
        c = self.aviso()
        for texto in [c.texto, c.html]:
            for prohibido in [PEDIDO, FEEDBACK, CONTACTO, TH, TOKEN, 'tokens/', 'pendientes/', 'order_ref',
                              'contact_requested', 'private_feedback', 'amazonaws', 'arn:']:
                self.assertNotIn(prohibido, texto)

    def test_incluye_datos_permitidos_y_enlace_al_panel(self):
        c = self.aviso()
        for texto in [c.texto, c.html]:
            self.assertIn('Ana G.', texto)
            self.assertIn('Trofeo acrílico', texto)
            self.assertIn('trofeos', texto)
            self.assertIn(f'{BASE}/admin/resenas/{RID}', texto)
        self.assertIn('Tiene foto: no', c.texto)
        self.assertIn('Tiene foto: sí', self.aviso(photo_key='pendientes/x/y.jpg').texto)

    def test_comentario_largo_se_trunca_a_280_caracteres(self):
        largo = ' '.join(['palabra'] * 200)
        c = self.aviso(comment=largo)
        cita = c.texto.split('“', 1)[1].split('”', 1)[0]
        self.assertLessEqual(len(cita), 280)
        self.assertTrue(cita.endswith('…'))

    def test_sin_comentario_muestra_marcador(self):
        self.assertIn('(sin comentario)', self.aviso(comment='   ').texto)

    def test_html_escapa_el_contenido_del_cliente(self):
        c = self.aviso(display_name='<img src=x onerror=alert(1)>', comment='<script>alert(1)</script>')
        self.assertNotIn('<img src=x', c.html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', c.html)
        self.assertNotIn('<script>', c.html)
        self.assertIn('&lt;script&gt;', c.html)

    def test_dos_versiones_texto_y_html_y_noindex(self):
        c = self.aviso()
        self.assertIsInstance(c.texto, str)
        self.assertTrue(c.html.startswith('<!doctype html>'))
        self.assertIn('noindex, nofollow', c.html)

    def test_asunto_del_aviso(self):
        self.assertEqual(self.aviso().asunto, 'Nueva reseña ORK3D · ★★★★☆')


class CorreoDeAccesoTest(unittest.TestCase):
    ENLACE = f'{BASE}/admin/entrar#c={"a" * 43}'

    def test_asunto_exacto_y_plazo(self):
        c = _correo.construir_acceso(self.ENLACE, REMITENTE, DESTINO)
        self.assertEqual(c.asunto, 'Acceso al panel de reseñas ORK3D')
        self.assertIn('10 minutos', c.texto)
        self.assertIn('10 minutos', c.html)

    def test_enlace_con_fragmento_en_texto_y_html(self):
        c = _correo.construir_acceso(self.ENLACE, REMITENTE, DESTINO)
        self.assertIn(self.ENLACE, c.texto)
        self.assertIn(f'href="{self.ENLACE}"', c.html)

    def test_sin_datos_del_sistema(self):
        c = _correo.construir_acceso(self.ENLACE, REMITENTE, DESTINO)
        for texto in [c.texto, c.html]:
            for prohibido in ['tokens/', 'resenas/', 'admin/sesiones', 'amazonaws', PEDIDO]:
                self.assertNotIn(prohibido, texto)


class ClienteSesTest(unittest.TestCase):
    class ClienteFalso:
        def __init__(self):
            self.llamadas = []

        def send_email(self, **kwargs):
            self.llamadas.append(kwargs)
            return {'MessageId': 'mensaje-1'}

    def test_send_email_usa_remitente_y_destinatario_configurados(self):
        cliente = self.ClienteFalso()
        correo = _correo.construir_resena(resena(), BASE, REMITENTE, DESTINO)
        identificador = _correo.SESCorreo(cliente).enviar(correo)
        self.assertEqual(identificador, 'mensaje-1')
        (llamada,) = cliente.llamadas
        self.assertEqual(llamada['Source'], REMITENTE)
        self.assertEqual(llamada['Destination'], {'ToAddresses': [DESTINO]})
        self.assertEqual(llamada['Message']['Subject'], {'Data': correo.asunto, 'Charset': 'UTF-8'})
        self.assertEqual(llamada['Message']['Body']['Text']['Data'], correo.texto)
        self.assertEqual(llamada['Message']['Body']['Html']['Charset'], 'UTF-8')


class NotificarTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)
        self.ses = FakeSES()

    def notificar(self, **cambios):
        return _correo.notificar_resena(self.ses, self.almacen, PRIVADO, resena(**cambios), base_url=BASE,
                                        origen=REMITENTE, destino=DESTINO, ahora=INICIO)

    def test_envio_exitoso_registra_la_notificacion(self):
        self.assertTrue(self.notificar())
        self.assertEqual(len(self.ses.enviados), 1)
        registro = json.loads(self.s3.objects[f'notificaciones/{RID}.json'])
        self.assertEqual(registro, {'v': 1, 'enviado': '2026-10-21T12:00:00Z'})

    def test_fallo_de_ses_devuelve_false_sin_registro_y_sin_levantar(self):
        self.ses.fallar_proximo()
        self.assertFalse(self.notificar())
        self.assertNotIn(f'notificaciones/{RID}.json', self.s3.objects)
        self.assertEqual(self.ses.enviados, [])

    def test_plantilla_que_falla_tampoco_levanta(self):
        self.assertFalse(_correo.notificar_resena(self.ses, self.almacen, PRIVADO, {'id': RID}, base_url=BASE,
                                                  origen=REMITENTE, destino=DESTINO, ahora=INICIO))

    def test_registro_existente_no_impide_otro_envio_explicito(self):
        self.assertTrue(self.notificar())
        self.assertTrue(self.notificar())
        self.assertEqual(len(self.ses.enviados), 2)


class HookPublicoTest(unittest.TestCase):
    """La Lambda pública avisa después de guardar la reseña pending. Nunca cambia su respuesta."""

    def setUp(self):
        self.s3 = FakeS3()
        self.ses = FakeSES()
        self.s3.objects[f'tokens/{TH}.json'] = json.dumps({
            'v': 1, 'created_at': '2026-10-20T10:00:00Z',
            'expires_at': (INICIO + dt.timedelta(days=60)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'revoked': False, 'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
            'category': 'trofeos', 'verified': True, 'order_ref': PEDIDO,
        }).encode('utf-8')
        self.svc = self.servicio(correo=self.ses)

    def servicio(self, **kwargs):
        return app.Service(self.s3, PRIVADO, ORIGEN, base_url=BASE, origen_correo=REMITENTE,
                           destino_correo=DESTINO, **kwargs)

    def payload(self, **cambios):
        p = {'token': TOKEN, 'rating': 4, 'comment': 'Muy buena compra, llegó a tiempo.',
             'aspects': ['calidad'], 'display_mode': 'nombre', 'name': 'Ana Gómez',
             'permission_to_publish': True, 'private_feedback': FEEDBACK,
             'contact_requested': True, 'contact': CONTACTO, 'website': ''}
        p.update(cambios)
        return p

    def test_resena_nueva_envia_el_aviso_y_registra_la_notificacion(self):
        status, body = self.svc.create_review(self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(len(self.ses.enviados), 1)
        self.assertEqual(self.ses.enviados[0]['to'], DESTINO)
        self.assertIn(f"notificaciones/{body['id']}.json", self.s3.objects)

    def test_aviso_no_trae_datos_privados_ni_del_token(self):
        self.svc.create_review(self.payload())
        correo = self.ses.enviados[0]
        texto = correo['text'] + correo['html']
        for prohibido in [FEEDBACK, CONTACTO, TOKEN, TH, PEDIDO, 'Qx7_']:
            self.assertNotIn(prohibido, texto)

    def test_fallo_de_ses_no_revierte_ni_la_reseña_ni_el_claim(self):
        self.ses.fallar_proximo()
        status, body = self.svc.create_review(self.payload())
        self.assertEqual(status, 200)
        self.assertIn(f"resenas/{body['id']}.json", self.s3.objects)
        self.assertIn(f'claims/{TH}.json', self.s3.objects)
        self.assertNotIn(f"notificaciones/{body['id']}.json", self.s3.objects)

    def test_fallo_de_ses_deja_la_reseña_sin_registro_para_mostrar_pendiente(self):
        self.ses.fallar_proximo()
        status, body = self.svc.create_review(self.payload())
        revisada = json.loads(self.s3.objects[f"resenas/{body['id']}.json"])
        self.assertEqual(revisada['status'], 'pending')

    def test_sin_configuracion_de_correo_no_envia_nada(self):
        sin_correo = app.Service(self.s3, PRIVADO, ORIGEN)
        status, _ = sin_correo.create_review(self.payload())
        self.assertEqual(status, 200)
        self.assertEqual(self.ses.enviados, [])

    def test_configuracion_incompleta_no_envia_nada(self):
        incompleto = app.Service(self.s3, PRIVADO, ORIGEN, correo=self.ses, base_url=BASE)
        self.assertFalse(incompleto.notificar)
        incompleto.create_review(self.payload())
        self.assertEqual(self.ses.enviados, [])


if __name__ == '__main__':
    unittest.main()
