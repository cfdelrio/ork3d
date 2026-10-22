#!/usr/bin/env node
/* E2E de experiencia visual del panel admin: lo que se ve y se usa, en cada ancho.
 *
 * Uso (desde la raíz del repo):
 *   NODE_PATH=/opt/node22/lib/node_modules node dev/e2e/admin-ux.e2e.js
 *
 * Chromium real contra dev/serve.py. La API admin se simula en el navegador (page.route): ningún dato real se toca
 * y ningún POST sale. Variables: PORT (8796), CHROME_PATH, PLAYWRIGHT_PATH.
 *
 * Qué prueba:
 *   - sin desborde horizontal de la página, en bandeja, detalle, diálogo y acceso, a 320, 375, 390, 430, 768 y 1280 px;
 *   - objetivos táctiles de al menos 44 × 44 px (botones, pestañas, enlaces y campos);
 *   - contraste de texto WCAG AA (4,5:1), con el mismo método que opiniones.e2e.js;
 *   - pestañas completas en escritorio (ninguna cortada al borde);
 *   - foco visible en cada control al que llega Tab (375 y 1280 px);
 *   - diálogo con teclado: el foco entra, Tab no sale, Escape cierra y devuelve el foco al disparador;
 *   - estático: ni style="" ni innerHTML ni scripts inline en las páginas y scripts del panel, y CSP en las dos páginas.
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8796);
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
const ANCHOS = [320, 375, 390, 430, 768, 1280];
const ANCHOS_FOCO = [375, 1280];
const ANCHO_ESCRITORIO = 940;
const ALTO = 800;
const MINIMO_TACTIL = 44;
const CONTRASTE_MINIMO = 4.5;

const ID = {
  A: '0123456789abcdef0123456789abcdef', // pendiente
  B: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', // publicada
  C: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'  // oculta
};

const casos = [];
function caso(nombre, fn) { casos.push({ nombre, fn }); }
function esperar(condicion, mensaje) { if (!condicion) throw new Error(mensaje); }

function arrancarServidor() {
  return new Promise((resolver, rechazar) => {
    const proc = spawn('python3', [path.join(ROOT, 'dev', 'serve.py'), String(PORT)], { cwd: ROOT });
    let arranque = '';
    let listo = false;
    const recoger = (d) => {
      if (listo) return;
      arranque += d;
      if (arranque.includes('ORK3D dev')) { listo = true; resolver(proc); }
    };
    proc.stdout.on('data', recoger);
    proc.stderr.on('data', recoger);
    proc.on('exit', (code) => { if (!listo) rechazar(new Error(`dev/serve.py salió con ${code}`)); });
  });
}

// ── API simulada ───────────────────────────────────────────────────────────────

function estadoInicial() {
  return {
    A: { id: ID.A, status: 'pending' },
    B: { id: ID.B, status: 'approved' },
    C: { id: ID.C, status: 'hidden' }
  };
}

// Texto largo a propósito: así se mide el ajuste de línea en los anchos angostos.
function reseña(r) {
  return {
    id: r.id, status: r.status, rating: 5,
    comment: 'Superó lo que esperaba para el torneo del club, con un acabado muy prolijo y una entrega puntual.',
    display_name: 'Elena T.', product_label: 'Trofeo acrílico personalizado con base de madera', category: 'trofeos',
    created_at: '2026-10-22T13:10:17Z',
    moderated_at: r.status === 'pending' ? null : '2026-10-22T13:12:31Z',
    published_at: r.status === 'approved' ? '2026-10-22T13:12:31Z' : null,
    permission_to_publish: true, tiene_foto: false, verified: true, aviso: 'enviado',
    aspects: ['resultado', 'calidad'], private_feedback: 'Interno: el pedido se entregó con una semana de demora',
    pedido: 'ORK-UX-0001', contact_requested: false, contact: ''
  };
}

// Sin sesión responde 401 (la página muestra el acceso). Cualquier POST queda registrado y se rechaza.
function instalarApi(page, estado, sesion) {
  return page.route('**/admin/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const ruta = url.pathname;
    const json = (status, cuerpo) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(cuerpo) });
    if (req.method() === 'GET' && ruta === '/admin/api/sesion') {
      return sesion === 200 ? json(200, { ok: true }) : json(401, { ok: false, error: 'no_autenticado' });
    }
    if (req.method() === 'GET' && ruta === '/admin/api/resenas') {
      const est = url.searchParams.get('estado');
      const lista = Object.values(estado.revs).filter((r) => est === 'all' || r.status === est).map(reseña);
      return json(200, { resenas: lista });
    }
    const m = /^\/admin\/api\/resenas\/([a-f0-9]{32})$/.exec(ruta);
    if (m && req.method() === 'GET') {
      const r = Object.values(estado.revs).find((x) => x.id === m[1]);
      return r ? json(200, { resena: reseña(r) }) : json(404, { ok: false, error: 'no_encontrado' });
    }
    if (req.method() !== 'GET') estado.posts.push(ruta);
    return json(409, { ok: false, error: 'no_permitido_en_pruebas_ux' });
  });
}

