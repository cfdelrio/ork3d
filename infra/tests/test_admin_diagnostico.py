"""Pruebas del diagnóstico del 404 de la API admin (infra/admin-diagnostico.sh y admin_diagnostico_ayuda.py).

Sin AWS ni red: aws_fake_admin.py guarda el estado y registra las llamadas; aws_fake_curl.py reemplaza curl.
FAKE_CURL_MAPEO=1 reproduce el borde observado en EC2: CloudFront reemplaza los 403/404 de la API por la página de
error del sitio con estado 404, y la Lambda sí respondió.
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
SCRIPT = os.path.join(RAIZ, 'infra', 'admin-diagnostico.sh')
AYUDA = os.path.join(RAIZ, 'infra', 'admin_diagnostico_ayuda.py')
FAKE_AWS = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_admin.py')
FAKE_CURL = os.path.join(RAIZ, 'infra', 'tests', 'aws_fake_curl.py')
LOG_GRUPO = '/aws/lambda/ork3d-resenas-admin'
OPERACIONES_PERMITIDAS = {'cloudfront get-distribution-config', 'lambda get-function-url-config',
                          'logs filter-log-events', 's3api get-bucket-policy'}


def cargar(nombre, ruta):
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def leer_json(ruta):
    with open(ruta, encoding='utf-8') as f:
        return json.load(f)


def leer_texto(ruta):
    with open(ruta, encoding='utf-8') as f:
        return f.read()


diag = cargar('admin_diagnostico_ayuda', AYUDA)
fake = cargar('aws_fake_admin', FAKE_AWS)


def registro(nombre, codigo, tipo, inv, cabeceras=None):
    return {'nombre': nombre, 'codigo': codigo, 'tipo': tipo, 'cabeceras': cabeceras or {},
            'invocaciones': inv, 't0': 0, 't1': 0}


def conjunto(post, put=('app', 1), get=('app', 1), unk=('html', 404, 1)):
    return [registro('get-sin-cookie', 401, get[0], get[1]),
            registro('put-control', 405, put[0], put[1]),
            registro('post-sin-origin', post[1], post[0], post[2]),
            registro('post-ruta-inexistente', unk[1], unk[0], unk[2])]


MAPEO = {'errores': [{'codigo': 403, 'respuesta': 404, 'pagina': '/error.html'},
                     {'codigo': 404, 'respuesta': 404, 'pagina': '/error.html'}],
         'listbucket_oac': None}
SIN_MAPEO = {'errores': [], 'listbucket_oac': None}


class DecidirTest(unittest.TestCase):
    def test_observado_en_ec2_es_mapeo_de_errores_personalizados(self):
        regs = conjunto(post=('html', 404, 1))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'MAPEO_ERRORES_PERSONALIZADOS')

    def test_403_de_la_app_llegado_integro_es_build_anterior(self):
        regs = conjunto(post=('app', 403, 1))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'NO_REPRODUCIDO')

    def test_400_csrf_de_la_app_cumple_el_contrato(self):
        regs = conjunto(post=('app', 400, 1))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'CONTRATO_CSRF_OK')

    def test_control_put_que_no_llega_a_la_api_es_problema_de_comportamiento(self):
        regs = conjunto(post=('html', 404, 1), put=('html', 1))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'BORDE_COMPORTAMIENTO')

    def test_control_sin_reporte_en_logs_no_concluye(self):
        regs = conjunto(post=('html', 404, 1), put=('app', 0))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'LOGS_NO_CORRELACIONADOS')

    def test_post_que_no_invoca_la_lambda_es_problema_de_borde(self):
        regs = conjunto(post=('html', 404, 0), put=('app', 1))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'BORDE_SIN_INVOCACION')

    def test_404_con_invocacion_sin_mapeo_de_403_queda_desconocido(self):
        regs = conjunto(post=('html', 404, 1))
        self.assertEqual(diag.decidir(regs, SIN_MAPEO)[0], 'DESCONOCIDO')

    def test_sin_logs_no_se_concluye_sobre_el_origen(self):
        regs = conjunto(post=('html', 404, None))
        self.assertEqual(diag.decidir(regs, MAPEO)[0], 'SIN_LOGS')

    def test_faltan_peticiones_es_incompleto(self):
        self.assertEqual(diag.decidir([registro('post-sin-origin', 404, 'html', 1)], MAPEO)[0], 'INCOMPLETO')


class AyudaDiagnosticoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='diag-ayuda-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def escribir(self, nombre, datos):
        ruta = os.path.join(self.tmp, nombre)
        with open(ruta, 'w', encoding='utf-8') as f:
            json.dump(datos, f)
        return ruta

    def leer(self, nombre):
        with open(os.path.join(self.tmp, nombre), encoding='utf-8') as f:
            return json.load(f)

    def test_tipo_cuerpo_distingue_api_html_y_otro(self):
        self.assertEqual(diag.tipo_cuerpo('{"ok": false, "error": "origen"}'), 'app')
        self.assertEqual(diag.tipo_cuerpo('<!DOCTYPE html><html></html>'), 'html')
        self.assertEqual(diag.tipo_cuerpo('texto suelto'), 'otro')

    def test_limpiar_quita_correos_cuentas_y_firmas(self):
        cuenta = '9' * 12
        texto = f'correo ana.moderadora@example.com cuenta {cuenta} X-Amz-Signature=ABCDEF&x=1 cookie=ork3d_admin_secreto'
        limpio = diag.limpiar(texto)
        for sensible in ('ana.moderadora@example.com', cuenta, 'ABCDEF', 'ork3d_admin_secreto'):
            self.assertNotIn(sensible, limpio)
        self.assertIn('[correo]', limpio)
        self.assertIn('[cuenta]', limpio)

    def test_peticion_guarda_solo_cabeceras_visibles_y_sin_cuerpo_completo(self):
        cab = os.path.join(self.tmp, 'h.txt')
        with open(cab, 'w', encoding='utf-8') as f:
            f.write('HTTP/2 404\r\nset-cookie: ork3d_admin=SECRETO\r\nserver: AmazonS3\r\nx-cache: Error from cloudfront\r\n')
        cuerpo = os.path.join(self.tmp, 'b.txt')
        with open(cuerpo, 'w', encoding='utf-8') as f:
            f.write('<html>' + 'x' * 10000)
        salida = os.path.join(self.tmp, 'peticiones.json')
        diag.peticion(salida, 'post-sin-origin', '404', cab, cuerpo, '100', '200')
        guardado = leer_texto(salida)
        self.assertNotIn('SECRETO', guardado)
        self.assertNotIn('set-cookie', guardado)
        r = self.leer('peticiones.json')[0]
        self.assertEqual(r['cabeceras'], {'server': 'AmazonS3', 'x-cache': 'Error from cloudfront'})
        self.assertEqual(r['tipo'], 'html')
        self.assertEqual(stat.S_IMODE(os.stat(salida).st_mode), 0o600)

    def test_invocaciones_cuenta_solo_las_marcas_de_su_ventana(self):
        peticiones = self.escribir('peticiones.json', [
            {'nombre': 'a', 'codigo': 200, 'tipo': 'app', 'cabeceras': {}, 'invocaciones': None, 't0': 10000, 't1': 10500},
            {'nombre': 'b', 'codigo': 200, 'tipo': 'app', 'cabeceras': {}, 'invocaciones': None, 't0': 14000, 't1': 14500}])
        logs = self.escribir('logs.json', {'events': [
            {'timestamp': 10200, 'message': 'REPORT RequestId: 1'},
            {'timestamp': 14100, 'message': 'REPORT RequestId: 2'},
            {'timestamp': 14400, 'message': 'REPORT RequestId: 3'},
            {'timestamp': 20000, 'message': 'REPORT RequestId: fuera'},
            {'timestamp': 14200, 'message': 'START RequestId: x'}]})
        diag.invocaciones(peticiones, logs)
        datos = self.leer('peticiones.json')
        self.assertEqual([d['invocaciones'] for d in datos], [1, 2])

    def test_ventana_cubre_todas_las_peticiones(self):
        peticiones = self.escribir('peticiones.json', [
            {'nombre': 'a', 't0': 1000, 't1': 1100}, {'nombre': 'b', 't0': 5000, 't1': 5100}])
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            diag.ventana(peticiones)
        inicio, fin = (int(v) for v in salida.getvalue().split())
        self.assertLessEqual(inicio, 1000)
        self.assertGreaterEqual(fin, 5100)

    def test_config_resume_errores_behaviors_y_listbucket_sin_nombres_de_origen(self):
        dominio_lambda = 'abcdefgh.lambda-url.us-east-1.on.aws'
        admin = fake.configuracion_distribucion()
        admin['Origins']['Items'].append({'Id': 'resenas-admin-lambda', 'DomainName': dominio_lambda,
                                          'OriginAccessControlId': 'E1OAC'})
        admin['Origins']['Quantity'] = 2
        admin['CustomErrorResponses'] = {'Quantity': 1, 'Items': [
            {'ErrorCode': 403, 'ResponseCode': '404', 'ResponsePagePath': '/error.html', 'ErrorCachingMinTTL': 60}]}
        admin['CacheBehaviors'] = {'Quantity': 1, 'Items': [ayuda_behavior()]}
        dist = self.escribir('dist.json', {'ETag': 'E1', 'DistributionConfig': admin})
        url = self.escribir('url.json', {'AuthType': 'AWS_IAM'})
        politica = self.escribir('politica.json', {'Policy': json.dumps({'Statement': [
            {'Effect': 'Allow', 'Action': 's3:ListBucket', 'Principal': {'Service': 'cloudfront.amazonaws.com'}}]})})
        resumen = os.path.join(self.tmp, 'resumen.json')
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            diag.config(resumen, dist, url, politica)
        texto = salida.getvalue()
        datos = leer_json(resumen)
        self.assertEqual(datos['errores'], [{'codigo': 403, 'respuesta': 404, 'pagina': '/error.html'}])
        self.assertEqual(datos['auth_url'], 'AWS_IAM')
        self.assertTrue(datos['listbucket_oac'])
        admin_resumen = datos['comportamientos'][0]
        self.assertEqual(admin_resumen['patron'], '/admin/api/*')
        self.assertIn('POST', admin_resumen['metodos'])
        self.assertEqual(admin_resumen['origen'], 'lambda-url')
        self.assertNotIn(dominio_lambda, json.dumps(datos))
        self.assertNotIn(dominio_lambda, texto)
        self.assertIn('posición 0 de 1', texto)

    def test_conclusion_escribe_600_con_opciones_y_recomendacion(self):
        peticiones = self.escribir('peticiones.json', conjunto(post=('html', 404, 1)))
        config = self.escribir('config.json', dict(MAPEO, listbucket_oac=True))
        conclusion = os.path.join(self.tmp, 'conclusion.txt')
        with contextlib.redirect_stdout(io.StringIO()):
            diag.clasificar(peticiones, config, conclusion)
        texto = leer_texto(conclusion)
        self.assertIn('CONCLUSIÓN: MAPEO_ERRORES_PERSONALIZADOS', texto)
        self.assertIn('Decisión aprobada: opción 2', texto)
        self.assertNotIn('Recomendación', texto)
        self.assertEqual(stat.S_IMODE(os.stat(conclusion).st_mode), 0o600)


def ayuda_behavior():
    return {'PathPattern': '/admin/api/*', 'TargetOriginId': 'resenas-admin-lambda', 'ViewerProtocolPolicy': 'redirect-to-https',
            'AllowedMethods': {'Quantity': 7, 'Items': ['GET', 'HEAD', 'OPTIONS', 'PUT', 'PATCH', 'POST', 'DELETE'],
                               'CachedMethods': {'Quantity': 2, 'Items': ['GET', 'HEAD']}},
            'CachePolicyId': '4135ea2d-6df8-44a3-9df3-4b5a84be39ad', 'OriginRequestPolicyId': 'ORP-PRUEBA'}


class DiagnosticoScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='diag-script-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.estado = os.path.join(self.tmp, 'estado.json')
        self.log = os.path.join(self.tmp, 'llamadas.log')
        self.marcas = os.path.join(self.tmp, 'marcas.txt')
        self.curl_req = os.path.join(self.tmp, 'curl.jsonl')
        self.dir = os.path.join(self.tmp, 'diagnostico')
        st = fake.estado_inicial()
        cfg = fake.configuracion_distribucion()
        cfg['Origins']['Items'].append({'Id': 'resenas-admin-lambda', 'DomainName': 'abc.lambda-url.us-east-1.on.aws',
                                        'OriginAccessControlId': 'E1OAC'})
        cfg['Origins']['Quantity'] = 2
        cfg['CustomErrorResponses'] = {'Quantity': 2, 'Items': [
            {'ErrorCode': 403, 'ResponseCode': '404', 'ResponsePagePath': '/error.html', 'ErrorCachingMinTTL': 60},
            {'ErrorCode': 404, 'ResponseCode': '404', 'ResponsePagePath': '/error.html', 'ErrorCachingMinTTL': 60}]}
        cfg['CacheBehaviors'] = {'Quantity': 1, 'Items': [ayuda_behavior()]}
        st['dist']['config'] = cfg
        st['logs'][LOG_GRUPO] = 30
        st['lambda'] = {'config': {'FunctionName': 'ork3d-resenas-admin'}, 'url': {'AuthType': 'AWS_IAM'},
                        'policy': [], 'zip_bytes': 1}
        with open(self.estado, 'w', encoding='utf-8') as f:
            json.dump(st, f)

    def correr(self, extra=None, *, permitir=True):
        env = {'PATH': os.environ.get('PATH', ''), 'AWS_CLI': FAKE_AWS, 'CURL_CMD': FAKE_CURL,
               'FAKE_ESTADO': self.estado, 'FAKE_LOG': self.log, 'FAKE_INVOCACIONES': self.marcas,
               'FAKE_CURL_REQ': self.curl_req, 'DIAG_RUTA': self.dir, 'DIAG_ESPERA': '0', 'DIAG_PAUSA': '3'}
        if permitir:
            env['ALLOW_ADMIN_DIAGNOSTICO'] = '1'
        env.update(extra or {})
        return subprocess.run(['bash', SCRIPT], env=env, cwd=RAIZ, capture_output=True, text=True, timeout=300)

    def operaciones_aws(self):
        if not os.path.exists(self.log):
            return set()
        with open(self.log, encoding='utf-8') as f:
            return {' '.join(l.split()[:2]) for l in f if l.strip() and not l.startswith('curl ')}

    def peticiones_curl(self):
        if not os.path.exists(self.curl_req):
            return []
        with open(self.curl_req, encoding='utf-8') as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_sin_flag_aborta_con_3_sin_tocar_aws_ni_red(self):
        r = self.correr(permitir=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.log))
        self.assertEqual(self.peticiones_curl(), [])

    def test_caso_observado_concluye_mapeo_solo_con_lectura_y_cuatro_peticiones(self):
        # Build anterior a §16.8: la API responde 403 y el borde lo reemplaza.
        r = self.correr({'FAKE_CURL_MAPEO': '1', 'FAKE_CURL_SOLICITAR': '403'})
        visible = r.stdout + r.stderr
        self.assertEqual(r.returncode, 0, visible)
        self.assertIn('post-sin-origin: HTTP 404', visible)
        self.assertIn('CONCLUSIÓN: MAPEO_ERRORES_PERSONALIZADOS', visible)
        conclusion = leer_texto(os.path.join(self.dir, 'conclusion.txt'))
        self.assertIn('CONCLUSIÓN: MAPEO_ERRORES_PERSONALIZADOS', conclusion)
        # Solo lectura: ninguna operación fuera de la lista del diagnóstico.
        self.assertTrue(self.operaciones_aws() <= OPERACIONES_PERMITIDAS, self.operaciones_aws())
        self.assertIn('cloudfront get-distribution-config', self.operaciones_aws())
        # Peticiones: cuatro, solo GET/PUT/POST contra /admin/api/.
        pets = self.peticiones_curl()
        self.assertEqual(len(pets), 4)
        self.assertTrue({p['metodo'] for p in pets} <= {'GET', 'PUT', 'POST'})
        self.assertTrue(all(p['ruta'].startswith('/admin/api/') for p in pets))
        registros = {d['nombre']: d for d in leer_json(os.path.join(self.dir, 'peticiones.json'))}
        self.assertEqual({n: d['invocaciones'] for n, d in registros.items()},
                         {'get-sin-cookie': 1, 'put-control': 1, 'post-sin-origin': 1, 'post-ruta-inexistente': 1})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        for nombre in os.listdir(self.dir):
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, nombre)).st_mode), 0o600, nombre)
        self.assertNotIn('X-Amz-Signature', visible)

    def test_sin_mapeo_el_400_csrf_es_contrato_ok(self):
        r = self.correr()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('CONCLUSIÓN: CONTRATO_CSRF_OK', r.stdout + r.stderr)

    def test_repetir_reutiliza_los_pasos_ya_hechos(self):
        self.assertEqual(self.correr({'FAKE_CURL_MAPEO': '1'}).returncode, 0)
        antes = len(self.peticiones_curl())
        r = self.correr({'FAKE_CURL_MAPEO': '1'})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.peticiones_curl()), antes)
        self.assertIn('ya registradas', r.stdout)

    def test_solo_operaciones_de_lectura_y_metodos_http_en_el_script(self):
        texto = leer_texto(SCRIPT)
        invocaciones = re.findall(r'^\s*ro_aws (\S+) (\S+)', texto, flags=re.MULTILINE)
        self.assertTrue(invocaciones)
        for servicio, operacion in invocaciones:
            self.assertIn(f'{servicio} {operacion}', OPERACIONES_PERMITIDAS)
        self.assertEqual(len(re.findall(r'"\$AWS"', texto)), 1, 'la CLI solo se llama dentro de ro_aws')
        metodos = set(re.findall(r'^\s*peticion \S+ (\w+) ', texto, flags=re.MULTILINE))
        self.assertEqual(metodos, {'GET', 'PUT', 'POST'})


if __name__ == '__main__':
    unittest.main()
