#!/usr/bin/env python3
"""Simulador de curl para probar infra/admin-ui-verify.sh y prod-lectura-v2.sh. Sin red.

Sirve el sitio desde FAKE_ADMIN_UI_SITIO (index.html, opiniones.html, sitemap.xml, admin-web/, assets/), tal como
llegaría de producción. Responde como el sitio real en lo que la verificación mira:
  https://ork3d.com/                    FAKE_ADMIN_UI_SITIO/index.html (con enlace a /opiniones)
  /opiniones, /sitemap.xml              archivos del sitio
  /opinar/<token>                       200 text/html (token inexistente)
  /admin/resenas, /admin/entrar         admin-web/resenas.html y entrar.html, no-store
  /admin/api/sesion                     401 no_autenticado, sin cookie
  /resenas/publicas.json                FAKE_ADMIN_UI_EXPORT (por defecto, export vacío)
  /assets/... y /admin-web/...          archivos del sitio; 404 si faltan
  https://www.ork3d.com/                301 a https://ork3d.com/
Cualquier otra petición: 404. Cualquier método que no sea GET: 405 (el tooling no debe pedir nada más).

Argumentos como curl: -D (cabeceras), -o (cuerpo), -w (solo %{http_code}, %{content_type}, %{redirect_url}), -sS,
--max-time, y la URL. Sin -o, el cuerpo sale por stdout. Cada petición se registra en FAKE_ADMIN_UI_CURL_LOG.
"""
import json
import mimetypes
import os
import sys
from urllib.parse import urlsplit

SITIO = os.environ.get('FAKE_ADMIN_UI_SITIO', '')
ESTADO = os.environ.get('FAKE_ADMIN_UI_ESTADO', '')
EXPORT = os.environ.get('FAKE_ADMIN_UI_EXPORT', '{"items": []}')
LOG = os.environ.get('FAKE_ADMIN_UI_CURL_LOG', '')
CON_VALOR = {'--max-time', '-D', '-o', '-w', '-X', '-H', '--data-binary', '--data', '-d', '-A', '--user-agent'}
TIPOS = {'.html': 'text/html; charset=utf-8', '.css': 'text/css; charset=utf-8',
         '.js': 'application/javascript; charset=utf-8', '.json': 'application/json',
         '.xml': 'application/xml; charset=utf-8', '.jpg': 'image/jpeg'}
NO_STORE = 'no-store, max-age=0'
PUBLICO = 'public, max-age=60, must-revalidate'


def analizar(args):
    opciones, url, metodo = {}, None, 'GET'
    i = 0
    while i < len(args):
        a = args[i]
        if a in CON_VALOR and i + 1 < len(args):
            opciones[a] = args[i + 1]
            i += 2
            continue
        if a == '-X' and i + 1 < len(args):
            metodo = args[i + 1]
        if not a.startswith('-'):
            url = a
        i += 1
    return opciones, url, metodo


def archivo(ruta_relativa):
    ruta = os.path.join(SITIO, ruta_relativa.lstrip('/'))
    if not os.path.isfile(ruta):
        return None
    with open(ruta, 'rb') as f:
        return f.read()


def objeto_s3(clave):
    """Lo que la publicación dejó en el bucket simulado: cuerpo y cabeceras guardadas. None si no hay objeto."""
    if not ESTADO:
        return None
    ruta = os.path.join(ESTADO, 's3', clave)
    if not os.path.isfile(ruta):
        return None
    with open(ruta, 'rb') as f:
        cuerpo = f.read()
    with open(ruta + '.json', encoding='utf-8') as f:
        meta = json.load(f)
    return cuerpo, meta