async function abrirPagina(browser, ancho, sesion = 200) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: ALTO }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const estado = { revs: estadoInicial(), posts: [] };
  const errores = [];
  page.on('pageerror', (e) => errores.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    // Sin sesión, el navegador registra el 401 de /sesion como error de recurso: es esperado, no un fallo de la página.
    if (m.type() === 'error' && !(sesion === 401 && m.text().startsWith('Failed to load resource'))) {
      errores.push(`console: ${m.text()}`);
    }
  });
  await instalarApi(page, estado, sesion);
  return { ctx, page, estado, errores };
}

function postsDe(s) {
  return s.estado.posts;
}

// ── Pantallas ──────────────────────────────────────────────────────────────────

async function esperarBandeja(p) {
  await p.locator('#lista .tarjeta').first().waitFor({ state: 'visible', timeout: 8000 });
}
async function esperarDetalle(p) {
  await p.locator('#detalle .pildora').waitFor({ state: 'visible', timeout: 8000 });
}

const PANTALLAS = [
  { nombre: 'bandeja', sesion: 200, abrir: async (p) => { await p.goto(`${BASE}/admin/resenas`); await esperarBandeja(p); } },
  { nombre: 'bandeja publicadas', sesion: 200, abrir: async (p) => {
    await p.goto(`${BASE}/admin/resenas`);
    await esperarBandeja(p);
    await p.click('#tab-approved');
    await p.locator('#tab-approved[aria-selected="true"]').waitFor({ state: 'visible' });
    await esperarBandeja(p);
  } },
  { nombre: 'detalle pendiente', sesion: 200, abrir: async (p) => { await p.goto(`${BASE}/admin/resenas/${ID.A}`); await esperarDetalle(p); } },
  { nombre: 'detalle publicada', sesion: 200, abrir: async (p) => { await p.goto(`${BASE}/admin/resenas/${ID.B}`); await esperarDetalle(p); } },
  { nombre: 'detalle oculta', sesion: 200, abrir: async (p) => { await p.goto(`${BASE}/admin/resenas/${ID.C}`); await esperarDetalle(p); } },
  { nombre: 'diálogo de aprobar', sesion: 200, abrir: async (p) => {
    await p.goto(`${BASE}/admin/resenas/${ID.A}`);
    await esperarDetalle(p);
    await p.locator('#acciones-detalle button[data-accion="aprobar"]').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
  } },
  { nombre: 'acceso sin sesión', sesion: 401, abrir: async (p) => {
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#seccion-acceso').waitFor({ state: 'visible', timeout: 8000 });
  } },
  { nombre: 'acceso del enlace', sesion: 401, abrir: async (p) => {
    await p.goto(`${BASE}/admin/entrar`);
    await p.locator('#confirmar-acceso').waitFor({ state: 'visible', timeout: 8000 });
  } }
];

// ── Mediciones en la página ────────────────────────────────────────────────────

async function desbordeHorizontal(p) {
  const d = await p.evaluate(() => ({ visible: document.documentElement.clientWidth, total: document.documentElement.scrollWidth }));
  return Math.max(0, d.total - d.visible);
}

