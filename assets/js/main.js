(function () {
  'use strict';

  var cfg = window.ORK3D_CONFIG || {};
  var number = cfg.WHATSAPP_NUMBER || '549XXXXXXXXXX';
  var defaultMsg = cfg.WHATSAPP_DEFAULT_MESSAGE || 'Hola, quiero pedir presupuesto.';

  function buildWaUrl(msg) {
    var text = encodeURIComponent(msg || defaultMsg);
    return 'https://wa.me/' + number + '?text=' + text;
  }

  // Wire up all [data-wa] links
  var links = document.querySelectorAll('[data-wa]');
  for (var i = 0; i < links.length; i++) {
    var el = links[i];
    var customMsg = el.getAttribute('data-wa-msg');
    el.setAttribute('href', buildWaUrl(customMsg));
    el.setAttribute('target', '_blank');
    el.setAttribute('rel', 'noopener');
  }

  // Mobile nav toggle
  var toggle = document.getElementById('navToggle');
  var navLinks = document.getElementById('navLinks');
  if (toggle && navLinks) {
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

  // Footer year
  var y = document.getElementById('year');
  if (y) y.textContent = new Date().getFullYear();
})();
