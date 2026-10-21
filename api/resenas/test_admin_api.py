"""Tests de la API administrativa (V2): invitaciones, bandeja, detalle, foto, moderación,
concurrencia optimista, lock del export, reenvío de aviso y mapeo de errores.

Correr desde la raíz del repo:
    python3 -m unittest discover -s api/resenas -v
"""
import base64
import datetime as dt
import hashlib
import json
import unittest
import uuid

from admin_fixtures import (BASE, CABECERAS_CSRF, CAMPOS_TOKEN, CONTACTO, FEEDBACK, INICIO, INVITACION, JPEG,
                            ORIGEN, PEDIDO, PRIVADO, SITIO, AdminSesionCase, datos, iso)
from fakes import FakeClientError, FakeS3

CLAVE_LOCK = 'admin/locks/export.json'


def foto_de(rid):
    return f'resenas/fotos/{rid}.jpg'


def bloqueo_vigente(duenio='otro', minutos=1):
    return json.dumps({'v': 1, 'duenio': duenio, 'expira': iso(INICIO + dt.timedelta(minutes=minutos))}).encode()


class FakeS3Concurrente(FakeS3):
    """La primera lectura de `clave` va seguida de una escritura ajena (otro moderador)."""

    def __init__(self, clave, ajeno):
        super().__init__()
        self.clave = clave
        self.ajeno = ajeno
        self.disparado = False

    def get_object(self, Bucket, Key, **kwargs):
        resultado = super().get_object(Bucket=Bucket, Key=Key, **kwargs)
        if Key == self.clave and not self.disparado:
            self.disparado = True
            self.ajeno(self)
        return resultado


class InvitacionesTest(AdminSesionCase):
    def test_crea_invitacion_y_devuelve_enlace_sin_datos_privados(self):
        r = self.invitacion(self.cookies)
        self.assertEqual(r['statusCode'], 201)
        cuerpo = datos(r)
        self.assertEqual(set(cuerpo), {'ok', 'enlace', 'vence', 'producto_label', 'categoria'})
        self.assertRegex(cuerpo['enlace'], r'^https://ork3d\.com/opinar/[A-Za-z0-9_-]{32}$')
        self.assertNotIn(PEDIDO, json.dumps(cuerpo))
        self.assertEqual((cuerpo['producto_label'], cuerpo['categoria']), ('Trofeo acrílico', 'trofeos'))

    def test_registro_guardado_tiene_los_campos_de_la_cli_y_no_guarda_el_token(self):
        token = datos(self.invitacion(self.cookies))['enlace'].rsplit('/', 1)[1]
        clave = f'tokens/{hashlib.sha256(token.encode("utf-8")).hexdigest()}.json'
        texto = self.s3.objects[clave].decode('utf-8')
        registro = json.loads(texto)
        self.assertEqual(set(registro), CAMPOS_TOKEN)
        self.assertEqual(registro['order_ref'], PEDIDO)
        self.assertEqual(registro['expires_at'], '2026-11-20T12:00:00Z')  # 30 días desde la creación
        self.assertFalse(registro['verified'])
        self.assertNotIn(token, texto)

    def test_mensaje_de_la_invitacion_no_se_guarda(self):
        datos(self.invitacion(self.cookies, mensaje='Hola, tu link: https://ork3d.com/opinar/x'))
        for clave, contenido in self.s3.objects.items():
            if clave.startswith('tokens/'):
                self.assertNotIn('mensaje', contenido.decode('utf-8'))
                self.assertNotIn('Hola, tu link', contenido.decode('utf-8'))

    def test_sin_dias_usa_sesenta(self):
        sin_dias = {k: v for k, v in INVITACION.items() if k != 'dias'}
        r = self.pedir('POST', '/admin/api/invitaciones', sin_dias, cookies=self.cookies)
        self.assertEqual(r['statusCode'], 201)
        clave = [k for k in self.s3.objects if k.startswith('tokens/')][0]
        self.assertEqual(json.loads(self.s3.objects[clave])['expires_at'], '2026-12-20T12:00:00Z')

    def test_validacion_devuelve_campos_y_no_escribe_nada(self):
        antes = set(self.s3.objects)
        r = self.invitacion(self.cookies, order_ref='', product_slug='Mal Slug', dias=400)
        self.assertEqual(r['statusCode'], 400)
        self.assertEqual(set(datos(r)['campos']), {'order_ref', 'product_slug', 'dias'})
        self.assertEqual(set(self.s3.objects), antes)

    def test_cuerpo_que_no_es_objeto_json_responde_400_json(self):
        for cuerpo in ['[1,2]', '{no json', '"texto"']:
            with self.subTest(cuerpo=cuerpo):
                r = self.pedir('POST', '/admin/api/invitaciones', cuerpo, cookies=self.cookies)
                self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'json'))

    def test_cuerpo_mayor_a_8_kb_responde_413(self):
        r = self.pedir('POST', '/admin/api/invitaciones', 'x' * 9000, cookies=self.cookies)
        self.assertEqual((r['statusCode'], datos(r)['error']), (413, 'demasiado_grande'))

    def test_cada_invitacion_crea_su_propio_token(self):
        enlaces = {datos(self.invitacion(self.cookies))['enlace'] for _ in range(3)}
        self.assertEqual(len(enlaces), 3)
        self.assertEqual(len([k for k in self.s3.objects if k.startswith('tokens/')]), 3)


