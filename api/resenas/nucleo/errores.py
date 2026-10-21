"""Errores de dominio. Ninguno lleva datos del cliente en el mensaje."""


class ErrorNucleo(Exception):
    """Base de los errores de dominio."""


class ErrorValidacion(ErrorNucleo):
    def __init__(self, campos):
        super().__init__('validacion')
        self.campos = campos


class ErrorConflicto(ErrorNucleo):
    """Falló una precondición de S3 (If-None-Match o If-Match): alguien escribió antes."""


class ErrorEstado(ErrorNucleo):
    def __init__(self, accion, estado_actual):
        super().__init__('estado_incompatible')
        self.accion = accion
        self.estado_actual = estado_actual


class ErrorSinPermiso(ErrorNucleo):
    pass


class ErrorNoEncontrado(ErrorNucleo):
    pass


class ErrorBloqueado(ErrorNucleo):
    """El lock lógico está ocupado o no se pudo tomar."""


class ErrorFoto(ErrorNucleo):
    """La foto no existe, no pertenece a la reseña o no es un JPEG sin metadatos."""


class ErrorCodigo(ErrorNucleo):
    """Código de login inválido, vencido o ya usado. Siempre se responde igual al cliente."""


class ErrorJson(ErrorNucleo):
    """El cuerpo de la petición no es un objeto JSON."""


class ErrorSesion(ErrorNucleo):
    """Falta la cookie de sesión o la sesión venció."""


class ErrorCsrf(ErrorNucleo):
    def __init__(self, http, motivo):
        super().__init__(motivo)
        self.http = http
        self.motivo = motivo


class ErrorAlmacen(ErrorNucleo):
    """Falla de S3 no recuperable para esta operación."""
