"""Analysmotor: N kameratrådar delar en detektor (YOLO, GPU) och en OCR.
Detektorn är utbytbar (detectors.py: ultralytics | hailo), övrigt är gemensamt.
Kedja per kamera: RTSP -> detektion -> OCR -> röstning per spår -> svenskt format -> dedup -> SQLite/bild -> OpenEye."""
import collections
import datetime
import itertools
import json
import logging
import logging.handlers
import os
import queue
import re
import threading
import time

import numpy as np

from . import DATA_DIR, VERSION
from . import detectors
from .camera import cv2, describe, make_reader
from .housekeeping import Housekeeper
from .openeye import EventClient
from .store import HITS_DIR, LOG_DIR, STATUS_PATH, Store

log = logging.getLogger('lpr')

# Svenska skyltar: ABC 123 / ABC 12A. I, Q, V, Å, Ä, Ö används inte; sista bokstaven aldrig O.
SE_RE = re.compile(r'^[A-HJ-PR-UW-Z]{3}[0-9]{2}[0-9A-HJ-NPR-UW-Z]$')
TO_LETTER = {'0': 'O', '1': 'I', '2': 'Z', '4': 'A', '5': 'S', '6': 'G', '8': 'B'}
TO_DIGIT = {'O': '0', 'Q': '0', 'D': '0', 'I': '1', 'L': '1', 'Z': '2', 'S': '5', 'G': '6', 'B': '8', 'T': '7'}
TRACK_IDS = itertools.count(1)


def normalize_se(raw):
    s = re.sub(r'[^A-Z0-9]', '', raw.upper())
    if len(s) != 6:
        return None
    s = ''.join([TO_LETTER.get(c, c) for c in s[:3]] + [TO_DIGIT.get(c, c) for c in s[3:5]] + [s[5]])
    return s if SE_RE.match(s) else None


def pretty(p):
    return f'{p[:3]} {p[3:]}'


def setup_logging(level='INFO', filename='lpr.log'):
    fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s')
    log.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(os.path.join(LOG_DIR, filename), maxBytes=10_000_000, backupCount=5)
    fh.setFormatter(fmt)
    log.addHandler(fh)


# ---------------------------------------------------------------- delade modeller
class OCR:
    def __init__(self, model):
        from fast_plate_ocr import LicensePlateRecognizer
        self.r = LicensePlateRecognizer(model, device='cpu')
        self.gray = self.r.config.image_color_mode == 'grayscale'
        self.lock = threading.Lock()

    def read(self, crop_bgr):
        im = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY if self.gray else cv2.COLOR_BGR2RGB)
        with self.lock:
            p = self.r.run(im, return_confidence=True)[0]
        probs = np.asarray(p.char_probs, dtype=float).ravel() if p.char_probs is not None else np.array([0.0])
        probs = probs[:len(p.plate)] if len(p.plate) else probs
        return p.plate, float(probs.mean()) if probs.size else 0.0, float(probs.min()) if probs.size else 0.0


# ---------------------------------------------------------------- spårning
class Track:
    def __init__(self, box, ts):
        self.id = next(TRACK_IDS)
        self.box, self.first, self.last = box, ts, ts
        self.frames = 0
        self.reads = []      # (plate, mean_conf, min_conf, crop, ctx, ts, width)
        self.raw = []
        self.emitted = None
        self.reject_crop = None

    def votes(self):
        return collections.Counter(r[0] for r in self.reads)


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-9)


def match(tracks, box, ts, timeout):
    best, bscore = None, 0.0
    cx, cy, w = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, box[2] - box[0]
    for t in tracks:
        if ts - t.last > timeout:
            continue
        s = iou(t.box, box)
        tx, ty = (t.box[0] + t.box[2]) / 2, (t.box[1] + t.box[3]) / 2
        d = ((tx - cx) ** 2 + (ty - cy) ** 2) ** 0.5
        if s < 0.1 and d < 2.0 * max(w, t.box[2] - t.box[0]):
            s = 0.1 - d / 1e5      # svag matchning på avstånd (snabb bil, låg fps)
        if s > bscore:
            best, bscore = t, s
    return best