def respuesta_archivo(ruta, admin):
    # El bucket manda: si la publicación dejó el objeto, se sirve tal cual; si no, el sitio de la prueba.
    guardado = objeto_s3(ruta.lstrip('/'))
    if guardado is not None:
        cuerpo, meta = guardado
        tipo = meta.get('ContentType') or 'application/octet-stream'
        cache = meta.get('CacheControl') or PUBLICO
        return 200, tipo, cache, cuerpo, ''
    cuerpo = archivo(ruta)
    if cuerpo is None:
        return 404, 'text/html; charset=utf-8', PUBLICO, b'<!DOCTYPE html><title>No encontrado</title>', ''
    tipo = TIPOS.get(os.path.splitext(ruta)[1], mimetypes.guess_type(ruta)[0] or 'application/octet-stream')
    return 200, tipo, NO_STORE if admin else PUBLICO, cuerpo, ''


def enrutar(host, ruta):
    """(estado, content-type, cache-control, cuerpo, location)."""
    if host == 'www.ork3d.com':
        return 301, 'text/html; charset=utf-8', PUBLICO, b'', 'https://ork3d.com/'
    if host != 'ork3d.com':
        return 404, 'text/html; charset=utf-8', PUBLICO, b'', ''
    if ruta == '/':
        return respuesta_archivo('index.html', False)
    if ruta == '/opiniones':
        return respuesta_archivo('opiniones.html', False)
    if ruta == '/sitemap.xml':
        return respuesta_archivo('sitemap.xml', False)
    if ruta.startswith('/opinar/'):
        return 200, 'text/html; charset=utf-8', PUBLICO, b'<!DOCTYPE html><title>Opinar</title>', ''
    if ruta == '/admin/api/sesion':
        cuerpo = '{"ok": false, "error": "no_autenticado", "mensaje": "Tenés que iniciar sesión."}'
        return 401, 'application/json', NO_STORE, cuerpo.encode('utf-8'), ''
    if ruta == '/resenas/publicas.json':
        return 200, 'application/json', PUBLICO, EXPORT.encode('utf-8'), ''
    if ruta == '/admin/resenas':
        return respuesta_archivo('admin-web/resenas.html', True)
    if ruta == '/admin/entrar':
        return respuesta_archivo('admin-web/entrar.html', True)
    if ruta.startswith('/assets/') or ruta.startswith('/admin-web/'):
        return respuesta_archivo(ruta, True)
    return 404, 'text/html; charset=utf-8', PUBLICO, b'<!DOCTYPE html><title>No encontrado</title>', ''


def main(args):
    opciones, url, metodo = analizar(args)
    partes = urlsplit(url)
    ruta = partes.path or '/'
    if LOG:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'metodo': metodo, 'url': url}) + '\n')
    if metodo != 'GET':
        estado, tipo, cache, cuerpo, location = 405, 'application/json', NO_STORE, b'{"ok": false}', ''
    else:
        estado, tipo, cache, cuerpo, location = enrutar(partes.hostname or '', ruta)
        # FAKE_ADMIN_UI_CURL_FORZAR=/ruta=CODIGO: responde ese código para esa ruta (prueba de respuestas inesperadas).
        forzar_ruta, _, forzar_codigo = os.environ.get('FAKE_ADMIN_UI_CURL_FORZAR', '').partition('=')
        if forzar_codigo and ruta == forzar_ruta:
            estado, tipo, cache, cuerpo, location = int(forzar_codigo), 'text/html; charset=utf-8', PUBLICO, b'error', ''

    if '-D' in opciones:
        with open(opciones['-D'], 'w', encoding='utf-8') as f:
            f.write(f'HTTP/2 {estado}\r\ncontent-type: {tipo}\r\ncache-control: {cache}\r\n')
            if location:
                f.write(f'location: {location}\r\n')
            f.write('\r\n')
    if '-o' in opciones:
        if opciones['-o'] != '/dev/null':
            with open(opciones['-o'], 'wb') as f:
                f.write(cuerpo)
    elif '-w' not in opciones:
        sys.stdout.buffer.write(cuerpo)
    if '-w' in opciones:
        formato = opciones['-w']
        salida = (formato.replace('%{http_code}', str(estado))
                  .replace('%{content_type}', tipo)
                  .replace('%{redirect_url}', location))
        sys.stdout.write(salida)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
