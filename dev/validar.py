#!/usr/bin/env python3
"""Validaciones estáticas de ORK3D: sintaxis, HTML, JSON y políticas de reseñas.

Uso (desde la raíz del repo):  python3 dev/validar.py
Sale con 1 si alguna validación falla. No hay validaciones que se salteen: si falta una
herramienta (node, bash) la validación falla y lo dice.
"""
import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fallos = []
aprobadas = 0


def ok(nombre):
    global aprobadas
    aprobadas += 1
    print(f'PASS  {nombre}')


def falla(nombre, detalle):
    fallos.append(nombre)
    print(f'FAIL  {nombre}\n      {detalle}')


def leer(ruta):
    with open(os.path.join(ROOT, ruta), encoding='utf-8') as f:
        return f.read()


def comprobar(nombre, condicion, detalle=''):
    if condicion:
        ok(nombre)
    else:
        falla(nombre, detalle or 'condición no cumplida')


def correr(comando):
    return subprocess.run(comando, cwd=ROOT, capture_output=True, text=True)


# ── Sintaxis ───────────────────────────────────────────────────────────────
def validar_javascript():
    node = shutil.which('node')
    archivos = sorted(glob.glob(os.path.join(ROOT, 'assets/js/*.js')) +
                      glob.glob(os.path.join(ROOT, 'dev/e2e/*.js')) + [os.path.join(ROOT, 'config.js')])
    if not node:
        falla('JavaScript: node disponible', 'no se encontró node; no se puede validar la sintaxis')
        return
    for ruta in archivos:
        r = correr([node, '--check', ruta])
        comprobar(f'JavaScript: sintaxis de {os.path.relpath(ruta, ROOT)}', r.returncode == 0, r.stderr.strip())


def validar_shell():
    archivos = ['deploy.sh', 'verify.sh', 'infra/setup.sh', 'infra/teardown.sh', 'infra/resenas-setup.sh',
                'infra/admin-setup.sh', 'infra/smoke-if-match.sh', 'infra/smoke-ses.sh', 'infra/admin-deploy.sh', 'admin/resenas.sh', 'admin/tests/run_tests.sh',
                'admin/tests/fixtures.sh']
    for rel in archivos:
        r = correr(['bash', '-n', rel])
        comprobar(f'Shell: sintaxis de {rel}', r.returncode == 0, r.stderr.strip())


def validar_python():
    archivos = glob.glob(os.path.join(ROOT, 'api/resenas/*.py')) + glob.glob(os.path.join(ROOT, 'api/resenas/nucleo/*.py')) + \
        glob.glob(os.path.join(ROOT, 'dev/*.py')) + glob.glob(os.path.join(ROOT, 'admin/tests/*.py')) + \
        glob.glob(os.path.join(ROOT, 'infra/tests/*.py'))
    for ruta in archivos:
        r = correr([sys.executable, '-m', 'py_compile', ruta])
        comprobar(f'Python: compila {os.path.relpath(ruta, ROOT)}', r.returncode == 0, r.stderr.strip())


def validar_json():
    for ruta in sorted(glob.glob(os.path.join(ROOT, 'infra/resenas/*.json'))):
        try:
            with open(ruta, encoding='utf-8') as f:
                json.load(f)
            ok(f'JSON: {os.path.relpath(ruta, ROOT)}')
        except ValueError as exc:
            falla(f'JSON: {os.path.relpath(ruta, ROOT)}', str(exc))


# ── HTML: etiquetas balanceadas ────────────────────────────────────────────
VACIAS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}
CIERRE_OPCIONAL = {'li', 'p', 'dt', 'dd', 'option'}


