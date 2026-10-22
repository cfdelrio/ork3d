#!/usr/bin/env python3
"""Simulador de curl para probar el smoke de infra/admin-deploy.sh y el diagnóstico. Sin red.

Lee -o (archivo del cuerpo), -D (archivo de cabeceras), -w (solo se usa %{http_code}), -X, -H (cabeceras),
--data-binary @archivo y la URL, como curl. Responde así:
  GET /admin/api/sesion                      401 no_autenticado (FAKE_CURL_SESION)
  PUT /admin/api/sesion                      405 de la API (control del diagnóstico)
  POST /admin/api/sesion/solicitar, válido  202 (FAKE_CURL_SOLICITAR_VALIDO). Válido = Origin correcto,
                                            Content-Type JSON, X-Admin-Request: 1, y x-amz-content-sha256 igual al
                                            SHA-256 de los bytes realmente enviados.
  POST /admin/api/sesion/solicitar, inválido 400 de la API (FAKE_CURL_SOLICITAR), sin efecto.
  otra ruta /admin/api/...                  404 de la API
  fuera de /admin/api/                      404 del sitio (página de error de S3, sin formato de la API)

FAKE_CURL_MAPEO=1 reproduce el borde observado: CloudFront reemplaza los 403 y 404 de la API por la página de error
del sitio con estado 404 (server AmazonS3, x-cache Error from cloudfront). La Lambda sí respondió: queda registrada.

Cada petición se agrega como una línea JSON a FAKE_CURL_REQ. Variables: FAKE_LOG (registro "curl <método> <ruta>"),
FAKE_INVOCACIONES (una marca en ms por cada petición que llega a la API; la lee el simulador de logs).
"""
import hashlib
import json
import os
import sys
import time

ORIGEN = 'https://ork3d.com'
CUERPO_401 = '{"ok": false, "error": "no_autenticado", "mensaje": "Tenés que iniciar sesión."}'
CUERPO_400 = '{"ok": false, "error": "origen", "mensaje": "Origen no permitido."}'
CUERPO_202 = '{"ok": true}'
CUERPO_405 = '{"ok": false, "error": "metodo", "mensaje": "Método no permitido."}'
CUERPO_404_API = '{"ok": false, "error": "no_encontrado", "mensaje": "No existe."}'
CUERPO_404_SITIO = '<!DOCTYPE html>\n<html lang="es">\n<head><title>ORK3D · Página no encontrada</title></head>\n</html>\n'


def leer_cabeceras_y_cuerpo(args):
    cabeceras, cuerpo = {}, None
    i = 0
    while i < len(args):
        a = args[i]
        if a == '-H':
            nombre, _, valor = args[i + 1].partition(':')
            cabeceras[nombre.strip().lower()] = valor.strip()
            i += 2
        elif a == '--data-binary':
            ruta = args[i + 1]
            ruta = ruta[1:] if ruta.startswith('@') else ruta
            with open(ruta, 'rb') as f:
                cuerpo = f.read()
            i += 2
        else:
            i += 1
    return cabeceras, cuerpo


def leer_argumentos(args):
    salida, cabeceras_salida, formato, metodo, url = None, None, '', 'GET', ''
    i = 0
    while i < len(args):
        a = args[i]
        if a == '-o':
            salida = args[i + 1]
            i += 2
        elif a == '-D':
            cabeceras_salida = args[i + 1]
            i += 2
        elif a == '-w':
            formato = args[i + 1]
            i += 2
        elif a == '-X':
            metodo = args[i + 1]
            i += 2
        elif a in ('-H', '--data-binary', '--max-time'):
            i += 2
        elif a.startswith('-'):
            i += 1
        else:
            url = a
            i += 1
    return salida, cabeceras_salida, formato, metodo, url


def respuesta_de_la_api(ruta, metodo, cabeceras, cuerpo):
    """Respuesta de la Lambda admin: (código, cuerpo, valido) o None si la ruta no es de la API."""
    if ruta == '/admin/api/sesion' and metodo == 'GET':
        return os.environ.get('FAKE_CURL_SESION', '401'), os.environ.get('FAKE_CURL_SESION_CUERPO', CUERPO_401), False
    if ruta == '/admin/api/sesion' and metodo == 'PUT':
        return '405', CUERPO_405, False
    if ruta == '/admin/api/sesion/solicitar' and metodo == 'POST':
        sha_ok = cabeceras.get('x-amz-content-sha256') == hashlib.sha256(cuerpo or b'').hexdigest()
        valido = (cabeceras.get('origin') == ORIGEN
                  and cabeceras.get('content-type', '').split(';')[0] == 'application/json'
                  and cabeceras.get('x-admin-request') == '1'
                  and sha_ok and cuerpo is not None)
        if valido:
            return (os.environ.get('FAKE_CURL_SOLICITAR_VALIDO', '202'),
                    os.environ.get('FAKE_CURL_SOLICITAR_VALIDO_CUERPO', CUERPO_202), True)
        return (os.environ.get('FAKE_CURL_SOLICITAR', '400'),
                os.environ.get('FAKE_CURL_SOLICITAR_CUERPO', CUERPO_400), False)
    if ruta.startswith('/admin/api/'):
        return '404', CUERPO_404_API, False
    return None


