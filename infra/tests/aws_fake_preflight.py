#!/usr/bin/env python3
"""Simulador de la AWS CLI para probar infra/preflight_v2.py. Sin red ni credenciales.

Responde con datos de muestra y registra cada llamada. Las operaciones sin respuesta de muestra devuelven {}.
Variables de entorno:
  FAKE_LOG          registro de llamadas (servicio y operación por línea)
  FAKE_STS_FAIL=1   la autenticación falla, con un texto que contiene una clave y un secreto de ejemplo
                    (sirve para comprobar que el preflight los redacta)
"""
import json
import os
import sys

SENTINELA = 'VALOR-SENTINELA-NO-DEBE-SALIR-7731'
CUENTA_FALSA = '9' * 12
# Clave y secreto falsos, armados por partes: así el escaneo de secretos del repo no los marca.
CLAVE_FALSA = 'AKIA' + 'FAKE' * 4
SECRETO_FALSO = 'no-es-un-secreto-real-' * 2
ENV_LAMBDA = {
    'RESENAS_BUCKET': 'bucket-privado-fake-1234',
    'ALLOWED_ORIGIN': 'https://ork3d.com',
    'GOOGLE_REVIEW_URL': 'https://g.page/r/' + SENTINELA,
}
POLITICA_BUCKET = {'Statement': [{'Sid': 'Forzar', 'Effect': 'Deny',
                                  'Condition': {'Bool': {'aws:SecureTransport': 'false'}}}]}
