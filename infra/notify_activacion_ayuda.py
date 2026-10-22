#!/usr/bin/env python3
"""Ayuda del aviso de reseña nueva (RESENAS_NOTIFY_ENABLED) en la Lambda pública. Sin AWS ni red.

Solo lee archivos que los scripts ya descargaron a una carpeta privada. Nunca imprime correos completos, tokens ni
valores de variables: solo nombres de variable, nombres de sentencia, conteos y forma redactada de los correos.

Activación (infra/notify-activacion.sh):
  estado-flag CONFIG                      apagado | activo | desconocido (sale 1 si es desconocido)
  politica LIVE PLANTILLA CONFIG CUENTA   PASS/FAIL: el rol tiene exactas las dos sentencias del aviso
  codigo ZIP RAIZ                         PASS/FAIL/INFO: el código desplegado es el del aviso, módulo por módulo
  snapshot CONFIG SALIDA                  guarda el Environment actual para revertir (600)
  plan-activar CONFIG SALIDA              Environment completo con RESENAS_NOTIFY_ENABLED=true (600)
  plan-revertir SNAPSHOT SALIDA           Environment guardado, tal cual (600)
  comparar ANTES DESPUES MODO             PASS/FAIL: activar = solo cambió la flag; revertir = igual al guardado
  logs ARCHIVO                            conteos de eventos del aviso en los logs; sin texto de los mensajes

Despliegue de la Lambda pública (infra/notify-public-code-deploy.sh, infra/notify-public-iam.sh):
  zip-aviso RAIZ SALIDA                   paquete mínimo y determinista: app.py + el cierre de imports (imprime SHA)
  comparar-paquetes DESPLEGADO PROPUESTO RAIZ    qué archivos cambian y por qué
  env-nuevo CONFIG ADMIN SALIDA           variables actuales + las nuevas; nunca reemplaza el mapa
  muestra-sentencias PLANTILLA            las sentencias del aviso con placeholders (para revisar el diff de IAM)
  politica-fusion LIVE PLANTILLA BUCKET ORIGEN DESTINO CUENTA SALIDA
                                          agrega solo las sentencias que faltan; nunca quita ni cambia las demás
  normalizar-opcional CRUDO SALIDA        stdout de un recurso opcional: vacío = ausente; JSON válido; otra cosa = FAIL
  instantanea CONFIG ZIP URL RECURSO CONCURRENCIA CLOUDFRONT EXPORT_SHA CARPETA   foto de lo que debe quedar igual
  verificar-publico ANTES DESPUES CODESHA ADMIN   PASS/FAIL tras la Fase A (flag false, env preservado, resto igual)
  comparar-restauracion ANTES DESPUES     PASS/FAIL tras revertir: configuración y código como antes
  comparar-politica ANTES DESPUES         PASS/FAIL: toda sentencia anterior del rol sigue igual
  config-iam CONFIG ADMIN SALIDA         configuración para verificar la política (bucket y correos del admin)
"""
import ast
import base64
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from collections import Counter

FLAG = 'RESENAS_NOTIFY_ENABLED'
REGION = 'us-east-1'
SID_SES = 'NotificarNuevaResenaAlModerador'
SID_AVISOS = 'GuardarAvisosDeNotificacion'
# Cierre de imports de app.py en la Lambda pública (lo verifica test_notify_activacion: si cambian los imports, falla).
MODULOS_AVISO = ('app.py', 'nucleo/__init__.py', 'nucleo/almacen.py', 'nucleo/correo.py', 'nucleo/errores.py',
                 'nucleo/jpeg.py', 'nucleo/registro.py', 'nucleo/tokens.py')
VARIABLES_PUBLICAS_NUEVAS = ('RESENAS_FROM_EMAIL', 'RESENAS_ADMIN_EMAIL', 'RESENAS_ADMIN_BASE_URL')
CORREOS = ('RESENAS_FROM_EMAIL', 'RESENAS_ADMIN_EMAIL')
CAMPOS_CONFIG = ('Runtime', 'Handler', 'MemorySize', 'Timeout', 'Role')
EVENTOS_AVISO = ('resena_creada', 'aviso_enviado', 'aviso_fallido')
PATRON_CATEGORIA = re.compile(r'[A-Za-z][A-Za-z0-9_.]{0,80}')
PATRON_PLACEHOLDER = re.compile(r'__[A-Z_]+__')
PATRON_CORREO = re.compile(r'[^@\s]+@[^@\s]+\.[^@\s]+')


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


