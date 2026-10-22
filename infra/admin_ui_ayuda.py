#!/usr/bin/env python3
"""Ayuda de la publicación de los estáticos del panel admin (infra/admin-ui-publicar.sh y admin-ui-verify.sh).

Sin AWS ni red: solo lee texto y JSON que el script ya obtuvo. Cada orden imprime su resultado y sale con 0 si es
correcta, 1 si no (PASS:/FAIL: en las de verificación).

  estaticos-admin                 por stdin, una ruta por línea (git diff --name-only); imprime solo los estáticos del panel
  validar-manifiesto ARCHIVO      5 columnas, sin campos vacíos, sin claves repetidas, rutas válidas y nunca /*
  rutas ARCHIVO                   rutas a invalidar: únicas y ordenadas, una por línea
  copia-entrada HEAD|- CLAVE      entrada de la copia de rollback (JSON), desde head-object o - si el objeto no existía
  args-put ENTRADA                argumentos de put-object que reproducen la entrada, uno por línea
  existia ENTRADA                 si / no
  cabeceras HEAD CT CC            PASS/FAIL: Content-Type y Cache-Control remotos son los del manifiesto
  cabeceras-entrada HEAD ENTRADA  PASS/FAIL: las cabeceras remotas son las de la copia (rollback exacto)
  csp HTML                        PASS/FAIL: CSP con default-src, script-src y style-src 'self', y object-src 'none'
  noindex HTML                    PASS/FAIL: <meta name="robots"> exacto "noindex, nofollow"
  sin-estilos HTML                PASS/FAIL: ningún atributo style=
  sin-scripts-inline HTML         PASS/FAIL: ningún <script> sin src
  sin-innerhtml JS...             PASS/FAIL: ningún innerHTML =, insertAdjacentHTML, document.write ni outerHTML =
  referencias HTML                imprime las URL /assets/... que la página referencia (con ?v=)
  contiene ARCHIVO TEXTO...       PASS/FAIL: todos los textos aparecen en el archivo
"""
import json
import re
import sys

ESTATICOS = (
    re.compile(r'^admin-web/(entrar|resenas)\.html$'),
    re.compile(r'^assets/css/admin\.css$'),
    re.compile(r'^assets/js/admin-[^/]+\.js$'),
)
RUTA_PROHIBIDA = '/*'
CSP_RE = re.compile(r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]*)"', re.IGNORECASE)
SCRIPT_RE = re.compile(r'<script\b([^>]*)>', re.IGNORECASE)
INNERHTML_RE = re.compile(r'\.innerHTML\s*=|insertAdjacentHTML|document\.write|\.outerHTML\s*=')
STYLE_RE = re.compile(r'\sstyle\s*=')
ROBOTS_RE = re.compile(r'<meta name="robots" content="([^"]*)"')
# Solo los assets del panel: el favicon y otros assets compartidos del sitio no son parte de este despliegue.
REF_RE = re.compile(r'(?:src|href)="(/assets/(?:css/admin|js/admin-)[^"]*)"')


def leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def leer_json(ruta):
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def filas(ruta):
    """Filas del manifiesto sin comentarios ni líneas vacías: [(numero, campos)]."""
    salida = []
    for numero, linea in enumerate(leer(ruta).splitlines(), 1):
        if not linea.strip() or linea.startswith('#'):
            continue
        salida.append((numero, linea.split('\t')))
    return salida


def validar_manifiesto(ruta):
    errores = []
    datos = filas(ruta)
    if not datos:
        return ['el manifiesto está vacío']
    archivos, claves = set(), set()
    for numero, campos in datos:
        if len(campos) != 5:
            errores.append(f'línea {numero}: {len(campos)} columnas, se esperaban 5')
            continue
        archivo, clave, ct, cc, rutas = campos
        if not all(campos):
            errores.append(f'línea {numero}: hay un campo vacío')
        if archivo in archivos or clave in claves:
            errores.append(f'línea {numero}: archivo o clave repetidos')
        if clave != archivo:
            errores.append(f'línea {numero}: la clave {clave} no es el archivo {archivo}')
        archivos.add(archivo)
        claves.add(clave)
        for ruta_web in rutas.split(','):
            if ruta_web == RUTA_PROHIBIDA or not ruta_web.startswith('/') or ' ' in ruta_web:
                errores.append(f'línea {numero}: ruta de invalidación no válida: {ruta_web!r}')
    return errores


def rutas(ruta):
    unicas = set()
    for _, campos in filas(ruta):
        unicas.update(campos[4].split(','))
    return sorted(unicas)


def copia_entrada(head, clave):
    if head == '-':
        return {'clave': clave, 'existia': False}
    datos = leer_json(head)
    return {
        'clave': clave,
        'existia': True,
        'content_type': datos.get('ContentType', ''),
        'cache_control': datos.get('CacheControl', ''),
        'content_encoding': datos.get('ContentEncoding', ''),
        'content_language': datos.get('ContentLanguage', ''),
        'content_disposition': datos.get('ContentDisposition', ''),
        'metadata': datos.get('Metadata', {}),
        'etag': datos.get('ETag', ''),
    }