class Balance(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pila = []
        self.errores = []

    def handle_starttag(self, tag, attrs):
        if tag not in VACIAS:
            self.pila.append((tag, self.getpos()))

    def handle_startendtag(self, tag, attrs):
        pass  # <path/>, <img/>: no abren nada

    def handle_endtag(self, tag):
        if tag in VACIAS:
            return
        for i in range(len(self.pila) - 1, -1, -1):
            if self.pila[i][0] == tag:
                for abierta, pos in self.pila[i + 1:]:
                    if abierta not in CIERRE_OPCIONAL:
                        self.errores.append(f'<{abierta}> (línea {pos[0]}) queda sin cerrar antes de </{tag}>')
                del self.pila[i:]
                return
        self.errores.append(f'</{tag}> (línea {self.getpos()[0]}) no tiene apertura')

    def cerrar(self):
        for tag, pos in self.pila:
            if tag not in CIERRE_OPCIONAL:
                self.errores.append(f'<{tag}> (línea {pos[0]}) nunca se cierra')


def validar_html():
    paginas = ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'error.html',
               'opinar.html', 'opiniones.html', 'admin-web/resenas.html', 'admin-web/entrar.html']
    for rel in paginas:
        parser = Balance()
        parser.feed(leer(rel))
        parser.cerrar()
        comprobar(f'HTML: etiquetas balanceadas en {rel}', not parser.errores, '; '.join(parser.errores[:5]))


# ── Políticas de privacidad y publicación ──────────────────────────────────
def validar_politicas():
    opinar = leer('opinar.html')
    comprobar('Reseñas: /opinar tiene noindex', re.search(r'<meta name="robots" content="[^"]*noindex', opinar) is not None)
    comprobar('Reseñas: /opinar tiene referrer no-referrer',
              '<meta name="referrer" content="no-referrer"' in opinar)
    terceros = ['googleapis', 'gstatic', 'gtag', 'googletagmanager', 'plausible', 'main.js', 'analytics']
    encontrados = [t for t in terceros if t in opinar]
    comprobar('Reseñas: /opinar no carga analytics ni terceros', not encontrados, f'aparece: {encontrados}')

    # /opiniones: lanzada. Indexable, con canonical exacto, enlazada desde el footer de las páginas públicas
    # principales y en el sitemap. No se enlaza desde páginas de error, robots, JS de navegación ni CSS global.
    opiniones = leer('opiniones.html')
    comprobar('Reseñas: /opiniones indexable (sin noindex)',
              re.search(r'<meta name="robots" content="[^"]*noindex', opiniones) is None)
    comprobar('Reseñas: /opiniones con canonical exacto',
              '<link rel="canonical" href="https://ork3d.com/opiniones" />' in opiniones)
    for rel in ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html']:
        pagina = leer(rel)
        footer = pagina[pagina.index('<div class="footer__links">'):]
        footer = footer[:footer.index('</div>')]
        comprobar(f'Reseñas: {rel} enlaza /opiniones desde el footer',
                  '<a href="/opiniones">Opiniones</a>' in footer)
    comprobar('Reseñas: /opiniones está en el sitemap', 'https://ork3d.com/opiniones' in leer('sitemap.xml'))

    enlazada = []
    for rel in ['error.html', 'robots.txt', 'assets/js/main.js', 'assets/css/styles.css']:
        if 'opiniones' in leer(rel):
            enlazada.append(rel)
    comprobar('Reseñas: /opiniones no se enlaza desde error, robots, JS de navegación ni CSS global',
              not enlazada, f'aparece en: {enlazada}')

    config = leer('config.js')
    comprobar('Config: RESENAS_API_URL y GOOGLE_REVIEW_URL existen',
              'RESENAS_API_URL' in config and 'GOOGLE_REVIEW_URL' in config)

    robots = leer('robots.txt')
    comprobar('SEO: robots.txt no permite /api/', 'Disallow: /api/' in robots)

    deploy = leer('deploy.sh')
    for patron in ['api/*', 'admin/*', 'dev/*', 'docs/*', 'resenas/*']:
        comprobar(f'Deploy: sync excluye {patron}', f'--exclude "{patron}"' in deploy)
    comprobar('Deploy: sube opinar.html y opiniones.html',
              'opinar.html' in deploy and 'opiniones.html' in deploy)

    redir = leer('infra/redirect-www.js')
    comprobar("CloudFront: regla /opinar/* antes de la de extensionless",
              "'/opinar/'" in redir and "'/opinar/'" in redir and
              redir.index("'/opinar/'") < redir.index('Extensionless'))


