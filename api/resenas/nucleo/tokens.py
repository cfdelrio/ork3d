"""Tokens de invitación: mismas reglas que `admin/resenas.sh token crear`.

- Token: 24 bytes aleatorios en base64url sin padding (32 caracteres). La CLI hace
  `openssl rand -base64 24 | tr '+/' '-_' | tr -d '='`: mismo alfabeto y longitud.
- Clave: tokens/<sha256 hex del token>.json. Create-only (If-None-Match) con reintento.
- Registro: los mismos campos que escribe la CLI. El mensaje de la invitación no se guarda.
"""
import base64
import datetime as _dt
import hashlib
import re
import secrets

from . import errores

TOKEN_RE = re.compile(r'[A-Za-z0-9_-]{32}')
SLUG_RE = re.compile(r'[a-z0-9-]+')
DIAS_MIN, DIAS_MAX = 1, 365
DIAS_POR_DEFECTO = 60  # igual que TOKEN_DAYS en admin/.env.example


def generar_token(fuente=None):
    crudo = (fuente or secrets.token_bytes)(24)
    return base64.urlsafe_b64encode(crudo).decode('ascii').rstrip('=')


def token_valido(token):
    return isinstance(token, str) and TOKEN_RE.fullmatch(token) is not None


def hash_token(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def validar_invitacion(p, dias_por_defecto=DIAS_POR_DEFECTO):
    """Valida el cuerpo de POST /admin/api/invitaciones. Devuelve datos limpios o lanza."""
    campos = {}
    if not isinstance(p, dict):
        raise errores.ErrorValidacion({'_': 'Formato inválido.'})

    def texto(clave, max_len):
        valor = p.get(clave, '')
        if valor is None:
            valor = ''
        if not isinstance(valor, str):
            campos[clave] = 'Formato inválido.'
            return ''
        limpio = ' '.join(valor.split())
        if not limpio:
            campos[clave] = 'Este campo es obligatorio.'
        elif len(limpio) > max_len:
            campos[clave] = f'Máximo {max_len} caracteres.'
        return limpio

    def slug(clave):
        limpio = texto(clave, 40)
        if limpio and clave not in campos and not SLUG_RE.fullmatch(limpio):
            campos[clave] = 'Usá minúsculas, números y guiones.'
        return limpio

    order_ref = texto('order_ref', 80)
    product_slug = slug('product_slug')
    product_label = texto('product_label', 80)
    category = slug('category')

    dias = p.get('dias')
    if dias is None:
        dias = dias_por_defecto
    if isinstance(dias, bool) or not isinstance(dias, int) or not DIAS_MIN <= dias <= DIAS_MAX:
        campos['dias'] = 'Entre 1 y 365 días.'
        dias = dias_por_defecto

    if campos:
        raise errores.ErrorValidacion(campos)
    return {'order_ref': order_ref, 'product_slug': product_slug,
            'product_label': product_label, 'category': category, 'dias': dias}


def construir_registro(datos, token, ahora):
    return {
        'v': 1,
        'token_hash': hash_token(token),
        'created_at': _iso(ahora),
        'expires_at': _iso(ahora + _dt.timedelta(days=datos['dias'])),
        'order_ref': datos['order_ref'],
        'product_slug': datos['product_slug'],
        'product_label': datos['product_label'],
        'category': datos['category'],
        'verified': False,
        'revoked': False,
        'revoked_at': None,
    }


def crear_token(almacen, bucket, datos, ahora, fuente=None, intentos=3):
    """Genera el token y escribe su registro con create-only. Reintenta ante colisión."""
    for _ in range(intentos):
        token = generar_token(fuente)
        registro = construir_registro(datos, token, ahora)
        try:
            almacen.escribir_json(bucket, f"tokens/{registro['token_hash']}.json", registro,
                                  solo_si_no_existe=True)
        except errores.ErrorConflicto:
            continue
        return token, registro
    raise errores.ErrorAlmacen('colision_token')
