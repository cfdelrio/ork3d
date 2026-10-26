#!/usr/bin/env node
/* E2E del caso real de reconocimiento empresarial, en Chromium real contra el sitio del repo servido por dev/serve.py.
 * Se prueba en dos páginas que comparten el mismo video: la landing de regalos (#caso-real) y la home (#empresas).
 *
 * Qué prueba:
 *   - cada sección tiene su jerarquía (eyebrow, h2), las cinco personalizaciones y su CTA y enlace secundario;
 *   - el video va silenciado, en línea, con controles y portada, con WebM antes que MP4, y no se descarga antes de verse;
 *   - al entrar en pantalla se reproduce una vez y queda en el último cuadro (sin loop);
 *   - con prefers-reduced-motion no arranca solo;
 *   - en 375, 390, 768 y 1280 px no hay desborde horizontal, la proporción del video está reservada y el CLS es 0;
 *   - ambas páginas usan el mismo archivo de video y la misma portada (sin duplicados);
 *   - los enlaces de la sección resuelven: el ancla #empresas de la home, la landing de regalos y el destino del CTA.
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
const SHOTS = process.env.SHOTS_DIR || path.join(ROOT, 'dev', 'e2e', 'capturas');
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const VISTAS = [375, 390, 768, 1280];
const CHIPS = ['Nombre', 'Mensaje', 'Colores', 'Identidad de marca', 'Símbolos del logro'];
const VIDEO_WEBM = '/assets/video/caso-real/reconocimiento-empresarial.webm';
const VIDEO_MP4 = '/assets/video/caso-real/reconocimiento-empresarial.mp4';
const PORTADA = '/assets/img/caso-real/reconocimiento-empresarial-poster.webp';
const RECONOCIMIENTOS = '/reconocimientos-empresariales';

// Cada página del caso real. La home reemplaza el CTA de cotización por WhatsApp, que es su flujo comercial.
const PAGINAS = [
  {
    // El caso completo vive en la landing evergreen. La landing de regalos solo tiene la versión breve.
    nombre: 'reconocimientos', ruta: '/reconocimientos-empresariales.html', seccion: '#caso-real',
    cta: { texto: 'Pedir una propuesta', whatsapp: true },
    secundario: { texto: 'Ver opiniones de clientes', href: '/opiniones' },
  },
  {
    nombre: 'home', ruta: '/', seccion: '#empresas',
    cta: { texto: 'Pedir una propuesta', whatsapp: true },
    secundario: { texto: 'Ver reconocimientos empresariales', href: RECONOCIMIENTOS },
  },
];

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

async function abrir(browser, ancho, { ruta = PAGINAS[0].ruta, reducir = false } = {}) {
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
  await page.goto(BASE + ruta, { waitUntil: 'load' });
  return { ctx, page, errores, medios };
}

const video = (p) => p.locator('[data-caso-video]');
const seccion = (p, pagina) => p.locator(pagina.seccion);
const estadoVideo = (p) => p.evaluate(() => {
  const v = document.querySelector('[data-caso-video]');
  return { paused: v.paused, ended: v.ended, t: v.currentTime, loop: v.loop };
});

for (const pagina of PAGINAS) {
  const n = pagina.nombre;

  caso(`${n}: la sección tiene su jerarquía, las cinco personalizaciones y los dos CTA`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, 1280, { ruta: pagina.ruta });
    try {
      const sec = seccion(page, pagina);
      afirmar(await sec.count() === 1, `falta la sección ${pagina.seccion}`);
      afirmar((await sec.locator('.caso-real__eyebrow').innerText()).trim() === 'PROYECTO REAL · RECONOCIMIENTO EMPRESARIAL',
        'el eyebrow no coincide');
      afirmar((await sec.locator('h2').first().innerText()).trim() === 'Un logro merece algo diseñado especialmente para quien lo consiguió.',
        'el título principal no coincide');
      const chips = await sec.locator('.caso-real__chips li').allInnerTexts();
      afirmar(JSON.stringify(chips) === JSON.stringify(CHIPS), `chips: ${JSON.stringify(chips)}`);

      const cta = sec.locator('a.btn--primary');
      afirmar((await cta.innerText()).trim() === pagina.cta.texto, 'texto del CTA principal');
      if (pagina.cta.whatsapp) {
        afirmar((await cta.getAttribute('data-wa')) !== null, 'el CTA de la home debe usar el flujo de WhatsApp');
        await page.waitForFunction((sel) => {
          const a = document.querySelector(sel);
          return a && a.getAttribute('href') && a.getAttribute('href').startsWith('https://wa.me/');
        }, `${pagina.seccion} a.btn--primary`);
        const href = await cta.getAttribute('href');
        afirmar(decodeURIComponent(href).includes('pedir una propuesta de reconocimiento empresarial'),
          'el mensaje de WhatsApp no tiene el contexto del caso');
      } else {
        afirmar((await cta.getAttribute('href')) === pagina.cta.href, `el CTA debe apuntar a ${pagina.cta.href}`);
        afirmar((await cta.getAttribute('data-track')) === pagina.cta.track, 'el CTA debe registrar su evento');
        afirmar(await page.locator(pagina.cta.href).count() === 1, `no existe el destino ${pagina.cta.href}`);
      }

      const secundario = sec.locator('a.btn--ghost');
      afirmar((await secundario.innerText()).trim() === pagina.secundario.texto, 'texto del enlace secundario');
      afirmar((await secundario.getAttribute('href')) === pagina.secundario.href, 'destino del enlace secundario');
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });

  caso(`${n}: el video va silenciado, en línea, con controles y portada, WebM antes que MP4, y sin descargarse antes de verse`, async (browser) => {
    const { ctx, page, errores, medios } = await abrir(browser, 1280, { ruta: pagina.ruta });
    try {
      const atributos = await video(page).evaluate((v) => ({
        muted: v.muted, playsinline: v.hasAttribute('playsinline'), controls: v.controls, autoplay: v.autoplay,
        loop: v.loop, preload: v.getAttribute('preload'), poster: v.getAttribute('poster'), dataPoster: v.getAttribute('data-poster'),
        fuentes: [...v.querySelectorAll('source')].map((s) => s.getAttribute('type')),
      }));
      afirmar(atributos.muted && atributos.playsinline && atributos.controls, `atributos: ${JSON.stringify(atributos)}`);
      afirmar(!atributos.autoplay && !atributos.loop, 'el video no debe llevar autoplay ni loop en el HTML');
      afirmar(atributos.preload === 'none', 'preload debe ser none');
      afirmar(atributos.poster === null, 'la portada no debe pedirse en la carga inicial (va en data-poster)');
      afirmar(atributos.dataPoster === PORTADA, `portada: ${atributos.dataPoster}`);
      afirmar(JSON.stringify(atributos.fuentes) === JSON.stringify(['video/webm', 'video/mp4']), `fuentes: ${atributos.fuentes}`);
      afirmar(medios.length === 0, `el video se descargó antes de verse: ${medios.join(', ')}`);
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });

  caso(`${n}: al entrar en pantalla se reproduce una vez y queda en el último cuadro`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, 1280, { ruta: pagina.ruta });
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

  caso(`${n}: con prefers-reduced-motion no arranca solo; la portada y los controles quedan`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, 375, { ruta: pagina.ruta, reducir: true });
    try {
      await page.locator('.caso-real__marco').first().scrollIntoViewIfNeeded();
      await page.waitForTimeout(2500);
      const e = await estadoVideo(page);
      afirmar(e.paused && e.t === 0, `con reducción de movimiento no debe arrancar: pausado=${e.paused}, t=${e.t}`);
      // En móvil el control para reproducir es el botón; en escritorio, los controles nativos del video.
      const controlDisponible = (await video(page).evaluate((v) => v.controls)) || (await page.locator('[data-caso-play]').isVisible());
      afirmar(controlDisponible, 'debe quedar algún control para reproducir');
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });

  for (const ancho of VISTAS) {
    caso(`${n}: sin desborde, con la proporción del video reservada y sin corrimientos (${ancho} px)`, async (browser) => {
      const { ctx, page, errores } = await abrir(browser, ancho, { ruta: pagina.ruta });
      try {
        const d = await page.evaluate(() => ({ visible: document.documentElement.clientWidth, total: document.documentElement.scrollWidth }));
        afirmar(d.total <= d.visible, `desborde horizontal de ${d.total - d.visible} px a ${ancho} px`);
        // El marco reserva la proporción: en móvil el video arranca oculto, pero el espacio ya está reservado.
        const caja = await page.locator('.caso-real__marco').first().boundingBox();
        afirmar(Math.abs(caja.width / caja.height - 464 / 832) < 0.01,
          `proporción del marco ${(caja.width / caja.height).toFixed(3)}, se esperaba ${(464 / 832).toFixed(3)}`);
        await page.locator('.caso-real__marco').first().scrollIntoViewIfNeeded();
        await page.waitForTimeout(1500);
        const cls = await page.evaluate(() => window.__cls);
        afirmar(cls < 0.01, `CLS ${cls.toFixed(4)} a ${ancho} px`);
        fs.mkdirSync(SHOTS, { recursive: true });
        const nombreCaptura = pagina.ruta === '/' ? 'home' : 'regalos';
        await seccion(page, pagina).screenshot({ path: path.join(SHOTS, `caso-real-${nombreCaptura}-${ancho}.png`) });
        afirmar(errores.length === 0, errores.join(' | '));
      } finally {
        await ctx.close();
      }
    });
  }
}

for (const pagina of PAGINAS) {
  caso(`${pagina.nombre}: en móvil el video no se descarga hasta tocar el botón y entonces se reproduce`, async (browser) => {
    const { ctx, page, errores, medios } = await abrir(browser, 375, { ruta: pagina.ruta });
    try {
      await page.locator('.caso-real__marco').first().scrollIntoViewIfNeeded();
      await page.waitForTimeout(2500);
      const oculto = await video(page).evaluate((v) => v.hidden);
      const boton = page.locator('[data-caso-play]');
      afirmar(oculto, 'en móvil el video debe quedar oculto hasta tocar el botón');
      afirmar(await boton.isVisible(), 'en móvil debe verse el botón de reproducción');
      afirmar(medios.length === 0, `el video se descargó sin tocar el botón: ${medios.join(', ')}`);
      await boton.click();
      await page.waitForFunction(() => document.querySelector('[data-caso-video]').currentTime > 0.3, null, { timeout: 8000 });
      afirmar(!(await video(page).evaluate((v) => v.hidden)), 'tras tocar el botón el video debe mostrarse');
      afirmar(!(await boton.isVisible()), 'tras tocar el botón el botón debe ocultarse');
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });

  caso(`${pagina.nombre}: en escritorio el botón de reproducción queda oculto y el video arranca al entrar en pantalla`, async (browser) => {
    const { ctx, page, errores } = await abrir(browser, 1280, { ruta: pagina.ruta });
    try {
      afirmar(!(await page.locator('[data-caso-play]').isVisible()), 'en escritorio no debe verse el botón');
      await video(page).scrollIntoViewIfNeeded();
      await page.waitForFunction(() => document.querySelector('[data-caso-video]').currentTime > 0.3, null, { timeout: 8000 });
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  });
}

caso('reconocimientos: video, explicación, foto, personalización y CTA en ese orden, sin superposiciones y sin datos de la persona ni de la marca en el texto', async (browser) => {
  for (const ancho of [390, 1440]) {
    const { ctx, page, errores } = await abrir(browser, ancho, { ruta: PAGINAS[0].ruta });
    try {
      const sec = page.locator('#caso-real');
      await sec.scrollIntoViewIfNeeded();
      await page.waitForFunction(() => {
        const i = document.querySelector('#caso-real .caso-real__foto img');
        return i && i.complete && i.naturalWidth > 0;
      }, null, { timeout: 15000 });
      const cajas = await page.evaluate(() => ['.caso-real__figura', '.caso-real__explicacion', '.caso-real__fotos', '.caso-real__detalles']
        .map((s) => {
          const r = document.querySelector('#caso-real ' + s).getBoundingClientRect();
          return { s, x: r.left, y: r.top + scrollY, r: r.right, b: r.bottom + scrollY };
        }));
      if (ancho === 390) {
        const ys = cajas.map((c) => c.y);
        afirmar(ys.every((y, i) => i === 0 || y > ys[i - 1]), `orden de lectura en móvil incorrecto: ${ys}`);
      }
      for (let i = 0; i < cajas.length; i++) {
        for (let j = i + 1; j < cajas.length; j++) {
          const a = cajas[i];
          const c = cajas[j];
          const solapan = a.x < c.r && c.x < a.r && a.y < c.b && c.y < a.b;
          afirmar(!solapan, `${a.s} se superpone con ${c.s} a ${ancho} px`);
        }
      }
      // Sin nombres, montos ni marca del cliente en texto, alt ni etiquetas: el material visual sí los muestra.
      const texto = await sec.evaluate((el) => [
        el.innerText,
        ...[...el.querySelectorAll('[alt], [aria-label]')].flatMap((e) => [e.getAttribute('alt'), e.getAttribute('aria-label')]),
      ].filter(Boolean).join(' | '));
      const datosSensibles = /Escobar|Gonzalo|Emprendimiento|PROHIT|\bHIT\b|50k|\$\s?50|SOY CAPAZ/i;
      afirmar(!datosSensibles.test(texto), `datos de la persona o la marca en la sección: ${texto.match(datosSensibles)}`);
      afirmar(errores.length === 0, errores.join(' | '));
    } finally {
      await ctx.close();
    }
  }
});

caso('ambas páginas usan el mismo archivo de video y la misma portada, sin duplicados', async (browser) => {
  const fuentesDe = async (ruta) => {
    const { ctx, page, errores } = await abrir(browser, 1280, { ruta });
    try {
      const datos = await video(page).evaluate((v) => ({
        poster: v.getAttribute('data-poster'),
        fuentes: [...v.querySelectorAll('source')].map((s) => s.getAttribute('src')),
      }));
      afirmar(errores.length === 0, errores.join(' | '));
      return datos;
    } finally {
      await ctx.close();
    }
  };
  const landing = await fuentesDe(PAGINAS[0].ruta);
  const home = await fuentesDe(PAGINAS[1].ruta);
  afirmar(JSON.stringify(landing) === JSON.stringify(home), `fuentes distintas: ${JSON.stringify(landing)} vs ${JSON.stringify(home)}`);
  afirmar(JSON.stringify(home.fuentes) === JSON.stringify([VIDEO_WEBM, VIDEO_MP4]), `fuentes de la home: ${home.fuentes}`);
  afirmar(home.poster === PORTADA, `portada de la home: ${home.poster}`);
});

caso('la home enlaza la sección con #empresas y la landing evergreen responde', async (browser) => {
  const { ctx, page, errores } = await abrir(browser, 1280, { ruta: '/' });
  try {
    const anclas = await page.locator('a[href="#empresas"]').count();
    afirmar(anclas >= 1, 'ningún enlace apunta a #empresas');
    afirmar(await page.locator('#empresas').count() === 1, 'el ancla #empresas no existe');
    const enlace = page.locator('#empresas a.btn--ghost');
    afirmar((await enlace.getAttribute('href')) === RECONOCIMIENTOS, 'la home debe enlazar a la landing evergreen');
    const r = await page.request.get(BASE + RECONOCIMIENTOS);
    afirmar(r.status() === 200, `${RECONOCIMIENTOS} responde ${r.status()}`);
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
