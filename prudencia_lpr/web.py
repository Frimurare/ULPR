"""Webbgränssnitt (Flask) för Prudencia LPR. Allt på svenska."""
import base64
import csv
import datetime
import functools
import io
import json
import logging
import logging.handlers
import os
import secrets
import threading
import time

import psutil
from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import PRODUCT, VERSION
from .camera import cv2, describe, grab_frame
from .openeye import EventClient, build_camera_id, decode_camera_id, recorder_cameras
from .store import BRAND_DIR, HITS_DIR, LOG_DIR, STATUS_PATH, Store

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOGO = os.path.join(HERE, 'static', 'prudencia-skolden.png')
LOGO_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'svg': 'image/svg+xml'}
MAX_LOGO = 2 * 1024 * 1024
MASK = '••••••••'
STATUS_TEXT = {'sent': 'Skickad', 'send_failed': 'Misslyckad', 'queued': 'I kö', 'logged': 'Endast loggad',
               'rate_limited': 'Hastighetsgräns', 'no_camera_id': 'Kamera-id saknas'}

wlog = logging.getLogger('lpr.web')
store = Store()
app = Flask(__name__, template_folder=os.path.join(HERE, 'templates'), static_folder=os.path.join(HERE, 'static'))
app.secret_key = store.get('secret_key')
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_NAME='plpr_session',
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12), MAX_CONTENT_LENGTH=MAX_LOGO + 64 * 1024,
                  JSON_AS_ASCII=False)
_fails = {}
_resend = {'running': False, 'done': 0, 'total': 0, 'last': None, 'finished': None}


def setup_web_logging():
    fh = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, 'web.log'), maxBytes=5_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    wlog.addHandler(fh)
    wlog.setLevel(logging.INFO)
    wlog.propagate = False


# ---------------------------------------------------------------- hjälpare
def csrf_token():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    return session['_csrf']


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def before():
    g.s = store.all_settings()
    if request.method == 'POST':
        tok = request.form.get('_csrf') or request.headers.get('X-CSRF-Token')
        if not tok or not secrets.compare_digest(tok, session.get('_csrf', '')):
            abort(400, 'Ogiltig eller saknad säkerhetstoken. Ladda om sidan och försök igen.')


