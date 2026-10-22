"""Login por magic link, sesiones opacas, CSRF y rate limit del acceso admin.

- Código de login: 32 bytes aleatorios (token_urlsafe), solo se guarda su SHA-256, un solo
  uso (claim create-only), vence a los 10 minutos.
- Sesión: 32 bytes aleatorios, solo se guarda su SHA-256, vence a las 8 h desde su creación.
- Cookie: HttpOnly; Secure; SameSite=Lax; Path=/admin.
- Rate limit: global (el destinatario es fijo), 3 solicitudes cada 15 minutos. Ante duda
  (conflictos repetidos), se niega: el límite falla cerrado.
"""
import datetime as _dt
import hashlib
import re
import secrets

from . import errores

COOKIE = 'ork3d_admin'
CODIGO_RE = re.compile(r'[A-Za-z0-9_-]{43}')  # token_urlsafe(32) produce 43 caracteres


def generar_secreto():
    return secrets.token_urlsafe(32)


def hash_secreto(secreto):
    return hashlib.sha256(secreto.encode('utf-8')).hexdigest()


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(s):
    return _dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_dt.timezone.utc)


def cookie_sesion(secreto, horas):
    return (f'{COOKIE}={secreto}; HttpOnly; Secure; SameSite=Lax; Path=/admin; '
            f'Max-Age={int(horas * 3600)}')


def cookie_expirada():
    return f'{COOKIE}=; HttpOnly; Secure; SameSite=Lax; Path=/admin; Max-Age=0'


def leer_cookie(cookies, cabeceras):
    """Busca la cookie de sesión en la lista de cookies de la Function URL o en el header."""
    for par in cookies or []:
        nombre, _, valor = par.partition('=')
        if nombre.strip() == COOKIE:
            return valor.strip()
    for par in (cabeceras.get('cookie') or '').split(';'):
        nombre, _, valor = par.partition('=')
        if nombre.strip() == COOKIE:
            return valor.strip()
    return None


def comprobar_mutacion(cabeceras, origen_permitido):
    """Cabeceras obligatorias de toda mutación (POST). Levanta ErrorCsrf si faltan.

    Son rechazos de forma de la petición y responden 400. CloudFront reemplaza los 403 y 404 de la distribución por la
    página de error del sitio (V2-ADMIN.md §16.7), así que el código no puede ser 403. La seguridad no depende del código:
    el rechazo ocurre antes de cualquier efecto.
    """
    if cabeceras.get('origin') != origen_permitido:
        raise errores.ErrorCsrf(400, 'origen')
    media = (cabeceras.get('content-type') or '').split(';')[0].strip().lower()
    if media != 'application/json':
        raise errores.ErrorCsrf(400, 'content_type')
    if cabeceras.get('x-admin-request') != '1':
        raise errores.ErrorCsrf(400, 'csrf')


class Sesiones:
    def __init__(self, almacen, bucket, ahora, horas=8):
        self.almacen = almacen
        self.bucket = bucket
        self.ahora = ahora
        self.horas = horas

    def _clave(self, secreto):
        return f'admin/sesiones/{hash_secreto(secreto)}.json'

    def crear(self):
        secreto = generar_secreto()
        ahora = self.ahora()
        vence = ahora + _dt.timedelta(hours=self.horas)
        self.almacen.escribir_json(self.bucket, self._clave(secreto),
                                   {'v': 1, 'creada': _iso(ahora), 'expira': _iso(vence)},
                                   solo_si_no_existe=True)
        return secreto, _iso(vence)

    def validar(self, secreto):
        """Devuelve la fecha de vencimiento si la sesión es válida, o None.

        No borra nada: lo llaman también los GET. Las sesiones vencidas las limpia la
        regla de ciclo de vida de admin/ (2 días). Un registro que no se puede leer no es una
        sesión: se niega (401), igual que una inexistente.
        """
        if not isinstance(secreto, str) or not secreto:
            return None
        try:
            registro, _ = self.almacen.leer_json(self.bucket, self._clave(secreto))
            if registro is None or _parse(registro['expira']) <= self.ahora():
                return None
            return registro['expira']
        except (ValueError, KeyError, TypeError):
            return None

    def cerrar(self, secreto):
        if isinstance(secreto, str) and secreto:
            self.almacen.borrar(self.bucket, self._clave(secreto))


class Logins:
    def __init__(self, almacen, bucket, ahora, minutos=10):
        self.almacen = almacen
        self.bucket = bucket
        self.ahora = ahora
        self.minutos = minutos

    def emitir(self):
        codigo = generar_secreto()
        ahora = self.ahora()
        vence = ahora + _dt.timedelta(minutes=self.minutos)
        self.almacen.escribir_json(self.bucket, f'admin/logins/{hash_secreto(codigo)}.json',
                                   {'v': 1, 'creado': _iso(ahora), 'expira': _iso(vence)},
                                   solo_si_no_existe=True)
        return codigo

    def consumir(self, codigo):
        """Consume el código una sola vez. Lanza ErrorCodigo si no sirve (siempre igual)."""
        if not isinstance(codigo, str) or not CODIGO_RE.fullmatch(codigo):
            raise errores.ErrorCodigo('formato')
        h = hash_secreto(codigo)
        try:
            registro, _ = self.almacen.leer_json(self.bucket, f'admin/logins/{h}.json')
            if registro is None:
                raise errores.ErrorCodigo('desconocido')
            vence = _parse(registro['expira'])
        except (ValueError, KeyError, TypeError) as exc:
            # Registro corrupto: no sirve, igual que uno inexistente.
            raise errores.ErrorCodigo('desconocido') from exc
        if vence <= self.ahora():
            raise errores.ErrorCodigo('vencido')
        try:
            self.almacen.escribir_json(self.bucket, f'admin/logins-usados/{h}.json',
                                       {'v': 1, 'usado': _iso(self.ahora())},
                                       solo_si_no_existe=True)
        except errores.ErrorConflicto:
            raise errores.ErrorCodigo('usado')
        return True


class LimiteAcceso:
    CLAVE = 'admin/limites/acceso.json'

    def __init__(self, almacen, bucket, ahora, limite=3, ventana_min=15):
        self.almacen = almacen
        self.bucket = bucket
        self.ahora = ahora
        self.limite = limite
        self.ventana = _dt.timedelta(minutes=ventana_min)

    def permitir(self):
        """True si la solicitud entra en el cupo. Falla cerrado si hay demasiados conflictos."""
        for _ in range(3):
            ahora = self.ahora()
            registro, etag = self.almacen.leer_json(self.bucket, self.CLAVE)
            if registro is None:
                nuevo = {'v': 1, 'ventana_inicio': _iso(ahora), 'cantidad': 1}
                try:
                    self.almacen.escribir_json(self.bucket, self.CLAVE, nuevo, solo_si_no_existe=True)
                    return True
                except errores.ErrorConflicto:
                    continue
            inicio = _parse(registro['ventana_inicio'])
            if ahora - inicio >= self.ventana:
                nuevo = {'v': 1, 'ventana_inicio': _iso(ahora), 'cantidad': 1}
            elif registro['cantidad'] >= self.limite:
                return False
            else:
                nuevo = {'v': 1, 'ventana_inicio': registro['ventana_inicio'],
                         'cantidad': registro['cantidad'] + 1}
            try:
                self.almacen.escribir_json(self.bucket, self.CLAVE, nuevo, etag=etag)
                return True
            except errores.ErrorConflicto:
                continue
        return False
