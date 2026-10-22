/* ORK3D · Panel admin de reseñas (V2).
 * Una sola página sirve /admin/resenas (panel y bandeja) y /admin/resenas/<id> (detalle).
 * Sin innerHTML: el contenido de clientes entra solo con textContent.
 * El link de invitación vive solo en el DOM de la pantalla: no se guarda en ningún storage.
 */
(function () {
  'use strict';

  var A = window.OrkAdmin;
  var $ = function (id) { return document.getElementById(id); };

  var RUTA_DETALLE = /^\/admin\/resenas\/([a-f0-9]{32})\/?$/;
  var ID_DETALLE = (RUTA_DETALLE.exec(window.location.pathname) || [])[1] || null;
  var SLUG_RE = /^[a-z0-9-]+$/;
  var MENSAJE_BASE = 'Hola, gracias por tu compra en ORK3D. ¿Nos contás cómo te fue? Dejá tu opinión acá (vence en {dias} días): {link}';
  var MSG_SESION = 'Tu sesión venció. Iniciá acceso de nuevo.';
  var MSG_EXPORT_PENDIENTE = 'Export pendiente: el estado cambió, pero el export no se pudo actualizar.';
  var MSG_RECARGAR = 'No se pudo completar la acción. Recargá la página e intentá de nuevo.';
  // Rechazos de cabeceras CSRF de la API: 400 con estos códigos (V2-ADMIN.md §5 y §16.8). El 403 de versiones
  // anteriores de la API se trata igual.
  var CSRF_ERRORES = ['origen', 'csrf', 'content_type'];
  // Plazo antes de que "Confirmar" acepte un clic tras abrir el diálogo. El segundo clic de un doble clic sobre
  // el botón que abrió el diálogo puede caer en "Confirmar" (pantallas angostas): sin este plazo, aprobaba sin que
  // nadie confirmara. Ver V2-ADMIN.md §17.
  var ESPERA_CONFIRMAR_MS = 800;
  var EXITO = {
    aprobar: 'Reseña aprobada.',
    rechazar: 'Reseña rechazada.',
    ocultar: 'Reseña oculta.',
    restaurar: 'Reseña restaurada.',
    'reenviar-aviso': 'Aviso enviado.'
  };

  // Estado de la interfaz (no contiene datos de clientes ni tokens).
  var ui = {
    ocupado: false,
    confirmacion: null,
    turnoConfirmacion: 0,
    pestana: 'pending',
    solicitudBandeja: 0,
    solicitudTotales: 0
  };

  // Nombre de cada sección en la cabecera (presentación).
  var SECCION_TEXTO = {
    'seccion-acceso': 'Acceso',
    'seccion-panel': 'Bandeja de reseñas',
    'seccion-detalle': 'Reseña'
  };

  // Textos de estado vacío por pestaña: explican qué pasa a continuación, no solo que no hay nada.
  var VACIO = {
    pending: ['No hay reseñas pendientes', 'Cuando un cliente envíe su opinión, aparece acá para revisarla.'],
    approved: ['Todavía no hay reseñas publicadas', 'Cuando aprobás una reseña pendiente, se ve en el sitio y aparece acá.'],
    hidden: ['No hay reseñas ocultas', 'Una reseña oculta sale del sitio, pero queda acá por si hay que restaurarla.'],
    rejected: ['No hay reseñas rechazadas', 'Las reseñas que no se publican quedan acá.'],
    all: ['Todavía no hay reseñas', 'Cuando lleguen opiniones de clientes, van a aparecer acá.']
  };

  var CAMPOS_FORM = [
    { clave: 'order_ref', campo: 'campo-pedido', error: 'err-pedido' },
    { clave: 'product_slug', campo: 'campo-producto', error: 'err-producto' },
    { clave: 'product_label', campo: 'campo-etiqueta', error: 'err-etiqueta' },
    { clave: 'category', campo: 'campo-categoria', error: 'err-categoria' },
    { clave: 'dias', campo: 'campo-dias', error: 'err-dias' }
  ];

  // ── Secciones y sesión ──────────────────────────────────────────────────

  function mostrar(seccion) {
    ['seccion-acceso', 'seccion-panel', 'seccion-detalle'].forEach(function (id) {
      $(id).hidden = $(id) !== seccion;
    });
    $('seccion-actual').textContent = SECCION_TEXTO[seccion.id] || 'Administración de reseñas';
    $('principal').focus();
  }

  function verificarSesion() {
    return A.llamar('GET', '/sesion').then(function (r) {
      if (r.status === 200) {
        return true;
      }
      if (r.status === 401) {
        return false;
      }
      throw new Error('estado ' + r.status);
    });
  }

  function mostrarAcceso(mensaje, tipo) {
    mostrar($('seccion-acceso'));
    $('cerrar-sesion').hidden = true;
    $('estado-sesion').hidden = true;
    A.avisar($('aviso-global'), mensaje || '', tipo || 'aviso');
    $('pedir-acceso').disabled = false;
  }

  function entrar() {
    $('cerrar-sesion').hidden = false;
    $('estado-sesion').hidden = false;
    A.avisar($('aviso-global'), '', '');
    if (ID_DETALLE) {
      cargarDetalle(ID_DETALLE);
    } else {
      mostrar($('seccion-panel'));
      cargarTotales();
      cargarBandeja();
    }
  }

  function iniciar() {
    A.avisar($('aviso-global'), 'Verificando acceso…', 'aviso');
    verificarSesion().then(function (ok) {
      if (ok) {
        entrar();
      } else {
        mostrarAcceso();
      }
    }, function () {
      mostrarAcceso(A.MSG_RED, 'error');
    });
  }

  $('pedir-acceso').addEventListener('click', function () {
    if (ui.ocupado) {
      return;
    }
    ui.ocupado = true;
    var boton = $('pedir-acceso');
    boton.disabled = true;
    A.llamar('POST', '/sesion/solicitar', {}).then(function () {
      // Respuesta uniforme: no revela si el correo salió o si hubo límite.
      A.avisar($('aviso-acceso'), 'Si corresponde, te llegó un correo con el enlace.', 'ok');
      // Enfriamiento del botón para no disparar correos en cadena.
      window.setTimeout(function () {
        boton.disabled = false;
      }, 30000);
    }, function () {
      A.avisar($('aviso-acceso'), A.MSG_RED, 'error');
      boton.disabled = false;
    }).then(function () {
      ui.ocupado = false;
    });
  });

  $('cerrar-sesion').addEventListener('click', function () {
    if (ui.ocupado) {
      return;
    }
    ui.ocupado = true;
    A.llamar('POST', '/sesion/cerrar', {}).then(function () {
      mostrarAcceso('Cerraste la sesión.', 'ok');
    }, function () {
      A.avisar($('aviso-global'), A.MSG_RED, 'error');
    }).then(function () {
      ui.ocupado = false;
    });
  });

  // ── Crear solicitud (invitación) ────────────────────────────────────────

  function leerFormulario() {
    return {
      order_ref: $('campo-pedido').value.trim(),
      product_slug: $('campo-producto').value.trim(),
      product_label: $('campo-etiqueta').value.trim(),
      category: $('campo-categoria').value.trim(),
      dias: $('campo-dias').value.trim()
    };
  }

  function validarFormulario(datos) {
    var errores = {};
    if (!datos.order_ref) {
      errores.order_ref = 'Escribí la referencia del pedido.';
    } else if (datos.order_ref.length > 80) {
      errores.order_ref = 'Máximo 80 caracteres.';
    }
    if (!SLUG_RE.test(datos.product_slug) || datos.product_slug.length > 40) {
      errores.product_slug = 'Usá minúsculas, números y guiones (hasta 40 caracteres).';
    }
    if (!datos.product_label) {
      errores.product_label = 'Escribí el nombre del producto.';
    } else if (datos.product_label.length > 80) {
      errores.product_label = 'Máximo 80 caracteres.';
    }
    if (!SLUG_RE.test(datos.category) || datos.category.length > 40) {
      errores.category = 'Usá minúsculas, números y guiones (hasta 40 caracteres).';
    }
    if (!/^\d+$/.test(datos.dias) || Number(datos.dias) < 1 || Number(datos.dias) > 365) {
      errores.dias = 'Elegí entre 1 y 365 días.';
    }
    return errores;
  }

  function mostrarErroresCampos(errores) {
    CAMPOS_FORM.forEach(function (c) {
      var campo = $(c.campo);
      var nodo = $(c.error);
      var texto = errores[c.clave] || '';
      nodo.textContent = texto;
      nodo.hidden = !texto;
      campo.setAttribute('aria-invalid', texto ? 'true' : 'false');
    });
  }

  function primerCampoConError(errores) {
    for (var i = 0; i < CAMPOS_FORM.length; i++) {
      if (errores[CAMPOS_FORM[i].clave]) {
        return $(CAMPOS_FORM[i].campo);
      }
    }
    return null;
  }

  function esRechazoCsrf(r) {
    var error = r.datos && r.datos.error;
    return r.status === 403 || (r.status === 400 && CSRF_ERRORES.indexOf(error) !== -1);
  }

  function mostrarErroresDelServidor(resultado) {
    var campos = (resultado.datos && resultado.datos.campos) || {};
    var errores = {};
    Object.keys(campos).forEach(function (clave) {
      errores[clave] = String(campos[clave]);
    });
    mostrarErroresCampos(errores);
    var primero = primerCampoConError(errores);
    if (primero) {
      primero.focus();
    } else {
      A.avisar($('err-general'), A.mensajeDe(resultado, 'Revisá los datos del formulario.'), 'error');
    }
  }

  function mostrarEnlace(datos) {
    $('resultado-enlace').value = String(datos.enlace || '');
    $('resultado-vence').textContent = datos.vence ? 'Vence el ' + A.fecha(datos.vence) + '.' : '';
    $('resultado').hidden = false;
    A.avisar($('aviso-resultado'), '', '');
    $('compartir-enlace').hidden = typeof navigator.share !== 'function';
    $('resultado').focus();
  }

  function componerMensaje(enlace) {
    var texto = $('campo-mensaje').value.trim();
    if (!texto) {
      texto = MENSAJE_BASE.replace('{dias}', $('campo-dias').value.trim() || '15');
    }
    if (texto.indexOf('{link}') === -1) {
      return texto + '\n' + enlace;
    }
    return texto.split('{link}').join(enlace);
  }

  $('form-invitacion').addEventListener('submit', function (evento) {
    evento.preventDefault();
    if (ui.ocupado) {
      return;
    }
    A.avisar($('err-general'), '', '');
    var datos = leerFormulario();
    var errores = validarFormulario(datos);
    mostrarErroresCampos(errores);
    var primero = primerCampoConError(errores);
    if (primero) {
      primero.focus();
      return;
    }

    ui.ocupado = true;
    $('crear-link').disabled = true;
    var cuerpo = {
      order_ref: datos.order_ref,
      product_slug: datos.product_slug,
      product_label: datos.product_label,
      category: datos.category,
      dias: Number(datos.dias)
    };
    A.llamar('POST', '/invitaciones', cuerpo).then(function (r) {
      if (r.status === 201) {
        mostrarEnlace(r.datos);
        return;
      }
      if (r.status === 401) {
        mostrarAcceso(MSG_SESION, 'aviso');
        return;
      }
      if (esRechazoCsrf(r)) {
        A.avisar($('err-general'), MSG_RECARGAR, 'error');
        return;
      }
      if (r.status === 400) {
        mostrarErroresDelServidor(r);
        return;
      }
      A.avisar($('err-general'), A.mensajeDe(r, 'No pudimos crear el link. Probá de nuevo.'), 'error');
    }, function () {
      A.avisar($('err-general'), A.MSG_RED, 'error');
    }).then(function () {
      ui.ocupado = false;
      $('crear-link').disabled = false;
    });
  });

  function copiarTexto(texto) {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      return navigator.clipboard.writeText(texto);
    }
    // Respaldo para navegadores sin API de portapapeles: seleccionar y copiar.
    return new Promise(function (resolver, rechazar) {
      var campo = $('resultado-enlace');
      campo.select();
      if (document.execCommand && document.execCommand('copy')) {
        resolver();
      } else {
        rechazar(new Error('sin portapapeles'));
      }
    });
  }

  $('copiar-enlace').addEventListener('click', function () {
    var enlace = $('resultado-enlace').value;
    if (!enlace) {
      return;
    }
    copiarTexto(enlace).then(function () {
      A.avisar($('aviso-resultado'), 'Copiado.', 'ok');
    }, function () {
      $('resultado-enlace').select();
      A.avisar($('aviso-resultado'), 'No pudimos copiarlo automáticamente. Seleccionalo y copialo a mano.', 'error');
    });
  });

  $('compartir-enlace').addEventListener('click', function () {
    var enlace = $('resultado-enlace').value;
    if (!enlace || typeof navigator.share !== 'function') {
      return;
    }
    navigator.share({
      title: 'Tu opinión sobre tu pedido · ORK3D',
      text: componerMensaje(enlace),
      url: enlace
    }).catch(function () {
      // La persona canceló la hoja de compartir: no es un error.
    });
  });

  // ── Totales ─────────────────────────────────────────────────────────────

  // Cuenta por estado sobre la lectura de todas las reseñas (estado=all). Sin endpoint nuevo.
  function pintarTotales(items) {
    var cuenta = { pending: 0, approved: 0, hidden: 0, rejected: 0 };
    items.forEach(function (r) {
      if (Object.prototype.hasOwnProperty.call(cuenta, r.status)) {
        cuenta[r.status] += 1;
      }
    });
    $('total-pendientes').textContent = String(cuenta.pending);
    $('total-publicadas').textContent = String(cuenta.approved);
    $('total-ocultas').textContent = String(cuenta.hidden);
    $('total-rechazadas').textContent = String(cuenta.rejected);
  }

  function cargarTotales() {
    var solicitud = ++ui.solicitudTotales;
    return A.llamar('GET', '/resenas?estado=all').then(function (r) {
      if (solicitud !== ui.solicitudTotales) {
        return;
      }
      if (r.status === 401) {
        mostrarAcceso(MSG_SESION, 'aviso');
        return;
      }
      if (r.status !== 200) {
        throw new Error('estado ' + r.status);
      }
      pintarTotales((r.datos && r.datos.resenas) || []);
      A.avisar($('aviso-totales'), '', '');
    }).catch(function () {
      if (solicitud !== ui.solicitudTotales) {
        return;
      }
      A.avisar($('aviso-totales'), 'No pudimos actualizar los totales. La bandeja sigue funcionando.', 'error');
    });
  }

  // ── Bandeja ─────────────────────────────────────────────────────────────

  function activarPestana(boton, enfocar) {
    var botones = document.querySelectorAll('#tabs [role="tab"]');
    Array.prototype.forEach.call(botones, function (b) {
      var activa = b === boton;
      b.setAttribute('aria-selected', activa ? 'true' : 'false');
      b.setAttribute('tabindex', activa ? '0' : '-1');
    });
    ui.pestana = boton.getAttribute('data-estado');
    $('lista-resenas').setAttribute('aria-labelledby', boton.id);
    if (enfocar) {
      boton.focus();
    }
    cargarBandeja();
  }

  $('tabs').addEventListener('click', function (evento) {
    var boton = evento.target.closest ? evento.target.closest('[role="tab"]') : null;
    if (boton) {
      activarPestana(boton, false);
    }
  });

  $('tabs').addEventListener('keydown', function (evento) {
    var botones = Array.prototype.slice.call(document.querySelectorAll('#tabs [role="tab"]'));
    var i = botones.indexOf(document.activeElement);
    if (i < 0) {
      return;
    }
    var destino = null;
    if (evento.key === 'ArrowRight') {
      destino = botones[(i + 1) % botones.length];
    } else if (evento.key === 'ArrowLeft') {
      destino = botones[(i - 1 + botones.length) % botones.length];
    } else if (evento.key === 'Home') {
      destino = botones[0];
    } else if (evento.key === 'End') {
      destino = botones[botones.length - 1];
    }
    if (destino) {
      evento.preventDefault();
      activarPestana(destino, true);
    }
  });

  function pildoraEstado(status) {
    var info = A.ESTADOS[status] || { texto: status || '', clase: '' };
    return A.crear('span', { 'class': 'pildora ' + info.clase }, info.texto);
  }

  function avisoTexto(aviso) {
    if (aviso === 'enviado') {
      return 'Aviso por correo: enviado';
    }
    if (aviso === 'pendiente') {
      return 'Aviso por correo: pendiente';
    }
    return '';
  }

  // Datos de la reseña en una línea, y las señales (foto, pedido, aviso) como etiquetas.
  function avisoCorto(aviso) {
    if (aviso === 'enviado') {
      return 'Aviso enviado';
    }
    if (aviso === 'pendiente') {
      return 'Aviso pendiente';
    }
    return '';
  }

  function tarjeta(r) {
    var nombre = r.display_name || 'Cliente de ORK3D';
    var item = A.crear('li');
    var art = A.crear('article', { 'class': 'tarjeta', 'aria-labelledby': 'nombre-' + r.id });

    var cab = A.crear('div', { 'class': 'tarjeta__cab' });
    cab.appendChild(pildoraEstado(r.status));
    cab.appendChild(A.estrellas(r.rating));
    art.appendChild(cab);

    art.appendChild(A.crear('h3', { 'class': 'tarjeta__nombre', id: 'nombre-' + r.id }, nombre));

    var datos = [r.product_label || 'Sin nombre de producto', r.category || 'sin categoría',
      'recibida ' + (A.fecha(r.created_at) || 'sin fecha')];
    art.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, datos.join(' · ')));

    var senales = [r.tiene_foto ? 'Con foto' : 'Sin foto'];
    if (r.verified) {
      senales.push('Pedido verificado');
    }
    if (avisoCorto(r.aviso)) {
      senales.push(avisoCorto(r.aviso));
    }
    var chips = A.crear('ul', { 'class': 'tarjeta__chips', 'aria-label': 'Datos de la reseña' });
    senales.forEach(function (texto) {
      chips.appendChild(A.crear('li', null, texto));
    });
    art.appendChild(chips);

    var ver = A.crear('a', { 'class': 'btn btn--secundario', href: '/admin/resenas/' + encodeURIComponent(r.id) }, 'Ver');
    ver.appendChild(A.crear('span', { 'class': 'sr-only' }, ' la reseña de ' + nombre));
    art.appendChild(ver);

    item.appendChild(art);
    return item;
  }

  function pintarBandeja(items) {
    var lista = $('lista');
    A.vaciar(lista);
    A.avisar($('aviso-bandeja'), '', '');
    if (!items.length) {
      var vacio = A.crear('li', { 'class': 'lista__vacia' });
      var caja = A.crear('div', { 'class': 'vacio' });
      caja.appendChild(A.crear('p', { 'class': 'vacio__titulo' }, VACIO[ui.pestana] ? VACIO[ui.pestana][0] : VACIO.all[0]));
      caja.appendChild(A.crear('p', { 'class': 'vacio__texto' }, VACIO[ui.pestana] ? VACIO[ui.pestana][1] : VACIO.all[1]));
      vacio.appendChild(caja);
      lista.appendChild(vacio);
      return;
    }
    items.forEach(function (r) {
      lista.appendChild(tarjeta(r));
    });
  }

  // Mientras carga, tres bloques de contenido: la pantalla no queda en blanco ni salta al llegar la lista.
  function pintarEsqueleto() {
    var lista = $('lista');
    A.vaciar(lista);
    for (var i = 0; i < 3; i++) {
      lista.appendChild(A.crear('li', { 'class': 'esqueleto', 'aria-hidden': 'true' }));
    }
  }

  function cargarBandeja() {
    var solicitud = ++ui.solicitudBandeja;
    var lista = $('lista');
    pintarEsqueleto();
    lista.setAttribute('aria-busy', 'true');
    $('reintentar-bandeja').hidden = true;
    A.avisar($('aviso-bandeja'), 'Cargando reseñas…', 'aviso');

    return A.llamar('GET', '/resenas?estado=' + encodeURIComponent(ui.pestana)).then(function (r) {
      if (solicitud !== ui.solicitudBandeja) {
        return;
      }
      lista.setAttribute('aria-busy', 'false');
      if (r.status === 401) {
        mostrarAcceso(MSG_SESION, 'aviso');
        return;
      }
      if (r.status !== 200) {
        throw new Error('estado ' + r.status);
      }
      pintarBandeja((r.datos && r.datos.resenas) || []);
    }).catch(function () {
      if (solicitud !== ui.solicitudBandeja) {
        return;
      }
      lista.setAttribute('aria-busy', 'false');
      A.vaciar(lista);
      A.avisar($('aviso-bandeja'), 'No pudimos cargar la bandeja.', 'error');
      $('reintentar-bandeja').hidden = false;
    });
  }

  $('reintentar-bandeja').addEventListener('click', function () {
    cargarBandeja();
  });

  // ── Detalle ─────────────────────────────────────────────────────────────

  function bloque() {
    return A.crear('div', { 'class': 'detalle__bloque' });
  }

  function etiqueta(texto) {
    return A.crear('p', { 'class': 'detalle__etiqueta' }, texto);
  }

  // Fila superior: estado y fecha de recepción. Es lo primero que se lee.
  function filaEstado(r) {
    var fila = A.crear('div', { 'class': 'detalle__fila' });
    fila.appendChild(pildoraEstado(r.status));
    fila.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'Recibida ' + (A.fecha(r.created_at) || 'sin fecha')));
    return fila;
  }

  // Lo que ve el cliente: estrellas, producto y categoría.
  function bloqueIdentidad(r) {
    var b = bloque();
    var estrellasGrandes = A.estrellas(r.rating);
    estrellasGrandes.classList.add('estrellas--detalle');
    b.appendChild(estrellasGrandes);
    b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' },
      (r.product_label || 'Sin nombre de producto') + ' · ' + (r.category || 'sin categoría')));
    return b;
  }

  // Solo equipo: fechas de moderación, aviso y permisos. Nada de esto sale del panel.
  function bloqueModeracion(r) {
    var b = bloque();
    b.appendChild(etiqueta('Moderación'));
    b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'Última moderación: ' + (A.fecha(r.moderated_at) || 'todavía no')));
    if (r.published_at) {
      b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'Publicada el ' + A.fecha(r.published_at)));
    }
    if (avisoTexto(r.aviso)) {
      b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, avisoTexto(r.aviso)));
    }
    b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' },
      'Autorizó la publicación: ' + (r.permission_to_publish ? 'sí' : 'no') + (r.verified ? ' · pedido verificado' : '')));
    return b;
  }

  // Zona con título y nota. El título va en h2: el h1 es la reseña.
  function zona(clase, titulo, nota) {
    var z = A.crear('section', { 'class': 'zona ' + clase, 'aria-label': titulo });
    z.appendChild(A.crear('h2', { 'class': 'zona__titulo' }, titulo));
    if (nota) {
      z.appendChild(A.crear('p', { 'class': 'zona__nota' }, nota));
    }
    return z;
  }

  function bloqueFoto(r) {
    var b = bloque();
    b.appendChild(etiqueta('Foto'));
    if (!r.tiene_foto) {
      b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'Sin foto.'));
      return b;
    }
    var img = A.crear('img', {
      'class': 'detalle__foto',
      src: '/admin/api/resenas/' + encodeURIComponent(r.id) + '/foto',
      alt: 'Foto que subió el cliente para su reseña',
      loading: 'lazy'
    });
    var error = A.crear('p', { 'class': 'campo__error' }, 'No pudimos mostrar la foto.');
    error.hidden = true;
    img.addEventListener('error', function () {
      img.hidden = true;
      error.hidden = false;
    });
    b.appendChild(img);
    b.appendChild(error);
    return b;
  }

  function bloqueComentario(r) {
    var b = bloque();
    b.appendChild(etiqueta('Comentario'));
    b.appendChild(A.crear('p', { 'class': 'detalle__texto' }, r.comment || 'Sin comentario.'));
    return b;
  }

  function bloqueAspectos(r) {
    var b = bloque();
    b.appendChild(etiqueta('Aspectos'));
    var aspectos = Array.isArray(r.aspects) ? r.aspects : [];
    if (!aspectos.length) {
      b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'Sin aspectos marcados.'));
      return b;
    }
    var lista = A.crear('ul', { 'class': 'chips' });
    aspectos.forEach(function (a) {
      lista.appendChild(A.crear('li', null, A.ASPECTOS[a] || a));
    });
    b.appendChild(lista);
    return b;
  }

  function bloqueInterno(r) {
    var b = bloque();
    b.appendChild(etiqueta('Feedback privado'));
    b.appendChild(A.crear('p', { 'class': 'detalle__texto' }, r.private_feedback || 'Sin feedback privado.'));
    b.appendChild(etiqueta('Pedido interno'));
    b.appendChild(A.crear('p', { 'class': 'detalle__texto' }, r.pedido || 'Sin referencia de pedido.'));
    b.appendChild(etiqueta('Contacto'));
    if (r.contact_requested) {
      b.appendChild(A.crear('p', { 'class': 'detalle__texto' }, r.contact || 'Sin dato de contacto.'));
    } else {
      b.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'No pidió que lo contactemos.'));
    }
    return b;
  }

  // Acciones permitidas por estado (espejo de la tabla de transiciones de V2-ADMIN.md §6).
  function accionesPara(r) {
    var acciones = [];
    if (r.status === 'pending') {
      acciones.push({
        accion: 'aprobar', texto: 'Aprobar', clase: 'btn--primario', confirmar: true,
        titulo: '¿Aprobar esta reseña?', detalle: 'Se publica en el sitio y se actualiza el export.'
      });
      acciones.push({
        accion: 'rechazar', texto: 'Rechazar', clase: 'btn--secundario', confirmar: true,
        titulo: '¿Rechazar esta reseña?', detalle: 'No se publica. La foto privada se conserva.'
      });
    } else if (r.status === 'approved') {
      acciones.push({
        accion: 'ocultar', texto: 'Ocultar', clase: 'btn--secundario', confirmar: true, peligro: true,
        titulo: '¿Ocultar esta reseña?', detalle: 'Sale del sitio y se borra su foto pública. La foto privada se conserva.'
      });
      acciones.push({
        accion: 'rechazar', texto: 'Rechazar', clase: 'btn--peligro', confirmar: true, peligro: true,
        titulo: '¿Rechazar esta reseña publicada?', detalle: 'Sale del sitio y se borra su foto pública.'
      });
    } else if (r.status === 'hidden') {
      acciones.push({ accion: 'restaurar', texto: 'Restaurar', clase: 'btn--primario', confirmar: false });
    }
    if (r.aviso === 'pendiente') {
      acciones.push({ accion: 'reenviar-aviso', texto: 'Reenviar aviso', clase: 'btn--secundario', confirmar: false });
    }
    return acciones;
  }

  function setBotonesAccion(valor) {
    Array.prototype.forEach.call(document.querySelectorAll('#acciones-detalle [data-accion]'), function (b) {
      b.disabled = valor;
    });
    $('regenerar-export').disabled = valor;
  }

  function pintarAcciones(r) {
    var cont = $('acciones-detalle');
    A.vaciar(cont);
    var acciones = accionesPara(r);
    if (!acciones.length) {
      cont.appendChild(A.crear('p', { 'class': 'tarjeta__meta' }, 'No hay acciones disponibles para esta reseña.'));
    }
    acciones.forEach(function (a) {
      var boton = A.crear('button', { type: 'button', 'class': 'btn ' + a.clase, 'data-accion': a.accion }, a.texto);
      boton.addEventListener('click', function () {
        if (ui.ocupado) {
          return;
        }
        if (a.confirmar) {
          abrirConfirmacion(a.titulo, a.detalle, function () {
            ejecutarAccion(r.id, a);
          }, a.peligro);
        } else {
          ejecutarAccion(r.id, a);
        }
      });
      cont.appendChild(boton);
    });
  }

  function pintarDetalle(r) {
    $('titulo-detalle').textContent = 'Reseña de ' + (r.display_name || 'Cliente de ORK3D');
    var d = $('detalle');
    A.vaciar(d);
    d.appendChild(filaEstado(r));

    var cliente = zona('zona--cliente', 'Lo que ve el cliente',
      'Así se vería en el sitio si la publicás.');
    cliente.appendChild(bloqueIdentidad(r));
    cliente.appendChild(bloqueFoto(r));
    cliente.appendChild(bloqueComentario(r));
    cliente.appendChild(bloqueAspectos(r));

    var interna = zona('zona--interna', 'Información interna',
      'Solo para el equipo. Nunca se publica.');
    interna.appendChild(bloqueModeracion(r));
    interna.appendChild(bloqueInterno(r));

    d.appendChild(cliente);
    d.appendChild(interna);
    pintarAcciones(r);
    $('titulo-detalle').focus();
  }

  // tras: aviso que se muestra al terminar la recarga. Una acción ya cambió el estado, así que
  // el resultado de esa acción tiene que sobrevivir a la recarga.
  function cargarDetalle(id, tras) {
    tras = tras || {};
    mostrar($('seccion-detalle'));
    A.vaciar($('detalle'));
    A.vaciar($('acciones-detalle'));
    $('regenerar-export').hidden = !tras.exportPendiente;
    A.avisar($('aviso-detalle'), 'Cargando reseña…', 'aviso');

    return A.llamar('GET', '/resenas/' + encodeURIComponent(id)).then(function (r) {
      if (r.status === 200) {
        pintarDetalle(r.datos.resena);
        A.avisar($('aviso-detalle'), tras.texto || '', tras.tipo || '');
        return;
      }
      if (r.status === 401) {
        mostrarAcceso(MSG_SESION, 'aviso');
        return;
      }
      if (r.status === 404) {
        A.avisar($('aviso-detalle'), 'No encontramos esa reseña.', 'error');
        return;
      }
      throw new Error('estado ' + r.status);
    }).catch(function () {
      A.avisar($('aviso-detalle'), 'No pudimos cargar la reseña. Probá de nuevo.', 'error');
    });
  }

  // ── Confirmación (diálogo nativo) ───────────────────────────────────────

  function abrirConfirmacion(titulo, detalle, alConfirmar, peligro) {
    $('dialogo-titulo').textContent = titulo;
    $('dialogo-texto').textContent = detalle || '';
    ui.confirmacion = alConfirmar;
    // Confirmar queda deshabilitado un instante. Cada apertura invalida el temporizador de la anterior.
    var confirmar = $('dialogo-confirmar');
    var espera = $('dialogo-espera');
    confirmar.className = 'btn ' + (peligro ? 'btn--peligro' : 'btn--primario');
    confirmar.disabled = true;
    espera.hidden = false;
    ui.turnoConfirmacion += 1;
    var turno = ui.turnoConfirmacion;
    window.setTimeout(function () {
      if (turno === ui.turnoConfirmacion) {
        confirmar.disabled = false;
        espera.hidden = true;
      }
    }, ESPERA_CONFIRMAR_MS);
    var dialogo = $('dialogo');
    if (typeof dialogo.showModal === 'function') {
      dialogo.showModal();
    } else {
      dialogo.setAttribute('open', '');
    }
    $('dialogo-cancelar').focus();
  }

  function cerrarConfirmacion() {
    var dialogo = $('dialogo');
    if (typeof dialogo.close === 'function') {
      if (dialogo.open) {
        dialogo.close();
      }
    } else {
      dialogo.removeAttribute('open');
    }
  }

  $('dialogo-cancelar').addEventListener('click', function () {
    ui.confirmacion = null;
    cerrarConfirmacion();
  });

  $('dialogo-confirmar').addEventListener('click', function () {
    var accion = ui.confirmacion;
    ui.confirmacion = null;
    cerrarConfirmacion();
    if (accion) {
      accion();
    }
  });

  $('dialogo').addEventListener('cancel', function () {
    ui.confirmacion = null;
  });

  // Tab no sale del diálogo: con Confirmar deshabilitado, Cancelar es el único control y el navegador
  // mandaría el foco a la barra del navegador. Se vuelve al primer o al último control habilitado.
  $('dialogo').addEventListener('keydown', function (evento) {
    if (evento.key !== 'Tab') {
      return;
    }
    var dialogo = $('dialogo');
    var habilitados = Array.prototype.slice.call(dialogo.querySelectorAll('button:not([disabled])'));
    var primero = habilitados[0];
    var ultimo = habilitados[habilitados.length - 1];
    var activo = document.activeElement;
    var fueraDelDialogo = !dialogo.contains(activo);
    if (evento.shiftKey && (activo === primero || fueraDelDialogo)) {
      evento.preventDefault();
      ultimo.focus();
    } else if (!evento.shiftKey && (activo === ultimo || fueraDelDialogo)) {
      evento.preventDefault();
      primero.focus();
    }
  });

  // ── Acciones de moderación ──────────────────────────────────────────────

  function resolverAccion(id, a, r) {
    if (r.status === 401) {
      mostrarAcceso(MSG_SESION, 'aviso');
      return null;
    }
    if (r.status === 200 || r.status === 202) {
      cargarTotales();
    }
    if (r.status === 200) {
      if (r.datos && r.datos.export === 'pendiente') {
        return cargarDetalle(id, { texto: MSG_EXPORT_PENDIENTE, tipo: 'aviso', exportPendiente: true });
      }
      return cargarDetalle(id, { texto: EXITO[a.accion] || 'Listo.', tipo: 'ok' });
    }
    if (r.status === 202) {
      return cargarDetalle(id, { texto: MSG_EXPORT_PENDIENTE, tipo: 'aviso', exportPendiente: true });
    }
    if (r.status === 409) {
      return cargarDetalle(id, { texto: A.mensajeDe(r, 'Esa acción ya no corresponde a esta reseña.'), tipo: 'error' });
    }
    if (r.status === 503) {
      var error = r.datos && r.datos.error;
      if (error === 'foto_no_publicada') {
        A.avisar($('aviso-detalle'), 'No se pudo publicar la foto. No cambió nada: probá de nuevo.', 'error');
      } else if (error === 'aviso_fallido') {
        A.avisar($('aviso-detalle'), 'El correo no salió. Probá de nuevo más tarde.', 'error');
      } else {
        A.avisar($('aviso-detalle'), A.mensajeDe(r, 'No pudimos completar la acción. Probá de nuevo.'), 'error');
      }
      return null;
    }
    if (esRechazoCsrf(r)) {
      A.avisar($('aviso-detalle'), MSG_RECARGAR, 'error');
      return null;
    }
    A.avisar($('aviso-detalle'), A.mensajeDe(r, 'No pudimos completar la acción. Probá de nuevo.'), 'error');
    return null;
  }

  function ejecutarAccion(id, a) {
    if (ui.ocupado) {
      return;
    }
    ui.ocupado = true;
    setBotonesAccion(true);
    A.avisar($('aviso-detalle'), 'Enviando…', 'aviso');
    var ruta = '/resenas/' + encodeURIComponent(id) + '/' + a.accion;
    A.llamar('POST', ruta, {}).then(function (r) {
      return resolverAccion(id, a, r);
    }, function () {
      A.avisar($('aviso-detalle'), A.MSG_RED, 'error');
    }).then(function () {
      ui.ocupado = false;
      setBotonesAccion(false);
    });
  }

  $('regenerar-export').addEventListener('click', function () {
    if (ui.ocupado) {
      return;
    }
    ui.ocupado = true;
    var boton = $('regenerar-export');
    boton.disabled = true;
    A.llamar('POST', '/exportar', {}).then(function (r) {
      if (r.status === 200) {
        boton.hidden = true;
        A.avisar($('aviso-detalle'), 'Export actualizado.', 'ok');
        return;
      }
      if (r.status === 401) {
        mostrarAcceso(MSG_SESION, 'aviso');
        return;
      }
      A.avisar($('aviso-detalle'), 'El export todavía no se pudo actualizar. Probá de nuevo en unos minutos.', 'aviso');
    }, function () {
      A.avisar($('aviso-detalle'), A.MSG_RED, 'error');
    }).then(function () {
      ui.ocupado = false;
      boton.disabled = false;
    });
  });

  iniciar();
})();
