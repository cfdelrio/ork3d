#!/usr/bin/env node
/* ORK3D · panel admin: navegador de SOLO LECTURA para la verificación de producción (infra/admin-ui-verify.sh).
 *
 * Abre /admin/resenas y /admin/entrar sin sesión, a 375, 390 y 1280 px. No se autentica, no hace clic en ningún
 * control (el botón de acceso envía un correo) y aborta toda petición que no sea GET al mismo origen: no hay POST,
 * no hay terceros. Revisa: respuesta 200, que la sección correcta se vea, errores de consola y de página (salvo el 401
 * esperado de /admin/api/sesion sin cookie), violaciones de CSP, desborde horizontal y que los assets de la página
 * carguen con 200.
 *
 * Códigos: 0 todo PASS · 1 algún FAIL · 3 Playwright no disponible (no es un fallo del despliegue: se informa).
 * Variables: ADMIN_UI_BASE (https://ork3d.com), PLAYWRIGHT_PATH (prueba), CHROME_PATH (opcional).
 */
'use strict';

const BASE = process.env.ADMIN_UI_BASE || 'https://ork3d.com';

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright'));
} catch (error) {
  console.log(`INFO  navegador omitido: Playwright no está disponible (${error.code || 'sin módulo'})`);
  process.exit(3);
}

const ANCHOS = [375, 390, 1280];
const PAGINAS = [
  {
    ruta: '/admin/entrar',
    visible: '#confirmar-acceso',
    assets: ['/assets/css/admin.css', '/assets/js/admin-comun.js', '/assets/js/admin-entrar.js']
  },
  {
    ruta: '/admin/resenas',
    visible: '#seccion-acceso',
    assets: ['/assets/css/admin.css', '/assets/js/admin-comun.js', '/assets/js/admin-panel.js']
  }
];

let fallos = 0;

function control(nombre, ok, detalle) {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${nombre}${detalle ? ` — ${detalle}` : ''}`);
  if (!ok) {
    fallos += 1;
  }
}

async function revisar(browser, pagina, ancho) {
  const etiqueta = `${pagina.ruta} a ${ancho}px`;
  const contexto = await browser.newContext({ viewport: { width: ancho, height: 800 }, locale: 'es-AR' });
  const errores = [];
  const csp = [];
  const respuestasAssets = [];
  try {
    // Solo GET al mismo origen. Cualquier otra petición (POST, terceros) se aborta y aparece como error.
    await contexto.route('**/*', (ruta) => {
      const peticion = ruta.request();
      if (peticion.method() === 'GET' && peticion.url().startsWith(`${BASE}/`)) {
        return ruta.continue();
      }
      return ruta.abort();
    });
    const pestana = await contexto.newPage();
    pestana.on('console', (mensaje) => {
      const texto = mensaje.text();
      if (/Content Security Policy|Refused to/.test(texto)) {
        csp.push(texto.slice(0, 160));
        return;
      }
      const origen = mensaje.location() ? mensaje.location().url : '';
      if (mensaje.type() === 'error' && !origen.includes('/admin/api/sesion')) {
        errores.push(texto.slice(0, 160));
      }
    });
    pestana.on('pageerror', (error) => errores.push(error.message.slice(0, 160)));
    pestana.on('response', (respuesta) => {
      if (respuesta.url().startsWith(`${BASE}/assets/`)) {
        respuestasAssets.push({ url: respuesta.url().split('?')[0].replace(BASE, ''), estado: respuesta.status() });
      }
    });

    const respuesta = await pestana.goto(BASE + pagina.ruta, { waitUntil: 'load', timeout: 30000 });
    control(`${etiqueta}: responde 200`, Boolean(respuesta) && respuesta.status() === 200,
      respuesta ? `código ${respuesta.status()}` : 'sin respuesta');

    const visible = await pestana.locator(pagina.visible).isVisible().catch(() => false);
    control(`${etiqueta}: se ve ${pagina.visible}`, visible);

    const desborde = await pestana.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    control(`${etiqueta}: sin desborde horizontal`, desborde <= 0, desborde > 0 ? `${desborde} px` : '');

    control(`${etiqueta}: sin errores de consola ni de página`, errores.length === 0, errores.slice(0, 2).join(' | '));
    control(`${etiqueta}: sin violaciones de CSP`, csp.length === 0, csp.slice(0, 2).join(' | '));

    const faltan = pagina.assets.filter(
      (asset) => !respuestasAssets.some((r) => r.url === asset && r.estado === 200));
    control(`${etiqueta}: assets de la página cargan (200)`, faltan.length === 0,
      faltan.length ? `sin 200: ${faltan.join(', ')}` : '');
  } finally {
    await contexto.close();
  }
}

async function main() {
  const browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || undefined,
    args: ['--no-sandbox']
  });
  try {
    for (const pagina of PAGINAS) {
      for (const ancho of ANCHOS) {
        await revisar(browser, pagina, ancho);
      }
    }
  } finally {
    await browser.close();
  }
  console.log(fallos ? `\nNavegador de solo lectura: ${fallos} FAIL` : '\nNavegador de solo lectura: todo PASS');
  process.exit(fallos ? 1 : 0);
}

main().catch((error) => {
  console.log(`FAIL  navegador de solo lectura: ${String(error.message).split('\n')[0]}`);
  process.exit(1);
});
