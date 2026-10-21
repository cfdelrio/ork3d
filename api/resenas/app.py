"""ORK3D · Reseñas · API pública (AWS Lambda, Python 3.12).

Rutas (Function URL, sin prefijo):
  GET  /tokens/{token}  → estado del token (ok | invalido | vencido | usado | revocado)
  POST /presign         → presigned POST para subir una foto a pendientes/
  POST /resenas         → crea la reseña en estado "pending" y consume el token

Datos: bucket privado (RESENAS_BUCKET). Ver docs/resenas/README.md y V2-ADMIN.md.
Logs: solo review_id, operación y resultado (nucleo/registro.py). Nunca un token, su hash
(ni prefijo), un comentario, feedback privado, contacto o correo.
Notificación (V2): si RESENAS_NOTIFY_ENABLED es "true", tras guardar una reseña pending se
envía un aviso al moderador. Es un efecto secundario: si falla, la reseña queda guardada.
boto3 viene incluido en el runtime de Lambda; los tests usan fakes (api/resenas/fakes.py).
"""
import base64
import datetime as _dt
import json
import os
import re
import uuid

from nucleo import correo as _correo
from nucleo import registro as _registro
from nucleo import tokens as _tokens
from nucleo.almacen import Almacen
from nucleo.jpeg import jpeg_sin_metadatos  # noqa: F401  (V1 la exponía desde este módulo)

MAX_BODY_BYTES = 8192
TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32}')
PRESIGN_TTL = 600
PHOTO_MIN_BYTES = 15_000
PHOTO_MAX_BYTES = 2_000_000
PHOTO_EXT = {'jpg': 'image/jpeg'}  # solo JPEG: el navegador recodifica a JPEG y el export público es .jpg
ASPECTS = {'diseno', 'personalizacion', 'calidad', 'fecha', 'atencion', 'resultado'}
DISPLAY_MODES = {'inicial', 'nombre', 'anonimo'}
ANON_NAME = 'Cliente de ORK3D'
LIMITS = {'comment': 600, 'private_feedback': 800, 'name': 40, 'contact': 120}
NAME_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{1,39}")
PHOTO_KEY_RE = re.compile(r'pendientes/([0-9a-f]{64})/([0-9a-f]{32})\.jpg')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

token_hash = _tokens.hash_token  # V1: mismo nombre, misma regla (sha256 hex del token)
_log = _registro.evento


class Invalid(Exception):
    def __init__(self, campos):
        super().__init__('validacion')
        self.campos = campos


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(s):
    return _dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_dt.timezone.utc)


def _error_code(exc):
    return (getattr(exc, 'response', None) or {}).get('Error', {}).get('Code', '')


def _status_code(exc):
    return ((getattr(exc, 'response', None) or {}).get('ResponseMetadata', {}) or {}).get('HTTPStatusCode')


def _is_missing(exc):
    return _error_code(exc) in ('NoSuchKey', 'NotFound', '404')


def _is_precondition(exc):
    return _error_code(exc) in ('PreconditionFailed', 'ConditionalRequestConflict') or _status_code(exc) in (409, 412)


# ── Validación ────────────────────────────────────────────────────────────

def _text(p, key, max_len, campos, multiline=True, required=False):
    value = p.get(key)
    if value is None:
        value = ''
    if not isinstance(value, str):
        campos[key] = 'Formato inválido.'
        return ''
    s = value.replace('\r\n', '\n').replace('\r', '\n')
    s = _CONTROL.sub('', s)
    s = s.strip() if multiline else ' '.join(s.split())
    if required and not s:
        campos[key] = 'Este campo es obligatorio.'
    elif len(s) > max_len:
        campos[key] = f'Máximo {max_len} caracteres.'
    return s


def display_name_for(mode, name):
    if mode == 'anonimo':
        return ANON_NAME
    parts = name.split()
    first = parts[0][:40]
    apellidos = [p for p in parts[1:] if p[0].isalpha()]
    if mode == 'nombre' or not apellidos:
        return first
    return f'{first} {apellidos[-1][0].upper()}.'


