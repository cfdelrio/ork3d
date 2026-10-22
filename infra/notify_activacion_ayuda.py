#!/usr/bin/env python3
"""Ayuda de infra/notify-activacion.sh: aviso de reseña nueva (RESENAS_NOTIFY_ENABLED) en la Lambda pública.

Sin AWS ni red: solo lee archivos que el script ya descargó a una carpeta privada. Nunca imprime correos, tokens
ni valores de variables: solo nombres de variable, nombres de sentencia, conteos y categorías de error.

  estado-flag CONFIG                      imprime apagado | activo | desconocido (sale 1 si es desconocido)
  politica LIVE PLANTILLA CONFIG CUENTA   PASS/FAIL: el rol tiene exactas las dos sentencias del aviso
  codigo ZIP RAIZ                         PASS/FAIL/INFO: el código desplegado tiene el aviso y coincide con HEAD
  snapshot CONFIG SALIDA                  guarda el Environment actual para revertir (600)
  plan-activar CONFIG SALIDA              Environment completo con RESENAS_NOTIFY_ENABLED=true (600)
  plan-revertir SNAPSHOT SALIDA           Environment guardado, tal cual (600)
  comparar ANTES DESPUES MODO             PASS/FAIL: activar = solo cambió la flag; revertir = igual al guardado
  logs ARCHIVO                            conteos de eventos del aviso en los logs; sin texto de los mensajes
"""
import glob
import json
import os
import re
import sys
import zipfile
from collections import Counter

FLAG = 'RESENAS_NOTIFY_ENABLED'
REGION = 'us-east-1'
SID_SES = 'NotificarNuevaResenaAlModerador'
SID_AVISOS = 'GuardarAvisosDeNotificacion'
ARCHIVOS_AVISO = ('app.py', 'nucleo/correo.py')
CAMPOS_CONFIG = ('Runtime', 'Handler', 'MemorySize', 'Timeout', 'CodeSha256', 'Role')
EVENTOS_AVISO = ('resena_creada', 'aviso_enviado', 'aviso_fallido')
PATRON_CATEGORIA = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,80}')
PATRON_PLACEHOLDER = re.compile(r'__[A-Z_]+__')


def _cargar(ruta):
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def _bytes(ruta):
    with open(ruta, 'rb') as f:
        return f.read()


