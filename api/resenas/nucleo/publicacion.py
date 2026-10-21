"""Publicación pública: export con allowlist, fotos públicas y regeneración bajo lock.

La allowlist del export NO cambia respecto de la V1. Cualquier campo nuevo rompe el test
de privacidad a propósito. La CLI (admin/resenas.sh, exportar_publicas) produce el mismo
conjunto de claves.
"""
import re
import time

from . import errores
from . import jpeg as _jpeg

ALLOWLIST_EXPORT = ('id', 'rating', 'comment', 'aspects', 'display_name', 'category',
                    'product_label', 'verified', 'photo_url', 'published_at')
NOMBRE_ANONIMO = 'Cliente de ORK3D'
CACHE_EXPORT = 'public, max-age=60, must-revalidate'
CACHE_FOTO = 'public, max-age=31536000, immutable'
CLAVE_RESENA = re.compile(r'resenas/([a-f0-9]{32})\.json')
CLAVE_FOTO_PRIVADA = re.compile(r'pendientes/([a-f0-9]{64})/([a-f0-9]{32})\.jpg')
CLAVE_FOTO_PUBLICA = re.compile(r'resenas/fotos/([a-f0-9]{32})\.jpg')


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def clave_foto_pertenece(clave, token_hash):
    """La clave privada tiene que ser de una subida hecha con ese token."""
    m = CLAVE_FOTO_PRIVADA.fullmatch(clave or '')
    return bool(m) and m.group(1) == token_hash


def construir_export(resenas, generado):
    items = []
    for r in resenas:
        if r.get('status') != 'approved' or r.get('permission_to_publish') is not True:
            continue
        items.append({
            'id': r['id'],
            'rating': r['rating'],
            'comment': r.get('comment', ''),
            'aspects': list(r.get('aspects') or []),
            'display_name': r.get('display_name') or NOMBRE_ANONIMO,
            'category': r.get('category'),
            'product_label': r.get('product_label'),
            'verified': bool(r.get('verified')),
            'photo_url': f"/resenas/fotos/{r['id']}.jpg" if r.get('photo_public_key') else None,
            'published_at': r.get('published_at'),
        })
    items.sort(key=lambda x: x.get('published_at') or '', reverse=True)
    return {'generated_at': generado, 'count': len(items), 'items': items}


def publicar_foto(almacen, bucket_privado, bucket_sitio, resena_id, token_hash, clave_privada):
    """Copia la foto privada a resenas/fotos/<id>.jpg. Devuelve la clave pública.

    Lee y escribe bytes (no CopyObject) para no ampliar permisos. La foto ya pasó la
    validación al subirla (app.py). Acá se verifica otra vez antes de publicarla.
    """
    if not clave_foto_pertenece(clave_privada, token_hash):
        raise errores.ErrorFoto('clave_ajena')
    datos = almacen.leer_bytes(bucket_privado, clave_privada)
    if datos is None:
        raise errores.ErrorFoto('no_existe')
    if not _jpeg.es_jpeg(datos) or not _jpeg.jpeg_sin_metadatos(datos):
        raise errores.ErrorFoto('no_jpeg_limpio')
    clave_publica = f'resenas/fotos/{resena_id}.jpg'
    almacen.escribir_bytes(bucket_sitio, clave_publica, datos, 'image/jpeg', CACHE_FOTO)
    return clave_publica


def quitar_foto_publica(almacen, bucket_sitio, resena_id):
    almacen.borrar(bucket_sitio, f'resenas/fotos/{resena_id}.jpg')


def _leer_resenas(almacen, bucket_privado):
    resenas = []
    for clave in almacen.listar(bucket_privado, 'resenas/'):
        if not CLAVE_RESENA.fullmatch(clave):
            continue
        obj, _ = almacen.leer_json(bucket_privado, clave)
        if obj is not None:
            resenas.append(obj)
    return resenas


def reparar_fotos_publicas(almacen, bucket_sitio, resenas):
    """Borra fotos públicas que ya no le corresponden a una reseña aprobada."""
    vigentes = {r['id'] for r in resenas
                if r.get('status') == 'approved' and r.get('photo_public_key')
                and r.get('permission_to_publish') is True}
    borradas = 0
    for clave in almacen.listar(bucket_sitio, 'resenas/fotos/'):
        m = CLAVE_FOTO_PUBLICA.fullmatch(clave)
        if m and m.group(1) not in vigentes:
            almacen.borrar(bucket_sitio, clave)
            borradas += 1
    return borradas


class Publicador:
    """Regenera resenas/publicas.json bajo lock y pide la invalidación de CloudFront.

    Es idempotente: cada corrida recalcula el export desde el estado completo. Si una
    corrida falla, la siguiente deja el mismo resultado.
    """

    def __init__(self, almacen, bucket_privado, bucket_sitio, invalidador, bloqueo,
                 ahora, espera=time.sleep, reintentos=3):
        self.almacen = almacen
        self.bucket_privado = bucket_privado
        self.bucket_sitio = bucket_sitio
        self.invalidador = invalidador
        self.bloqueo = bloqueo
        self.ahora = ahora
        self.espera = espera
        self.reintentos = reintentos

    def _adquirir(self):
        for intento in range(self.reintentos):
            try:
                return self.bloqueo.adquirir()
            except errores.ErrorBloqueado:
                if intento == self.reintentos - 1:
                    raise
                self.espera(0.2 * (intento + 1))
        raise errores.ErrorBloqueado('export')

    def regenerar(self):
        duenio = self._adquirir()
        try:
            resenas = _leer_resenas(self.almacen, self.bucket_privado)
            export = construir_export(resenas, _iso(self.ahora()))
            self.almacen.escribir_json(self.bucket_sitio, 'resenas/publicas.json', export,
                                       cache_control=CACHE_EXPORT)
            reparadas = reparar_fotos_publicas(self.almacen, self.bucket_sitio, resenas)
            self.invalidador.invalidar(['/resenas/*'])
            return {'export': 'actualizado', 'publicadas': export['count'],
                    'fotos_reparadas': reparadas}
        finally:
            try:
                self.bloqueo.liberar(duenio)
            except Exception:  # un lock que no se libera vence solo; no tapa el error original
                pass