class ListadoTest(AdminSesionCase):
    def setUp(self):
        super().setUp()
        self.pendiente_antigua = self.sembrar_resena(status='pending', created_at='2026-10-20T10:00:00Z')
        self.pendiente_nueva = self.sembrar_resena(status='pending', created_at='2026-10-21T09:00:00Z',
                                                   aviso=True)
        self.aprobada = self.sembrar_resena(status='approved', created_at='2026-10-19T08:00:00Z',
                                            published_at='2026-10-19T09:00:00Z')
        self.rechazada = self.sembrar_resena(status='rejected', created_at='2026-10-21T11:00:00Z')
        self.oculta = self.sembrar_resena(status='hidden', created_at='2026-10-18T08:00:00Z')

    def listar(self, estado=None):
        qs = f'estado={estado}' if estado else ''
        r = self.pedir('GET', '/admin/api/resenas', cookies=self.cookies, qs=qs)
        self.assertEqual(r['statusCode'], 200, r['body'])
        return datos(r)

    def ids(self, estado=None):
        return [f['id'] for f in self.listar(estado)['resenas']]

    def test_todas_muestran_pendientes_primero_y_las_mas_nuevas_arriba(self):
        self.assertEqual(self.ids('all'), [self.pendiente_nueva, self.pendiente_antigua, self.aprobada,
                                           self.oculta, self.rechazada])

    def test_filtros_por_estado(self):
        self.assertEqual(self.ids('pending'), [self.pendiente_nueva, self.pendiente_antigua])
        self.assertEqual(self.ids('approved'), [self.aprobada])
        self.assertEqual(self.ids('rejected'), [self.rechazada])
        self.assertEqual(self.ids('hidden'), [self.oculta])

    def test_por_defecto_es_all_y_el_total_coincide(self):
        cuerpo = self.listar()
        self.assertEqual((cuerpo['estado'], cuerpo['total']), ('all', 5))

    def test_filtro_invalido_responde_400(self):
        r = self.pedir('GET', '/admin/api/resenas', cookies=self.cookies, qs='estado=borradas')
        self.assertEqual((r['statusCode'], datos(r)['error']), (400, 'validacion'))

    def test_aviso_refleja_si_salio_el_correo(self):
        filas = {f['id']: f for f in self.listar()['resenas']}
        self.assertEqual(filas[self.pendiente_antigua]['aviso'], 'pendiente')
        self.assertEqual(filas[self.pendiente_nueva]['aviso'], 'enviado')
        self.assertEqual(filas[self.aprobada]['aviso'], 'no_aplica')

    def test_filas_no_traen_comentario_ni_datos_privados(self):
        texto = json.dumps(self.listar())
        for prohibido in [CONTACTO, FEEDBACK, PEDIDO, 'token_hash', 'photo_key', 'tokens/', 'pendientes/',
                          'Muy buena compra.']:
            self.assertNotIn(prohibido, texto)
        for fila in self.listar()['resenas']:
            self.assertNotIn('comment', fila)
            self.assertNotIn('contact', fila)

    def test_sin_sesion_responde_401(self):
        self.assertEqual(self.pedir('GET', '/admin/api/resenas')['statusCode'], 401)