def parse_review(p):
    """Valida el payload del formulario. Devuelve un dict limpio o lanza Invalid(campos)."""
    campos = {}
    if not isinstance(p, dict):
        raise Invalid({'_': 'Formato inválido.'})

    rating = p.get('rating')
    if isinstance(rating, str) and rating.isdigit() and len(rating) <= 2:
        rating = int(rating)
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        campos['rating'] = 'Elegí entre 1 y 5 estrellas.'

    comment = _text(p, 'comment', LIMITS['comment'], campos)
    private_feedback = _text(p, 'private_feedback', LIMITS['private_feedback'], campos)

    aspects = p.get('aspects')
    if aspects is None:
        aspects = []
    if not isinstance(aspects, list) or len(aspects) > 6 or not all(isinstance(a, str) and a in ASPECTS for a in aspects):
        campos['aspects'] = 'Opción inválida.'
        aspects = []
    aspects = sorted(set(aspects))

    mode = p.get('display_mode', 'inicial')
    if not isinstance(mode, str) or mode not in DISPLAY_MODES:
        campos['display_mode'] = 'Opción inválida.'
        mode = 'inicial'

    name = _text(p, 'name', LIMITS['name'], campos, multiline=False)
    display_name = ANON_NAME if mode == 'anonimo' else None
    if mode != 'anonimo' and 'name' not in campos:
        if not name or not NAME_RE.fullmatch(name):
            campos['name'] = 'Poné tu nombre (solo letras).'
        else:
            display_name = display_name_for(mode, name)

    if p.get('permission_to_publish') is not True:
        campos['permission_to_publish'] = 'Necesitamos tu autorización para publicar la reseña.'

    photo_key = p.get('photo_key') or None
    if photo_key is not None and (not isinstance(photo_key, str) or len(photo_key) > 200):
        campos['photo_key'] = 'Formato inválido.'
        photo_key = None

    contact_requested = p.get('contact_requested') is True
    contact = None
    if contact_requested:
        contact = _text(p, 'contact', LIMITS['contact'], campos, multiline=False, required=True) or None

    if p.get('website'):  # honeypot: lo maneja create_review antes de validar
        raise Invalid({'_honeypot': True})
    if campos:
        raise Invalid(campos)

    return {
        'rating': int(rating), 'comment': comment, 'aspects': aspects,
        'display_mode': mode, 'display_name': display_name,
        'photo_key': photo_key, 'private_feedback': private_feedback,
        'contact_requested': contact_requested, 'contact': contact,
    }


# ── Servicio ──────────────────────────────────────────────────────────────

