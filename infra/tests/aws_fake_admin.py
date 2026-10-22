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
BOOLEANAS = {'invoked-via-function-url'}
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


def configuracion_distribucion():
    return {
        'CallerReference': 'deploy-test', 'Aliases': {'Quantity': 1, 'Items': ['ork3d.com']},
        'DefaultRootObject': 'index.html', 'Comment': '', 'Enabled': True, 'PriceClass': 'PriceClass_All',
        'Origins': {'Quantity': 1, 'Items': [{
            'Id': 'sitio', 'DomainName': 'ork3d.com.s3.us-east-1.amazonaws.com', 'OriginPath': '',
            'S3OriginConfig': {'OriginAccessIdentity': ''}, 'OriginAccessControlId': 'E1OACDEMO',
            'CustomHeaders': {'Quantity': 0}}]},
        'DefaultCacheBehavior': {
            'TargetOriginId': 'sitio', 'ViewerProtocolPolicy': 'redirect-to-https', 'CachePolicyId': CACHE_OPTIMIZADO,
            'AllowedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD'],
                               'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            'Compress': True,
            'FunctionAssociations': {'Quantity': 1, 'Items': [{
                'EventType': 'viewer-request', 'FunctionARN': f'arn:aws:cloudfront::{ACC}:function/ork3d-redirect-www'}]},
            'LambdaFunctionAssociations': {'Quantity': 0}},
        'CacheBehaviors': {'Quantity': 1, 'Items': [{
            'PathPattern': '/opinar*', 'TargetOriginId': 'sitio', 'ViewerProtocolPolicy': 'redirect-to-https',
            'CachePolicyId': CACHE_OPTIMIZADO,
            'AllowedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD'],
                               'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            'Compress': True, 'FunctionAssociations': {'Quantity': 0}, 'LambdaFunctionAssociations': {'Quantity': 0}}]},
    }


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
        'fn': {'etag': 'E2FUNCDEMO', 'codigo': 'function handler(event) { return event.request; }\n'},
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
        st['dist']['config'] = json.loads(leer_archivo(o['distribution-config']))
        st['dist']['etag'] = 'E' + uuid.uuid4().hex[:12].upper()
        st['dist']['estado'] = 'InProgress'
        st['dist']['pendientes'] = int(os.environ.get('FAKE_DIST_LENTO', '0'))
        return {'Distribution': {'Id': DIST, 'Status': 'InProgress'}, 'ETag': st['dist']['etag']}

    if (svc, op) == ('cloudfront', 'describe-function'):
        return {'FunctionSummary': {'Name': o.get('name'), 'FunctionMetadata': {'Stage': 'LIVE'}},
                'ETag': st['fn']['etag']}

    if (svc, op) == ('cloudfront', 'get-function'):
        if pos:
            with open(pos[-1], 'w', encoding='utf-8') as f:
                f.write(st['fn']['codigo'])
        return {'ETag': st['fn']['etag'], 'ContentType': 'application/javascript'}

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
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'{svc} {op}\n')
    st = cargar(ruta)
    try:
        resultado = despachar(svc, op, o, pos, st)
    except Fallo as exc:
        guardar(ruta, st)
        sys.stderr.write(f'An error occurred ({exc.codigo}) when calling the {op} operation: {exc.msg}\n')
        sys.exit(255)
    guardar(ruta, st)
    if resultado is not None:
        print(json.dumps(resultado))


if __name__ == '__main__':
    main()
