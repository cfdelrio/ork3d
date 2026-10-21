"""Pruebas de la guarda de deploy en .github/workflows/*.yml. Sin red y sin GitHub: se leen los YAML del repo.

Reglas:
- el job que despliega a producción (environment: production o ./deploy.sh) tiene, a nivel de job, exactamente
  `if: github.ref == 'refs/heads/main'`;
- ese job conserva `environment: production` y su step sigue ejecutando ./deploy.sh con BUCKET y DISTRIBUTION_ID;
- push solo sobre main, y los disparadores son push y workflow_dispatch, sin otros.

El lector es mínimo y se apoya en la indentación de estos archivos (el repo no depende de PyYAML). Las pruebas de
mutación comprueban que la regla rechaza cada variante rota.
"""
import glob
import os
import re
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
CARPETA = os.path.join(RAIZ, '.github', 'workflows')
GUARDA = "github.ref == 'refs/heads/main'"
COMANDOS_DE_DEPLOY = ('./deploy.sh', 'aws s3 sync', 'create-invalidation')
VARIABLES = ('BUCKET: ${{ vars.BUCKET }}', 'DISTRIBUTION_ID: ${{ vars.DISTRIBUTION_ID }}')


def leer(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read().splitlines()


def secciones(lineas):
    """Divide el YAML por claves de primer nivel: {'on': [...], 'jobs': [...], ...}."""
    res, actual = {}, None
    for linea in lineas:
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_-]*):', linea)
        if m:
            actual = res[m.group(1)] = []
        elif actual is not None:
            actual.append(linea)
    return res


def claves(bloque, sangria):
    """(clave, valor) de las líneas con exactamente `sangria` espacios antes de la clave."""
    patron = re.compile(' ' * sangria + r'([A-Za-z0-9_-]+):\s*(.*?)\s*$')
    return [m.groups() for m in map(patron.match, bloque) if m]


def bajo(bloque, clave, sangria):
    """Líneas debajo de `clave:` (a esa sangría), hasta la siguiente clave del mismo nivel."""
    patron = re.compile(' ' * sangria + r'([A-Za-z0-9_-]+):')
    res, dentro = [], False
    for linea in bloque:
        m = patron.match(linea)
        if m:
            dentro = m.group(1) == clave
        elif dentro:
            res.append(linea)
    return res


def jobs_de(bloque):
    """Bloques de cada job de `jobs:`, por nombre (los nombres van con dos espacios)."""
    res, actual = {}, None
    for linea in bloque:
        m = re.match(r'^  ([A-Za-z0-9_-]+):\s*$', linea)
        if m:
            actual = res[m.group(1)] = []
        elif actual is not None:
            actual.append(linea)
    return res


def pasos_de(bloque_job):
    """Bloques de cada item de `steps:` (cada item empieza con `- ` a seis espacios)."""
    res, actual, en_steps = [], None, False
    for linea in bloque_job:
        if linea.rstrip() == '    steps:':
            en_steps = True
            continue
        if re.match(r'^    \S', linea):
            en_steps = False
        if en_steps and linea.startswith('      - '):
            actual = []
            res.append(actual)
        if en_steps and actual is not None:
            actual.append(linea)
    return res


def es_de_deploy(bloque):
    """True si el job apunta a producción o ejecuta un comando de deploy."""
    return ('environment', 'production') in claves(bloque, 4) or \
        any(c in linea for linea in bloque for c in COMANDOS_DE_DEPLOY)


def problemas_de(lineas):
    """Incumplimientos de la regla en un workflow. Lista vacía si cumple o si el workflow no despliega nada."""
    sec = secciones(lineas)
    on = sec.get('on', [])
    jobs = jobs_de(sec.get('jobs', []))
    despliegan = [n for n, b in jobs.items() if es_de_deploy(b)]
    if not despliegan:
        return []
    problemas = []
    disparadores = sorted(k for k, _ in claves(on, 2))
    if disparadores != ['push', 'workflow_dispatch']:
        problemas.append(f'disparadores {disparadores}: se esperaban exactamente push y workflow_dispatch')
    ramas = [v for k, v in claves(bajo(on, 'push', 2), 4) if k == 'branches']
    if ramas != ['[main]']:
        problemas.append(f'push.branches {ramas}: se esperaba [main]')
    for nombre in despliegan:
        bloque = jobs[nombre]
        guardas = [v for k, v in claves(bloque, 4) if k == 'if']
        if guardas != [GUARDA]:
            problemas.append(f'job {nombre}: if de job {guardas}: se esperaba exactamente {GUARDA}')
        if ('environment', 'production') not in claves(bloque, 4):
            problemas.append(f'job {nombre}: falta environment: production')
            continue
        pasos = [p for p in pasos_de(bloque) if any(linea.strip() == './deploy.sh' for linea in p)]
        if len(pasos) != 1:
            problemas.append(f'job {nombre}: se esperaba un step con ./deploy.sh, hay {len(pasos)}')
            continue
        texto = '\n'.join(pasos[0])
        faltan = [v for v in VARIABLES if v not in texto]
        if faltan:
            problemas.append(f'job {nombre}: al step de deploy le faltan {faltan}')
    return problemas


def con_cambios(texto, *cambios):
    """Aplica cambios (viejo, nuevo) sobre el texto; cada `viejo` debe aparecer exactamente una vez."""
    for viejo, nuevo in cambios:
        if texto.count(viejo) != 1:
            raise AssertionError(f'el cambio no aplica una sola vez: {viejo!r}')
        texto = texto.replace(viejo, nuevo)
    return texto.splitlines()


