#!/usr/bin/env python3
"""Ayudas de infra/admin-deploy.sh: transforman y comparan JSON. No llaman a AWS ni a la red.

Subcomandos (los códigos de salida son los que usa el script):
  politica PLANTILLA SALIDA CUENTA REGION DIST BUCKET SITIO FROM ADMIN
  zip RAIZ SALIDA                          imprime el CodeSha256 del paquete (base64)
  env SALIDA                               variables de la Lambda desde el entorno (no imprime valores)
  bucket-privado CONFIG_LAMBDA             imprime RESENAS_BUCKET de la respuesta de get-function-configuration
  lambda-coincide CONFIG_LAMBDA SHA [ENV]  exit 0 si la función admin coincide; 1 si difiere (motivos sin valores)
  politica-coincide ACTUAL ESPERADA        exit 0 si los documentos IAM son iguales (orden de Statement ignorado)
  recurso-coincide TIPO LISTA NOMBRE       TIPO oac|orp. Imprime el ID si coincide; exit 4 si no existe; 1 si difiere
  dist-analizar CONFIG_DIST                imprime el estado de origen y behavior; exit 5 si hay conflicto de patrones
  dist-nueva CONFIG_DIST SALIDA HOST OAC ORP   agrega origen y behavior admin (sin tocar lo existente)
  dist-verificar ANTES DESPUES ORP         exit 0 si el default, el origen S3 y los behaviors previos no cambiaron
  mascarar CORREO                          imprime el correo enmascarado
  detalle ARCHIVO_STDERR                   código, operación y mensaje de un error de la CLI, sanitizados
  get ARCHIVO RUTA                         imprime un valor (RUTA con puntos)
  extraer ARCHIVO RUTA SALIDA              escribe el sub-documento de RUTA
  permisos GET_POLICY ARN_DISTRIBUCION     ninguno | ok | DIFIERE (las dos sentencias de CloudFront)
  sids POLITICA                            sids y acciones de un documento IAM
"""
import base64
import fnmatch
import glob
import hashlib
import json
import os
import re
import sys
import zipfile

ORIGEN_ID = 'resenas-admin-lambda'
PATRON = '/admin/api/*'
CACHE_DESHABILITADO = '4135ea2d-6df8-44a3-9df3-4b5a84be39ad'  # CachingDisabled (administrada por AWS)
ORP_NOMBRE = 'ork3d-resenas-admin-orp'
OAC_NOMBRE = 'ork3d-resenas-admin-oac'
VARIABLES = ('RESENAS_BUCKET', 'RESENAS_SITE_BUCKET', 'RESENAS_DISTRIBUTION_ID', 'RESENAS_ADMIN_BASE_URL',
             'ALLOWED_ORIGIN', 'RESENAS_ADMIN_EMAIL', 'RESENAS_FROM_EMAIL', 'RESENAS_SESION_HORAS',
             'RESENAS_LOGIN_MINUTOS', 'RESENAS_LOGIN_LIMITE', 'RESENAS_LOGIN_VENTANA_MIN', 'RESENAS_TOKEN_DAYS')
VALORES_FIJOS = {'RESENAS_SITE_BUCKET': 'ork3d.com', 'RESENAS_ADMIN_BASE_URL': 'https://ork3d.com',
                 'ALLOWED_ORIGIN': 'https://ork3d.com', 'RESENAS_SESION_HORAS': '8',
                 'RESENAS_LOGIN_MINUTOS': '10', 'RESENAS_LOGIN_LIMITE': '3', 'RESENAS_LOGIN_VENTANA_MIN': '15',
                 'RESENAS_TOKEN_DAYS': '15'}
ARCHIVOS_NUCLEO = 'nucleo/*.py'


def cargar(ruta):
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def guardar(ruta, datos):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(datos, f, indent=2, ensure_ascii=False, sort_keys=True)


def sustituir(valor, mapa):
    if isinstance(valor, str):
        for clave, nuevo in mapa.items():
            valor = valor.replace(clave, nuevo)
        return valor
    if isinstance(valor, list):
        return [sustituir(v, mapa) for v in valor]
    if isinstance(valor, dict):
        return {k: sustituir(v, mapa) for k, v in valor.items()}
    return valor