class Service:
    def __init__(self, s3, bucket, allowed_origin, google_review_url=None, *,
                 correo=None, base_url=None, origen_correo=None, destino_correo=None):
        self.s3 = s3
        self.bucket = bucket
        self.allowed_origin = allowed_origin
        self.google_review_url = google_review_url
        self._almacen = Almacen(s3)
        # Notificación: solo si hay servicio de correo y las tres direcciones/URL configuradas.
        self.correo = correo
        self.base_url = base_url
        self.origen_correo = origen_correo
        self.destino_correo = destino_correo
        self.notificar = bool(correo and base_url and origen_correo and destino_correo)

    # --- acceso a S3 -------------------------------------------------------
    def _get_json(self, key):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if _is_missing(exc):
                return None
            raise
        return json.loads(obj['Body'].read())

    def _put_json(self, key, data, **extra):
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                           ContentType='application/json; charset=utf-8', **extra)

    def _exists(self, key):
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as exc:
            if _is_missing(exc):
                return False
            raise

    # --- estado del token --------------------------------------------------
    def token_state(self, token):
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            return None, 'invalido'
        h = token_hash(token)
        rec = self._get_json(f'tokens/{h}.json')
        if rec is None:
            _log('token_invalido')
            return None, 'invalido'
        if rec.get('revoked'):
            return rec, 'revocado'
        if _now() > _parse_iso(rec['expires_at']):
            return rec, 'vencido'
        if self._exists(f'claims/{h}.json'):
            return rec, 'usado'
        return rec, 'ok'

    # --- endpoints ---------------------------------------------------------
    def get_token(self, token):
        rec, state = self.token_state(token)
        if state == 'ok':
            return 200, {'ok': True, 'producto': rec.get('product_label'),
                         'categoria': rec.get('category'), 'vence': rec['expires_at'][:10]}
        return 200, {'ok': False, 'motivo': state}

    def presign(self, payload):
        ext = payload.get('ext')
        if not isinstance(ext, str) or ext not in PHOTO_EXT:
            return 400, {'ok': False, 'error': 'formato', 'formatos': sorted(PHOTO_EXT)}
        token = payload.get('token')
        _, state = self.token_state(token)
        if state != 'ok':
            return 403, {'ok': False, 'motivo': state}
        h = token_hash(token)
        key = f'pendientes/{h}/{uuid.uuid4().hex}.{ext}'
        content_type = PHOTO_EXT[ext]
        post = self.s3.generate_presigned_post(
            Bucket=self.bucket, Key=key,
            Fields={'Content-Type': content_type},
            Conditions=[{'Content-Type': content_type},
                        ['content-length-range', PHOTO_MIN_BYTES, PHOTO_MAX_BYTES],
                        {'key': key}],
            ExpiresIn=PRESIGN_TTL)
        _log('presign')
        return 200, {'ok': True, 'url': post['url'], 'fields': post['fields'],
                     'key': key, 'max_bytes': PHOTO_MAX_BYTES}

    def create_review(self, payload):
        try:
            data = parse_review(payload)
        except Invalid as exc:
            if exc.campos.get('_honeypot'):
                _log('honeypot')
                return 202, {'ok': True}
            return 400, {'ok': False, 'error': 'validacion', 'campos': exc.campos}

        token = payload.get('token')
        rec, state = self.token_state(token)
        if state == 'usado':
            return 409, {'ok': False, 'motivo': 'usado'}
        if state != 'ok':
            return 403, {'ok': False, 'motivo': state}
        h = token_hash(token)

        photo_key = data['photo_key']
        if photo_key:
            match = PHOTO_KEY_RE.fullmatch(photo_key)
            if not match or match.group(1) != h:
                return 400, {'ok': False, 'error': 'validacion', 'campos': {'photo_key': 'Foto inválida.'}}
            try:
                size = self.s3.head_object(Bucket=self.bucket, Key=photo_key)['ContentLength']
            except Exception as exc:
                if _is_missing(exc):
                    return 400, {'ok': False, 'error': 'validacion', 'campos': {'photo_key': 'La foto no llegó. Probá de nuevo.'}}
                raise
            if not PHOTO_MIN_BYTES <= size <= PHOTO_MAX_BYTES:
                return 400, {'ok': False, 'error': 'validacion', 'campos': {'photo_key': 'La foto tiene un tamaño no permitido.'}}
            datos = self.s3.get_object(Bucket=self.bucket, Key=photo_key)['Body'].read()
            if not jpeg_sin_metadatos(datos):
                _log('photo_rechazada')
                return 400, {'ok': False, 'error': 'validacion', 'campos': {'photo_key': 'La foto no es un JPEG válido o trae metadatos (EXIF, comentarios). Quitalos y volvé a subirla.'}}

        review_id = uuid.uuid4().hex
        now = _iso(_now())
        review = {
            'v': 1, 'id': review_id, 'token_hash': h, 'status': 'pending',
            'created_at': now, 'moderated_at': None, 'published_at': None,
            'rating': data['rating'], 'comment': data['comment'], 'aspects': data['aspects'],
            'display_mode': data['display_mode'], 'display_name': data['display_name'],
            'permission_to_publish': True,
            'photo_key': photo_key, 'photo_public_key': None,
            'private_feedback': data['private_feedback'],
            'contact_requested': data['contact_requested'], 'contact': data['contact'],
            'product_slug': rec.get('product_slug'), 'product_label': rec.get('product_label'),
            'category': rec.get('category'), 'verified': bool(rec.get('verified')),
        }

        # Consumir el token de forma atómica: solo una escritura puede crear la marca de uso.
        try:
            self._put_json(f'claims/{h}.json', {'claimed_at': now, 'review_id': review_id},
                           IfNoneMatch='*')
        except Exception as exc:
            if _is_precondition(exc):
                _log('claim_conflicto')
                return 409, {'ok': False, 'motivo': 'usado'}
            raise

        try:
            self._put_json(f'resenas/{review_id}.json', review)
        except Exception:
            # Compensación: liberar el token para que el cliente pueda reintentar.
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=f'claims/{h}.json')
            except Exception:
                _log('claim_rollback_fallido')
            raise

        _log('resena_creada', review_id=review_id, foto=bool(photo_key))
        # La reseña ya existe. El aviso es un efecto secundario: nunca cambia la respuesta.
        self._notificar(review)
        return 200, {'ok': True, 'id': review_id, 'google_url': self.google_review_url}

    def _notificar(self, review):
        if not self.notificar:
            return
        try:
            _correo.notificar_resena(self.correo, self._almacen, self.bucket, review,
                                     base_url=self.base_url, origen=self.origen_correo,
                                     destino=self.destino_correo, ahora=_now())
        except Exception as exc:  # defensa: notificar_resena ya no levanta, pero el hook tampoco
            _log('aviso_fallido', review_id=review.get('id'), error=type(exc).__name__)

    # --- ruteo -------------------------------------------------------------
    def route(self, method, path, raw_body):
        if method == 'OPTIONS':
            return 204, None
        if len(raw_body.encode('utf-8')) > MAX_BODY_BYTES:
            return 413, {'ok': False, 'error': 'demasiado_grande'}
        if method == 'GET' and path.startswith('/tokens/'):
            return self.get_token(path[len('/tokens/'):])
        if method == 'POST' and path in ('/presign', '/resenas'):
            try:
                payload = json.loads(raw_body or '{}')
            except (ValueError, RecursionError):
                return 400, {'ok': False, 'error': 'json'}
            if not isinstance(payload, dict):
                return 400, {'ok': False, 'error': 'json'}
            return self.presign(payload) if path == '/presign' else self.create_review(payload)
        return 404, {'ok': False, 'error': 'no_encontrado'}

    def response(self, status, payload):
        headers = {
            'Content-Type': 'application/json; charset=utf-8',
            'Cache-Control': 'no-store',
            'X-Content-Type-Options': 'nosniff',
            'Access-Control-Allow-Origin': self.allowed_origin,
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
            'Access-Control-Allow-Headers': 'content-type',
            'Vary': 'Origin',
        }
        body = '' if payload is None else json.dumps(payload, ensure_ascii=False)
        return {'statusCode': status, 'headers': headers, 'body': body}