class ReglasDeDeployTest(unittest.TestCase):
    def setUp(self):
        rutas = sorted(glob.glob(os.path.join(CARPETA, '*.yml')) + glob.glob(os.path.join(CARPETA, '*.yaml')))
        self.assertTrue(rutas, 'no hay workflows en .github/workflows')
        self.flujos = {os.path.basename(r): leer(r) for r in rutas}
        self.jobs = jobs_de(secciones(self.flujos['deploy.yml']).get('jobs', []))

    def test_todos_los_workflows_cumplen_la_regla(self):
        for nombre, lineas in self.flujos.items():
            with self.subTest(workflow=nombre):
                self.assertEqual(problemas_de(lineas), [])

    def test_ningun_job_que_despliega_queda_sin_guarda(self):
        for nombre_wf, lineas in self.flujos.items():
            for nombre, bloque in jobs_de(secciones(lineas).get('jobs', [])).items():
                if es_de_deploy(bloque):
                    with self.subTest(workflow=nombre_wf, job=nombre):
                        self.assertEqual([v for k, v in claves(bloque, 4) if k == 'if'], [GUARDA])

    def test_el_job_deploy_es_de_produccion(self):
        self.assertIn('deploy', self.jobs)
        self.assertIn(('environment', 'production'), claves(self.jobs['deploy'], 4))

    def test_push_sigue_limitado_a_main(self):
        on = secciones(self.flujos['deploy.yml']).get('on', [])
        self.assertEqual([v for k, v in claves(bajo(on, 'push', 2), 4) if k == 'branches'], ['[main]'])

    def test_hay_workflow_dispatch_y_no_otros_disparadores(self):
        on = secciones(self.flujos['deploy.yml']).get('on', [])
        self.assertEqual(sorted(k for k, _ in claves(on, 2)), ['push', 'workflow_dispatch'])

    def test_guarda_exacta_a_nivel_de_job(self):
        self.assertEqual([v for k, v in claves(self.jobs['deploy'], 4) if k == 'if'], [GUARDA])

    def test_environment_production_se_conserva(self):
        self.assertIn(('environment', 'production'), claves(self.jobs['deploy'], 4))

    def test_el_step_sigue_ejecutando_deploy_sh_con_sus_variables(self):
        pasos = [p for p in pasos_de(self.jobs['deploy']) if any(linea.strip() == './deploy.sh' for linea in p)]
        self.assertEqual(len(pasos), 1)
        texto = '\n'.join(pasos[0])
        for variable in VARIABLES:
            self.assertIn(variable, texto)


class DetectaRupturasTest(unittest.TestCase):
    """Cada variante rota de deploy.yml debe fallar la regla. Si alguna pasa, la regla no protege nada."""

    def setUp(self):
        with open(os.path.join(CARPETA, 'deploy.yml'), encoding='utf-8') as f:
            self.texto = f.read()
        self.guarda = "    if: github.ref == 'refs/heads/main'\n"

    def assertFalla(self, lineas, fragmento):
        problemas = problemas_de(lineas)
        self.assertTrue(any(fragmento in p for p in problemas), f'no detectó {fragmento!r}: {problemas}')

    def test_sin_guarda_de_job_se_rechaza(self):
        self.assertFalla(con_cambios(self.texto, (self.guarda, '')), 'if de job []')

    def test_guarda_solo_en_el_step_se_rechaza(self):
        lineas = con_cambios(
            self.texto,
            (self.guarda, ''),
            ('      - name: Run deploy\n', "      - name: Run deploy\n        if: github.ref == 'refs/heads/main'\n"))
        self.assertFalla(lineas, 'if de job []')

    def test_guarda_de_otra_rama_se_rechaza(self):
        lineas = con_cambios(self.texto, (self.guarda, "    if: github.ref == 'refs/heads/dev'\n"))
        self.assertFalla(lineas, 'if de job')

    def test_guarda_duplicada_se_rechaza(self):
        lineas = con_cambios(self.texto, (self.guarda, self.guarda + self.guarda))
        self.assertFalla(lineas, 'if de job')

    def test_job_nuevo_que_despliega_sin_guarda_se_rechaza(self):
        extra = ('          ./deploy.sh\n'
                 '  otro:\n'
                 '    runs-on: ubuntu-latest\n'
                 '    environment: production\n'
                 '    steps:\n'
                 '      - name: Deploy directo\n'
                 '        run: ./deploy.sh\n')
        self.assertFalla(con_cambios(self.texto, ('          ./deploy.sh\n', extra)), 'job otro: if de job []')

    def test_rama_ampliada_en_push_se_rechaza(self):
        lineas = con_cambios(self.texto, ('    branches: [main]\n', "    branches: [main, 'release/**']\n"))
        self.assertFalla(lineas, 'push.branches')

    def test_disparador_extra_se_rechaza(self):
        lineas = con_cambios(self.texto, ('  workflow_dispatch:\n', '  workflow_dispatch:\n  pull_request:\n'))
        self.assertFalla(lineas, 'disparadores')

    def test_sin_environment_production_se_rechaza(self):
        self.assertFalla(con_cambios(self.texto, ('    environment: production\n', '')), 'falta environment')

    def test_sin_step_de_deploy_se_rechaza(self):
        lineas = con_cambios(self.texto, ('          ./deploy.sh\n', '          echo sin deploy\n'))
        self.assertFalla(lineas, 'hay 0')

    def test_sin_variables_del_step_se_rechaza(self):
        lineas = con_cambios(self.texto, ('          BUCKET: ${{ vars.BUCKET }}\n', ''))
        self.assertFalla(lineas, 'le faltan')


if __name__ == '__main__':
    unittest.main()