def redactar(correo):
    """c***@g***.com: primera letra del usuario y del dominio, y el dominio de nivel superior."""
    local, _, dominio = correo.partition('@')
    if not dominio:
        return '(sin formato)'
    return local[:1] + '***@' + dominio[:1] + '***.' + dominio.rsplit('.', 1)[-1]


def _renderizar(plantilla, valores):
    """Sentencias de lambda-permisos.json con los valores dados. {} si queda algún placeholder sin valor."""
    with open(plantilla, encoding='utf-8') as f:
        texto = f.read()
    for clave, valor in valores.items():
        texto = texto.replace(f'__{clave}__', valor)
    if PATRON_PLACEHOLDER.search(texto):
        return {}
    return {s['Sid']: s for s in json.loads(texto)['Statement'] if 'Sid' in s}


# ── Activación ────────────────────────────────────────────────────────────────

def estado_flag(config):
    valor = _variables(_cargar(config)).get(FLAG)
    if valor is None or str(valor).lower() == 'false':
        return 'apagado'
    if str(valor).lower() == 'true':
        return 'activo'
    return 'desconocido'


def sentencias_esperadas(config, plantilla, cuenta):
    """Las sentencias del aviso con la configuración real de la Lambda pública. None si faltan datos."""
    variables = _variables(_cargar(config))
    valores = {'RESENAS_BUCKET': variables.get('RESENAS_BUCKET'),
               'RESENAS_FROM_EMAIL': variables.get('RESENAS_FROM_EMAIL'),
               'RESENAS_ADMIN_EMAIL': variables.get('RESENAS_ADMIN_EMAIL'),
               'AWS_REGION': REGION, 'ACCOUNT_ID': cuenta}
    if not all(valores.values()):
        return None
    return _renderizar(plantilla, valores)


def politica(live, plantilla, config, cuenta):
    esperadas = sentencias_esperadas(config, plantilla, cuenta)
    if esperadas is None:
        return ['FAIL: faltan RESENAS_BUCKET o los correos en la configuración de la Lambda pública']
    if SID_SES not in esperadas or SID_AVISOS not in esperadas:
        return ['FAIL: la plantilla lambda-permisos.json no trae las sentencias del aviso']
    documento = _cargar(live)
    documento = documento.get('PolicyDocument', documento)
    vivas = {s.get('Sid'): s for s in _lista(documento.get('Statement'))}
    faltan = [sid for sid in (SID_SES, SID_AVISOS)
              if sid not in vivas or _normal(vivas[sid]) != _normal(esperadas[sid])]
    if faltan:
        return [f"FAIL: el rol no tiene exactas las sentencias del aviso: {', '.join(faltan)}"]
    return [f'PASS: el rol tiene las sentencias del aviso ({SID_SES}, {SID_AVISOS})']


def codigo(zip_path, raiz):
    """Compara el paquete desplegado con HEAD módulo por módulo. Los módulos del cierre deben ser idénticos."""
    lineas = []
    with zipfile.ZipFile(zip_path) as z:
        desplegado = {n: z.read(n) for n in z.namelist() if n.endswith('.py')}
    texto_app = desplegado.get('app.py', b'').decode('utf-8', 'replace')
    texto_correo = desplegado.get('nucleo/correo.py', b'').decode('utf-8', 'replace')
    tiene_aviso = ('self._notificar(review)' in texto_app and 'def _notificar' in texto_app
                   and FLAG in texto_app and 'def notificar_resena' in texto_correo
                   and 'aviso_fallido' in texto_correo)
    if not tiene_aviso:
        return ['FAIL: el código desplegado no trae el aviso: hace falta desplegar la Lambda pública antes de activar']
    for nombre in MODULOS_AVISO:
        ruta = os.path.join(raiz, 'api', 'resenas', *nombre.split('/'))
        if nombre not in desplegado:
            lineas.append(f'FAIL: {nombre} no está en el paquete desplegado')
        elif _bytes(ruta) != desplegado[nombre]:
            lineas.append(f'FAIL: {nombre} difiere de HEAD: desplegar HEAD antes de activar')
    if any(l.startswith('FAIL') for l in lineas):
        return lineas
    lineas.append('PASS: el código desplegado trae el aviso y cada módulo de su cierre es idéntico a HEAD')
    for nombre in sorted(set(desplegado) - set(MODULOS_AVISO)):
        lineas.append(f'INFO: {nombre} está en el paquete pero el aviso no lo importa')
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
        cambiados = [c for c in CAMPOS_CONFIG + ('CodeSha256',) if cfg_antes.get(c) != cfg_despues.get(c)]
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
        distintas = sorted(k for k in set(vars_antes) | set(vars_despues) if vars_antes.get(k) != vars_despues.get(k))
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