class DetalleTest(AdminSesionCase):
    def test_incluye_pedido_resuelto_en_el_servidor_y_datos_de_moderacion(self):
        rid = self.sembrar_resena(status='pending')
        resena = datos(self.pedir('GET', f'/admin/api/resenas/{rid}', cookies=self.cookies))['resena']
        self.assertEqual(resena['pedido'], PEDIDO)
        self.assertEqual(resena['comment'], 'Muy buena compra.')
        self.assertEqual(resena['private_feedback'], FEEDBACK)
        self.assertEqual(resena['contact'], CONTACTO)  # el admin ve el contacto
        self.assertEqual(resena['aviso'], 'pendiente')

    def test_no_devuelve_hash_claves_ni_rutas_del_almacen(self):
        rid = self.sembrar_resena(foto=True)
        th = json.loads(self.s3.objects[f'resenas/{rid}.json'])['token_hash']
        texto = self.pedir('GET', f'/admin/api/resenas/{rid}', cookies=self.cookies)['body']
        for prohibido in [th, 'token_hash', 'photo_key', 'photo_public_key', 'pendientes/', 'resenas/', 'tokens/',
                          'admin/', 'amazonaws', 'arn:', PRIVADO, SITIO]:
            self.assertNotIn(prohibido, texto)

    def test_sin_sesion_responde_401(self):
        rid = self.sembrar_resena()
        self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}')['statusCode'], 401)

    def test_inexistente_o_id_mal_formado_responde_404(self):
        self.assertEqual(self.pedir('GET', '/admin/api/resenas/' + 'f' * 32, cookies=self.cookies)['statusCode'], 404)
        self.assertEqual(self.pedir('GET', '/admin/api/resenas/no-es-un-id', cookies=self.cookies)['statusCode'], 404)


class FotoTest(AdminSesionCase):
    def test_devuelve_los_bytes_con_cache_prohibida(self):
        rid = self.sembrar_resena(foto=True)
        r = self.pedir('GET', f'/admin/api/resenas/{rid}/foto', cookies=self.cookies)
        self.assertEqual(r['statusCode'], 200)
        self.assertTrue(r['isBase64Encoded'])
        self.assertEqual(base64.b64decode(r['body']), JPEG)
        self.assertEqual(r['headers']['Content-Type'], 'image/jpeg')
        self.assertEqual(r['headers']['Cache-Control'], 'no-store')
        self.assertEqual(r['headers']['X-Content-Type-Options'], 'nosniff')
        self.assertNotIn('Location', r['headers'])

    def test_requiere_sesion(self):
        rid = self.sembrar_resena(foto=True)
        self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}/foto')['statusCode'], 401)

    def test_clave_de_otro_token_responde_404(self):
        rid = self.sembrar_resena(foto=True)
        resena = self.resena(rid)
        resena['token_hash'] = 'a' * 64
        self.s3.objects[f'resenas/{rid}.json'] = json.dumps(resena).encode('utf-8')
        self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}/foto', cookies=self.cookies)['statusCode'], 404)

    def test_sin_foto_o_sin_objeto_responde_404(self):
        sin_foto = self.sembrar_resena(foto=False)
        con_foto = self.sembrar_resena(foto=True)
        del self.s3.objects[self.resena(con_foto)['photo_key']]
        for rid in [sin_foto, con_foto]:
            with self.subTest(rid=rid):
                self.assertEqual(self.pedir('GET', f'/admin/api/resenas/{rid}/foto', cookies=self.cookies)['statusCode'], 404)