# ── IAM: mínimo privilegio ────────────────────────────────────────────────
def validar_iam():
    def sentencias(nombre):
        with open(os.path.join(ROOT, 'infra/resenas', nombre), encoding='utf-8') as f:
            return json.load(f)['Statement']

    for nombre in ['lambda-permisos.json', 'admin-permisos.json', 'admin-lambda-permisos.json']:
        for st in sentencias(nombre):
            acciones = st['Action'] if isinstance(st['Action'], list) else [st['Action']]
            comprobar(f'IAM {nombre}: sin s3:* ni recursos "*"',
                      'Resource' in st and st['Resource'] != '*' and 's3:*' not in acciones,
                      f'{st.get("Sid")}')

    lam = sentencias('lambda-permisos.json')
    puts = [r for st in lam if st['Action'] == 's3:PutObject' for r in st['Resource']]
    comprobar('IAM lambda: no puede escribir tokens/ (solo el CLI crea tokens)',
              not any('/tokens/' in r for r in puts), f'escribe en: {puts}')
    gets_tokens = any('/tokens/' in r for st in lam for r in (st['Resource'] if isinstance(st['Resource'], list) else [st['Resource']]))
    comprobar('IAM lambda: lee tokens/ para validarlos', gets_tokens)

    politica = json.load(open(os.path.join(ROOT, 'infra/resenas/bucket-policy.json'), encoding='utf-8'))
    niegan_http = [st for st in politica['Statement'] if st.get('Effect') == 'Deny' and
                   'aws:SecureTransport' in json.dumps(st.get('Condition', {}))]
    comprobar('Bucket privado: niega tráfico sin TLS', bool(niegan_http))


def validar_despliegue():
    infra = leer('infra/resenas-setup.sh')
    comprobar('Despliegue: el script arranca en DRY_RUN=1 por defecto',
              'DRY_RUN="${DRY_RUN:-1}"' in infra)
    comprobar('Despliegue: el modo real pide escribir CREAR antes de tocar AWS',
              'CREAR' in infra and 'read -r -p' in infra)
    comprobar('Despliegue: el script no borra recursos de AWS (delete-*, --recursive, s3 rm)',
              re.search(r'delete-|--recursive|\bs3 rm\b', infra) is None)
    ruta_runbook = os.path.join(ROOT, 'docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md')
    comprobar('Despliegue: existe el runbook manual', os.path.isfile(ruta_runbook))
    if os.path.isfile(ruta_runbook):
        runbook = leer('docs/resenas/RUNBOOK-DESPLIEGUE-MANUAL.md')
        comprobar('Despliegue: el runbook exige dry-run y trae rollback',
                  'DRY_RUN=1' in runbook and 'Rollback' in runbook)
    # Ningún número de cuenta AWS (12 dígitos) ni access key versionada en el repo.
    cuentas = correr(['git', 'grep', '-n', '-E', r'(^|[^0-9])[0-9]{12}([^0-9]|$)', '--', '.'])
    comprobar('Seguridad: sin números de cuenta AWS de 12 dígitos en el repo',
              cuentas.returncode == 1 and cuentas.stdout == '', cuentas.stdout[:300])
    llaves = correr(['git', 'grep', '-n', '-E', 'AKIA[0-9A-Z]{16}', '--', '.'])
    comprobar('Seguridad: sin access keys AKIA en el repo', llaves.returncode == 1, llaves.stdout[:200])


