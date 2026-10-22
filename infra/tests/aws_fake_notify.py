#!/usr/bin/env python3
"""Simulador con estado de la AWS CLI para probar infra/notify-activacion.sh. Sin red, sin credenciales, sin AWS real.

El estado vive en FAKE_ESTADO (JSON): publica (configuración de ork3d-resenas, con RevisionId), politica (documento
del rol), identidades (correo → estado SES), cuenta (SendingEnabled), logs (mensajes) y objetos (claves del bucket).
Cada llamada se registra en FAKE_LOG como "servicio operación". Lo que no se simula falla con UnsupportedOperation,
así una llamada nueva del script se ve en las pruebas.

Variables de entorno:
  FAKE_ESTADO                   archivo de estado (obligatorio)
  FAKE_LOG                      registro de llamadas
  FAKE_NOTIFY_FALLA_UPDATE=1    update-function-configuration falla sin cambiar nada
  FAKE_NOTIFY_MUEVE_REVISION=1  otra escritura se adelanta: la revisión cambia antes de la escritura (se rechaza)
La descarga del código la sirve aws_fake_curl.py con FAKE_CODIGO_ANTERIOR_ARCHIVO (URL fake-code.example).
"""
import json
import os
import sys
import uuid

FN = 'ork3d-resenas'
BOOLEANAS = {'only-show-errors'}
ESCRITURAS = ('update-', 'put-', 'create-', 'delete-', 'add-', 'remove-', 'publish', 'invoke', 'tag-')


class Fallo(Exception):
    def __init__(self, codigo, msg, op):
        super().__init__(msg)
        self.codigo, self.msg, self.op = codigo, msg, op


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


def despachar(svc, op, o, st):
    publica = st['publica']
    if (svc, op) == ('lambda', 'get-function-configuration'):
        if o.get('function-name') != FN:
            raise Fallo('ResourceNotFoundException', 'Function not found', op)
        return publica['config']
    if (svc, op) == ('lambda', 'get-function'):
        return {'Configuration': publica['config'],
                'Code': {'Location': 'https://fake-code.example/ork3d-resenas.zip'}}
    if (svc, op) == ('lambda', 'update-function-configuration'):
        if os.environ.get('FAKE_NOTIFY_MUEVE_REVISION') == '1':
            publica['config']['RevisionId'] = str(uuid.uuid4())   # otra escritura se adelantó
        if o.get('revision-id') != publica['config']['RevisionId']:
            raise Fallo('PreconditionFailedException',
                        'The Revision Id provided does not match the latest Revision Id.', op)
        if os.environ.get('FAKE_NOTIFY_FALLA_UPDATE') == '1':
            raise Fallo('InvalidParameterValueException', 'falla simulada de la escritura', op)
        with open(o['environment'][len('file://'):], encoding='utf-8') as f:
            env = json.load(f)
        publica['config']['Environment'] = {'Variables': dict(env['Variables'])}
        publica['config']['RevisionId'] = str(uuid.uuid4())
        return {'FunctionName': FN, 'RevisionId': publica['config']['RevisionId']}
    if (svc, op) == ('lambda', 'wait:function-updated-v2'):
        return None
    if (svc, op) == ('iam', 'get-role-policy'):
        if o.get('policy-name') != st['politica_nombre']:
            raise Fallo('NoSuchEntityException', 'The role policy with name X cannot be found.', op)
        return {'RoleName': 'ork3d-resenas-lambda', 'PolicyName': o['policy-name'],
                'PolicyDocument': st['politica']}
    if (svc, op) == ('sesv2', 'get-email-identity'):
        identidad = st['identidades'].get(o.get('email-identity'))
        if identidad is None:
            raise Fallo('NotFoundException', 'Email identity not found', op)
        return identidad
    if (svc, op) == ('sesv2', 'get-account'):
        return {'SendingEnabled': st['cuenta']['SendingEnabled']}
    if (svc, op) == ('logs', 'filter-log-events'):
        return {'events': [{'timestamp': 1, 'message': m, 'eventId': str(i)} for i, m in enumerate(st['logs'])]}
    if (svc, op) == ('s3api', 'head-object'):
        if o.get('key') in st['objetos']:
            return {'ContentLength': 10}
        raise Fallo('404', 'Not Found', op)
    raise Fallo('UnsupportedOperation', f'{svc} {op} no está simulada', op)


def main():
    svc, op = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    if op == 'wait':
        op = 'wait:' + args[0]
        args = args[1:]
    o, _ = opciones(args)
    ruta = os.environ['FAKE_ESTADO']
    with open(ruta, encoding='utf-8') as f:
        st = json.load(f)
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'{svc} {op}\n')
    try:
        resultado = despachar(svc, op, o, st)
    except Fallo as exc:
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(st, f)
        nombre_api = ''.join(p.capitalize() for p in op.replace('wait:', '').split('-'))  # como la CLI real
        sys.stderr.write(f'An error occurred ({exc.codigo}) when calling the {nombre_api} operation: {exc.msg}\n')
        sys.exit(255)
    with open(ruta, 'w', encoding='utf-8') as f:
        json.dump(st, f)
    if resultado is not None:
        print(json.dumps(resultado))


if __name__ == '__main__':
    main()
