"""ORK3D · Reseñas · API mínima (AWS Lambda, Python 3.12, sin dependencias externas).

Rutas (Function URL, sin prefijo):
  GET  /tokens/{token}  → estado del token (ok | invalido | vencido | usado | revocado)
  POST /presign         → presigned POST para subir una foto a pendientes/
  POST /resenas         → crea la reseña en estado "pending" y consume el token

Datos: bucket privado (RESENAS_BUCKET). Ver docs/resenas/README.md.
Nunca se loguea un token completo, una IP ni el contenido de una reseña.
boto3 viene incluido en el runtime de Lambda; los tests usan un fake (api/resenas/fakes.py).
"""
import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import uuid

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

log = logging.getLogger('resenas')
log.setLevel(logging.INFO)


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


def token_hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _log(ev, **fields):
    log.info(json.dumps({'ev': ev, **fields}, ensure_ascii=False))


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


# Segmentos JPEG que puede tener una foto nuestra: cabeceras y tablas de imagen, nada más.
# APP0 solo si es JFIF y APP2 solo si es perfil de color ICC (no lleva datos personales).
# Cualquier otro APPn (EXIF con GPS, XMP, IPTC) y COM se rechazan.
SEGMENTOS_JPEG = {0xC0, 0xC1, 0xC2, 0xC4, 0xDA, 0xDB, 0xDD}  # SOF0-2, DHT, SOS, DQT, DRI
REINICIOS = set(range(0xD0, 0xD8))


def jpeg_sin_metadatos(datos):
    """True si `datos` es un JPEG que solo tiene segmentos de imagen (y JFIF).

    Recorre los marcadores sin decodificar la imagen. La foto pública no puede llevar
    datos ocultos aunque el cliente se salte la recodificación del navegador.
    """
    if datos[:2] != b'\xff\xd8':
        return False
    pos, n = 2, len(datos)
    while pos + 1 < n:
        if datos[pos] != 0xFF:
            return False
        marcador = datos[pos + 1]
        if marcador == 0xFF:  # relleno
            pos += 1
            continue
        pos += 2
        if marcador == 0xD9:  # EOI: tiene que ser el último byte; nada puede ir después
            return pos == n
        if marcador == 0x01 or marcador in REINICIOS:  # sin longitud
            continue
        if pos + 2 > n:
            return False
        largo = int.from_bytes(datos[pos:pos + 2], 'big')
        if largo < 2 or pos + largo > n:
            return False
        if marcador == 0xE0:
            if not datos[pos + 2:pos + largo].startswith(b'JFIF\x00'):
                return False
        elif marcador == 0xE2:
            if not datos[pos + 2:pos + largo].startswith(b'ICC_PROFILE\x00'):
                return False
        elif marcador not in SEGMENTOS_JPEG:
            return False
        pos += largo
        if marcador == 0xDA:  # SOS: los datos comprimidos siguen hasta el próximo marcador
            while pos + 1 < n and not (datos[pos] == 0xFF and datos[pos + 1] != 0x00
                                       and datos[pos + 1] not in REINICIOS):
                pos += 1
    return False


# ── Servicio ──────────────────────────────────────────────────────────────

class Service:
    def __init__(self, s3, bucket, allowed_origin, google_review_url=None):
        self.s3 = s3
        self.bucket = bucket
        self.allowed_origin = allowed_origin
        self.google_review_url = google_review_url

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
            _log('token_invalido', th=h[:8])
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
        _log('presign', th=h[:8], ext=ext)
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
                _log('photo_rechazada', th=h[:8])
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
                _log('claim_conflicto', th=h[:8])
                return 409, {'ok': False, 'motivo': 'usado'}
            raise

        try:
            self._put_json(f'resenas/{review_id}.json', review)
        except Exception:
            # Compensación: liberar el token para que el cliente pueda reintentar.
            try:
                self.s3.delete_object(Bucket=self.bucket, Key=f'claims/{h}.json')
            except Exception:
                _log('claim_rollback_fallido', th=h[:8])
            raise

        _log('resena_creada', th=h[:8], id=review_id, rating=data['rating'], foto=bool(photo_key))
        return 200, {'ok': True, 'id': review_id, 'google_url': self.google_review_url}

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
        _SVC = Service(
            s3=boto3.client('s3'),
            bucket=os.environ['RESENAS_BUCKET'],
            allowed_origin=os.environ.get('ALLOWED_ORIGIN', 'https://ork3d.com'),
            google_review_url=os.environ.get('GOOGLE_REVIEW_URL') or None,
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
    except Exception:  # no se loguea el detalle: podría contener datos
        _log('error_interno', ruta=path)
        status, payload = 500, {'ok': False, 'error': 'interno'}
    return svc.response(status, payload)
