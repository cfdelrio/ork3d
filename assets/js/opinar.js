(function () {
  'use strict';

  var cfg = window.ORK3D_CONFIG || {};
  var API = String(cfg.RESENAS_API_URL || '').replace(/\/+$/, '');
  var GOOGLE_URL = cfg.GOOGLE_REVIEW_URL || '';
  var WA_URL = 'https://wa.me/' + (cfg.WHATSAPP_NUMBER || '');

  // Los límites de foto reflejan PHOTO_MIN_BYTES / PHOTO_MAX_BYTES de api/resenas/app.py.
  var FOTO_MIN = 15000;
  var FOTO_MAX = 2000000;
  var MAX_LADO = 1600;
  var CALIDAD_JPG = 0.85;
  var NOMBRE_RE = /^[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ' .\-]{1,39}$/;

  var RUTA_RE = /^\/opinar\/([A-Za-z0-9_-]{32})\/?$/;
  var coincidencia = RUTA_RE.exec(window.location.pathname);
  var token = coincidencia ? coincidencia[1] : '';

  var ESTADOS = {
    invalido: {
      titulo: 'Este enlace no es válido',
      texto: 'Revisá que lo hayas copiado completo. Si el problema sigue, escribinos por WhatsApp y te ayudamos.'
    },
    vencido: {
      titulo: 'Este enlace venció',
      texto: 'Escribinos por WhatsApp y te mandamos uno nuevo.'
    },
    revocado: {
      titulo: 'Este enlace ya no está disponible',
      texto: 'Escribinos por WhatsApp si necesitás ayuda con tu pedido.'
    },
    usado: {
      titulo: 'Ya recibimos tu opinión',
      texto: 'Gracias por tomarte el tiempo. Si querés cambiar algo, escribinos por WhatsApp.'
    },
    error: {
      titulo: 'No pudimos verificar tu enlace',
      texto: 'Probá de nuevo en unos minutos.'
    }
  };

  var MENSAJE_FOTO = {
    pesada: 'La foto pesa más de 2 MB. Probá con una más chica.',
    chica: 'La foto es muy chica. Probá con otra.',
    imagen: 'No pudimos leer esa imagen. Probá con una JPG o PNG.',
    subida: 'No pudimos subir la foto. Probá de nuevo o enviá la reseña sin foto.'
  };

  // Qué elemento recibe el foco cuando un campo tiene error.
  var FOCO = {
    rating: 'rating-5',
    comment: 'comentario',
    photo_key: 'foto',
    display_mode: 'modo-inicial',
    name: 'nombre',
    contact: 'contacto',
    permission_to_publish: 'permiso',
    private_feedback: 'privado'
  };

  function $(id) { return document.getElementById(id); }

  var el = {
    titulo: $('titulo'),
    estado: $('estado'),
    form: $('formulario'),
    producto: $('producto'),
    comentario: $('comentario'),
    cuenta: $('cuenta-comentario'),
    foto: $('foto'),
    fotoBloque: $('foto-bloque'),
    fotoPreview: $('foto-preview'),
    fotoQuitar: $('foto-quitar'),
    fotoEstado: $('foto-estado'),
    nombre: $('nombre'),
    bloqueNombre: $('bloque-nombre'),
    pideContacto: $('pide-contacto'),
    bloqueContacto: $('bloque-contacto'),
    contacto: $('contacto'),
    enviar: $('enviar'),
    general: $('err-general'),
    gracias: $('gracias'),
    googleCta: $('google-cta')
  };

  var enviando = false;
  var fotoKey = null;
  var fotoSubiendo = false;
  var fotoTicket = 0;
  var fotoPreviewUrl = null;

  // ── Red ───────────────────────────────────────────────────────────────
  function llamar(ruta, opciones) {
    return fetch(API + ruta, opciones).then(function (r) {
      return r.json().then(
        function (body) { return { status: r.status, body: body || {} }; },
        function () { return { status: r.status, body: {} }; }
      );
    });
  }

  function sinRespuesta() {
    return { status: 0, body: {} };
  }

  // ── Estados de pantalla ──────────────────────────────────────────────
  function enfocar(nodo) {
    nodo.focus();
  }

  function mostrarEstado(clave) {
    var m = ESTADOS[clave] || ESTADOS.error;
    el.titulo.textContent = m.titulo;
    el.estado.innerHTML = '';
    var p = document.createElement('p');
    p.textContent = m.texto;
    var wa = document.createElement('a');
    wa.className = 'btn btn--ghost';
    wa.href = WA_URL;
    wa.target = '_blank';
    wa.rel = 'noopener noreferrer';
    wa.textContent = 'Escribir por WhatsApp';
    el.estado.append(p, wa);
    el.estado.hidden = false;
    el.form.hidden = true;
    enfocar(el.titulo);
  }

  function mostrarFormulario(datos) {
    el.titulo.textContent = 'Contanos cómo te llegó';
    var texto = 'Tu pedido: ' + (datos.producto || 'ORK3D');
    if (datos.vence) texto += ' · podés dejar tu opinión hasta el ' + formatoFecha(datos.vence);
    el.producto.textContent = texto;
    el.estado.hidden = true;
    el.form.hidden = false;
  }

  function mostrarGracias(googleUrl) {
    el.titulo.textContent = 'Gracias por tu opinión';
    el.form.hidden = true;
    el.estado.hidden = true;
    el.gracias.hidden = false;
    var url = googleUrl || GOOGLE_URL;
    if (url) {
      el.googleCta.href = url;
      el.googleCta.hidden = false;
    }
    enfocar(el.titulo);
  }

  function formatoFecha(iso) {
    var partes = String(iso).split('-');
    return partes.length === 3 ? partes[2] + '/' + partes[1] + '/' + partes[0] : iso;
  }

  // ── Errores de campo ─────────────────────────────────────────────────
  function mostrarError(campo, texto) {
    var p = $('err-' + campo);
    if (!p) {
      mostrarGeneral(texto);
      return;
    }
    p.textContent = texto;
    p.hidden = false;
  }

  function mostrarGeneral(texto) {
    el.general.textContent = texto;
    el.general.hidden = false;
  }

  function limpiarErrores() {
    var errores = el.form.querySelectorAll('.campo__error');
    for (var i = 0; i < errores.length; i++) {
      errores[i].hidden = true;
      errores[i].textContent = '';
    }
    el.general.hidden = true;
  }

  function limpiarCampo(ev) {
    var p = $('err-' + ev.target.name);
    if (p) {
      p.hidden = true;
      p.textContent = '';
    }
    el.general.hidden = true;
  }

  function enfocarPrimerError(campos) {
    var claves = Object.keys(campos);
    for (var i = 0; i < claves.length; i++) {
      var destino = FOCO[claves[i]];
      if (destino && $(destino)) {
        $(destino).focus();
        return;
      }
    }
    if (claves.length) el.general.focus();
  }

  // ── Validación (espejo de la del servidor; el servidor manda) ────────
  function leerFormulario() {
    var rating = el.form.querySelector('input[name=rating]:checked');
    var modo = el.form.querySelector('input[name=display_mode]:checked');
    var aspectos = el.form.querySelectorAll('input[name=aspects]:checked');
    var lista = [];
    for (var i = 0; i < aspectos.length; i++) lista.push(aspectos[i].value);
    var anonimo = modo && modo.value === 'anonimo';
    return {
      token: token,
      rating: rating ? Number(rating.value) : null,
      aspects: lista,
      comment: el.comentario.value.trim(),
      display_mode: modo ? modo.value : 'inicial',
      name: anonimo ? '' : el.nombre.value.trim().replace(/\s+/g, ' '),
      private_feedback: $('privado').value.trim(),
      contact_requested: el.pideContacto.checked,
      contact: el.contacto.value.trim(),
      permission_to_publish: $('permiso').checked,
      photo_key: fotoKey,
      website: el.form.elements.website.value
    };
  }

  function validar(d) {
    var errores = {};
    if (!d.rating) errores.rating = 'Elegí entre 1 y 5 estrellas.';
    if (d.display_mode !== 'anonimo' && !NOMBRE_RE.test(d.name)) {
      errores.name = 'Poné tu nombre (solo letras).';
    }
    if (d.contact_requested && !d.contact) errores.contact = 'Escribí cómo te contactamos.';
    if (!d.permission_to_publish) {
      errores.permission_to_publish = 'Necesitamos tu autorización para publicar la reseña.';
    }
    return errores;
  }

  function mostrarCampos(campos) {
    var claves = Object.keys(campos);
    for (var i = 0; i < claves.length; i++) {
      if (claves[i] === 'photo_key') fotoKey = null;
      mostrarError(claves[i] === '_' ? 'general' : claves[i], campos[claves[i]]);
    }
    enfocarPrimerError(campos);
  }

  // ── Foto: se reduce y re-codifica en el navegador (quita EXIF) y se sube firmada ──
  function reducirImagen(archivo) {
    return new Promise(function (resolver, rechazar) {
      var url = URL.createObjectURL(archivo);
      var img = new Image();
      img.onload = function () {
        URL.revokeObjectURL(url);
        var escala = Math.min(1, MAX_LADO / Math.max(img.naturalWidth, img.naturalHeight));
        var canvas = document.createElement('canvas');
        canvas.width = Math.round(img.naturalWidth * escala);
        canvas.height = Math.round(img.naturalHeight * escala);
        var ctx = canvas.getContext('2d');
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(function (blob) {
          if (blob) resolver(blob);
          else rechazar(new Error('imagen'));
        }, 'image/jpeg', CALIDAD_JPG);
      };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        rechazar(new Error('imagen'));
      };
      img.src = url;
    });
  }

  function subirFoto(blob) {
    return llamar('/presign', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token, ext: 'jpg' })
    }).catch(sinRespuesta).then(function (res) {
      if (res.status === 403) {
        mostrarEstado(res.body.motivo || 'error');
        throw new Error('cortado');
      }
      if (res.status !== 200 || !res.body.ok) throw new Error('subida');
      var datos = new FormData();
      var campos = res.body.fields;
      Object.keys(campos).forEach(function (k) { datos.append(k, campos[k]); });
      datos.append('file', blob, 'foto.jpg');  // el archivo va último: así lo exige S3
      return fetch(res.body.url, { method: 'POST', body: datos }).then(function (r) {
        if (!r.ok) throw new Error('subida');
        return res.body.key;
      }, function () {
        throw new Error('subida');
      });
    });
  }

  function mostrarPreview(blob) {
    if (fotoPreviewUrl) URL.revokeObjectURL(fotoPreviewUrl);
    fotoPreviewUrl = URL.createObjectURL(blob);
    el.fotoPreview.src = fotoPreviewUrl;
    el.fotoBloque.hidden = false;
  }

  function setFotoEstado(texto) {
    el.fotoEstado.textContent = texto;
  }

  function refrescarBoton() {
    el.enviar.disabled = enviando || fotoSubiendo;
    if (enviando) el.enviar.textContent = 'Enviando…';
    else if (fotoSubiendo) el.enviar.textContent = 'Subiendo foto…';
    else el.enviar.textContent = 'Enviar reseña';
  }

  function descartarFoto() {
    fotoTicket++;
    fotoKey = null;
    fotoSubiendo = false;
    if (fotoPreviewUrl) {
      URL.revokeObjectURL(fotoPreviewUrl);
      fotoPreviewUrl = null;
    }
    el.fotoPreview.removeAttribute('src');
    el.fotoBloque.hidden = true;
    setFotoEstado('');
    refrescarBoton();
  }

  function procesarFoto(archivo) {
    var mio = ++fotoTicket;
    fotoKey = null;
    fotoSubiendo = true;
    refrescarBoton();
    setFotoEstado('Preparando la foto…');
    el.foto.setAttribute('aria-busy', 'true');
    reducirImagen(archivo).then(function (blob) {
      if (blob.size > FOTO_MAX) throw new Error('pesada');
      if (blob.size < FOTO_MIN) throw new Error('chica');
      if (mio !== fotoTicket) return null;
      mostrarPreview(blob);
      setFotoEstado('Subiendo la foto…');
      return subirFoto(blob);
    }).then(function (clave) {
      if (mio !== fotoTicket || !clave) return;
      fotoKey = clave;
      setFotoEstado('Foto lista.');
    }).catch(function (err) {
      if (mio !== fotoTicket || err.message === 'cortado') return;
      descartarFoto();
      mostrarError('photo_key', MENSAJE_FOTO[err.message] || MENSAJE_FOTO.imagen);
    }).then(function () {
      if (mio !== fotoTicket) return;
      fotoSubiendo = false;
      el.foto.removeAttribute('aria-busy');
      refrescarBoton();
    });
  }

  // ── Envío ────────────────────────────────────────────────────────────
  function procesarRespuesta(res) {
    var b = res.body;
    if (res.status === 200 && b.ok) return mostrarGracias(b.google_url);
    if (res.status === 202) return mostrarGracias(null);  // honeypot: misma respuesta que un envío real
    if (res.status === 400 && b.campos) return mostrarCampos(b.campos);
    if (res.status === 403 || res.status === 409) return mostrarEstado(b.motivo || 'usado');
    mostrarGeneral('No pudimos enviar tu reseña. Probá de nuevo en unos minutos.');
  }

  function enviar(ev) {
    ev.preventDefault();
    if (enviando || fotoSubiendo) return;
    limpiarErrores();
    var datos = leerFormulario();
    var errores = validar(datos);
    if (Object.keys(errores).length) {
      mostrarCampos(errores);
      return;
    }
    enviando = true;
    refrescarBoton();
    llamar('/resenas', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(datos)
    }).catch(sinRespuesta).then(function (res) {
      enviando = false;
      refrescarBoton();
      procesarRespuesta(res);
    });
  }

  // ── Eventos del formulario ──────────────────────────────────────────
  function actualizarContador() {
    el.cuenta.textContent = String(el.comentario.value.length);
  }

  function actualizarModo() {
    var modo = el.form.querySelector('input[name=display_mode]:checked');
    el.bloqueNombre.hidden = !modo || modo.value === 'anonimo';
  }

  function iniciarFormulario() {
    el.form.addEventListener('submit', enviar);
    el.form.addEventListener('input', limpiarCampo);
    el.form.addEventListener('change', limpiarCampo);
    el.comentario.addEventListener('input', actualizarContador);
    el.form.addEventListener('change', function (ev) {
      if (ev.target.name === 'display_mode') actualizarModo();
    });
    el.pideContacto.addEventListener('change', function () {
      el.bloqueContacto.hidden = !el.pideContacto.checked;
    });
    el.foto.addEventListener('change', function () {
      var archivo = el.foto.files && el.foto.files[0];
      descartarFoto();
      if (archivo) procesarFoto(archivo);
    });
    el.fotoQuitar.addEventListener('click', function () {
      el.foto.value = '';
      descartarFoto();
    });
    actualizarContador();
    actualizarModo();
  }

  // ── Arranque ─────────────────────────────────────────────────────────
  function iniciar() {
    var anios = $('year');
    if (anios) anios.textContent = String(new Date().getFullYear());
    var enlaces = document.querySelectorAll('[data-wa-link]');
    for (var i = 0; i < enlaces.length; i++) {
      enlaces[i].href = WA_URL;
      enlaces[i].target = '_blank';
      enlaces[i].rel = 'noopener noreferrer';
    }

    iniciarFormulario();

    if (!token) {
      mostrarEstado('invalido');
      return;
    }
    if (!API) {
      mostrarEstado('error');
      return;
    }
    llamar('/tokens/' + token, { method: 'GET' }).catch(sinRespuesta).then(function (res) {
      if (res.status === 200 && res.body.ok) return mostrarFormulario(res.body);
      if (res.status === 200 && res.body.motivo) return mostrarEstado(res.body.motivo);
      mostrarEstado('error');
    });
  }

  iniciar();
})();
