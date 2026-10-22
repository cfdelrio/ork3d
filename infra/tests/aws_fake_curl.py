#!/usr/bin/env python3
"""Simulador de curl para probar el smoke de infra/admin-deploy.sh. Sin red.

Lee -o (archivo del cuerpo), -w (solo se usa %{http_code}), -X, -H (cabeceras), --data-binary @archivo y la URL,
como curl. Responde así:
  GET /admin/api/sesion                      401 no_autenticado (FAKE_CURL_SESION)
  POST /admin/api/sesion/solicitar, válido  202 (FAKE_CURL_SOLICITAR_VALIDO). Válido = Origin correcto,
                                            Content-Type JSON, X-Admin-Request: 1, y x-amz-content-sha256 igual al
                                            SHA-256 de los bytes realmente enviados.
  POST /admin/api/sesion/solicitar, inválido 403 de la API (FAKE_CURL_SOLICITAR), sin efecto.

Cada petición se agrega como una línea JSON a FAKE_CURL_REQ, con método, ruta, cabeceras, cuerpo y "valido".
Variables: FAKE_LOG (registro "curl <método> <ruta>"), FAKE_CURL_REQ (registro detallado).
"""
import hashlib
import json
import os
import sys

ORIGEN = 'https://ork3d.com'
CUERPO_401 = '{"ok": false, "error": "no_autenticado", "mensaje": "Tenés que iniciar sesión."}'
CUERPO_403 = '{"ok": false, "error": "origen", "mensaje": "Origen no permitido."}'
CUERPO_202 = '{"ok": true}'


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


def main():
    args = sys.argv[1:]
    salida, formato, metodo, url = None, '', 'GET', ''
    i = 0
    while i < len(args):
        a = args[i]
        if a == '-o':
            salida = args[i + 1]
            i += 2
        elif a == '-w':
            formato = args[i + 1]
            i += 2
        elif a == '-X':
            metodo = args[i + 1]
            i += 2
        elif a in ('-H', '--data-binary'):
            i += 2
        elif a.startswith('-'):
            i += 1
        else:
            url = a
            i += 1
    ruta = url.split('ork3d.com', 1)[-1]
    cabeceras, cuerpo = leer_cabeceras_y_cuerpo(args)
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'curl {metodo} {ruta}\n')

    valido = False
    if ruta == '/admin/api/sesion' and metodo == 'GET':
        codigo, cuerpo_resp = os.environ.get('FAKE_CURL_SESION', '401'), os.environ.get(
            'FAKE_CURL_SESION_CUERPO', CUERPO_401)
    elif ruta == '/admin/api/sesion/solicitar' and metodo == 'POST':
        sha_ok = cabeceras.get('x-amz-content-sha256') == hashlib.sha256(cuerpo or b'').hexdigest()
        valido = (cabeceras.get('origin') == ORIGEN
                  and cabeceras.get('content-type', '').split(';')[0] == 'application/json'
                  and cabeceras.get('x-admin-request') == '1'
                  and sha_ok and cuerpo is not None)
        if valido:
            codigo = os.environ.get('FAKE_CURL_SOLICITAR_VALIDO', '202')
            cuerpo_resp = os.environ.get('FAKE_CURL_SOLICITAR_VALIDO_CUERPO', CUERPO_202)
        else:
            codigo = os.environ.get('FAKE_CURL_SOLICITAR', '403')
            cuerpo_resp = os.environ.get('FAKE_CURL_SOLICITAR_CUERPO', CUERPO_403)
    else:
        codigo, cuerpo_resp = '404', '{}'

    if os.environ.get('FAKE_CURL_REQ'):
        with open(os.environ['FAKE_CURL_REQ'], 'a', encoding='utf-8') as f:
            f.write(json.dumps({'metodo': metodo, 'ruta': ruta, 'cabeceras': cabeceras,
                                'cuerpo': None if cuerpo is None else cuerpo.decode('utf-8'),
                                'valido': valido}, ensure_ascii=False) + '\n')
    if salida:
        with open(salida, 'w', encoding='utf-8') as f:
            f.write(cuerpo_resp)
    if '%{http_code}' in formato:
        sys.stdout.write(codigo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
