"""API administrativa de reseñas (V2). Lambda `ork3d-resenas-admin`, Python 3.12.

Rutas (mismo origen vía CloudFront en /admin/api/*):
  POST /admin/api/sesion/solicitar      envía el enlace de acceso al correo fijo
  POST /admin/api/sesion/confirmar      consume el código y abre la sesión (cookie)
  GET  /admin/api/sesion                estado de la sesión
  POST /admin/api/sesion/cerrar         cierra la sesión
  POST /admin/api/invitaciones          crea token e invitación (mismas reglas que la CLI)
  GET  /admin/api/resenas               bandeja (?estado=pending|approved|rejected|hidden|all)
  GET  /admin/api/resenas/{id}          detalle administrativo
  GET  /admin/api/resenas/{id}/foto     bytes de la foto privada de esa reseña (no-store)
  POST /admin/api/resenas/{id}/aprobar|rechazar|ocultar|restaurar|reenviar-aviso
  POST /admin/api/exportar              regenera resenas/publicas.json e invalida CloudFront

Nunca se devuelven token, token_hash, claves S3, cookies ni datos AWS. Los GET no escriben
ni borran nada. Las mutaciones exigen sesión y cabeceras CSRF (ver nucleo/sesion.py).
"""
import base64
import datetime as _dt
import json
import os
import re
import time
import uuid
from urllib.parse import parse_qs

from nucleo import bloqueo as _bloqueo
from nucleo import correo as _correo
from nucleo import errores
from nucleo import publicacion as _publicacion
from nucleo import registro as _registro
from nucleo import reglas as _reglas
from nucleo import sesion as _sesion
from nucleo import tokens as _tokens
from nucleo.almacen import Almacen

MAX_CUERPO = 8192
ID_RE = re.compile(r'[a-f0-9]{32}')
_HASH_RE = re.compile(r'[a-f0-9]{64}')
ESTADOS_FILTRO = ('pending', 'approved', 'rejected', 'hidden', 'all')
GRUPO_ORDEN = {'pending': 0, 'approved': 1, 'hidden': 2, 'rejected': 3}
ACCIONES_RESENA = ('aprobar', 'rechazar', 'ocultar', 'restaurar', 'reenviar-aviso')
CABECERAS_BASE = {
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
}


def _ahora():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


class CloudFrontInvalidador:
    """Invalidación de CloudFront (create_invalidation). Recibe un cliente boto3 'cloudfront'."""

    def __init__(self, cliente, distribucion_id):
        self.cliente = cliente
        self.distribucion_id = distribucion_id

    def invalidar(self, rutas):
        return self.cliente.create_invalidation(
            DistributionId=self.distribucion_id,
            InvalidationBatch={'Paths': {'Quantity': len(rutas), 'Items': list(rutas)},
                               'CallerReference': uuid.uuid4().hex},
        )


def _respuesta(status, cuerpo=None, *, cookies=None, binario=None, tipo=None):
    return {'status': status, 'cuerpo': cuerpo, 'cookies': cookies or [],
            'binario': binario, 'tipo': tipo}


def _error(status, codigo, mensaje, **extra):
    return _respuesta(status, {'ok': False, 'error': codigo, 'mensaje': mensaje, **extra})


