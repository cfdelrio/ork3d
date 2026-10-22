#!/usr/bin/env node
/* ORK3D · /opiniones en producción, en Chromium real. SOLO LECTURA: navega y pide archivos, no escribe nada.
 *
 * Compara lo que se ve con el export real que sirve el sitio:
 *   - la página responde 200 y tiene un único h1;
 *   - el resumen deja de estar "cargando" en menos de 20 s;
 *   - el promedio y la cantidad del resumen coinciden con el export (mismo cálculo que el sitio);
 *   - hay una tarjeta por cada reseña con rating válido;
 *   - no hay errores de consola ni respuestas 4xx/5xx del mismo origen.
 *
 * Uso: node infra/opiniones-navegador.js
 * Variables: BASE (por defecto https://ork3d.com), SHOTS_DIR (capturas opcionales), PLAYWRIGHT_PATH, CHROME_PATH.
 * Códigos: 0 todo PASS · 1 algún FAIL · 2 Playwright o Chromium no disponibles (no es un fallo del sitio).
 */
const path = require('path');
const fs = require('fs');

let chromium;
try {
  ({ chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright'));
} catch (e) {
  console.error('No está Playwright: instalarlo o definir PLAYWRIGHT_PATH. Este chequeo no se pudo ejecutar.');
  process.exit(2);
}

const BASE = process.env.BASE || 'https://ork3d.com';
const CHROME = process.env.CHROME_PATH || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const SHOTS = process.env.SHOTS_DIR || null;

function promedioTexto(valores) {
  const suma = valores.reduce((acc, v) => acc + v, 0);
  return (Math.round((suma / valores.length) * 10) / 10).toFixed(1).replace('.', ',');
}

(async () => {
  let browser;
  try {
    browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  } catch (e) {
    console.error('No se pudo abrir Chromium: ' + e.message.split('\n')[0]);
    process.exit(2);
  }
  const fallos = [];
  const control = (nombre, ok, detalle) => {
    console.log((ok ? 'PASS  ' : 'FAIL  ') + nombre + (detalle ? ' — ' + detalle : ''));
    if (!ok) fallos.push(nombre);
  };
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, locale: 'es-AR' });
  const page = await context.newPage();
  const errores = [];
  const respuestasMalas = [];
  page.on('pageerror', (e) => errores.push('excepción: ' + e.message));
  page.on('console', (m) => {
    const url = m.location().url || '';
    if (m.type() === 'error' && url.startsWith(BASE)) errores.push('consola: ' + m.text());
  });
  page.on('response', (r) => {
    if (r.status() >= 400 && r.url().startsWith(BASE)) respuestasMalas.push(r.status() + ' ' + r.url());
  });
  try {
    // El export real, leído igual que lo lee el sitio.
    const exportRes = await context.request.get(BASE + '/resenas/publicas.json');
    const exportDoc = exportRes.ok() ? await exportRes.json() : { items: [] };
    const items = Array.isArray(exportDoc.items) ? exportDoc.items : [];
    const validos = items.map((i) => Number(i && i.rating)).filter((n) => isFinite(n) && n >= 1 && n <= 5);

    const respuesta = await page.goto(BASE + '/opiniones', { waitUntil: 'load' });
    control('/opiniones responde 200', respuesta && respuesta.status() === 200, respuesta ? String(respuesta.status()) : 'sin respuesta');
    control('un único h1', (await page.locator('h1').count()) === 1);

    await page.waitForFunction(() => {
      const l = document.getElementById('resenas-listado');
      return l && l.getAttribute('data-estado') !== 'cargando';
    }, null, { timeout: 20000 }).catch(() => {});
    const estado = await page.locator('#resenas-listado').getAttribute('data-estado');
    control('el resumen deja de cargar', estado !== 'cargando' && estado !== null, 'data-estado=' + estado);

    if (validos.length) {
      const esperado = promedioTexto(validos) + ' de 5 · ' + validos.length + (validos.length === 1 ? ' opinión' : ' opiniones');
      const mostrado = (await page.locator('#resumen-nota .resumen__texto').textContent().catch(() => '')) || '';
      control('promedio y cantidad coinciden con el export', mostrado === esperado, `mostrado "${mostrado}", esperado "${esperado}"`);
      const tarjetas = await page.locator('#resenas-lista > li').count();
      control('una tarjeta por reseña publicada', tarjetas === validos.length, `${tarjetas} tarjetas, ${validos.length} reseñas`);
    } else {
      const texto = (await page.locator('#resumen-nota').textContent().catch(() => '')) || '';
      control('sin reseñas: mensaje vacío, sin 0,0', /Todavía no hay opiniones|No pudimos cargar/.test(texto) && !texto.includes('0,0'), texto.slice(0, 60));
    }

    const fotos = await page.locator('#resenas-lista img').count();
    control('fotos con alt', (await page.locator('#resenas-lista img:not([alt]), #resenas-lista img[alt=""]').count()) === 0, `${fotos} fotos`);
    if (SHOTS) {
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.screenshot({ path: path.join(SHOTS, 'opiniones-produccion.png'), fullPage: true });
    }
  } catch (e) {
    control('recorrido en el navegador', false, e.message.split('\n')[0]);
  } finally {
    control('sin errores de consola ni excepciones', errores.length === 0, errores.slice(0, 3).join(' | '));
    control('sin respuestas 4xx/5xx del mismo origen', respuestasMalas.length === 0, respuestasMalas.slice(0, 3).join(' | '));
    await browser.close();
  }
  if (fallos.length) {
    console.log(`Navegador: ${fallos.length} control(es) en FAIL.`);
    process.exit(1);
  }
  console.log('Navegador: todo PASS.');
  process.exit(0);
})();