MAPA_SITIO = {'/': 'index.html', '/opiniones': 'opiniones.html', '/admin/entrar': 'admin-web/entrar.html',
              '/admin/resenas': 'admin-web/resenas.html'}


def respuesta_del_sitio(ruta):
    """Página del sitio servida desde FAKE_SITIO_DIR (copia de lo publicado). None si la ruta no es del sitio."""
    carpeta = os.environ.get('FAKE_SITIO_DIR', '')
    if not carpeta or not (ruta in MAPA_SITIO or ruta.startswith('/assets/js/')):
        return None
    relativa = MAPA_SITIO.get(ruta, ruta.lstrip('/'))
    origen = os.path.join(carpeta, relativa)
    if not os.path.isfile(origen):
        return '404', b'<html>pagina no encontrada</html>', {'content-type': 'text/html', 'x-cache': 'Error from cloudfront'}
    with open(origen, 'rb') as f:
        contenido = f.read()
    cache = 'no-store, max-age=0' if relativa.startswith(('admin-web/', 'assets/js/admin')) else 'max-age=60'
    return '200', contenido, {'content-type': 'text/html; charset=utf-8', 'cache-control': cache,
                              'x-cache': 'Miss from cloudfront'}


def main():
    salida, cabeceras_salida, formato, metodo, url = leer_argumentos(sys.argv[1:])
    if 'fake-code.example' in url:
        # Descarga del código anterior de la Lambda (URL firmada de Lambda). Sirve el archivo de FAKE_CODIGO_ANTERIOR_ARCHIVO.
        origen = os.environ.get('FAKE_CODIGO_ANTERIOR_ARCHIVO', '')
        if not origen or not os.path.exists(origen):
            return 6
        with open(origen, 'rb') as f:
            contenido = f.read()
        if salida:
            with open(salida, 'wb') as f:
                f.write(contenido)
        return 0
    ruta = url.split('ork3d.com', 1)[-1]
    cabeceras, cuerpo = leer_cabeceras_y_cuerpo(sys.argv[1:])
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'curl {metodo} {ruta}\n')

    cuerpo_bytes = None
    sitio = respuesta_del_sitio(ruta) if metodo == 'GET' else None
    api = None if sitio is not None else respuesta_de_la_api(ruta, metodo, cabeceras, cuerpo)
    if sitio is not None:
        codigo, cuerpo_bytes, cabeceras_resp = sitio
        cuerpo_resp, valido = '', False
    elif api is None:
        codigo, cuerpo_resp, valido = '404', CUERPO_404_SITIO, False
        cabeceras_resp = {'content-type': 'text/html; charset=utf-8', 'server': 'AmazonS3',
                          'x-cache': 'Error from cloudfront'}
    else:
        codigo, cuerpo_resp, valido = api
        cabeceras_resp = {'content-type': 'application/json; charset=utf-8', 'x-cache': 'Miss from cloudfront'}
        if os.environ.get('FAKE_INVOCACIONES'):
            with open(os.environ['FAKE_INVOCACIONES'], 'a', encoding='utf-8') as f:
                f.write(f'{int(time.time() * 1000)}\n')
        if os.environ.get('FAKE_CURL_MAPEO') == '1' and codigo in ('403', '404'):
            codigo, cuerpo_resp = '404', CUERPO_404_SITIO
            cabeceras_resp = {'content-type': 'text/html; charset=utf-8', 'server': 'AmazonS3',
                              'x-cache': 'Error from cloudfront'}

    if os.environ.get('FAKE_CURL_REQ'):
        with open(os.environ['FAKE_CURL_REQ'], 'a', encoding='utf-8') as f:
            f.write(json.dumps({'metodo': metodo, 'ruta': ruta, 'cabeceras': cabeceras,
                                'cuerpo': None if cuerpo is None else cuerpo.decode('utf-8'),
                                'valido': valido}, ensure_ascii=False) + '\n')
    if salida:
        with open(salida, 'wb') as f:
            f.write(cuerpo_bytes if cuerpo_bytes is not None else cuerpo_resp.encode('utf-8'))
    if cabeceras_salida:
        with open(cabeceras_salida, 'w', encoding='utf-8') as f:
            f.write(f'HTTP/2 {codigo}\r\n')
            for nombre, valor in cabeceras_resp.items():
                f.write(f'{nombre}: {valor}\r\n')
            f.write('\r\n')
    if '%{http_code}' in formato:
        sys.stdout.write(codigo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
