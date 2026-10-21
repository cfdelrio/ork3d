"""Validación de JPEG sin metadatos. Movida de app.py sin cambios de comportamiento.

Segmentos JPEG que puede tener una foto nuestra: cabeceras y tablas de imagen, nada más.
APP0 solo si es JFIF y APP2 solo si es perfil de color ICC (no lleva datos personales).
Cualquier otro APPn (EXIF con GPS, XMP, IPTC) y COM se rechazan.
"""

SEGMENTOS_JPEG = {0xC0, 0xC1, 0xC2, 0xC4, 0xDA, 0xDB, 0xDD}  # SOF0-2, DHT, SOS, DQT, DRI
REINICIOS = set(range(0xD0, 0xD8))


def es_jpeg(datos):
    return datos[:3] == b'\xff\xd8\xff'


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
