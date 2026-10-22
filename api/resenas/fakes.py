"""Fake de S3 para los tests de api/resenas (sin boto3 ni red).

Implementa el subconjunto que usa app.py y se comporta como S3 en lo que importa:
- put_object con IfNoneMatch='*' falla con 412 si la clave ya existe.
- get_object de una clave inexistente falla con NoSuchKey; head_object, con 404.
- get_object acepta Range='bytes=a-b'.
- delete_object es idempotente.
- generate_presigned_post guarda la política y browser_post la hace cumplir
  (rango de tamaño, Content-Type exacto y clave firmada), como S3 con un POST.

Los errores son FakeClientError: exponen .response igual que botocore.ClientError.
"""
import re
import threading

_RANGO = re.compile(r'bytes=(\d+)-(\d+)')


class FakeClientError(Exception):
    def __init__(self, code, status, message=''):
        super().__init__(f'{code}: {message}')
        self.response = {
            'Error': {'Code': code, 'Message': message},
            'ResponseMetadata': {'HTTPStatusCode': status},
        }


class _Body:
    def __init__(self, data):
        self._data = data

    def read(self):
        return self._data


class FakeS3:
    def __init__(self):
        # S3 resuelve cada escritura condicional de forma atómica: la comprobación y la escritura son una sola
        # operación. El fake lo modela con un candado; sin él, una prueba con hilos no dice nada.
        self._atomico = threading.Lock()
        self.objects = {}    # clave -> bytes
        self.metadatos = {}  # clave -> {'ContentType', 'CacheControl'}
        self.presigned = {}  # clave -> {'fields', 'conditions', 'expires_in'}
        self.calls = []      # [(operación, clave)]
        self.escrituras = [] # [(bucket, clave)] de cada put_object que se aplicó
        self.borrados = []   # [(bucket, clave)] de cada delete_object
        self._fallos = []    # [(operación, prefijo, excepción)]

    @staticmethod
    def etag(datos):
        """ETag como S3: MD5 entre comillas. Cambia con cada contenido nuevo."""
        import hashlib
        return '"' + hashlib.md5(datos).hexdigest() + '"'

    def fail_on(self, operacion, exc, prefix=''):
        """La próxima llamada a `operacion` sobre claves con ese prefijo lanza `exc`."""
        self._fallos.append((operacion, prefix, exc))

    def _registrar(self, operacion, clave):
        self.calls.append((operacion, clave))
        for i, (op, prefijo, exc) in enumerate(self._fallos):
            if op == operacion and clave.startswith(prefijo):
                del self._fallos[i]
                raise exc

    def put_object(self, Bucket, Key, Body=b'', ContentType=None, IfNoneMatch=None,
                   IfMatch=None, CacheControl=None, **_):
        with self._atomico:
            return self._put_object(Bucket, Key, Body, ContentType, IfNoneMatch, IfMatch, CacheControl)

    def _put_object(self, Bucket, Key, Body, ContentType, IfNoneMatch, IfMatch, CacheControl):
        self._registrar('put_object', Key)
        if IfNoneMatch not in (None, '*'):
            raise NotImplementedError('el fake solo modela IfNoneMatch="*"')
        if IfNoneMatch == '*' and Key in self.objects:
            raise FakeClientError('PreconditionFailed', 412,
                                  'At least one of the pre-conditions you specified did not hold')
        if IfMatch is not None:
            # If-Match: solo escribe si el objeto existe y su ETag es el esperado.
            if Key not in self.objects:
                raise FakeClientError('PreconditionFailed', 412, 'If-Match: la clave no existe')
            if self.etag(self.objects[Key]) != IfMatch:
                raise FakeClientError('PreconditionFailed', 412, 'If-Match: el ETag no coincide')
        datos = Body.encode('utf-8') if isinstance(Body, str) else bytes(Body)
        self.objects[Key] = datos
        self.metadatos[Key] = {'ContentType': ContentType, 'CacheControl': CacheControl}
        self.escrituras.append((Bucket, Key))
        return {'ETag': self.etag(datos)}

    def get_object(self, Bucket, Key, Range=None, **_):
        self._registrar('get_object', Key)
        if Key not in self.objects:
            raise FakeClientError('NoSuchKey', 404, 'The specified key does not exist.')
        datos = self.objects[Key]
        etag = self.etag(datos)
        if Range is not None:
            m = _RANGO.fullmatch(Range)
            if m is None:
                raise NotImplementedError(f'Range no modelado: {Range}')
            inicio = int(m.group(1))
            if inicio >= len(datos):
                raise FakeClientError('InvalidRange', 416, 'The requested range is not satisfiable')
            datos = datos[inicio:int(m.group(2)) + 1]
        return {'Body': _Body(datos), 'ContentLength': len(datos), 'ETag': etag}

    def head_object(self, Bucket, Key, **_):
        self._registrar('head_object', Key)
        if Key not in self.objects:
            # Un HEAD no trae cuerpo: botocore lo reporta con código "404", no NoSuchKey.
            raise FakeClientError('404', 404, 'Not Found')
        return {'ContentLength': len(self.objects[Key]), 'ETag': self.etag(self.objects[Key])}

    def delete_object(self, Bucket, Key, **_):
        self._registrar('delete_object', Key)
        self.objects.pop(Key, None)
        self.metadatos.pop(Key, None)
        self.borrados.append((Bucket, Key))
        return {}

    def list_objects_v2(self, Bucket, Prefix='', ContinuationToken=None, **_):
        """Una sola página, ordenada por clave (basta para los tests)."""
        claves = sorted(k for k in self.objects if k.startswith(Prefix or ''))
        return {'Contents': [{'Key': k} for k in claves], 'KeyCount': len(claves),
                'IsTruncated': False}

    def generate_presigned_post(self, Bucket, Key, Fields=None, Conditions=None, ExpiresIn=3600):
        self._registrar('presign', Key)
        fields = {**(Fields or {}), 'key': Key, 'policy': 'fake', 'x-amz-signature': 'fake'}
        self.presigned[Key] = {
            'fields': fields,
            'conditions': list(Conditions or []),
            'expires_in': ExpiresIn,
        }
        return {'url': f'https://{Bucket}.s3.amazonaws.com/', 'fields': fields}

    def browser_post(self, clave, cuerpo, content_type):
        """Simula el POST del navegador a la URL firmada: S3 solo guarda lo que cumple la política."""
        firma = self.presigned.get(clave)
        if firma is None:
            raise FakeClientError('AccessDenied', 403, 'No hay política firmada para esta clave.')
        valores = {'key': clave, 'Content-Type': content_type}
        for cond in firma['conditions']:
            if isinstance(cond, dict):
                for campo, esperado in cond.items():
                    if valores.get(campo) != esperado:
                        raise FakeClientError('AccessDenied', 403, f'Policy condition failed: {campo}')
            elif cond[0] == 'content-length-range':
                _, minimo, maximo = cond
                if not minimo <= len(cuerpo) <= maximo:
                    code = 'EntityTooLarge' if len(cuerpo) > maximo else 'EntityTooSmall'
                    raise FakeClientError(code, 400, 'Tamaño fuera de la política firmada.')
            else:
                raise NotImplementedError(f'condición no modelada: {cond!r}')
        self.objects[clave] = bytes(cuerpo)
        self.calls.append(('browser_post', clave))