def politica(plantilla, salida, cuenta, region, dist, bucket, sitio, origen, admin):
    mapa = {'__ACCOUNT_ID__': cuenta, '__AWS_REGION__': region, '__DISTRIBUTION_ID__': dist,
            '__RESENAS_BUCKET__': bucket, '__SITE_BUCKET__': sitio,
            '__RESENAS_FROM_EMAIL__': origen, '__RESENAS_ADMIN_EMAIL__': admin}
    doc = sustituir(cargar(plantilla), mapa)
    texto = json.dumps(doc)
    if any(clave in texto for clave in mapa):
        print('ERROR: quedan marcadores sin sustituir en la política', file=sys.stderr)
        return 1
    guardar(salida, doc)
    return 0


def zip_admin(raiz, salida):
    archivos = [(os.path.join(raiz, 'api', 'resenas', 'admin_app.py'), 'admin_app.py')]
    for ruta in sorted(glob.glob(os.path.join(raiz, 'api', 'resenas', ARCHIVOS_NUCLEO))):
        archivos.append((ruta, 'nucleo/' + os.path.basename(ruta)))
    with zipfile.ZipFile(salida, 'w', zipfile.ZIP_DEFLATED) as z:
        for ruta, nombre in archivos:
            info = zipfile.ZipInfo(nombre, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            with open(ruta, 'rb') as f:
                z.writestr(info, f.read(), compress_type=zipfile.ZIP_DEFLATED)
    with open(salida, 'rb') as f:
        digest = hashlib.sha256(f.read()).digest()
    print(base64.b64encode(digest).decode('ascii'))
    return 0


def env(salida):
    variables = {}
    for nombre in VARIABLES:
        valor = VALORES_FIJOS.get(nombre) or os.environ.get(nombre, '')
        if not valor:
            print(f'ERROR: falta la variable {nombre}', file=sys.stderr)
            return 2
        variables[nombre] = valor
    guardar(salida, {'Variables': variables})
    os.chmod(salida, 0o600)
    print('variables de entorno: ' + ', '.join(sorted(variables)))
    return 0


def bucket_privado(config):
    datos = cargar(config)
    return (datos.get('Environment') or {}).get('Variables', {}).get('RESENAS_BUCKET', '')


def lambda_coincide(config, sha, env_esperado=None):
    cfg = cargar(config)
    motivos = []
    if cfg.get('Runtime') != 'python3.12':
        motivos.append(f"runtime {cfg.get('Runtime')}")
    if cfg.get('Handler') != 'admin_app.handler':
        motivos.append(f"handler {cfg.get('Handler')}")
    if cfg.get('MemorySize') != 256:
        motivos.append(f"memoria {cfg.get('MemorySize')}")
    if cfg.get('Timeout') != 10:
        motivos.append(f"timeout {cfg.get('Timeout')}")
    if not str(cfg.get('Role', '')).endswith('/ork3d-resenas-admin-lambda'):
        motivos.append('rol distinto del esperado')
    if cfg.get('State') != 'Active':
        motivos.append(f"estado {cfg.get('State')}")
    if cfg.get('LastUpdateStatus') != 'Successful':
        motivos.append(f"última actualización {cfg.get('LastUpdateStatus')}")
    actuales = ((cfg.get('Environment') or {}).get('Variables') or {})
    if set(actuales) != set(VARIABLES):
        faltan = sorted(set(VARIABLES) - set(actuales))
        sobran = sorted(set(actuales) - set(VARIABLES))
        motivos.append(f'variables: faltan {faltan}, sobran {sobran}')
    if env_esperado is not None:
        esperadas = cargar(env_esperado).get('Variables', {})
        distintas = sorted(k for k in esperadas if k in actuales and actuales[k] != esperadas[k])
        if distintas:
            motivos.append(f'valores distintos en {distintas} (no se muestran)')
    if cfg.get('CodeSha256') != sha:
        motivos.append('código distinto del paquete del repo')
    for motivo in motivos:
        print('DIFIERE: ' + motivo)
    return 0 if not motivos else 1


def normalizar_politica(doc):
    doc = dict(doc)
    st = doc.get('Statement')
    if isinstance(st, dict):
        st = [st]
    doc['Statement'] = sorted(st or [], key=lambda s: json.dumps(s, sort_keys=True))
    return doc


def politica_coincide(actual, esperada):
    a = normalizar_politica(cargar(actual))
    e = normalizar_politica(cargar(esperada))
    if a == e:
        print('IGUAL')
        return 0
    print('DIFIERE: el documento de la política no coincide')
    return 1


def recurso_coincide(tipo, lista, nombre):
    datos = cargar(lista)
    if tipo == 'oac':
        items = (datos.get('OriginAccessControlList') or {}).get('Items') or []
        candidatos = [i for i in items if i.get('Name') == nombre]
        if not candidatos:
            return 4
        o = candidatos[0]
        ok = (o.get('SigningProtocol') == 'sigv4' and o.get('SigningBehavior') == 'always'
              and o.get('OriginAccessControlOriginType') == 'lambda')
        if not ok:
            print('DIFIERE: OAC con otra configuración')
            return 1
        print(o.get('Id'))
        return 0
    items = (datos.get('OriginRequestPolicyList') or {}).get('Items') or []
    candidatos = [i for i in items if i.get('OriginRequestPolicy', {}).get('OriginRequestPolicyConfig', {})
                  .get('Name') == nombre]
    if not candidatos:
        return 4
    p = candidatos[0]['OriginRequestPolicy']
    cfg = {k: v for k, v in p['OriginRequestPolicyConfig'].items() if k not in ('Name', 'Comment')}
    esperada = {k: v for k, v in cargar(ORP_PLANTILLA).items() if k not in ('Name', 'Comment')}
    if cfg != esperada:
        print('DIFIERE: política de origen con otra configuración')
        return 1
    print(p.get('Id'))
    return 0


ORP_PLANTILLA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resenas', 'admin-origin-request-policy.json')


def origen_admin(host, oac):
    return {'Id': ORIGEN_ID, 'DomainName': host, 'OriginPath': '', 'OriginAccessControlId': oac,
            'CustomHeaders': {'Quantity': 0},
            'CustomOriginConfig': {'HTTPPort': 80, 'HTTPSPort': 443, 'OriginProtocolPolicy': 'https-only',
                                   'OriginSslProtocols': {'Quantity': 1, 'Items': ['TLSv1.2']},
                                   'OriginReadTimeout': 30, 'OriginKeepaliveTimeout': 5},
            'ConnectionAttempts': 3, 'ConnectionTimeout': 10}


def behavior_admin(orp):
    return {'PathPattern': PATRON, 'TargetOriginId': ORIGEN_ID, 'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {'Quantity': 7, 'Items': ['GET', 'HEAD', 'OPTIONS', 'PUT', 'POST', 'PATCH', 'DELETE'],
                               'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            'CachePolicyId': CACHE_DESHABILITADO, 'OriginRequestPolicyId': orp, 'Compress': False,
            'FunctionAssociations': {'Quantity': 0}, 'LambdaFunctionAssociations': {'Quantity': 0}}


def _items(bloque):
    return list((bloque or {}).get('Items') or [])


def analizar(cfg):
    origenes = _items(cfg.get('Origins'))
    behaviors = _items(cfg.get('CacheBehaviors'))
    origen = any(o.get('Id') == ORIGEN_ID for o in origenes)
    beh = [b for b in behaviors if b.get('PathPattern') == PATRON]
    conflicto = [b.get('PathPattern') for b in behaviors
                 if b.get('PathPattern') != PATRON and fnmatch.fnmatchcase('/admin/api/sesion', b.get('PathPattern', ''))]
    return origen, bool(beh), conflicto


def dist_analizar(config):
    cfg = cargar(config).get('DistributionConfig') or {}
    origen, beh, conflicto = analizar(cfg)
    print(f"ORIGEN_ADMIN={'si' if origen else 'no'}")
    print(f"BEHAVIOR_ADMIN={'si' if beh else 'no'}")
    print(f"CONFLICTO={'si' if conflicto else 'no'}")
    if conflicto:
        print('CONFLICTO_PATRONES=' + ','.join(conflicto))
        return 5
    return 0


def dist_nueva(config, salida, host, oac, orp):
    completa = cargar(config)
    cfg = completa.get('DistributionConfig') or {}
    origenes = _items(cfg.get('Origins'))
    behaviors = _items(cfg.get('CacheBehaviors'))
    origen_ok, beh_ok, conflicto = analizar(cfg)
    if conflicto:
        print('ERROR: otro behavior ya cubre /admin/api/*: ' + ','.join(conflicto), file=sys.stderr)
        return 5
    if origen_ok:
        existente = [o for o in origenes if o.get('Id') == ORIGEN_ID][0]
        if existente.get('DomainName') != host or existente.get('OriginAccessControlId') != oac:
            print('ERROR: el origen ' + ORIGEN_ID + ' ya existe con otro dominio u OAC', file=sys.stderr)
            return 4
    if beh_ok:
        existente = [b for b in behaviors if b.get('PathPattern') == PATRON][0]
        if existente != behavior_admin(orp):
            print('ERROR: el behavior ' + PATRON + ' ya existe con otra configuración', file=sys.stderr)
            return 4
    if not origen_ok:
        origenes.append(origen_admin(host, oac))
        cfg['Origins'] = {'Quantity': len(origenes), 'Items': origenes}
        print('AGREGADO origen admin')
    if not beh_ok:
        behaviors.append(behavior_admin(orp))
        cfg['CacheBehaviors'] = {'Quantity': len(behaviors), 'Items': behaviors}
        print('AGREGADO behavior /admin/api/*')
    guardar(salida, cfg)
    return 0


def dist_verificar(antes, despues, orp):
    a = (cargar(antes).get('DistributionConfig') or {})
    d = (cargar(despues).get('DistributionConfig') or {})
    errores = []
    if a.get('DefaultCacheBehavior') != d.get('DefaultCacheBehavior'):
        errores.append('el default behavior cambió')
    s3_a = [o for o in _items(a.get('Origins')) if o.get('S3OriginConfig') is not None]
    s3_d = [o for o in _items(d.get('Origins')) if o.get('S3OriginConfig') is not None]
    if s3_a != s3_d:
        errores.append('el origen S3 cambió')
    previos_a = _items(a.get('CacheBehaviors'))
    previos_d = [b for b in _items(d.get('CacheBehaviors')) if b.get('PathPattern') != PATRON]
    if previos_a != previos_d:
        errores.append('los behaviors existentes cambiaron')
    nuevos = [b for b in _items(d.get('CacheBehaviors')) if b.get('PathPattern') == PATRON]
    if len(nuevos) != 1:
        errores.append(f'se esperaba un behavior {PATRON}, hay {len(nuevos)}')
    else:
        b = nuevos[0]
        if b.get('TargetOriginId') != ORIGEN_ID or b.get('OriginRequestPolicyId') != orp:
            errores.append('behavior admin con origen o política incorrectos')
        if b.get('CachePolicyId') != CACHE_DESHABILITADO:
            errores.append('behavior admin sin CachingDisabled')
        if b.get('ViewerProtocolPolicy') != 'redirect-to-https':
            errores.append('behavior admin sin redirect-to-https')
        if 'POST' not in (b.get('AllowedMethods') or {}).get('Items', []):
            errores.append('behavior admin sin POST')
    for e in errores:
        print('ERROR: ' + e)
    return 0 if not errores else 1


def mascarar(correo):
    local, _, dominio = correo.partition('@')
    if not dominio:
        return '(sin formato)'
    return local[:1] + '***@' + dominio[:1] + '***.' + dominio.rsplit('.', 1)[-1]


def _enmascarar_correos(texto):
    return re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}', lambda m: mascarar(m.group(0)), texto)


