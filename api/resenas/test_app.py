"""Tests de api/resenas/app.py. Solo stdlib y el fake de S3 (sin boto3 ni red).

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import base64
import datetime as dt
import json
import unittest

import app
from fakes import FakeClientError, FakeS3, jpeg, segmento

TOKEN = 'Qx7_mLp2vR9tZkW4aB8cD1eF3gH5jK6n'
OTRO_TOKEN = 'Zz0_' + 'a' * 28
JPEG = jpeg()
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 20_000


def _ahora():
    return dt.datetime.now(dt.timezone.utc)


def _iso(d):
    return d.strftime('%Y-%m-%dT%H:%M:%SZ')


def registro_token(**cambios):
    """Registro como lo escribe el CLI de admin en tokens/<hash>.json."""
    reg = {
        'v': 1,
        'created_at': _iso(_ahora() - dt.timedelta(days=1)),
        'expires_at': _iso(_ahora() + dt.timedelta(days=60)),
        'revoked': False,
        'product_slug': 'trofeo-acrilico',
        'product_label': 'Trofeo acrílico',
        'category': 'trofeos',
        'verified': True,
    }
    reg.update(cambios)
    return reg


def payload(**cambios):
    """Formulario válido de reseña; cada test cambia solo lo que prueba."""
    p = {
        'token': TOKEN,
        'rating': 5,
        'comment': 'Llegó a tiempo y se ve muy bien.',
        'aspects': ['calidad', 'diseno'],
        'display_mode': 'inicial',
        'name': 'Ana Gómez',
        'permission_to_publish': True,
        'private_feedback': '',
        'contact_requested': False,
        'website': '',
    }
    p.update(cambios)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.svc = app.Service(self.s3, 'bucket-test', 'https://ork3d.com',
                               google_review_url='https://g.page/r/ejemplo/review')
        self.sembrar_token()

    def sembrar_token(self, token=TOKEN, **cambios):
        clave = f'tokens/{app.token_hash(token)}.json'
        self.s3.objects[clave] = json.dumps(registro_token(**cambios)).encode('utf-8')

    def sembrar_claim(self, token=TOKEN):
        self.s3.objects[f'claims/{app.token_hash(token)}.json'] = b'{}'

    def estado(self, token=TOKEN):
        return self.svc.token_state(token)[1]

    def resenas(self):
        return sorted(k for k in self.s3.objects if k.startswith('resenas/'))

    def resena_guardada(self):
        (clave,) = self.resenas()
        return json.loads(self.s3.objects[clave])

    def subir(self, cuerpo, ext='jpg', token=TOKEN):
        status, firma = self.svc.presign({'token': token, 'ext': ext})
        self.assertEqual(status, 200)
        self.s3.browser_post(firma['key'], cuerpo, app.PHOTO_EXT[ext])
        return firma['key']


class EstadoDelToken(Base):
    def test_token_ok_devuelve_datos_del_producto(self):
        vence = json.loads(self.s3.objects[f'tokens/{app.token_hash(TOKEN)}.json'])['expires_at'][:10]
        self.assertEqual(self.svc.get_token(TOKEN),
                         (200, {'ok': True, 'producto': 'Trofeo acrílico', 'categoria': 'trofeos', 'vence': vence}))

    def test_formato_invalido_no_consulta_s3(self):
        for token in ['', 'corto', 'x' * 33, 'con espacios' + 'a' * 20, None, 123, ['x']]:
            with self.subTest(token=token):
                self.assertEqual(self.estado(token), 'invalido')
        self.assertEqual(self.s3.calls, [])

    def test_salto_de_linea_al_final_no_es_un_token_valido(self):
        self.assertEqual(self.estado(TOKEN + '\n'), 'invalido')
        self.assertEqual(self.s3.calls, [])

    def test_token_desconocido(self):
        self.assertEqual(self.estado(OTRO_TOKEN), 'invalido')

    def test_token_vencido(self):
        self.sembrar_token(expires_at=_iso(_ahora() - dt.timedelta(seconds=1)))
        self.assertEqual(self.estado(), 'vencido')

    def test_token_revocado_aunque_no_haya_vencido(self):
        self.sembrar_token(revoked=True)
        self.assertEqual(self.estado(), 'revocado')

    def test_token_usado(self):
        self.sembrar_claim()
        self.assertEqual(self.estado(), 'usado')
        self.assertEqual(self.svc.get_token(TOKEN), (200, {'ok': False, 'motivo': 'usado'}))


class Presign(Base):
    def test_devuelve_clave_y_politica_acotada(self):
        status, body = self.svc.presign({'token': TOKEN, 'ext': 'jpg'})
        self.assertEqual(status, 200)
        h = app.token_hash(TOKEN)
        self.assertRegex(body['key'], rf'^pendientes/{h}/[0-9a-f]{{32}}\.jpg$')
        firma = self.s3.presigned[body['key']]
        self.assertEqual(firma['expires_in'], 600)
        self.assertEqual(body['max_bytes'], 2_000_000)
        self.assertIn(['content-length-range', 15_000, 2_000_000], firma['conditions'])
        self.assertIn({'Content-Type': 'image/jpeg'}, firma['conditions'])
        self.assertIn({'key': body['key']}, firma['conditions'])

    def test_no_consume_el_token(self):
        self.svc.presign({'token': TOKEN, 'ext': 'jpg'})
        self.assertEqual(self.estado(), 'ok')

    def test_extension_no_permitida(self):
        for ext in ['gif', 'JPG', 'png', 'webp', '', None, ['jpg'], {'a': 1}]:
            with self.subTest(ext=ext):
                status, body = self.svc.presign({'token': TOKEN, 'ext': ext})
                self.assertEqual((status, body['error']), (400, 'formato'))
        self.assertEqual(self.s3.presigned, {})

    def test_token_no_utilizable_no_firma(self):
        self.sembrar_claim()
        status, body = self.svc.presign({'token': TOKEN, 'ext': 'jpg'})
        self.assertEqual((status, body), (403, {'ok': False, 'motivo': 'usado'}))
        self.assertEqual(self.s3.presigned, {})

    def test_la_politica_firmada_limita_tamano_y_tipo(self):
        _, firma = self.svc.presign({'token': TOKEN, 'ext': 'jpg'})
        clave = firma['key']
        with self.assertRaises(FakeClientError):
            self.s3.browser_post(clave, b'x' * 14_999, 'image/jpeg')
        with self.assertRaises(FakeClientError):
            self.s3.browser_post(clave, b'x' * 2_000_001, 'image/jpeg')
        with self.assertRaises(FakeClientError):
            self.s3.browser_post(clave, JPEG, 'image/png')
        self.assertNotIn(clave, self.s3.objects)
        self.s3.browser_post(clave, JPEG, 'image/jpeg')
        self.assertIn(clave, self.s3.objects)


class CrearResena(Base):
    def test_resena_valida_queda_pendiente_y_consume_el_token(self):
        status, body = self.svc.create_review(payload())
        self.assertEqual(status, 200)
        self.assertEqual(body, {'ok': True, 'id': body['id'], 'google_url': 'https://g.page/r/ejemplo/review'})
        r = self.resena_guardada()
        self.assertEqual(r['status'], 'pending')
        self.assertTrue(r['permission_to_publish'])
        self.assertEqual(r['display_name'], 'Ana G.')
        self.assertEqual(r['product_label'], 'Trofeo acrílico')
        self.assertTrue(r['verified'])
        self.assertEqual(self.estado(), 'usado')

    def test_el_nombre_real_no_se_guarda(self):
        self.svc.create_review(payload(name='Ana Gómez'))
        r = self.resena_guardada()
        self.assertNotIn('name', r)
        self.assertNotIn('Gómez', json.dumps(r))

    def test_el_token_no_aparece_en_ninguna_clave_ni_objeto(self):
        self.svc.create_review(payload())
        for clave, datos in self.s3.objects.items():
            self.assertNotIn(TOKEN, clave)
            self.assertNotIn(TOKEN.encode('utf-8'), datos)

    def test_segundo_envio_con_el_mismo_token_se_rechaza(self):
        self.svc.create_review(payload())
        status, body = self.svc.create_review(payload(comment='otra vez'))
        self.assertEqual((status, body), (409, {'ok': False, 'motivo': 'usado'}))
        self.assertEqual(len(self.resenas()), 1)

    def test_dos_envios_que_pasan_el_chequeo_a_la_vez_solo_uno_gana(self):
        # Carrera: el otro envío escribió la marca de uso después de que este leyera el token libre.
        self.sembrar_claim()
        self.svc._exists = lambda clave: False
        status, body = self.svc.create_review(payload())
        self.assertEqual((status, body), (409, {'ok': False, 'motivo': 'usado'}))
        self.assertEqual(self.resenas(), [])

    def test_token_vencido_o_revocado_no_crea_resena(self):
        casos = [
            (dict(expires_at=_iso(_ahora() - dt.timedelta(seconds=5))), 'vencido'),
            (dict(revoked=True), 'revocado'),
        ]
        for cambios, motivo in casos:
            with self.subTest(motivo=motivo):
                self.sembrar_token(**cambios)
                status, body = self.svc.create_review(payload())
                self.assertEqual((status, body), (403, {'ok': False, 'motivo': motivo}))
        self.assertEqual(self.resenas(), [])

    def test_token_inexistente_no_crea_resena(self):
        status, body = self.svc.create_review(payload(token=OTRO_TOKEN))
        self.assertEqual((status, body), (403, {'ok': False, 'motivo': 'invalido'}))

    def test_sin_permiso_para_publicar_no_guarda_ni_consume(self):
        status, body = self.svc.create_review(payload(permission_to_publish=False))
        self.assertEqual(status, 400)
        self.assertIn('permission_to_publish', body['campos'])
        self.assertEqual(self.resenas(), [])
        self.assertEqual(self.estado(), 'ok')

    def test_rating_fuera_de_rango_o_mal_tipado(self):
        for valor in [0, 6, -1, True, None, 'cinco', 4.0, [4], '9' * 5000]:
            with self.subTest(rating=valor):
                status, body = self.svc.create_review(payload(rating=valor))
                self.assertEqual(status, 400)
                self.assertIn('rating', body['campos'])

    def test_rating_numerico_en_texto_se_acepta(self):
        status, _ = self.svc.create_review(payload(rating='4'))
        self.assertEqual(status, 200)
        self.assertEqual(self.resena_guardada()['rating'], 4)

    def test_calificacion_baja_se_guarda_y_muestra_google(self):
        # Reseñas negativas no se filtran: la reseña entra igual y el CTA de Google no depende de la nota.
        status, body = self.svc.create_review(payload(rating=1, comment='Llegó roto.'))
        self.assertEqual(status, 200)
        self.assertEqual(body['google_url'], 'https://g.page/r/ejemplo/review')
        self.assertEqual(self.resena_guardada()['rating'], 1)

    def test_comentario_y_feedback_privado_con_limite(self):
        status, body = self.svc.create_review(payload(comment='x' * 601, private_feedback='y' * 801))
        self.assertEqual(status, 400)
        self.assertEqual(set(body['campos']), {'comment', 'private_feedback'})

    def test_caracteres_de_control_se_quitan(self):
        self.svc.create_review(payload(comment='Hola\x00\x07 mundo\r\nchau'))
        self.assertEqual(self.resena_guardada()['comment'], 'Hola mundo\nchau')

    def test_aspectos_se_normalizan_y_se_validan(self):
        self.svc.create_review(payload(aspects=['calidad', 'diseno', 'calidad']))
        self.assertEqual(self.resena_guardada()['aspects'], ['calidad', 'diseno'])

    def test_aspectos_invalidos(self):
        for valor in ['calidad', ['precio'], [['calidad']], [1], list('abcdefg')]:
            with self.subTest(aspects=valor):
                status, body = self.svc.create_review(payload(aspects=valor))
                self.assertEqual(status, 400)
                self.assertIn('aspects', body['campos'])
        self.assertEqual(self.estado(), 'ok')

    def test_modos_de_nombre_mostrado(self):
        casos = [
            ('inicial', 'Ana Gómez', 'Ana G.'),
            ('inicial', 'Ana', 'Ana'),
            ('inicial', 'María José de la Cruz', 'María C.'),
            ('inicial', 'Juan .', 'Juan'),
            ('nombre', 'Ana Gómez', 'Ana'),
            ('anonimo', '', app.ANON_NAME),
            ('anonimo', 'Ana Gómez', app.ANON_NAME),
        ]
        for modo, nombre, esperado in casos:
            with self.subTest(modo=modo, nombre=nombre):
                self.s3.objects.clear()
                self.sembrar_token()
                status, _ = self.svc.create_review(payload(display_mode=modo, name=nombre))
                self.assertEqual(status, 200)
                self.assertEqual(self.resena_guardada()['display_name'], esperado)

    def test_modo_de_nombre_invalido(self):
        for modo in ['publico', [], None, 1]:
            with self.subTest(modo=modo):
                status, body = self.svc.create_review(payload(display_mode=modo))
                self.assertEqual(status, 400)
                self.assertIn('display_mode', body['campos'])

    def test_nombre_invalido_en_modos_con_nombre(self):
        for nombre in ['', 'J', 'Ana123', '<script>', 'x' * 41]:
            with self.subTest(nombre=nombre):
                status, body = self.svc.create_review(payload(name=nombre))
                self.assertEqual(status, 400)
                self.assertIn('name', body['campos'])

    def test_contacto_obligatorio_si_lo_pide(self):
        status, body = self.svc.create_review(payload(contact_requested=True, contact=''))
        self.assertEqual(status, 400)
        self.assertIn('contact', body['campos'])
        status, _ = self.svc.create_review(payload(contact_requested=True, contact='ana@example.com'))
        self.assertEqual(status, 200)
        self.assertEqual(self.resena_guardada()['contact'], 'ana@example.com')

    def test_sin_pedido_de_contacto_no_se_guarda_contacto(self):
        self.svc.create_review(payload(contact_requested=False, contact='ana@example.com'))
        r = self.resena_guardada()
        self.assertFalse(r['contact_requested'])
        self.assertIsNone(r['contact'])


class Honeypot(Base):
    def test_responde_202_y_no_guarda_nada(self):
        status, body = self.svc.create_review(payload(website='http://spam.example'))
        self.assertEqual((status, body), (202, {'ok': True}))
        self.assertEqual(self.resenas(), [])
        self.assertEqual(self.estado(), 'ok')

    def test_gana_aunque_haya_otros_errores(self):
        status, _ = self.svc.create_review(payload(website='x', rating=9, permission_to_publish=False))
        self.assertEqual(status, 202)


class FotoDeLaResena(Base):
    def test_foto_valida_se_adjunta_y_queda_en_pendientes(self):
        clave = self.subir(JPEG)
        status, _ = self.svc.create_review(payload(photo_key=clave))
        self.assertEqual(status, 200)
        r = self.resena_guardada()
        self.assertEqual(r['photo_key'], clave)
        self.assertIsNone(r['photo_public_key'])
        self.assertIn(clave, self.s3.objects)

    def test_la_foto_de_otro_token_no_se_acepta(self):
        clave = self.subir(JPEG)
        self.sembrar_token(OTRO_TOKEN)
        status, body = self.svc.create_review(payload(token=OTRO_TOKEN, photo_key=clave))
        self.assertEqual((status, body['campos']), (400, {'photo_key': 'Foto inválida.'}))
        self.assertEqual(self.estado(OTRO_TOKEN), 'ok')

    def test_clave_con_formato_raro_no_se_acepta(self):
        h = app.token_hash(TOKEN)
        claves = [
            f'pendientes/{h}/{"a" * 32}.gif',
            f'pendientes/{h}/{"a" * 32}.jpg\n',
            f'pendientes/{h}/nombre.jpg',
            f'resenas/{h}/{"a" * 32}.jpg',
            f'../tokens/{h}.json',
        ]
        for clave in claves:
            with self.subTest(clave=clave):
                status, body = self.svc.create_review(payload(photo_key=clave))
                self.assertEqual((status, body['campos']), (400, {'photo_key': 'Foto inválida.'}))
        self.assertEqual(self.estado(), 'ok')

    def test_foto_no_subida(self):
        clave = f'pendientes/{app.token_hash(TOKEN)}/{"a" * 32}.jpg'
        status, body = self.svc.create_review(payload(photo_key=clave))
        self.assertEqual((status, body['campos']), (400, {'photo_key': 'La foto no llegó. Probá de nuevo.'}))
        self.assertEqual(self.estado(), 'ok')

    def test_objeto_vacio_se_rechaza_antes_de_leer_su_cabecera(self):
        clave = f'pendientes/{app.token_hash(TOKEN)}/{"e" * 32}.jpg'
        self.s3.objects[clave] = b''
        status, body = self.svc.create_review(payload(photo_key=clave))
        self.assertEqual((status, body['campos']), (400, {'photo_key': 'La foto tiene un tamaño no permitido.'}))
        self.assertNotIn(('get_object', clave), self.s3.calls)

    def test_tamano_fuera_de_rango_se_rechaza_en_el_servidor(self):
        # Defensa en profundidad: aunque la política firmada fallara, el servidor vuelve a validar.
        h = app.token_hash(TOKEN)
        for tam in [14_999, 2_000_001]:
            with self.subTest(tam=tam):
                clave = f'pendientes/{h}/{"b" * 32}.jpg'
                self.s3.objects[clave] = JPEG[:4] + b'\x00' * (tam - 4)
                status, body = self.svc.create_review(payload(photo_key=clave))
                self.assertEqual((status, body['campos']),
                                 (400, {'photo_key': 'La foto tiene un tamaño no permitido.'}))
        self.assertEqual(self.estado(), 'ok')

    def test_el_contenido_debe_ser_la_imagen_declarada(self):
        h = app.token_hash(TOKEN)
        falsas = [
            ('jpg', PNG),
            ('jpg', b'<script>alert(1)</script>' + b' ' * 20_000),
            ('jpg', JPEG + b'<script>alert(1)</script>'),  # datos después del EOI
        ]
        for ext, datos in falsas:
            with self.subTest(ext=ext):
                clave = f'pendientes/{h}/{"d" * 32}.{ext}'
                self.s3.objects[clave] = datos
                status, body = self.svc.create_review(payload(photo_key=clave))
                self.assertEqual((status, body['campos']),
                                 (400, {'photo_key': 'La foto no es un JPEG válido o trae metadatos (EXIF, comentarios). Quitalos y volvé a subirla.'}))
        self.assertEqual(self.estado(), 'ok')

    def test_jpeg_con_metadatos_se_rechaza_y_no_consume_el_token(self):
        casos = {
            'EXIF con GPS': jpeg(segmentos_extra=segmento(0xE1, b'Exif\x00\x00GPS-SECRETO')),
            'XMP': jpeg(segmentos_extra=segmento(0xE1, b'http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta/>')),
            'IPTC': jpeg(segmentos_extra=segmento(0xED, b'Photoshop 3.0\x00')),
            'comentario': jpeg(segmentos_extra=segmento(0xFE, b'autor: Ana')),
        }
        for nombre, datos in casos.items():
            with self.subTest(metadato=nombre):
                clave = self.subir(datos)
                status, body = self.svc.create_review(payload(photo_key=clave))
                self.assertEqual((status, body['campos']),
                                 (400, {'photo_key': 'La foto no es un JPEG válido o trae metadatos (EXIF, comentarios). Quitalos y volvé a subirla.'}))
        self.assertEqual(self.estado(), 'ok')
        self.assertEqual(self.resenas(), [])


class JpegSinMetadatos(unittest.TestCase):
    def test_acepta_perfil_de_color_icc_que_agrega_el_navegador(self):
        icc = segmento(0xE2, b'ICC_PROFILE\x00\x01\x01' + b'\x00' * 472)
        self.assertTrue(app.jpeg_sin_metadatos(jpeg(segmentos_extra=icc)))
        self.assertFalse(app.jpeg_sin_metadatos(jpeg(segmentos_extra=segmento(0xE2, b'FPXR\x00' + b'\x00' * 20))))

    def test_acepta_jpeg_limpio_incluido_multiples_barridos(self):
        self.assertTrue(app.jpeg_sin_metadatos(JPEG))
        self.assertTrue(app.jpeg_sin_metadatos(jpeg(entropia=b'\xff\x00' * 9_000)))          # byte stuffing
        self.assertTrue(app.jpeg_sin_metadatos(jpeg(entropia=b'x' * 8_000 + b'\xff\xd0' + b'y' * 8_000)))  # RST
        progresivo = b'x' * 8_000 + segmento(0xC4, b'\x00' * 20) + segmento(0xDA, b'\x01\x01\x00\x00\x3f\x00') + b'y' * 8_000
        self.assertTrue(app.jpeg_sin_metadatos(jpeg(entropia=progresivo)))                       # varios barridos

    def test_rechaza_metadatos_en_cualquier_segmento(self):
        casos = {
            'EXIF': jpeg(segmentos_extra=segmento(0xE1, b'Exif\x00\x00GPS')),
            'XMP': jpeg(segmentos_extra=segmento(0xE1, b'http://ns.adobe.com/xap/1.0/\x00')),
            'IPTC': jpeg(segmentos_extra=segmento(0xED, b'Photoshop 3.0\x00')),
            'COM': jpeg(segmentos_extra=segmento(0xFE, b'hola')),
            'APP0 que no es JFIF': jpeg(app0=b'Exif\x00\x00'),
            'metadato después de un barrido': jpeg(entropia=b'x' * 8_000 + segmento(0xE1, b'Exif\x00\x00') + b'y' * 8_000),
        }
        for nombre, datos in casos.items():
            with self.subTest(caso=nombre):
                self.assertFalse(app.jpeg_sin_metadatos(datos))

    def test_rechaza_lo_que_no_es_un_jpeg_completo(self):
        casos = {
            'vacío': b'',
            'PNG': PNG,
            'sin SOI': b'\x00' + JPEG[1:],
            'sin EOI': JPEG[:-2],
            'truncado': JPEG[:len(JPEG) // 2],
            'datos después del EOI': JPEG + b'extra',
            'longitud inválida': b'\xff\xd8\xff\xe0\x00\x01',
        }
        for nombre, datos in casos.items():
            with self.subTest(caso=nombre):
                self.assertFalse(app.jpeg_sin_metadatos(datos))


class Compensacion(Base):
    def test_si_falla_guardar_la_resena_el_token_queda_libre(self):
        self.s3.fail_on('put_object', RuntimeError('disco'), prefix='resenas/')
        with self.assertRaises(RuntimeError):
            self.svc.create_review(payload())
        self.assertEqual(self.estado(), 'ok')
        status, _ = self.svc.create_review(payload())
        self.assertEqual(status, 200)

    def test_si_falla_tambien_la_compensacion_se_registra_y_gana_el_error_original(self):
        self.s3.fail_on('put_object', RuntimeError('disco'), prefix='resenas/')
        self.s3.fail_on('delete_object', RuntimeError('otra falla'), prefix='claims/')
        with self.assertLogs('resenas', 'INFO') as cm:
            with self.assertRaises(RuntimeError) as ctx:
                self.svc.create_review(payload())
        self.assertEqual(str(ctx.exception), 'disco')
        self.assertIn('claim_rollback_fallido', '\n'.join(cm.output))


class Rutas(Base):
    def test_body_mayor_al_limite_da_413_sin_parsear(self):
        raw = '{"a":"' + 'x' * app.MAX_BODY_BYTES + '"}'
        status, body = self.svc.route('POST', '/resenas', raw)
        self.assertEqual((status, body['error']), (413, 'demasiado_grande'))

    def test_json_invalido_o_anidado_al_extremo_da_400(self):
        for raw in ['{', 'no es json', '[' * 3000 + ']' * 3000, '[1,2]']:
            with self.subTest(raw=raw[:10]):
                status, body = self.svc.route('POST', '/presign', raw)
                self.assertEqual((status, body['error']), (400, 'json'))

    def test_metodos_y_rutas_no_definidas_dan_404(self):
        self.assertEqual(self.svc.route('DELETE', '/resenas', '')[0], 404)
        self.assertEqual(self.svc.route('GET', '/resenas', '')[0], 404)
        self.assertEqual(self.svc.route('GET', '/', '')[0], 404)

    def test_options_responde_204_sin_cuerpo(self):
        self.assertEqual(self.svc.route('OPTIONS', '/resenas', ''), (204, None))

    def test_cabeceras_de_respuesta(self):
        r = self.svc.response(200, {'ok': True})
        self.assertEqual(r['headers']['Cache-Control'], 'no-store')
        self.assertEqual(r['headers']['Access-Control-Allow-Origin'], 'https://ork3d.com')
        self.assertEqual(r['headers']['Vary'], 'Origin')
        self.assertEqual(json.loads(r['body']), {'ok': True})
        self.assertEqual(self.svc.response(204, None)['body'], '')


class Handler(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.svc = app.Service(self.s3, 'bucket-test', 'https://ork3d.com')
        self.s3.objects[f'tokens/{app.token_hash(TOKEN)}.json'] = json.dumps(registro_token()).encode('utf-8')
        app._SVC = self.svc
        self.addCleanup(setattr, app, '_SVC', None)

    def llamar(self, metodo, ruta, cuerpo=None, codificado=False):
        evento = {'requestContext': {'http': {'method': metodo}}, 'rawPath': ruta}
        if cuerpo is not None:
            if codificado:
                evento['body'] = base64.b64encode(cuerpo.encode('utf-8')).decode('ascii')
                evento['isBase64Encoded'] = True
            else:
                evento['body'] = cuerpo
        r = app.handler(evento)
        return r['statusCode'], r['headers'], (json.loads(r['body']) if r['body'] else None)

    def test_get_token(self):
        status, cab, body = self.llamar('GET', f'/tokens/{TOKEN}')
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(cab['Cache-Control'], 'no-store')
        self.assertEqual(cab['Access-Control-Allow-Origin'], 'https://ork3d.com')

    def test_preflight(self):
        status, cab, body = self.llamar('OPTIONS', '/resenas')
        self.assertEqual((status, body), (204, None))
        self.assertIn('POST', cab['Access-Control-Allow-Methods'])

    def test_barra_final_en_la_ruta(self):
        status, _, _ = self.llamar('POST', '/presign/', json.dumps({'token': TOKEN, 'ext': 'jpg'}))
        self.assertEqual(status, 200)

    def test_cuerpo_en_base64(self):
        status, _, _ = self.llamar('POST', '/presign', json.dumps({'token': TOKEN, 'ext': 'jpg'}), codificado=True)
        self.assertEqual(status, 200)

    def test_base64_o_utf8_invalidos_dan_400(self):
        casos = [
            {'body': 'abc', 'isBase64Encoded': True},
            {'body': base64.b64encode(b'\xff\xfe').decode('ascii'), 'isBase64Encoded': True},
        ]
        for extra in casos:
            with self.subTest(body=extra['body']):
                evento = {'requestContext': {'http': {'method': 'POST'}}, 'rawPath': '/resenas', **extra}
                self.assertEqual(app.handler(evento)['statusCode'], 400)

    def test_rutas_inexistentes_dan_404(self):
        for metodo, ruta in [('GET', '/tokens/'), ('GET', '/'), ('DELETE', '/resenas')]:
            with self.subTest(metodo=metodo, ruta=ruta):
                self.assertEqual(self.llamar(metodo, ruta)[0], 404)

    def test_error_interno_no_expone_ni_registra_el_detalle(self):
        self.s3.fail_on('put_object', RuntimeError('detalle-secreto'), prefix='claims/')
        with self.assertLogs('resenas', 'INFO') as cm:
            status, _, body = self.llamar('POST', '/resenas', json.dumps(payload()))
        self.assertEqual((status, body), (500, {'ok': False, 'error': 'interno'}))
        registro = '\n'.join(cm.output)
        self.assertIn('error_interno', registro)
        self.assertNotIn('detalle-secreto', registro)

    def test_los_logs_no_tienen_token_ni_texto_de_la_resena(self):
        with self.assertLogs('resenas', 'INFO') as cm:
            status, _, _ = self.llamar('POST', '/resenas',
                                       json.dumps(payload(comment='frase-unica-123', name='Ana Gómez')))
        self.assertEqual(status, 200)
        registro = '\n'.join(cm.output)
        self.assertIn('resena_creada', registro)
        for secreto in [TOKEN, 'frase-unica-123', 'Gómez']:
            self.assertNotIn(secreto, registro)

    def test_contrato_token_inexistente_responde_200_invalido(self):
        # Validado en producción: GET de un token inexistente → HTTP 200 {"ok": false, "motivo": "invalido"}.
        status, _, body = self.llamar('GET', '/tokens/' + 'A' * 32)
        self.assertEqual((status, body), (200, {'ok': False, 'motivo': 'invalido'}))

    def test_contrato_preflight_responde_204_con_origen_permitido(self):
        # Validado en producción: OPTIONS /resenas → HTTP 204 con Access-Control-Allow-Origin del sitio.
        status, cab, body = self.llamar('OPTIONS', '/resenas')
        self.assertEqual((status, body), (204, None))
        self.assertEqual(cab['Access-Control-Allow-Origin'], 'https://ork3d.com')

    def test_error_de_permiso_en_s3_no_se_disfraza_de_token_invalido(self):
        # Un AccessDenied de S3 es un problema de permisos, no un token inexistente: debe verse como error (500).
        self.s3.fail_on('get_object', FakeClientError('AccessDenied', 403, 'Access Denied'), prefix='tokens/')
        with self.assertLogs('resenas', 'INFO'):
            status, _, body = self.llamar('GET', f'/tokens/{TOKEN}')
        self.assertEqual((status, body), (500, {'ok': False, 'error': 'interno'}))


if __name__ == '__main__':
    unittest.main()
