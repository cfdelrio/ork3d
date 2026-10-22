#!/usr/bin/env python3
"""Preflight AWS de V2: lee la configuración actual de la cuenta. SOLO LECTURA.

Lee: Lambda pública (configuración, Function URL, resource policy, concurrencia), Lambda admin (si existe),
rol IAM de la Lambda pública (políticas administradas e inline), distribución de CloudFront del sitio (con su
copia de rollback), función de redirección, OAC y políticas de origen, bucket privado y bucket del sitio, log
groups de CloudWatch y SES (estado de la cuenta e identidades, sin crear ni verificar nada).

Garantías:
- Cada llamada a AWS pasa por ro(). Solo ejecuta operaciones de PERMITIDAS, nunca las que tienen un verbo
  mutante (VERBOS_MUTANTES) y nunca las de EXCLUIDAS (contenido de objetos, URLs firmadas, contactos).
  Cualquier otra operación aborta el script antes de llamar a AWS.
- Cada lectura tiene un contrato (leer()): un rc distinto de cero, un stdout vacío donde no se espera, o una
  salida que no es JSON se reportan como ERROR con servicio, operación, rc y tipo. Un stdout vacío se trata como
  "sin datos" solo cuando la operación lo admite (vacio_ok). Nunca se parsea un stdout vacío.
- La configuración de la Lambda se conserva en memoria y la usan los pasos siguientes (bucket privado, rol de
  IAM). La copia de rollback no guarda los valores de las variables.
- No imprime AccessKey, SecretKey ni SessionToken. Los errores de la CLI se redactan antes de mostrarse.
- Enmascara el account ID (últimos 4 dígitos), las direcciones de correo y el nombre de los buckets en los ARN.
- De las variables de entorno imprime solo los nombres.
- No lee contenido de objetos: ni reseñas, ni tokens, ni contactos, ni comentarios. De los prefijos del bucket
  privado solo informa sí/no, con max-keys 1.
- Las copias de rollback se guardan con permisos 600 en OUT/rollback.

Uso (desde EC2, con el perfil configurado):
    AWS_PROFILE=default AWS_REGION=us-east-1 python3 infra/preflight_v2.py [/tmp/ork3d-v2-preflight]

Códigos de salida: 0 todo leído; 1 no se pudo autenticar; 2 algún paso no se pudo leer; 3 un intento de
operación no permitida (el guard detuvo el script).
"""
import collections
import json
import os
import re
import subprocess
import sys

Resultado = collections.namedtuple('Resultado', 'rc out err')

FN_PUBLICA = 'ork3d-resenas'
FN_ADMIN = 'ork3d-resenas-admin'
REDIRECT = 'ork3d-redirect-www'
DOMINIO = 'ork3d.com'
PREFIJOS = ('tokens/', 'claims/', 'resenas/', 'pendientes/', 'admin/', 'notificaciones/')
OUT_POR_DEFECTO = '/tmp/ork3d-v2-preflight'
CODIGO_ERROR = re.compile(r'An error occurred \((\w+)\)')

# Lista explícita de lectura: (servicio, expresión sobre el nombre de la operación).
PERMITIDAS = (
    ('sts', r'get-caller-identity'),
    ('lambda', r'get-.+'),
    ('lambda', r'list-.+'),
    ('cloudfront', r'get-.+'),
    ('cloudfront', r'list-.+'),
    ('s3api', r'get-.+'),
    ('s3api', r'head-.+'),
    ('s3api', r'list-objects-v2'),  # solo para comprobar que un prefijo tiene objetos, con --max-keys 1
    ('iam', r'get-.+'),
    ('iam', r'list-.+'),
    ('logs', r'describe-.+'),
    ('sesv2', r'get-.+'),
    ('sesv2', r'list-.+'),
)