# ---------------------------------------------------------------- OpenEye-sändare
class RateLimiter:
    def __init__(self, per_min, per_hour, per_day):
        self.limits = [(60, per_min), (3600, per_hour), (86400, per_day)]
        self.sent = collections.deque()

    def allow(self):
        now = time.time()
        while self.sent and now - self.sent[0] > 86400:
            self.sent.popleft()
        for window, limit in self.limits:
            if sum(1 for t in self.sent if now - t <= window) + 1 > limit:
                return False
        self.sent.append(now)
        return True


class Sender(threading.Thread):
    def __init__(self, engine):
        super().__init__(daemon=True, name='sender')
        self.e = engine
        self.q = queue.Queue()
        self.latencies = collections.deque(maxlen=200)
        self.rl, self.rl_key = None, None
        self.last_status = None
        self.last_ok = None

    def run(self):
        while True:
            it = self.q.get()
            s = self.e.settings
            key = (s['rl_min'], s['rl_hour'], s['rl_day'])
            if key != self.rl_key:
                self.rl, self.rl_key = RateLimiter(*key), key
            if not (s['oe_enabled'] and s['oe_aki'] and s['oe_aks'] and s['oe_url']):
                self.e.store.update_reading(it['rid'], status='logged')
                continue
            if not it['ext_id']:
                self.e.store.update_reading(it['rid'], status='no_camera_id')
                log.warning('OWS: kameran "%s" saknar OpenEye kamera-id, skylt %s skickas inte', it['camera'], it['plate'])
                continue
            if not self.rl.allow():
                log.warning('OWS: hastighetsgräns nådd, skylt %s skickas inte', it['plate'])
                self.e.store.update_reading(it['rid'], status='rate_limited')
                continue
            client = EventClient(s['oe_url'], s['oe_aki'], s['oe_aks'])
            st, txt = client.post_plate(it['ext_id'], it['plate'], it['when'])
            now = time.time()
            lat = int((now - it['frame_ts']) * 1000)
            self.latencies.append(lat)
            self.last_status = st
            if st == 202:
                self.last_ok = now
            self.e.store.update_reading(it['rid'], ows_status=st, status='sent' if st == 202 else 'send_failed',
                                        ows_detail='' if st == 202 else txt[:200], frame_to_post_ms=lat)
            log.log(logging.INFO if st == 202 else logging.ERROR, 'OWS kamera="%s" skylt=%s -> %s%s (%d ms)',
                    it['camera'], it['plate'], st, '' if st == 202 else ' ' + txt[:200], lat)