// Controles visibles con menos de 44 px en algún lado. El enlace de salto se mide solo cuando tiene foco.
async function objetivosPequenos(p) {
  return p.evaluate((minimo) => {
    const salida = [];
    for (const el of document.querySelectorAll('a[href], button, input, textarea, select, [role="tab"]')) {
      if (el.classList.contains('adm__saltar') && document.activeElement !== el) continue;
      if (!el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
      const r = el.getBoundingClientRect();
      if (r.width < minimo || r.height < minimo) {
        const nombre = el.id ? `#${el.id}` : (el.textContent || '').trim().slice(0, 24);
        salida.push(`${el.tagName.toLowerCase()} ${nombre} ${Math.round(r.width)}×${Math.round(r.height)}`);
      }
    }
    return salida;
  }, MINIMO_TACTIL);
}

// Contraste de cada nodo con texto propio visible. El fondo se busca subiendo por los ancestros; si hay alfa, se mezcla.
async function contrastesBajos(p) {
  return p.evaluate((minimo) => {
    function parse(c) {
      const m = c.match(/rgba?\(([^)]+)\)/);
      if (!m) return null;
      const v = m[1].split(',').map((s) => parseFloat(s));
      return { r: v[0], g: v[1], b: v[2], a: v.length > 3 ? v[3] : 1 };
    }
    function mezclar(fg, bg) {
      return { r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a),
        b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 };
    }
    function lum(c) {
      const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(c.r) + 0.7152 * f(c.g) + 0.0722 * f(c.b);
    }
    const base = parse(getComputedStyle(document.body).backgroundColor) || { r: 255, g: 255, b: 255, a: 1 };
    function fondo(el) {
      for (let e = el; e; e = e.parentElement) {
        const c = parse(getComputedStyle(e).backgroundColor);
        if (c && c.a > 0) return c.a < 1 ? mezclar(c, base) : c;
      }
      return base;
    }
    const salida = [];
    for (const el of document.querySelectorAll('body *')) {
      const tieneTexto = [...el.childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
      if (!tieneTexto || !el.getClientRects().length || getComputedStyle(el).visibility === 'hidden') continue;
      let fg = parse(getComputedStyle(el).color);
      if (!fg) continue;
      const bg = fondo(el);
      if (fg.a < 1) fg = mezclar(fg, bg);
      const a = lum(fg);
      const b = lum(bg);
      const ratio = (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
      if (ratio < minimo) {
        salida.push(`${el.tagName.toLowerCase()} "${el.textContent.trim().slice(0, 24)}" ${ratio.toFixed(2)}`);
      }
    }
    return salida;
  }, CONTRASTE_MINIMO);
}

async function pestanasCortadas(p) {
  return p.evaluate(() => {
    const tabs = document.getElementById('tabs');
    return !!tabs && tabs.scrollWidth > tabs.clientWidth + 1;
  });
}

// ── Casos ──────────────────────────────────────────────────────────────────────

caso('diseño: sin desborde, objetivos de 44 px, contraste WCAG AA y pestañas completas, en cada pantalla y ancho', async (browser) => {
  const problemas = [];
  let medidas = 0;
  for (const pantalla of PANTALLAS) {
    for (const ancho of ANCHOS) {
      const s = await abrirPagina(browser, ancho, pantalla.sesion);
      try {
        await pantalla.abrir(s.page);
        medidas += 1;
        const etiqueta = `${pantalla.nombre} a ${ancho}px`;
        const desborde = await desbordeHorizontal(s.page);
        if (desborde > 0) problemas.push(`${etiqueta}: desborde horizontal de ${desborde}px`);
        const pequenos = await objetivosPequenos(s.page);
        if (pequenos.length) problemas.push(`${etiqueta}: objetivos menores de 44 px: ${pequenos.join('; ')}`);
        const bajos = await contrastesBajos(s.page);
        if (bajos.length) problemas.push(`${etiqueta}: contraste bajo: ${bajos.join('; ')}`);
        if (pantalla.nombre.startsWith('bandeja') && ancho >= ANCHO_ESCRITORIO && await pestanasCortadas(s.page)) {
          problemas.push(`${etiqueta}: las pestañas quedan cortadas al borde`);
        }
        if (s.errores.length) problemas.push(`${etiqueta}: errores de página: ${s.errores.join(' | ')}`);
      } finally {
        await s.ctx.close();
      }
    }
  }
  esperar(medidas === PANTALLAS.length * ANCHOS.length, `se midieron ${medidas} de ${PANTALLAS.length * ANCHOS.length}`);
  esperar(problemas.length === 0, `${problemas.length} problema(s): ${problemas.slice(0, 8).join(' | ')}`);
});

caso('foco: cada control al que llega Tab muestra un anillo visible (375 y 1280 px)', async (browser) => {
  const sinAnillo = new Set();
  let recorridos = 0;
  for (const ancho of ANCHOS_FOCO) {
    for (const nombre of ['bandeja', 'detalle pendiente', 'acceso sin sesión']) {
      const pantalla = PANTALLAS.find((x) => x.nombre === nombre);
      const s = await abrirPagina(browser, ancho, pantalla.sesion);
      try {
        await pantalla.abrir(s.page);
        for (let i = 0; i < 60; i++) {
          await s.page.keyboard.press('Tab');
          const foco = await s.page.evaluate(() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            const cs = getComputedStyle(el);
            const anillo = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) >= 2) || cs.boxShadow !== 'none';
            const nombreEl = el.id || `${el.tagName.toLowerCase()} ${(el.textContent || '').trim().slice(0, 20)}`;
            return { nombre: nombreEl, visible: el.matches(':focus-visible'), anillo };
          });
          if (!foco) continue;
          recorridos += 1;
          if (!foco.visible || !foco.anillo) sinAnillo.add(`${foco.nombre} (${nombre}, ${ancho}px)`);
        }
        esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
      } finally {
        await s.ctx.close();
      }
    }
  }
  esperar(recorridos >= 40, `pocos controles recorridos con Tab: ${recorridos}`);
  esperar(sinAnillo.size === 0, `foco sin anillo visible: ${[...sinAnillo].slice(0, 8).join(' | ')}`);
});