class ModeracionTest(AdminSesionCase):
    def accion(self, rid, nombre):
        return self.pedir('POST', f'/admin/api/resenas/{rid}/{nombre}', cookies=self.cookies)

    def estado(self, rid):
        return self.resena(rid)['status']

    def items_publicos(self):
        return json.loads(self.s3.objects['resenas/publicas.json'])['items']

    def test_aprobar_publica_y_regenera_el_export(self):
        rid = self.sembrar_resena(status='pending')
        r = self.accion(rid, 'aprobar')
        self.assertEqual((r['statusCode'], datos(r)), (200, {'ok': True, 'estado': 'approved', 'export': 'actualizado'}))
        self.assertEqual([i['id'] for i in self.items_publicos()], [rid])
        self.assertEqual(self.cf.invalidaciones, [['/resenas/*']])
        self.assertIsNotNone(self.resena(rid)['published_at'])
        self.assertIsNotNone(self.resena(rid)['moderated_at'])

    def test_aprobar_con_foto_copia_los_bytes_solo_al_sitio(self):
        rid = self.sembrar_resena(foto=True)
        self.assertEqual(self.accion(rid, 'aprobar')['statusCode'], 200)
        self.assertEqual(self.s3.objects[foto_de(rid)], JPEG)
        self.assertIn((SITIO, foto_de(rid)), self.s3.escrituras)
        self.assertEqual({b for b, k in self.s3.escrituras if k.startswith('resenas/fotos/')}, {SITIO})
        self.assertEqual(self.items_publicos()[0]['photo_url'], f'/resenas/fotos/{rid}.jpg')
        self.assertEqual(self.resena(rid)['photo_public_key'], foto_de(rid))

    def test_aprobar_sin_permiso_responde_409_y_no_cambia_nada(self):
        rid = self.sembrar_resena(status='pending', permiso=False, foto=True)
        antes = dict(self.s3.objects)
        r = self.accion(rid, 'aprobar')
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'sin_permiso'))
        self.assertEqual(self.s3.objects, antes)

    def test_aprobar_dos_veces_responde_409_con_estado_actual(self):
        rid = self.sembrar_resena(status='pending')
        self.assertEqual(self.accion(rid, 'aprobar')['statusCode'], 200)
        r = self.accion(rid, 'aprobar')
        self.assertEqual((r['statusCode'], datos(r)['error'], datos(r)['estado_actual']),
                         (409, 'estado_incompatible', 'approved'))

    def test_rechazar_pendiente_no_toca_el_export(self):
        rid = self.sembrar_resena(status='pending')
        r = self.accion(rid, 'rechazar')
        self.assertEqual((r['statusCode'], datos(r)['estado']), (200, 'rejected'))
        self.assertEqual(self.cf.invalidaciones, [])
        self.assertNotIn('resenas/publicas.json', self.s3.objects)

    def aprobada_con_foto_publica(self):
        """Reseña aprobada con su foto ya publicada, como la deja aprobar."""
        rid = uuid.uuid4().hex
        self.sembrar_resena(rid, status='approved', published_at='2026-10-19T09:00:00Z', foto=True,
                            photo_public_key=foto_de(rid))
        self.s3.objects[foto_de(rid)] = JPEG
        return rid

    def test_rechazar_aprobada_retira_foto_y_la_saca_del_export(self):
        rid = self.aprobada_con_foto_publica()
        self.assertEqual(self.accion(rid, 'rechazar')['statusCode'], 200)
        self.assertNotIn(foto_de(rid), self.s3.objects)
        self.assertEqual(self.items_publicos(), [])
        self.assertEqual(self.cf.invalidaciones, [['/resenas/*']])

    def test_ocultar_aprobada_retira_foto_y_la_saca_del_export(self):
        rid = self.aprobada_con_foto_publica()
        r = self.accion(rid, 'ocultar')
        self.assertEqual((r['statusCode'], datos(r)['estado']), (200, 'hidden'))
        self.assertNotIn(foto_de(rid), self.s3.objects)
        self.assertEqual(self.items_publicos(), [])

    def test_ocultar_pendiente_responde_409(self):
        rid = self.sembrar_resena(status='pending')
        r = self.accion(rid, 'ocultar')
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'estado_incompatible'))

    def test_restaurar_oculta_repone_foto_y_conserva_published_at(self):
        rid = self.sembrar_resena(status='hidden', published_at='2026-10-19T09:00:00Z', foto=True)
        r = self.accion(rid, 'restaurar')
        self.assertEqual((r['statusCode'], datos(r)['estado']), (200, 'approved'))
        self.assertEqual(self.resena(rid)['published_at'], '2026-10-19T09:00:00Z')
        self.assertEqual(self.s3.objects[foto_de(rid)], JPEG)
        self.assertEqual([i['id'] for i in self.items_publicos()], [rid])

    def test_restaurar_sin_permiso_responde_409(self):
        rid = self.sembrar_resena(status='hidden', permiso=False)
        r = self.accion(rid, 'restaurar')
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'sin_permiso'))
        self.assertEqual(self.estado(rid), 'hidden')

    def test_restaurar_rechazada_responde_409(self):
        rid = self.sembrar_resena(status='rejected')
        self.assertEqual(self.accion(rid, 'restaurar')['statusCode'], 409)

    def test_transiciones_que_la_web_no_permite_aunque_la_cli_si(self):
        # Desviaciones documentadas en docs/resenas/V2-ADMIN.md §2.
        casos = [('rechazada', 'aprobar', 'rejected'), ('oculta', 'rechazar', 'hidden')]
        for nombre, accion, estado in casos:
            with self.subTest(nombre=nombre, accion=accion):
                rid = self.sembrar_resena(status=estado)
                r = self.accion(rid, accion)
                self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'estado_incompatible'))
                self.assertEqual(self.estado(rid), estado)

    def test_id_inexistente_responde_404(self):
        self.assertEqual(self.accion('f' * 32, 'aprobar')['statusCode'], 404)

    def test_accion_desconocida_responde_404(self):
        rid = self.sembrar_resena()
        self.assertEqual(self.accion(rid, 'borrar')['statusCode'], 404)

    def test_falla_al_copiar_la_foto_responde_503_y_no_cambia_el_estado(self):
        rid = self.sembrar_resena(foto=True)
        self.s3.fail_on('put_object', FakeClientError('InternalError', 500, 'boom'), prefix='resenas/fotos/')
        r = self.accion(rid, 'aprobar')
        self.assertEqual((r['statusCode'], datos(r)['error']), (503, 'foto_no_publicada'))
        self.assertEqual(self.estado(rid), 'pending')
        self.assertNotIn(foto_de(rid), self.s3.objects)

    def test_export_pendiente_responde_202_y_exportar_lo_repara(self):
        rid = self.sembrar_resena(status='pending')
        self.cf.fallar_proximo()
        r = self.accion(rid, 'aprobar')
        self.assertEqual(r['statusCode'], 202)
        self.assertEqual(datos(r), {'ok': True, 'estado': 'approved', 'export': 'pendiente', 'reintentar': True})
        self.assertEqual(self.estado(rid), 'approved')
        r2 = self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        self.assertEqual(datos(r2), {'ok': True, 'export': 'actualizado', 'publicadas': 1})
        self.assertEqual([i['id'] for i in self.items_publicos()], [rid])


