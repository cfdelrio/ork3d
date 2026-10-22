#!/usr/bin/env python3
"""Ayuda de la publicación del panel admin (infra/admin-frontend-deploy.sh). Sin AWS ni red.

  bloque-admin ARCHIVO                 0 si infra/redirect-www.js tiene el bloque admin completo; 1 si no
  funcion LIVE REPO                    imprime IGUAL, ACTUALIZAR o DISTINTO (ver abajo)
  config-funcion META SALIDA           escribe {Comment, Runtime} de la función para update-function
  invalidacion SALIDA REFERENCIA       escribe el lote de invalidación del panel

Compara el código LIVE de la función de CloudFront con el del repo:
  IGUAL       coinciden byte a byte: no se escribe nada.
  ACTUALIZAR  LIVE es el repo sin el bloque admin: solo faltan las reglas del panel. Se puede publicar.
  DISTINTO    cualquier otra diferencia: el script no escribe. Puede ser un cambio del sitio público no revisado.
"""
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys

INICIO_BLOQUE = '  // Panel admin V2.'
FIN_BLOQUE = '  // Extensionless path'
REGLAS_ADMIN = ('/admin-web/resenas.html', '/admin-web/entrar.html', '/admin/api/')
RUNTIME_CONOCIDOS = ('cloudfront-js-1.0', 'cloudfront-js-2.0')
RUTAS_INVALIDAR = ['/admin/entrar', '/admin/resenas', '/admin/resenas/*', '/admin-web/*', '/assets/js/admin-*']