# ── Despliegue de la Lambda pública ───────────────────────────────────────────

def cierre_de_imports(raiz):
    """Módulos de nucleo/ que app.py importa, directa o indirectamente. Se calcula desde el código."""
    def importados(ruta):
        salida = set()
        with open(ruta, encoding='utf-8') as f:
            arbol = ast.parse(f.read())
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.ImportFrom):
                if nodo.module is None and nodo.level == 1:
                    salida.update(a.name for a in nodo.names)
                elif nodo.module == 'nucleo' and nodo.level == 0:
                    salida.update(a.name for a in nodo.names)
                elif nodo.module and nodo.level == 0 and nodo.module.startswith('nucleo.'):
                    salida.add(nodo.module.split('.')[1])
                elif nodo.module and nodo.level == 1:
                    salida.add(nodo.module)
            elif isinstance(nodo, ast.Import):
                salida.update(a.name.split('.')[1] for a in nodo.names if a.name.startswith('nucleo.'))
        return salida

    def ruta(modulo):
        base = os.path.join(raiz, 'api', 'resenas')
        return os.path.join(base, 'app.py') if modulo == 'app' else os.path.join(base, 'nucleo', modulo + '.py')

    cierre, pendientes = set(), ['app']
    while pendientes:
        modulo = pendientes.pop()
        if modulo in cierre:
            continue
        cierre.add(modulo)
        pendientes.extend(d for d in importados(ruta(modulo)) if os.path.exists(ruta(d)))
    return cierre


def zip_aviso(raiz, salida):
    """Paquete mínimo: MODULOS_AVISO con fechas fijas (mismo contenido, mismo SHA). Imprime el CodeSha256."""
    with zipfile.ZipFile(salida, 'w', zipfile.ZIP_DEFLATED) as z:
        for nombre in MODULOS_AVISO:
            info = zipfile.ZipInfo(nombre, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, _bytes(os.path.join(raiz, 'api', 'resenas', *nombre.split('/'))),
                       compress_type=zipfile.ZIP_DEFLATED)
    print(base64.b64encode(hashlib.sha256(_bytes(salida)).digest()).decode('ascii'))
    return 0


def _motivo(nombre):
    if nombre in MODULOS_AVISO:
        return 'necesario: lo importa app.py o el cierre de imports del aviso'
    return 'no lo importa el aviso: no se incluye'


def comparar_paquetes(desplegado, propuesto):
    """Lista exacta de archivos que cambian entre el paquete desplegado y el propuesto."""
    def contenidos(ruta):
        with zipfile.ZipFile(ruta) as z:
            return {n: z.read(n) for n in z.namelist() if n.endswith('.py')}
    viejo, nuevo = contenidos(desplegado), contenidos(propuesto)
    lineas, cuenta = [], Counter()
    for nombre in sorted(set(viejo) | set(nuevo)):
        if nombre not in viejo:
            cuenta['nuevos'] += 1
            lineas.append(f'INFO: NUEVO   {nombre}  ({_motivo(nombre)})')
        elif nombre not in nuevo:
            cuenta['se_quitan'] += 1
            lineas.append(f'INFO: SOBRA   {nombre}  (el paquete propuesto no lo incluye: app.py no lo importa)')
        elif viejo[nombre] != nuevo[nombre]:
            cuenta['cambian'] += 1
            lineas.append(f'INFO: CAMBIA  {nombre}  ({_motivo(nombre)})')
        else:
            cuenta['iguales'] += 1
            lineas.append(f'INFO: IGUAL   {nombre}')
    lineas.append(f"INFO: resumen: {cuenta['cambian']} cambian, {cuenta['nuevos']} nuevos, "
                  f"{cuenta['se_quitan']} se quitan, {cuenta['iguales']} iguales")
    return lineas