class ConcurrenciaTest(AdminSesionCase):
    """Dos moderaciones sobre la misma reseña: solo una gana; la otra recibe conflicto y no sobrescribe."""

    def preparar_concurrencia(self, rid, ajeno):
        nuevo = FakeS3Concurrente(f'resenas/{rid}.json', ajeno)
        nuevo.objects = dict(self.s3.objects)
        self.s3 = nuevo
        self.svc = self.servicio(nuevo)
        return nuevo

    def test_dos_moderaciones_solo_gana_una_y_la_otra_recibe_conflicto(self):
        rid = self.sembrar_resena(status='pending', foto=True)
        resultados_ajenos = []

        def rechazar_en_paralelo(s3_):
            otro = self.servicio(s3_)
            resultados_ajenos.append(otro.atender('POST', f'/admin/api/resenas/{rid}/rechazar', {},
                                                  dict(CABECERAS_CSRF), self.cookies, b''))

        s3 = self.preparar_concurrencia(rid, rechazar_en_paralelo)
        r = self.pedir('POST', f'/admin/api/resenas/{rid}/aprobar', cookies=self.cookies)
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'conflicto_concurrencia'))
        self.assertEqual(resultados_ajenos[0]['status'], 200)
        self.assertEqual(json.loads(s3.objects[f'resenas/{rid}.json'])['status'], 'rejected')
        self.assertNotIn(foto_de(rid), s3.objects)  # la copia pública de la perdedora se retiró
        self.assertNotIn('resenas/publicas.json', s3.objects)

    def test_escritura_ajena_no_se_pisa_con_la_copia_vieja(self):
        rid = self.sembrar_resena(status='pending')

        def editar(s3_):
            resena = json.loads(s3_.objects[f'resenas/{rid}.json'])
            resena['private_feedback'] = 'Editada por otro moderador'
            s3_.put_object(Bucket=PRIVADO, Key=f'resenas/{rid}.json', Body=json.dumps(resena))

        s3 = self.preparar_concurrencia(rid, editar)
        r = self.pedir('POST', f'/admin/api/resenas/{rid}/aprobar', cookies=self.cookies)
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'conflicto_concurrencia'))
        final = json.loads(s3.objects[f'resenas/{rid}.json'])
        self.assertEqual((final['status'], final['private_feedback']), ('pending', 'Editada por otro moderador'))
        self.assertNotIn('resenas/publicas.json', s3.objects)


