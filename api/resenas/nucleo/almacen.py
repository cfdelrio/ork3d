"""Operaciones de S3 que usa el núcleo, sobre un cliente tipo boto3 (o FakeS3 en tests).

Concurrencia optimista: `leer_json` devuelve el ETag y `escribir_json(etag=...)` escribe
con If-Match: si el objeto cambió, se levanta ErrorConflicto y nada se sobrescribe.
La creación exclusiva usa If-None-Match: '*'.

Antes del deploy hay que verificar contra S3 real que If-Match se comporta así (ver
docs/resenas/V2-ADMIN.md §10). Los tests locales simulan el comportamiento esperado.
"""
import json

from . import errores

_NO_EXISTE = {'NoSuchKey', '404', 'NotFound'}
_PRECONDICION = {'PreconditionFailed', 'ConditionalRequestConflict'}


def _codigo(exc):
    return (getattr(exc, 'response', None) or {}).get('Error', {}).get('Code', '')


def _http(exc):
    return ((getattr(exc, 'response', None) or {}).get('ResponseMetadata', {}) or {}).get('HTTPStatusCode')


def _no_existe(exc):
    return _codigo(exc) in _NO_EXISTE or _http(exc) == 404


class Almacen:
    def __init__(self, s3):
        self.s3 = s3

    def leer_json(self, bucket, key):
        """(objeto, etag) o (None, None) si la clave no existe."""
        try:
            obj = self.s3.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _no_existe(exc):
                return None, None
            raise
        return json.loads(obj['Body'].read()), obj.get('ETag')

    def escribir_json(self, bucket, key, obj, *, solo_si_no_existe=False, etag=None,
                      cache_control=None):
        """Escribe JSON. Devuelve el ETag nuevo si S3 lo informa.

        solo_si_no_existe: If-None-Match '*'. Si la clave existe → ErrorConflicto.
        etag: If-Match. Si la clave cambió o no existe → ErrorConflicto.
        """
        extra = {}
        if solo_si_no_existe:
            extra['IfNoneMatch'] = '*'
        if etag is not None:
            extra['IfMatch'] = etag
        if cache_control:
            extra['CacheControl'] = cache_control
        cuerpo = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            respuesta = self.s3.put_object(Bucket=bucket, Key=key, Body=cuerpo,
                                           ContentType='application/json; charset=utf-8', **extra)
        except Exception as exc:
            if _codigo(exc) in _PRECONDICION or _http(exc) in (409, 412):
                raise errores.ErrorConflicto(key) from exc
            if etag is not None and _no_existe(exc):
                raise errores.ErrorConflicto(key) from exc
            raise
        return (respuesta or {}).get('ETag')

    def leer_bytes(self, bucket, key):
        try:
            obj = self.s3.get_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _no_existe(exc):
                return None
            raise
        return obj['Body'].read()

    def escribir_bytes(self, bucket, key, datos, content_type, cache_control=None):
        extra = {'CacheControl': cache_control} if cache_control else {}
        self.s3.put_object(Bucket=bucket, Key=key, Body=datos, ContentType=content_type, **extra)

    def existe(self, bucket, key):
        try:
            self.s3.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            if _no_existe(exc):
                return False
            raise
        return True

    def borrar(self, bucket, key):
        """Idempotente: borrar una clave que no existe no es error."""
        self.s3.delete_object(Bucket=bucket, Key=key)

    def listar(self, bucket, prefijo):
        claves, token = [], None
        while True:
            params = {'Bucket': bucket, 'Prefix': prefijo}
            if token:
                params['ContinuationToken'] = token
            respuesta = self.s3.list_objects_v2(**params)
            claves.extend(c['Key'] for c in respuesta.get('Contents', []))
            if not respuesta.get('IsTruncated'):
                return claves
            token = respuesta.get('NextContinuationToken')