def env_nuevo(config, admin, salida):
    """Variables actuales + solo las nuevas. Nunca reemplaza un valor existente en silencio ni activa el flag."""
    actuales = _variables(_cargar(config))
    de_admin = _variables(_cargar(admin))
    lineas, nuevas = [], {}
    for nombre in VARIABLES_PUBLICAS_NUEVAS:
        valor = de_admin.get(nombre) or ''
        if not valor:
            lineas.append(f'FAIL: la Lambda admin no tiene {nombre}: no hay de dónde tomarla')
            continue
        if nombre in CORREOS and not PATRON_CORREO.fullmatch(valor):
            lineas.append(f'FAIL: el valor de {nombre} en la Lambda admin no tiene formato de correo')
            continue
        mostrado = redactar(valor) if nombre in CORREOS else valor
        if nombre in actuales:
            if actuales[nombre] == valor:
                lineas.append(f'INFO: {nombre} ya está igual en la Lambda pública')
            else:
                lineas.append(f'FAIL: {nombre} existe en la Lambda pública con otro valor: no se pisa, revisar')
            continue
        nuevas[nombre] = valor
        lineas.append(f'INFO: AÑADE    {nombre} = {mostrado}')
    flag = actuales.get(FLAG)
    if flag is not None and str(flag).lower() != 'false':
        lineas.append(f'FAIL: {FLAG} está en {flag} en la Lambda pública: no se despliega con avisos encendidos')
    elif flag is None:
        nuevas[FLAG] = 'false'
        lineas.append(f'INFO: AÑADE    {FLAG} = false')
    else:
        lineas.append(f'INFO: {FLAG} ya está en false')
    if any(l.startswith('FAIL') for l in lineas):
        return lineas
    final = dict(actuales)
    final.update(nuevas)
    _guardar(salida, {'Variables': final})
    lineas.append(f'INFO: variables finales: {len(final)} ({len(actuales)} preservadas, {len(nuevas)} añadidas)')
    return lineas


def muestra_sentencias(plantilla):
    """Sentencias del aviso tal como están en la plantilla, con sus placeholders: el diff de IAM sin correos reales."""
    with open(plantilla, encoding='utf-8') as f:
        sentencias = {s['Sid']: s for s in json.load(f)['Statement'] if 'Sid' in s}
    for sid in (SID_SES, SID_AVISOS):
        print(f'INFO: {sid}')
        print(json.dumps(sentencias[sid], indent=2, ensure_ascii=False))
    return 0


def politica_fusion(live, plantilla, bucket, origen, destino, cuenta, salida):
    """Agrega solo las sentencias del aviso que faltan. Nunca quita, reordena ni cambia las existentes."""
    esperadas = _renderizar(plantilla, {'RESENAS_BUCKET': bucket, 'RESENAS_FROM_EMAIL': origen,
                                        'RESENAS_ADMIN_EMAIL': destino, 'AWS_REGION': REGION, 'ACCOUNT_ID': cuenta})
    if SID_SES not in esperadas or SID_AVISOS not in esperadas:
        return ['FAIL: la plantilla no genera las sentencias del aviso']
    documento = _cargar(live)
    documento = documento.get('PolicyDocument', documento)
    existentes = _lista(documento.get('Statement'))
    vivas = {s.get('Sid'): s for s in existentes}
    lineas, anadir = [], []
    for sid in (SID_SES, SID_AVISOS):
        if sid not in vivas:
            anadir.append(esperadas[sid])
            lineas.append(f'INFO: AÑADIR     {sid}')
        elif _normal(vivas[sid]) == _normal(esperadas[sid]):
            lineas.append(f'INFO: YA PRESENTE {sid}')
        else:
            lineas.append(f'FAIL: {sid} existe con otro contenido: no se cambia en automático, revisar')
    if any(l.startswith('FAIL') for l in lineas):
        return lineas
    if not anadir:
        lineas.append('INFO: sin cambios: el rol ya tiene el aviso')
        return lineas
    _guardar(salida, {'Version': documento.get('Version', '2012-10-17'), 'Statement': existentes + anadir})
    lineas.append(f'INFO: documento fusionado: {len(existentes)} sentencias existentes sin cambios y {len(anadir)} nuevas')
    return lineas


def _dato(ruta):
    """Valor de un JSON de snapshot; un archivo que dice ausente se devuelve como None."""
    doc = _cargar(ruta)
    return None if isinstance(doc, dict) and doc.get('ausente') else doc


