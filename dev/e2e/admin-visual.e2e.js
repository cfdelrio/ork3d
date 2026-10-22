#!/usr/bin/env node
/* Capturas y comprobaciones visuales del panel admin de reseñas contra dev/serve.py.
 *
 * Crea reseñas de prueba por la API pública, inicia sesión por el enlace del correo simulado, deja una reseña en cada
 * estado moderando desde la interfaz y captura cada pantalla en desktop y móvil. Las pantallas de error y de sesión
 * vencida se simulan con page.route (el servidor no las produce a voluntad).
 *
 * Uso (desde la raíz del repo):
 *   NODE_PATH=/opt/node22/lib/node_modules node dev/e2e/admin-visual.e2e.js
 * Variables: PORT (8797), SHOTS_DIR (dev/e2e/capturas/admin-ux), PREFIJO (antes|despues; nombre de las capturas),
 * CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8797);
const BASE = `http://127.0.0.1:${PORT}`;
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas', 'admin-ux');
const PREFIJO = process.env.PREFIJO || 'antes';
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const ASUNTO_ACCESO = 'Acceso al panel de reseñas ORK3D';
const CODIGO_RE = /^[A-Za-z0-9_-]{43}$/;
const ID_RE = /^[a-f0-9]{32}$/;
const DESKTOP = { width: 1280, height: 900 };
const MOVIL = { width: 390, height: 844 };

fs.mkdirSync(SHOTS, { recursive: true });

function esperar(condicion, mensaje) {
  if (!condicion) throw new Error(mensaje);
}

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let salida = '';
    proc.stdout.on('data', (d) => { salida += d; if (salida.includes('ORK3D dev')) resolver(proc); });
    proc.stderr.on('data', (d) => { salida += d; });
    proc.on('exit', (code) => rechazar(new Error(`dev/serve.py salió con ${code}: ${salida}`)));
  });
}

async function getJson(ruta) {
  return (await fetch(BASE + ruta)).json();
}

async function crearResena(token, datos) {
  const r = await fetch(`${BASE}/api/resenas/resenas`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token, display_mode: 'inicial', permission_to_publish: true, ...datos })
  });
  esperar(r.ok, `la API pública rechazó la reseña "${datos.comment}": ${r.status}`);
}

async function idDeResena(marcador) {
  const estado = await getJson('/dev/state');
  const r = estado.resenas.find((x) => (x.comment || '').includes(marcador));
  esperar(r && ID_RE.test(r.id || ''), `no encontré la reseña ${marcador}`);
  return r.id;
}

async function contexto(browser, viewport, sesion) {
  const ctx = await browser.newContext({ viewport, locale: 'es-AR', storageState: sesion });
  const page = await ctx.newPage();
  const errores = [];
  page.on('pageerror', (e) => errores.push(`pageerror: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') errores.push(`console: ${m.text()}`); });
  return { ctx, page, errores };
}

async function captura(page, nombre) {
  await page.waitForTimeout(250);
  await page.screenshot({ path: path.join(SHOTS, `${PREFIJO}-${nombre}.png`), fullPage: true });
  console.log(`captura ${PREFIJO}-${nombre}.png`);
}

// Confirmar una acción del detalle: clic en el botón, esperar el plazo del diálogo y confirmar.
async function accionConfirmada(page, accion) {
  await page.locator(`[data-accion="${accion}"]`).click();
  await page.locator('#dialogo[open]').waitFor({ state: 'visible' });
  await page.waitForFunction(() => !document.getElementById('dialogo-confirmar').disabled, null, { timeout: 5000 });
  await page.click('#dialogo-confirmar');
  await page.waitForFunction(() => !document.getElementById('dialogo').open, null, { timeout: 5000 });
}

async function iniciarSesion(browser) {
  const { ctx, page, errores } = await contexto(browser, DESKTOP);
  await page.goto(`${BASE}/admin/resenas`);
  await page.locator('#pedir-acceso').waitFor({ state: 'visible', timeout: 8000 });
  await page.click('#pedir-acceso');
  await page.locator('#aviso-acceso').filter({ hasText: 'Si corresponde' }).waitFor({ state: 'visible' });
  const correos = await getJson('/dev/correos');
  const acceso = correos.filter((c) => c.subject === ASUNTO_ACCESO).pop();
  esperar(acceso, 'no llegó el correo de acceso');
  const prefijo = `${BASE}/admin/entrar#c=`;
  const cuerpo = `${acceso.text || ''}\n${acceso.html || ''}`;
  const pos = cuerpo.indexOf(prefijo);
  esperar(pos >= 0, 'el correo no trae el enlace');
  const codigo = cuerpo.slice(pos + prefijo.length, pos + prefijo.length + 43);
  esperar(CODIGO_RE.test(codigo), 'código con formato inesperado');
  await page.goto(`${BASE}/admin/entrar#c=${codigo}`);
  await page.locator('#confirmar-acceso').waitFor({ state: 'visible' });
  await page.click('#confirmar-acceso');
  await page.waitForURL(`${BASE}/admin/resenas`);
  await page.locator('#seccion-panel').waitFor({ state: 'visible', timeout: 8000 });
  const sesion = await ctx.storageState();
  await ctx.close();
  return { sesion, errores };
}

async function main() {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  const errores = [];
  try {
    const tokens = await getJson('/dev/tokens');
    await crearResena(tokens.ok1, { rating: 5, aspects: ['calidad', 'diseno'], name: 'Ana Gómez',
      comment: 'Muy buena calidad del trofeo; el grabado quedó nítido. visual-A' });
    await crearResena(tokens.ok2, { rating: 4, aspects: ['atencion'], name: 'Carla Ruiz',
      comment: 'Llegó a tiempo y el acabado es prolijo, aunque la base podría ser un poco más firme. visual-B',
      private_feedback: 'Interno: revisar el proveedor de bases', contact_requested: true, contact: 'carla@example.test' });
    await crearResena(tokens.ok3, { rating: 2, aspects: ['diseno'], name: 'Diego Ros',
      comment: 'Esperaba otro color. visual-C' });
    await crearResena(tokens.ok4, { rating: 5, aspects: ['resultado'], name: 'Elena Torres',
      comment: 'Superó lo que esperaba para el torneo del club. visual-D' });

    const { sesion } = await iniciarSesion(browser);
    const ids = { A: await idDeResena('visual-A'), B: await idDeResena('visual-B'),
      C: await idDeResena('visual-C'), D: await idDeResena('visual-D') };

    // Estados: A publicada, B oculta, C rechazada, D pendiente. Todo por la interfaz, con su confirmación.
    let s = await contexto(browser, DESKTOP, sesion);
    await s.page.goto(`${BASE}/admin/resenas/${ids.A}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await accionConfirmada(s.page, 'aprobar');
    await s.page.waitForTimeout(300);
    await s.page.goto(`${BASE}/admin/resenas/${ids.B}`);
    await accionConfirmada(s.page, 'aprobar');
    await s.page.waitForTimeout(300);
    await accionConfirmada(s.page, 'ocultar');
    await s.page.goto(`${BASE}/admin/resenas/${ids.C}`);
    await accionConfirmada(s.page, 'rechazar');
    errores.push(...s.errores);
    await s.ctx.close();

    // Bandeja desktop y móvil: pendientes (por defecto) y publicadas.
    s = await contexto(browser, DESKTOP, sesion);
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    await captura(s.page, 'bandeja-pendientes-desktop');
    await s.page.click('#tab-approved');
    await s.page.waitForTimeout(400);
    await captura(s.page, 'bandeja-publicadas-desktop');
    await s.page.goto(`${BASE}/admin/resenas/${ids.D}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await captura(s.page, 'detalle-pendiente-desktop');
    await s.page.locator('[data-accion="aprobar"]').click();
    await s.page.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await captura(s.page, 'confirmacion-desktop');
    await s.page.keyboard.press('Escape');
    await s.page.goto(`${BASE}/admin/resenas/${ids.A}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await captura(s.page, 'detalle-publicada-desktop');
    errores.push(...s.errores);
    await s.ctx.close();

    s = await contexto(browser, MOVIL, sesion);
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    await captura(s.page, 'bandeja-pendientes-movil');
    await s.page.click('#tab-approved');
    await s.page.waitForTimeout(400);
    await captura(s.page, 'bandeja-publicadas-movil');
    await s.page.goto(`${BASE}/admin/resenas/${ids.B}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await captura(s.page, 'detalle-oculta-movil');
    await s.page.goto(`${BASE}/admin/resenas/${ids.A}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await captura(s.page, 'detalle-publicada-movil');
    await s.page.goto(`${BASE}/admin/resenas/${ids.D}`);
    await s.page.locator('#detalle').waitFor({ state: 'visible' });
    await captura(s.page, 'detalle-pendiente-movil');
    await s.page.locator('[data-accion="aprobar"]').click();
    await s.page.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await captura(s.page, 'confirmacion-movil');
    await s.page.keyboard.press('Escape');
    errores.push(...s.errores);
    await s.ctx.close();

    // Formulario de invitación (desktop y móvil) con los campos llenos.
    for (const [nombre, vp] of [['desktop', DESKTOP], ['movil', MOVIL]]) {
      s = await contexto(browser, vp, sesion);
      await s.page.goto(`${BASE}/admin/resenas`);
      await s.page.locator('#form-invitacion').waitFor({ state: 'visible' });
      await s.page.fill('#campo-pedido', 'ORK-VISUAL-0001');
      await s.page.fill('#campo-producto', 'trofeo-personalizado');
      await s.page.fill('#campo-etiqueta', 'Trofeo personalizado');
      await s.page.fill('#campo-categoria', 'torneos');
      await captura(s.page, `invitacion-${nombre}`);
      errores.push(...s.errores);
      await s.ctx.close();
    }

    // Estados de error y de sesión vencida (simulados con route).
    s = await contexto(browser, MOVIL, sesion);
    await s.page.route('**/admin/api/resenas?estado=pending', (r) => r.abort());
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#reintentar-bandeja').waitFor({ state: 'visible', timeout: 8000 });
    await captura(s.page, 'error-bandeja-movil');
    await s.ctx.close();

    s = await contexto(browser, MOVIL, sesion);
    await s.page.route('**/admin/api/resenas?estado=pending', (r) => r.fulfill({ status: 401,
      contentType: 'application/json', body: '{"ok": false, "error": "no_autenticado"}' }));
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#seccion-acceso').waitFor({ state: 'visible', timeout: 8000 });
    await captura(s.page, 'sesion-vencida-movil');
    await s.ctx.close();

    // Login: sin sesión, y la confirmación del enlace.
    s = await contexto(browser, DESKTOP);
    await s.page.goto(`${BASE}/admin/resenas`);
    await s.page.locator('#pedir-acceso').waitFor({ state: 'visible' });
    await captura(s.page, 'login-desktop');
    await s.ctx.close();
    s = await contexto(browser, MOVIL);
    await s.page.goto(`${BASE}/admin/entrar`);
    await s.page.locator('#confirmar-acceso').waitFor({ state: 'visible' });
    await captura(s.page, 'entrar-sin-enlace-movil');
    await s.ctx.close();
    console.log(`errores de consola/página: ${errores.length}${errores.length ? ' — ' + errores.slice(0, 3).join(' | ') : ''}`);
    console.log(`capturas en ${SHOTS}`);
    esperar(errores.length === 0, `hay errores de consola o de página en el panel: ${errores.length}`);
  } finally {
    await browser.close();
    servidor.kill();
  }
}

main().catch((e) => { console.error(e.message.split('\n')[0]); process.exit(1); });