# Lecturas que aunque entren en PERMITIDAS no se llaman: devuelven contenido sensible o URLs firmadas.
EXCLUIDAS = (
    ('s3api', r'get-object.*'),            # contenido de objetos: reseñas, tokens, contactos
    ('lambda', r'get-function'),           # devuelve una URL firmada con el código
    ('lambda', r'get-layer-version.*'),    # idem, para capas
    ('iam', r'get-credential-report'),     # datos de credenciales
    ('sesv2', r'get-contact.*'),           # contactos
    ('sesv2', r'list-contacts'),
    ('sesv2', r'list-suppressed-destinations'),  # direcciones suprimidas
    ('sesv2', r'get-suppressed-destination'),
)

# Segunda barrera: aunque una operación coincida con PERMITIDAS, un verbo mutante la bloquea.
VERBOS_MUTANTES = re.compile(
    r'(^|-)(create|update|delete|put|add|remove|attach|detach|publish|invoke|tag|untag|set|enable|'
    r'disable|start|stop|send|verify|copy|sync|restore|modify|test|simulate|reset|revoke|authorize|'
    r'associate|import|reboot|run|cancel|upload|batch|generate)(-|$)')

PATRONES_SECRETOS = (
    re.compile(r'\b(AKIA|ASIA|AROA|AIDA)[A-Z0-9]{16}\b'),
    re.compile(r'(?i)(secret|token|session|password)[^\n]{0,20}?[:=]\s*\S{12,}'),
    re.compile(r'\b[A-Za-z0-9/+]{40}\b'),
)


class OperacionNoPermitida(Exception):
    """El guard bloqueó una operación fuera de la lista de lectura. El script se detiene."""


class AutenticacionFallida(Exception):
    pass


class ErrorAws(Exception):
    """Una lectura de AWS falló o devolvió algo que su contrato no admite. Solo lleva datos sanitizados."""

    def __init__(self, servicio, operacion, rc, tipo, detalle=''):
        self.servicio, self.operacion, self.rc, self.tipo = servicio, operacion, rc, tipo
        super().__init__(f'{servicio} {operacion} rc={rc} tipo={tipo}' + (f' · {detalle}' if detalle else ''))


def perfil():
    return os.environ.get('AWS_PROFILE') or 'default'


def region():
    return os.environ.get('AWS_REGION') or 'us-east-1'


def permitida(servicio, operacion):
    """True solo si la pareja es de lectura según la lista explícita, sin verbo mutante ni exclusión."""
    if VERBOS_MUTANTES.search(operacion):
        return False
    if any(servicio == s and re.fullmatch(p, operacion) for s, p in EXCLUIDAS):
        return False
    return any(servicio == s and re.fullmatch(p, operacion) for s, p in PERMITIDAS)


def enmascarar(texto):
    """Oculta los account ID de 12 dígitos dejando los últimos cuatro."""
    return re.sub(r'\b\d{12}\b', lambda m: '****' + m.group(0)[-4:], texto or '')


def enmascarar_recursos(texto):
    """Como enmascarar, y además oculta el nombre de los buckets en los ARN de S3 (deja cuatro letras)."""
    return enmascarar(re.sub(r'arn:aws:s3:::([A-Za-z0-9.\-]+)',
                             lambda m: 'arn:aws:s3:::' + m.group(1)[:4] + '…', texto or ''))


def redactar(texto):
    """Quita lo que parece clave, token o secreto, y enmascara cuentas, antes de mostrar un error."""
    t = texto or ''
    for patron in PATRONES_SECRETOS:
        t = patron.sub('[oculto]', t)
    return enmascarar(t)


def correo_enmascarado(correo):
    local, _, dominio = (correo or '').partition('@')
    if not dominio:
        return '(sin formato)'
    return local[:1] + '***@' + dominio[:1] + '***.' + dominio.rsplit('.', 1)[-1]


def sin_valores(cfg):
    """Copia de la configuración de la Lambda con los valores de las variables reemplazados."""
    copia = json.loads(json.dumps(cfg))
    env = copia.get('Environment')
    if env and 'Variables' in env:
        env['Variables'] = {k: '<valor omitido>' for k in env['Variables']}
    return copia