def leer_texto(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


def bloque_admin(texto):
    """Devuelve el bloque admin o lanza ValueError si el texto no tiene exactamente un bloque completo."""
    if texto.count(INICIO_BLOQUE) != 1 or texto.count(FIN_BLOQUE) != 1:
        raise ValueError('marcadores del bloque admin ausentes o repetidos')
    i, j = texto.index(INICIO_BLOQUE), texto.index(FIN_BLOQUE)
    if j < i:
        raise ValueError('el bloque admin está desordenado')
    return texto[i:j]


def sin_bloque(texto):
    """El código sin el bloque admin: lo que tendría la función antes de agregar las reglas del panel."""
    bloque = bloque_admin(texto)
    for regla in REGLAS_ADMIN:
        if regla not in bloque:
            raise ValueError(f'el bloque admin no contiene {regla}')
    return texto.replace(bloque, '', 1)


def funcion(live, repo):
    texto_live, texto_repo = leer_texto(live), leer_texto(repo)
    if texto_live == texto_repo:
        return 'IGUAL'
    try:
        if texto_live == sin_bloque(texto_repo):
            return 'ACTUALIZAR'
    except ValueError:
        pass
    return 'DISTINTO'


ANCLA_COMENTARIO = '  // Extensionless path'
ANCLA_REWRITE = "  if (uri !== '/' && uri.indexOf('.') === -1)"


def escribir(ruta, texto):
    with open(ruta, 'w', encoding='utf-8') as f:
        f.write(texto)
    os.chmod(ruta, 0o600)


def ancla(texto):
    """Posición donde se inserta el bloque admin: antes del rewrite de extensionless. Debe aparecer una sola vez."""
    for marca in (ANCLA_COMENTARIO, ANCLA_REWRITE):
        if texto.count(marca) == 1:
            return texto.index(marca)
    if ANCLA_COMENTARIO not in texto and ANCLA_REWRITE not in texto:
        raise ValueError('falta el rewrite de extensionless (ancla del bloque admin)')
    raise ValueError('el rewrite de extensionless aparece más de una vez')


def estado(live, repo):
    """PUBLICADA, SIN_BLOQUE o INCOMPATIBLE: <motivo>. Solo lee."""
    texto_live, bloque = leer_texto(live), bloque_admin(leer_texto(repo))
    if bloque in texto_live:
        return 'PUBLICADA' if texto_live.count(bloque) == 1 else 'INCOMPATIBLE: bloque admin repetido'
    codigo_live = sin_comentarios(texto_live)
    if '/admin' in codigo_live or 'admin-web' in codigo_live:
        return 'INCOMPATIBLE: LIVE ya tiene reglas /admin distintas del bloque del repo'
    try:
        i = ancla(texto_live)
    except ValueError as exc:
        return f'INCOMPATIBLE: {exc}'
    antes = texto_live[:i]
    if not re.search(r'var\s+request\s*=\s*event\.request', antes):
        return 'INCOMPATIBLE: no se encuentra var request = event.request antes del rewrite'
    if not re.search(r'var\s+uri\s*=\s*request\.uri', antes):
        return 'INCOMPATIBLE: no se encuentra var uri = request.uri antes del rewrite'
    return 'SIN_BLOQUE'


def inyectar(live, repo, salida):
    """Escribe LIVE con el bloque admin insertado en el ancla. No quita ni cambia nada de LIVE."""
    texto_live = leer_texto(live)
    bloque = bloque_admin(leer_texto(repo))
    i = ancla(texto_live)
    escribir(salida, texto_live[:i] + bloque + texto_live[i:])
    return 0


def verificar(live, propuesta, repo):
    """0 si la propuesta es LIVE con el bloque admin y nada más. ValueError si cambia algo de LIVE."""
    texto_live, texto_prop = leer_texto(live), leer_texto(propuesta)
    bloque = bloque_admin(leer_texto(repo))
    if texto_prop.count(bloque) != 1:
        raise ValueError('la propuesta no contiene el bloque admin exactamente una vez')
    if texto_prop.replace(bloque, '', 1) != texto_live:
        raise ValueError('la propuesta elimina o cambia algo de LIVE, además del bloque admin')
    return 0


def sin_comentarios(texto):
    """Quita comentarios // y /* */ respetando cadenas (las URLs llevan //). Basta para este archivo."""
    salida, i, n, estado_ = [], 0, len(texto), None
    while i < n:
        c = texto[i]
        siguiente = texto[i + 1] if i + 1 < n else ''
        if estado_ is None:
            if c == '/' and siguiente == '/':
                estado_, i = 'linea', i + 2
                continue
            if c == '/' and siguiente == '*':
                estado_, i = 'bloque', i + 2
                continue
            if c in '\'"`':
                estado_ = c
            salida.append(c)
            i += 1
        elif estado_ == 'linea':
            if c == '\n':
                estado_ = None
                salida.append(c)
            i += 1
        elif estado_ == 'bloque':
            if c == '*' and siguiente == '/':
                estado_, i = None, i + 2
            else:
                i += 1
        else:
            salida.append(c)
            if c == '\\' and i + 1 < n:
                salida.append(texto[i + 1])
                i += 2
                continue
            if c == estado_:
                estado_ = None
            i += 1
    return ''.join(salida)


def lineas_normalizadas(texto):
    return [' '.join(linea.split()) for linea in sin_comentarios(texto).splitlines() if linea.strip()]


def clasificar_diferencias(live_texto, repo_texto):
    """Diferencias semánticas entre LIVE y el repo. Cada una: categoria (funcional, drift, formato, admin)."""
    a, b = lineas_normalizadas(live_texto), lineas_normalizadas(repo_texto)
    if a == b:
        return [] if live_texto == repo_texto else [{'categoria': 'formato', 'live': [], 'repo': []}]
    hallazgos = []
    for etiqueta, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if etiqueta == 'equal':
            continue
        solo_live, solo_repo = a[i1:i2], b[j1:j2]
        if any('/admin' in linea or 'admin-web' in linea for linea in solo_live + solo_repo):
            categoria = 'admin'
        elif etiqueta == 'delete':
            categoria = 'funcional'
        else:
            categoria = 'drift'
        hallazgos.append({'categoria': categoria, 'live': solo_live, 'repo': solo_repo})
    return hallazgos


ETIQUETAS = {'funcional': 'funcional pública preexistente (solo en LIVE: se conserva)',
             'drift': 'drift desconocido (distinto en LIVE y en el repo)',
             'formato': 'formato/comentario (sin efecto)',
             'admin': 'admin nueva (lo que agrega el bloque)'}


def diff(live, repo, salida):
    texto_live, texto_repo = leer_texto(live), leer_texto(repo)
    sin = texto_repo.replace(bloque_admin(texto_repo), '', 1)
    hallazgos = clasificar_diferencias(texto_live, sin)
    cuenta = {c: sum(1 for h in hallazgos if h['categoria'] == c) for c in ETIQUETAS}
    print(f"  sha256 LIVE {hashlib.sha256(texto_live.encode('utf-8')).hexdigest()[:12]} · "
          f"repo sin bloque {hashlib.sha256(sin.encode('utf-8')).hexdigest()[:12]} · "
          f"repo {hashlib.sha256(texto_repo.encode('utf-8')).hexdigest()[:12]}")
    for categoria, etiqueta in ETIQUETAS.items():
        print(f'  {etiqueta}: {cuenta[categoria]}')
    for h in hallazgos[:12]:
        if h['categoria'] == 'admin':
            continue
        print(f"    [{h['categoria']}] LIVE: {' | '.join(h['live'])[:240] or '-'}")
        print(f"    [{h['categoria']}] repo: {' | '.join(h['repo'])[:240] or '-'}")
    textual_sin = ''.join(difflib.unified_diff(sin.splitlines(True), texto_live.splitlines(True),
                                               'repo sin bloque admin', 'LIVE', n=2))
    textual_full = ''.join(difflib.unified_diff(texto_repo.splitlines(True), texto_live.splitlines(True),
                                                'repo', 'LIVE', n=2))
    resumen = '\n'.join(f'{ETIQUETAS[c]}: {cuenta[c]}' for c in ETIQUETAS)
    escribir(salida, '== Semántico (LIVE vs repo sin bloque) ==\n' + resumen
             + '\n\n== Textual (repo sin bloque -> LIVE) ==\n' + textual_sin
             + '\n== Textual (repo completo -> LIVE) ==\n' + textual_full)
    print(f'  diff completo en {salida}')
    return 0


def historial(live, raiz, git):
    """Compara LIVE con cada versión de infra/redirect-www.js en git. Solo lee."""
    texto_live = leer_texto(live)
    try:
        shas = subprocess.run([git, 'log', '--format=%H', '--', 'infra/redirect-www.js'], cwd=raiz,
                              capture_output=True, text=True, check=True).stdout.split()
    except (subprocess.CalledProcessError, OSError):
        print('  historial de git no disponible')
        return 0
    mas_cercana = None
    for sha in shas[:40]:
        try:
            version = subprocess.run([git, 'show', f'{sha}:infra/redirect-www.js'], cwd=raiz, capture_output=True,
                                     text=True, check=True).stdout
        except (subprocess.CalledProcessError, OSError):
            continue
        if version == texto_live:
            print(f'  versión del repo igual a LIVE: {sha[:7]}')
            return 0
        distancia = sum(1 for l in difflib.unified_diff(version.splitlines(), texto_live.splitlines(), n=0)
                        if l[:1] in '+-' and l[:3] not in ('+++', '---'))
        if mas_cercana is None or distancia < mas_cercana[1]:
            mas_cercana = (sha, distancia)
    print('  ninguna versión del historial (40 más recientes) es igual a LIVE')
    if mas_cercana:
        print(f'  la más cercana: {mas_cercana[0][:7]} ({mas_cercana[1]} líneas distintas)')
    return 0


def config_funcion(meta, salida):
    with open(meta, encoding='utf-8') as f:
        resumen = json.load(f).get('FunctionSummary', {})
    config = resumen.get('FunctionConfig', {})
    runtime = config.get('Runtime')
    if runtime not in RUNTIME_CONOCIDOS:
        raise SystemExit(f'runtime de la función no reconocido: {runtime!r}')
    with open(salida, 'w', encoding='utf-8') as f:
        json.dump({'Comment': config.get('Comment', ''), 'Runtime': runtime}, f)
    os.chmod(salida, 0o600)
    return 0


def invalidacion(salida, referencia):
    lote = {'Paths': {'Quantity': len(RUTAS_INVALIDAR), 'Items': RUTAS_INVALIDAR}, 'CallerReference': referencia}
    with open(salida, 'w', encoding='utf-8') as f:
        json.dump(lote, f)
    os.chmod(salida, 0o600)
    return 0


def main(argv):
    orden, args = argv[0], argv[1:]
    if orden == 'estado':
        print(estado(*args))
        return 0
    if orden == 'inyectar':
        return inyectar(*args)
    if orden == 'verificar':
        try:
            return verificar(*args)
        except ValueError as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 1
    if orden == 'diff':
        return diff(*args)
    if orden == 'historial':
        return historial(*args)
    if orden == 'bloque-admin':
        try:
            bloque_admin(leer_texto(args[0]))
            sin_bloque(leer_texto(args[0]))
        except ValueError as exc:
            print(f'ERROR: {exc}', file=sys.stderr)
            return 1
        return 0
    if orden == 'funcion':
        print(funcion(*args))
        return 0
    if orden == 'config-funcion':
        return config_funcion(*args)
    if orden == 'invalidacion':
        return invalidacion(*args)
    raise SystemExit(f'orden desconocida: {orden}')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
