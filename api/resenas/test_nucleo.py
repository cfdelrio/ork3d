"""Tests del núcleo reusable (api/resenas/nucleo): tokens, transiciones, S3 con precondiciones,
lock del export, allowlist y fotos. Sin boto3 ni red: FakeS3, FakeCloudFront y reloj inyectado.

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import base64
import datetime as dt
import hashlib
import json
import unittest

from admin_fixtures import INICIO, JPEG, PRIVADO, SITIO, Reloj, iso
from fakes import FakeClientError, FakeCloudFront, FakeS3, jpeg, segmento
from nucleo import bloqueo as _bloqueo
from nucleo import errores
from nucleo import publicacion as _publicacion
from nucleo import reglas
from nucleo import tokens as _tokens
from nucleo.almacen import Almacen

DATOS = {'order_ref': 'PEDIDO-1', 'product_slug': 'trofeo-acrilico', 'product_label': 'Trofeo acrílico',
         'category': 'trofeos', 'dias': 60}
CAMPOS_CLI = {'v', 'token_hash', 'created_at', 'expires_at', 'order_ref', 'product_slug',
              'product_label', 'category', 'verified', 'revoked', 'revoked_at'}
ALLOWLIST_V1 = ('id', 'rating', 'comment', 'aspects', 'display_name', 'category',
                'product_label', 'verified', 'photo_url', 'published_at')
CLAVE_LOCK = 'admin/locks/export.json'
_DENTRO_DE_UN_MINUTO = iso(INICIO + dt.timedelta(minutes=1))


def _fuente(valor):
    """Fuente de aleatoriedad fija: 24 bytes iguales. Valores distintos dan tokens distintos."""
    return lambda n: bytes([valor]) * n


def _resena(rid, **cambios):
    base = {'id': rid, 'status': 'approved', 'permission_to_publish': True, 'rating': 5,
            'comment': 'Bien', 'aspects': ['calidad'], 'display_name': 'Ana G.', 'category': 'trofeos',
            'product_label': 'Trofeo acrílico', 'verified': True, 'photo_public_key': None,
            'published_at': '2026-10-20T10:00:00Z', 'token_hash': 'f' * 64,
            'contact': 'NO-DEBE-SALIR', 'private_feedback': 'NO-DEBE-SALIR', 'order_ref': 'NO-DEBE-SALIR'}
    base.update(cambios)
    return base


class TokensTest(unittest.TestCase):
    def test_token_tiene_32_caracteres_base64url_sin_padding(self):
        for _ in range(100):
            token = _tokens.generar_token()
            self.assertRegex(token, r'^[A-Za-z0-9_-]{32}$')
            self.assertTrue(_tokens.token_valido(token))

    def test_formula_equivale_a_openssl_base64_con_tr(self):
        # openssl rand -base64 24 | tr '+/' '-_' | tr -d '='
        crudo = bytes(range(24))
        esperado = base64.b64encode(crudo).decode('ascii').replace('+', '-').replace('/', '_').replace('=', '')
        self.assertEqual(_tokens.generar_token(lambda n: crudo), esperado)
        self.assertEqual(len(esperado), 32)

    def test_token_valido_rechaza_lo_que_no_produce_la_cli(self):
        for malo in ['', 'a' * 31, 'a' * 33, 'a' * 31 + '+', 'a' * 31 + '=', 'a' * 31 + '/', None, 123]:
            with self.subTest(valor=malo):
                self.assertFalse(_tokens.token_valido(malo))

    def test_hash_es_sha256_hex_del_token(self):
        token = 'a' * 32
        self.assertEqual(_tokens.hash_token(token), hashlib.sha256(token.encode('utf-8')).hexdigest())
        self.assertRegex(_tokens.hash_token(token), r'^[a-f0-9]{64}$')


class ValidarInvitacionTest(unittest.TestCase):
    def valida(self, **cambios):
        return _tokens.validar_invitacion({**DATOS, **cambios})

    def test_espacios_se_normalizan(self):
        self.assertEqual(self.valida(order_ref='  PEDIDO   1 ')['order_ref'], 'PEDIDO 1')

    def test_dias_por_defecto_si_falta_o_es_null(self):
        sin_dias = {k: v for k, v in DATOS.items() if k != 'dias'}
        self.assertEqual(_tokens.validar_invitacion(sin_dias, 45)['dias'], 45)
        self.assertEqual(_tokens.validar_invitacion({**DATOS, 'dias': None}, 45)['dias'], 45)

    def test_obligatorios_vacios_se_marcan(self):
        with self.assertRaises(errores.ErrorValidacion) as ctx:
            self.valida(order_ref='   ', product_label='')
        self.assertEqual(set(ctx.exception.campos), {'order_ref', 'product_label'})

    def test_longitudes_maximas(self):
        for campo in ['order_ref', 'product_label']:
            self.assertEqual(len(self.valida(**{campo: 'x' * 80})[campo]), 80)
            with self.assertRaises(errores.ErrorValidacion) as ctx:
                self.valida(**{campo: 'x' * 81})
            self.assertIn(campo, ctx.exception.campos)
        for campo in ['product_slug', 'category']:
            self.assertEqual(self.valida(**{campo: 'x' * 40})[campo], 'x' * 40)
            with self.assertRaises(errores.ErrorValidacion) as ctx:
                self.valida(**{campo: 'x' * 41})
            self.assertIn(campo, ctx.exception.campos)

    def test_slugs_solo_minusculas_numeros_y_guiones(self):
        for campo in ['product_slug', 'category']:
            for malo in ['Trofeo', 'trofeo_x', 'trofeo x', 'ñandu', '../x']:
                with self.subTest(campo=campo, valor=malo), self.assertRaises(errores.ErrorValidacion) as ctx:
                    self.valida(**{campo: malo})
                self.assertIn(campo, ctx.exception.campos)
            self.assertEqual(self.valida(**{campo: 'trofeo-2026'})[campo], 'trofeo-2026')

    def test_dias_entre_1_y_365_y_enteros(self):
        for valido in [1, 365]:
            self.assertEqual(self.valida(dias=valido)['dias'], valido)
        for malo in [0, 366, -1, 1.5, '15', True, False]:
            with self.subTest(dias=malo), self.assertRaises(errores.ErrorValidacion) as ctx:
                self.valida(dias=malo)
            self.assertIn('dias', ctx.exception.campos)

    def test_cuerpo_que_no_es_objeto_es_validacion(self):
        with self.assertRaises(errores.ErrorValidacion):
            _tokens.validar_invitacion(['x'])


class CrearTokenTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)

    def test_registro_tiene_exactamente_los_campos_de_la_cli(self):
        _, registro = _tokens.crear_token(self.almacen, PRIVADO, DATOS, INICIO, fuente=_fuente(1))
        self.assertEqual(set(registro), CAMPOS_CLI)
        self.assertEqual(registro['expires_at'], '2026-12-20T12:00:00Z')
        self.assertFalse(registro['verified'])
        self.assertFalse(registro['revoked'])
        self.assertIsNone(registro['revoked_at'])
        self.assertNotIn('message', registro)  # el mensaje de la invitación no se guarda

    def test_objeto_se_indexa_por_hash_y_no_contiene_el_token(self):
        token, registro = _tokens.crear_token(self.almacen, PRIVADO, DATOS, INICIO, fuente=_fuente(1))
        clave = f'tokens/{_tokens.hash_token(token)}.json'
        self.assertIn(clave, self.s3.objects)
        self.assertNotIn(token, self.s3.objects[clave].decode('utf-8'))
        self.assertEqual(json.loads(self.s3.objects[clave]), registro)

    def test_colision_reintenta_con_otro_token_sin_pisar_el_existente(self):
        colisionado = _tokens.generar_token(_fuente(1))
        clave_previa = f'tokens/{_tokens.hash_token(colisionado)}.json'
        self.s3.objects[clave_previa] = b'{"previo": true}'
        salidas = iter([_fuente(1), _fuente(2)])
        token, _ = _tokens.crear_token(self.almacen, PRIVADO, DATOS, INICIO,
                                       fuente=lambda n: next(salidas)(n))
        self.assertNotEqual(token, colisionado)
        self.assertEqual(self.s3.objects[clave_previa], b'{"previo": true}')

    def test_tres_colisiones_seguidas_fallan_sin_escribir(self):
        colisionado = _tokens.generar_token(_fuente(1))
        self.s3.objects[f'tokens/{_tokens.hash_token(colisionado)}.json'] = b'{}'
        with self.assertRaises(errores.ErrorAlmacen):
            _tokens.crear_token(self.almacen, PRIVADO, DATOS, INICIO, fuente=_fuente(1))
        self.assertEqual([c for c in self.s3.calls if c[0] == 'put_object'], [('put_object', f'tokens/{_tokens.hash_token(colisionado)}.json')] * 3)


class ReglasTest(unittest.TestCase):
    def test_transiciones_validas(self):
        casos = {('aprobar', 'pending'): 'approved', ('rechazar', 'pending'): 'rejected',
                 ('rechazar', 'approved'): 'rejected', ('ocultar', 'approved'): 'hidden',
                 ('restaurar', 'hidden'): 'approved'}
        for (accion, estado), nuevo in casos.items():
            with self.subTest(accion=accion, estado=estado):
                self.assertEqual(reglas.destino(accion, estado), nuevo)

    def test_cualquier_otra_combinacion_es_409_con_estado_actual(self):
        invalidas = [('aprobar', 'approved'), ('aprobar', 'rejected'), ('aprobar', 'hidden'),
                     ('rechazar', 'rejected'), ('rechazar', 'hidden'), ('ocultar', 'pending'),
                     ('ocultar', 'hidden'), ('ocultar', 'rejected'), ('restaurar', 'approved'),
                     ('restaurar', 'pending'), ('restaurar', 'rejected')]
        for accion, estado in invalidas:
            with self.subTest(accion=accion, estado=estado), self.assertRaises(errores.ErrorEstado) as ctx:
                reglas.destino(accion, estado)
            self.assertEqual(ctx.exception.estado_actual, estado)

    def test_diferencias_con_la_cli_quedan_documentadas(self):
        # La CLI acepta estas; la web no (ver docs/resenas/V2-ADMIN.md §2, desviaciones 2 a 4).
        for accion, estado in [('aprobar', 'rejected'), ('rechazar', 'hidden'), ('ocultar', 'pending')]:
            with self.subTest(accion=accion, estado=estado), self.assertRaises(errores.ErrorEstado):
                reglas.destino(accion, estado)

    def test_aprobar_y_restaurar_exigen_permiso_de_publicacion(self):
        for accion in ['aprobar', 'restaurar']:
            with self.subTest(accion=accion):
                with self.assertRaises(errores.ErrorSinPermiso):
                    reglas.exige_permiso(accion, {'permission_to_publish': False})
                with self.assertRaises(errores.ErrorSinPermiso):
                    reglas.exige_permiso(accion, {})
                reglas.exige_permiso(accion, {'permission_to_publish': True})
        reglas.exige_permiso('rechazar', {'permission_to_publish': False})
        reglas.exige_permiso('ocultar', {})

    def test_export_cambia_solo_donde_cambia_lo_publico(self):
        self.assertTrue(reglas.cambia_export('aprobar', 'pending'))
        self.assertTrue(reglas.cambia_export('rechazar', 'approved'))
        self.assertTrue(reglas.cambia_export('ocultar', 'approved'))
        self.assertTrue(reglas.cambia_export('restaurar', 'hidden'))
        self.assertFalse(reglas.cambia_export('rechazar', 'pending'))


class AlmacenTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)

    def test_inexistente_devuelve_none(self):
        self.assertEqual(self.almacen.leer_json('b', 'x.json'), (None, None))
        self.assertFalse(self.almacen.existe('b', 'x.json'))

    def test_borrar_es_idempotente(self):
        self.almacen.borrar('b', 'k')
        self.almacen.borrar('b', 'k')

    def test_create_only_no_sobrescribe(self):
        self.almacen.escribir_json('b', 'k', {'v': 1}, solo_si_no_existe=True)
        with self.assertRaises(errores.ErrorConflicto):
            self.almacen.escribir_json('b', 'k', {'v': 2}, solo_si_no_existe=True)
        self.assertEqual(self.almacen.leer_json('b', 'k')[0], {'v': 1})

    def test_if_match_con_etag_vigente_escribe(self):
        self.almacen.escribir_json('b', 'k', {'v': 1})
        _, etag = self.almacen.leer_json('b', 'k')
        self.almacen.escribir_json('b', 'k', {'v': 2}, etag=etag)
        self.assertEqual(self.almacen.leer_json('b', 'k')[0], {'v': 2})

    def test_if_match_con_etag_viejo_no_sobrescribe(self):
        self.almacen.escribir_json('b', 'k', {'v': 1})
        _, etag_viejo = self.almacen.leer_json('b', 'k')
        self.almacen.escribir_json('b', 'k', {'v': 2})  # otro escritor llega primero
        with self.assertRaises(errores.ErrorConflicto):
            self.almacen.escribir_json('b', 'k', {'v': 3}, etag=etag_viejo)
        self.assertEqual(self.almacen.leer_json('b', 'k')[0], {'v': 2})

    def test_if_match_sobre_clave_borrada_es_conflicto(self):
        self.almacen.escribir_json('b', 'k', {'v': 1})
        _, etag = self.almacen.leer_json('b', 'k')
        self.almacen.borrar('b', 'k')
        with self.assertRaises(errores.ErrorConflicto):
            self.almacen.escribir_json('b', 'k', {'v': 2}, etag=etag)

    def test_listar_recorre_todas_las_paginas(self):
        class Paginado(FakeS3):
            def list_objects_v2(self, Bucket, Prefix='', ContinuationToken=None, **_):
                claves = sorted(k for k in self.objects if k.startswith(Prefix))
                inicio = int(ContinuationToken or 0)
                fin = inicio + 2 < len(claves)
                return {'Contents': [{'Key': k} for k in claves[inicio:inicio + 2]],
                        'IsTruncated': fin,
                        'NextContinuationToken': str(inicio + 2) if fin else None}

        s3 = Paginado()
        for i in range(5):
            s3.objects[f'p/{i}'] = b'{}'
        self.assertEqual(Almacen(s3).listar('b', 'p/'), [f'p/{i}' for i in range(5)])


class BloqueoTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)
        self.reloj = Reloj()

    def bloqueo(self, duracion_s=60):
        return _bloqueo.Bloqueo(self.almacen, PRIVADO, 'export', duracion_s, self.reloj)

    def dueno_actual(self):
        registro, _ = self.almacen.leer_json(PRIVADO, CLAVE_LOCK)
        return registro and registro['duenio']

    def test_adquirido_si_no_hay_lock(self):
        duenio = self.bloqueo().adquirir()
        self.assertRegex(duenio, r'^[a-f0-9]{32}$')
        self.assertEqual(self.dueno_actual(), duenio)

    def test_ocupado_si_el_lock_esta_vigente(self):
        self.bloqueo().adquirir()
        with self.assertRaises(errores.ErrorBloqueado):
            self.bloqueo().adquirir()

    def test_vencido_se_toma(self):
        self.bloqueo(60).adquirir()
        self.reloj.avanzar(seconds=61)
        nuevo = self.bloqueo(60).adquirir()
        self.assertEqual(self.dueno_actual(), nuevo)

    def test_sobre_lock_vencido_solo_gana_una_toma(self):
        # Otro proceso toma el lock vencido justo después de que este lo lee: la escritura con
        # If-Match de este proceso debe fallar, y el lock queda con el dueño ganador.
        self.bloqueo(60).adquirir()
        self.reloj.avanzar(seconds=61)
        original = self.almacen.leer_json
        estado = {'compitido': False, 'ganador': None}

        def leer_y_compite(bucket, key):
            resultado = original(bucket, key)
            if not estado['compitido']:
                estado['compitido'] = True
                estado['ganador'] = self.bloqueo(60).adquirir()
            return resultado

        self.almacen.leer_json = leer_y_compite
        with self.assertRaises(errores.ErrorBloqueado):
            self.bloqueo(60).adquirir()
        self.assertEqual(self.dueno_actual(), estado['ganador'])

    def test_liberar_solo_con_el_dueno_correcto(self):
        b = self.bloqueo()
        duenio = b.adquirir()
        b.liberar('otro')
        self.assertEqual(self.dueno_actual(), duenio)
        b.liberar(duenio)
        self.assertIsNone(self.dueno_actual())

    def test_liberar_sin_lock_no_falla(self):
        self.bloqueo().liberar('cualquiera')

    def test_tras_liberar_se_puede_volver_a_tomar(self):
        b = self.bloqueo()
        b.liberar(b.adquirir())
        self.assertRegex(self.bloqueo().adquirir(), r'^[a-f0-9]{32}$')


def _publicador_de(almacen, reloj, cf, espera, reintentos=3):
    return _publicacion.Publicador(almacen, PRIVADO, SITIO, cf,
                                   _bloqueo.Bloqueo(almacen, PRIVADO, 'export', 60, reloj),
                                   reloj, espera=espera, reintentos=reintentos)


class ExportTest(unittest.TestCase):
    def test_allowlist_no_cambio_respecto_de_v1(self):
        self.assertEqual(_publicacion.ALLOWLIST_EXPORT, ALLOWLIST_V1)

    def test_cada_item_tiene_exactamente_las_claves_de_la_allowlist(self):
        export = _publicacion.construir_export([_resena('a' * 32, photo_public_key='resenas/fotos/x.jpg')], iso(INICIO))
        self.assertEqual(export['count'], 1)
        for item in export['items']:
            self.assertEqual(set(item), set(ALLOWLIST_V1))

    def test_solo_aprobadas_con_permiso(self):
        resenas = [_resena('1' * 32),
                   _resena('2' * 32, status='pending'),
                   _resena('3' * 32, status='rejected'),
                   _resena('4' * 32, status='hidden'),
                   _resena('5' * 32, permission_to_publish=False)]
        self.assertEqual([i['id'] for i in _publicacion.construir_export(resenas, iso(INICIO))['items']],
                         ['1' * 32])

    def test_orden_por_published_at_descendente(self):
        resenas = [_resena('a' * 32, published_at='2026-10-01T00:00:00Z'),
                   _resena('b' * 32, published_at='2026-10-03T00:00:00Z'),
                   _resena('c' * 32, published_at='2026-10-02T00:00:00Z')]
        ids = [i['id'] for i in _publicacion.construir_export(resenas, iso(INICIO))['items']]
        self.assertEqual(ids, ['b' * 32, 'c' * 32, 'a' * 32])

    def test_photo_url_solo_si_hay_foto_publica(self):
        con = _resena('a' * 32, photo_public_key=f'resenas/fotos/{"a" * 32}.jpg')
        sin = _resena('b' * 32, photo_public_key=None)
        items = {i['id']: i for i in _publicacion.construir_export([con, sin], iso(INICIO))['items']}
        self.assertEqual(items['a' * 32]['photo_url'], f'/resenas/fotos/{"a" * 32}.jpg')
        self.assertIsNone(items['b' * 32]['photo_url'])

    def test_nombre_anonimo_por_defecto(self):
        item = _publicacion.construir_export([_resena('a' * 32, display_name=None)], iso(INICIO))['items'][0]
        self.assertEqual(item['display_name'], _publicacion.NOMBRE_ANONIMO)


class FotoPublicaTest(unittest.TestCase):
    TH = hashlib.sha256(b'foto').hexdigest()
    RID = 'a' * 32

    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)
        self.clave_privada = f'pendientes/{self.TH}/{"b" * 32}.jpg'

    def test_copia_bytes_exactos_con_cache_inmutable_solo_al_sitio(self):
        self.s3.objects[self.clave_privada] = JPEG
        clave = _publicacion.publicar_foto(self.almacen, PRIVADO, SITIO, self.RID, self.TH, self.clave_privada)
        self.assertEqual(clave, f'resenas/fotos/{self.RID}.jpg')
        self.assertEqual(self.s3.objects[clave], JPEG)
        self.assertEqual(self.s3.metadatos[clave]['CacheControl'], _publicacion.CACHE_FOTO)
        self.assertEqual(self.s3.metadatos[clave]['ContentType'], 'image/jpeg')
        self.assertEqual(self.s3.escrituras, [(SITIO, clave)])

    def test_clave_de_otro_token_no_se_publica(self):
        with self.assertRaises(errores.ErrorFoto) as ctx:
            _publicacion.publicar_foto(self.almacen, PRIVADO, SITIO, self.RID, 'c' * 64, self.clave_privada)
        self.assertEqual(ctx.exception.args[0], 'clave_ajena')

    def test_foto_con_exif_no_se_publica(self):
        self.s3.objects[self.clave_privada] = jpeg(segmentos_extra=segmento(0xE1, b'Exif\x00\x00' + b'x' * 30))
        with self.assertRaises(errores.ErrorFoto) as ctx:
            _publicacion.publicar_foto(self.almacen, PRIVADO, SITIO, self.RID, self.TH, self.clave_privada)
        self.assertEqual(ctx.exception.args[0], 'no_jpeg_limpio')
        self.assertEqual(self.s3.escrituras, [])

    def test_png_no_se_publica(self):
        self.s3.objects[self.clave_privada] = b'\x89PNG\r\n\x1a\n' + b'\x00' * 100
        with self.assertRaises(errores.ErrorFoto):
            _publicacion.publicar_foto(self.almacen, PRIVADO, SITIO, self.RID, self.TH, self.clave_privada)

    def test_foto_inexistente(self):
        with self.assertRaises(errores.ErrorFoto) as ctx:
            _publicacion.publicar_foto(self.almacen, PRIVADO, SITIO, self.RID, self.TH, self.clave_privada)
        self.assertEqual(ctx.exception.args[0], 'no_existe')

    def test_quitar_es_idempotente(self):
        self.s3.objects[f'resenas/fotos/{self.RID}.jpg'] = JPEG
        _publicacion.quitar_foto_publica(self.almacen, SITIO, self.RID)
        _publicacion.quitar_foto_publica(self.almacen, SITIO, self.RID)
        self.assertNotIn(f'resenas/fotos/{self.RID}.jpg', self.s3.objects)

    def test_reparar_borra_solo_fotos_sin_resena_aprobada_vigente(self):
        vigente, rechazada, sin_permiso = 'a' * 32, 'b' * 32, 'c' * 32
        resenas = [_resena(vigente, photo_public_key=f'resenas/fotos/{vigente}.jpg'),
                   _resena(rechazada, status='rejected', photo_public_key=f'resenas/fotos/{rechazada}.jpg'),
                   _resena(sin_permiso, permission_to_publish=False, photo_public_key=f'resenas/fotos/{sin_permiso}.jpg')]
        for r in resenas:
            self.s3.objects[f'resenas/fotos/{r["id"]}.jpg'] = JPEG
        borradas = _publicacion.reparar_fotos_publicas(self.almacen, SITIO, resenas)
        self.assertEqual(borradas, 2)
        self.assertIn(f'resenas/fotos/{vigente}.jpg', self.s3.objects)
        self.assertNotIn(f'resenas/fotos/{rechazada}.jpg', self.s3.objects)


class PublicadorTest(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        self.almacen = Almacen(self.s3)
        self.reloj = Reloj()
        self.cf = FakeCloudFront()
        self.esperas = []

    def publicador(self, reintentos=3, espera=None):
        return _publicador_de(self.almacen, self.reloj, self.cf, espera or self.esperas.append, reintentos)

    def sembrar(self, rid='a' * 32, **cambios):
        self.s3.objects[f'resenas/{rid}.json'] = json.dumps(_resena(rid, **cambios)).encode('utf-8')

    def export(self):
        return json.loads(self.s3.objects['resenas/publicas.json'])

    def test_regenerar_escribe_export_invalida_y_libera_el_lock(self):
        self.sembrar()
        resultado = self.publicador().regenerar()
        self.assertEqual(resultado['publicadas'], 1)
        self.assertEqual(self.export()['count'], 1)
        self.assertEqual(self.cf.invalidaciones, [['/resenas/*']])
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)

    def test_lock_vigente_reintenta_y_no_escribe(self):
        self.s3.objects[CLAVE_LOCK] = json.dumps({'v': 1, 'duenio': 'otro', 'expira': _DENTRO_DE_UN_MINUTO}).encode()
        with self.assertRaises(errores.ErrorBloqueado):
            self.publicador(reintentos=3).regenerar()
        self.assertEqual(len(self.esperas), 2)
        self.assertNotIn('resenas/publicas.json', self.s3.objects)
        self.assertEqual(self.cf.invalidaciones, [])
        self.assertEqual(json.loads(self.s3.objects[CLAVE_LOCK])['duenio'], 'otro')

    def test_lock_que_se_libera_durante_los_reintentos_permite_regenerar(self):
        self.s3.objects[CLAVE_LOCK] = json.dumps({'v': 1, 'duenio': 'otro', 'expira': _DENTRO_DE_UN_MINUTO}).encode()
        self.sembrar()
        liberar = lambda segundos: self.s3.objects.pop(CLAVE_LOCK, None)
        self.assertEqual(self.publicador(espera=liberar).regenerar()['publicadas'], 1)

    def test_lock_vencido_se_toma_y_se_libera(self):
        self.s3.objects[CLAVE_LOCK] = json.dumps({'v': 1, 'duenio': 'otro', 'expira': iso(INICIO)}).encode()
        self.reloj.avanzar(seconds=61)
        self.sembrar()
        self.publicador().regenerar()
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)

    def test_fallo_al_escribir_el_export_libera_el_lock(self):
        self.sembrar()
        self.s3.fail_on('put_object', FakeClientError('InternalError', 500, 'boom'), prefix='resenas/publicas.json')
        with self.assertRaises(FakeClientError):
            self.publicador().regenerar()
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)

    def test_fallo_al_invalidar_libera_el_lock_y_propaga(self):
        self.sembrar()
        self.cf.fallar_proximo()
        with self.assertRaises(FakeClientError):
            self.publicador().regenerar()
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)
        self.assertIn('resenas/publicas.json', self.s3.objects)  # el export ya estaba escrito

    def test_reintentar_es_seguro_y_produce_el_mismo_export(self):
        self.sembrar('a' * 32, published_at='2026-10-20T10:00:00Z')
        self.sembrar('b' * 32, published_at='2026-10-19T10:00:00Z')
        self.publicador().regenerar()
        primero = self.s3.objects['resenas/publicas.json']
        self.publicador().regenerar()
        self.assertEqual(self.s3.objects['resenas/publicas.json'], primero)

    def test_repara_foto_publica_huerfana(self):
        self.sembrar('a' * 32, status='rejected')
        self.s3.objects[f'resenas/fotos/{"a" * 32}.jpg'] = JPEG
        resultado = self.publicador().regenerar()
        self.assertEqual(resultado['fotos_reparadas'], 1)
        self.assertNotIn(f'resenas/fotos/{"a" * 32}.jpg', self.s3.objects)


if __name__ == '__main__':
    unittest.main()
