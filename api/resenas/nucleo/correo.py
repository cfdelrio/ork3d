"""Correos: aviso de nueva reseña, acceso al panel, y envío por SES.

El envío es un efecto secundario. Notificar nunca afecta la transacción crítica: si falla,
la reseña queda guardada, el claim no se libera y solo se registra `aviso_fallido` sin datos.

Destinatario y remitente salen solo de la configuración (RESENAS_ADMIN_EMAIL y
RESENAS_FROM_EMAIL). No hay direcciones en el código.
"""
import html as _html
from collections import namedtuple

from . import errores
from . import registro as _registro

Correo = namedtuple('Correo', 'origen destino asunto texto html')

LIMITE_COMENTARIO = 280

# Límites del aviso (best-effort). Peor caso por reseña: reservar en S3 (2 s) + enviar en SES (4 s) + liberar la
# reserva en S3 solo si SES rechazó (2 s) = 8 s. La Lambda pública tiene 10 s y la reseña ya está guardada: el aviso
# nunca debe acercarse a ese límite. Un solo intento: un reintento multiplicaría el peor caso.
CONFIG_SES = {'connect_timeout': 2, 'read_timeout': 2, 'retries': {'max_attempts': 1}}
CONFIG_AVISOS_S3 = {'connect_timeout': 1, 'read_timeout': 1, 'retries': {'max_attempts': 1}}
PEOR_CASO_AVISO_S = 8


def _es_respuesta_de_error(exc):
    """SES respondió con un error (ClientError de botocore, o su doble): el correo no salió.

    Un timeout, una conexión cortada o cualquier otra excepción no dice nada sobre el envío: puede haber salido.
    """
    respuesta = getattr(exc, 'response', None)
    return isinstance(respuesta, dict) and 'Error' in respuesta


def _e(valor):
    return _html.escape(str(valor), quote=True)


def estrellas(rating):
    n = min(max(int(rating), 1), 5)
    return '★' * n + '☆' * (5 - n)


def asunto_resena(rating):
    return f'Nueva reseña ORK3D · {estrellas(rating)}'


def _truncar(texto, n=LIMITE_COMENTARIO):
    texto = ' '.join((texto or '').split())
    return texto if len(texto) <= n else texto[:n - 1].rstrip() + '…'


def construir_resena(resena, base_url, origen, destino):
    """Aviso de nueva reseña. Allowlist: rating, nombre, producto, categoría, comentario
    truncado, si tiene foto y el enlace al panel. Nada más: ni pedido, ni feedback, ni contacto.
    """
    enlace = f"{base_url.rstrip('/')}/admin/resenas/{resena['id']}"
    asunto = asunto_resena(resena['rating'])
    comentario = _truncar(resena.get('comment')) or '(sin comentario)'
    foto = 'sí' if resena.get('photo_key') else 'no'
    nombre = resena.get('display_name') or 'Cliente de ORK3D'
    estrellas_txt = estrellas(resena['rating'])
    producto = resena.get('product_label') or ''
    categoria = resena.get('category') or ''

    texto = '\n'.join([
        estrellas_txt,
        nombre,
        producto,
        f'Categoría: {categoria}',
        '',
        f'“{comentario}”',
        '',
        f'Tiene foto: {foto}',
        '',
        f'Revisar reseña: {enlace}',
        '',
        'Aviso interno para moderación. No respondas a este correo.',
    ])
    html_doc = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex, nofollow"></head>'
        '<body style="margin:0;padding:24px;background:#F3F0E9;font-family:Arial,sans-serif;color:#191919">'
        f'<p style="font-size:22px;color:#FF704E;margin:0 0 8px">{_e(estrellas_txt)}</p>'
        f'<p style="font-size:18px;margin:0 0 4px"><strong>{_e(nombre)}</strong></p>'
        f'<p style="margin:0 0 4px">{_e(producto)}</p>'
        f'<p style="margin:0 0 16px;color:#555">Categoría: {_e(categoria)}</p>'
        f'<blockquote style="margin:0 0 16px;padding:12px 16px;border-left:4px solid #D6F17C;background:#fff">'
        f'{_e(comentario)}</blockquote>'
        f'<p style="margin:0 0 20px">Tiene foto: {_e(foto)}</p>'
        f'<p style="margin:0 0 24px"><a href="{_e(enlace)}" style="display:inline-block;padding:12px 18px;'
        'background:#D6F17C;color:#191919;text-decoration:none;border-radius:999px">Revisar reseña</a></p>'
        '<p style="font-size:12px;color:#555;margin:0">Aviso interno para moderación. No respondas a este correo.</p>'
        '</body></html>'
    )
    return Correo(origen, destino, asunto, texto, html_doc)


