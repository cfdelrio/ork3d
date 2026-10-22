#!/usr/bin/env node
/* E2E de moderación del panel admin: lectura sin mutaciones y acciones solo con una interacción explícita.
 *
 * Uso (desde la raíz del repo):
 *   NODE_PATH=/opt/node22/lib/node_modules node dev/e2e/admin-moderacion.e2e.js
 *
 * Chromium real contra dev/serve.py: páginas y JS reales. La API admin se simula en el navegador (page.route),
 * así cada POST se cuenta sin tocar datos reales. Variables: PORT (8797), CHROME_PATH, PLAYWRIGHT_PATH.
 *
 * Qué prueba: en el panel, lo que no es una acción explícita (cargar, renderizar, refrescar, navegar, filtrar) no
 * envía ninguna mutación. Un doble click sobre el disparador de una acción no puede confirmarla: el segundo click
 * de un doble click puede caer en "Confirmar" en pantallas angostas (ver V2-ADMIN.md §17).
 */
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');

const ROOT = path.resolve(__dirname, '..', '..');
const PORT = Number(process.env.PORT || 8797);
const BASE = `http://127.0.0.1:${PORT}`;
const CHROME = process.env.CHROME_PATH
  || (fs.existsSync('/opt/pw-browsers/chromium') ? '/opt/pw-browsers/chromium' : undefined);
// Geometría donde el 2.º clic de un doble click sobre "Aprobar" cae en "Confirmar" (medido en el barrido de §17).
const ANCHO_RIESGO = 390;
const ALTO_RIESGO = 480;

const ID = {
  A: '0123456789abcdef0123456789abcdef', // pendiente
  B: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', // aprobada
  C: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', // oculta
  D: 'cccccccccccccccccccccccccccccccc'  // pendiente con aviso por reenviar
};
const TRANSICION = {
  aprobar: { desde: ['pending'], a: 'approved' },
  rechazar: { desde: ['pending', 'approved'], a: 'rejected' },
  ocultar: { desde: ['approved'], a: 'hidden' },
  restaurar: { desde: ['hidden'], a: 'approved' }
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

function estadoInicial() {
  return {
    A: { id: ID.A, status: 'pending', aviso: null },
    B: { id: ID.B, status: 'approved', aviso: null, moderated_at: '2026-10-22T13:12:31Z', published_at: '2026-10-22T13:12:31Z' },
    C: { id: ID.C, status: 'hidden', aviso: null },
    D: { id: ID.D, status: 'pending', aviso: 'pendiente' }
  };
}

function reseña(r) {
  return {
    id: r.id, status: r.status, rating: 5, comment: 'Muy buena', display_name: 'Ana', product_label: 'Torneo',
    category: 'producto', created_at: '2026-10-22T13:10:17Z', moderated_at: r.moderated_at || null,
    published_at: r.published_at || null, permission_to_publish: true, tiene_foto: false, verified: false,
    aviso: r.aviso, aspects: [], private_feedback: '', pedido: 'SMOKE', contact_requested: false, contact: ''
  };
}

// API simulada en el navegador. Cada POST queda registrado en estado.posts.
function instalarApi(page, estado) {
  return page.route('**/admin/api/**', async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    const ruta = url.pathname;
    const json = (status, cuerpo) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(cuerpo) });
    if (req.method() === 'GET' && ruta === '/admin/api/sesion') return json(200, { ok: true });
    if (req.method() === 'GET' && ruta === '/admin/api/resenas') {
      const est = url.searchParams.get('estado');
      const lista = Object.values(estado.revs).filter((r) => est === 'all' || r.status === est).map(reseña);
      return json(200, { resenas: lista });
    }
    const m = /^\/admin\/api\/resenas\/([a-f0-9]{32})(?:\/([a-z-]+))?$/.exec(ruta);
    if (m && req.method() === 'GET' && !m[2]) {
      const r = Object.values(estado.revs).find((x) => x.id === m[1]);
      return r ? json(200, { resena: reseña(r) }) : json(404, { ok: false, error: 'no_encontrado' });
    }
    if (m && req.method() === 'POST' && m[2]) {
      const r = Object.values(estado.revs).find((x) => x.id === m[1]);
      const accion = m[2];
      estado.posts.push({ id: m[1], accion });
      if (!r) return json(404, { ok: false, error: 'no_encontrado' });
      if (accion === 'reenviar-aviso') {
        if (r.aviso !== 'pendiente') return json(409, { ok: false, error: 'ya_enviado' });
        r.aviso = 'enviado';
        return json(200, { ok: true });
      }
      const t = TRANSICION[accion];
      if (!t || !t.desde.includes(r.status)) return json(409, { ok: false, error: 'estado_incompatible' });
      r.status = t.a;
      if (accion === 'aprobar') { r.moderated_at = '2026-10-22T13:12:31Z'; r.published_at = r.moderated_at; }
      return json(200, { ok: true });
    }
    return json(404, { ok: false, error: 'no_encontrado' });
  });
}

