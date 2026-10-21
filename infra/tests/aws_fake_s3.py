#!/usr/bin/env python3
"""Simulador mínimo de la AWS CLI para probar infra/smoke-if-match.sh. Sin red ni credenciales.

Modela lo que el smoke usa de S3: versiones, delete markers, ETag, If-Match (412 PreconditionFailed) y
borrado por versión. Un candado (fcntl) serializa cada operación, así que dos escritores simultáneos con el
mismo If-Match se comportan como en S3: el primero gana y el segundo recibe 412.

Variables de entorno:
  FAKE_STATE                   archivo JSON con el estado del bucket (obligatorio)
  FAKE_LOG                     registro de llamadas: servicio, operación, key y nombre del body
  FAKE_BUCKET                  bucket que devuelve la Lambda (por defecto bucket-fake)
  FAKE_NO_BUCKET=1             la Lambda no tiene la variable RESENAS_BUCKET
  FAKE_IGNORE_IFMATCH_BODIES   bodies (basename) para los que If-Match se ignora: simula un S3 roto solo
                               para esos escritores, y así se puede probar que el smoke aborta si ganan ambos.
  FAKE_NO_DELETE=1             delete-object falla con AccessDenied: sirve para probar que la limpieza
                               incompleta se reporta y el smoke sale con 5.
"""
import base64
import fcntl
import hashlib
import json
import os
import sys
import uuid

CUENTA_FALSA = '9' * 12
VALOR = {'--bucket', '--key', '--body', '--content-type', '--if-match', '--version-id', '--query',
         '--output', '--prefix', '--max-keys', '--function-name', '--region', '--profile'}


def parsear(args):
    opciones, posicionales, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a in VALOR and i + 1 < len(args):
            opciones[a] = args[i + 1]
            i += 2
        elif a.startswith('--'):
            opciones[a] = True
            i += 1
        else:
            posicionales.append(a)
            i += 1
    return opciones, posicionales


def fallo(operacion, codigo, mensaje):
    sys.stderr.write(f'An error occurred ({codigo}) when calling the {operacion} operation: {mensaje}\n')
    sys.exit(255)


def etag_de(datos):
    return '"' + hashlib.md5(datos).hexdigest() + '"'


def actual(versiones):
    if not versiones or versiones[-1]['DeleteMarker']:
        return None
    return versiones[-1]


def cargar(ruta):
    if not os.path.exists(ruta):
        return {'keys': {}}
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def guardar(ruta, estado):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(estado, f)


def ignora_ifmatch(ruta_body):
    lista = os.environ.get('FAKE_IGNORE_IFMATCH_BODIES', '').split(',')
    return os.path.basename(ruta_body) in lista


def main():
    servicio, operacion = sys.argv[1], sys.argv[2]
    opciones, posicionales = parsear(sys.argv[3:])
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f"{servicio} {operacion} {opciones.get('--key', '')} "
                    f"{os.path.basename(str(opciones.get('--body', '')))}\n")

    if servicio == 'sts':
        print(json.dumps({'Account': CUENTA_FALSA, 'Arn': f'arn:aws:iam::{CUENTA_FALSA}:user/operador',
                          'UserId': 'AIDAEXAMPLE'}))
        return
    if servicio == 'lambda':
        print('None' if os.environ.get('FAKE_NO_BUCKET') == '1' else os.environ.get('FAKE_BUCKET', 'bucket-fake'))
        return
    if '--generate-cli-skeleton' in opciones:  # comprobación offline de que la CLI conoce IfMatch
        print(json.dumps({'Bucket': '', 'Key': '', 'IfMatch': ''}))
        return

    ruta = os.environ['FAKE_STATE']
    candado = open(ruta + '.lock', 'w', encoding='utf-8')
    fcntl.flock(candado, fcntl.LOCK_EX)
    try:
        estado = cargar(ruta)
        claves = estado['keys']
        if operacion == 'put-object':
            clave = opciones['--key']
            versiones = claves.get(clave, [])
            with open(opciones['--body'], 'rb') as f:
                cuerpo = f.read()
            if '--if-match' in opciones and not ignora_ifmatch(opciones['--body']):
                act = actual(versiones)
                if act is None or act['ETag'] != opciones['--if-match']:
                    fallo('PutObject', 'PreconditionFailed',
                          'At least one of the preconditions you specified did not hold.')
            version = {'VersionId': uuid.uuid4().hex, 'ETag': etag_de(cuerpo),
                       'Body': base64.b64encode(cuerpo).decode('ascii'), 'DeleteMarker': False}
            versiones.append(version)
            claves[clave] = versiones
            guardar(ruta, estado)
            print(json.dumps({'ETag': version['ETag'], 'VersionId': version['VersionId']}))
        elif operacion == 'head-object':
            act = actual(claves.get(opciones['--key'], []))
            if act is None:
                fallo('HeadObject', '404', 'Not Found')
            if opciones.get('--query') == 'ETag' and opciones.get('--output') == 'text':
                print(act['ETag'])
            else:
                print(json.dumps({'ETag': act['ETag']}))
        elif operacion == 'get-object':
            act = actual(claves.get(opciones['--key'], []))
            if act is None:
                fallo('GetObject', 'NoSuchKey', 'The specified key does not exist.')
            datos = base64.b64decode(act['Body'])
            with open(posicionales[-1], 'wb') as f:
                f.write(datos)
            print(json.dumps({'ContentLength': len(datos)}))
        elif operacion == 'list-object-versions':
            prefijo = opciones.get('--prefix', '')
            versiones, marcas = [], []
            for clave, vs in claves.items():
                if not clave.startswith(prefijo):
                    continue
                for v in vs:
                    entrada = {'Key': clave, 'VersionId': v['VersionId']}
                    if v['DeleteMarker']:
                        marcas.append(entrada)
                    else:
                        versiones.append({**entrada, 'ETag': v['ETag']})
            print(json.dumps({'Versions': versiones, 'DeleteMarkers': marcas}))
        elif operacion == 'delete-object':
            clave = opciones['--key']
            vs = claves.get(clave, [])
            if os.environ.get('FAKE_NO_DELETE') == '1':
                fallo('DeleteObject', 'AccessDenied', 'Access Denied')
            if '--version-id' in opciones:
                nuevas = [v for v in vs if v['VersionId'] != opciones['--version-id']]
                if len(nuevas) == len(vs):
                    fallo('DeleteObject', 'NoSuchVersion', 'The specified version does not exist.')
                vs = nuevas
            else:
                vs.append({'VersionId': uuid.uuid4().hex, 'ETag': None, 'Body': '', 'DeleteMarker': True})
            if vs:
                claves[clave] = vs
            else:
                claves.pop(clave, None)
            guardar(ruta, estado)
            print('{}')
        else:
            fallo(operacion, 'UnsupportedOperation', 'operación no simulada')
    finally:
        fcntl.flock(candado, fcntl.LOCK_UN)
        candado.close()


if __name__ == '__main__':
    main()
