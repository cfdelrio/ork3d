(function () {
  'use strict';

  // Lee resenas/publicas.json (lo escribe `admin/resenas.sh aprobar|exportar`).
  // Solo muestra campos públicos y todo con textContent: el texto de un cliente nunca es HTML.

  var ASPECTOS = {
    diseno: 'Diseño',
    personalizacion: 'Personalización',
    calidad: 'Calidad',
    fecha: 'Fecha de entrega',
    atencion: 'Atención',
    resultado: 'Resultado final'
  };
  var FOTO_RE = /^\/resenas\/fotos\/[a-f0-9]{32}\.jpg$/;
  var ESTRELLA = 'M12 2.5l2.94 6.02 6.64.96-4.8 4.68 1.13 6.6L12 17.6l-5.91 3.16 1.13-6.6-4.8-4.68 6.64-.96z';

  var lista = document.getElementById('resenas-lista');
  var estado = document.getElementById('resenas-estado');
  var resumen = document.getElementById('resenas-resumen');
  if (!lista) return;

  function mostrarEstado(texto) {
    estado.textContent = texto;
    estado.hidden = false;
  }

  function fecha(iso) {
    var partes = String(iso || '').slice(0, 10).split('-');
    return partes.length === 3 ? partes[2] + '/' + partes[1] + '/' + partes[0] : '';
  }

  function estrellas(valor) {
    var n = Math.max(1, Math.min(5, Math.round(Number(valor) || 0)));
    var caja = document.createElement('div');
    caja.className = 'resena__estrellas';
    caja.setAttribute('role', 'img');
    caja.setAttribute('aria-label', n + ' de 5 estrellas');
    for (var i = 1; i <= 5; i++) {
      var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('class', i <= n ? 'on' : 'off');
      var path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', ESTRELLA);
      svg.appendChild(path);
      caja.appendChild(svg);
    }
    return caja;
  }

  function tarjeta(item) {
    var li = document.createElement('li');
    li.className = 'resena';

    var cabecera = document.createElement('div');
    cabecera.className = 'resena__cabecera';
    cabecera.appendChild(estrellas(item.rating));
    if (item.verified) {
      var sello = document.createElement('span');
      sello.className = 'resena__sello';
      sello.textContent = 'Pedido verificado';
      cabecera.appendChild(sello);
    }
    li.appendChild(cabecera);

    if (item.product_label) {
      var producto = document.createElement('p');
      producto.className = 'resena__producto';
      producto.textContent = item.product_label;
      li.appendChild(producto);
    }

    if (item.comment) {
      var texto = document.createElement('p');
      texto.className = 'resena__texto';
      texto.textContent = '“' + item.comment + '”';
      li.appendChild(texto);
    }

    var aspectos = (Array.isArray(item.aspects) ? item.aspects : []).filter(function (a) {
      return Object.prototype.hasOwnProperty.call(ASPECTOS, a);
    });
    if (aspectos.length) {
      var chips = document.createElement('ul');
      chips.className = 'resena__chips';
      chips.setAttribute('aria-label', 'Aspectos destacados');
      aspectos.forEach(function (a) {
        var chip = document.createElement('li');
        chip.textContent = ASPECTOS[a];
        chips.appendChild(chip);
      });
      li.appendChild(chips);
    }

    if (typeof item.photo_url === 'string' && FOTO_RE.test(item.photo_url)) {
      var img = document.createElement('img');
      img.className = 'resena__foto';
      img.src = item.photo_url;
      img.alt = 'Foto enviada por ' + (item.display_name || 'un cliente');
      img.loading = 'lazy';
      img.decoding = 'async';
      li.appendChild(img);
    }

    var pie = document.createElement('p');
    pie.className = 'resena__autor';
    var autor = document.createElement('span');
    autor.textContent = item.display_name || 'Cliente';
    pie.appendChild(autor);
    var cuando = fecha(item.published_at);
    if (cuando) {
      var tiempo = document.createElement('time');
      tiempo.setAttribute('datetime', String(item.published_at).slice(0, 10));
      tiempo.textContent = ' · ' + cuando;
      pie.appendChild(tiempo);
    }
    li.appendChild(pie);

    return li;
  }

  function renderizar(datos) {
    var items = datos && Array.isArray(datos.items) ? datos.items : [];
    if (!items.length) {
      mostrarEstado('Todavía no hay opiniones publicadas.');
      return;
    }
    var suma = 0;
    items.forEach(function (item) {
      suma += Math.max(1, Math.min(5, Number(item.rating) || 0));
    });
    var promedio = (suma / items.length).toFixed(1).replace('.', ',');
    resumen.textContent = items.length + (items.length === 1 ? ' opinión' : ' opiniones') +
      ' · promedio ' + promedio + ' de 5';
    resumen.hidden = false;

    items.forEach(function (item) {
      lista.appendChild(tarjeta(item));
    });
    lista.hidden = false;
  }

  fetch('/resenas/publicas.json', { cache: 'no-cache' })
    .then(function (r) {
      if (r.status === 404) return { items: [] };  // todavía no se exportó nada
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderizar)
    .catch(function () {
      mostrarEstado('No pudimos cargar las opiniones. Probá de nuevo en unos minutos.');
    });
})();
