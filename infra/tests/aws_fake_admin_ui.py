#!/usr/bin/env python3
"""Simulador de aws para probar infra/admin-ui-publicar.sh y admin-ui-verify.sh. Sin red, sin credenciales.

Estado en FAKE_ADMIN_UI_ESTADO: s3/<clave> (cuerpo) y s3/<clave>.json (cabeceras y metadata). Cada llamada se registra
en FAKE_ADMIN_UI_LOG como una línea JSON {"op": "s3api put-object", "args": [...]}, sin cuerpos.

Operaciones: s3api head-object | get-object | put-object | delete-object; cloudfront create-invalidation (devuelve el
id por stdout, como --query/--output text) | wait invalidation-completed; lambda get-function-configuration;
logs filter-log-events.

Un objeto ausente responde como AWS: "An error occurred (404) when calling the HeadObject operation: Not Found", salida 254.
FAKE_ADMIN_UI_FALLA_PUT=<clave>: put-object de esa clave falla (para probar la publicación parcial).
FAKE_ADMIN_UI_EXPORT / FAKE_ADMIN_UI_LAMBDA: no los usa aws; los sirve el simulador de curl.
"""
import json
import os
import shutil
import sys

ESTADO = os.environ.get('FAKE_ADMIN_UI_ESTADO', '')
LOG = os.environ.get('FAKE_ADMIN_UI_LOG', '')
MULTI = {'paths', 'metadata'}
CAMPOS_CABECERA = {
    'content-type': 'ContentType', 'cache-control': 'CacheControl', 'content-encoding': 'ContentEncoding',
    'content-language': 'ContentLanguage', 'content-disposition': 'ContentDisposition',
}


def analizar(args):
    opciones, posicionales = {}, []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith('--'):
            nombre = a[2:]
            valores = []
            i += 1
            if nombre in MULTI:
                while i < len(args) and not args[i].startswith('--'):
                    valores.append(args[i])
                    i += 1
            elif i < len(args) and not args[i].startswith('--'):
                valores.append(args[i])
                i += 1
            opciones[nombre] = valores
        else:
            posicionales.append(a)
            i += 1
    return opciones, posicionales


def registrar(args):
    if LOG:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'op': ' '.join(args[:2]), 'args': args[2:]}) + '\n')


def ruta_objeto(clave):
    return os.path.join(ESTADO, 's3', clave)


def no_existe(clave):
    sys.stderr.write('An error occurred (404) when calling the HeadObject operation: Not Found\n')
    return 254


def leer_meta(clave):
    with open(ruta_objeto(clave) + '.json', encoding='utf-8') as f:
        return json.load(f)


def cabeceras_de(opciones):
    meta = {}
    for opcion, campo in CAMPOS_CABECERA.items():
        if opciones.get(opcion):
            meta[campo] = opciones[opcion][0]
    if opciones.get('metadata'):
        meta['Metadata'] = dict(par.split('=', 1) for par in opciones['metadata'])
    return meta


def main(args):
    registrar(args)
    servicio, operacion = args[0], args[1]
    opciones, posicionales = analizar(args[2:])
    cli = f'{servicio} {operacion}'

    if cli == 's3api head-object':
        clave = opciones['key'][0]
        if os.environ.get('FAKE_ADMIN_UI_ERROR_HEAD') == clave:
            sys.stderr.write('An error occurred (403) when calling the HeadObject operation: Forbidden\n')
            return 254
        if not os.path.exists(ruta_objeto(clave)):
            return no_existe(clave)
        meta = leer_meta(clave)
        meta['ContentLength'] = os.path.getsize(ruta_objeto(clave))
        meta['ETag'] = '"fake-etag"'
        print(json.dumps(meta, indent=2, sort_keys=True))
        return 0

    if cli == 's3api get-object':
        clave = opciones['key'][0]
        if not os.path.exists(ruta_objeto(clave)):
            return no_existe(clave)
        shutil.copyfile(ruta_objeto(clave), posicionales[-1])
        print(json.dumps({'ContentType': leer_meta(clave).get('ContentType', '')}, indent=2))
        return 0

    if cli == 's3api put-object':
        clave = opciones['key'][0]
        if os.environ.get('FAKE_ADMIN_UI_FALLA_PUT') == clave:
            sys.stderr.write('An error occurred (AccessDenied) when calling the PutObject operation: prueba\n')
            return 255
        destino = ruta_objeto(clave)
        os.makedirs(os.path.dirname(destino), exist_ok=True)
        shutil.copyfile(opciones['body'][0], destino)
        with open(destino + '.json', 'w', encoding='utf-8') as f:
            json.dump(cabeceras_de(opciones), f)
        print(json.dumps({'ETag': '"fake-etag"'}, indent=2))
        return 0

    if cli == 's3api delete-object':
        clave = opciones['key'][0]
        for ruta in (ruta_objeto(clave), ruta_objeto(clave) + '.json'):
            if os.path.exists(ruta):
                os.remove(ruta)
        print('{}')
        return 0

    if cli == 'cloudfront create-invalidation':
        rutas = opciones.get('paths', [])
        if LOG:
            with open(LOG, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'op': 'invalidacion', 'rutas': rutas}) + '\n')
        print('IINVFAKE' + str(len(rutas)))
        return 0

    if cli == 'cloudfront wait':
        return 0

    if cli == 'lambda get-function-configuration':
        print(os.environ.get('FAKE_ADMIN_UI_LAMBDA', json.dumps(
            {'Environment': {'Variables': {'RESENAS_NOTIFY_ENABLED': 'true'}}})))
        return 0

    if cli == 'logs filter-log-events':
        print(json.dumps({'events': []}))
        return 0

    sys.stderr.write(f'operación no simulada: {cli}\n')
    return 255


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