# ── Entrada Lambda ────────────────────────────────────────────────────────

_SVC = None


def _service():
    global _SVC
    if _SVC is None:
        import boto3  # disponible en el runtime de Lambda; no se importa en los tests
        from botocore.config import Config
        correo_svc = None
        if os.environ.get('RESENAS_NOTIFY_ENABLED', 'false').lower() == 'true':
            cfg = Config(connect_timeout=3, read_timeout=3, retries={'max_attempts': 2})
            correo_svc = _correo.SESCorreo(boto3.client('ses', config=cfg))
        _SVC = Service(
            s3=boto3.client('s3'),
            bucket=os.environ['RESENAS_BUCKET'],
            allowed_origin=os.environ.get('ALLOWED_ORIGIN', 'https://ork3d.com'),
            google_review_url=os.environ.get('GOOGLE_REVIEW_URL') or None,
            correo=correo_svc,
            base_url=os.environ.get('RESENAS_ADMIN_BASE_URL') or None,
            origen_correo=os.environ.get('RESENAS_FROM_EMAIL') or None,
            destino_correo=os.environ.get('RESENAS_ADMIN_EMAIL') or None,
        )
    return _SVC


def _cuerpo(event):
    raw = event.get('body') or ''
    if event.get('isBase64Encoded') and raw:
        raw = base64.b64decode(raw).decode('utf-8')
    return raw


def handler(event, context=None):
    svc = _service()
    http = (event.get('requestContext') or {}).get('http') or {}
    method = (http.get('method') or event.get('httpMethod') or 'GET').upper()
    path = (event.get('rawPath') or event.get('path') or '/').rstrip('/') or '/'
    try:
        raw_body = _cuerpo(event)  # base64 o UTF-8 inválidos (ValueError)
    except ValueError:
        return svc.response(400, {'ok': False, 'error': 'json'})
    try:
        status, payload = svc.route(method, path, raw_body)
    except Exception as exc:  # sin detalle: podría contener datos. La ruta va como plantilla.
        _log('error_interno', ruta=_registro.ruta_plantilla(path), error=type(exc).__name__)
        status, payload = 500, {'ok': False, 'error': 'interno'}
    return svc.response(status, payload)