def como_lista(valor):
    """Un campo de la respuesta que puede venir como lista, como valor suelto o ausente."""
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def tipo_de(err):
    m = CODIGO_ERROR.search(err or '')
    return m.group(1) if m else 'desconocido'


def ro(servicio, operacion, *args, pos=None, check=True):
    """Única puerta a AWS del preflight. Verifica la lista explícita antes de llamar a la CLI.
    Con check=True, un rc distinto de cero lanza ErrorAws. Con check=False devuelve el resultado tal cual."""
    if not permitida(servicio, operacion):
        raise OperacionNoPermitida(f'{servicio} {operacion}')
    cmd = [os.environ.get('AWS_CLI', 'aws'), servicio, operacion, *args,
           '--region', region(), '--output', 'json']
    if pos:
        cmd.append(pos)
    env = dict(os.environ, AWS_PAGER='', AWS_PROFILE=perfil(), AWS_REGION=region())
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    res = Resultado(r.returncode, r.stdout, redactar(r.stderr))
    if check and r.returncode != 0:
        raise ErrorAws(servicio, operacion, r.returncode, tipo_de(res.err), res.err.strip()[:200])
    return res


def json_de(texto, servicio, operacion, rc=0):
    """Parsea JSON. Si no lo es, error de tipo json_invalido, sin mostrar el contenido."""
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        raise ErrorAws(servicio, operacion, rc, 'json_invalido', 'la salida no es JSON') from None


def leer(servicio, operacion, *args, pos=None, vacio_ok=False, ausente=()):
    """Lee una operación y devuelve su JSON según su contrato.

    - rc != 0: devuelve None si el tipo de error está en `ausente` (el recurso no tiene esa configuración).
      Cualquier otro tipo lanza ErrorAws con servicio, operación, rc y tipo.
    - stdout vacío: devuelve {} si `vacio_ok` (la operación no devuelve datos cuando no hay nada configurado).
      Si no, lanza ErrorAws de tipo stdout_vacio.
    - stdout que no es JSON: lanza ErrorAws de tipo json_invalido.
    """
    res = ro(servicio, operacion, *args, pos=pos, check=False)
    if res.rc != 0:
        tipo = tipo_de(res.err)
        if tipo in ausente:
            return None
        raise ErrorAws(servicio, operacion, res.rc, tipo, res.err.strip()[:200])
    texto = res.out.strip()
    if not texto:
        if vacio_ok:
            return {}
        raise ErrorAws(servicio, operacion, res.rc, 'stdout_vacio', 'la operación debía devolver JSON')
    return json_de(texto, servicio, operacion, res.rc)


