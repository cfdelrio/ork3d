#!/usr/bin/env python3
"""Ayuda de la verificación de lectura de producción (infra/prod-lectura-v2.sh). Sin AWS ni red: solo lee archivos.

  exportar ARCHIVO                 PASS/FAIL: resenas/publicas.json tiene exactamente la allowlist pública
  fotos ARCHIVO                    imprime la photo_url de cada item del export que tiene foto
  robots ARCHIVO VALOR             PASS/FAIL: <meta name="robots"> tiene exactamente VALOR
  sin-enlace ARCHIVO RUTA          PASS/FAIL: la página no enlaza RUTA (p. ej. /opiniones)
  sitemap-sin ARCHIVO RUTA         PASS/FAIL: el sitemap no lista RUTA
  flag-notificaciones ARCHIVO      PASS/FAIL: RESENAS_NOTIFY_ENABLED de la configuración de la Lambda está apagado
  logs-limpios ARCHIVO             PASS/FAIL: ningún evento de la salida de filter-log-events (sin filtro, 24 h) trae
                                   cookie ni Set-Cookie en su texto. Solo imprime conteos y nombres de categoría.
"""
import json
import re
import sys

ALLOWLIST_EXPORT = ('id', 'rating', 'comment', 'aspects', 'display_name', 'category', 'product_label',
                    'verified', 'photo_url', 'published_at')
# Un valor con forma de correo o de token en el export es una fuga, aunque la clave sea permitida.
PATRON_CORREO = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
PATRON_TOKEN_HASH = re.compile(r'[a-f0-9]{64}')


def _leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def exportar(ruta):
    texto = _leer(ruta)
    try:
        datos = json.loads(texto)
    except ValueError:
        return 'FAIL: el export no es JSON'
    items = datos.get('items') if isinstance(datos, dict) else None
    if not isinstance(items, list):
        return 'FAIL: el export no tiene la lista items'
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return f'FAIL: el item {i} no es un objeto'
        if set(item) != set(ALLOWLIST_EXPORT):
            extra = sorted(set(item) - set(ALLOWLIST_EXPORT))
            faltan = sorted(set(ALLOWLIST_EXPORT) - set(item))
            return f'FAIL: item {i} fuera de la allowlist (sobran {extra}, faltan {faltan})'
    if PATRON_CORREO.search(texto):
        return 'FAIL: el export contiene una dirección de correo'
    if PATRON_TOKEN_HASH.search(texto):
        return 'FAIL: el export contiene un hash de 64 caracteres (token_hash)'
    return f'PASS: {len(items)} reseña(s), solo la allowlist'


def fotos(ruta):
    datos = json.loads(_leer(ruta))
    return [item['photo_url'] for item in datos.get('items', []) if item.get('photo_url')]


def robots(ruta, valor):
    m = re.search(r'<meta name="robots" content="([^"]*)"', _leer(ruta))
    if not m:
        return 'FAIL: la página no declara robots'
    if m.group(1) != valor:
        return f'FAIL: robots es "{m.group(1)}", se esperaba "{valor}"'
    return f'PASS: robots "{valor}"'


def sin_enlace(ruta, destino):
    nombre = destino.lstrip('/')
    if re.search(r'href=["\']/?' + re.escape(nombre) + r'["\'/#?]', _leer(ruta)):
        return f'FAIL: la página enlaza {destino}'
    return f'PASS: la página no enlaza {destino}'


def sitemap_sin(ruta, destino):
    if destino.lstrip('/') in _leer(ruta):
        return f'FAIL: el sitemap lista {destino}'
    return f'PASS: el sitemap no lista {destino}'


def flag_notificaciones(ruta):
    """Lee la salida de get-function-configuration y solo mira RESENAS_NOTIFY_ENABLED (nunca imprime el resto)."""
    try:
        cfg = json.loads(_leer(ruta))
    except ValueError:
        return 'FAIL: la configuración de la Lambda no es JSON'
    valor = ((cfg.get('Environment') or {}).get('Variables') or {}).get('RESENAS_NOTIFY_ENABLED')
    if valor is None or str(valor).lower() == 'false':
        return 'PASS: RESENAS_NOTIFY_ENABLED apagado (ausente o false)'
    return f'FAIL: RESENAS_NOTIFY_ENABLED={valor}; las notificaciones están encendidas'


# Señales de cookie en el texto de un evento, sin distinguir mayúsculas. La prosa ("la cookie dura 8 h", "HttpOnly
# en la documentación") y el código ("cookie = leer(evento)" en un traceback) no cuentan: las cabeceras se reconocen
# por los dos puntos, y el atributo HttpOnly por ir detrás de un par nombre=valor.
PATRONES_COOKIE = (
    ('nombre de la cookie de sesión', re.compile(r'ork3d_admin', re.IGNORECASE)),
    ('cabecera Set-Cookie', re.compile(r'(?<![\w-])set-cookie["\']?\s*:', re.IGNORECASE)),
    ('cabecera Cookie', re.compile(r'(?<![\w-])cookie["\']?\s*:', re.IGNORECASE)),
    ('atributo HttpOnly de una cookie', re.compile(r'[\w.-]+=[^;\n]*;[^\n]*?\bhttponly\b', re.IGNORECASE)),
)


def logs_limpios(ruta):
    """Busca PATRONES_COOKIE en el texto de TODOS los eventos de la salida de filter-log-events (sin filtro).

    Cuenta eventos con señal, no eventos devueltos. Nunca imprime el texto. Un evento sin texto legible falla:
    no se puede afirmar que esté limpio.
    """
    try:
        datos = json.loads(_leer(ruta))
    except ValueError:
        return 'FAIL: la salida de los logs no es JSON'
    eventos = datos.get('events', []) if isinstance(datos, dict) else None
    if not isinstance(eventos, list):
        return 'FAIL: la salida de los logs no tiene la lista events'
    sin_texto = sum(1 for e in eventos if not isinstance(e, dict) or not isinstance(e.get('message'), str))
    if sin_texto:
        return f'FAIL: {sin_texto} evento(s) sin texto legible: no se pueden revisar'
    categorias = {}
    con_senal = 0
    for evento in eventos:
        halladas = [nombre for nombre, patron in PATRONES_COOKIE if patron.search(evento['message'])]
        if halladas:
            con_senal += 1
            for nombre in halladas:
                categorias[nombre] = categorias.get(nombre, 0) + 1
    if con_senal:
        resumen = ', '.join(f'{nombre} ({n})' for nombre, n in sorted(categorias.items()))
        return f'FAIL: {con_senal} de {len(eventos)} evento(s) con cookie o Set-Cookie: {resumen}'
    return f'PASS: {len(eventos)} evento(s) revisados'


def main(argv):
    orden, args = argv[0], argv[1:]
    if orden == 'exportar':
        resultado = exportar(args[0])
        print(resultado)
        return 0 if resultado.startswith('PASS') else 1
    if orden == 'fotos':
        for url in fotos(args[0]):
            print(url)
        return 0
    if orden == 'robots':
        resultado = robots(*args)
    elif orden == 'sin-enlace':
        resultado = sin_enlace(*args)
    elif orden == 'sitemap-sin':
        resultado = sitemap_sin(*args)
    elif orden == 'flag-notificaciones':
        resultado = flag_notificaciones(*args)
    elif orden == 'logs-limpios':
        resultado = logs_limpios(*args)
    else:
        raise SystemExit(f'orden desconocida: {orden}')
    print(resultado)
    return 0 if resultado.startswith('PASS') else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
