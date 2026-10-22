#!/usr/bin/env python3
"""Simulador de la AWS CLI para probar infra/smoke-ses.sh. Sin red, sin credenciales y sin envíos reales.

Registra cada llamada (servicio y operación). Cualquier operación sin respuesta propia devuelve {} y también
queda registrada: así las pruebas pueden comprobar que el smoke no cambia configuración.
Variables de entorno:
  FAKE_LOG                    registro de llamadas
  FAKE_IDENTIDADES            JSON con las identidades SES: [{IdentityType, IdentityName, VerificationStatus,
                              VerifiedForSendingStatus, DkimStatus}]. Por defecto: dos EMAIL_ADDRESS y un DOMAIN.
  FAKE_ENVIO_COPIA            archivo donde se copia la petición de ses send-email (para revisar su contenido)
  FAKE_STS_FAIL=1             la autenticación falla
  FAKE_ENVIO_DESHABILITADO=1  SendingEnabled=false en la cuenta
  FAKE_PRODUCCION=1           ProductionAccessEnabled=true (la cuenta ya salió del sandbox)
  FAKE_SEND_FAIL=1            send-email falla con MessageRejected
"""
import json
import os
import shutil
import sys
import uuid

IDENTIDADES_POR_DEFECTO = [
    {'IdentityType': 'EMAIL_ADDRESS', 'IdentityName': 'remitente-prueba@ejemplo.test',
     'VerificationStatus': 'SUCCESS', 'VerifiedForSendingStatus': True},
    {'IdentityType': 'EMAIL_ADDRESS', 'IdentityName': 'moderacion-prueba@ejemplo.test',
     'VerificationStatus': 'SUCCESS', 'VerifiedForSendingStatus': True},
    {'IdentityType': 'DOMAIN', 'IdentityName': 'ejemplo.test',
     'VerificationStatus': 'SUCCESS', 'VerifiedForSendingStatus': True, 'DkimStatus': 'SUCCESS'},
]


def registrar(servicio, operacion):
    if os.environ.get('FAKE_LOG'):
        with open(os.environ['FAKE_LOG'], 'a', encoding='utf-8') as f:
            f.write(f'{servicio} {operacion}\n')


def opcion(args, nombre):
    return args[args.index(nombre) + 1] if nombre in args else None


def fallo(operacion, codigo, mensaje):
    sys.stderr.write(f'An error occurred ({codigo}) when calling the {operacion} operation: {mensaje}\n')
    sys.exit(255)


def identidades():
    raw = os.environ.get('FAKE_IDENTIDADES')
    return json.loads(raw) if raw else IDENTIDADES_POR_DEFECTO


def main():
    servicio, operacion = sys.argv[1], sys.argv[2]
    args = sys.argv[3:]
    registrar(servicio, operacion)

    if (servicio, operacion) == ('sts', 'get-caller-identity'):
        if os.environ.get('FAKE_STS_FAIL') == '1':
            fallo('GetCallerIdentity', 'InvalidClientTokenId', 'The security token included in the request is invalid.')
        cuenta = '9' * 12  # cuenta falsa, construida para que el escaneo del repo no la marque
        print(json.dumps({'Account': cuenta, 'Arn': f'arn:aws:iam::{cuenta}:user/operador'}))
        return

    if (servicio, operacion) == ('sesv2', 'get-account'):
        print(json.dumps({'SendingEnabled': os.environ.get('FAKE_ENVIO_DESHABILITADO') != '1',
                          'ProductionAccessEnabled': os.environ.get('FAKE_PRODUCCION') == '1',
                          'EnforcementStatus': 'HEALTHY'}))
        return

    if (servicio, operacion) == ('sesv2', 'get-email-identity'):
        nombre = (opcion(args, '--email-identity') or '').lower()
        for i in identidades():
            if i['IdentityName'].lower() == nombre:
                print(json.dumps({'IdentityType': i['IdentityType'],
                                  'VerificationStatus': i['VerificationStatus'],
                                  'VerifiedForSendingStatus': i['VerifiedForSendingStatus'],
                                  'DkimAttributes': {'Status': i.get('DkimStatus', 'NOT_STARTED')}}))
                return
        fallo('GetEmailIdentity', 'NotFoundException', 'Email identity does not exist.')

    if (servicio, operacion) == ('sesv2', 'list-email-identities'):
        print(json.dumps({'EmailIdentities': [
            {'IdentityType': i['IdentityType'], 'IdentityName': i['IdentityName'],
             'VerificationStatus': i['VerificationStatus']} for i in identidades()]}))
        return

    if (servicio, operacion) == ('ses', 'send-email'):
        if os.environ.get('FAKE_SEND_FAIL') == '1':
            fallo('SendEmail', 'MessageRejected', 'Email address is not verified.')
        ruta = (opcion(args, '--cli-input-json') or '').replace('file://', '', 1)
        if os.environ.get('FAKE_ENVIO_COPIA') and ruta:
            shutil.copyfile(ruta, os.environ['FAKE_ENVIO_COPIA'])
        print(json.dumps({'MessageId': uuid.uuid4().hex}))
        return

    print('{}')


if __name__ == '__main__':
    main()
