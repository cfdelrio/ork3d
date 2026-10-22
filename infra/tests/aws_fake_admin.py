#!/usr/bin/env python3
"""Simulador con estado de la AWS CLI para probar infra/admin-deploy.sh. Sin red, sin credenciales, sin AWS real.

El estado vive en FAKE_ESTADO (JSON). Si no existe, se crea con estado_inicial(). Cada llamada se registra en
FAKE_LOG como "servicio operación" y, para las escrituras, también se guarda en el estado. Las operaciones que no
simula fallan con UnsupportedOperation: así una llamada nueva del script se ve en las pruebas.

Variables de entorno:
  FAKE_ESTADO          archivo de estado (obligatorio)
  FAKE_LOG             registro de llamadas
  FAKE_ROL_TARDE=N     create-function falla N veces con InvalidParameterValueException (rol aún no asumible)
  FAKE_DIST_LENTO=N    tras update-distribution, get-distribution responde InProgress N veces
  FAKE_ORP_RECHAZO=1   create-origin-request-policy siempre falla con un mensaje con datos sensibles
  FAKE_ACTUALIZACION_FALLA=1  update-function-code deja LastUpdateStatus=Failed (el waiter falla)
  (create-origin-request-policy también rechaza cabeceras x-amz-* en Headers.Items, como la primera prueba real)
"""
import base64
import copy
import hashlib
import json
import os
import sys
import uuid

ACC = '9' * 12
DIST = 'ECOW0H6A48HVP'
FN_PUBLICA = 'ork3d-resenas'
FN_ADMIN = 'ork3d-resenas-admin'
BUCKET = 'bucket-privado-fake-1234'
SENTINELA = 'VALOR-SENTINELA-ADMIN-DEPLOY-5522'
FROM = 'remitente-prueba@ejemplo.test'
ADMIN = 'moderacion-prueba@ejemplo.test'
BASICA = 'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
BOOLEANAS = {'invoked-via-function-url', 'only-show-errors'}
MUTANTES = ('create', 'put', 'attach', 'add-permission', 'update', 'delete')
ENV_PUBLICA = {'RESENAS_BUCKET': BUCKET, 'ALLOWED_ORIGIN': 'https://ork3d.com',
               'GOOGLE_REVIEW_URL': 'https://g.page/r/' + SENTINELA}
TRUST = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Service': 'lambda.amazonaws.com'},
                                                  'Action': 'sts:AssumeRole'}]}
CACHE_OPTIMIZADO = '658327ea-f89d-4fab-a63d-7e88639e58f6'


class Fallo(Exception):
    def __init__(self, codigo, mensaje, op=''):
        super().__init__(mensaje)
        self.codigo, self.msg, self.op = codigo, mensaje, op


def _comportamiento_completo(**campos):
    """Forma serializada que CloudFront devuelve para un behavior leído: todos los bloques, incluso vacíos."""
    base = {'Compress': True, 'SmoothStreaming': False,
            'TrustedSigners': {'Enabled': False, 'Quantity': 0},
            'TrustedKeyGroups': {'Enabled': False, 'Quantity': 0},
            'LambdaFunctionAssociations': {'Quantity': 0}, 'FunctionAssociations': {'Quantity': 0},
            'FieldLevelEncryptionId': ''}
    base.update(campos)
    return base