@app.after_request
def headers(resp):
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'same-origin'
    resp.headers.setdefault('Content-Security-Policy',
                            "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
                            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
    if request.endpoint not in ('static', 'logo'):
        resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.context_processor
def ctx():
    return {'product': PRODUCT, 'version': VERSION, 'company': g.s.get('company_name', '') if hasattr(g, 's') else '',
            'logo_rev': g.s.get('logo_file', '') if hasattr(g, 's') else ''}


def login_required(f):
    @functools.wraps(f)
    def wrap(*a, **kw):
        if not session.get('auth'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Inte inloggad'}), 401
            return redirect(url_for('login', next=request.path))
        if g.s['must_change_pw'] and request.endpoint not in ('change_password', 'logout'):
            return redirect(url_for('change_password'))
        return f(*a, **kw)
    return wrap


def engine_status():
    try:
        with open(STATUS_PATH) as f:
            st = json.load(f)
        st['alive'] = time.time() - st.get('updated', 0) < 15
        return st
    except (OSError, ValueError):
        return {'alive': False, 'cameras': {}}


def data_info(st):
    """Driftdata: storlek från motorns cache (status.json), antal och äldsta läsning från databasen."""
    d = st.get('data') or {}
    n = store.query('SELECT COUNT(*) n, MIN(ts_local) first FROM readings')[0]
    limit_gb = float(store.get('max_data_gb') or 0)
    used = d.get('data_bytes')
    return {'used_bytes': used, 'used_mb': round(used / 1e6, 1) if used is not None else None,
            'used_gb': round(used / 1e9, 3) if used is not None else None, 'limit_gb': limit_gb,
            'pct': round(100 * used / (limit_gb * 1e9)) if used is not None and limit_gb > 0 else None,
            'db_mb': round(d.get('db_file_bytes', 0) / 1e6, 2), 'hits_mb': round(d.get('hits_bytes', 0) / 1e6, 1),
            'count': n['n'], 'oldest': (n['first'] or '').replace('T', ' '),
            'last_prune': store.get('last_prune_info') or ''}


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else ''


def as_int(v, default=None, lo=None, hi=None):
    try:
        x = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None and x < lo or hi is not None and x > hi:
        return default
    return x


def as_float(v, default=None, lo=None, hi=None):
    try:
        x = float(str(v).replace(',', '.'))
    except (TypeError, ValueError):
        return default
    if lo is not None and x < lo or hi is not None and x > hi:
        return default
    return x


def parse_roi(txt):
    txt = (txt or '').strip()
    if not txt:
        return None
    try:
        v = [int(float(x)) for x in txt.replace(';', ',').split(',')]
    except ValueError:
        raise ValueError('ROI ska anges som x1,y1,x2,y2 i pixlar.')
    if len(v) != 4 or v[2] <= v[0] or v[3] <= v[1] or min(v) < 0:
        raise ValueError('ROI ska anges som x1,y1,x2,y2 där x2 > x1 och y2 > y1.')
    return v


def gpu_info():
    try:
        import pynvml
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        m = pynvml.nvmlDeviceGetMemoryInfo(h)
        u = pynvml.nvmlDeviceGetUtilizationRates(h)
        name = pynvml.nvmlDeviceGetName(h)
        return {'name': name.decode() if isinstance(name, bytes) else name, 'used_mib': m.used // 2**20,
                'total_mib': m.total // 2**20, 'util': u.gpu,
                'temp': pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)}
    except Exception:
        return None


# ---------------------------------------------------------------- inloggning
@app.route('/logga-in', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.remote_addr or '-'
        n, until = _fails.get(ip, (0, 0))
        if until > time.time():
            flash(f'För många misslyckade försök. Försök igen om {int(until - time.time())} sekunder.', 'error')
            return render_template('login.html'), 429
        if check_password_hash(store.get('admin_hash'), request.form.get('password', '')):
            _fails.pop(ip, None)
            session.clear()
            session.permanent = True
            session['auth'] = True
            csrf_token()
            wlog.info('Inloggning lyckades från %s', ip)
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('overview'))
        n += 1
        _fails[ip] = (0, time.time() + 60) if n >= 5 else (n, 0)
        wlog.warning('Misslyckad inloggning från %s', ip)
        flash('Fel lösenord.', 'error')
    return render_template('login.html')


@app.route('/logga-ut', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/byt-losenord', methods=['GET', 'POST'])
def change_password():
    if not session.get('auth'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        cur, new, rep = (request.form.get(k, '') for k in ('current', 'new', 'repeat'))
        if not check_password_hash(store.get('admin_hash'), cur):
            flash('Nuvarande lösenord stämmer inte.', 'error')
        elif len(new) < 8:
            flash('Det nya lösenordet måste ha minst 8 tecken.', 'error')
        elif new != rep:
            flash('Lösenorden matchar inte.', 'error')
        elif new == 'Prudencia':
            flash('Välj ett annat lösenord än standardlösenordet.', 'error')
        else:
            store.set_many({'admin_hash': generate_password_hash(new), 'must_change_pw': 0}, bump=False)
            wlog.info('Administratörslösenordet byttes')
            flash('Lösenordet är bytt.', 'ok')
            return redirect(url_for('overview'))
    return render_template('password.html', forced=bool(g.s['must_change_pw']))


# ---------------------------------------------------------------- logotyp
@app.route('/logotyp')
def logo():
    f = g.s.get('logo_file')
    path = os.path.join(BRAND_DIR, f) if f else ''
    if f and os.path.isfile(path):
        ext = f.rsplit('.', 1)[-1].lower()
        resp = send_file(path, mimetype=LOGO_TYPES.get(ext, 'application/octet-stream'), max_age=300)
        if ext == 'svg':
            resp.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return resp
    return send_file(DEFAULT_LOGO, mimetype='image/png', max_age=300)


@app.route('/utseende', methods=['GET', 'POST'])
@login_required
def appearance():
    if request.method == 'POST':
        name = (request.form.get('company_name') or '').strip()[:80]
        values = {'company_name': name}
        up = request.files.get('logo')
        if up and up.filename:
            ext = up.filename.rsplit('.', 1)[-1].lower() if '.' in up.filename else ''
            data = up.read(MAX_LOGO + 1)
            if ext not in LOGO_TYPES:
                flash('Logotypen måste vara PNG, JPG eller SVG.', 'error')
                return redirect(url_for('appearance'))
            if len(data) > MAX_LOGO:
                flash('Logotypen får vara högst 2 MB.', 'error')
                return redirect(url_for('appearance'))
            ok = (ext == 'png' and data[:8] == b'\x89PNG\r\n\x1a\n') or \
                 (ext in ('jpg', 'jpeg') and data[:3] == b'\xff\xd8\xff') or \
                 (ext == 'svg' and b'<svg' in data[:4096].lower())
            if not ok:
                flash('Filen verkar inte vara en giltig bild av angiven typ.', 'error')
                return redirect(url_for('appearance'))
            if ext == 'svg' and (b'<script' in data.lower() or b'javascript:' in data.lower()):
                flash('SVG-filer med skript tillåts inte.', 'error')
                return redirect(url_for('appearance'))
            for old in os.listdir(BRAND_DIR):
                if old.startswith('logo'):
                    os.remove(os.path.join(BRAND_DIR, old))
            fname = f'logo-{int(time.time())}.{"jpg" if ext == "jpeg" else ext}'
            with open(os.path.join(BRAND_DIR, fname), 'wb') as fh:
                fh.write(data)
            values['logo_file'] = fname
            wlog.info('Ny logotyp uppladdad (%s, %d byte)', ext, len(data))
        store.set_many(values, bump=False)
        flash('Utseendet är sparat.', 'ok')
        return redirect(url_for('appearance'))
    return render_template('appearance.html', custom=bool(g.s.get('logo_file')))


@app.route('/utseende/aterstall', methods=['POST'])
@login_required
def appearance_reset():
    for old in os.listdir(BRAND_DIR):
        if old.startswith('logo'):
            os.remove(os.path.join(BRAND_DIR, old))
    store.set('logo_file', '', bump=False)
    flash('Logotypen är återställd till Prudencia-skölden.', 'ok')
    return redirect(url_for('appearance'))


# ---------------------------------------------------------------- översikt
@app.route('/')
@login_required
def overview():
    return render_template('overview.html')


def readings_for_ui(rows):
    out = []
    for r in rows:
        out.append({'id': r['id'], 'ts': (r['ts_local'] or '').replace('T', ' '), 'camera': r['camera'], 'plate': r['plate'],
                    'confidence': round((r['confidence'] or 0) * 100), 'status': r['status'],
                    'status_text': STATUS_TEXT.get(r['status'], r['status'] or ''), 'ows_status': r['ows_status'],
                    'ows_detail': r.get('ows_detail') or '', 'has_crop': bool(r['crop']),
                    'has_ctx': bool(r['context'])})
    return out


@app.route('/api/oversikt')
@login_required
def api_overview():
    st = engine_status()
    today = datetime.date.today().isoformat()
    counts = {r['camera_id']: r['n'] for r in store.query(
        'SELECT camera_id, COUNT(*) n FROM readings WHERE ts_local >= ? GROUP BY camera_id', (today,))}
    last = {r['camera_id']: r for r in store.query(
        'SELECT camera_id, plate, MAX(ts_local) ts FROM readings GROUP BY camera_id')}
    cams = []
    for c in store.cameras():
        live = st.get('cameras', {}).get(str(c['id'])) if st['alive'] else None
        if not c['enabled']:
            state, text = 'off', 'Inaktiverad'
        elif not live:
            state, text = 'bad', 'Motorn kör inte' if not st['alive'] else 'Startar'
        elif live['connected']:
            state, text = 'ok', 'Ansluten'
        else:
            state, text = 'bad', live.get('error') or 'Ej ansluten'
        lr = last.get(c['id'])
        cams.append({'id': c['id'], 'name': c['name'], 'state': state, 'state_text': text,
                     'fps': live['fps_proc'] if live else None, 'fps_in': live['fps_in'] if live else None,
                     'reconnects': live['reconnects'] if live else None,
                     'resolution': 'x'.join(map(str, live['resolution'])) if live and live.get('resolution') else '',
                     'today': counts.get(c['id'], 0), 'last_plate': lr['plate'] if lr else '',
                     'last_ts': (lr['ts'] or '').replace('T', ' ') if lr else '',
                     'has_ext_id': bool(c['external_id'])})
    vm = psutil.virtual_memory()
    proc_rss = None
    if st.get('pid'):
        try:
            proc_rss = psutil.Process(st['pid']).memory_info().rss // 2**20
        except psutil.Error:
            pass
    sysinfo = {'cpu': psutil.cpu_percent(interval=None), 'mem_pct': vm.percent, 'mem_used_gib': round(vm.used / 2**30, 1),
               'mem_total_gib': round(vm.total / 2**30, 1), 'engine_rss_mib': proc_rss, 'gpu': gpu_info(),
               'engine_gpu_mib': st.get('gpu_mib'), 'det_ms': st.get('det_ms'), 'accel': st.get('accel'), 'engine_alive': st['alive'],
               'engine_since': fmt_ts(st.get('started')) if st['alive'] else '', 'queue': st.get('queue'),
               'oe_enabled': bool(g.s['oe_enabled']), 'oe_last': st.get('ows_last_status'),
               'oe_last_ok': fmt_ts(st.get('ows_last_ok')), 'version': VERSION}
    sysinfo['failed'] = store.query("SELECT COUNT(*) n FROM readings WHERE status IN ('send_failed','rate_limited')")[0]['n']
    sysinfo['today'] = sum(counts.values())
    sysinfo['data'] = data_info(st)
    recent = readings_for_ui(store.query('SELECT * FROM readings ORDER BY id DESC LIMIT 20'))
    return jsonify({'cameras': cams, 'system': sysinfo, 'recent': recent, 'now': fmt_ts(time.time())})


@app.route('/bild/<int:rid>/<kind>')
@login_required
def image(rid, kind):
    rows = store.query('SELECT crop, context FROM readings WHERE id=?', (rid,))
    if not rows or kind not in ('skylt', 'oversikt'):
        abort(404)
    rel = rows[0]['crop' if kind == 'skylt' else 'context']
    if not rel:
        abort(404)
    return send_from_directory(HITS_DIR, rel, max_age=86400)


# ---------------------------------------------------------------- kameror
def camera_form_defaults():
    return {'id': None, 'name': '', 'enabled': 1, 'source_mode': 'host', 'host': '', 'port': 554,
            'path': '/axis-media/media.amp?videocodec=h264&fps=10', 'username': '', 'password': '', 'rtsp_url': '',
            'external_id': '', 'min_plate_w': None, 'target_fps': None, 'roi': None}


def camera_from_form(f, existing=None):
    """Validerar formuläret. Tomt lösenord vid ändring = behåll befintligt."""
    errors = []
    c = {'name': (f.get('name') or '').strip()[:60], 'enabled': 1 if f.get('enabled') else 0,
         'source_mode': 'url' if f.get('source_mode') == 'url' else 'host', 'host': (f.get('host') or '').strip(),
         'port': as_int(f.get('port'), 554, 1, 65535), 'path': (f.get('path') or '').strip(),
         'username': (f.get('username') or '').strip(), 'rtsp_url': (f.get('rtsp_url') or '').strip(),
         'external_id': (f.get('external_id') or '').strip(),
         'min_plate_w': as_int(f.get('min_plate_w'), None, 20, 1000) if (f.get('min_plate_w') or '').strip() else None,
         'target_fps': as_float(f.get('target_fps'), None, 0.5, 30) if (f.get('target_fps') or '').strip() else None}
    pw = f.get('password') or ''
    c['password'] = (existing or {}).get('password', '') if (pw == '' or pw == MASK) else pw
    if f.get('clear_password'):
        c['password'] = ''
    try:
        c['roi'] = parse_roi(f.get('roi'))
    except ValueError as e:
        errors.append(str(e))
        c['roi'] = None
    if not c['name']:
        errors.append('Ange ett namn på kameran.')
    if c['source_mode'] == 'host' and not c['host']:
        errors.append('Ange kamerans adress (IP eller värdnamn).')
    if c['source_mode'] == 'url' and not c['rtsp_url'].lower().startswith(('rtsp://', 'rtsps://')):
        errors.append('RTSP-adressen måste börja med rtsp://')
    if c['external_id'] and not decode_camera_id(c['external_id']):
        errors.append('OpenEye kamera-id ska ha formatet _OE_ följt av base64(recorderId|kamerans uuid).')
    return c, errors


@app.route('/kameror')
@login_required
def cameras():
    cams = store.cameras()
    for c in cams:
        c['source'] = describe(c)
    return render_template('cameras.html', cams=cams)


@app.route('/kameror/ny', methods=['GET', 'POST'])
@app.route('/kameror/<int:cid>', methods=['GET', 'POST'])
@login_required
def camera_edit(cid=None):
    existing = store.camera(cid) if cid else None
    if cid and not existing:
        abort(404)
    if request.method == 'POST':
        c, errors = camera_from_form(request.form, existing)
        if not errors:
            new_id = store.save_camera(c, cid)
            wlog.info('Kamera "%s" %s', c['name'], 'ändrad' if cid else 'tillagd')
            flash(f'Kameran "{c["name"]}" är sparad. Analysmotorn läser in ändringen inom några sekunder.', 'ok')
            return redirect(url_for('cameras'))
        for e in errors:
            flash(e, 'error')
        c['id'] = cid
        cam = c
    else:
        cam = existing or camera_form_defaults()
    view = dict(cam)
    view['has_password'] = bool(cam.get('password'))
    view['password'] = ''
    view['roi_text'] = ','.join(map(str, cam['roi'])) if cam.get('roi') else ''
    dec = decode_camera_id(cam.get('external_id') or '')
    return render_template('camera_edit.html', cam=view, decoded=dec, s=g.s,
                           default_w=g.s['default_min_plate_w'], default_fps=g.s['default_fps'])


@app.route('/kameror/<int:cid>/ta-bort', methods=['POST'])
@login_required
def camera_delete(cid):
    c = store.camera(cid)
    if c:
        store.delete_camera(cid)
        wlog.info('Kamera "%s" borttagen', c['name'])
        flash(f'Kameran "{c["name"]}" är borttagen. Tidigare läsningar finns kvar i historiken.', 'ok')
    return redirect(url_for('cameras'))


@app.route('/api/kameror/testa', methods=['POST'])
@login_required
def api_camera_test():
    cid = as_int(request.form.get('id'))
    existing = store.camera(cid) if cid else None
    c, errors = camera_from_form(request.form, existing)
    errors = [e for e in errors if 'namn' not in e and 'kamera-id' not in e and 'ROI' not in e]
    if errors:
        return jsonify({'ok': False, 'error': ' '.join(errors)})
    t0 = time.time()
    frame, err = grab_frame(c)
    if frame is None:
        return jsonify({'ok': False, 'error': err})
    h, w = frame.shape[:2]
    scale = min(1.0, 1280 / w)
    small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
    ok, jpg = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return jsonify({'ok': True, 'width': w, 'height': h, 'ms': int((time.time() - t0) * 1000),
                    'image': 'data:image/jpeg;base64,' + base64.b64encode(jpg.tobytes()).decode()})


@app.route('/api/openeye/recorder', methods=['POST'])
@login_required
def api_recorder_cameras():
    host = (request.form.get('host') or '').strip()
    user = (request.form.get('user') or '').strip()
    pw = request.form.get('password') or ''
    rec_id = (request.form.get('recorder_id') or '').strip()
    if not host or not user:
        return jsonify({'ok': False, 'error': 'Ange recorderns adress och användarnamn.'})
    try:
        cams = recorder_cameras(host, user, pw)
    except Exception as e:
        msg = str(e)
        if '401' in msg:
            msg = 'Inloggningen nekades (401). Kontrollera användarnamn och lösenord.'
        return jsonify({'ok': False, 'error': f'Kunde inte hämta kameralistan: {msg[:200]}'})
    store.set_many({'oe_recorder_host': host, 'oe_recorder_user': user, 'oe_recorder_id': rec_id}, bump=False)
    for c in cams:
        c['external_id'] = build_camera_id(rec_id, c['uuid']) if rec_id and c['uuid'] else ''
    return jsonify({'ok': True, 'cameras': cams})


# ---------------------------------------------------------------- OpenEye
@app.route('/openeye', methods=['GET', 'POST'])
@login_required
def openeye():
    if request.method == 'POST':
        f = request.form
        url = (f.get('oe_url') or '').strip().rstrip('/')
        if url and not url.startswith('https://'):
            flash('Event API-adressen ska börja med https://', 'error')
            return redirect(url_for('openeye'))
        values = {'oe_enabled': 1 if f.get('oe_enabled') else 0, 'oe_url': url,
                  'rl_min': as_int(f.get('rl_min'), g.s['rl_min'], 1, 50),
                  'rl_hour': as_int(f.get('rl_hour'), g.s['rl_hour'], 1, 500),
                  'rl_day': as_int(f.get('rl_day'), g.s['rl_day'], 1, 5000)}
        aki = (f.get('oe_aki') or '').strip()
        if aki:
            values['oe_aki'] = aki
        aks = f.get('oe_aks') or ''
        if aks and aks != MASK:
            values['oe_aks'] = aks.strip()
        if f.get('clear_keys'):
            values.update(oe_aki='', oe_aks='', oe_enabled=0)
        store.set_many(values)
        wlog.info('OpenEye-inställningar sparade (aktiverad=%s)', values['oe_enabled'])
        flash('OpenEye-inställningarna är sparade.', 'ok')
        return redirect(url_for('openeye'))
    failed = store.query("SELECT COUNT(*) n FROM readings WHERE status IN ('send_failed','rate_limited')")[0]['n']
    no_id = store.query("SELECT COUNT(*) n FROM cameras WHERE enabled=1 AND (external_id IS NULL OR external_id='')")[0]['n']
    aki = g.s['oe_aki']
    aki_hint = (aki[:3] + '•' * max(0, len(aki) - 5) + aki[-2:]) if len(aki) > 6 else ('•' * len(aki))
    return render_template('openeye.html', s=g.s, has_aks=bool(g.s['oe_aks']), aki_hint=aki_hint, mask=MASK, failed=failed, no_id=no_id,
                           cams=[c for c in store.cameras() if c['external_id']], resend=_resend)


def oe_client():
    s = store.all_settings()
    if not (s['oe_aki'] and s['oe_aks'] and s['oe_url']):
        return None
    return EventClient(s['oe_url'], s['oe_aki'], s['oe_aks'])


@app.route('/api/openeye/test', methods=['POST'])
@login_required
def api_oe_test():
    cl = oe_client()
    if not cl:
        return jsonify({'ok': False, 'code': None, 'text': 'Ange AKI, AKS och Event API-adress och spara först.'})
    cam = store.camera(as_int(request.form.get('camera_id'), 0))
    if not cam or not cam['external_id']:
        return jsonify({'ok': False, 'code': None, 'text': 'Välj en kamera som har OpenEye kamera-id.'})
    plate = (request.form.get('plate') or 'TST 001').strip().upper()[:10]
    code, txt = cl.post_plate(cam['external_id'], plate, time.time())
    wlog.info('Testhändelse kamera="%s" skylt=%s -> %s', cam['name'], plate, code)
    explain = {202: 'Mottagen av OpenEye.', 401: 'Nyckeln känns inte igen: kontrollera AKI, AKS och att adressen hör till rätt konto.',
               400: 'OpenEye avvisade händelsen: kontrollera kamera-id.', -1: 'Nätverksfel, OpenEye kunde inte nås.'}
    return jsonify({'ok': code == 202, 'code': code, 'text': explain.get(code, txt[:200])})


def _resend_worker(rows):
    cl = oe_client()
    _resend.update(running=True, done=0, total=len(rows), last=None, finished=None)
    try:
        for r in rows:
            cam = store.camera(r['camera_id']) if r['camera_id'] else None
            ext = cam['external_id'] if cam else ''
            if not ext:
                store.update_reading(r['id'], status='no_camera_id')
                continue
            code, txt = cl.post_plate(ext, r['plate'], datetime.datetime.fromisoformat(r['ts_utc']).timestamp())
            store.update_reading(r['id'], ows_status=code, status='sent' if code == 202 else 'send_failed',
                                 ows_detail='' if code == 202 else txt[:200])
            _resend['done'] += 1
            _resend['last'] = code
            if code != 202:
                wlog.warning('Omsändning avbruten: id=%s -> %s', r['id'], code)
                break
            time.sleep(2.0)
    finally:
        _resend.update(running=False, finished=fmt_ts(time.time()))
        wlog.info('Omsändning klar: %d av %d', _resend['done'], _resend['total'])


@app.route('/openeye/skicka-om', methods=['POST'])
@login_required
def oe_resend():
    if _resend['running']:
        flash('En omsändning pågår redan.', 'error')
    elif not oe_client():
        flash('OpenEye är inte konfigurerat.', 'error')
    else:
        rows = store.query("SELECT id, camera_id, plate, ts_utc FROM readings WHERE status IN ('send_failed','rate_limited') "
                           "ORDER BY id LIMIT 200")
        if not rows:
            flash('Det finns inga misslyckade händelser att skicka om.', 'ok')
        else:
            threading.Thread(target=_resend_worker, args=(rows,), daemon=True).start()
            flash(f'Omsändning av {len(rows)} händelser har startat (en var 2:a sekund).', 'ok')
    return redirect(url_for('openeye'))


# ---------------------------------------------------------------- historik
def history_filter(a):
    where, args = [], []
    if a.get('fran'):
        where.append('ts_local >= ?'); args.append(a['fran'])
    if a.get('till'):
        where.append('ts_local < ?')
        try:
            args.append((datetime.date.fromisoformat(a['till']) + datetime.timedelta(days=1)).isoformat())
        except ValueError:
            args.append(a['till'])
    if a.get('kamera'):
        where.append('camera_id = ?'); args.append(as_int(a['kamera'], 0))
    if a.get('skylt'):
        q = ''.join(ch for ch in a['skylt'].upper() if ch.isalnum())
        if q:
            where.append("REPLACE(plate,' ','') LIKE ?"); args.append(f'%{q}%')
    if a.get('status'):
        where.append('status = ?'); args.append(a['status'])
    return (' WHERE ' + ' AND '.join(where)) if where else '', args


@app.route('/historik')
@login_required
def history():
    w, args = history_filter(request.args)
    per = 50
    page = max(1, as_int(request.args.get('sida'), 1))
    total = store.query(f'SELECT COUNT(*) n FROM readings{w}', args)[0]['n']
    rows = readings_for_ui(store.query(f'SELECT * FROM readings{w} ORDER BY ts_local DESC, id DESC LIMIT ? OFFSET ?',
                                       args + [per, (page - 1) * per]))
    pages = max(1, (total + per - 1) // per)
    cams = store.query('SELECT DISTINCT camera_id id, camera name FROM readings WHERE camera_id IS NOT NULL ORDER BY camera')
    qs = {k: v for k, v in request.args.items() if k != 'sida' and v}
    return render_template('history.html', rows=rows, total=total, page=page, pages=pages, cams=cams, a=request.args,
                           qs=qs, statuses=STATUS_TEXT)


@app.route('/historik.csv')
@login_required
def history_csv():
    w, args = history_filter(request.args)
    rows = store.query(f'SELECT * FROM readings{w} ORDER BY ts_local DESC, id DESC LIMIT 100000', args)
    buf = io.StringIO()
    buf.write('﻿')
    wr = csv.writer(buf, delimiter=';')
    wr.writerow(['Tid', 'Kamera', 'Skylt', 'Säkerhet (%)', 'Röster', 'Status', 'OpenEye-svar'])
    for r in rows:
        wr.writerow([(r['ts_local'] or '').replace('T', ' '), r['camera'], r['plate'], round((r['confidence'] or 0) * 100),
                     r['votes'], STATUS_TEXT.get(r['status'], r['status']), r['ows_status'] or ''])
    fn = f"lasningar-{datetime.date.today().isoformat()}.csv"
    return Response(buf.getvalue(), content_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{fn}"'})


# ---------------------------------------------------------------- system
@app.route('/system', methods=['GET', 'POST'])
@login_required
def system():
    if request.method == 'POST':
        f, s = request.form, g.s
        values = {
            'retention_days': as_int(f.get('retention_days'), s['retention_days'], 1, 3650),
            'max_data_gb': round(as_float(f.get('max_data_gb'), s['max_data_gb'], 0, 100000), 3),
            'log_level': f.get('log_level') if f.get('log_level') in ('DEBUG', 'INFO', 'WARNING', 'ERROR') else s['log_level'],
            'web_port': as_int(f.get('web_port'), s['web_port'], 1024, 65535),
            'save_rejects': 1 if f.get('save_rejects') else 0,
            'default_min_plate_w': as_int(f.get('default_min_plate_w'), s['default_min_plate_w'], 20, 1000),
            'default_fps': as_float(f.get('default_fps'), s['default_fps'], 0.5, 30),
            'det_conf': as_float(f.get('det_conf'), s['det_conf'], 0.05, 0.95),
            'min_char_conf': as_float(f.get('min_char_conf'), s['min_char_conf'], 0.05, 0.99),
            'early_votes': as_int(f.get('early_votes'), s['early_votes'], 1, 20),
            'min_votes': as_int(f.get('min_votes'), s['min_votes'], 1, 20),
            'track_timeout_s': as_float(f.get('track_timeout_s'), s['track_timeout_s'], 0.5, 30),
            'dedup_s': as_int(f.get('dedup_s'), s['dedup_s'], 0, 86400),
        }
        store.set_many(values)
        msg = 'Inställningarna är sparade.'
        if values['web_port'] != s['web_port']:
            msg += f' Den nya webbporten {values["web_port"]} gäller efter omstart av webbgränssnittet.'
        wlog.info('Systeminställningar sparade')
        flash(msg, 'ok')
        return redirect(url_for('system'))
    st = engine_status()
    du = psutil.disk_usage(HITS_DIR)
    return render_template('system.html', s=g.s, st=st, started=fmt_ts(st.get('started')), disk=du, dd=data_info(st),
                           port=app.config.get('PORT'))


@app.route('/api/logg')
@login_required
def api_log():
    name = 'web.log' if request.args.get('fil') == 'webb' else 'lpr.log'
    path = os.path.join(LOG_DIR, name)
    lines = []
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 200_000))
            lines = fh.read().decode(errors='replace').splitlines()[-200:]
    return jsonify({'lines': lines})


@app.route('/system/starta-om', methods=['POST'])
@login_required
def restart():
    what = request.form.get('vad')
    if what == 'webb':
        wlog.warning('Omstart av webbgränssnittet begärd')
        threading.Timer(1.0, lambda: os._exit(0)).start()
        flash('Webbgränssnittet startar om. Ladda om sidan om några sekunder.', 'ok')
    else:
        store.set('restart_request', time.time(), bump=False)
        wlog.warning('Omstart av analysmotorn begärd')
        flash('Analysmotorn startar om. Kamerorna är tillbaka inom ungefär en halv minut.', 'ok')
    return redirect(url_for('system'))


@app.errorhandler(400)
def bad_request(e):
    return render_template('error.html', code=400, msg=e.description), 400


@app.errorhandler(404)
def not_found(e):
    return render_template('error.html', code=404, msg='Sidan finns inte.'), 404


@app.errorhandler(413)
def too_large(e):
    return render_template('error.html', code=413, msg='Filen är för stor (högst 2 MB).'), 413


def serve():
    setup_web_logging()
    port = store.get('web_port')
    app.config['PORT'] = port
    psutil.cpu_percent(interval=None)
    wlog.info('Webbgränssnitt %s startar på port %s', VERSION, port)
    from waitress import serve as wserve
    wserve(app, host='0.0.0.0', port=port, threads=8, ident=None)