def normalizar_opcional(crudo, salida):
    """Salida de un recurso opcional, ya aceptada por el shell como respuesta válida de AWS.

    stdout vacío (o solo espacios) = ausente, y se guarda como {"ausente": true}.
    JSON válido = se conserva tal cual. Cualquier otra cosa (texto, HTML, null) = FAIL: no se toma como ausencia.
    """
    contenido = _bytes(crudo)
    if not contenido.strip():
        _guardar(salida, {'ausente': True})
        return 0
    try:
        doc = json.loads(contenido.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        print('FAIL: la salida no es JSON válido')
        return 1
    if doc is None:
        print('FAIL: la salida es null: no es una respuesta válida')
        return 1
    _guardar(salida, doc)
    return 0


def concurrencia_canonica(doc):
    """Sin ReservedConcurrentExecutions no hay concurrencia reservada: se guarda como ausencia explícita.
    No se inventa un 0: si la clave existe, su valor se conserva tal cual (también si es 0)."""
    if isinstance(doc, dict) and 'ReservedConcurrentExecutions' in doc:
        return doc
    return {'ausente': True}


def instantanea(config, zip_codigo, url, recurso, concurrencia, cloudfront, export_sha, carpeta):
    """Foto de lo que la Fase A no debe cambiar. Escribe cada parte en la carpeta (600)."""
    os.makedirs(carpeta, exist_ok=True)
    for nombre, origen in (('config.json', config), ('url.json', url), ('recurso.json', recurso)):
        _guardar(os.path.join(carpeta, nombre), _cargar(origen))
    _guardar(os.path.join(carpeta, 'concurrencia.json'), concurrencia_canonica(_cargar(concurrencia)))
    # Solo lo que cambia cuando alguien toca la distribución. InProgressInvalidationBatches cambia solo, sin cambio de config.
    distribucion = _cargar(cloudfront).get('Distribution') or {}
    _guardar(os.path.join(carpeta, 'cloudfront.json'), {'ETag': _cargar(cloudfront).get('ETag'),
                                                       'Estado': distribucion.get('Status'),
                                                       'UltimaModificacion': distribucion.get('LastModifiedTime')})
    shutil.copyfile(zip_codigo, os.path.join(carpeta, 'desplegado.zip'))
    os.chmod(os.path.join(carpeta, 'desplegado.zip'), 0o600)
    with open(os.path.join(carpeta, 'codigo.sha'), 'w', encoding='utf-8') as f:
        f.write(base64.b64encode(hashlib.sha256(_bytes(zip_codigo)).digest()).decode('ascii'))
    with open(os.path.join(carpeta, 'export.sha'), 'w', encoding='utf-8') as f:
        f.write(export_sha.strip())
    for nombre in os.listdir(carpeta):
        os.chmod(os.path.join(carpeta, nombre), 0o600)
    os.chmod(carpeta, 0o700)
    return 0


def comparar_politica(antes, despues):
    """Toda sentencia que tenía el rol antes sigue igual después. Lo que se agregó no cuenta aquí."""
    def sentencias(ruta):
        doc = _cargar(ruta)
        doc = doc.get('PolicyDocument', doc)
        return {json.dumps(s, sort_keys=True): s for s in _lista(doc.get('Statement'))}
    viejas, nuevas = sentencias(antes), sentencias(despues)
    perdidas = [s.get('Sid', '(sin Sid)') for k, s in viejas.items() if k not in nuevas]
    if perdidas:
        return [f"FAIL: se perdieron o cambiaron sentencias existentes: {', '.join(perdidas)}"]
    return [f'PASS: las {len(viejas)} sentencias anteriores siguen iguales; se agregaron {len(nuevas) - len(viejas)}']


def config_iam(config, admin, salida):
    """Configuración para verificar la política: bucket de la Lambda pública y correos de la Lambda admin (la misma
    fuente que usa la fusión, así la verificación no depende de si la Fase A ya cambió el Environment)."""
    de_admin = _variables(_cargar(admin))
    variables = {'RESENAS_BUCKET': _variables(_cargar(config)).get('RESENAS_BUCKET'),
                 'RESENAS_FROM_EMAIL': de_admin.get('RESENAS_FROM_EMAIL'),
                 'RESENAS_ADMIN_EMAIL': de_admin.get('RESENAS_ADMIN_EMAIL')}
    _guardar(salida, {'Environment': {'Variables': variables}})
    return 0


def _leer_texto(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read().strip()


def verificar_publico(antes, despues, codesha, admin):
    """PASS/FAIL de la Fase A: flag apagado, correos del admin, y todo lo demás igual que antes."""
    a, d = os.path.join(antes, 'config.json'), os.path.join(despues, 'config.json')
    cfg_a, cfg_d = _cargar(a), _cargar(d)
    vars_a, vars_d = _variables(cfg_a), _variables(cfg_d)
    vars_admin = _variables(_cargar(admin))
    lineas = []

    def marca(condicion, texto):
        lineas.append(('PASS: ' if condicion else 'FAIL: ') + texto)

    marca(cfg_d.get('State') == 'Active' and cfg_d.get('LastUpdateStatus') == 'Successful',
          'ork3d-resenas Active y LastUpdateStatus Successful')
    marca(_leer_texto(os.path.join(despues, 'codigo.sha')) == codesha,
          'código desplegado = paquete propuesto (CodeSha256 esperado)')
    flag = vars_d.get(FLAG)
    marca(flag is None or str(flag).lower() == 'false', f'{FLAG} apagado (ausente o false)')
    for nombre in VARIABLES_PUBLICAS_NUEVAS:
        esperado = vars_admin.get(nombre)
        marca(bool(esperado) and vars_d.get(nombre) == esperado,
              f'{nombre} igual que en la Lambda admin' + (f' ({redactar(esperado)})' if nombre in CORREOS else ''))
    preservadas = [k for k in vars_a if vars_d.get(k) == vars_a[k]]
    marca(len(preservadas) == len(vars_a),
          f'variables anteriores preservadas: {len(preservadas)} de {len(vars_a)}')
    for campo in CAMPOS_CONFIG:
        marca(cfg_a.get(campo) == cfg_d.get(campo), f'{campo} igual que antes')
    for nombre, etiqueta in (('url.json', 'Function URL igual que antes'),
                             ('recurso.json', 'resource policy de la Lambda igual que antes'),
                             ('concurrencia.json', 'concurrencia igual que antes'),
                             ('cloudfront.json', 'CloudFront (ETag y estado) igual que antes')):
        marca(_cargar(os.path.join(antes, nombre)) == _cargar(os.path.join(despues, nombre)), etiqueta)
    marca(_leer_texto(os.path.join(antes, 'export.sha')) == _leer_texto(os.path.join(despues, 'export.sha')),
          'export público sin cambios (si cambió, revisar si fue una reseña aprobada)')
    return lineas


def comparar_restauracion(antes, despues):
    cfg_a, cfg_d = _cargar(os.path.join(antes, 'config.json')), _cargar(os.path.join(despues, 'config.json'))
    lineas = []
    vars_a, vars_d = _variables(cfg_a), _variables(cfg_d)
    marca = lambda c, t: lineas.append(('PASS: ' if c else 'FAIL: ') + t)
    marca(vars_a == vars_d, 'variables exactamente como antes del despliegue')
    marca(_leer_texto(os.path.join(antes, 'codigo.sha')) == _leer_texto(os.path.join(despues, 'codigo.sha')),
          'código exactamente como antes del despliegue')
    marca(cfg_d.get('LastUpdateStatus') == 'Successful', 'LastUpdateStatus Successful tras revertir')
    for nombre, etiqueta in (('url.json', 'Function URL igual que antes'),
                             ('recurso.json', 'resource policy igual que antes'),
                             ('concurrencia.json', 'concurrencia igual que antes'),
                             ('cloudfront.json', 'CloudFront igual que antes')):
        marca(_cargar(os.path.join(antes, nombre)) == _cargar(os.path.join(despues, nombre)), etiqueta)
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
        if orden == 'zip-aviso':
            return zip_aviso(*args)
        if orden == 'env-nuevo':
            lineas = env_nuevo(*args)
        elif orden == 'muestra-sentencias':
            return muestra_sentencias(*args)
        elif orden == 'politica-fusion':
            lineas = politica_fusion(*args)
        elif orden == 'normalizar-opcional':
            return normalizar_opcional(*args)
        elif orden == 'instantanea':
            return instantanea(*args)
        elif orden == 'comparar-paquetes':
            lineas = comparar_paquetes(*args)
        elif orden == 'verificar-publico':
            lineas = verificar_publico(*args)
        elif orden == 'comparar-restauracion':
            lineas = comparar_restauracion(*args)
        elif orden == 'comparar-politica':
            lineas = comparar_politica(*args)
        elif orden == 'config-iam':
            return config_iam(*args)
        elif orden == 'politica':
            lineas = politica(*args)
        elif orden == 'codigo':
            lineas = codigo(*args)
        elif orden == 'comparar':
            lineas = comparar(*args)
        elif orden == 'logs':
            lineas = logs(*args)
        else:
            raise SystemExit(f'orden desconocida: {orden}')
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
        print(f'FAIL: no se pudo leer o procesar ({type(exc).__name__})')
        return 1
    for linea in lineas:
        print(linea)
    return 1 if any(l.startswith('FAIL') for l in lineas) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
