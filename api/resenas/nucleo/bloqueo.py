"""Lock lógico en S3 para serializar regeneraciones del export.

Clave: admin/locks/<nombre>.json con {v, duenio, expira}.
- Adquirir: create-only (If-None-Match). Si existe y no venció → ErrorBloqueado.
  Si venció → se toma con If-Match sobre su ETag (un solo ganador).
- Liberar: solo borra si el dueño coincide. Un lock vencido se puede tomar, así que una
  falla que no libera no deja el sistema trabado.
"""
import datetime as _dt
import uuid

from . import errores


def _iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(s):
    return _dt.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=_dt.timezone.utc)


class Bloqueo:
    def __init__(self, almacen, bucket, nombre, duracion_s, ahora):
        self.almacen = almacen
        self.bucket = bucket
        self.clave = f'admin/locks/{nombre}.json'
        self.duracion = _dt.timedelta(seconds=duracion_s)
        self.ahora = ahora

    def adquirir(self):
        ahora = self.ahora()
        duenio = uuid.uuid4().hex
        registro = {'v': 1, 'duenio': duenio, 'expira': _iso(ahora + self.duracion)}
        try:
            self.almacen.escribir_json(self.bucket, self.clave, registro, solo_si_no_existe=True)
            return duenio
        except errores.ErrorConflicto:
            pass
        actual, etag = self.almacen.leer_json(self.bucket, self.clave)
        if actual is None or _parse(actual['expira']) > ahora:
            raise errores.ErrorBloqueado('export')
        try:
            self.almacen.escribir_json(self.bucket, self.clave, registro, etag=etag)
        except errores.ErrorConflicto:
            raise errores.ErrorBloqueado('export')
        return duenio

    def liberar(self, duenio):
        actual, _ = self.almacen.leer_json(self.bucket, self.clave)
        if actual and actual.get('duenio') == duenio:
            self.almacen.borrar(self.bucket, self.clave)