def _guardar(ruta, datos):
    with os.fdopen(os.open(ruta, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as f:
        json.dump(datos, f)


def _variables(doc):
    """Acepta la configuración de get-function-configuration o un snapshot {"Variables": {...}}."""
    if 'Environment' not in doc and 'Variables' in doc:
        return dict(doc['Variables'] or {})
    return dict(((doc.get('Environment') or {}).get('Variables')) or {})


def _lista(valor):
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


def _normal(sentencia):
    return {'Effect': sentencia.get('Effect'),
            'Action': sorted(_lista(sentencia.get('Action'))),
            'Resource': sorted(_lista(sentencia.get('Resource'))),
            'Condition': sentencia.get('Condition')}


def estado_flag(config):
    valor = _variables(_cargar(config)).get(FLAG)
    if valor is None or str(valor).lower() == 'false':
        return 'apagado'
    if str(valor).lower() == 'true':
        return 'activo'
    return 'desconocido'


def sentencias_esperadas(config, plantilla, cuenta):
    """Las sentencias de lambda-permisos.json renderizadas con la configuración real. None si faltan datos."""
    variables = _variables(_cargar(config))
    valores = {'RESENAS_BUCKET': variables.get('RESENAS_BUCKET'),
               'RESENAS_FROM_EMAIL': variables.get('RESENAS_FROM_EMAIL'),
               'RESENAS_ADMIN_EMAIL': variables.get('RESENAS_ADMIN_EMAIL'),
               'AWS_REGION': REGION, 'ACCOUNT_ID': cuenta}
    if not all(valores.values()):
        return None
    with open(plantilla, encoding='utf-8') as f:
        texto = f.read()
    for clave, valor in valores.items():
        texto = texto.replace(f'__{clave}__', valor)
    if PATRON_PLACEHOLDER.search(texto):
        return {}
    return {s['Sid']: s for s in json.loads(texto)['Statement'] if 'Sid' in s}


def politica(live, plantilla, config, cuenta):
    esperadas = sentencias_esperadas(config, plantilla, cuenta)
    if esperadas is None:
        return ['FAIL: faltan RESENAS_BUCKET o los correos en la configuración de la Lambda pública']
    if SID_SES not in esperadas or SID_AVISOS not in esperadas:
        return ['FAIL: la plantilla lambda-permisos.json no trae las sentencias del aviso']
    documento = _cargar(live)
    documento = documento.get('PolicyDocument', documento)   # salida de get-role-policy
    vivas = {s.get('Sid'): s for s in _lista(documento.get('Statement'))}
    faltan = [sid for sid in (SID_SES, SID_AVISOS)
              if sid not in vivas or _normal(vivas[sid]) != _normal(esperadas[sid])]
    if faltan:
        return [f"FAIL: el rol no tiene exactas las sentencias del aviso: {', '.join(faltan)}"]
    return [f'PASS: el rol tiene las sentencias del aviso ({SID_SES}, {SID_AVISOS})']


def codigo(zip_path, raiz):
    lineas = []
    repo = {'app.py': os.path.join(raiz, 'api', 'resenas', 'app.py')}
    for ruta in sorted(glob.glob(os.path.join(raiz, 'api', 'resenas', 'nucleo', '*.py'))):
        repo['nucleo/' + os.path.basename(ruta)] = ruta
    with zipfile.ZipFile(zip_path) as z:
        desplegado = {n: z.read(n) for n in z.namelist() if n.endswith('.py')}
    texto_app = desplegado.get('app.py', b'').decode('utf-8', 'replace')
    texto_correo = desplegado.get('nucleo/correo.py', b'').decode('utf-8', 'replace')
    tiene_aviso = ('self._notificar(review)' in texto_app and 'def _notificar' in texto_app
                   and FLAG in texto_app and 'def notificar_resena' in texto_correo
                   and 'aviso_fallido' in texto_correo)
    if not tiene_aviso:
        return ['FAIL: el código desplegado no trae el aviso: hace falta desplegar la Lambda pública antes de activar']
    for nombre in ARCHIVOS_AVISO:
        ruta = repo.get(nombre)
        if nombre not in desplegado or ruta is None:
            lineas.append(f'FAIL: {nombre} no está en el paquete desplegado')
        elif _bytes(ruta) != desplegado[nombre]:
            lineas.append(f'FAIL: {nombre} difiere de HEAD: desplegar HEAD antes de activar')
    if any(l.startswith('FAIL') for l in lineas):
        return lineas
    lineas.append('PASS: el código desplegado trae el aviso y app.py y nucleo/correo.py son idénticos a HEAD')
    for nombre in sorted(repo):
        if nombre in ARCHIVOS_AVISO:
            continue
        if nombre not in desplegado:
            lineas.append(f'INFO: {nombre} no está en el paquete desplegado')
        elif _bytes(repo[nombre]) != desplegado[nombre]:
            lineas.append(f'INFO: {nombre} difiere de HEAD (no bloquea el aviso)')
    for nombre in sorted(set(desplegado) - set(repo)):
        lineas.append(f'INFO: {nombre} solo está en el paquete desplegado')
    return lineas


def plan_activar(config, salida):
    variables = _variables(_cargar(config))
    variables[FLAG] = 'true'
    _guardar(salida, {'Variables': variables})


def snapshot(config, salida):
    _guardar(salida, {'Variables': _variables(_cargar(config))})


def plan_revertir(snap, salida):
    _guardar(salida, {'Variables': _variables(_cargar(snap))})


def comparar(antes, despues, modo):
    cfg_antes, cfg_despues = _cargar(antes), _cargar(despues)
    vars_antes, vars_despues = _variables(cfg_antes), _variables(cfg_despues)
    if modo == 'activar':
        lineas = []
        if cfg_despues.get('LastUpdateStatus') != 'Successful':
            lineas.append(f"FAIL: LastUpdateStatus={cfg_despues.get('LastUpdateStatus')}, se esperaba Successful")
        if vars_despues.get(FLAG) != 'true':
            lineas.append('FAIL: RESENAS_NOTIFY_ENABLED no quedó en true')
        cambiados = [c for c in CAMPOS_CONFIG if cfg_antes.get(c) != cfg_despues.get(c)]
        if cambiados:
            lineas.append(f"FAIL: cambió la configuración: {', '.join(cambiados)}")
        otras = sorted(k for k in set(vars_antes) | set(vars_despues)
                       if k != FLAG and vars_antes.get(k) != vars_despues.get(k))
        if otras:
            lineas.append(f"FAIL: cambiaron variables distintas de la flag: {', '.join(otras)}")
        if not lineas:
            lineas.append(f'PASS: solo cambió RESENAS_NOTIFY_ENABLED a true; las {len(vars_despues) - 1} '
                          'variables restantes y la configuración quedan iguales')
        return lineas
    if modo == 'revertir':
        if vars_antes == vars_despues:
            return ['PASS: las variables quedaron exactamente como antes de activar']
        distintas = sorted(k for k in set(vars_antes) | set(vars_despues)
                           if vars_antes.get(k) != vars_despues.get(k))
        return [f"FAIL: no coinciden con lo guardado: {', '.join(distintas)}"]
    raise SystemExit(f'modo desconocido: {modo}')


def _registro(mensaje):
    inicio, fin = mensaje.find('{'), mensaje.rfind('}')
    if inicio < 0 or fin < inicio:
        return None
    try:
        registro = json.loads(mensaje[inicio:fin + 1])
    except ValueError:
        return None
    return registro if isinstance(registro, dict) else None


def logs(ruta):
    datos = _cargar(ruta)
    eventos = datos.get('events', []) if isinstance(datos, dict) else None
    if not isinstance(eventos, list):
        return ['FAIL: la salida de los logs no tiene la lista events']
    conteo, categorias = Counter(), Counter()
    for evento in eventos:
        mensaje = evento.get('message') if isinstance(evento, dict) else None
        registro = _registro(mensaje) if isinstance(mensaje, str) else None
        if not registro or registro.get('ev') not in EVENTOS_AVISO:
            continue
        conteo[registro['ev']] += 1
        if registro['ev'] == 'aviso_fallido':
            error = registro.get('error')
            categorias[error if isinstance(error, str) and PATRON_CATEGORIA.fullmatch(error) else 'otro'] += 1
    lineas = [f'INFO: eventos en la ventana: {len(eventos)}']
    lineas += [f'INFO: {nombre}: {conteo[nombre]}' for nombre in EVENTOS_AVISO]
    lineas += [f'INFO: aviso_fallido por error {cat}: {n}' for cat, n in sorted(categorias.items())]
    return lineas


def main(argv):
    orden, args = argv[0], argv[1:]
    try:
        if orden == 'estado-flag':
            estado = estado_flag(args[0])
            print(estado)
            return 0 if estado != 'desconocido' else 1
        if orden == 'snapshot':
            snapshot(*args)
            return 0
        if orden == 'plan-activar':
            plan_activar(*args)
            return 0
        if orden == 'plan-revertir':
            plan_revertir(*args)
            return 0
        if orden == 'politica':
            lineas = politica(*args)
        elif orden == 'codigo':
            lineas = codigo(*args)
        elif orden == 'comparar':
            lineas = comparar(*args)
        elif orden == 'logs':
            lineas = logs(*args)
        else:
            raise SystemExit(f'orden desconocida: {orden}')
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f'FAIL: no se pudo leer o procesar ({type(exc).__name__})')
        return 1
    for linea in lineas:
        print(linea)
    return 1 if any(l.startswith('FAIL') for l in lineas) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