caso('diálogo: el foco entra, Tab no sale, Escape cierra y devuelve el foco al disparador (390 px)', async (browser) => {
  const s = await abrirPagina(browser, 390);
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas/${ID.A}`);
    await esperarDetalle(p);
    await p.locator('#acciones-detalle button[data-accion="aprobar"]').focus();
    await p.keyboard.press('Enter');
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    const dentro = () => p.evaluate(() => document.getElementById('dialogo').contains(document.activeElement));
    esperar(await dentro(), 'al abrir, el foco debe estar dentro del diálogo');
    for (let i = 0; i < 4; i++) {
      await p.keyboard.press('Tab');
      esperar(await dentro(), `Tab ${i + 1} sacó el foco del diálogo`);
    }
    await p.keyboard.press('Escape');
    esperar(!(await p.locator('#dialogo').evaluate((d) => d.open)), 'Escape debe cerrar el diálogo');
    const vuelta = await p.evaluate(() => document.activeElement && document.activeElement.getAttribute('data-accion'));
    esperar(vuelta === 'aprobar', `al cerrar, el foco debe volver a "Aprobar" (quedó en "${vuelta}")`);
    esperar(postsDe(s).length === 0, 'abrir y cerrar con teclado no debe enviar nada');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
  } finally {
    await s.ctx.close();
  }
});

caso('estático: sin style="", sin innerHTML ni scripts inline, y con CSP en las páginas del panel', async () => {
  for (const rel of ['admin-web/resenas.html', 'admin-web/entrar.html']) {
    const html = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    esperar(!/\sstyle\s*=/.test(html), `${rel}: hay atributos style=""`);
    esperar(!/<script(?![^>]*\bsrc=)[^>]*>/i.test(html), `${rel}: hay un script inline`);
    esperar(/http-equiv="Content-Security-Policy"[^>]*style-src 'self'/.test(html), `${rel}: falta la CSP con style-src 'self'`);
  }
  for (const rel of ['assets/js/admin-comun.js', 'assets/js/admin-panel.js', 'assets/js/admin-entrar.js']) {
    const js = fs.readFileSync(path.join(ROOT, rel), 'utf8');
    esperar(!/\.innerHTML\s*=/.test(js), `${rel}: asigna innerHTML`);
    esperar(!/insertAdjacentHTML|document\.write|outerHTML\s*=/.test(js), `${rel}: escribe HTML crudo`);
    esperar(!/setAttribute\(\s*['"]style['"]/.test(js), `${rel}: escribe el atributo style`);
  }
});

async function main() {
  fs.mkdirSync(path.join(ROOT, 'dev', 'e2e', 'capturas'), { recursive: true });
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
  console.log(`\nUX E2E del panel admin: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