RESPUESTAS = {
    ('lambda', 'get-function-configuration'): {
        'FunctionName': 'ork3d-resenas', 'Runtime': 'python3.12', 'Handler': 'app.handler',
        'MemorySize': 256, 'Timeout': 10, 'State': 'Active', 'LastUpdateStatus': 'Successful',
        'Role': f'arn:aws:iam::{CUENTA_FALSA}:role/ork3d-resenas-lambda',
        'Environment': {'Variables': ENV_LAMBDA}},
    ('lambda', 'get-function-url-config'): {
        'AuthType': 'NONE', 'FunctionUrl': 'https://abcdefghijklmnopqrstuvwx.lambda-url.us-east-1.on.aws/'},
    ('lambda', 'get-policy'): {'Policy': json.dumps({'Statement': [{
        'Sid': 'FunctionURLAllowPublicAccess', 'Action': 'lambda:InvokeFunctionUrl', 'Principal': '*',
        'Condition': {'StringEquals': {'lambda:FunctionUrlAuthType': 'NONE'}}}]})},
    ('lambda', 'get-function-concurrency'): {'ReservedConcurrentExecutions': 5},
    ('iam', 'get-role'): {'Role': {'CreateDate': '2026-01-01T00:00:00+00:00'}},
    ('iam', 'list-attached-role-policies'): {'AttachedPolicies': [{'PolicyName': 'AWSLambdaBasicExecutionRole'}]},
    ('iam', 'list-role-policies'): {'PolicyNames': ['resenas-bucket-privado']},
    ('iam', 'get-role-policy'): {'PolicyDocument': {'Statement': [{
        'Sid': 'Bucket', 'Action': ['s3:GetObject', 's3:PutObject'],
        'Resource': 'arn:aws:s3:::bucket-privado-fake-1234/*'}]}},
    ('cloudfront', 'list-distributions'): {'DistributionList': {'Items': [
        {'Id': 'EXXXXXXXXXXXX', 'Aliases': {'Items': ['ork3d.com']}}]}},
    ('cloudfront', 'get-distribution'): {'Distribution': {'Status': 'Deployed'}},
    ('cloudfront', 'get-distribution-config'): {'ETag': 'E2TAGDEMO', 'DistributionConfig': {
        'Origins': {'Items': [{'Id': 'sitio', 'DomainName': 'ork3d.com.s3.us-east-1.amazonaws.com',
                               'S3OriginConfig': {'OriginAccessIdentity': ''},
                               'OriginAccessControlId': 'E1OACDEMO'}]},
        'DefaultCacheBehavior': {'TargetOriginId': 'sitio', 'ViewerProtocolPolicy': 'redirect-to-https',
                                 'CachePolicyId': '658327ea-f89d-4fab-a63d-7e88639e58f6',
                                 'AllowedMethods': {'Quantity': 2},
                                 'FunctionAssociations': {'Quantity': 1, 'Items': [{
                                     'EventType': 'viewer-request',
                                     'FunctionARN': f'arn:aws:cloudfront::{CUENTA_FALSA}:function/ork3d-redirect-www'}]}},
        'CacheBehaviors': {'Items': []}}},
    ('cloudfront', 'get-cache-policy'): {'CachePolicy': {'CachePolicyConfig': {'Name': 'Managed-CachingOptimized'}}},
    ('cloudfront', 'list-functions'): {'FunctionList': {'Items': [
        {'Name': 'ork3d-redirect-www', 'FunctionMetadata': {'Stage': 'LIVE'}}]}},
    ('cloudfront', 'list-origin-access-controls'): {'OriginAccessControlList': {'Items': []}},
    ('cloudfront', 'list-origin-request-policies'): {'OriginRequestPolicyList': {'Items': []}},
    ('s3api', 'get-bucket-versioning'): {'Status': 'Enabled'},
    ('s3api', 'get-bucket-encryption'): {'ServerSideEncryptionConfiguration': {'Rules': [
        {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]}},
    ('s3api', 'get-public-access-block'): {'PublicAccessBlockConfiguration': {
        'BlockPublicAcls': True, 'IgnorePublicAcls': True, 'BlockPublicPolicy': True, 'RestrictPublicBuckets': True}},
    ('s3api', 'get-bucket-lifecycle-configuration'): {'Rules': [
        {'ID': 'pendientes-vencen-a-los-2-dias', 'Filter': {'Prefix': 'pendientes/'}, 'Status': 'Enabled'}]},
    ('s3api', 'get-bucket-cors'): {'CORSRules': []},
    ('s3api', 'get-bucket-policy'): {'Policy': json.dumps(POLITICA_BUCKET)},
    ('s3api', 'list-objects-v2'): {'KeyCount': 1},
    ('s3api', 'head-object'): {'ContentLength': 10},
    ('logs', 'describe-log-groups'): {'logGroups': [{'logGroupName': '/aws/lambda/ork3d-resenas',
                                                    'retentionInDays': 30}]},
    ('sesv2', 'get-account'): {'SendingEnabled': True, 'ProductionAccessEnabled': False,
                               'EnforcementStatus': 'HEALTHY',
                               'SendQuota': {'Max24HourSend': 200.0, 'MaxSendRate': 1.0, 'SentLast24Hours': 0.0}},
    ('sesv2', 'list-email-identities'): {'EmailIdentities': [
        {'IdentityType': 'DOMAIN', 'IdentityName': 'ork3d.com', 'VerificationStatus': 'SUCCESS'},
        {'IdentityType': 'EMAIL_ADDRESS', 'IdentityName': 'ana.moderadora@example.com',
         'VerificationStatus': 'SUCCESS'}]},
    ('sesv2', 'get-email-identity'): {'DkimAttributes': {'Status': 'SUCCESS'}},
}


def posicionales(args):
    return [a for a in args if not a.startswith('--') and a not in ('json', 'text', 'ork3d.com')]


def main():
    servicio, operacion = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'{servicio} {operacion}\n')
    if (servicio, operacion) == ('sts', 'get-caller-identity'):
        if os.environ.get('FAKE_STS_FAIL') == '1':
            sys.stderr.write(f'An error occurred (InvalidClientTokenId) when calling the GetCallerIdentity '
                             f'operation: The security token included in the request is invalid. '
                             f'access_key={CLAVE_FALSA} secret={SECRETO_FALSO}\n')
            sys.exit(255)
        print(json.dumps({'Account': CUENTA_FALSA, 'Arn': f'arn:aws:iam::{CUENTA_FALSA}:user/operador',
                          'UserId': 'AIDAEXAMPLE'}))
        return
    if (servicio, operacion) == ('lambda', 'get-function-configuration') and '--function-name' in args \
            and args[args.index('--function-name') + 1] == 'ork3d-resenas-admin':
        sys.stderr.write('An error occurred (ResourceNotFoundException) when calling the GetFunctionConfiguration '
                         'operation: Function not found\n')
        sys.exit(255)
    if (servicio, operacion) == ('cloudfront', 'get-function'):
        destino = posicionales(args)[-1]
        with open(destino, 'w', encoding='utf-8') as f:
            f.write('function handler(event) { return event.request; }\n')
        print(json.dumps({'ContentType': 'application/javascript', 'ETag': 'E2FUNCDEMO'}))
        return
    if (servicio, operacion) == ('s3api', 'get-bucket-website'):
        sys.stderr.write('An error occurred (NoSuchWebsiteConfiguration) when calling the GetBucketWebsite '
                         'operation: The specified bucket does not have a website configuration\n')
        sys.exit(255)
    print(json.dumps(RESPUESTAS.get((servicio, operacion), {})))


if __name__ == '__main__':
    main()
