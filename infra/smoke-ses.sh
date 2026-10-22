#!/usr/bin/env bash
# Smoke de SES para V2: envía UN correo de prueba a la moderación desde una identidad verificada.
#
# Requiere ALLOW_SES_SEND=1. Sin esa variable aborta antes de llamar a AWS (exit 3).
# Variables obligatorias: RESENAS_FROM_EMAIL (remitente) y RESENAS_ADMIN_EMAIL (destinatario de moderación).
# Ambas deben ser identidades EMAIL_ADDRESS verificadas y habilitadas para enviar en AWS_REGION (us-east-1 por
# defecto). Remitente: la política ses:SendEmail de la Lambda usa identity/<RESENAS_FROM_EMAIL>. Destinatario:
# en el sandbox de SES solo se escribe a direcciones verificadas. Si alguna no lo es, el smoke se niega.
#
# Llamadas a AWS: sts get-caller-identity, sesv2 get-account, sesv2 get-email-identity (dos veces) y
# ses send-email (una sola vez, sin reintentos). No cambia la configuración de SES ni escribe en S3.
# Las direcciones se muestran enmascaradas. El MessageId se muestra truncado.
#
# Códigos de salida: 0 enviado · 1 una comprobación o el envío falló · 2 falta o es inválida una variable ·
#                    3 falta ALLOW_SES_SEND=1
set -uo pipefail

if [[ "${ALLOW_SES_SEND:-}" != "1" ]]; then
  echo "ABORTA: este smoke envía un correo real. Para ejecutarlo: ALLOW_SES_SEND=1 RESENAS_FROM_EMAIL=... RESENAS_ADMIN_EMAIL=... infra/smoke-ses.sh" >&2
  exit 3
fi

FROM="${RESENAS_FROM_EMAIL:-}"
ADMIN="${RESENAS_ADMIN_EMAIL:-}"
PATRON_CORREO='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'
if [[ -z "$FROM" ]]; then echo "ERROR: falta RESENAS_FROM_EMAIL (remitente verificado en SES)." >&2; exit 2; fi
if [[ -z "$ADMIN" ]]; then echo "ERROR: falta RESENAS_ADMIN_EMAIL (destinatario de moderación)." >&2; exit 2; fi
if [[ ! "$FROM" =~ $PATRON_CORREO ]]; then echo "ERROR: RESENAS_FROM_EMAIL no tiene formato de correo." >&2; exit 2; fi
if [[ ! "$ADMIN" =~ $PATRON_CORREO ]]; then echo "ERROR: RESENAS_ADMIN_EMAIL no tiene formato de correo." >&2; exit 2; fi

AWS="${AWS_CLI:-aws}"
export AWS_PROFILE="${AWS_PROFILE:-default}" AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""
T="$(mktemp -d)"
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT

# Ayudas: solo leen archivos de la CLI o generan la petición. Nunca imprimen una dirección completa.
cat > "$T/ayuda.py" <<'PY'
import json, os, sys

SUBJECT = 'ORK3D · prueba técnica SES V2'
BODY = 'Prueba técnica del sistema de reseñas ORK3D. No requiere ninguna acción.'


def enmascarar(correo):
    local, _, dominio = correo.partition('@')
    return local[:1] + '***@' + dominio[:1] + '***.' + dominio.rsplit('.', 1)[-1]


def cuenta(ruta):
    d = json.load(open(ruta, encoding='utf-8'))
    envio = bool(d.get('SendingEnabled'))
    sandbox = not d.get('ProductionAccessEnabled')
    print(f"SES: envío habilitado={'sí' if envio else 'no'} · sandbox={'sí' if sandbox else 'no'}")
    return 0 if envio else 1