class Informe:
    def __init__(self, out):
        self.out = out
        self.lineas = []
        self.errores = []
        os.makedirs(os.path.join(out, 'rollback'), mode=0o700, exist_ok=True)

    def linea(self, texto=''):
        print(texto)
        self.lineas.append(texto)

    def error(self, texto):
        limpio = redactar(texto)[:400]
        self.linea('ERROR: ' + limpio)
        self.errores.append(limpio)

    def guardar_rollback(self, nombre, datos):
        ruta = os.path.join(self.out, 'rollback', nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(datos, f, indent=2, ensure_ascii=False)
        os.chmod(ruta, 0o600)
        self.linea(f'  copia de rollback guardada en {ruta} (permisos 600, sin valores de variables)')

    def guardar_resumen(self):
        ruta = os.path.join(self.out, 'resumen.txt')
        with open(ruta, 'w', encoding='utf-8') as f:
            f.write('\n'.join(self.lineas) + '\n')
        os.chmod(ruta, 0o600)
        return ruta


def paso(inf, titulo, fn, *args):
    """Un paso del informe. Si una lectura falla, el error queda registrado y devuelve None; los demás pasos siguen."""
    inf.linea('')
    inf.linea('## ' + titulo)
    try:
        return fn(inf, *args)
    except ErrorAws as exc:
        inf.error(str(exc))
        return None


def chequeo(inf, nombre, fn):
    """Una comprobación dentro de un paso: si falla, se registra y las siguientes comprobaciones siguen."""
    try:
        return fn()
    except ErrorAws as exc:
        inf.error(f'{nombre}: {exc}')
        return None


def omitido(inf, titulo, motivo):
    inf.linea('')
    inf.linea('## ' + titulo)
    inf.linea(f'No se analiza: {motivo}')


def paso_identidad(inf):
    try:
        d = leer('sts', 'get-caller-identity')
    except ErrorAws as exc:
        raise AutenticacionFallida(str(exc)) from None
    inf.linea(f"Cuenta: ****{d['Account'][-4:]} · identidad: {enmascarar(d['Arn'])}")
    return d


def paso_config_publica(inf, fn):
    """Configuración de la Lambda pública. Devuelve la respuesta completa, en memoria, sin sanitizar."""
    cfg = leer('lambda', 'get-function-configuration', '--function-name', fn)
    variables = sorted(((cfg.get('Environment') or {}).get('Variables') or {}).keys())
    inf.linea(f"Nombre: {cfg.get('FunctionName')} · runtime: {cfg.get('Runtime')} · handler: {cfg.get('Handler')}")
    inf.linea(f"Memoria: {cfg.get('MemorySize')} MB · timeout: {cfg.get('Timeout')} s · "
              f"estado: {cfg.get('State')} / {cfg.get('LastUpdateStatus')}")
    inf.linea(f"Rol: {enmascarar(cfg.get('Role', ''))}")
    inf.linea('Variables de entorno (solo nombres): ' + (', '.join(variables) or '(ninguna)'))
    inf.guardar_rollback('lambda-publica-configuracion.json', sin_valores(cfg))
    return cfg


def paso_lambda_detalles(inf, fn):
    url = leer('lambda', 'get-function-url-config', '--function-name', fn, ausente=('ResourceNotFoundException',))
    if url is None:
        inf.linea('Function URL: no configurada')
    else:
        host = url.get('FunctionUrl', '').split('//')[-1].split('/')[0]
        inf.linea(f"Function URL: AuthType={url.get('AuthType')} · host={host}")

    pol = leer('lambda', 'get-policy', '--function-name', fn, ausente=('ResourceNotFoundException',))
    if pol is None:
        inf.linea('Resource policy: no existe')
    else:
        doc = json_de(pol.get('Policy') or '{}', 'lambda', 'get-policy')
        for st in como_lista(doc.get('Statement')):
            principal = st.get('Principal')
            resumen = principal if isinstance(principal, str) else json.dumps(principal, sort_keys=True)
            inf.linea(f"  permiso: {st.get('Sid')} · {st.get('Action')} · principal={resumen} · "
                      f"condiciones={'sí' if st.get('Condition') else 'no'}")

    conc = leer('lambda', 'get-function-concurrency', '--function-name', fn, vacio_ok=True)
    inf.linea('Concurrencia reservada: ' + str(conc.get('ReservedConcurrentExecutions', 'ninguna')))


def paso_lambda_admin(inf):
    admin = leer('lambda', 'get-function-configuration', '--function-name', FN_ADMIN,
                 ausente=('ResourceNotFoundException',))
    if admin is None:
        inf.linea(f'Lambda admin {FN_ADMIN}: no existe (esperado antes del deploy)')
    else:
        inf.linea(f'Lambda admin {FN_ADMIN}: YA EXISTE (este preflight no la modifica)')


def politica_inline(inf, rol, nombre):
    resp = leer('iam', 'get-role-policy', '--role-name', rol, '--policy-name', nombre, ausente=('NoSuchEntity',))
    if resp is None:
        inf.linea(f"  política '{nombre}': no existe")
        return
    documento = resp.get('PolicyDocument') or {}
    inf.guardar_rollback(f'iam-{nombre}.json', documento)
    for st in como_lista(documento.get('Statement')):
        acciones = como_lista(st.get('Action'))
        recursos = como_lista(st.get('Resource'))
        inf.linea(f"  política '{nombre}': {st.get('Sid', '(sin Sid)')} · acciones: {', '.join(map(str, acciones)) or '-'}"
                  f" · recursos: {enmascarar_recursos(' | '.join(map(str, recursos))) or '-'}")


def paso_iam(inf, rol):
    inf.linea(f'Rol: {rol}')
    creado = (leer('iam', 'get-role', '--role-name', rol).get('Role') or {}).get('CreateDate', '?')
    inf.linea(f'Creado: {creado}')
    adjuntas = leer('iam', 'list-attached-role-policies', '--role-name', rol, vacio_ok=True).get('AttachedPolicies')
    inf.linea('Políticas administradas adjuntas: '
              + (', '.join(p.get('PolicyName', '?') for p in como_lista(adjuntas)) or 'ninguna'))
    nombres = leer('iam', 'list-role-policies', '--role-name', rol, vacio_ok=True).get('PolicyNames') or []
    inf.linea('Políticas inline: ' + (', '.join(nombres) or 'ninguna'))
    for nombre in nombres:
        chequeo(inf, f"política inline '{nombre}'", lambda n=nombre: politica_inline(inf, rol, n))


def nombre_politica(tipo, pid):
    if not pid:
        return '-'
    try:
        if tipo == 'cache':
            d = leer('cloudfront', 'get-cache-policy', '--id', pid)
            return d.get('CachePolicy', {}).get('CachePolicyConfig', {}).get('Name', '?')
        d = leer('cloudfront', 'get-origin-request-policy', '--id', pid)
        return d.get('OriginRequestPolicy', {}).get('OriginRequestPolicyConfig', {}).get('Name', '?')
    except ErrorAws as exc:
        return f'? ({exc.tipo})'


def describir_behavior(inf, nombre, beh):
    funciones = [f"{f.get('EventType')}:{str(f.get('FunctionARN', '')).rsplit('/', 1)[-1]}"
                 for f in (beh.get('FunctionAssociations') or {}).get('Items', [])]
    inf.linea(f"  {nombre}: target={beh.get('TargetOriginId')} · viewer={beh.get('ViewerProtocolPolicy')} · "
              f"caché={nombre_politica('cache', beh.get('CachePolicyId'))} · "
              f"origen-request={nombre_politica('orp', beh.get('OriginRequestPolicyId'))} · "
              f"funciones={', '.join(funciones) or 'ninguna'} · métodos={(beh.get('AllowedMethods') or {}).get('Quantity')}")


def funcion_redireccion(inf):
    publicadas = leer('cloudfront', 'list-functions', '--stage', 'LIVE', vacio_ok=True) \
        .get('FunctionList', {}).get('Items') or []
    if not any(f.get('Name') == REDIRECT for f in publicadas):
        inf.linea(f'Función {REDIRECT}: no está publicada en LIVE')
        return
    ruta = os.path.join(inf.out, 'rollback', f'{REDIRECT}-LIVE.js')
    codigo = leer('cloudfront', 'get-function', '--name', REDIRECT, '--stage', 'LIVE', pos=ruta)
    inf.linea(f"Función {REDIRECT}: publicada en LIVE · ETag={codigo.get('ETag')}")
    if os.path.exists(ruta):
        os.chmod(ruta, 0o600)
        inf.linea(f'  código LIVE guardado para rollback en {ruta} (permisos 600)')


def oac_existentes(inf):
    oacs = leer('cloudfront', 'list-origin-access-controls', vacio_ok=True) \
        .get('OriginAccessControlList', {}).get('Items') or []
    inf.linea('OAC existentes: ' + (', '.join(o.get('Name', '?') for o in oacs) or 'ninguna'))


def politicas_origen_propias(inf):
    orps = leer('cloudfront', 'list-origin-request-policies', '--type', 'custom', vacio_ok=True) \
        .get('OriginRequestPolicyList', {}).get('Items') or []
    nombres = [i.get('OriginRequestPolicy', {}).get('OriginRequestPolicyConfig', {}).get('Name') for i in orps]
    inf.linea('Políticas de origen propias: ' + (', '.join(n for n in nombres if n) or 'ninguna'))


def paso_cloudfront(inf):
    items = leer('cloudfront', 'list-distributions', vacio_ok=True).get('DistributionList', {}).get('Items') or []
    sitio = [d for d in items if DOMINIO in (d.get('Aliases') or {}).get('Items', [])]
    if len(sitio) != 1:
        inf.linea(f'Distribuciones con alias {DOMINIO}: {len(sitio)} (se esperaba 1). Se omite esta parte.')
        return {}
    did = sitio[0]['Id']
    dist = leer('cloudfront', 'get-distribution', '--id', did).get('Distribution') or {}
    rc = leer('cloudfront', 'get-distribution-config', '--id', did)
    inf.guardar_rollback('distribucion-config.json', rc)
    cfg = rc.get('DistributionConfig') or {}
    inf.linea(f"Distribución {did}: status={dist.get('Status')} · ETag de configuración={rc.get('ETag')}")
    for o in (cfg.get('Origins') or {}).get('Items', []):
        tipo = 'S3' if o.get('S3OriginConfig') is not None else 'custom'
        inf.linea(f"  origen {o.get('Id')} ({tipo}): {o.get('DomainName')} · OAC={o.get('OriginAccessControlId') or '-'}")
    describir_behavior(inf, 'behavior por defecto', cfg.get('DefaultCacheBehavior') or {})
    for b in (cfg.get('CacheBehaviors') or {}).get('Items', []):
        describir_behavior(inf, f"behavior {b.get('PathPattern')}", b)
    chequeo(inf, 'función de redirección', lambda: funcion_redireccion(inf))
    chequeo(inf, 'OAC', lambda: oac_existentes(inf))
    chequeo(inf, 'políticas de origen', lambda: politicas_origen_propias(inf))
    return {'cfg': cfg, 'id': did}


def versioning(bucket):
    return leer('s3api', 'get-bucket-versioning', '--bucket', bucket, vacio_ok=True).get('Status', 'desactivado')


def cifrado(inf, bucket):
    enc = leer('s3api', 'get-bucket-encryption', '--bucket', bucket,
               ausente=('ServerSideEncryptionConfigurationNotFoundError',))
    reglas = ((enc or {}).get('ServerSideEncryptionConfiguration') or {}).get('Rules') or []
    algoritmo = (reglas[0].get('ApplyServerSideEncryptionByDefault') or {}).get('SSEAlgorithm') if reglas else None
    inf.linea(f"  cifrado: {algoritmo or 'sin configuración'}")


def bloqueo_publico(inf, bucket):
    pab = leer('s3api', 'get-public-access-block', '--bucket', bucket,
               ausente=('NoSuchPublicAccessBlockConfiguration',))
    if pab is None:
        inf.linea('  public access block: no configurado')
        return
    cfg = pab.get('PublicAccessBlockConfiguration') or {}
    claves = ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')
    completo = all(cfg.get(k) is True for k in claves)
    inf.linea('  public access block: ' + ('completo' if completo else 'incompleto')
              + ' · ' + json.dumps(cfg, sort_keys=True))


def lifecycle(inf, bucket):
    lc = leer('s3api', 'get-bucket-lifecycle-configuration', '--bucket', bucket,
              ausente=('NoSuchLifecycleConfiguration',))
    reglas = (lc or {}).get('Rules') or []
    inf.guardar_rollback('lifecycle-privado.json', {'Rules': reglas})
    detalle = ', '.join(f"{r.get('ID')} ({(r.get('Filter') or {}).get('Prefix', '')}, {r.get('Status')})"
                        for r in reglas)
    inf.linea(f'  lifecycle: {len(reglas)} regla(s)' + (f': {detalle}' if reglas else ''))


def cors(inf, bucket):
    c = leer('s3api', 'get-bucket-cors', '--bucket', bucket, ausente=('NoSuchCORSConfiguration',))
    inf.linea('  CORS: ' + ('no configurado' if c is None else f"{len(c.get('CORSRules') or [])} regla(s)"))


def politica_bucket(inf, bucket):
    p = leer('s3api', 'get-bucket-policy', '--bucket', bucket, ausente=('NoSuchBucketPolicy',))
    if p is None:
        inf.linea('  bucket policy: no presente')
        return
    forzado = 'aws:SecureTransport' in (p.get('Policy') or '')
    inf.linea('  bucket policy: presente' + (' (incluye la condición aws:SecureTransport)' if forzado else ''))


def hay_objetos(bucket, prefijo):
    """Solo sabe si el prefijo tiene al menos un objeto. No devuelve nombres de keys."""
    r = leer('s3api', 'list-objects-v2', '--bucket', bucket, '--prefix', prefijo, '--max-keys', '1', vacio_ok=True)
    return (r.get('KeyCount') or 0) > 0


def bucket_privado(inf, bucket):
    chequeo(inf, 'versioning', lambda: inf.linea('  versioning: ' + versioning(bucket)))
    chequeo(inf, 'cifrado', lambda: cifrado(inf, bucket))
    chequeo(inf, 'public access block', lambda: bloqueo_publico(inf, bucket))
    chequeo(inf, 'lifecycle', lambda: lifecycle(inf, bucket))
    chequeo(inf, 'CORS', lambda: cors(inf, bucket))
    chequeo(inf, 'bucket policy', lambda: politica_bucket(inf, bucket))
    for prefijo in PREFIJOS:
        chequeo(inf, f'prefijo {prefijo}',
                lambda p=prefijo: inf.linea(f"  prefijo {p}: {'sí' if hay_objetos(bucket, p) else 'no'}"))


def website(inf, bucket):
    w = leer('s3api', 'get-bucket-website', '--bucket', bucket, ausente=('NoSuchWebsiteConfiguration',))
    inf.linea('  website: ' + ('no configurado' if w is None else 'configurado'))


def publicas(inf, bucket):
    r = ro('s3api', 'head-object', '--bucket', bucket, '--key', 'resenas/publicas.json', check=False)
    if r.rc == 0:
        inf.linea('  resenas/publicas.json: existe')
    elif tipo_de(r.err) in ('404', 'NotFound'):
        inf.linea('  resenas/publicas.json: no existe')
    else:
        raise ErrorAws('s3api', 'head-object', r.rc, tipo_de(r.err), r.err.strip()[:200])


def bucket_sitio(inf, bucket):
    inf.linea(f'Bucket del sitio: {bucket} (origen S3 de la distribución)')
    chequeo(inf, 'versioning', lambda: inf.linea('  versioning: ' + versioning(bucket)))
    chequeo(inf, 'public access block', lambda: bloqueo_publico(inf, bucket))
    chequeo(inf, 'website', lambda: website(inf, bucket))
    chequeo(inf, 'resenas/publicas.json', lambda: publicas(inf, bucket))


def paso_s3(inf, variables, origen_sitio):
    if variables is None:
        inf.linea('Bucket privado: no se inspecciona porque no se leyó la configuración de la Lambda.')
    elif not variables.get('RESENAS_BUCKET'):
        inf.error('RESENAS_BUCKET: la Lambda pública no tiene esa variable; no se inspecciona el bucket privado.')
    else:
        privado = variables['RESENAS_BUCKET']
        inf.linea(f'Bucket privado: {privado} (valor leído de la configuración en memoria)')
        bucket_privado(inf, privado)
    if origen_sitio:
        bucket_sitio(inf, origen_sitio.split('.s3')[0])
    else:
        inf.linea('Bucket del sitio: no determinado (la distribución no tiene origen S3 o no se pudo leer)')


def paso_logs(inf):
    grupos = leer('logs', 'describe-log-groups', '--log-group-name-prefix', '/aws/lambda/ork3d',
                  vacio_ok=True).get('logGroups') or []
    for g in grupos:
        inf.linea(f"Log group {g.get('logGroupName')}: retención={g.get('retentionInDays', 'sin límite')} días")
    if not grupos:
        inf.linea('Sin log groups /aws/lambda/ork3d')


def identidad_ses(inf, identidad):
    nombre = identidad.get('IdentityName', '')
    mostrado = correo_enmascarado(nombre) if identidad.get('IdentityType') == 'EMAIL_ADDRESS' else nombre
    det = leer('sesv2', 'get-email-identity', '--email-identity', nombre)
    dkim = (det.get('DkimAttributes') or {}).get('Status', 'n/d')
    inf.linea(f"  {identidad.get('IdentityType')} {mostrado}: verificación={identidad.get('VerificationStatus')} · DKIM={dkim}")


def paso_ses(inf):
    cuenta = leer('sesv2', 'get-account')
    cuota = cuenta.get('SendQuota') or {}
    inf.linea(f"SES: envío={cuenta.get('SendingEnabled')} · acceso de producción={cuenta.get('ProductionAccessEnabled')} · "
              f"enforcement={cuenta.get('EnforcementStatus')}")
    inf.linea(f"SES cuota: máximo 24 h={cuota.get('Max24HourSend')} · tasa={cuota.get('MaxSendRate')}/s · "
              f"enviados 24 h={cuota.get('SentLast24Hours')}")
    identidades = leer('sesv2', 'list-email-identities', vacio_ok=True).get('EmailIdentities') or []
    inf.linea(f'Identidades en {region()}: {len(identidades)} (SES es regional: solo se mira esta región)')
    for i in identidades:
        chequeo(inf, 'identidad SES', lambda i=i: identidad_ses(inf, i))


def main(argv):
    out = argv[1] if len(argv) > 1 else OUT_POR_DEFECTO
    os.environ.setdefault('AWS_PROFILE', 'default')
    os.environ.setdefault('AWS_REGION', 'us-east-1')
    inf = Informe(out)
    inf.linea(f'Preflight AWS V2 (solo lectura) · perfil={perfil()} · región={region()}')
    try:
        paso_identidad(inf)
    except AutenticacionFallida as exc:
        print(f'ERROR: no se pudo autenticar con el perfil {perfil()}: {redactar(str(exc))}', file=sys.stderr)
        return 1
    except OperacionNoPermitida as exc:
        print(f'BLOQUEADO: operación no permitida en el preflight: {exc}', file=sys.stderr)
        return 3

    try:
        cfg = paso(inf, '2. Lambda pública V1: configuración', paso_config_publica, FN_PUBLICA)
        paso(inf, '3. Lambda pública V1: Function URL, permisos y concurrencia', paso_lambda_detalles, FN_PUBLICA)
        paso(inf, '4. Lambda admin', paso_lambda_admin)
        rol = ((cfg or {}).get('Role') or '').rsplit('/', 1)[-1]
        if rol:
            paso(inf, '5. IAM del rol de la Lambda pública', paso_iam, rol)
        else:
            omitido(inf, '5. IAM del rol de la Lambda pública', 'no se leyó el rol (falló la configuración de la Lambda)')
        cf = paso(inf, '6. CloudFront', paso_cloudfront) or {}
        origen_s3 = next((o.get('DomainName') for o in ((cf.get('cfg') or {}).get('Origins', {}).get('Items') or [])
                          if o.get('S3OriginConfig') is not None), None)
        variables = None if cfg is None else (cfg.get('Environment') or {}).get('Variables') or {}
        paso(inf, '7. S3 (bucket privado y del sitio)', paso_s3, variables, origen_s3)
        paso(inf, '8. CloudWatch Logs', paso_logs)
        paso(inf, '9. SES (solo lectura)', paso_ses)
    except OperacionNoPermitida as exc:
        print(f'BLOQUEADO: operación no permitida en el preflight: {exc}', file=sys.stderr)
        return 3

    ruta = inf.guardar_resumen()
    inf.linea('')
    inf.linea(f"Resumen en {ruta}. Pasos con error: {len(inf.errores)}")
    return 2 if inf.errores else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
