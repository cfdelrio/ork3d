#!/usr/bin/env python3
"""Preflight AWS de V2: lee la configuración actual de la cuenta. SOLO LECTURA.

Lee: Lambda pública (configuración, Function URL, resource policy, concurrencia), rol IAM de la Lambda
pública, distribución de CloudFront del sitio (con su copia de rollback), función de redirección, OAC y
políticas de origen existentes, buckets privado y del sitio (configuración y existencia de prefijos), log
groups de CloudWatch y SES (estado de la cuenta e identidades, sin crear ni verificar nada).

Garantías:
- Cada llamada a AWS pasa por ro(). Solo ejecuta operaciones de PERMITIDAS, nunca las que tienen un verbo
  mutante (VERBOS_MUTANTES) y nunca las de EXCLUIDAS (contenido de objetos, URLs firmadas, contactos).
  Cualquier otra operación aborta el script antes de llamar a AWS.
- No imprime AccessKey, SecretKey ni SessionToken. Los errores de la CLI se redactan antes de mostrarse.
- Enmascara el account ID (últimos 4 dígitos) y las direcciones de correo.
- De las variables de entorno de la Lambda imprime solo los nombres. Las copias de rollback no guardan valores.
- No lee contenido de objetos: ni reseñas, ni tokens, ni contactos, ni comentarios.
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
PREFIJOS = ('resenas/', 'tokens/', 'claims/', 'pendientes/', 'notificaciones/', 'admin/')
OUT_POR_DEFECTO = '/tmp/ork3d-v2-preflight'

# Lista explícita de lectura: (servicio, expresión sobre el nombre de la operación).
PERMITIDAS = (
    ('sts', r'get-caller-identity'),
    ('lambda', r'get-.+'),
    ('lambda', r'list-.+'),
    ('cloudfront', r'get-.+'),
    ('cloudfront', r'list-.+'),
    ('s3api', r'get-.+'),
    ('s3api', r'head-.+'),
    ('s3api', r'list-objects-v2'),  # solo para comprobar que un prefijo existe, con --max-keys 1
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


def ro(servicio, operacion, *args, pos=None, check=True):
    """Única puerta a AWS del preflight. Verifica la lista explícita antes de llamar a la CLI."""
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
        raise RuntimeError(f'{servicio} {operacion} rc={r.returncode}: {res.err.strip()[:300]}')
    return res


class Informe:
    def __init__(self, out):
        self.out = out
        self.lineas = []
        self.errores = []
        os.makedirs(os.path.join(out, 'rollback'), mode=0o700, exist_ok=True)

    def linea(self, texto=''):
        print(texto)
        self.lineas.append(texto)

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
    inf.linea('')
    inf.linea('## ' + titulo)
    try:
        return fn(inf, *args)
    except OperacionNoPermitida:
        raise
    except Exception as exc:  # un paso que falla no detiene los demás
        inf.linea('ERROR: ' + redactar(str(exc))[:300])
        inf.errores.append(titulo)
        return None


def paso_identidad(inf):
    r = ro('sts', 'get-caller-identity', check=False)
    if r.rc != 0:
        raise AutenticacionFallida(r.err.strip()[:300])
    d = json.loads(r.out)
    inf.linea(f"Cuenta: ****{d['Account'][-4:]} · identidad: {enmascarar(d['Arn'])}")
    return d


def paso_lambda_publica(inf, fn):
    cfg = json.loads(ro('lambda', 'get-function-configuration', '--function-name', fn).out)
    variables = sorted(((cfg.get('Environment') or {}).get('Variables') or {}).keys())
    inf.linea(f"Nombre: {cfg.get('FunctionName')} · runtime: {cfg.get('Runtime')} · handler: {cfg.get('Handler')}")
    inf.linea(f"Memoria: {cfg.get('MemorySize')} MB · timeout: {cfg.get('Timeout')} s · "
              f"estado: {cfg.get('State')} / {cfg.get('LastUpdateStatus')}")
    inf.linea(f"Rol: {enmascarar(cfg.get('Role', ''))}")
    inf.linea('Variables de entorno (solo nombres): ' + (', '.join(variables) or '(ninguna)'))
    inf.guardar_rollback('lambda-publica-configuracion.json', sin_valores(cfg))

    url = ro('lambda', 'get-function-url-config', '--function-name', fn, check=False)
    if url.rc == 0:
        u = json.loads(url.out)
        host = u.get('FunctionUrl', '').split('//')[-1].split('/')[0]
        inf.linea(f"Function URL: AuthType={u.get('AuthType')} · host={host}")
    else:
        inf.linea('Function URL: no configurada')

    pol = ro('lambda', 'get-policy', '--function-name', fn, check=False)
    if pol.rc == 0:
        doc = json.loads(json.loads(pol.out).get('Policy', '{}'))
        for st in doc.get('Statement', []):
            principal = st.get('Principal')
            resumen = principal if isinstance(principal, str) else json.dumps(principal, sort_keys=True)
            inf.linea(f"  permiso: {st.get('Sid')} · {st.get('Action')} · principal={resumen} · "
                      f"condiciones={'sí' if st.get('Condition') else 'no'}")
    else:
        inf.linea('Resource policy: no existe')

    conc = ro('lambda', 'get-function-concurrency', '--function-name', fn, check=False)
    inf.linea('Concurrencia reservada: ' + (str(json.loads(conc.out).get('ReservedConcurrentExecutions', 'ninguna'))
                                             if conc.rc == 0 else 'no leída'))

    admin = ro('lambda', 'get-function-configuration', '--function-name', FN_ADMIN, check=False)
    if admin.rc == 0:
        inf.linea(f'Lambda admin {FN_ADMIN}: YA EXISTE (este preflight no la modifica)')
    elif 'ResourceNotFoundException' in admin.err:
        inf.linea(f'Lambda admin {FN_ADMIN}: no existe (esperado antes del deploy)')
    else:
        inf.linea(f'Lambda admin {FN_ADMIN}: no se pudo determinar')
    return cfg


def paso_iam(inf, rol):
    r = json.loads(ro('iam', 'get-role', '--role-name', rol).out).get('Role', {})
    inf.linea(f"Rol {rol}: creado {r.get('CreateDate', '?')}")
    adjuntas = json.loads(ro('iam', 'list-attached-role-policies', '--role-name', rol).out)
    inf.linea('Políticas administradas adjuntas: '
              + (', '.join(p.get('PolicyName', '?') for p in adjuntas.get('AttachedPolicies', [])) or 'ninguna'))
    nombres = json.loads(ro('iam', 'list-role-policies', '--role-name', rol).out).get('PolicyNames', [])
    for nombre in nombres:
        doc = json.loads(ro('iam', 'get-role-policy', '--role-name', rol, '--policy-name', nombre).out)
        documento = doc.get('PolicyDocument', {})
        inf.guardar_rollback(f'iam-{nombre}.json', documento)
        for st in documento.get('Statement', []):
            acciones = st.get('Action') if isinstance(st.get('Action'), list) else [st.get('Action')]
            recursos = st.get('Resource') if isinstance(st.get('Resource'), list) else [st.get('Resource')]
            inf.linea(f"  política '{nombre}': {st.get('Sid', '(sin Sid)')} · {', '.join(map(str, acciones))} · "
                      f"{enmascarar_recursos(' | '.join(map(str, recursos)))}")


def nombre_politica(tipo, pid):
    if not pid:
        return '-'
    if tipo == 'cache':
        r = ro('cloudfront', 'get-cache-policy', '--id', pid, check=False)
        return json.loads(r.out).get('CachePolicy', {}).get('CachePolicyConfig', {}).get('Name', '?') if r.rc == 0 else '?'
    r = ro('cloudfront', 'get-origin-request-policy', '--id', pid, check=False)
    return json.loads(r.out).get('OriginRequestPolicy', {}).get('OriginRequestPolicyConfig', {}).get('Name', '?') \
        if r.rc == 0 else '?'


def describir_behavior(inf, nombre, beh):
    funciones = [f"{f.get('EventType')}:{str(f.get('FunctionARN', '')).rsplit('/', 1)[-1]}"
                 for f in (beh.get('FunctionAssociations') or {}).get('Items', [])]
    inf.linea(f"  {nombre}: target={beh.get('TargetOriginId')} · viewer={beh.get('ViewerProtocolPolicy')} · "
              f"caché={nombre_politica('cache', beh.get('CachePolicyId'))} · "
              f"origen-request={nombre_politica('orp', beh.get('OriginRequestPolicyId'))} · "
              f"funciones={', '.join(funciones) or 'ninguna'} · métodos={(beh.get('AllowedMethods') or {}).get('Quantity')}")


def paso_cloudfront(inf):
    items = json.loads(ro('cloudfront', 'list-distributions').out).get('DistributionList', {}).get('Items', []) or []
    sitio = [d for d in items if DOMINIO in (d.get('Aliases') or {}).get('Items', [])]
    if len(sitio) != 1:
        inf.linea(f'Distribuciones con alias {DOMINIO}: {len(sitio)} (se esperaba 1). Se omite esta parte.')
        return {}
    did = sitio[0]['Id']
    dist = json.loads(ro('cloudfront', 'get-distribution', '--id', did).out).get('Distribution', {})
    rc = json.loads(ro('cloudfront', 'get-distribution-config', '--id', did).out)
    inf.guardar_rollback('distribucion-config.json', rc)
    cfg = rc.get('DistributionConfig', {})
    inf.linea(f"Distribución {did}: status={dist.get('Status')} · ETag de configuración={rc.get('ETag')}")
    for o in cfg.get('Origins', {}).get('Items', []):
        tipo = 'S3' if o.get('S3OriginConfig') is not None else 'custom'
        inf.linea(f"  origen {o.get('Id')} ({tipo}): {o.get('DomainName')} · OAC={o.get('OriginAccessControlId') or '-'}")
    describir_behavior(inf, 'behavior por defecto', cfg.get('DefaultCacheBehavior', {}))
    for b in (cfg.get('CacheBehaviors') or {}).get('Items', []):
        describir_behavior(inf, f"behavior {b.get('PathPattern')}", b)

    publicadas = json.loads(ro('cloudfront', 'list-functions', '--stage', 'LIVE').out) \
        .get('FunctionList', {}).get('Items', []) or []
    if any(f.get('Name') == REDIRECT for f in publicadas):
        ruta = os.path.join(inf.out, 'rollback', f'{REDIRECT}-LIVE.js')
        codigo = ro('cloudfront', 'get-function', '--name', REDIRECT, '--stage', 'LIVE', pos=ruta, check=False)
        etag = json.loads(codigo.out).get('ETag') if codigo.rc == 0 and codigo.out.strip() else None
        inf.linea(f'Función {REDIRECT}: publicada en LIVE · ETag={etag}')
        if os.path.exists(ruta):
            os.chmod(ruta, 0o600)
            inf.linea(f'  código LIVE guardado para rollback en {ruta} (permisos 600)')
    else:
        inf.linea(f'Función {REDIRECT}: no está publicada en LIVE')

    oacs = json.loads(ro('cloudfront', 'list-origin-access-controls').out).get('OriginAccessControlList', {}).get('Items', []) or []
    inf.linea('OAC existentes: ' + (', '.join(o.get('Name', '?') for o in oacs) or 'ninguna'))
    orps = json.loads(ro('cloudfront', 'list-origin-request-policies', '--type', 'custom').out) \
        .get('OriginRequestPolicyList', {}).get('Items', []) or []
    nombres = [i.get('OriginRequestPolicy', {}).get('OriginRequestPolicyConfig', {}).get('Name') for i in orps]
    inf.linea('Políticas de origen propias: ' + (', '.join(n for n in nombres if n) or 'ninguna'))
    return {'cfg': cfg, 'id': did}


def paso_s3(inf, variables, origen_sitio):
    privado = variables.get('RESENAS_BUCKET')
    if privado:
        inf.linea(f'Bucket privado {privado[:4]}… (nombre enmascarado en el informe)')
        inf.linea('  versioning: ' + json.loads(ro('s3api', 'get-bucket-versioning', '--bucket', privado).out).get('Status', 'desactivado'))
        enc = ro('s3api', 'get-bucket-encryption', '--bucket', privado, check=False)
        algoritmo = None
        if enc.rc == 0:
            reglas = json.loads(enc.out).get('ServerSideEncryptionConfiguration', {}).get('Rules', [])
            algoritmo = (reglas[0].get('ApplyServerSideEncryptionByDefault', {}) if reglas else {}).get('SSEAlgorithm')
        inf.linea(f"  cifrado: {algoritmo or 'sin configuración'}")
        pab = ro('s3api', 'get-public-access-block', '--bucket', privado, check=False)
        inf.linea('  public access block: ' + (json.dumps(json.loads(pab.out).get('PublicAccessBlockConfiguration', {}), sort_keys=True)
                                              if pab.rc == 0 else 'no configurado'))
        lc = ro('s3api', 'get-bucket-lifecycle-configuration', '--bucket', privado, check=False)
        reglas = json.loads(lc.out).get('Rules', []) if lc.rc == 0 else []
        inf.guardar_rollback('lifecycle-privado.json', {'Rules': reglas})
        inf.linea(f'  lifecycle: {len(reglas)} regla(s): ' + ', '.join(
            f"{r.get('ID')} ({(r.get('Filter') or {}).get('Prefix', '')}, {r.get('Status')})" for r in reglas))
        cors = ro('s3api', 'get-bucket-cors', '--bucket', privado, check=False)
        inf.linea('  CORS: ' + ('configurado' if cors.rc == 0 else 'no configurado'))
        pol = ro('s3api', 'get-bucket-policy', '--bucket', privado, check=False)
        forzado = pol.rc == 0 and 'aws:SecureTransport' in json.loads(pol.out).get('Policy', '')
        inf.linea('  bucket policy: ' + ('presente' + (' (niega tráfico sin TLS)' if forzado else '') if pol.rc == 0 else 'no presente'))
        for prefijo in PREFIJOS:
            lo = ro('s3api', 'list-objects-v2', '--bucket', privado, '--prefix', prefijo, '--max-keys', '1', check=False)
            hay = lo.rc == 0 and json.loads(lo.out).get('KeyCount', 0) > 0
            inf.linea(f"  prefijo {prefijo}: {'hay al menos un objeto' if hay else 'vacío o inexistente'}")
    else:
        inf.linea('RESENAS_BUCKET no está en la configuración de la Lambda: se omite el bucket privado')

    if origen_sitio:
        sitio = origen_sitio.split('.s3')[0]
        inf.linea(f'Bucket del sitio: {sitio} (origen S3 de la distribución)')
        inf.linea('  versioning: ' + json.loads(ro('s3api', 'get-bucket-versioning', '--bucket', sitio).out).get('Status', 'desactivado'))
        pab = ro('s3api', 'get-public-access-block', '--bucket', sitio, check=False)
        inf.linea('  public access block: ' + (json.dumps(json.loads(pab.out).get('PublicAccessBlockConfiguration', {}), sort_keys=True)
                                              if pab.rc == 0 else 'no configurado'))
        web = ro('s3api', 'get-bucket-website', '--bucket', sitio, check=False)
        inf.linea('  website: ' + ('configurado' if web.rc == 0 else 'no configurado'))
        head = ro('s3api', 'head-object', '--bucket', sitio, '--key', 'resenas/publicas.json', check=False)
        inf.linea('  resenas/publicas.json: ' + ('existe' if head.rc == 0 else 'no existe'))


def paso_logs(inf):
    grupos = json.loads(ro('logs', 'describe-log-groups', '--log-group-name-prefix', '/aws/lambda/ork3d').out).get('logGroups', [])
    for g in grupos:
        inf.linea(f"Log group {g.get('logGroupName')}: retención={g.get('retentionInDays', 'sin límite')} días")
    if not grupos:
        inf.linea('Sin log groups /aws/lambda/ork3d')


def paso_ses(inf):
    cuenta = json.loads(ro('sesv2', 'get-account').out)
    cuota = cuenta.get('SendQuota', {})
    inf.linea(f"SES: envío={cuenta.get('SendingEnabled')} · acceso de producción={cuenta.get('ProductionAccessEnabled')} · "
              f"enforcement={cuenta.get('EnforcementStatus')}")
    inf.linea(f"SES cuota: máximo 24 h={cuota.get('Max24HourSend')} · tasa={cuota.get('MaxSendRate')}/s · "
              f"enviados 24 h={cuota.get('SentLast24Hours')}")
    identidades = json.loads(ro('sesv2', 'list-email-identities').out).get('EmailIdentities', []) or []
    inf.linea(f'Identidades: {len(identidades)}')
    for i in identidades:
        nombre = i.get('IdentityName', '')
        mostrado = correo_enmascarado(nombre) if i.get('IdentityType') == 'EMAIL_ADDRESS' else nombre
        det = json.loads(ro('sesv2', 'get-email-identity', '--email-identity', nombre).out)
        dkim = (det.get('DkimAttributes') or {}).get('Status', 'n/d')
        inf.linea(f"  {i.get('IdentityType')} {mostrado}: verificación={i.get('VerificationStatus')} · DKIM={dkim}")


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
        cfg = paso(inf, '2. Lambda pública V1 y Lambda admin', paso_lambda_publica, FN_PUBLICA) or {}
        rol = (cfg.get('Role') or '').rsplit('/', 1)[-1]
        if rol:
            paso(inf, '3. IAM del rol de la Lambda pública', paso_iam, rol)
        cf = paso(inf, '4. CloudFront', paso_cloudfront) or {}
        origen_s3 = next((o.get('DomainName') for o in (cf.get('cfg', {}).get('Origins', {}).get('Items') or [])
                          if o.get('S3OriginConfig') is not None), None)
        paso(inf, '5. S3 (bucket privado y del sitio)', paso_s3, (cfg.get('Environment') or {}).get('Variables') or {}, origen_s3)
        paso(inf, '6. CloudWatch Logs', paso_logs)
        paso(inf, '7. SES (solo lectura)', paso_ses)
    except OperacionNoPermitida as exc:
        print(f'BLOQUEADO: operación no permitida en el preflight: {exc}', file=sys.stderr)
        return 3

    ruta = inf.guardar_resumen()
    inf.linea('')
    inf.linea(f"Resumen en {ruta}. Pasos con error: {len(inf.errores)}")
    return 2 if inf.errores else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