async function abrirPagina(browser, { ancho = 1280, alto = 800 } = {}) {
  const ctx = await browser.newContext({ viewport: { width: ancho, height: alto }, locale: 'es-AR' });
  const page = await ctx.newPage();
  const estado = { revs: estadoInicial(), posts: [] };
  const errores = [];
  page.on('pageerror', (e) => errores.push(`pageerror: ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') errores.push(`console: ${m.text()}`); });
  await instalarApi(page, estado);
  return { ctx, page, estado, errores };
}

function postsDe(s, accion) {
  return s.estado.posts.filter((p) => !accion || p.accion === accion);
}

function boton(p, accion) {
  return p.locator(`#acciones-detalle button[data-accion="${accion}"]`);
}

async function irDetalle(p, id) {
  await p.goto(`${BASE}/admin/resenas/${id}`);
  await p.locator('#detalle .pildora').waitFor({ state: 'visible', timeout: 8000 });
}

async function centro(locator) {
  await locator.scrollIntoViewIfNeeded();
  const caja = await locator.boundingBox();
  return { x: caja.x + caja.width / 2, y: caja.y + caja.height / 2 };
}

async function dialogoAbierto(p) {
  return p.locator('#dialogo').evaluate((d) => d.open);
}

// Espera a que "Confirmar" esté habilitado (tras el plazo de seguridad). Si nunca se deshabilita, responde enseguida.
async function esperarConfirmar(p) {
  await p.locator('#dialogo-confirmar:not([disabled])').waitFor({ state: 'visible', timeout: 8000 });
}

// ── Lectura: ninguna mutación ───────────────────────────────────────────────

caso('lectura: lista, filtros, Ver, recarga, atrás y adelante no envían ninguna mutación', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await p.goto(`${BASE}/admin/resenas`);
    await p.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    esperar(postsDe(s).length === 0, 'cargar la bandeja no debe mutar');
    await p.click('#tab-approved');
    await p.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    await p.click('#tab-all');
    await p.locator('#lista .tarjeta').nth(3).waitFor({ state: 'visible' });
    await p.click('#tab-pending');
    await p.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    esperar(postsDe(s).length === 0, 'filtrar pestañas no debe mutar');
    await p.locator('#lista .tarjeta a.btn').first().click();
    await p.locator('#detalle .pildora').waitFor({ state: 'visible' });
    await boton(p, 'aprobar').waitFor({ state: 'visible' });
    esperar(postsDe(s).length === 0, 'abrir el detalle no debe mutar');
    await p.reload();
    await p.locator('#detalle .pildora').waitFor({ state: 'visible' });
    esperar(postsDe(s).length === 0, 'refrescar el detalle no debe mutar');
    await p.goBack();
    await p.locator('#lista .tarjeta').first().waitFor({ state: 'visible' });
    await p.goForward();
    await p.locator('#detalle .pildora').waitFor({ state: 'visible' });
    esperar(postsDe(s).length === 0, 'volver atrás y adelante no debe mutar');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
  } finally {
    await s.ctx.close();
  }
});

caso('lectura: cada estado renderiza sus botones sin mutar (pendiente, aprobada, oculta)', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    for (const id of [ID.A, ID.B, ID.C]) {
      await irDetalle(s.page, id);
      await s.page.locator('#acciones-detalle button, #acciones-detalle p').first().waitFor({ state: 'visible' });
    }
    esperar(postsDe(s).length === 0, 'renderizar botones de cualquier estado no debe mutar');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
  } finally {
    await s.ctx.close();
  }
});

