#!/usr/bin/env python3
"""Ayuda del diagnóstico del 404 de la API admin (infra/admin-diagnostico.sh). Sin AWS ni red.

Transforma lo que recoge el script: estado y cabeceras visibles de cada petición, la configuración de CloudFront,
el conteo de invocaciones de la Lambda admin y la conclusión. No guarda cuerpos, cookies ni correos.

  peticion SALIDA NOMBRE CODIGO CABECERAS CUERPO T0 T1  agrega una petición a SALIDA
  ventana PETICIONES                                    imprime "INICIO FIN" (ms) para filter-log-events
  invocaciones PETICIONES LOGS                          cuenta invocaciones (REPORT) de cada petición
  config SALIDA DISTRIBUCION [URL] [POLITICA_BUCKET]    resume errores personalizados, behaviors y origen
  clasificar PETICIONES CONFIG CONCLUSION               decide y escribe la conclusión
  limpiar ARCHIVO                                       imprime un archivo sin correos, cuentas ni firmas
"""
import json
import os
import re
import sys

CABECERAS_VISIBLES = ('content-type', 'server', 'x-cache', 'via', 'cache-control')
MARCA_APP = '"ok": false'
MARGEN_ANTES_MS = 1000
MARGEN_DESPUES_MS = 1000
PATRON_ADMIN = '/admin/api/*'


def leer(ruta, por_defecto):
    if not ruta or not os.path.exists(ruta) or os.path.getsize(ruta) == 0:
        return por_defecto
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def guardar(ruta, datos):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)
    os.chmod(ruta, 0o600)