class ServicioAdmin:
    def __init__(self, s3, *, bucket_privado, bucket_sitio, origen_permitido, base_url,
                 correo_svc, correo_origen, correo_destino, invalidador,
                 horas=8, minutos_login=10, limite_login=3, ventana_login=15,
                 dias_token=_tokens.DIAS_POR_DEFECTO, bloqueo_s=60,
                 ahora=_ahora, espera=time.sleep):
        self.almacen = Almacen(s3)
        self.bucket = bucket_privado
        self.sitio = bucket_sitio
        self.origen = origen_permitido
        self.base_url = base_url.rstrip('/')
        self.correo_svc = correo_svc
        self.correo_origen = correo_origen
        self.correo_destino = correo_destino
        self.horas = horas
        self.dias_token = dias_token
        self.ahora = ahora
        self.sesiones = _sesion.Sesiones(self.almacen, bucket_privado, ahora, horas)
        self.logins = _sesion.Logins(self.almacen, bucket_privado, ahora, minutos_login)
        self.limite = _sesion.LimiteAcceso(self.almacen, bucket_privado, ahora, limite_login,
                                           ventana_login)
        self.publicador = _publicacion.Publicador(
            self.almacen, bucket_privado, bucket_sitio, invalidador,
            _bloqueo.Bloqueo(self.almacen, bucket_privado, 'export', bloqueo_s, ahora),
            ahora, espera=espera)

    # ── Entrada ─────────────────────────────────────────────────────────────
    def atender(self, metodo, ruta, query, cabeceras, cookies, cuerpo):
        """Punto de entrada único. Devuelve un dict de respuesta (ver _respuesta)."""
        if len(cuerpo or b'') > MAX_CUERPO:
            return _error(413, 'demasiado_grande', 'El cuerpo supera los 8 KB.')
        try:
            return self._despachar(metodo, ruta, query, cabeceras, cookies, cuerpo)
        except errores.ErrorValidacion as exc:
            return _error(400, 'validacion', 'Revisá los datos.', campos=exc.campos)
        except errores.ErrorJson:
            return _error(400, 'json', 'El cuerpo no es un JSON válido.')
        except errores.ErrorSesion:
            return _error(401, 'no_autenticado', 'Tenés que iniciar sesión.')
        except errores.ErrorCsrf as exc:
            return _error(exc.http, exc.motivo, 'La solicitud no es válida.')
        except errores.ErrorCodigo:
            return _error(401, 'codigo_invalido', 'El enlace no es válido o venció.')
        except errores.ErrorNoEncontrado:
            return _error(404, 'no_encontrado', 'No existe.')
        except errores.ErrorEstado as exc:
            return _error(409, 'estado_incompatible', 'Esa acción no aplica al estado actual.',
                          estado_actual=exc.estado_actual)
        except errores.ErrorSinPermiso:
            return _error(409, 'sin_permiso', 'La reseña no tiene permiso para publicar.')
        except errores.ErrorConflicto:
            return _error(409, 'conflicto_concurrencia',
                          'La reseña cambió mientras la revisabas. Recargá y volvé a intentar.')
        except errores.ErrorBloqueado:
            return _error(503, 'export_ocupado', 'El export se está regenerando. Reintentá en unos segundos.',
                          reintentar=True)
        except Exception as exc:  # sin detalle hacia afuera ni en el log
            _registro.evento('error_interno', ruta=_registro.ruta_plantilla(ruta),
                             error=type(exc).__name__)
            return _error(500, 'interno', 'Error interno.')

    def _despachar(self, metodo, ruta, query, cabeceras, cookies, cuerpo):
        if not ruta.startswith('/admin/api/'):
            return _error(404, 'no_encontrado', 'No existe.')
        partes = ruta[len('/admin/api/'):].strip('/').split('/')

        if partes == ['sesion', 'solicitar']:
            return self._only(metodo, 'POST', self._solicitar, cabeceras, cuerpo)
        if partes == ['sesion', 'confirmar']:
            return self._only(metodo, 'POST', self._confirmar, cabeceras, cuerpo)
        if partes == ['sesion']:
            return self._only(metodo, 'GET', self._sesion_estado, cabeceras, cookies)
        if partes == ['sesion', 'cerrar']:
            return self._only(metodo, 'POST', self._cerrar, cabeceras, cookies)
        if partes == ['invitaciones']:
            return self._only(metodo, 'POST', self._invitacion, cabeceras, cookies, cuerpo)
        if partes == ['resenas']:
            return self._only(metodo, 'GET', self._listar, cabeceras, cookies, query)
        if partes == ['exportar']:
            return self._only(metodo, 'POST', self._exportar, cabeceras, cookies)
        if len(partes) >= 2 and partes[0] == 'resenas' and ID_RE.fullmatch(partes[1]):
            resena_id = partes[1]
            if len(partes) == 2:
                return self._only(metodo, 'GET', self._detalle, cabeceras, cookies, resena_id)
            if partes[2:] == ['foto']:
                return self._only(metodo, 'GET', self._foto, cabeceras, cookies, resena_id)
            if len(partes) == 3 and partes[2] in ACCIONES_RESENA:
                if partes[2] == 'reenviar-aviso':
                    return self._only(metodo, 'POST', self._reenviar, cabeceras, cookies, resena_id)
                return self._only(metodo, 'POST', self._moderar, cabeceras, cookies, resena_id,
                                  partes[2])
        return _error(404, 'no_encontrado', 'No existe.')

    @staticmethod
    def _only(metodo, esperado, fn, *args):
        if metodo != esperado:
            return _error(405, 'metodo', 'Método no permitido.')
        return fn(*args)

    # ── Auth y CSRF ───────────────────────────────────────────────────────────
    def _exigir_sesion(self, cabeceras, cookies):
        secreto = _sesion.leer_cookie(cookies, cabeceras)
        if self.sesiones.validar(secreto) is None:
            raise errores.ErrorSesion()
        return secreto

    def _csrf(self, cabeceras):
        _sesion.comprobar_mutacion(cabeceras, self.origen)

    def _cuerpo_json(self, cuerpo, admite_vacio=True):
        if not cuerpo:
            return {} if admite_vacio else None
        try:
            datos = json.loads(cuerpo.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise errores.ErrorJson()
        if not isinstance(datos, dict):
            raise errores.ErrorJson()
        return datos

    def _solicitar(self, cabeceras, cuerpo):
        self._csrf(cabeceras)
        if self._cuerpo_json(cuerpo):
            raise errores.ErrorValidacion({'_': 'No se aceptan campos en esta solicitud.'})
        if self.limite.permitir():
            codigo = self.logins.emitir()
            enlace = f'{self.base_url}/admin/entrar#c={codigo}'
            correo_acceso = _correo.construir_acceso(enlace, self.correo_origen, self.correo_destino)
            try:
                self.correo_svc.enviar(correo_acceso)
                resultado = 'enviado'
            except Exception as exc:
                _registro.evento('aviso_fallido', op='acceso', error=type(exc).__name__)
                resultado = 'fallido'
        else:
            resultado = 'limitado'
        _registro.evento('login_solicitado', resultado=resultado)
        # Respuesta uniforme: nunca revela si el correo salió, si hubo límite o si hubo error.
        return _respuesta(202, {'ok': True})

    def _confirmar(self, cabeceras, cuerpo):
        self._csrf(cabeceras)
        datos = self._cuerpo_json(cuerpo)
        try:
            self.logins.consumir(datos.get('codigo'))
        except errores.ErrorCodigo as exc:
            _registro.evento('login_rechazado', resultado=str(exc.args[0]))
            raise
        secreto, expira = self.sesiones.crear()
        _registro.evento('login_confirmado', resultado='ok')
        return _respuesta(200, {'ok': True, 'expira': expira},
                          cookies=[_sesion.cookie_sesion(secreto, self.horas)])

    def _sesion_estado(self, cabeceras, cookies):
        secreto = _sesion.leer_cookie(cookies, cabeceras)
        expira = self.sesiones.validar(secreto)
        if expira is None:
            raise errores.ErrorSesion()
        return _respuesta(200, {'ok': True, 'expira': expira})

    def _cerrar(self, cabeceras, cookies):
        secreto = self._exigir_sesion(cabeceras, cookies)
        self._csrf(cabeceras)
        self.sesiones.cerrar(secreto)
        _registro.evento('sesion_cerrada')
        return _respuesta(200, {'ok': True}, cookies=[_sesion.cookie_expirada()])

    # ── Invitaciones ───────────────────────────────────────────────────────────
    def _invitacion(self, cabeceras, cookies, cuerpo):
        self._exigir_sesion(cabeceras, cookies)
        self._csrf(cabeceras)
        datos = _tokens.validar_invitacion(self._cuerpo_json(cuerpo), self.dias_token)
        token, registro_token = _tokens.crear_token(self.almacen, self.bucket, datos, self.ahora())
        _registro.evento('invitacion', resultado='creada')
        return _respuesta(201, {'ok': True, 'enlace': f'{self.base_url}/opinar/{token}',
                                'vence': registro_token['expires_at'],
                                'producto_label': registro_token['product_label'],
                                'categoria': registro_token['category']})

    # ── Lectura ────────────────────────────────────────────────────────────────
    def _todas(self):
        resenas = []
        for clave in self.almacen.listar(self.bucket, 'resenas/'):
            if not re.fullmatch(r'resenas/[a-f0-9]{32}\.json', clave):
                continue
            obj, _ = self.almacen.leer_json(self.bucket, clave)
            if obj is not None:
                resenas.append(obj)
        return resenas

    def _notificados(self):
        ids = set()
        for clave in self.almacen.listar(self.bucket, 'notificaciones/'):
            m = re.fullmatch(r'notificaciones/([a-f0-9]{32})\.json', clave)
            if m:
                ids.add(m.group(1))
        return ids

    @staticmethod
    def _fila(resena, notificados):
        if resena['id'] in notificados:
            aviso = 'enviado'
        elif resena.get('status') == 'pending':
            aviso = 'pendiente'
        else:
            aviso = 'no_aplica'
        return {
            'id': resena['id'],
            'rating': resena.get('rating'),
            'display_name': resena.get('display_name') or _publicacion.NOMBRE_ANONIMO,
            'product_label': resena.get('product_label'),
            'category': resena.get('category'),
            'created_at': resena.get('created_at'),
            'status': resena.get('status'),
            'verified': bool(resena.get('verified')),
            'tiene_foto': bool(resena.get('photo_key')),
            'aviso': aviso,
        }

    def _listar(self, cabeceras, cookies, query):
        self._exigir_sesion(cabeceras, cookies)
        estado = (query.get('estado') or ['all'])[0]
        if estado not in ESTADOS_FILTRO:
            raise errores.ErrorValidacion({'estado': 'Filtro inválido.'})
        notificados = self._notificados()
        filas = [self._fila(r, notificados) for r in self._todas()
                 if estado == 'all' or r.get('status') == estado]
        filas.sort(key=lambda f: f['created_at'] or '', reverse=True)
        filas.sort(key=lambda f: GRUPO_ORDEN.get(f['status'], 9))  # estable: conserva el orden previo
        return _respuesta(200, {'ok': True, 'estado': estado, 'total': len(filas), 'resenas': filas})

    def _cargar(self, resena_id):
        obj, etag = self.almacen.leer_json(self.bucket, f'resenas/{resena_id}.json')
        if obj is None:
            raise errores.ErrorNoEncontrado(resena_id)
        return obj, etag

    def _detalle(self, cabeceras, cookies, resena_id):
        self._exigir_sesion(cabeceras, cookies)
        resena, _ = self._cargar(resena_id)
        pedido = None
        if _HASH_RE.fullmatch(resena.get('token_hash') or ''):
            token, _ = self.almacen.leer_json(self.bucket, f"tokens/{resena['token_hash']}.json")
            pedido = (token or {}).get('order_ref')
        vista = {
            **self._fila(resena, self._notificados()),
            'display_mode': resena.get('display_mode'),
            'product_slug': resena.get('product_slug'),
            'comment': resena.get('comment') or '',
            'aspects': resena.get('aspects') or [],
            'moderated_at': resena.get('moderated_at'),
            'published_at': resena.get('published_at'),
            'permission_to_publish': resena.get('permission_to_publish') is True,
            'private_feedback': resena.get('private_feedback') or '',
            'contact_requested': bool(resena.get('contact_requested')),
            'contact': resena.get('contact'),
            'pedido': pedido,
        }
        return _respuesta(200, {'ok': True, 'resena': vista})

    def _foto(self, cabeceras, cookies, resena_id):
        self._exigir_sesion(cabeceras, cookies)
        resena, _ = self._cargar(resena_id)
        clave = resena.get('photo_key')
        if not clave or not _publicacion.clave_foto_pertenece(clave, resena.get('token_hash', '')):
            raise errores.ErrorNoEncontrado('foto')
        datos = self.almacen.leer_bytes(self.bucket, clave)
        if datos is None:
            raise errores.ErrorNoEncontrado('foto')
        return _respuesta(200, binario=datos, tipo='image/jpeg')

    # ── Moderación ─────────────────────────────────────────────────────────────
    def _moderar(self, cabeceras, cookies, resena_id, accion):
        self._exigir_sesion(cabeceras, cookies)
        self._csrf(cabeceras)
        resena, etag = self._cargar(resena_id)
        estado_antes = resena.get('status')
        nuevo_estado = _reglas.destino(accion, estado_antes)
        _reglas.exige_permiso(accion, resena)
        ahora = _iso(self.ahora())

        clave_publica = None
        if accion in ('aprobar', 'restaurar') and resena.get('photo_key'):
            try:
                clave_publica = _publicacion.publicar_foto(
                    self.almacen, self.bucket, self.sitio, resena_id,
                    resena.get('token_hash', ''), resena['photo_key'])
            except errores.ErrorFoto:
                return _error(409, 'foto_invalida', 'La foto no se puede publicar. Revisala antes.')
            except Exception as exc:  # S3: nada cambió todavía
                _registro.evento('error_interno', ruta=_registro.ruta_plantilla(f'/resenas/{resena_id}/{accion}'),
                                 error=type(exc).__name__)
                return _error(503, 'foto_no_publicada', 'No pudimos publicar la foto. Probá de nuevo.')

        nueva = dict(resena)
        nueva['status'] = nuevo_estado
        nueva['moderated_at'] = ahora
        if accion == 'aprobar':
            nueva['published_at'] = ahora
        elif accion == 'restaurar':
            nueva['published_at'] = resena.get('published_at') or ahora
        nueva['photo_public_key'] = clave_publica if accion in ('aprobar', 'restaurar') else None

        try:
            self.almacen.escribir_json(self.bucket, f'resenas/{resena_id}.json', nueva, etag=etag)
        except errores.ErrorConflicto:
            if clave_publica:
                self._retirar_copia_si_no_vigente(resena_id, clave_publica)
            raise

        if accion in ('rechazar', 'ocultar') and resena.get('photo_public_key'):
            try:
                _publicacion.quitar_foto_publica(self.almacen, self.sitio, resena_id)
            except Exception as exc:  # el export siguiente repara fotos huérfanas
                _registro.evento('error_interno', ruta='/resenas/{id}/foto-publica',
                                 error=type(exc).__name__)

        export = 'actualizado'
        if _reglas.cambia_export(accion, estado_antes):
            export = self._regenerar_export()

        _registro.evento('moderacion', review_id=resena_id, op=accion, estado=nuevo_estado,
                         resultado='ok')
        cuerpo = {'ok': True, 'estado': nuevo_estado, 'export': export}
        if export == 'pendiente':
            cuerpo['reintentar'] = True
        return _respuesta(200 if export == 'actualizado' else 202, cuerpo)

    def _retirar_copia_si_no_vigente(self, resena_id, clave_publica):
        """Un conflicto dejó una copia pública que la reseña ya no referencia: se borra."""
        actual, _ = self.almacen.leer_json(self.bucket, f'resenas/{resena_id}.json')
        vigente = (actual and actual.get('status') == 'approved'
                   and actual.get('photo_public_key') == clave_publica)
        if not vigente:
            _publicacion.quitar_foto_publica(self.almacen, self.sitio, resena_id)

    def _regenerar_export(self):
        try:
            resultado = self.publicador.regenerar()
        except Exception as exc:
            _registro.evento('export', resultado='pendiente', error=type(exc).__name__)
            return 'pendiente'
        _registro.evento('export', resultado='actualizado', count=resultado['publicadas'])
        return 'actualizado'

    def _exportar(self, cabeceras, cookies):
        self._exigir_sesion(cabeceras, cookies)
        self._csrf(cabeceras)
        try:
            resultado = self.publicador.regenerar()
        except errores.ErrorBloqueado:
            raise
        except Exception as exc:
            _registro.evento('export', resultado='fallido', error=type(exc).__name__)
            return _respuesta(503, {'ok': False, 'error': 'export_fallido', 'reintentar': True,
                                    'mensaje': 'No pudimos regenerar el export. Reintentá.'})
        _registro.evento('export', resultado='actualizado', count=resultado['publicadas'])
        return _respuesta(200, {'ok': True, 'export': 'actualizado',
                                'publicadas': resultado['publicadas']})

    def _reenviar(self, cabeceras, cookies, resena_id):
        self._exigir_sesion(cabeceras, cookies)
        self._csrf(cabeceras)
        resena, _ = self._cargar(resena_id)
        if resena.get('status') != 'pending':
            raise errores.ErrorEstado('reenviar-aviso', resena.get('status'))
        if self.almacen.existe(self.bucket, f'notificaciones/{resena_id}.json'):
            return _error(409, 'ya_enviado', 'El aviso ya salió.')
        ok = _correo.notificar_resena(self.correo_svc, self.almacen, self.bucket, resena,
                                      base_url=self.base_url, origen=self.correo_origen,
                                      destino=self.correo_destino, ahora=self.ahora())
        if not ok:
            return _error(503, 'aviso_fallido', 'No pudimos enviar el aviso. Probá de nuevo.')
        return _respuesta(200, {'ok': True, 'aviso': 'enviado'})


# ── Adaptación a Function URL (Lambda) y a otros servidores ──────────────────────

def a_respuesta_http(r):
    """Convierte la respuesta interna al formato de respuesta de la Function URL."""
    headers = dict(CABECERAS_BASE)
    if r['binario'] is not None:
        headers['Content-Type'] = r['tipo'] or 'application/octet-stream'
        return {'statusCode': r['status'], 'headers': headers,
                'body': base64.b64encode(r['binario']).decode('ascii'), 'isBase64Encoded': True,
                'cookies': r['cookies']}
    headers['Content-Type'] = 'application/json; charset=utf-8'
    cuerpo = '' if r['cuerpo'] is None else json.dumps(r['cuerpo'], ensure_ascii=False)
    return {'statusCode': r['status'], 'headers': headers, 'body': cuerpo,
            'isBase64Encoded': False, 'cookies': r['cookies']}


def evento_a_entrada(event):
    """Normaliza el evento de la Function URL (o de un servidor de desarrollo)."""
    http = (event.get('requestContext') or {}).get('http') or {}
    metodo = (http.get('method') or event.get('httpMethod') or 'GET').upper()
    ruta = (event.get('rawPath') or event.get('path') or '/').rstrip('/') or '/'
    query = parse_qs(event.get('rawQueryString') or '')
    cabeceras = {str(k).lower(): v for k, v in (event.get('headers') or {}).items()}
    cookies = event.get('cookies') or []
    crudo = event.get('body') or ''
    if event.get('isBase64Encoded') and crudo:
        cuerpo = base64.b64decode(crudo)
    else:
        cuerpo = crudo.encode('utf-8') if isinstance(crudo, str) else crudo
    return metodo, ruta, query, cabeceras, cookies, cuerpo


# ── Entrada Lambda ──────────────────────────────────────────────────────────────

_SVC = None


def _servicio():
    global _SVC
    if _SVC is None:
        import boto3  # disponible en el runtime de Lambda; no se importa en los tests
        from botocore.config import Config
        cfg = Config(connect_timeout=3, read_timeout=5, retries={'max_attempts': 2})
        _SVC = ServicioAdmin(
            boto3.client('s3', config=cfg),
            bucket_privado=os.environ['RESENAS_BUCKET'],
            bucket_sitio=os.environ['RESENAS_SITE_BUCKET'],
            origen_permitido=os.environ.get('ALLOWED_ORIGIN', 'https://ork3d.com'),
            base_url=os.environ['RESENAS_ADMIN_BASE_URL'],
            correo_svc=_correo.SESCorreo(boto3.client('ses', config=cfg)),
            correo_origen=os.environ['RESENAS_FROM_EMAIL'],
            correo_destino=os.environ['RESENAS_ADMIN_EMAIL'],
            invalidador=CloudFrontInvalidador(boto3.client('cloudfront'),
                                              os.environ['RESENAS_DISTRIBUTION_ID']),
            horas=int(os.environ.get('RESENAS_SESION_HORAS', '8')),
            minutos_login=int(os.environ.get('RESENAS_LOGIN_MINUTOS', '10')),
            limite_login=int(os.environ.get('RESENAS_LOGIN_LIMITE', '3')),
            ventana_login=int(os.environ.get('RESENAS_LOGIN_VENTANA_MIN', '15')),
            dias_token=int(os.environ.get('RESENAS_TOKEN_DAYS', str(_tokens.DIAS_POR_DEFECTO))),
        )
    return _SVC


def handler(event, context=None):
    svc = _servicio()
    metodo, ruta, query, cabeceras, cookies, cuerpo = evento_a_entrada(event)
    return a_respuesta_http(svc.atender(metodo, ruta, query, cabeceras, cookies, cuerpo))