# ---------------------------------------------------------------- en kamera
class CameraWorker(threading.Thread):
    def __init__(self, engine, cam):
        super().__init__(daemon=True, name=f"cam-{cam['id']}")
        self.e, self.cam = engine, cam
        self.stop_ev = threading.Event()
        self.reader = make_reader(cam, engine.settings.get('decoder', 'opencv'))
        self.tracks = []
        self.last_emit = {}
        self.stat = collections.Counter()
        self.proc_frames = 0
        self.fps_in = self.fps_proc = 0.0
        self.last_rate_t = time.time()
        self.last_plate, self.last_plate_ts = None, None
        self.started = time.time()

    def stop(self):
        self.stop_ev.set()
        self.reader.stop()

    def cfg(self, key):
        c, s = self.cam, self.e.settings
        if key == 'min_plate_w':
            return c.get('min_plate_w') or s['default_min_plate_w']
        if key == 'target_fps':
            return c.get('target_fps') or s['default_fps']
        return s[key]

    def run(self):
        self.reader.start()
        log.info('KAMERA "%s" startad (%s, min bredd %dpx, %s fps, ROI %s)', self.cam['name'], describe(self.cam),
                 self.cfg('min_plate_w'), self.cfg('target_fps'), self.cam.get('roi') or 'hela bilden')
        last_seq = 0
        while not self.stop_ev.is_set():
            t0 = time.time()
            frame, ts, seq = self.reader.latest()
            if frame is not None and seq != last_seq:
                last_seq = seq
                try:
                    self.process(frame, ts)
                    self.proc_frames += 1
                except Exception:
                    log.exception('Fel i bildbehandling, kamera "%s"', self.cam['name'])
            elif self.tracks:
                self.close_stale(time.time())
            self.update_rates()
            dt = 1.0 / max(0.5, float(self.cfg('target_fps'))) - (time.time() - t0)
            if dt > 0:
                self.stop_ev.wait(dt)
        log.info('KAMERA "%s" stoppad', self.cam['name'])

    def update_rates(self):
        now = time.time()
        p = now - self.last_rate_t
        if p >= 5:
            self.fps_in, self.fps_proc = self.reader.frames_in / p, self.proc_frames / p
            self.reader.frames_in = self.proc_frames = 0
            self.last_rate_t = now

    def process(self, frame, ts):
        H, W = frame.shape[:2]
        min_w = self.cfg('min_plate_w')
        for box, dconf in self.e.detector.detect(frame, self.cam.get('roi')):
            x1, y1, x2, y2 = map(int, box)
            w = x2 - x1
            self.stat['det'] += 1
            t = match(self.tracks, box, ts, self.cfg('track_timeout_s'))
            if t is None:
                t = Track(box, ts)
                self.tracks.append(t)
                log.debug('SPÅR %d ny skylt w=%dpx (%d,%d) det=%.2f kamera="%s"', t.id, w, x1, y1, dconf, self.cam['name'])
            t.box, t.last = box, ts
            t.frames += 1
            if w < min_w or t.emitted:
                self.stat['small' if w < min_w else 'after_emit'] += 1
                continue
            px, py = int(w * .06), int((y2 - y1) * .12)
            crop = frame[max(0, y1 - py):min(H, y2 + py), max(0, x1 - px):min(W, x2 + px)]
            if crop.size == 0:
                continue
            raw, mconf, minc = self.e.ocr.read(crop)
            self.stat['ocr'] += 1
            plate = normalize_se(raw)
            t.raw.append(f'{raw}:{mconf:.2f}')
            cw, ch = w * 2, (y2 - y1) * 4
            ctx = frame[max(0, y1 - ch):min(H, y2 + ch), max(0, x1 - cw):min(W, x2 + cw)]
            if plate and minc >= self.cfg('min_char_conf'):
                t.reads.append((plate, mconf, minc, crop.copy(), ctx.copy(), ts, w))
                self.stat['valid_read'] += 1
            else:
                self.stat['invalid_read'] += 1
                t.reject_crop = (crop.copy(), raw)
            v = t.votes()
            if v and v.most_common(1)[0][1] >= self.cfg('early_votes'):
                self.finalize(t, 'tidig')
        self.close_stale(ts)

    def close_stale(self, now):
        for t in list(self.tracks):
            if now - t.last > self.cfg('track_timeout_s'):
                if not t.emitted:
                    self.finalize(t, 'slut')
                self.tracks.remove(t)

    def finalize(self, t, reason):
        v = t.votes().most_common(2)
        if not v:
            if t.frames >= 2:
                log.debug('SPÅR %d stängt: ingen giltig läsning (%d rutor, rå=%s)', t.id, t.frames, ','.join(t.raw[-6:]) or '-')
                self.save_reject(t)
            t.emitted = 'none'
            return
        top, n = v[0]
        second = v[1][1] if len(v) > 1 else 0
        if n < self.cfg('min_votes') or n <= second:
            log.info('SPÅR %d stängt: ingen konsensus %s kamera="%s"', t.id, dict(t.votes()), self.cam['name'])
            self.save_reject(t)
            t.emitted = 'none'
            return
        t.emitted = top
        now = time.time()
        if now - self.last_emit.get(top, 0) < self.cfg('dedup_s'):
            log.info('SPÅR %d skylt=%s DUBBLETT (sågs för %.0f s sedan) kamera="%s"', t.id, pretty(top),
                     now - self.last_emit[top], self.cam['name'])
            self.stat['dedup'] += 1
            return
        self.last_emit = {k: v for k, v in self.last_emit.items() if now - v < 3600}
        self.last_emit[top] = now
        reads = [r for r in t.reads if r[0] == top]
        best = max(reads, key=lambda r: (r[1], r[6]))
        conf = float(np.mean([r[1] for r in reads]))
        dt = datetime.datetime.fromtimestamp(best[5])
        day = dt.strftime('%Y-%m-%d')
        os.makedirs(os.path.join(HITS_DIR, day), exist_ok=True)
        rel = f"{day}/{dt.strftime('%H%M%S')}_c{self.cam['id']}_{top}_t{t.id}"
        cv2.imwrite(os.path.join(HITS_DIR, rel + '.jpg'), best[3])
        cv2.imwrite(os.path.join(HITS_DIR, rel + '_ctx.jpg'), best[4], [cv2.IMWRITE_JPEG_QUALITY, 85])
        self.e.hk.add_file(os.path.join(HITS_DIR, rel + '.jpg'))
        self.e.hk.add_file(os.path.join(HITS_DIR, rel + '_ctx.jpg'))
        rid = self.e.store.insert_reading(
            ts_utc=datetime.datetime.fromtimestamp(t.first, datetime.timezone.utc).isoformat(timespec='milliseconds'),
            ts_local=datetime.datetime.fromtimestamp(t.first).isoformat(timespec='seconds'),
            camera_id=self.cam['id'], camera=self.cam['name'], plate=pretty(top), confidence=round(conf, 3), votes=n,
            reads=len(t.reads), frames=t.frames, plate_w=int(best[6]), crop=rel + '.jpg', context=rel + '_ctx.jpg',
            track_id=t.id, status='queued', raw_reads=','.join(t.raw[-12:]))
        self.stat['plates'] += 1
        self.last_plate, self.last_plate_ts = pretty(top), t.first
        log.info('PLATE %s kamera="%s" säkerhet=%.2f röster=%d/%d rutor=%d w=%dpx spår=%d (%s)', pretty(top),
                 self.cam['name'], conf, n, len(t.reads), t.frames, best[6], t.id, reason)
        self.e.sender.q.put({'rid': rid, 'plate': pretty(top), 'when': t.first, 'frame_ts': reads[-1][5],
                             'ext_id': (self.cam.get('external_id') or '').strip(), 'camera': self.cam['name']})

    def save_reject(self, t):
        if not (self.e.settings['save_rejects'] and t.reject_crop is not None):
            return
        d = os.path.join(HITS_DIR, 'rejects', datetime.date.today().isoformat())
        os.makedirs(d, exist_ok=True)
        raw = re.sub(r'[^A-Za-z0-9]', '', t.reject_crop[1]) or 'x'
        path = os.path.join(d, f"{datetime.datetime.now().strftime('%H%M%S')}_c{self.cam['id']}_t{t.id}_{raw}.jpg")
        cv2.imwrite(path, t.reject_crop[0])
        self.e.hk.add_file(path)

    def status(self):
        r = self.reader
        return {'id': self.cam['id'], 'name': self.cam['name'], 'connected': r.connected, 'fps_in': round(self.fps_in, 1),
                'fps_proc': round(self.fps_proc, 1), 'reconnects': r.reconnects, 'error': r.last_error,
                'resolution': r.resolution, 'last_plate': self.last_plate, 'last_plate_ts': self.last_plate_ts,
                'connected_since': r.connected_since, 'tracks': len(self.tracks), 'counts': dict(self.stat)}