class ExportTest(AdminSesionCase):
    def items(self):
        return json.loads(self.s3.objects['resenas/publicas.json'])['items']

    def test_exportar_regenera_y_responde_publicadas(self):
        self.sembrar_resena(status='approved', published_at='2026-10-19T09:00:00Z')
        r = self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        self.assertEqual(datos(r), {'ok': True, 'export': 'actualizado', 'publicadas': 1})

    def test_lock_vigente_responde_503_export_ocupado(self):
        self.s3.objects[CLAVE_LOCK] = bloqueo_vigente()
        r = self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        self.assertEqual((r['statusCode'], datos(r)['error'], datos(r)['reintentar']), (503, 'export_ocupado', True))
        self.assertEqual(json.loads(self.s3.objects[CLAVE_LOCK])['duenio'], 'otro')

    def test_lock_vencido_no_bloquea(self):
        self.s3.objects[CLAVE_LOCK] = bloqueo_vigente(minutos=-1)
        self.assertEqual(self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)['statusCode'], 200)
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)

    def test_fallo_de_invalidacion_responde_503_reintentable_y_libera_el_lock(self):
        self.cf.fallar_proximo()
        r = self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        self.assertEqual((r['statusCode'], datos(r)['error'], datos(r)['reintentar']), (503, 'export_fallido', True))
        self.assertNotIn(CLAVE_LOCK, self.s3.objects)

    def test_exportar_repara_fotos_publicas_huerfanas(self):
        rid = self.sembrar_resena(status='rejected')
        self.s3.objects[foto_de(rid)] = JPEG
        self.assertEqual(self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)['statusCode'], 200)
        self.assertNotIn(foto_de(rid), self.s3.objects)

    def test_exportar_dos_veces_produce_el_mismo_contenido(self):
        self.sembrar_resena(status='approved', published_at='2026-10-19T09:00:00Z')
        self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        primero = self.s3.objects['resenas/publicas.json']
        self.pedir('POST', '/admin/api/exportar', cookies=self.cookies)
        self.assertEqual(self.s3.objects['resenas/publicas.json'], primero)

    def test_exportar_requiere_sesion_y_csrf(self):
        self.assertEqual(self.pedir('POST', '/admin/api/exportar')['statusCode'], 401)
        r = self.pedir('POST', '/admin/api/exportar', cookies=self.cookies,
                       cabeceras={'origin': ORIGEN, 'content-type': 'application/json'})
        self.assertEqual(r['statusCode'], 403)