def identidad(ruta, correo, rol):
    d = json.load(open(ruta, encoding='utf-8'))
    problemas = []
    if d.get('IdentityType') != 'EMAIL_ADDRESS':
        problemas.append(f"es {d.get('IdentityType')}, se esperaba EMAIL_ADDRESS")
    if d.get('VerificationStatus') != 'SUCCESS':
        problemas.append(f"verificación {d.get('VerificationStatus')}")
    if d.get('VerifiedForSendingStatus') is not True:
        problemas.append('no está habilitada para enviar')
    if problemas:
        print(f"{rol}: no utilizable: " + '; '.join(problemas))
        return 1
    print(f"{rol}: {enmascarar(correo)} · EMAIL_ADDRESS verificada y habilitada para enviar")
    return 0


def envio(origen, destino, salida):
    pedido = {'Source': origen, 'Destination': {'ToAddresses': [destino]},
              'Message': {'Subject': {'Data': SUBJECT, 'Charset': 'UTF-8'},
                          'Body': {'Text': {'Data': BODY, 'Charset': 'UTF-8'}}}}
    with open(salida, 'w', encoding='utf-8') as f:
        json.dump(pedido, f, ensure_ascii=False)
    os.chmod(salida, 0o600)
    return 0


def truncado(ruta):
    mid = json.load(open(ruta, encoding='utf-8')).get('MessageId') or ''
    if not mid:
        print('MessageId: no recibido')
        return 1
    print(f'MessageId: {mid[:8]}… (truncado)')
    return 0


if __name__ == '__main__':
    orden, *args = sys.argv[1:]
    funciones = {'cuenta': cuenta, 'identidad': identidad, 'envio': envio, 'truncado': truncado}
    sys.exit(funciones[orden](*args))
PY

codigo() {
  python3 -c 'import re, sys; m = re.search(r"An error occurred \((\w+)\)", open(sys.argv[1], encoding="utf-8", errors="replace").read()); print(m.group(1) if m else "desconocido")' "$1"
}

verificar() {  # verificar CORREO ROL: identidad EMAIL_ADDRESS verificada y habilitada para enviar
  local correo="$1" rol="$2"
  if ! "$AWS" sesv2 get-email-identity --email-identity "$correo" --output json > "$T/$rol.json" 2> "$T/$rol.err"; then
    echo "$rol: no es una identidad EMAIL_ADDRESS de SES en $AWS_REGION (código $(codigo "$T/$rol.err"))." >&2
    return 1
  fi
  python3 "$T/ayuda.py" identidad "$T/$rol.json" "$correo" "$rol"
}

echo "SES smoke · región=$AWS_REGION · perfil=$AWS_PROFILE"

# 1. Autenticación (solo lectura).
if ! "$AWS" sts get-caller-identity --output json > "$T/id.json" 2> "$T/id.err"; then
  echo "ERROR: no se pudo autenticar con el perfil $AWS_PROFILE (código $(codigo "$T/id.err"))." >&2
  exit 1
fi

# 2. Cuenta SES: envío habilitado y modo sandbox (solo lectura).
if ! "$AWS" sesv2 get-account --output json > "$T/cuenta.json" 2> "$T/cuenta.err"; then
  echo "ERROR: no se pudo leer la cuenta SES (código $(codigo "$T/cuenta.err"))." >&2
  exit 1
fi
if ! python3 "$T/ayuda.py" cuenta "$T/cuenta.json"; then
  echo "ERROR: el envío de SES está deshabilitado en la cuenta. No se envía nada." >&2
  exit 1
fi

# 3. Remitente y destinatario: identidades EMAIL_ADDRESS verificadas (solo lectura).
verificar "$FROM" remitente || exit 1
verificar "$ADMIN" destinatario || exit 1

# 4. Un único envío, sin reintentos.
python3 "$T/ayuda.py" envio "$FROM" "$ADMIN" "$T/envio.json"
if ! "$AWS" ses send-email --cli-input-json "file://$T/envio.json" --output json > "$T/envio-salida.json" 2> "$T/envio.err"; then
  echo "ERROR: el envío falló (código $(codigo "$T/envio.err")). No se reintenta: un solo envío por ejecución." >&2
  exit 1
fi
echo "Enviado: un correo de prueba a la moderación."
python3 "$T/ayuda.py" truncado "$T/envio-salida.json"
exit 0