class FakeSES:
    """Doble de SES: guarda los correos enviados. Puede fallar la próxima llamada a propósito.

    Implementa la misma interfaz que nucleo.correo.ServicioCorreo (enviar).
    """

    def __init__(self):
        self.enviados = []   # [{'from', 'to', 'subject', 'text', 'html'}]
        self._fallos = []

    def fallar_proximo(self, exc=None):
        self._fallos.append(exc or FakeClientError('MessageRejected', 400, 'Email address not verified.'))

    def enviar(self, correo):
        if self._fallos:
            raise self._fallos.pop(0)
        self.enviados.append({'from': correo.origen, 'to': correo.destino,
                              'subject': correo.asunto, 'text': correo.texto, 'html': correo.html})
        return f'msg-{len(self.enviados)}'


class FakeCloudFront:
    """Doble de CloudFront: registra las invalidaciones. Puede fallar a propósito."""

    def __init__(self):
        self.invalidaciones = []  # [[rutas], ...]
        self._fallos = []

    def fallar_proximo(self, exc=None):
        self._fallos.append(exc or FakeClientError('ServiceUnavailable', 503, 'CloudFront no disponible'))

    def invalidar(self, rutas):
        if self._fallos:
            raise self._fallos.pop(0)
        self.invalidaciones.append(list(rutas))
        return f'inv-{len(self.invalidaciones)}'


def segmento(marcador, cuerpo):
    """Segmento JPEG: marcador 0xFF xx, longitud big-endian que incluye los 2 bytes de longitud."""
    return b'\xff' + bytes([marcador]) + (len(cuerpo) + 2).to_bytes(2, 'big') + cuerpo


def jpeg(app0=b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00', segmentos_extra=b'',
         entropia=b'x' * 16_000):
    """JPEG sintético con la estructura de marcadores de un JPEG real (la imagen no se decodifica)."""
    return (b'\xff\xd8' + segmento(0xE0, app0) + segmentos_extra
            + segmento(0xDB, b'\x00' * 65) + segmento(0xC0, b'\x08\x00\x10\x00\x10\x03' + b'\x01\x11\x00' * 3)
            + segmento(0xC4, b'\x00' * 20) + segmento(0xDA, b'\x01\x01\x00\x00\x3f\x00')
            + entropia + b'\xff\xd9')