class ReenvioTest(AdminSesionCase):
    def reenviar(self, rid):
        return self.pedir('POST', f'/admin/api/resenas/{rid}/reenviar-aviso', cookies=self.cookies)

    def test_envia_el_aviso_y_lo_registra(self):
        rid = self.sembrar_resena(status='pending')
        r = self.reenviar(rid)
        self.assertEqual((r['statusCode'], datos(r)), (200, {'ok': True, 'aviso': 'enviado'}))
        self.assertEqual(len(self.ses.enviados), 1)
        self.assertIn(f'notificaciones/{rid}.json', self.s3.objects)

    def test_segundo_reenvio_responde_409_ya_enviado(self):
        rid = self.sembrar_resena(status='pending')
        self.reenviar(rid)
        r = self.reenviar(rid)
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'ya_enviado'))
        self.assertEqual(len(self.ses.enviados), 1)

    def test_ses_caido_responde_503_y_no_registra_el_aviso(self):
        rid = self.sembrar_resena(status='pending')
        self.ses.fallar_proximo()
        r = self.reenviar(rid)
        self.assertEqual((r['statusCode'], datos(r)['error']), (503, 'aviso_fallido'))
        self.assertNotIn(f'notificaciones/{rid}.json', self.s3.objects)
        self.assertEqual(self.estado_de(rid), 'pending')

    def test_solo_aplica_a_pendientes(self):
        rid = self.sembrar_resena(status='approved')
        r = self.reenviar(rid)
        self.assertEqual((r['statusCode'], datos(r)['error']), (409, 'estado_incompatible'))
        self.assertEqual(self.ses.enviados, [])

    def test_el_correo_no_trae_datos_privados(self):
        rid = self.sembrar_resena(status='pending', foto=True)
        self.reenviar(rid)
        correo = self.ses.enviados[0]
        texto = correo['text'] + correo['html']
        self.assertIn(f'{BASE}/admin/resenas/{rid}', texto)
        self.assertEqual(correo['subject'], 'Nueva reseña ORK3D · ★★★★★')
        th = hashlib.sha256(f'token-de-{rid}'.encode('utf-8')).hexdigest()
        for prohibido in [PEDIDO, FEEDBACK, CONTACTO, th, 'tokens/', 'pendientes/', 'order_ref']:
            self.assertNotIn(prohibido, texto)

    def estado_de(self, rid):
        return self.resena(rid)['status']


class RespuestasTest(AdminSesionCase):
    def test_respuestas_json_llevan_cabeceras_de_seguridad(self):
        r = self.pedir('GET', '/admin/api/sesion', cookies=self.cookies)
        self.assertEqual(r['headers']['Content-Type'], 'application/json; charset=utf-8')
        self.assertEqual(r['headers']['Cache-Control'], 'no-store')
        self.assertEqual(r['headers']['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(r['headers']['Referrer-Policy'], 'no-referrer')

    def test_error_interno_responde_500_sin_detalle(self):
        rid = self.sembrar_resena()
        self.s3.fail_on('get_object', RuntimeError('secreto-interno-123'), prefix='resenas/')
        r = self.pedir('GET', f'/admin/api/resenas/{rid}', cookies=self.cookies)
        self.assertEqual(r['statusCode'], 500)
        self.assertEqual(datos(r), {'ok': False, 'error': 'interno', 'mensaje': 'Error interno.'})
        self.assertNotIn('secreto-interno-123', r['body'])


if __name__ == '__main__':
    unittest.main()