# ── Panel admin V2: estático, CloudFront, ciclo de vida, deploy e infraestructura ──
def validar_admin_v2():
    for rel in ['admin-web/resenas.html', 'admin-web/entrar.html']:
        html = leer(rel)
        comprobar(f'Admin: {rel} noindex, nofollow',
                  re.search(r'<meta name="robots" content="noindex, nofollow"\s*/?>', html) is not None)
        comprobar(f'Admin: {rel} referrer no-referrer',
                  re.search(r'<meta name="referrer" content="no-referrer"\s*/?>', html) is not None)
        csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]*)"', html)
        comprobar(f'Admin: {rel} CSP por meta', csp is not None)
        politica = csp.group(1) if csp else ''
        comprobar(f'Admin: {rel} CSP sin unsafe-inline ni unsafe-eval',
                  'unsafe-' not in politica, politica[:160])
        comprobar(f'Admin: {rel} CSP con script-src self', "script-src 'self'" in politica)
        inline = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>', html)
        comprobar(f'Admin: {rel} sin scripts inline', not inline, str(inline)[:120])
        comprobar(f'Admin: {rel} sin <style> ni atributos style=',
                  '<style' not in html and ' style="' not in html)

    # Nada del panel en el sitio público: ni sitemap ni enlaces desde las páginas públicas.
    comprobar('Admin: el sitemap no incluye /admin', '/admin' not in leer('sitemap.xml'))
    for rel in ['index.html', 'torneos.html', 'regalos-empresariales-fin-de-ano.html', 'error.html',
                'opinar.html', 'opiniones.html']:
        comprobar(f'Admin: {rel} no enlaza el panel', '/admin' not in leer(rel))

    # Reglas de CloudFront: /admin/api/* pasa sin reescribir; las páginas se reescriben antes de la regla extensionless.
    redir = leer('infra/redirect-www.js')
    comprobar("Admin CloudFront: /admin/api/* no se reescribe", "'/admin/api/'" in redir)
    comprobar('Admin CloudFront: /admin/resenas y /admin/entrar van a sus páginas',
              '/admin-web/resenas.html' in redir and '/admin-web/entrar.html' in redir)
    comprobar('Admin CloudFront: reglas admin antes de la extensionless',
              redir.index('/admin-web/resenas.html') < redir.index('Extensionless') and
              redir.index('/admin-web/entrar.html') < redir.index('Extensionless'))

    # Ciclo de vida: las sesiones, logins y locks de admin/ vencen a los 2 días.
    with open(os.path.join(ROOT, 'infra/resenas/bucket-lifecycle.json'), encoding='utf-8') as f:
        reglas = json.load(f)['Rules']
    admin = [r for r in reglas if (r.get('Filter') or {}).get('Prefix') == 'admin/']
    comprobar('Admin: lifecycle de admin/ activo y de 2 días',
              len(admin) == 1 and admin[0].get('Status') == 'Enabled' and admin[0]['Expiration']['Days'] == 2)

    # Deploy: un cambio en admin-web/ dispara la publicación.
    comprobar('Admin: deploy.yml incluye admin-web/** en sus rutas', "'admin-web/**'" in leer('.github/workflows/deploy.yml'))

    # Infraestructura del panel: DRY_RUN por defecto y empaquetado del núcleo.
    setup_admin = leer('infra/admin-setup.sh')
    comprobar('Admin: admin-setup.sh arranca en DRY_RUN=1', 'DRY_RUN="${DRY_RUN:-1}"' in setup_admin)
    comprobar('Admin: admin-setup.sh no tiene comandos de borrado',
              re.search(r'\bdelete-|--recursive|\bs3 rm\b|delete-function', setup_admin) is None)
    comprobar('Admin: admin-setup.sh empaqueta admin_app.py con el núcleo',
              'admin_app.py nucleo' in setup_admin)
    comprobar('Admin: resenas-setup.sh detiene el deploy sin el núcleo',
              'if [[ ! -d api/resenas/nucleo ]]; then' in leer('infra/resenas-setup.sh'))

    # OAC hacia la Function URL: exactamente dos acciones, solo para cloudfront.amazonaws.com y la distribución.
    spec_permisos = json.loads(leer('infra/resenas/admin-function-url-permisos.json'))['Statement']
    conceden = {st['Action'] for st in spec_permisos
                if st.get('Principal') == {'Service': 'cloudfront.amazonaws.com'}
                and 'distribution/__DISTRIBUTION_ID__' in json.dumps(st.get('Condition', {}))}
    comprobar('Admin OAC: la especificación concede InvokeFunctionUrl e InvokeFunction a cloudfront.amazonaws.com por SourceArn',
              conceden == {'lambda:InvokeFunctionUrl', 'lambda:InvokeFunction'}, str(sorted(conceden)))
    # Forwarding de CloudFront: todas las cabeceras del viewer salvo Host (allExcept); sin declarar x-amz-*;
    # cookie ork3d_admin y query estado solamente.
    orp = json.loads(leer('infra/resenas/admin-origin-request-policy.json'))
    cabeceras = orp['HeadersConfig']
    comprobar('Admin CloudFront: la política de origen usa allExcept excluyendo solo Host, sin cabeceras x-amz-*',
              cabeceras['HeaderBehavior'] == 'allExcept' and cabeceras['Headers']['Items'] == ['Host']
              and not [h for h in cabeceras['Headers']['Items'] if h.lower().startswith('x-amz-')])
    comprobar('Admin CloudFront: cookies solo ork3d_admin y query solo estado',
              orp['CookiesConfig']['Cookies']['Items'] == ['ork3d_admin']
              and orp['QueryStringsConfig']['QueryStrings']['Items'] == ['estado'])