def _enmascarar_cuentas(texto):
    return re.sub(r'\b\d{12}\b', lambda m: '****' + m.group(0)[-4:], texto)


SANITIZADORES = (
    _enmascarar_correos,
    _enmascarar_cuentas,
    lambda t: re.sub(r'\b(AKIA|ASIA)[A-Z0-9]{16}\b', '[oculto]', t),
    lambda t: re.sub(r'(?i)authorization:\s*\S+', '[oculto]', t),
    lambda t: re.sub(r'(?i)(secret|token|session|password|signature|x-amz-security-token)[^\s,;]{0,40}?[:=]\s*\S+',
                     '[oculto]', t),
    lambda t: re.sub(r'https?://\S+\?\S+', '[url con parámetros]', t),
)


def sanitizar(texto):
    """Quita lo que no debe llegar a un registro: correos y cuentas enmascarados; claves, tokens y URLs firmadas."""
    for sanitizador in SANITIZADORES:
        texto = sanitizador(texto)
    return texto


def detalle(archivo):
    """Código, operación y mensaje de un error de la CLI, sanitizados. Para diagnóstico en el registro."""
    texto = open(archivo, encoding='utf-8', errors='replace').read()
    m = re.search(r'An error occurred \((\w+)\) when calling the ([\w-]+) operation: (.*)', texto, re.S)
    if m:
        codigo, operacion, mensaje = m.group(1), m.group(2), m.group(3).strip()
    else:
        codigo, operacion, mensaje = 'desconocido', '', texto.strip()
    mensaje = sanitizar(mensaje)[:300]
    cabeza = codigo + (f' en {operacion}' if operacion else '')
    print(f'{cabeza}: {mensaje}' if mensaje else cabeza)
    return 0


