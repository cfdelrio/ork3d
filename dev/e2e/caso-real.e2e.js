#!/usr/bin/env node
/* E2E de la sección "caso real" (reconocimiento empresarial) en /regalos-empresariales-fin-de-ano, en Chromium real
 * contra el sitio del repo servido por dev/serve.py.
 *
 * Qué prueba:
 *   - la sección tiene su jerarquía (eyebrow, h2), las cinco personalizaciones y el CTA al flujo de cotización;
 *   - el video va silenciado, en línea, con controles y portada, con WebM antes que MP4, y no se descarga antes de verse;
 *   - al entrar en pantalla se reproduce una vez y queda en el último cuadro (sin loop);
 *   - con prefers-reduced-motion no arranca solo;
 *   - en 375, 390, 768 y 1280 px no hay desborde horizontal, la proporción del video está reservada y el CLS es 0;
 *   - el CTA lleva a #cotizacion.
 *
 * Uso (desde la raíz del repo):  SHOTS_DIR=<carpeta para capturas> node dev/e2e/caso-real.e2e.js
 * Variables opcionales: PORT (8800), CHROME_PATH, PLAYWRIGHT_PATH.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8800);
const BASE = `http://127.0.0.1:${PORT}`;
const PAGINA = '/regalos-empresariales-fin-de-ano.html';
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const VISTAS = [375, 390, 768, 1280];
const CHIPS = ['Nombre', 'Mensaje', 'Colores', 'Identidad de marca', 'Símbolos del logro'];

const casos = [];
function caso(nombre, fn) { casos.push({ nombre, fn }); }
function afirmar(condicion, mensaje) { if (!condicion) throw new Error(mensaje); }

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let salida = '';
    let listo = false;
    const recoger = (d) => {
      if (listo) return;
      salida += d;
      if (salida.includes('ORK3D dev')) { listo = true; resolver(proc); }
    };
    proc.stdout.on('data', recoger);
    proc.stderr.on('data', recoger);
    proc.on('exit', (code) => rechazar(new Error(`dev/serve.py salió con ${code}: ${salida}`)));
  });
}

// Solo cuentan los errores del propio sitio: los recursos de terceros no dependen de este repo.
function esExterno(url) {
  return !url.startsWith(BASE);
}

async function abrir(browser, ancho, { reducir = false } = {}) {
  const ctx = await browser.newContext({
    viewport: { width: ancho, height: 800 },
    locale: 'es-AR',
    reducedMotion: reducir ? 'reduce' : 'no-preference',
  });
  // CLS acumulado de la página: cada corrimiento sin interacción suma a window.__cls.
  await ctx.addInitScript(() => {
    window.__cls = 0;
    new PerformanceObserver((l) => {
      for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cls += e.value;
    }).observe({ type: 'layout-shift', buffered: true });
  });
  const page = await ctx.newPage();
  const errores = [];
  const medios = [];
  page.on('pageerror', (e) => errores.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !esExterno((m.location() && m.location().url) || BASE)) {
      errores.push('consola: ' + m.text());
    }
  });
  page.on('request', (r) => { if (/\.(webm|mp4)(\?|$)/.test(r.url())) medios.push(r.url()); });
  await page.goto(BASE + PAGINA, { waitUntil: 'load' });
  return { ctx, page, errores, medios };
}

const video = (p) => p.locator('[data-caso-video]');
const estadoVideo = (p) => p.evaluate(() => {
  const v = document.querySelector('[data-caso-video]');
  return { paused: v.paused, ended: v.ended, t: v.currentTime, loop: v.loop };
});

caso('la sección tiene su jerarquía, las cinco personalizaciones y el CTA al flujo de cotización', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1280);
  try {
    const sec = page.locator('#caso-real');
    afirmar(await sec.count() === 1, 'falta la sección #caso-real');
    afirmar((await sec.locator('.caso-real__eyebrow').innerText()).trim() === 'PROYECTO REAL · RECONOCIMIENTO EMPRESARIAL',
      'el eyebrow no coincide');
    afirmar((await sec.locator('h1, h2').first().innerText()).trim() === 'Un logro merece algo diseñado especialmente para quien lo consiguió.',
      'el título principal no coincide');
    const chips = await sec.locator('.caso-real__chips li').allInnerTexts();
    afirmar(JSON.stringify(chips) === JSON.stringify(CHIPS), `chips: ${JSON.stringify(chips)}`);
    const cta = sec.locator('a.btn--primary');
    afirmar((await cta.getAttribute('href')) === '#cotizacion', 'el CTA principal debe apuntar a #cotizacion');
    afirmar((await cta.getAttribute('data-track')) === 'caso_real_cta_click', 'el CTA debe registrar su evento');
    afirmar(await page.locator('#cotizacion').count() === 1, 'no existe el destino #cotizacion');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('el video va silenciado, en línea, con controles y portada, WebM antes que MP4, y sin descargarse antes de verse', async (browser) => {
  const { ctx, page, errores, medios } = await abrir(browser, 1280);
  try {
    const atributos = await video(page).evaluate((v) => ({
      muted: v.muted, playsinline: v.hasAttribute('playsinline'), controls: v.controls, autoplay: v.autoplay,
      loop: v.loop, preload: v.getAttribute('preload'), poster: v.getAttribute('poster'),
      fuentes: [...v.querySelectorAll('source')].map((s) => s.getAttribute('type')),
    }));
    afirmar(atributos.muted && atributos.playsinline && atributos.controls, `atributos: ${JSON.stringify(atributos)}`);
    afirmar(!atributos.autoplay && !atributos.loop, 'el video no debe llevar autoplay ni loop en el HTML');
    afirmar(atributos.preload === 'none', 'preload debe ser none');
    afirmar(atributos.poster === '/assets/img/caso-real/reconocimiento-empresarial-poster.webp', `portada: ${atributos.poster}`);
    afirmar(JSON.stringify(atributos.fuentes) === JSON.stringify(['video/webm', 'video/mp4']), `fuentes: ${atributos.fuentes}`);
    afirmar(medios.length === 0, `el video se descargó antes de verse: ${medios.join(', ')}`);
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('al entrar en pantalla se reproduce una vez y queda en el último cuadro', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1280);
  try {
    await video(page).scrollIntoViewIfNeeded();
    await page.waitForFunction(() => document.querySelector('[data-caso-video]').ended, null, { timeout: 8000 });
    const e = await estadoVideo(page);
    afirmar(e.t > 2.4, `el video terminó en ${e.t.toFixed(2)} s, se esperaban ~2.6 s`);
    afirmar(e.paused && !e.loop, 'tras terminar debe quedar pausado en el último cuadro, sin loop');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

caso('con prefers-reduced-motion no arranca solo; la portada y los controles quedan', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 375, { reducir: true });
  try {
    await video(page).scrollIntoViewIfNeeded();
    await page.waitForTimeout(2500);
    const e = await estadoVideo(page);
    afirmar(e.paused && e.t === 0, `con reducción de movimiento no debe arrancar: pausado=${e.paused}, t=${e.t}`);
    afirmar(await video(page).evaluate((v) => v.controls), 'los controles deben quedar disponibles');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

for (const ancho of VISTAS) {
  caso(`sin desborde, con la proporción del video reservada y sin corrimientos (${ancho} px)`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, ancho);
    try {
      const d = await page.evaluate(() => ({ visible: document.documentElement.clientWidth, total: document.documentElement.scrollWidth }));
      afirmar(d.total <= d.visible, `desborde horizontal de ${d.total - d.visible} px a ${ancho} px`);
      const caja = await video(page).boundingBox();
      afirmar(Math.abs(caja.width / caja.height - 464 / 832) < 0.01,
        `proporción del video ${(caja.width / caja.height).toFixed(3)}, se esperaba ${(464 / 832).toFixed(3)}`);
      await video(page).scrollIntoViewIfNeeded();
      await page.waitForTimeout(1500);
      const cls = await page.evaluate(() => window.__cls);
      afirmar(cls < 0.01, `CLS ${cls.toFixed(4)} a ${ancho} px`);
      fs.mkdirSync(SHOTS, { recursive: true });
      await page.locator('#caso-real').screenshot({ path: path.join(SHOTS, `caso-real-${ancho}.png`) });
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });
}

caso('el CTA principal lleva a la cotización', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1280);
  try {
    await page.locator('#caso-real a.btn--primary').click();
    await page.waitForFunction(() => location.hash === '#cotizacion');
    afirmar(await page.locator('#cotizacion').isVisible(), 'la sección de cotización debe quedar visible');
    afirmar(errores.length === 0, errores.join(' | '));
  } finally {
    await ctx.close();
  }
});

async function main() {
  const servidor = await arrancarServidor();
  const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
  let fallos = 0;
  try {
    for (const c of casos) {
      try {
        await c.fn(browser);
        console.log(`PASS  ${c.nombre}`);
      } catch (e) {
        fallos += 1;
        console.log(`FAIL  ${c.nombre}\n      ${String(e.message).split('\n')[0]}`);
      }
    }
  } finally {
    await browser.close();
    servidor.kill();
  }
  console.log(`\nCaso real E2E: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