# ── Preflight y smoke de V2 (EC2): el preflight solo lee; el smoke es el único escritor y pide permiso ──
def validar_preflight_v2():
    preflight = 'infra/preflight_v2.py'
    r = correr([sys.executable, '-m', 'py_compile', preflight])
    comprobar('Preflight V2: compila infra/preflight_v2.py', r.returncode == 0, r.stderr.strip())
    fuente = leer(preflight)
    llamadas = set(re.findall(r"\b(?:ro|leer)\(\s*'([a-z0-9]+)',\s*'([a-z0-9-]+)'", fuente))
    spec = importlib.util.spec_from_file_location('preflight_v2', os.path.join(ROOT, preflight))
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    fuera = sorted(f'{s} {o}' for s, o in llamadas if not modulo.permitida(s, o))
    comprobar('Preflight V2: toda llamada literal a AWS está en la lista de solo lectura',
              len(llamadas) > 20 and not fuera, str(fuera)[:200])
    comprobar('Preflight V2: subprocess solo aparece dentro de ro(), la única puerta a la CLI',
              fuente.count('subprocess.run(') == 1)
    smoke = leer('infra/smoke-if-match.sh')
    comprobar('Smoke V2: escribe solo bajo admin/smoke-if-match/', 'PREFIJO="admin/smoke-if-match/"' in smoke)
    # Entorno vacío: sin ALLOW_SMOKE_WRITE el smoke debe abortar con 3 antes de tocar AWS.
    r = correr(['env', '-i', 'PATH=' + os.environ.get('PATH', ''), 'bash', 'infra/smoke-if-match.sh'])
    comprobar('Smoke V2: sin ALLOW_SMOKE_WRITE=1 aborta con código 3 antes de llamar a AWS',
              r.returncode == 3 and 'ABORTA' in r.stderr, f'rc={r.returncode} {r.stderr.strip()[:200]}')


# ── Smoke de SES para V2: envía un correo solo con ALLOW_SES_SEND=1 ──
def validar_smoke_ses():
    smoke = leer('infra/smoke-ses.sh')
    comprobar('Smoke SES: la guarda ALLOW_SES_SEND=1 va antes de cualquier llamada a AWS',
              smoke.index('ALLOW_SES_SEND') < smoke.index('"$AWS" '))
    # Entorno vacío: sin ALLOW_SES_SEND=1 el smoke debe abortar con 3 antes de tocar AWS.
    r = correr(['env', '-i', 'PATH=' + os.environ.get('PATH', ''), 'bash', 'infra/smoke-ses.sh'])
    comprobar('Smoke SES: sin ALLOW_SES_SEND=1 aborta con código 3 antes de llamar a AWS',
              r.returncode == 3 and 'ABORTA' in r.stderr, f'rc={r.returncode} {r.stderr.strip()[:200]}')


