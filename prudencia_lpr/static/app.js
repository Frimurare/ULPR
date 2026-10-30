(function () {
  'use strict';
  var CSRF = (document.querySelector('meta[name=csrf-token]') || {}).content || '';
  var page = document.body.dataset.page;

  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function post(url, data) {
    return fetch(url, { method: 'POST', body: data, headers: { 'X-CSRF-Token': CSRF }, credentials: 'same-origin' })
      .then(function (r) {
        if (r.status === 401) { location.href = '/logga-in'; throw new Error('Inte inloggad'); }
        return r.json();
      });
  }

  // bekräftelse på formulär med data-confirm
  document.querySelectorAll('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (ev) { if (!confirm(f.dataset.confirm)) ev.preventDefault(); });
  });

  // ---------------------------------------------------------------- översikt
  function badgeFor(r) {
    var cls = r.status === 'sent' ? 'ok' : (r.status === 'send_failed' || r.status === 'rate_limited') ? 'bad' : 'off';
    var txt = r.status === 'sent' ? (r.ows_status + ' Skickad') :
      (r.ows_status && r.ows_status !== 202 ? r.ows_status + ' ' : '') + r.status_text;
    var b = el('span', 'badge ' + cls, txt);
    if (r.ows_detail) b.title = r.ows_detail;
    return b;
  }
  function setTile(id, value, cls, sub) {
    var v = $(id); v.textContent = value; v.className = 'tile-value' + (cls ? ' ' + cls : '');
    if (sub !== undefined) $(id + '-sub').textContent = sub;
  }
  function renderOverview(d) {
    var s = d.system;
    $('updated').textContent = 'Uppdaterad ' + d.now;
    setTile('t-engine', s.engine_alive ? 'Kör' : 'Stoppad', s.engine_alive ? 'ok' : 'bad',
      s.engine_alive ? 'Sedan ' + s.engine_since : 'Analysmotorn svarar inte');
    $('t-today').textContent = s.today;
    $('t-failed').textContent = s.failed ? s.failed + ' ej levererade till OpenEye' : 'Alla levererade';
    if (!s.oe_enabled) setTile('t-oe', 'Avstängd', 'off', 'Läsningar sparas bara lokalt');
    else if (s.oe_last === null || s.oe_last === undefined) setTile('t-oe', 'Aktiverad', '', 'Inget skickat sedan start');
    else setTile('t-oe', s.oe_last === 202 ? 'OK' : 'Fel ' + s.oe_last, s.oe_last === 202 ? 'ok' : 'bad',
      s.oe_last_ok ? 'Senast mottagen ' + s.oe_last_ok : '');
    if (s.gpu) {
      setTile('t-gpu', s.gpu.util + ' %', '', 'Minne ' + (s.gpu.used_mib / 1024).toFixed(1) + ' / ' +
        (s.gpu.total_mib / 1024).toFixed(1) + ' GB' + (s.engine_gpu_mib ? ', LPR ' + s.engine_gpu_mib + ' MB' : '') +
        (s.det_ms ? ', ' + s.det_ms + ' ms/bild' : ''));
    } else if (s.accel && s.accel.name) {
      setTile('t-gpu', s.accel.name, 'ok', (s.accel.temp ? 'Krets ' + s.accel.temp + ' °C' : '') +
        (s.accel.cpu_temp ? ', CPU ' + s.accel.cpu_temp + ' °C' : '') + (s.det_ms ? ', ' + s.det_ms + ' ms/bild' : ''));
    } else setTile('t-gpu', 'Ingen', 'off', 'Ingen GPU hittades');
    var dd = s.data;
    if (dd.used_mb === null) setTile('t-data', '–', 'off', dd.count + ' läsningar');
    else setTile('t-data', (dd.used_mb >= 1000 ? (dd.used_mb / 1000).toFixed(2) + ' GB' : dd.used_mb + ' MB'),
      dd.pct !== null && dd.pct >= 100 ? 'bad' : '',
      (dd.limit_gb ? 'av ' + dd.limit_gb + ' GB (' + dd.pct + ' %)' : 'Ingen maxgräns') + ', ' + dd.count + ' läsningar' +
      (dd.oldest ? ', äldsta ' + dd.oldest.slice(0, 10) : ''));
    setTile('t-cpu', Math.round(s.cpu) + ' %', '', 'Hela servern');
    setTile('t-mem', Math.round(s.mem_pct) + ' %', '', s.mem_used_gib + ' / ' + s.mem_total_gib + ' GB' +
      (s.engine_rss_mib ? ', LPR ' + s.engine_rss_mib + ' MB' : ''));

    var cams = $('cams'); cams.textContent = '';
    if (!d.cameras.length) {
      var e = el('div', 'card empty'); e.appendChild(document.createTextNode('Inga kameror. '));
      var a = el('a', null, 'Lägg till en kamera'); a.href = '/kameror/ny'; e.appendChild(a); cams.appendChild(e);
    }
    d.cameras.forEach(function (c) {
      var card = el('div', 'cam ' + c.state);
      var head = el('div', 'cam-head');
      var name = el('a', 'cam-name', c.name); name.href = '/kameror/' + c.id;
      head.appendChild(name);
      var st = el('span', 'badge ' + (c.state === 'ok' ? 'ok' : c.state === 'bad' ? 'bad' : 'off'));
      st.appendChild(el('span', 'dot ' + c.state)); st.appendChild(document.createTextNode(c.state_text));
      head.appendChild(st); card.appendChild(head);
      var dl = el('dl');
      function kv(k, v) { dl.appendChild(el('dt', null, k)); dl.appendChild(el('dd', null, v)); }
      kv('Bildfrekvens', c.fps !== null ? c.fps + ' analyserade/s (' + c.fps_in + ' in)' : '–');
      if (c.resolution) kv('Upplösning', c.resolution);
      kv('Senaste läsning', c.last_plate ? c.last_plate + ', ' + c.last_ts : 'Ingen ännu');
      kv('Läsningar idag', String(c.today));
      kv('Återanslutningar', c.reconnects !== null ? String(c.reconnects) : '–');
      if (!c.has_ext_id) kv('OpenEye', 'Kamera-id saknas');
      card.appendChild(dl); cams.appendChild(card);
    });

    var tb = document.querySelector('#recent tbody'); tb.textContent = '';
    if (!d.recent.length) {
      var tr0 = el('tr'); var td0 = el('td', 'muted', 'Inga läsningar ännu.'); td0.colSpan = 6; tr0.appendChild(td0); tb.appendChild(tr0);
    }
    d.recent.forEach(function (r) {
      var tr = el('tr');
      tr.appendChild(el('td', 'nowrap', r.ts));
      tr.appendChild(el('td', null, r.camera));
      var tdp = el('td'); tdp.appendChild(el('span', 'plate', r.plate)); tr.appendChild(tdp);
      var tdi = el('td');
      if (r.has_crop) {
        var a2 = el('a'); a2.href = '/bild/' + r.id + '/oversikt'; a2.target = '_blank'; a2.rel = 'noopener';
        var img = el('img', 'thumb'); img.src = '/bild/' + r.id + '/skylt'; img.alt = 'Skylt ' + r.plate; img.loading = 'lazy';
        a2.appendChild(img); tdi.appendChild(a2);
      }
      tr.appendChild(tdi);
      tr.appendChild(el('td', null, r.confidence + ' %'));
      var tdo = el('td'); tdo.appendChild(badgeFor(r)); tr.appendChild(tdo);
      tb.appendChild(tr);
    });
  }
  function pollOverview() {
    fetch('/api/oversikt', { credentials: 'same-origin' }).then(function (r) {
      if (r.status === 401) { location.href = '/logga-in'; return null; }
      return r.json();
    }).then(function (d) { if (d) renderOverview(d); })
      .catch(function () { $('updated').textContent = 'Kunde inte hämta status, försöker igen …'; });
  }
  if (page === 'overview') { pollOverview(); setInterval(pollOverview, 5000); }

  // ---------------------------------------------------------------- kamera
  if (page === 'camera') {
    var form = $('camform');
    var modeInputs = form.querySelectorAll('input[name=source_mode]');
    function applyMode() {
      var m = form.querySelector('input[name=source_mode]:checked').value;
      form.querySelectorAll('[data-mode]').forEach(function (d) { d.hidden = d.dataset.mode !== m; });
    }
    modeInputs.forEach(function (i) { i.addEventListener('change', applyMode); }); applyMode();

    var canvas = $('roi-canvas'), ctx = canvas.getContext('2d'), img = new Image(), full = null, drag = null;
    function roiValue() {
      var v = $('roi').value.split(',').map(function (x) { return parseInt(x, 10); });
      return v.length === 4 && v.every(function (x) { return !isNaN(x); }) ? v : null;
    }
    function draw(tmp) {
      if (!full) return;
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      var r = tmp || roiValue(); if (!r) return;
      var k = canvas.width / full.w;
      ctx.fillStyle = 'rgba(0,0,0,0.45)';
      ctx.beginPath(); ctx.rect(0, 0, canvas.width, canvas.height);
      ctx.rect(r[0] * k, r[1] * k, (r[2] - r[0]) * k, (r[3] - r[1]) * k); ctx.fill('evenodd');
      ctx.strokeStyle = '#facc15'; ctx.lineWidth = 2; ctx.strokeRect(r[0] * k, r[1] * k, (r[2] - r[0]) * k, (r[3] - r[1]) * k);
    }
    function toFull(ev) {
      var b = canvas.getBoundingClientRect(), k = full.w / b.width;
      return [Math.max(0, Math.min(full.w, Math.round((ev.clientX - b.left) * k))),
        Math.max(0, Math.min(full.h, Math.round((ev.clientY - b.top) * k)))];
    }
    function rectOf(a, b) { return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])]; }
    canvas.addEventListener('pointerdown', function (ev) { if (!full) return; drag = toFull(ev); canvas.setPointerCapture(ev.pointerId); });
    canvas.addEventListener('pointermove', function (ev) { if (drag) draw(rectOf(drag, toFull(ev))); });
    canvas.addEventListener('pointerup', function (ev) {
      if (!drag) return; var r = rectOf(drag, toFull(ev)); drag = null;
      if (r[2] - r[0] > 20 && r[3] - r[1] > 20) $('roi').value = r.join(',');
      draw();
    });
    $('roi').addEventListener('input', function () { draw(); });
    $('roi-clear').addEventListener('click', function () { $('roi').value = ''; draw(); });

    $('btn-test').addEventListener('click', function () {
      var btn = this; btn.disabled = true; $('test-msg').textContent = 'Ansluter till kameran …';
      post('/api/kameror/testa', new FormData(form)).then(function (d) {
        btn.disabled = false;
        if (!d.ok) { $('test-msg').textContent = d.error; return; }
        $('test-msg').textContent = 'Anslutningen fungerar: ' + d.width + 'x' + d.height + ' (' + d.ms + ' ms).';
        img.onload = function () {
          full = { w: d.width, h: d.height };
          canvas.width = img.naturalWidth; canvas.height = img.naturalHeight;
          canvas.hidden = false; $('preview-empty').hidden = true; draw();
        };
        img.src = d.image;
      }).catch(function (e) { btn.disabled = false; $('test-msg').textContent = 'Fel: ' + e.message; });
    });

    $('rec-fetch').addEventListener('click', function () {
      var btn = this, fd = new FormData();
      fd.append('host', $('rec-host').value); fd.append('user', $('rec-user').value);
      fd.append('password', $('rec-pass').value); fd.append('recorder_id', $('rec-id').value);
      btn.disabled = true; $('rec-msg').textContent = 'Hämtar kameralistan …';
      post('/api/openeye/recorder', fd).then(function (d) {
        btn.disabled = false;
        var t = $('rec-table'), tb = t.querySelector('tbody'); tb.textContent = '';
        if (!d.ok) { $('rec-msg').textContent = d.error; t.hidden = true; return; }
        $('rec-msg').textContent = d.cameras.length + ' kameror hittades.' + ($('rec-id').value ? '' : ' Ange recorder-id för att kunna fylla i kamera-id.');
        d.cameras.forEach(function (c) {
          var tr = el('tr');
          tr.appendChild(el('td', null, c.name)); tr.appendChild(el('td', 'mono small', c.ip || ''));
          tr.appendChild(el('td', 'small', [c.manufacturer, c.model].filter(Boolean).join(' ')));
          tr.appendChild(el('td', 'small', c.state === 'InService' ? 'I drift' : (c.state || '')));
          var td = el('td', 'right nowrap');
          if (c.external_id) {
            var b = el('button', 'btn small primary', 'Använd id'); b.type = 'button';
            b.addEventListener('click', function () {
              $('external_id').value = c.external_id;
              if (!form.querySelector('input[name=name]').value) form.querySelector('input[name=name]').value = c.name;
              $('rec-msg').textContent = 'Kamera-id för "' + c.name + '" ifyllt. Spara kameran för att använda det.';
            });
            td.appendChild(b);
          }
          var main = (c.streams || []).filter(function (s) { return s.url; }).sort(function (a, b2) { return (b2.w || 0) - (a.w || 0); })[0];
          if (main) {
            var b3 = el('button', 'btn small', 'Använd ström'); b3.type = 'button';
            b3.title = main.url + (main.w ? ' (' + main.w + 'x' + main.h + ')' : '');
            b3.addEventListener('click', function () {
              form.querySelector('input[name=source_mode][value=url]').checked = true; applyMode();
              form.querySelector('input[name=rtsp_url]').value = main.url;
              $('rec-msg').textContent = 'RTSP-adress ifylld från recordern. Ange kamerans inloggning och testa anslutningen.';
            });
            td.appendChild(document.createTextNode(' ')); td.appendChild(b3);
          }
          tr.appendChild(td); tb.appendChild(tr);
        });
        t.hidden = false;
      }).catch(function (e) { btn.disabled = false; $('rec-msg').textContent = 'Fel: ' + e.message; });
    });
  }

  // ---------------------------------------------------------------- OpenEye
  if (page === 'openeye' && $('oe-test')) {
    $('oe-test').addEventListener('submit', function (ev) {
      ev.preventDefault();
      var f = this, btn = f.querySelector('button'), res = $('oe-test-result');
      btn.disabled = true; res.hidden = false; res.className = 'result'; res.textContent = 'Skickar …';
      post('/api/openeye/test', new FormData(f)).then(function (d) {
        btn.disabled = false;
        res.className = 'result ' + (d.ok ? 'ok' : 'bad');
        res.textContent = (d.code !== null && d.code !== undefined ? 'Svarskod ' + d.code + ': ' : '') + d.text;
      }).catch(function (e) { btn.disabled = false; res.className = 'result bad'; res.textContent = 'Fel: ' + e.message; });
    });
  }

  // ---------------------------------------------------------------- utseende
  if (page === 'appearance') {
    $('logo-file').addEventListener('change', function () {
      var f = this.files[0];
      $('logo-name').textContent = f ? f.name + ' (' + Math.round(f.size / 1024) + ' kB)' : 'Ingen fil vald';
    });
  }

  // ---------------------------------------------------------------- system
  if (page === 'system') {
    function loadLog() {
      var which = document.querySelector('input[name=logfile]:checked').value;
      fetch('/api/logg?fil=' + which, { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
        var pre = $('log'); pre.textContent = d.lines.length ? d.lines.join('\n') : 'Loggen är tom.';
        pre.scrollTop = pre.scrollHeight;
      });
    }
    document.querySelectorAll('input[name=logfile]').forEach(function (i) { i.addEventListener('change', loadLog); });
    $('log-refresh').addEventListener('click', loadLog);
    loadLog();
  }
})();