def configuracion_distribucion():
    return {
        'CallerReference': 'deploy-test', 'Aliases': {'Quantity': 1, 'Items': ['ork3d.com']},
        'DefaultRootObject': 'index.html', 'Comment': '', 'Enabled': True, 'PriceClass': 'PriceClass_All',
        'Origins': {'Quantity': 1, 'Items': [{
            'Id': 'sitio', 'DomainName': 'ork3d.com.s3.us-east-1.amazonaws.com', 'OriginPath': '',
            'S3OriginConfig': {'OriginAccessIdentity': ''}, 'OriginAccessControlId': 'E1OACDEMO',
            'CustomHeaders': {'Quantity': 0}}]},
        'DefaultCacheBehavior': _comportamiento_completo(
            TargetOriginId='sitio', ViewerProtocolPolicy='redirect-to-https', CachePolicyId=CACHE_OPTIMIZADO,
            AllowedMethods={'Quantity': 2, 'Items': ['GET', 'HEAD'],
                            'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            FunctionAssociations={'Quantity': 1, 'Items': [{
                'EventType': 'viewer-request', 'FunctionARN': f'arn:aws:cloudfront::{ACC}:function/ork3d-redirect-www'}]}),
        'CacheBehaviors': {'Quantity': 1, 'Items': [_comportamiento_completo(
            PathPattern='/opinar*', TargetOriginId='sitio', ViewerProtocolPolicy='redirect-to-https',
            CachePolicyId=CACHE_OPTIMIZADO,
            AllowedMethods={'Quantity': 2, 'Items': ['GET', 'HEAD'],
                            'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}})]},
    }


# Campos que CloudFront exige en un CacheBehavior nuevo. Sin ellos, UpdateDistribution falla (SmoothStreaming, primero).
CAMPOS_REQUERIDOS_BEHAVIOR = ('SmoothStreaming', 'Compress', 'TrustedSigners', 'TrustedKeyGroups',
                              'LambdaFunctionAssociations', 'FunctionAssociations', 'FieldLevelEncryptionId')


def validar_behavior_nuevo(behavior):
    for campo in CAMPOS_REQUERIDOS_BEHAVIOR:
        if campo not in behavior:
            raise Fallo('InvalidArgument', f'The parameter {campo} flag is missing.' if campo == 'SmoothStreaming'
                        else f'The parameter {campo} is missing.', 'UpdateDistribution')
    if not isinstance(behavior['SmoothStreaming'], bool):
        raise Fallo('InvalidArgument', 'The parameter SmoothStreaming must be a boolean.', 'UpdateDistribution')
    for bloque in ('TrustedSigners', 'TrustedKeyGroups'):
        if set(behavior[bloque]) < {'Enabled', 'Quantity'}:
            raise Fallo('InvalidArgument', f'The parameter {bloque} is incomplete.', 'UpdateDistribution')
    am = behavior.get('AllowedMethods') or {}
    if 'CachedMethods' not in am:
        raise Fallo('InvalidArgument', 'The parameter CachedMethods is missing.', 'UpdateDistribution')


CODIGO_FUNCION_DEMO = 'function handler(event) { return event.request; }\n'
CONFIG_FUNCION_DEMO = {'Comment': 'ORK3D redirects', 'Runtime': 'cloudfront-js-2.0'}
ETAPAS_FUNCION = ('LIVE', 'DEVELOPMENT')


def etag_nuevo(st, etapa):
    st['fn']['contador'] += 1
    return f'E-{etapa}-{st["fn"]["contador"]}'


def etapa_de(o):
    etapa = o.get('stage', 'DEVELOPMENT')  # como la CLI: sin --stage, DEVELOPMENT
    if etapa not in ETAPAS_FUNCION:
        raise Fallo('InvalidArgument', f'stage inválido: {etapa}', 'describe-function')
    return etapa


def estado_inicial():
    return {
        'iam': {'ork3d-resenas-lambda': {'trust': TRUST, 'attached': [BASICA], 'inline': {
            'resenas-bucket-privado': {'Version': '2012-10-17', 'Statement': [
                {'Sid': 'Bucket', 'Effect': 'Allow', 'Action': ['s3:GetObject', 's3:PutObject'],
                 'Resource': f'arn:aws:s3:::{BUCKET}/*'}]}}}},
        'publica': {'config': {'FunctionName': FN_PUBLICA, 'Runtime': 'python3.12', 'Handler': 'app.handler',
                               'MemorySize': 256, 'Timeout': 10, 'State': 'Active',
                               'LastUpdateStatus': 'Successful',
                               'Role': f'arn:aws:iam::{ACC}:role/ork3d-resenas-lambda',
                               'Environment': {'Variables': dict(ENV_PUBLICA)}},
                    'policy': [{'Sid': 'FunctionURLAllowPublicAccess', 'Action': 'lambda:InvokeFunctionUrl',
                                'Principal': '*', 'Condition': {'StringEquals': {
                                    'lambda:FunctionUrlAuthType': 'NONE'}}}]},
        'bucket': {'nombre': BUCKET, 'lifecycle': {'Rules': [{'ID': 'pendientes-vencen-a-los-2-dias',
                                                               'Filter': {'Prefix': 'pendientes/'},
                                                               'Status': 'Enabled'}]}},
        'logs': {},
        'lambda': None,
        'oacs': [],
        'orps': [],
        'dist': {'etag': 'E2TAGDEMO', 'estado': 'Deployed', 'pendientes': 0,
                 'config': configuracion_distribucion()},
        'fn': {'contador': 0, 'llamadas_live': 0,
               'LIVE': {'codigo': CODIGO_FUNCION_DEMO, 'etag': 'E-LIVE-0', 'config': dict(CONFIG_FUNCION_DEMO)},
               'DEVELOPMENT': {'codigo': CODIGO_FUNCION_DEMO, 'etag': 'E-DEV-0', 'config': dict(CONFIG_FUNCION_DEMO)}},
        'ses': {'envio': True, 'produccion': False, 'identidades': {
            FROM: {'IdentityType': 'EMAIL_ADDRESS', 'VerificationStatus': 'SUCCESS',
                   'VerifiedForSendingStatus': True},
            ADMIN: {'IdentityType': 'EMAIL_ADDRESS', 'VerificationStatus': 'SUCCESS',
                    'VerifiedForSendingStatus': True}}},
    }


def cargar(ruta):
    if not os.path.exists(ruta):
        return estado_inicial()
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def guardar(ruta, estado):
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(estado, f, ensure_ascii=False)


def opciones(args):
    o, pos, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith('--'):
            clave = a[2:]
            if clave in BOOLEANAS:
                o[clave] = True
                i += 1
            else:
                o[clave] = args[i + 1] if i + 1 < len(args) else ''
                i += 2
        else:
            pos.append(a)
            i += 1
    return o, pos


def leer_archivo(ruta, binario=False):
    ruta = ruta.replace('fileb://', '', 1).replace('file://', '', 1)
    with open(ruta, 'rb' if binario else 'r', encoding=None if binario else 'utf-8') as f:
        return f.read()


def despachar(svc, op, o, pos, st):
    if (svc, op) == ('sts', 'get-caller-identity'):
        return {'Account': ACC, 'Arn': f'arn:aws:iam::{ACC}:user/operador', 'UserId': 'AIDAFAKE'}

    if (svc, op) == ('lambda', 'get-function-configuration'):
        nombre = o.get('function-name')
        if nombre == FN_PUBLICA:
            return st['publica']['config']
        if nombre == FN_ADMIN and st['lambda']:
            return st['lambda']['config']
        raise Fallo('ResourceNotFoundException', 'Function not found', op)

    if (svc, op) == ('lambda', 'get-policy'):
        nombre = o.get('function-name')
        if nombre == FN_PUBLICA:
            return {'Policy': json.dumps({'Statement': st['publica']['policy']})}
        if not st['lambda'] or not st['lambda']['policy']:
            raise Fallo('ResourceNotFoundException', 'No policy', op)
        return {'Policy': json.dumps({'Statement': st['lambda']['policy']})}

    if (svc, op) == ('lambda', 'get-function-url-config'):
        if not st['lambda'] or not st['lambda']['url']:
            raise Fallo('ResourceNotFoundException', 'No function URL', op)
        return st['lambda']['url']

    if (svc, op) == ('lambda', 'get-function-concurrency'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        return {}

    if (svc, op) == ('lambda', 'create-function'):
        if st['lambda']:
            raise Fallo('ResourceConflictException', 'Function already exist', op)
        rol = o.get('role', '').rsplit('/', 1)[-1]
        pendientes = int(os.environ.get('FAKE_ROL_TARDE', '0'))
        if st.get('intentos_rol', 0) < pendientes:
            st['intentos_rol'] = st.get('intentos_rol', 0) + 1
            raise Fallo('InvalidParameterValueException',
                        'The role defined for the function cannot be assumed by Lambda.', op)
        if rol not in st['iam']:
            raise Fallo('InvalidParameterValueException',
                        'The role defined for the function cannot be assumed by Lambda.', op)
        zip_bytes = leer_archivo(o['zip-file'], binario=True)
        sha = base64.b64encode(hashlib.sha256(zip_bytes).digest()).decode('ascii')
        variables = json.loads(leer_archivo(o['environment']))['Variables']
        config = {'FunctionName': FN_ADMIN, 'Runtime': o['runtime'], 'Handler': o['handler'], 'Role': o['role'],
                  'MemorySize': int(o['memory-size']), 'Timeout': int(o['timeout']), 'State': 'Active',
                  'LastUpdateStatus': 'Successful', 'CodeSha256': sha,
                  'Environment': {'Variables': variables}}
        st['lambda'] = {'config': config, 'url': None, 'policy': [], 'zip_bytes': len(zip_bytes)}
        return config

    if (svc, op) == ('lambda', 'wait:function-active-v2'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        return None

    if (svc, op) == ('lambda', 'get-function'):
        if o.get('function-name') != FN_ADMIN or not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        # Location es una URL firmada de descarga: el script no debe imprimirla. Aquí lleva una firma falsa a propósito.
        return {'Configuration': st['lambda']['config'],
                'Code': {'RepositoryType': 'S3',
                         'Location': 'https://fake-code.example/admin-codigo.zip?X-Amz-Signature=FAKE'}}

    if (svc, op) == ('lambda', 'update-function-code'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        zip_bytes = leer_archivo(o['zip-file'], binario=True)
        cfg = st['lambda']['config']
        cfg['CodeSha256'] = base64.b64encode(hashlib.sha256(zip_bytes).digest()).decode('ascii')
        cfg['LastUpdateStatus'] = 'Failed' if os.environ.get('FAKE_ACTUALIZACION_FALLA') == '1' else 'Successful'
        st['lambda']['zip_bytes'] = len(zip_bytes)
        return cfg

    if (svc, op) == ('lambda', 'wait:function-updated-v2'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        if st['lambda']['config']['LastUpdateStatus'] != 'Successful':
            raise Fallo('ResourceNotReadyException', 'Waiter FunctionUpdatedV2 failed: Max attempts exceeded', op)
        return None

    if (svc, op) == ('lambda', 'create-function-url-config'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        url = {'FunctionUrl': 'https://abcdefghijklmnopqrstuvwx.lambda-url.us-east-1.on.aws/',
               'AuthType': o.get('auth-type'), 'FunctionArn': f'arn:aws:lambda:us-east-1:{ACC}:function:{FN_ADMIN}'}
        st['lambda']['url'] = url
        return url

    if (svc, op) == ('lambda', 'add-permission'):
        if not st['lambda']:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        sid = o['statement-id']
        if any(s['Sid'] == sid for s in st['lambda']['policy']):
            raise Fallo('ResourceConflictException', 'The statement id already exists', op)
        condicion = {'ArnLike': {'AWS:SourceArn': o['source-arn']}}
        if 'function-url-auth-type' in o:
            condicion['StringEquals'] = {'lambda:FunctionUrlAuthType': o['function-url-auth-type']}
        if 'invoked-via-function-url' in o:
            condicion['Bool'] = {'lambda:InvokedViaFunctionUrl': 'true'}
        st['lambda']['policy'].append({'Sid': sid, 'Effect': 'Allow',
                                       'Principal': {'Service': o['principal']}, 'Action': o['action'],
                                       'Resource': f'arn:aws:lambda:us-east-1:{ACC}:function:{FN_ADMIN}',
                                       'Condition': condicion})
        return {'Statement': json.dumps(st['lambda']['policy'][-1])}

    if (svc, op) == ('iam', 'get-role'):
        nombre = o.get('role-name')
        if nombre not in st['iam']:
            raise Fallo('NoSuchEntity', f'Role {nombre} not found', op)
        return {'Role': {'RoleName': nombre, 'Arn': f'arn:aws:iam::{ACC}:role/{nombre}',
                         'CreateDate': '2026-01-01T00:00:00+00:00',
                         'AssumeRolePolicyDocument': st['iam'][nombre]['trust']}}

    if (svc, op) == ('iam', 'create-role'):
        nombre = o.get('role-name')
        if nombre in st['iam']:
            raise Fallo('EntityAlreadyExists', 'Role already exists', op)
        st['iam'][nombre] = {'trust': json.loads(leer_archivo(o['assume-role-policy-document'])),
                             'attached': [], 'inline': {}}
        return {'Role': {'RoleName': nombre, 'Arn': f'arn:aws:iam::{ACC}:role/{nombre}'}}

    if (svc, op) == ('iam', 'list-attached-role-policies'):
        nombre = o.get('role-name')
        if nombre not in st['iam']:
            raise Fallo('NoSuchEntity', 'Role not found', op)
        return {'AttachedPolicies': [{'PolicyName': a.rsplit('/', 1)[-1], 'PolicyArn': a}
                                     for a in st['iam'][nombre]['attached']]}

    if (svc, op) == ('iam', 'attach-role-policy'):
        nombre = o.get('role-name')
        if nombre not in st['iam']:
            raise Fallo('NoSuchEntity', 'Role not found', op)
        st['iam'][nombre]['attached'].append(o['policy-arn'])
        return None

    if (svc, op) == ('iam', 'list-role-policies'):
        nombre = o.get('role-name')
        if nombre not in st['iam']:
            raise Fallo('NoSuchEntity', 'Role not found', op)
        return {'PolicyNames': list(st['iam'][nombre]['inline'].keys())}

    if (svc, op) == ('iam', 'get-role-policy'):
        nombre, politica = o.get('role-name'), o.get('policy-name')
        if nombre not in st['iam'] or politica not in st['iam'][nombre]['inline']:
            raise Fallo('NoSuchEntity', 'Policy not found', op)
        return {'RoleName': nombre, 'PolicyName': politica,
                'PolicyDocument': st['iam'][nombre]['inline'][politica]}

    if (svc, op) == ('iam', 'put-role-policy'):
        nombre = o.get('role-name')
        if nombre not in st['iam']:
            raise Fallo('NoSuchEntity', 'Role not found', op)
        st['iam'][nombre]['inline'][o['policy-name']] = json.loads(leer_archivo(o['policy-document']))
        return None

    if (svc, op) == ('logs', 'describe-log-groups'):
        prefijo = o.get('log-group-name-prefix', '')
        grupos = []
        for nombre, dias in st['logs'].items():
            if nombre.startswith(prefijo):
                grupo = {'logGroupName': nombre}
                if dias is not None:
                    grupo['retentionInDays'] = dias
                grupos.append(grupo)
        return {'logGroups': grupos}

    if (svc, op) == ('logs', 'filter-log-events'):
        # Una línea REPORT por invocación: la escribe el simulador de curl (FAKE_INVOCACIONES) cuando la API responde.
        ruta_marcas = os.environ.get('FAKE_INVOCACIONES', '')
        marcas = []
        if ruta_marcas and os.path.exists(ruta_marcas):
            with open(ruta_marcas, encoding='utf-8') as f:
                marcas = [int(linea) for linea in f if linea.strip()]
        return {'events': [{'timestamp': m, 'message': 'REPORT RequestId: fake', 'eventId': str(m)} for m in marcas]}

    if (svc, op) == ('logs', 'create-log-group'):
        nombre = o.get('log-group-name')
        if nombre in st['logs']:
            raise Fallo('ResourceAlreadyExistsException', 'exists', op)
        st['logs'][nombre] = None
        return None

    if (svc, op) == ('logs', 'put-retention-policy'):
        st['logs'][o.get('log-group-name')] = int(o.get('retention-in-days'))
        return None

    if (svc, op) == ('s3api', 'head-bucket'):
        if o.get('bucket') != st['bucket']['nombre']:
            raise Fallo('404', 'Not Found', op)
        return None

    if (svc, op) == ('s3api', 'get-bucket-lifecycle-configuration'):
        if not st['bucket'].get('lifecycle'):
            raise Fallo('NoSuchLifecycleConfiguration', 'none', op)
        return st['bucket']['lifecycle']

    if (svc, op) == ('cloudfront', 'get-distribution'):
        d = st['dist']
        if d['estado'] == 'InProgress':
            d['pendientes'] -= 1
            if d['pendientes'] <= 0:
                d['estado'] = 'Deployed'
        return {'Distribution': {'Id': DIST, 'Status': d['estado'], 'DistributionConfig': d['config']},
                'ETag': d['etag']}

    if (svc, op) == ('cloudfront', 'get-distribution-config'):
        return {'ETag': st['dist']['etag'], 'DistributionConfig': st['dist']['config']}

    if (svc, op) == ('cloudfront', 'update-distribution'):
        if o.get('if-match') != st['dist']['etag']:
            raise Fallo('PreconditionFailed', 'The If-Match version is missing or not valid', op)
        nueva = json.loads(leer_archivo(o['distribution-config']))
        previos = {b['PathPattern'] for b in st['dist']['config']['CacheBehaviors'].get('Items', [])}
        for b in nueva['CacheBehaviors'].get('Items', []):
            if b['PathPattern'] not in previos:
                validar_behavior_nuevo(b)
        st['dist']['config'] = nueva
        st['dist']['etag'] = 'E' + uuid.uuid4().hex[:12].upper()
        st['dist']['estado'] = 'InProgress'
        st['dist']['pendientes'] = int(os.environ.get('FAKE_DIST_LENTO', '0'))
        return {'Distribution': {'Id': DIST, 'Status': 'InProgress'}, 'ETag': st['dist']['etag']}

    if (svc, op) == ('cloudfront', 'describe-function'):
        etapa = etapa_de(o)
        f = st['fn'][etapa]
        return {'FunctionSummary': {'Name': o.get('name'), 'FunctionConfig': copy.deepcopy(f['config']),
                                    'FunctionMetadata': {'Stage': etapa}},
                'ETag': f['etag']}

    if (svc, op) == ('cloudfront', 'get-function'):
        etapa = etapa_de(o)
        if etapa == 'LIVE':
            st['fn']['llamadas_live'] += 1
            # Simula un cambio de LIVE entre dos lecturas (otra persona publicó): FAKE_CAMBIO_LIVE_EN_LLAMADA=N.
            if str(st['fn']['llamadas_live']) == os.environ.get('FAKE_CAMBIO_LIVE_EN_LLAMADA', ''):
                st['fn']['LIVE']['codigo'] += '// publicado por otra persona\n'
                st['fn']['LIVE']['etag'] = etag_nuevo(st, 'LIVE')
        f = st['fn'][etapa]
        if pos:
            with open(pos[-1], 'w', encoding='utf-8') as fh:
                fh.write(f['codigo'])
        return {'ETag': f['etag'], 'ContentType': 'application/javascript'}

    if (svc, op) == ('cloudfront', 'update-function'):
        # UpdateFunction escribe DEVELOPMENT y exige su ETag vigente. El ETag de LIVE da PreconditionFailed.
        f = st['fn']['DEVELOPMENT']
        if os.environ.get('FAKE_FALLA_UPDATE') == '1' or o.get('if-match') != f['etag']:
            raise Fallo('PreconditionFailed', 'The If-Match version is missing or not valid for the resource.', op)
        cfg = json.loads(leer_archivo(o['function-config']))
        if 'Runtime' not in cfg:
            raise Fallo('InvalidArgument', 'FunctionConfig sin Runtime', op)
        # Como la API real: reemplaza FunctionConfig completo. Un campo omitido (p. ej. KVS) se pierde.
        f['config'] = {k: v for k, v in cfg.items() if k in ('Comment', 'Runtime', 'KeyValueStoreAssociations')}
        f['codigo'] = leer_archivo(o['function-code'], binario=True).decode('utf-8')
        f['etag'] = etag_nuevo(st, 'DEVELOPMENT')
        return {'FunctionSummary': {'Name': o.get('name'), 'FunctionConfig': copy.deepcopy(f['config'])},
                'ETag': f['etag']}

    if (svc, op) == ('cloudfront', 'publish-function'):
        # PublishFunction copia DEVELOPMENT a LIVE y exige el ETag de DEVELOPMENT vigente.
        dev, live = st['fn']['DEVELOPMENT'], st['fn']['LIVE']
        if os.environ.get('FAKE_FALLA_PUBLISH') == '1' or o.get('if-match') != dev['etag']:
            raise Fallo('PreconditionFailed', 'The If-Match version is missing or not valid for the resource.', op)
        live['codigo'] = dev['codigo']
        live['config'] = copy.deepcopy(dev['config'])
        live['etag'] = etag_nuevo(st, 'LIVE')
        return {'FunctionSummary': {'Name': o.get('name'), 'FunctionConfig': copy.deepcopy(live['config'])},
                'ETag': live['etag']}

    if (svc, op) == ('cloudfront', 'create-invalidation'):
        lote = json.loads(leer_archivo(o['invalidation-batch']))
        st.setdefault('invalidaciones', []).append(lote['Paths']['Items'])
        return {'Invalidation': {'Id': 'FAKEINV' + str(len(st['invalidaciones'])), 'Status': 'InProgress'}}

    if (svc, op) == ('s3', 'cp'):
        origen, destino = pos[0], pos[1]
        if destino.startswith('s3://'):
            clave = destino.split('/', 3)[3]
            st.setdefault('sitio', {})[clave] = leer_archivo(origen)
            return None
        clave = origen.split('/', 3)[3]
        if clave not in st.get('sitio', {}):
            raise Fallo('404', 'Not Found', op)
        with open(destino, 'w', encoding='utf-8') as f:
            f.write(st['sitio'][clave])
        return None

    if (svc, op) == ('cloudfront', 'list-origin-access-controls'):
        return {'OriginAccessControlList': {'Quantity': len(st['oacs']), 'Items': st['oacs']}}

    if (svc, op) == ('cloudfront', 'create-origin-access-control'):
        cfg = json.loads(leer_archivo(o['origin-access-control-config']))
        if any(i['Name'] == cfg['Name'] for i in st['oacs']):
            raise Fallo('OriginAccessControlAlreadyExists', 'exists', op)
        oid = 'OAC' + uuid.uuid4().hex[:10].upper()
        st['oacs'].append({'Id': oid, **cfg})
        return {'OriginAccessControl': {'Id': oid, 'OriginAccessControlConfig': cfg}}

    if (svc, op) == ('cloudfront', 'list-origin-request-policies'):
        items = [{'OriginRequestPolicy': {'Id': p['Id'], 'OriginRequestPolicyConfig': p['config']}}
                 for p in st['orps']]
        return {'OriginRequestPolicyList': {'Quantity': len(items), 'Items': items}}

    if (svc, op) == ('cloudfront', 'create-origin-request-policy'):
        cfg = json.loads(leer_archivo(o['origin-request-policy-config']))
        # CloudFront limita OriginRequestPolicyConfig.Comment a 128 caracteres (rechazo observado en el tercer intento).
        if len(cfg.get('Comment', '')) > 128:
            raise Fallo('InvalidArgument', 'The parameter Comment is too big.', op)
        if os.environ.get('FAKE_ORP_RECHAZO') == '1':
            # Mensaje con datos que jamás deben llegar al registro: correo, cuenta, clave y URL firmada.
            cuenta, clave = '9' * 12, 'AKIA' + 'FAKE' * 4  # datos falsos, armados por partes
            raise Fallo('InvalidArgument', 'Policy rejected for ana.moderadora@example.com account ' + cuenta +
                        ' key ' + clave + ' see https://h.example/x?X-Amz-Signature=abc', op)
        # Lo que el servicio rechazó en la primera prueba real: cabeceras x-amz-* listadas en Headers.Items.
        for item in cfg.get('HeadersConfig', {}).get('Headers', {}).get('Items', []):
            if item.lower().startswith('x-amz-'):
                raise Fallo('InvalidArgument', f'Invalid header in HeadersConfig: {item}', op)
        if any(p['config']['Name'] == cfg['Name'] for p in st['orps']):
            raise Fallo('OriginRequestPolicyAlreadyExists', 'exists', op)
        pid = 'ORP' + uuid.uuid4().hex[:10].upper()
        st['orps'].append({'Id': pid, 'config': cfg})
        return {'OriginRequestPolicy': {'Id': pid, 'OriginRequestPolicyConfig': cfg}}

    if (svc, op) == ('sesv2', 'get-account'):
        return {'SendingEnabled': st['ses']['envio'], 'ProductionAccessEnabled': st['ses']['produccion'],
                'EnforcementStatus': 'HEALTHY'}

    if (svc, op) == ('sesv2', 'get-email-identity'):
        nombre = o.get('email-identity')
        ident = st['ses']['identidades'].get(nombre)
        if ident is None:
            raise Fallo('NotFoundException', 'Email identity does not exist.', op)
        return {'IdentityType': ident['IdentityType'], 'VerificationStatus': ident['VerificationStatus'],
                'VerifiedForSendingStatus': ident['VerifiedForSendingStatus'],
                'DkimAttributes': {'Status': 'SUCCESS'}}

    raise Fallo('UnsupportedOperation', f'{svc} {op} no simulado', op)


def main():
    svc, op = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    if op == 'wait':
        op = 'wait:' + args[0]
        args = args[1:]
    o, pos = opciones(args)
    ruta = os.environ['FAKE_ESTADO']
    st = cargar(ruta)
    if os.environ.get('FAKE_LOG'):
        detalle = ''
        if (svc, op) == ('cloudfront', 'get-distribution-config'):
            detalle = f" etag={st['dist']['etag']}"
        if (svc, op) in (('cloudfront', 'describe-function'), ('cloudfront', 'get-function')):
            detalle = f" stage={o.get('stage', 'DEVELOPMENT')}"
        if (svc, op) in (('cloudfront', 'update-function'), ('cloudfront', 'publish-function')):
            detalle = f" if-match={o.get('if-match', '')}"
        if (svc, op) == ('s3', 'cp'):
            detalle = ' upload' if pos[1].startswith('s3://') else ' download'
        if (svc, op) == ('cloudfront', 'update-distribution'):
            detalle = f" if-match={o.get('if-match', '')}"
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'{svc} {op}{detalle}\n')
    try:
        resultado = despachar(svc, op, o, pos, st)
    except Fallo as exc:
        guardar(ruta, st)
        nombre_api = ''.join(p.capitalize() for p in op.replace('wait:', '').split('-'))  # como la CLI real
        sys.stderr.write(f'An error occurred ({exc.codigo}) when calling the {nombre_api} operation: {exc.msg}\n')
        sys.exit(255)
    guardar(ruta, st)
    if resultado is not None:
        print(json.dumps(resultado))


if __name__ == '__main__':
    main()
