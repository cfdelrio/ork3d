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
        self.objects = {}    # clave -> bytes
        self.presigned = {}  # clave -> {'fields', 'conditions', 'expires_in'}
        self.calls = []      # [(operación, clave)]
        self._fallos = []    # [(operación, prefijo, excepción)]

    def fail_on(self, operacion, exc, prefix=''):
        """La próxima llamada a `operacion` sobre claves con ese prefijo lanza `exc`."""
        self._fallos.append((operacion, prefix, exc))

    def _registrar(self, operacion, clave):
        self.calls.append((operacion, clave))
        for i, (op, prefijo, exc) in enumerate(self._fallos):
            if op == operacion and clave.startswith(prefijo):
                del self._fallos[i]
                raise exc

    def put_object(self, Bucket, Key, Body=b'', ContentType=None, IfNoneMatch=None, **_):
        self._registrar('put_object', Key)
        if IfNoneMatch not in (None, '*'):
            raise NotImplementedError('el fake solo modela IfNoneMatch="*"')
        if IfNoneMatch == '*' and Key in self.objects:
            raise FakeClientError('PreconditionFailed', 412,
                                  'At least one of the pre-conditions you specified did not hold')
        self.objects[Key] = Body.encode('utf-8') if isinstance(Body, str) else bytes(Body)
        return {}

    def get_object(self, Bucket, Key, Range=None, **_):
        self._registrar('get_object', Key)
        if Key not in self.objects:
            raise FakeClientError('NoSuchKey', 404, 'The specified key does not exist.')
        datos = self.objects[Key]
        if Range is not None:
            m = _RANGO.fullmatch(Range)
            if m is None:
                raise NotImplementedError(f'Range no modelado: {Range}')
            inicio = int(m.group(1))
            if inicio >= len(datos):
                raise FakeClientError('InvalidRange', 416, 'The requested range is not satisfiable')
            datos = datos[inicio:int(m.group(2)) + 1]
        return {'Body': _Body(datos), 'ContentLength': len(datos)}

    def head_object(self, Bucket, Key, **_):
        self._registrar('head_object', Key)
        if Key not in self.objects:
            # Un HEAD no trae cuerpo: botocore lo reporta con código "404", no NoSuchKey.
            raise FakeClientError('404', 404, 'Not Found')
        return {'ContentLength': len(self.objects[Key])}

    def delete_object(self, Bucket, Key, **_):
        self._registrar('delete_object', Key)
        self.objects.pop(Key, None)
        return {}

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
