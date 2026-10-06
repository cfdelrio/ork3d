(function () {
  'use strict';

  var cfg = window.ORK3D_CONFIG || {};
  var number = cfg.WHATSAPP_NUMBER || '549XXXXXXXXXX';
  var defaultMsg = cfg.WHATSAPP_DEFAULT_MESSAGE || 'Hola, quiero pedir presupuesto.';

  // ── Analytics ──────────────────────────────────────────────────────────
  function track(event, props) {
    props = props || {};
    props.page = window.location.pathname;
    if (typeof gtag === 'function') gtag('event', event, props);
    if (typeof plausible === 'function') plausible(event, { props: props });
    if (typeof console !== 'undefined') console.debug('[ORK3D]', event, props);
  }

  // ── WhatsApp ────────────────────────────────────────────────────────────
  var WA_BASE = 'https://wa.me/';

  function buildWaUrl(msg) {
    return WA_BASE + number + '?text=' + encodeURIComponent(msg || defaultMsg);
  }

  // Wire all [data-wa] elements; source comes from data-wa-source attribute
  function wireWaLinks() {
    var links = document.querySelectorAll('[data-wa]');
    for (var i = 0; i < links.length; i++) {
      (function (el) {
        var msg    = el.getAttribute('data-wa-msg');
        var source = el.getAttribute('data-wa-source') || 'generic';
        el.setAttribute('href', buildWaUrl(msg));
        el.setAttribute('target', '_blank');
        el.setAttribute('rel', 'noopener');
        el.addEventListener('click', function () {
          track('click_whatsapp', { source: source });
        });
      })(links[i]);
    }
  }

  // ── Date-aware CTA on /torneos ──────────────────────────────────────────
  function setupDeadlineCta() {
    var input = document.getElementById('torneoFecha');
    var btn   = document.getElementById('deadlineCta');
    if (!input || !btn) return;

    function buildMsg() {
      var fecha = input.value;
      return 'Hola ORK3D 👋\nQuiero consultar por premios para un torneo.\n\nDeporte: \nFecha de la final: ' +
        (fecha || '(a confirmar)') +
        '\nPremios que necesito: \nTengo logo: S\xED / No';
    }

    function refresh() {
      btn.setAttribute('href', buildWaUrl(buildMsg()));
      btn.setAttribute('target', '_blank');
      btn.setAttribute('rel', 'noopener');
    }

    input.addEventListener('change', function () {
      if (input.value) track('start_quote', { source: 'tournament_deadline' });
      refresh();
    });
    input.addEventListener('input', refresh);
    refresh();

    btn.addEventListener('click', function () {
      track('click_whatsapp', { source: 'tournament_deadline' });
    });
  }

  // ── Mobile nav ──────────────────────────────────────────────────────────
  function setupNav() {
    var toggle   = document.getElementById('navToggle');
    var navLinks = document.getElementById('navLinks');
    if (!toggle || !navLinks) return;
    toggle.addEventListener('click', function () {
      var open = navLinks.classList.toggle('is-open');
      toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    navLinks.addEventListener('click', function (e) {
      if (e.target.tagName === 'A') {
        navLinks.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // ── Sticky mobile CTA ──────────────────────────────────────────────────
  function setupStickyMobile() {
    var sticky = document.getElementById('stickyCta');
    var hero   = document.getElementById('hero');
    if (!sticky || !hero) return;

    function show(visible) {
      sticky.classList.toggle('is-visible', visible);
      sticky.setAttribute('aria-hidden', visible ? 'false' : 'true');
      document.body.classList.toggle('sticky-active', visible);
    }

    if ('IntersectionObserver' in window) {
      new IntersectionObserver(function (entries) {
        show(!entries[0].isIntersecting);
      }, { threshold: 0 }).observe(hero);
    } else {
      show(true);
    }
  }

  // ── Gift quote form ────────────────────────────────────────────────────
  function setupGiftQuoteForm() {
    var form    = document.getElementById('quoteForm');
    var submit  = document.getElementById('quoteSubmit');
    var errBox  = document.getElementById('formStateError');
    var success = document.getElementById('formStateSuccess');
    var waFall  = document.getElementById('successWaFallback');
    if (!form) return;

    var required = ['qEmpresa','qNombre','qEmail','qWa','qCantidad','qDestinatarios','qFecha'];

    function validate() {
      var ok = true;
      required.forEach(function (id) {
        var el  = document.getElementById(id);
        var err = document.getElementById(id + '-error');
        if (!el) return;
        var empty = !el.value.trim();
        var emailBad = (id === 'qEmail' && !empty && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(el.value));
        if (empty || emailBad) {
          el.setAttribute('aria-invalid', 'true');
          if (err) err.textContent = empty ? 'Campo requerido.' : 'Email inválido.';
          ok = false;
        } else {
          el.removeAttribute('aria-invalid');
          if (err) err.textContent = '';
        }
      });
      return ok;
    }

    function buildMsg() {
      var g = function (id) { var e = document.getElementById(id); return e ? e.value.trim() : ''; };
      var dest = g('qDestinatarios');
      var destMap = { equipo:'Equipo / empleados', clientes:'Clientes', proveedores:'Proveedores', otro:'Otro' };
      return 'Hola ORK3D 👋\nQuiero consultar regalos empresariales de fin de año.\n\n' +
        'Empresa: ' + g('qEmpresa') + '\n' +
        'Nombre: ' + g('qNombre') + '\n' +
        'Email: ' + g('qEmail') + '\n' +
        'WhatsApp: ' + g('qWa') + '\n' +
        'Cantidad: ' + g('qCantidad') + '\n' +
        'Para: ' + (destMap[dest] || dest) + '\n' +
        'Fecha ideal: ' + (g('qFecha') || 'a confirmar') + '\n' +
        'Presupuesto aprox./u: ' + (g('qPresupuesto') || 'sin especificar') + '\n' +
        (g('qComentarios') ? '\nDetalle: ' + g('qComentarios') : '');
    }

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      if (!validate()) {
        if (errBox) errBox.hidden = false;
        track('form_error', { form: 'gift_quote' });
        return;
      }
      if (errBox) errBox.hidden = true;
      if (submit) submit.classList.add('is-loading');
      track('form_submit', { form: 'gift_quote' });
      var url = buildWaUrl(buildMsg());
      if (waFall) waFall.href = url;
      window.open(url, '_blank', 'noopener');
      setTimeout(function () {
        form.hidden = true;
        if (success) success.hidden = false;
        if (submit) submit.classList.remove('is-loading');
      }, 800);
    });

    required.forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener('blur', function () { validate(); });
    });
  }

  // ── Gift page analytics (data-track elements) ──────────────────────────
  function setupGiftPageAnalytics() {
    var tracked = document.querySelectorAll('[data-track]');
    for (var i = 0; i < tracked.length; i++) {
      (function (el) {
        var ev  = el.getAttribute('data-track');
        var src = el.getAttribute('data-track-source') || '';
        el.addEventListener('click', function () {
          track(ev, src ? { source: src } : {});
        });
      })(tracked[i]);
    }

    if ('IntersectionObserver' in window) {
      var gifts = document.getElementById('regalos');
      if (gifts) {
        new IntersectionObserver(function (entries, obs) {
          if (entries[0].isIntersecting) {
            track('gifts_section_viewed', {});
            obs.disconnect();
          }
        }, { threshold: 0.3 }).observe(gifts);
      }
    }
  }

  // ── Page-level events ──────────────────────────────────────────────────
  function trackPageView() {
    var p = window.location.pathname;
    if (p === '/torneos' || p === '/torneos.html' || p === '/torneos/') {
      track('view_tournaments', {});
    }
    if (p.indexOf('regalos-empresariales') !== -1) {
      track('view_gift_page', {});
    }
  }

  // ── Footer year ─────────────────────────────────────────────────────────
  function setYear() {
    var y = document.getElementById('year');
    if (y) y.textContent = new Date().getFullYear();
  }

  // ── Init ────────────────────────────────────────────────────────────────
  wireWaLinks();
  setupDeadlineCta();
  setupGiftQuoteForm();
  setupGiftPageAnalytics();
  setupNav();
  setupStickyMobile();
  trackPageView();
  setYear();
})();