def args_put(entrada):
    args = []
    for campo, opcion in (('content_type', '--content-type'), ('cache_control', '--cache-control'),
                          ('content_encoding', '--content-encoding'), ('content_language', '--content-language'),
                          ('content_disposition', '--content-disposition')):
        if entrada.get(campo):
            args += [opcion, entrada[campo]]
    if entrada.get('metadata'):
        args += ['--metadata'] + [f'{k}={v}' for k, v in sorted(entrada['metadata'].items())]
    return args


def comparar(nombre, esperado, real):
    if esperado == real:
        return None
    return f'{nombre}: {real!r}, se esperaba {esperado!r}'


def cabeceras_manifiesto(head, ct, cc):
    datos = leer_json(head)
    return [d for d in (comparar('Content-Type', ct, datos.get('ContentType', '')),
                        comparar('Cache-Control', cc, datos.get('CacheControl', ''))) if d]


def cabeceras_copia(head, entrada):
    datos = leer_json(head)
    diferencias = [d for d in (
        comparar('Content-Type', entrada.get('content_type', ''), datos.get('ContentType', '')),
        comparar('Cache-Control', entrada.get('cache_control', ''), datos.get('CacheControl', '')),
        comparar('Content-Encoding', entrada.get('content_encoding', ''), datos.get('ContentEncoding', '')),
        comparar('Content-Language', entrada.get('content_language', ''), datos.get('ContentLanguage', '')),
        comparar('Content-Disposition', entrada.get('content_disposition', ''), datos.get('ContentDisposition', '')),
        comparar('Metadata', entrada.get('metadata', {}), datos.get('Metadata', {})),
    ) if d]
    return diferencias


def csp(texto):
    m = CSP_RE.search(texto)
    if not m:
        return 'no hay CSP (meta http-equiv)'
    politica = m.group(1)
    faltan = [d for d in ("default-src 'self'", "script-src 'self'", "style-src 'self'", "object-src 'none'")
              if d not in politica]
    return f'faltan {faltan}' if faltan else None


def noindex(texto):
    m = ROBOTS_RE.search(texto)
    if not m:
        return 'no declara robots'
    return None if m.group(1) == 'noindex, nofollow' else f'robots es {m.group(1)!r}'


def referencias(texto):
    vistos = []
    for url in REF_RE.findall(texto):
        if url not in vistos:
            vistos.append(url)
    return vistos


def _resultado(ok, nombre, detalle=None):
    if ok:
        print(f'PASS: {nombre}')
        return 0
    print(f'FAIL: {nombre}' + (f' — {detalle}' if detalle else ''))
    return 1


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    orden, args = argv[0], argv[1:]

    if orden == 'estaticos-admin':
        for linea in sys.stdin.read().splitlines():
            if any(p.match(linea) for p in ESTATICOS):
                print(linea)
        return 0

    if orden == 'validar-manifiesto':
        errores = validar_manifiesto(args[0])
        return _resultado(not errores, 'manifiesto válido', '; '.join(errores))

    if orden == 'rutas':
        errores = validar_manifiesto(args[0])
        if errores:
            print('; '.join(errores), file=sys.stderr)
            return 1
        print('\n'.join(rutas(args[0])))
        return 0

    if orden == 'copia-entrada':
        print(json.dumps(copia_entrada(args[0], args[1]), indent=2, sort_keys=True))
        return 0

    if orden == 'args-put':
        print('\n'.join(args_put(leer_json(args[0]))))
        return 0

    if orden == 'existia':
        print('si' if leer_json(args[0]).get('existia') else 'no')
        return 0

    if orden == 'cabeceras':
        diferencias = cabeceras_manifiesto(args[0], args[1], args[2])
        return _resultado(not diferencias, 'Content-Type y Cache-Control como en el manifiesto', '; '.join(diferencias))

    if orden == 'cabeceras-entrada':
        diferencias = cabeceras_copia(args[0], leer_json(args[1]))
        return _resultado(not diferencias, 'cabeceras como en la copia de rollback', '; '.join(diferencias))

    if orden == 'csp':
        error = csp(leer(args[0]))
        return _resultado(error is None, 'CSP presente y estricta', error)

    if orden == 'noindex':
        error = noindex(leer(args[0]))
        return _resultado(error is None, 'noindex, nofollow exacto', error)

    if orden == 'sin-estilos':
        return _resultado(not STYLE_RE.search(leer(args[0])), 'sin atributos style=')

    if orden == 'sin-scripts-inline':
        inline = [m.group(0) for m in SCRIPT_RE.finditer(leer(args[0])) if 'src=' not in m.group(1)]
        return _resultado(not inline, 'sin scripts inline', f'{len(inline)} script(s) sin src')

    if orden == 'sin-innerhtml':
        codigo = 0
        for ruta in args:
            hallazgos = INNERHTML_RE.findall(leer(ruta))
            codigo |= _resultado(not hallazgos, f'sin innerHTML ni HTML crudo en {ruta}', f'{len(hallazgos)} uso(s)')
        return codigo

    if orden == 'referencias':
        print('\n'.join(referencias(leer(args[0]))))
        return 0

    if orden == 'contiene':
        texto = leer(args[0])
        faltan = [t for t in args[1:] if t not in texto]
        return _resultado(not faltan, f'{len(args) - 1} texto(s) presentes en {args[0]}', f'faltan {faltan}')

    print(f'orden desconocida: {orden}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
