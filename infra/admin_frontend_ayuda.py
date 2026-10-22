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
import json
import os
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