def obtener(archivo, ruta):
    """Valor por ruta con puntos (p. ej. Distribution.Status). Listas: separadas por coma. Booleanos: true/false."""
    valor = cargar(archivo)
    for clave in ruta.split('.'):
        valor = valor.get(clave) if isinstance(valor, dict) else None
        if valor is None:
            return ''
    if isinstance(valor, bool):
        return 'true' if valor else 'false'
    if isinstance(valor, list):
        return ','.join(str(v) for v in valor)
    return str(valor)


def extraer(archivo, ruta, salida):
    valor = cargar(archivo)
    for clave in ruta.split('.'):
        valor = valor[clave]
    guardar(salida, valor)
    return 0


def permisos(politica_json, arn_distribucion):
    """Revisa las dos sentencias de CloudFront en la política de la Lambda admin.

    Imprime 'ninguno' si no hay ninguna; 'ok' si las dos están exactas; exit 1 si hay una distinta o solo una.
    """
    texto = cargar(politica_json).get('Policy')
    doc = json.loads(texto) if texto else {'Statement': []}
    st = doc.get('Statement') or []
    esperadas = {
        'ork3d-admin-cf-url': ('lambda:InvokeFunctionUrl', {
            'StringEquals': {'lambda:FunctionUrlAuthType': 'AWS_IAM'}}),
        'ork3d-admin-cf-invoke': ('lambda:InvokeFunction', {
            'Bool': {'lambda:InvokedViaFunctionUrl': 'true'}}),
    }
    presentes = {s.get('Sid'): s for s in st if s.get('Sid') in esperadas}
    if not presentes:
        print('ninguno')
        return 0
    for sid, (accion, condicion) in esperadas.items():
        s = presentes.get(sid)
        if s is None or s.get('Action') != accion:
            print(f'DIFIERE: falta o no coincide la sentencia {sid}')
            return 1
        if (s.get('Principal') or {}).get('Service') != 'cloudfront.amazonaws.com':
            print(f'DIFIERE: {sid} no es para cloudfront.amazonaws.com')
            return 1
        cond = s.get('Condition') or {}
        for operador, pares in condicion.items():
            for clave, valor in pares.items():
                if str((cond.get(operador) or {}).get(clave)).lower() != valor.lower():
                    print(f'DIFIERE: {sid} con condición distinta a {clave}')
                    return 1
        origen = str((cond.get('ArnLike') or {}).get('AWS:SourceArn', ''))
        if origen != arn_distribucion:
            print(f'DIFIERE: {sid} sin SourceArn de la distribución esperada')
            return 1
    print('ok')
    return 0


