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

  // ── Product card CTAs (section #productos) ─────────────────────────────
  function wireProductCards() {
    var cards = document.querySelectorAll('[data-product-id]');
    for (var i = 0; i < cards.length; i++) {
      (function (el) {
        var productId   = el.getAttribute('data-product-id');
        var productName = el.getAttribute('data-product-name');
        el.addEventListener('click', function () {
          track('click_product_whatsapp', {
            product_id: productId,
            product_name: productName,
            source_section: 'fabricamos',
            destination: 'whatsapp'
          });
        });
      })(cards[i]);
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

  // ── Viewport reveal ─────────────────────────────────────────────────────
  function setupReveal() {
    document.documentElement.classList.add('js');
    var items = document.querySelectorAll('[data-reveal]');
    if (!items.length) return;
    if (!('IntersectionObserver' in window)) {
      for (var k = 0; k < items.length; k++) items[k].classList.add('is-in');
      return;
    }
    var io = new IntersectionObserver(function (entries, obs) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          entries[i].target.classList.add('is-in');
          obs.unobserve(entries[i].target);
        }
      }
    }, { threshold: 0.15, rootMargin: '0px 0px -8% 0px' });
    for (var j = 0; j < items.length; j++) io.observe(items[j]);
  }

  // ── Foto ampliada (sección "piezas") ──────────────────────────────────
  // Progressive enhancement: cada foto queda dentro de un botón que abre un <dialog> a pantalla completa. Si el
  // navegador no soporta showModal, no se cambia nada. La imagen ampliada es el src que ya usa el mosaico (la
  // variante más grande publicada): no se pide ningún recurso nuevo.
  function setupPhotoLightbox() {
    var dialogo = document.createElement('dialog');
    if (typeof dialogo.showModal !== 'function') return;
    var figuras = document.querySelectorAll('#piezas .mosaic__item');
    if (!figuras.length) return;

    dialogo.className = 'lightbox';
    dialogo.setAttribute('aria-label', 'Foto ampliada');
    var cerrar = document.createElement('button');
    cerrar.type = 'button';
    cerrar.className = 'lightbox__close';
    cerrar.setAttribute('aria-label', 'Cerrar foto ampliada');
    cerrar.textContent = '\u00d7';
    var ampliada = document.createElement('img');
    ampliada.decoding = 'async';
    var pie = document.createElement('p');
    pie.className = 'lightbox__caption';
    dialogo.appendChild(cerrar);
    dialogo.appendChild(ampliada);
    dialogo.appendChild(pie);
    document.body.appendChild(dialogo);

    for (var i = 0; i < figuras.length; i++) {
      (function (figura) {
        var picture = figura.querySelector('picture');
        var img = picture && picture.querySelector('img');
        if (!img) return;
        var boton = document.createElement('button');
        boton.type = 'button';
        boton.className = 'mosaic__zoom';
        boton.setAttribute('aria-haspopup', 'dialog');
        boton.setAttribute('aria-label', 'Ampliar foto: ' + img.alt);
        picture.parentNode.insertBefore(boton, picture);
        boton.appendChild(picture);
        boton.addEventListener('click', function () {
          ampliada.src = img.getAttribute('src');
          ampliada.alt = img.alt;
          // Proporción de la foto (atributos width/height del markup): para escalarla a la pantalla sin deformarla.
          var ancho = parseFloat(img.getAttribute('width')) || 1;
          var alto = parseFloat(img.getAttribute('height')) || 1;
          ampliada.style.setProperty('--ratio', String(ancho / alto));
          pie.textContent = img.alt;
          document.documentElement.classList.add('is-lightbox-open');
          dialogo.showModal();
        });
      })(figuras[i]);
    }

    cerrar.addEventListener('click', function () { dialogo.close(); });
    // El único control enfocable es el botón de cierre: Tab no debe sacar el foco del diálogo.
    dialogo.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') {
        e.preventDefault();
        cerrar.focus();
      }
    });
    // Clic en el fondo (el propio dialog, no la foto) cierra. Escape lo cierra el navegador.
    dialogo.addEventListener('click', function (e) { if (e.target === dialogo) dialogo.close(); });
    dialogo.addEventListener('close', function () {
      document.documentElement.classList.remove('is-lightbox-open');
      ampliada.removeAttribute('src');
    });
  }

  // ── Video del caso real. En escritorio se reproduce una vez al entrar en pantalla (muted, sin loop, sin precarga).
  // En móvil no arranca solo: el video queda oculto hasta tocar el botón, para no competir como LCP con el hero al bajar.
  // Con prefers-reduced-motion no arranca solo en ningún tamaño: queda la portada y los controles nativos.
  // La portada (data-poster) se asigna solo cuando hace falta, para no sumar peso a la carga inicial de la home.
  function setupCasoRealVideo() {
    var video = document.querySelector('[data-caso-video]');
    if (!video) return;
    var boton = document.querySelector('[data-caso-play]');
    var movil = window.matchMedia && window.matchMedia('(max-width: 899px)').matches;
    var reducir = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function ponerPortada() {
      if (!video.poster) video.poster = video.getAttribute('data-poster');
    }
    function reproducir() {
      var p = video.play();
      if (p && p.catch) p.catch(function () {});
    }
    if (movil && boton) {
      video.hidden = true;
      boton.hidden = false;
      boton.addEventListener('click', function () {
        video.hidden = false;
        boton.hidden = true;
        ponerPortada();
        reproducir();
      });
      return;
    }
    if (reducir) { ponerPortada(); return; }
    if (!('IntersectionObserver' in window)) return;
    var io = new IntersectionObserver(function (entries, obs) {
      if (!entries[0].isIntersecting) return;
      obs.disconnect();
      reproducir();
    }, { threshold: 0.6 });
    io.observe(video);
  }

  // ── Init ────────────────────────────────────────────────────────────────
  wireWaLinks();
  wireProductCards();
  setupReveal();
  setupPhotoLightbox();
  setupDeadlineCta();
  setupGiftQuoteForm();
  setupGiftPageAnalytics();
  setupCasoRealVideo();
  setupNav();
  setupStickyMobile();
  trackPageView();
  setYear();
})();