def construir_acceso(enlace, origen, destino):
    """Correo de acceso al panel. Sin datos del sistema."""
    asunto = 'Acceso al panel de reseñas ORK3D'
    texto = '\n'.join([
        'Pediste acceso al panel de reseñas de ORK3D.',
        '',
        'Confirmá el acceso en este enlace. Vence en 10 minutos y se usa una sola vez:',
        enlace,
        '',
        'Si no lo pediste, ignorá este correo.',
    ])
    html_doc = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8">'
        '<meta name="robots" content="noindex, nofollow"></head>'
        '<body style="margin:0;padding:24px;background:#F3F0E9;font-family:Arial,sans-serif;color:#191919">'
        '<p style="margin:0 0 16px">Pediste acceso al panel de reseñas de ORK3D.</p>'
        f'<p style="margin:0 0 24px"><a href="{_e(enlace)}" style="display:inline-block;padding:12px 18px;'
        'background:#D6F17C;color:#191919;text-decoration:none;border-radius:999px">Confirmar acceso</a></p>'
        '<p style="margin:0 0 8px">Vence en 10 minutos y se usa una sola vez.</p>'
        '<p style="font-size:12px;color:#555;margin:0">Si no lo pediste, ignorá este correo.</p>'
        '</body></html>'
    )
    return Correo(origen, destino, asunto, texto, html_doc)


class ServicioCorreo:
    """Interfaz de envío. Las implementaciones levantan excepción si el envío falla."""

    def enviar(self, correo):
        raise NotImplementedError


class SESCorreo(ServicioCorreo):
    """Envío con SES (SendEmail). Recibe un cliente boto3 'ses' ya configurado con CONFIG_SES."""

    def __init__(self, cliente):
        self.cliente = cliente

    def enviar(self, correo):
        respuesta = self.cliente.send_email(
            Source=correo.origen,
            Destination={'ToAddresses': [correo.destino]},
            Message={
                'Subject': {'Data': correo.asunto, 'Charset': 'UTF-8'},
                'Body': {
                    'Text': {'Data': correo.texto, 'Charset': 'UTF-8'},
                    'Html': {'Data': correo.html, 'Charset': 'UTF-8'},
                },
            },
        )
        return (respuesta or {}).get('MessageId')


def _liberar(almacen, bucket, clave, resena_id):
    try:
        almacen.borrar(bucket, clave)
    except Exception as exc:
        _registro.evento('aviso_liberacion_fallida', review_id=resena_id, error=type(exc).__name__)


def notificar_resena(servicio, almacen, bucket, resena, *, base_url, origen, destino, ahora):
    """Aviso de una reseña, como máximo un correo por reseña. Devuelve True si el correo salió. Nunca levanta.

    1. Reserva notificaciones/<id>.json con escritura condicional (solo si no existe) ANTES de enviar. Si ya existe,
       no se envía. Si la reserva falla por otra causa, no se envía: nunca hay correo sin reserva.
    2. Envía. Si SES respondió con un error, el correo no salió: se libera la reserva para permitir un reenvío
       manual desde el panel.
    3. Si el envío pudo salir (timeout, conexión cortada u otro error), la reserva se queda. Eso puede perder un
       aviso, pero nunca lo duplica.
    """
    rid = resena.get('id')
    try:
        correo = construir_resena(resena, base_url, origen, destino)
    except Exception as exc:
        _registro.evento('aviso_fallido', review_id=rid, error=type(exc).__name__, resultado='no_construido')
        return False
    clave = f'notificaciones/{rid}.json'
    try:
        almacen.escribir_json(bucket, clave, {'v': 2, 'reservado': ahora.strftime('%Y-%m-%dT%H:%M:%SZ')},
                              solo_si_no_existe=True)
    except errores.ErrorConflicto:
        _registro.evento('aviso_ya_registrado', review_id=rid)
        return False
    except Exception as exc:
        _registro.evento('aviso_fallido', review_id=rid, error=type(exc).__name__, resultado='sin_reserva')
        return False
    try:
        servicio.enviar(correo)
    except Exception as exc:
        if _es_respuesta_de_error(exc):
            _liberar(almacen, bucket, clave, rid)
            _registro.evento('aviso_fallido', review_id=rid, error=type(exc).__name__, resultado='rechazado')
        else:
            _registro.evento('aviso_fallido', review_id=rid, error=type(exc).__name__, resultado='incierto')
        return False
    _registro.evento('aviso_enviado', review_id=rid)
    return True