def sids(politica_json):
    """Sids y acciones de un documento IAM (sin recursos ni condiciones: no tienen secretos)."""
    doc = cargar(politica_json)
    st = doc.get('Statement') or []
    if isinstance(st, dict):
        st = [st]
    for s in st:
        acciones = s.get('Action')
        acciones = acciones if isinstance(acciones, list) else [acciones]
        print(f"{s.get('Sid', '(sin Sid)')}: {', '.join(acciones)}")
    return 0


def main(argv):
    orden, args = argv[1], argv[2:]
    if orden == 'politica':
        return politica(*args)
    if orden == 'zip':
        return zip_admin(*args)
    if orden == 'env':
        return env(*args)
    if orden == 'bucket-privado':
        print(bucket_privado(*args))
        return 0
    if orden == 'lambda-coincide':
        return lambda_coincide(*args)
    if orden == 'politica-coincide':
        return politica_coincide(*args)
    if orden == 'recurso-coincide':
        return recurso_coincide(*args)
    if orden == 'dist-analizar':
        return dist_analizar(*args)
    if orden == 'dist-nueva':
        return dist_nueva(args[0], args[1], args[2], args[3], args[4])
    if orden == 'dist-verificar':
        return dist_verificar(*args)
    if orden == 'mascarar':
        print(mascarar(args[0]))
        return 0
    if orden == 'detalle':
        return detalle(*args)
    if orden == 'get':
        print(obtener(*args))
        return 0
    if orden == 'extraer':
        return extraer(*args)
    if orden == 'permisos':
        return permisos(*args)
    if orden == 'sids':
        return sids(*args)
    print(f'orden desconocida: {orden}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