// ── Aprobar: solo con interacción explícita ─────────────────────────────────

caso('aprobar: cancelar o Escape no envían nada', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    await boton(p, 'aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await p.click('#dialogo-cancelar');
    esperar(!(await dialogoAbierto(p)), 'cancelar cierra el diálogo');
    await boton(p, 'aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await p.keyboard.press('Escape');
    esperar(!(await dialogoAbierto(p)), 'Escape cierra el diálogo');
    esperar(postsDe(s).length === 0, 'cancelar o Escape no deben enviar nada');
  } finally {
    await s.ctx.close();
  }
});

caso('aprobar: un clic explícito en Confirmar envía exactamente una vez', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    await boton(p, 'aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await esperarConfirmar(p);
    await p.click('#dialogo-confirmar');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña aprobada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(postsDe(s, 'aprobar').length === 1, `debe haber un solo POST de aprobar, hubo ${postsDe(s, 'aprobar').length}`);
    esperar((await p.locator('#detalle .pildora').textContent()).trim() === 'Aprobada', 'la píldora debe decir Aprobada');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
  } finally {
    await s.ctx.close();
  }
});

caso('aprobar: doble clic sobre "Aprobar" en la geometría de riesgo no aprueba; el diálogo queda abierto', async (browser) => {
  const s = await abrirPagina(browser, { ancho: ANCHO_RIESGO, alto: ALTO_RIESGO });
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    const punto = await centro(boton(p, 'aprobar'));
    await p.mouse.click(punto.x, punto.y);                 // clic 1: abre el diálogo
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    // Precondición: bajo el puntero queda "Confirmar". Sin esto, la prueba no mediría el riesgo.
    const bajoElPuntero = await p.evaluate(([x, y]) => {
      const confirmar = document.getElementById('dialogo-confirmar');
      const e = document.elementFromPoint(x, y);
      return !!e && (e === confirmar || confirmar.contains(e));
    }, [punto.x, punto.y]);
    esperar(bajoElPuntero, 'precondición: en esta geometría el 2.º clic cae en Confirmar');
    await p.mouse.down({ clickCount: 2 });                 // clic 2 del doble clic, sobre el mismo punto
    await p.mouse.up({ clickCount: 2 });
    await p.waitForTimeout(1200);
    esperar(postsDe(s, 'aprobar').length === 0, 'un doble clic sobre Aprobar no puede aprobar la reseña');
    esperar(await dialogoAbierto(p), 'el diálogo debe seguir abierto hasta una confirmación explícita');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
  } finally {
    await s.ctx.close();
  }
});

caso('diálogo: un clic sobre Confirmar justo después de abrirlo no confirma (aprobar, rechazar, ocultar)', async (browser) => {
  const casos = [
    { accion: 'aprobar', id: ID.A },
    { accion: 'rechazar', id: ID.A },
    { accion: 'ocultar', id: ID.B }
  ];
  for (const c of casos) {
    const s = await abrirPagina(browser);
    try {
      const p = s.page;
      await irDetalle(p, c.id);
      await boton(p, c.accion).click();
      await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
      const punto = await centro(p.locator('#dialogo-confirmar'));
      await p.mouse.click(punto.x, punto.y);               // clic sobre Confirmar, dentro del plazo
      await p.mouse.down({ clickCount: 2 });               // y su segundo clic (doble clic)
      await p.mouse.up({ clickCount: 2 });
      await p.waitForTimeout(1200);
      esperar(postsDe(s, c.accion).length === 0, `${c.accion}: un doble clic sobre Confirmar al abrir no puede confirmar`);
      esperar(await dialogoAbierto(p), `${c.accion}: el diálogo debe seguir abierto`);
    } finally {
      await s.ctx.close();
    }
  }
});

caso('diálogo: Confirmar está deshabilitado al abrirse y se habilita después del plazo', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    await boton(p, 'aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    esperar(await p.locator('#dialogo-confirmar').isDisabled(), 'Confirmar debe estar deshabilitado al abrir el diálogo');
    await esperarConfirmar(p);
    esperar(await p.locator('#dialogo-confirmar').isEnabled(), 'Confirmar debe habilitarse después del plazo');
    esperar(postsDe(s).length === 0, 'habilitar Confirmar no debe enviar nada');
  } finally {
    await s.ctx.close();
  }
});