def accel_info(det):
    """Temperatur m.m. för AI-kretsen (Hailo) och SoC, visas i webben när ingen NVIDIA-GPU finns."""
    out = {}
    if getattr(det, 'name', '') == 'hailo':
        out['name'] = det.device_name
        out['temp'] = det.chip_temp()
    try:
        with open('/sys/class/thermal/thermal_zone0/temp') as f:
            out['cpu_temp'] = round(int(f.read()) / 1000, 1)
    except (OSError, ValueError):
        pass
    return out or None


# ---------------------------------------------------------------- motorn
CAM_SIG_KEYS = ('name', 'enabled', 'source_mode', 'host', 'port', 'path', 'username', 'password', 'rtsp_url',
                'external_id', 'min_plate_w', 'target_fps', 'roi')


class Engine:
    def __init__(self):
        self.store = Store()
        self.settings = self.store.all_settings()
        setup_logging(self.settings['log_level'])
        self.started = time.time()
        self.rev = self.settings['config_rev']
        s = self.settings
        log.info('Prudencia LPR %s startar (detektor %s via %s, OCR %s, avkodning %s)', VERSION,
                 os.path.basename(s['detector']), detectors.backend_for(s), s['ocr_model'], s['decoder'])
        self.detector = detectors.create(s)
        self.ocr = OCR(s['ocr_model'])
        self.sender = Sender(self)
        self.sender.start()
        self.hk = Housekeeper(self.store)
        self.workers = {}
        self.sigs = {}
        # läsningar som låg i kö när tjänsten stoppades markeras så de kan skickas om
        n = self.store.execute("UPDATE readings SET status='send_failed', ows_detail='avbruten vid omstart' "
                               "WHERE status='queued'")
        if n:
            log.warning('%d läsningar låg kvar i kö från förra körningen, markerade för omsändning', n)

    def sync_cameras(self):
        want = {c['id']: c for c in self.store.cameras(only_enabled=True)}
        for cid in list(self.workers):
            if cid not in want or self.sigs.get(cid) != tuple(json.dumps(want[cid].get(k)) for k in CAM_SIG_KEYS):
                self.workers.pop(cid).stop()
                self.sigs.pop(cid, None)
        for cid, cam in want.items():
            if cid not in self.workers:
                w = CameraWorker(self, cam)
                self.workers[cid] = w
                self.sigs[cid] = tuple(json.dumps(cam.get(k)) for k in CAM_SIG_KEYS)
                w.start()

    def reload(self):
        old = self.settings
        self.settings = self.store.all_settings()
        if self.settings['log_level'] != old['log_level']:
            log.setLevel(getattr(logging, self.settings['log_level'], logging.INFO))
        self.detector.conf = self.settings['det_conf']
        if any(self.settings[k] != old[k] for k in ('det_imgsz', 'detector', 'ocr_model', 'det_backend', 'hailo_tile',
                                                     'hailo_overlap', 'decoder')):
            log.warning('Modellinställningar ändrade, startar om motorn')
            self.exit_for_restart()
        self.sync_cameras()
        if (self.settings['max_data_gb'], self.settings['retention_days']) != (old['max_data_gb'], old['retention_days']):
            log.info('Lagringsregler ändrade (maxstorlek %s, lagringstid %s dagar), städar direkt',
                     f"{self.settings['max_data_gb']:g} GB" if self.settings['max_data_gb'] else 'ingen gräns',
                     self.settings['retention_days'])
            self.housekeeping(force=True)
        log.info('Konfiguration inläst (revision %s, %d aktiva kameror)', self.settings['config_rev'], len(self.workers))

    def exit_for_restart(self):
        for w in self.workers.values():
            w.stop()
        logging.shutdown()
        os._exit(0)     # systemd Restart=always startar oss igen

    def write_status(self):
        st = {'version': VERSION, 'pid': os.getpid(), 'started': self.started, 'updated': time.time(),
              'gpu_mib': round(self.detector.gpu_mib()), 'detector': self.detector.info(), 'accel': accel_info(self.detector),
              'det_ms': round(1000 * float(np.median(self.detector.times)), 1) if self.detector.times else None,
              'queue': self.sender.q.qsize(), 'data': self.hk.info(self.settings['max_data_gb']), 'ows_last_status': self.sender.last_status, 'ows_last_ok': self.sender.last_ok,
              'ows_lat_med': int(np.median(self.sender.latencies)) if self.sender.latencies else None,
              'cameras': {str(cid): w.status() for cid, w in self.workers.items()}}
        tmp = STATUS_PATH + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(st, f)
        os.replace(tmp, STATUS_PATH)

    def housekeeping(self, force=False):
        try:
            self.hk.run(self.settings, force=force)
        except Exception:
            log.exception('Fel vid städning av driftdata')

    def stats_line(self):
        for w in self.workers.values():
            s = w.status()
            log.info('STATUS kamera="%s" rtsp=%s fps=%.1f/%.1f återanslutningar=%d räknare=%s', s['name'],
                     'uppe' if s['connected'] else 'NERE', s['fps_proc'], s['fps_in'], s['reconnects'], s['counts'])
        log.info('STATUS motor gpu=%dMiB det=%sms kö=%d', self.detector.gpu_mib(),
                 f"{1000 * float(np.median(self.detector.times)):.0f}" if self.detector.times else '-', self.sender.q.qsize())

    def run(self):
        self.housekeeping(force=True)
        self.sync_cameras()
        last_stats = time.time()
        while True:
            try:
                rev = self.store.get('config_rev')
                if rev != self.rev:
                    self.rev = rev
                    self.reload()
                if self.store.get('restart_request') > self.started:
                    log.warning('Omstart begärd från webbgränssnittet')
                    self.exit_for_restart()
                self.write_status()
                self.housekeeping()
                if time.time() - last_stats >= 300:
                    self.stats_line()
                    last_stats = time.time()
            except Exception:
                log.exception('Fel i motorns huvudloop')
            time.sleep(2)


def prune(store):
    """Kör städningen direkt (app.py stada)."""
    return Housekeeper(store).run(store.all_settings(), force=True)
