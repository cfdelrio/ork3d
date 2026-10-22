(function () {
  'use strict';

  // Lee resenas/publicas.json. Lo regenera el panel al aprobar, ocultar o rechazar una reseña, así que el sitio
  // solo muestra lo que el export trae: aprobadas y con permiso de publicación.
  // Solo muestra campos públicos y todo con textContent: el texto de un cliente nunca se interpreta como HTML.
  // Un fallo del export deja un mensaje de estado; el resto de la página sigue igual.

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
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var lista = document.getElementById('resenas-lista');
  var listado = document.getElementById('resenas-listado');
  var resumen = document.getElementById('resenas-resumen');
  var resumenNota = document.getElementById('resumen-nota');
  var listadoTitulo = document.getElementById('listado-titulo');
  if (!lista || !listado || !resumen || !resumenNota || !listadoTitulo) return;

  // Estados en un solo lugar: el resumen (cargando, vacío, error). Así no se mueve nada al cambiar de estado.
  function mostrarEstado(texto, tipo) {
    resumenNota.textContent = texto;
    resumen.setAttribute('data-estado', tipo);
    listado.setAttribute('data-estado', tipo);
    lista.hidden = true;
    listadoTitulo.hidden = true;
  }

  // Rating válido: número entre 1 y 5. Lo que no lo es no entra ni al listado ni al promedio.
  function ratingValido(valor) {
    var n = Number(valor);
    return isFinite(n) && n >= 1 && n <= 5 ? n : null;
  }

  function fecha(iso) {
    var partes = String(iso || '').slice(0, 10).split('-');
    return partes.length === 3 ? partes[2] + '/' + partes[1] + '/' + partes[0] : '';
  }

  // Más recientes primero. Sin ranking: no se destaca ninguna reseña por su calificación.
  function ordenar(items) {
    return items.slice().sort(function (a, b) {
      var fa = String(a.published_at || '');
      var fb = String(b.published_at || '');
      if (fa !== fb) return fb < fa ? -1 : 1;
      return String(a.id) < String(b.id) ? -1 : 1;
    });
  }

  function promedio(valores) {
    var suma = valores.reduce(function (acc, v) { return acc + v; }, 0);
    return (Math.round((suma / valores.length) * 10) / 10).toFixed(1).replace('.', ',');
  }

  function estrellas(valor, etiqueta) {
    var n = Math.max(1, Math.min(5, Math.round(valor)));
    var caja = document.createElement('div');
    caja.className = 'resena__estrellas';
    caja.setAttribute('role', 'img');
    caja.setAttribute('aria-label', etiqueta);
    for (var i = 1; i <= 5; i++) {
      var svg = document.createElementNS(SVG_NS, 'svg');
      svg.setAttribute('viewBox', '0 0 24 24');
      svg.setAttribute('aria-hidden', 'true');
      svg.setAttribute('focusable', 'false');
      svg.setAttribute('class', i <= n ? 'on' : 'off');
      var path = document.createElementNS(SVG_NS, 'path');
      path.setAttribute('d', ESTRELLA);
      svg.appendChild(path);
      caja.appendChild(svg);
    }
    return caja;
  }

  function tarjeta(item, rating) {
    var li = document.createElement('li');
    li.className = 'resena';

    var cabecera = document.createElement('div');
    cabecera.className = 'resena__cabecera';
    cabecera.appendChild(estrellas(rating, rating + ' de 5 estrellas'));
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
      img.alt = 'Foto enviada por ' + (item.display_name || 'un cliente');
      img.loading = 'lazy';
      img.decoding = 'async';
      // Si la foto no carga, la tarjeta sigue sin imagen: no queda un hueco roto.
      img.addEventListener('error', function () {
        li.classList.add('resena--sin-foto');
        img.remove();
      });
      img.src = item.photo_url;
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

  function renderizarResumen(valores) {
    var prom = promedio(valores);
    var cantidad = valores.length;
    resumenNota.textContent = '';
    resumenNota.appendChild(estrellas(Number(prom.replace(',', '.')), 'Promedio ' + prom + ' de 5 estrellas'));
    var texto = document.createElement('p');
    texto.className = 'resumen__texto';
    texto.textContent = prom + ' de 5 · ' + cantidad + (cantidad === 1 ? ' opinión' : ' opiniones');
    resumenNota.appendChild(texto);
    resumen.setAttribute('data-estado', 'listo');
  }

  function renderizar(datos) {
    var todos = datos && Array.isArray(datos.items) ? datos.items : null;
    if (!todos) throw new Error('El export no tiene la lista items');

    var validos = todos
      .map(function (item) { return { item: item, rating: item ? ratingValido(item.rating) : null }; })
      .filter(function (par) { return par.rating !== null; });

    if (!validos.length) {
      mostrarEstado('Todavía no hay opiniones publicadas. Cuando las revisemos, van a aparecer acá.', 'vacio');
      return;
    }

    renderizarResumen(validos.map(function (par) { return par.rating; }));
    ordenar(validos.map(function (par) { return par.item; })).forEach(function (item) {
      lista.appendChild(tarjeta(item, ratingValido(item.rating)));
    });
    listado.setAttribute('data-estado', 'listo');
    listadoTitulo.hidden = false;
    lista.hidden = false;
  }

  function fallar() {
    mostrarEstado('No pudimos cargar las opiniones. Probá de nuevo en unos minutos.', 'error');
  }

  fetch('/resenas/publicas.json', { cache: 'no-cache' })
    .then(function (r) {
      if (r.status === 404) return { items: [] };   // todavía no se exportó nada
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(renderizar)
    .catch(fallar);
})();