caso('aprobar: doble clic sobre Confirmar tras el plazo envía como máximo una vez', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    await boton(p, 'aprobar').click();
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await esperarConfirmar(p);
    await p.locator('#dialogo-confirmar').dblclick();
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña aprobada.' }).waitFor({ state: 'visible', timeout: 8000 });
    await p.waitForTimeout(500);
    esperar(postsDe(s, 'aprobar').length === 1, `un doble clic debe enviar una sola vez, envió ${postsDe(s, 'aprobar').length}`);
  } finally {
    await s.ctx.close();
  }
});

caso('rechazar y ocultar: confirmación explícita envía una vez; cancelar no envía', async (browser) => {
  for (const c of [{ accion: 'rechazar', id: ID.A }, { accion: 'ocultar', id: ID.B }]) {
    const s = await abrirPagina(browser);
    try {
      const p = s.page;
      await irDetalle(p, c.id);
      await boton(p, c.accion).click();
      await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
      await p.click('#dialogo-cancelar');
      esperar(postsDe(s).length === 0, `${c.accion}: cancelar no envía`);
      await boton(p, c.accion).click();
      await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
      await esperarConfirmar(p);
      await p.click('#dialogo-confirmar');
      await p.waitForTimeout(800);
      esperar(postsDe(s, c.accion).length === 1, `${c.accion}: una confirmación explícita envía una vez`);
    } finally {
      await s.ctx.close();
    }
  }
});

// ── Acciones sin diálogo: doble clic envía como máximo una vez ──────────────

caso('restaurar y reenviar aviso (sin diálogo): doble clic envía como máximo una vez', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.C);
    const restaurar = boton(p, 'restaurar');
    const punto = await centro(restaurar);
    await p.mouse.dblclick(punto.x, punto.y);
    await p.waitForTimeout(1000);
    esperar(postsDe(s, 'restaurar').length <= 1, `restaurar: un doble clic envió ${postsDe(s, 'restaurar').length}`);
    esperar(postsDe(s, 'restaurar').length === 1, 'restaurar debe enviarse una vez');
    esperar(s.errores.length === 0, `errores de página: ${s.errores.join(' | ')}`);
    await irDetalle(p, ID.D);
    const reenviar = boton(p, 'reenviar-aviso');
    const punto2 = await centro(reenviar);
    await p.mouse.dblclick(punto2.x, punto2.y);
    await p.waitForTimeout(1000);
    esperar(postsDe(s, 'reenviar-aviso').length <= 1, `reenviar aviso: un doble clic envió ${postsDe(s, 'reenviar-aviso').length}`);
  } finally {
    await s.ctx.close();
  }
});

// ── Teclado ─────────────────────────────────────────────────────────────────

caso('teclado: Enter en Aprobar y Enter en Cancelar no aprueban; Enter en Confirmar tras el plazo aprueba una vez', async (browser) => {
  const s = await abrirPagina(browser);
  try {
    const p = s.page;
    await irDetalle(p, ID.A);
    await boton(p, 'aprobar').focus();
    await p.keyboard.press('Enter');
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await p.keyboard.press('Enter');                       // el foco está en Cancelar
    esperar(!(await dialogoAbierto(p)), 'Enter sobre Cancelar cierra el diálogo');
    esperar(postsDe(s).length === 0, 'Enter sobre Aprobar y Cancelar no deben enviar nada');
    await boton(p, 'aprobar').focus();
    await p.keyboard.press('Enter');
    await p.locator('#dialogo[open]').waitFor({ state: 'visible' });
    await esperarConfirmar(p);
    await p.locator('#dialogo-confirmar').focus();
    await p.keyboard.press('Enter');
    await p.locator('#aviso-detalle').filter({ hasText: 'Reseña aprobada.' }).waitFor({ state: 'visible', timeout: 8000 });
    esperar(postsDe(s, 'aprobar').length === 1, `Enter en Confirmar debe enviar una vez, envió ${postsDe(s, 'aprobar').length}`);
  } finally {
    await s.ctx.close();
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
  console.log(`\nModeración E2E: ${casos.length - fallos} PASS, ${fallos} FAIL`);
  process.exit(fallos ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