def limpiar(texto):
    """Quita de un texto lo que no debe quedar en un registro: correos, cuentas de 12 dígitos y firmas."""
    texto = re.sub(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', '[correo]', texto)
    texto = re.sub(r'\b\d{12}\b', '[cuenta]', texto)
    return re.sub(r'(X-Amz-(?:Signature|Credential|Security-Token)|token|cookie)=[^&\s"\']+', r'\1=[redactado]',
                  texto, flags=re.IGNORECASE)


def tipo_cuerpo(cuerpo):
    if cuerpo.lstrip().startswith('{') and MARCA_APP in cuerpo:
        return 'app'
    if '<html' in cuerpo.lower():
        return 'html'
    return 'otro'


def peticion(salida, nombre, codigo, cabeceras_ruta, cuerpo_ruta, t0, t1):
    visibles = {}
    with open(cabeceras_ruta, encoding='utf-8', errors='replace') as f:
        for linea in f:
            if ':' not in linea or linea.startswith('HTTP/'):
                continue
            clave, _, valor = linea.partition(':')
            clave = clave.strip().lower()
            if clave in CABECERAS_VISIBLES:
                visibles[clave] = valor.strip()
    with open(cuerpo_ruta, encoding='utf-8', errors='replace') as f:
        cuerpo = f.read(4096)
    registros = leer(salida, [])
    registros.append({'nombre': nombre, 'codigo': int(codigo) if str(codigo).isdigit() else 0,
                      'tipo': tipo_cuerpo(cuerpo), 'cabeceras': visibles, 'invocaciones': None,
                      't0': int(t0), 't1': int(t1)})
    guardar(salida, registros)
    return 0


def ventana(peticiones):
    registros = leer(peticiones, [])
    if not registros:
        raise SystemExit('ventana: no hay peticiones registradas')
    inicio = min(r['t0'] for r in registros) - 60000
    fin = max(r['t1'] for r in registros) + 120000
    print(f'{inicio} {fin}')
    return 0


def invocaciones(peticiones, logs):
    registros = leer(peticiones, [])
    marcas = [e.get('timestamp', 0) for e in leer(logs, {}).get('events', []) if 'REPORT' in e.get('message', '')]
    for r in registros:
        r['invocaciones'] = sum(1 for m in marcas
                                if r['t0'] - MARGEN_ANTES_MS <= m <= r['t1'] + MARGEN_DESPUES_MS)
    guardar(peticiones, registros)
    print('  invocaciones de la Lambda por petición: '
          + ', '.join(f"{r['nombre']}={r['invocaciones']}" for r in registros))
    return 0


def _comportamiento(b, patron, origenes):
    origen = origenes.get(b.get('TargetOriginId'), {})
    dominio = origen.get('DomainName', '')
    return {'patron': patron,
            'origen': 'lambda-url' if 'lambda-url' in dominio else ('s3' if '.s3' in dominio else 'otro'),
            'oac': bool(origen.get('OriginAccessControlId')),
            'metodos': sorted((b.get('AllowedMethods') or {}).get('Items', [])),
            'cache': b.get('CachePolicyId', ''),
            'orp': bool(b.get('OriginRequestPolicyId')),
            'funciones': len((b.get('FunctionAssociations') or {}).get('Items', [])),
            'funciones_lambda': len((b.get('LambdaFunctionAssociations') or {}).get('Items', []))}


def _listbucket(politica):
    """True si alguna sentencia Allow de la política del bucket concede s3:ListBucket; None si no hay política."""
    datos = leer(politica, None)
    if not datos or not datos.get('Policy'):
        return None
    sentencias = json.loads(datos['Policy']).get('Statement', [])
    if isinstance(sentencias, dict):
        sentencias = [sentencias]
    for s in sentencias:
        acciones = s.get('Action', [])
        acciones = [acciones] if isinstance(acciones, str) else acciones
        if s.get('Effect') == 'Allow' and 's3:ListBucket' in acciones:
            return True
    return False


def config(salida, distribucion, url=None, politica=None):
    cfg = leer(distribucion, {}).get('DistributionConfig', {})
    origenes = {o.get('Id'): o for o in (cfg.get('Origins') or {}).get('Items', [])}
    errores = [{'codigo': int(e.get('ErrorCode', 0)), 'respuesta': int(e.get('ResponseCode') or 0),
                'pagina': e.get('ResponsePagePath')}
               for e in (cfg.get('CustomErrorResponses') or {}).get('Items', [])]
    behaviors = [_comportamiento(b, b.get('PathPattern'), origenes)
                 for b in (cfg.get('CacheBehaviors') or {}).get('Items', [])]
    resumen = {'errores': errores,
               'default': _comportamiento(cfg.get('DefaultCacheBehavior', {}), '(default)', origenes),
               'comportamientos': behaviors,
               'auth_url': leer(url, {}).get('AuthType'),
               'listbucket_oac': _listbucket(politica)}
    guardar(salida, resumen)
    print('  CustomErrorResponses (distribución completa, afecta a todos los behaviors):')
    for e in errores:
        print(f"    {e['codigo']} → {e['respuesta']} {e['pagina']}")
    if not errores:
        print('    ninguna')
    admin = [(i, b) for i, b in enumerate(behaviors) if b['patron'] == PATRON_ADMIN]
    if admin:
        i, b = admin[0]
        print(f"  behavior {PATRON_ADMIN}: posición {i} de {len(behaviors)} · métodos {', '.join(b['metodos'])} "
              f"· origen {b['origen']} · OAC {'sí' if b['oac'] else 'no'} · ORP {'sí' if b['orp'] else 'no'}")
    else:
        print(f'  behavior {PATRON_ADMIN}: AUSENTE')
    print(f"  Function URL AuthType: {resumen['auth_url'] or 'sin datos'}")
    lb = resumen['listbucket_oac']
    print('  política del bucket del sitio: ' + ('sin datos' if lb is None else
                                                  ('concede s3:ListBucket' if lb else 'no concede s3:ListBucket')))
    return 0


def decidir(registros, cfg):
    """Devuelve (código, motivo) a partir de las peticiones registradas y el resumen de configuración."""
    por_nombre = {r['nombre']: r for r in registros}
    post, put = por_nombre.get('post-sin-origin'), por_nombre.get('put-control')
    if post is None or put is None:
        return 'INCOMPLETO', 'faltan peticiones: post-sin-origin y put-control son obligatorias'
    if post['codigo'] == 403 and post['tipo'] == 'app':
        return 'NO_REPRODUCIDO', 'el 403 de la app llegó íntegro: el 404 no se reprodujo en esta ejecución'
    if put['tipo'] != 'app':
        return ('BORDE_COMPORTAMIENTO',
                'el control PUT /admin/api/sesion no llega a la API: el behavior /admin/api/* o sus métodos no '
                'se aplican en el borde')
    if put['invocaciones'] is None or post['invocaciones'] is None:
        return 'SIN_LOGS', 'no se leyeron las invocaciones de la Lambda: no se puede concluir sobre el origen'
    if put['invocaciones'] == 0:
        return ('LOGS_NO_CORRELACIONADOS',
                'la API respondió al control PUT pero no hay REPORT en la ventana: los logs no son visibles '
                '(retraso, grupo o región). Repetir con DIAG_ESPERA mayor antes de concluir')
    if post['invocaciones'] == 0:
        return ('BORDE_SIN_INVOCACION',
                'el control PUT invocó la Lambda y el POST no: CloudFront responde antes del origen para el POST. '
                'Revisar AllowedMethods, OAC/ORP y funciones del behavior')
    mapeo = {e['codigo'] for e in cfg.get('errores', [])}
    if post['codigo'] == 404 and post['tipo'] != 'app' and 403 in mapeo:
        return ('MAPEO_ERRORES_PERSONALIZADOS',
                'la Lambda respondió (invocación registrada) con 403 de la app y CloudFront lo reemplazó por la '
                'página de error del sitio con estado 404. El router no es la causa: el mapeo es de la distribución '
                'completa (CustomErrorResponses), no de /admin/api/*')
    return 'DESCONOCIDO', 'la combinación no coincide con ningún patrón conocido: adjuntar peticiones y configuración'


OPCIONES_MAPEO = [
    'Decisión requerida (docs/resenas/V2-ADMIN.md §16.7). El router no cambia.',
    '  1. Dejar de mapear 403 en la distribución: el sitio debe servir 404 real para los faltantes (s3:ListBucket',
    '     en el OAC del bucket). Toca la web pública: verificar antes y después /opinar, /opiniones y error.html.',
    '  2. Cambiar el status de los rechazos de la API que hoy son 403 (CSRF) a un código que no se reescribe.',
    '     Cambia el contrato de la API y el criterio del CHECKPOINT 6B del smoke: requiere aprobación explícita.',
    '  3. Servir la API desde otro host sin errores personalizados. Requiere DNS: fuera de alcance sin confirmación.',
]


def clasificar(peticiones, config_resumen, conclusion):
    registros = leer(peticiones, [])
    cfg = leer(config_resumen, {})
    codigo, motivo = decidir(registros, cfg)
    lineas = [f"  {r['nombre']}: HTTP {r['codigo']} · cuerpo {r['tipo']} · server "
              f"{r['cabeceras'].get('server', '-')} · x-cache {r['cabeceras'].get('x-cache', '-')} · "
              f"invocaciones {r['invocaciones']}" for r in registros]
    lineas += ['', f'CONCLUSIÓN: {codigo}', motivo]
    if codigo == 'MAPEO_ERRORES_PERSONALIZADOS':
        recomendacion = ('Recomendación: opción 1 si el bucket ya concede s3:ListBucket; si no, opción 2 tras '
                         'aprobación.' if cfg.get('listbucket_oac') else
                         'Recomendación: opción 2 tras aprobación (la opción 1 exige conceder s3:ListBucket antes).')
        lineas += [''] + OPCIONES_MAPEO + ['', recomendacion]
    texto = '\n'.join(lineas) + '\n'
    with open(conclusion, 'w', encoding='utf-8') as f:
        f.write(texto)
    os.chmod(conclusion, 0o600)
    sys.stdout.write(texto)
    return 0


def main(argv):
    orden, args = argv[0], argv[1:]
    if orden == 'peticion':
        return peticion(*args)
    if orden == 'ventana':
        return ventana(*args)
    if orden == 'invocaciones':
        return invocaciones(*args)
    if orden == 'config':
        return config(*args)
    if orden == 'clasificar':
        return clasificar(*args)
    if orden == 'limpiar':
        with open(args[0], encoding='utf-8', errors='replace') as f:
            sys.stdout.write(limpiar(f.read()))
        return 0
    raise SystemExit(f'orden desconocida: {orden}')


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
