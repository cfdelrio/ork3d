#!/usr/bin/env python3
"""Simulador de S3 (s3 cp, s3api head-object) y CloudFront (create-invalidation, wait) para probar
infra/opiniones-publicar.sh. Sin red ni credenciales.

Estado en FAKE_ESTADO (JSON): objetos {clave: {"b64", "ct", "cc"}} e invalidaciones [[rutas...]].
FAKE_LOG recibe una línea por llamada: "servicio operación [detalle]". Lo que no se simula falla.
"""
import base64
import json
import os
import sys


def cargar():
    with open(os.environ['FAKE_ESTADO'], encoding='utf-8') as f:
        return json.load(f)


def guardar(st):
    with open(os.environ['FAKE_ESTADO'], 'w', encoding='utf-8') as f:
        json.dump(st, f)


def registrar(linea):
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(linea + '\n')


def fallo(codigo, operacion, mensaje):
    sys.stderr.write(f'An error occurred ({codigo}) when calling the {operacion} operation: {mensaje}\n')
    sys.exit(255)


def valor_de(args, flag, por_defecto=None):
    if flag in args:
        return args[args.index(flag) + 1]
    return por_defecto


def clave_s3(uri, bucket):
    prefijo = f's3://{bucket}/'
    return uri[len(prefijo):] if uri.startswith(prefijo) else None


def main():
    servicio, operacion, args = sys.argv[1], sys.argv[2], sys.argv[3:]
    st = cargar()
    if servicio == 's3api' and operacion == 'head-object':
        clave = valor_de(args, '--key')
        registrar(f'{servicio} {operacion} {clave}')
        obj = st['objetos'].get(clave)
        if obj is None:
            fallo('404', 'HeadObject', 'Not Found')
        print(json.dumps({'ContentLength': len(base64.b64decode(obj['b64'])),
                          'ContentType': obj['ct'], 'CacheControl': obj['cc']}))
        return 0
    if servicio == 's3' and operacion == 'cp':
        origen, destino = args[0], args[1]
        clave = clave_s3(origen, valor_de(args, '--bucket', 'ork3d.com') or 'ork3d.com')
        if clave is not None:                       # descarga: s3://... → local o stdout
            obj = st['objetos'].get(clave)
            if obj is None:
                fallo('NoSuchKey', 'GetObject', 'The specified key does not exist.')
            registrar(f's3 cp descarga {clave}')
            datos = base64.b64decode(obj['b64'])
            if destino == '-':
                sys.stdout.buffer.write(datos)
            else:
                with open(destino, 'wb') as f:
                    f.write(datos)
            return 0
        clave = clave_s3(destino, 'ork3d.com')      # subida: local → s3://...
        if clave is None:
            fallo('InvalidArgument', 'CopyObject', 'destino no soportado')
        with open(origen, 'rb') as f:
            datos = f.read()
        ct = valor_de(args, '--content-type', 'application/octet-stream')
        cc = valor_de(args, '--cache-control', '')
        st['objetos'][clave] = {'b64': base64.b64encode(datos).decode('ascii'), 'ct': ct, 'cc': cc}
        registrar(f's3 cp subida {clave}')
        guardar(st)
        return 0
    if servicio == 'cloudfront' and operacion == 'create-invalidation':
        dist = valor_de(args, '--distribution-id')
        i = args.index('--paths') + 1
        rutas = []
        while i < len(args) and not args[i].startswith('--'):
            rutas.append(args[i])
            i += 1
        st.setdefault('invalidaciones', []).append({'dist': dist, 'rutas': rutas})
        registrar(f'cloudfront create-invalidation {dist} {len(rutas)}')
        guardar(st)
        print('INVFAKE' + str(len(st['invalidaciones'])))
        return 0
    if servicio == 'cloudfront' and operacion == 'wait':
        registrar('cloudfront wait invalidation-completed')
        return 0
    fallo('UnsupportedOperation', operacion, f'{servicio} {operacion} no está simulada')


if __name__ == '__main__':
    sys.exit(main())