# ── Despliegue de la API admin V2: solo con ALLOW_ADMIN_DEPLOY=1 ──
def validar_admin_deploy():
    script = leer('infra/admin-deploy.sh')
    comprobar('Admin deploy: la guarda ALLOW_ADMIN_DEPLOY=1 va antes de cualquier llamada a AWS',
              script.index('ALLOW_ADMIN_DEPLOY') < script.index('"$AWS" sts get-caller-identity'))
    comprobar('Admin deploy: no hay ninguna llamada de escritura fuera de la sección 9 (escrituras)',
              script.index('# ── 9. Escrituras') < script.index('# ── 10. '))
    # Límite de CloudFront: el Comment de la origin request policy es de 128 caracteres como máximo.
    # Validar el shape con botocore no lo detecta; por eso se controla aquí.
    comentario = json.loads(leer('infra/resenas/admin-origin-request-policy.json')).get('Comment', '')
    comprobar('Admin ORP: el Comment existe y cabe en 128 caracteres (límite de CloudFront), con margen de 100',
              0 < len(comentario) <= 100, f'{len(comentario)} caracteres')
    # Forma del CacheBehavior admin: CloudFront rechazó un behavior sin SmoothStreaming. El shape de botocore no lo
    # exige, así que se controla aquí: campos explícitos, booleanos y bloques vacíos.
    spec_ayuda = importlib.util.spec_from_file_location('admin_deploy_ayuda', os.path.join(ROOT, 'infra/admin_deploy_ayuda.py'))
    modulo_ayuda = importlib.util.module_from_spec(spec_ayuda)
    spec_ayuda.loader.exec_module(modulo_ayuda)
    behavior = modulo_ayuda.behavior_admin('ORP-PRUEBA')
    comprobar('Admin deploy: el CacheBehavior admin incluye SmoothStreaming=false y bloques vacíos explícitos',
              behavior.get('SmoothStreaming') is False
              and behavior.get('TrustedSigners') == {'Enabled': False, 'Quantity': 0}
              and behavior.get('TrustedKeyGroups') == {'Enabled': False, 'Quantity': 0}
              and behavior.get('LambdaFunctionAssociations') == {'Quantity': 0}
              and behavior.get('FunctionAssociations') == {'Quantity': 0}
              and behavior.get('FieldLevelEncryptionId') == '' and behavior.get('Compress') is True,
              str(sorted(behavior)))
    r = correr([sys.executable, '-m', 'py_compile', os.path.join(ROOT, 'infra/admin_deploy_ayuda.py')])
    comprobar('Admin deploy: compila infra/admin_deploy_ayuda.py', r.returncode == 0, r.stderr.strip())
    # Entorno vacío: sin ALLOW_ADMIN_DEPLOY=1 el script debe abortar con 3 antes de tocar AWS.
    r = correr(['env', '-i', 'PATH=' + os.environ.get('PATH', ''), 'bash', 'infra/admin-deploy.sh'])
    comprobar('Admin deploy: sin ALLOW_ADMIN_DEPLOY=1 aborta con código 3 antes de llamar a AWS',
              r.returncode == 3 and 'ABORTA' in r.stderr, f'rc={r.returncode} {r.stderr.strip()[:200]}')


def main():
    validar_javascript()
    validar_shell()
    validar_python()
    validar_json()
    validar_html()
    validar_politicas()
    validar_iam()
    validar_despliegue()
    validar_admin_v2()
    validar_preflight_v2()
    validar_smoke_ses()
    validar_admin_deploy()
    print(f'\nValidaciones: {aprobadas} PASS, {len(fallos)} FAIL')
    sys.exit(1 if fallos else 0)


if __name__ == '__main__':
    main()
