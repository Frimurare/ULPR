"""RTSP-hjälp: bygga URL, maskera inloggning, läsa ström, ta testbild."""
import logging
import os
import re
import threading
import time
import urllib.parse

os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp')
import cv2  # noqa: E402

log = logging.getLogger('lpr')


def rtsp_url(cam):
    """Fullständig RTSP-URL inkl. inloggning (används bara internt, loggas aldrig)."""
    if cam.get('source_mode') == 'url' and cam.get('rtsp_url'):
        url = cam['rtsp_url'].strip()
        u, p = cam.get('username') or '', cam.get('password') or ''
        if u and '@' not in url.split('//', 1)[-1].split('/', 1)[0]:
            scheme, rest = url.split('//', 1)
            url = f'{scheme}//{urllib.parse.quote(u, safe="")}:{urllib.parse.quote(p, safe="")}@{rest}'
        return url
    path = cam.get('path') or '/'
    if not path.startswith('/'):
        path = '/' + path
    auth = ''
    if cam.get('username'):
        auth = f"{urllib.parse.quote(cam['username'], safe='')}:{urllib.parse.quote(cam.get('password') or '', safe='')}@"
    return f"rtsp://{auth}{cam.get('host', '').strip()}:{int(cam.get('port') or 554)}{path}"


def redact(url):
    return re.sub(r'//[^@/]+@', '//***@', url or '')


def describe(cam):
    """Kort, säker beskrivning av källan för loggar och UI."""
    return redact(rtsp_url(cam)).split('?', 1)[0]


def open_capture(url, timeout_ms=10000):
    return cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms])


def grab_frame(cam, timeout_ms=8000):
    """Öppnar strömmen, läser några rutor och returnerar (frame, felmeddelande)."""
    cap = open_capture(rtsp_url(cam), timeout_ms)
    try:
        if not cap.isOpened():
            return None, 'Kunde inte öppna strömmen (kontrollera adress, port, sökväg och inloggning).'
        frame = None
        for _ in range(5):
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
                break
        if frame is None:
            return None, 'Strömmen öppnades men ingen bild kunde läsas.'
        return frame, None
    finally:
        cap.release()


class Reader(threading.Thread):
    """Läser strömmen kontinuerligt, behåller bara senaste rutan, återansluter med backoff."""

    def __init__(self, cam):
        super().__init__(daemon=True, name=f"reader-{cam['id']}")
        self.cam = cam
        self.url = rtsp_url(cam)
        self.label = cam['name']
        self.stop_ev = threading.Event()
        self.lock = threading.Lock()
        self.frame, self.ts, self.seq = None, 0.0, 0
        self.connected = False
        self.reconnects = 0
        self.frames_in = 0
        self.resolution = None
        self.last_error = ''
        self.connected_since = None

    def stop(self):
        self.stop_ev.set()

    def run(self):
        backoff = 2
        while not self.stop_ev.is_set():
            cap = open_capture(self.url)
            if not cap.isOpened():
                self.last_error = 'Kan inte ansluta till strömmen'
                log.warning('RTSP: kan inte öppna "%s" (%s), nytt försök om %ss', self.label, describe(self.cam), backoff)
                cap.release()
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            if self.stop_ev.is_set():
                cap.release()
                break
            self.resolution = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
            log.info('RTSP: ansluten till "%s" (%dx%d)', self.label, *self.resolution)
            self.connected, self.last_error, backoff = True, '', 2
            self.connected_since = time.time()
            while not self.stop_ev.is_set():
                ok, f = cap.read()
                if not ok or f is None:
                    break
                with self.lock:
                    self.frame, self.ts = f, time.time()
                    self.seq += 1
                self.frames_in += 1
            cap.release()
            self.connected = False
            self.connected_since = None
            if self.stop_ev.is_set():
                break
            self.reconnects += 1
            self.last_error = 'Strömmen tappades'
            log.warning('RTSP: strömmen tappades för "%s", återansluter', self.label)
            self.stop_ev.wait(2)

    def latest(self):
        with self.lock:
            return self.frame, self.ts, self.seq


class FFmpegReader(Reader):
    """OBS: RTSP-adressen (med inloggning) hamnar på ffmpegs kommandorad och syns i processlistan för
    andra användare på maskinen. Använd hellre decoder = pyav.
    Som Reader men avkodar i en ffmpeg-process med hårdvaruacceleration (-hwaccel drm = V4L2 stateless,
    t.ex. HEVC på Raspberry Pi 5). Rutorna kommer som rå YUV 4:2:0 via en pipe. Codecs utan hårdvarustöd
    avkodas automatiskt i mjukvara av ffmpeg."""

    HWACCEL = os.environ.get('LPR_FFMPEG_HWACCEL', 'drm')

    def __init__(self, cam):
        super().__init__(cam)
        self.proc = None

    def stop(self):
        self.stop_ev.set()
        self._kill()

    def _kill(self):
        p = self.proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def _probe(self):
        import json
        import subprocess
        r = subprocess.run(['ffprobe', '-v', 'error', '-rtsp_transport', 'tcp', '-timeout', '10000000',
                            '-select_streams', 'v:0', '-show_entries', 'stream=width,height,codec_name', '-of', 'json',
                            self.url], capture_output=True, timeout=20, text=True)
        st = json.loads(r.stdout or '{}').get('streams') or []
        if not st:
            raise RuntimeError((r.stderr or 'ingen videoström').strip().splitlines()[-1][:200])
        return int(st[0]['width']), int(st[0]['height']), st[0].get('codec_name', '?')

    def run(self):
        import subprocess
        import numpy as np
        backoff = 2
        while not self.stop_ev.is_set():
            try:
                w, h, codec = self._probe()
            except Exception as e:
                self.last_error = 'Kan inte ansluta till strömmen'
                log.warning('RTSP: kan inte öppna "%s" (%s): %s, nytt försök om %ss', self.label, describe(self.cam),
                            redact(str(e)), backoff)
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-rtsp_transport', 'tcp',
                   '-timeout', '10000000', '-hwaccel', self.HWACCEL, '-i', self.url, '-an', '-sn',
                   '-fps_mode', 'passthrough', '-f', 'rawvideo', '-pix_fmt', 'yuv420p', 'pipe:1']
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            # yuv420p genom pipen (halva datamängden) och färgkonvertering i OpenCV (NEON, flertrådad):
            # ~40 % av en kärna billigare än att låta ffmpeg/swscale leverera BGR på en Pi 5.
            size = w * h * 3 // 2
            self.resolution = (w, h)
            first = True
            while not self.stop_ev.is_set():
                buf = np.empty((h * 3 // 2, w), np.uint8)
                mv, got = memoryview(buf).cast('B'), 0
                while got < size:
                    n = self.proc.stdout.readinto(mv[got:])
                    if not n:
                        break
                    got += n
                if got < size:
                    break
                if first:
                    log.info('RTSP: ansluten till "%s" (%dx%d, %s, ffmpeg -hwaccel %s)', self.label, w, h, codec, self.HWACCEL)
                    self.connected, self.last_error, backoff, first = True, '', 2, False
                    self.connected_since = time.time()
                ts = time.time()
                bgr = cv2.cvtColor(buf, cv2.COLOR_YUV2BGR_I420)
                with self.lock:
                    self.frame, self.ts = bgr, ts
                    self.seq += 1
                self.frames_in += 1
            self._kill()
            try:
                err = self.proc.stderr.read().decode(errors='replace').strip()[-300:]
                self.proc.wait(timeout=5)
            except Exception:
                err = ''
            self.connected = False
            self.connected_since = None
            if self.stop_ev.is_set():
                break
            self.reconnects += 1
            self.last_error = 'Strömmen tappades'
            log.warning('RTSP: strömmen tappades för "%s", återansluter%s', self.label, (' (' + redact(err) + ')') if err else '')
            self.stop_ev.wait(2)


class PyAVReader(Reader):
    """Avkodar i processen med PyAV mot systemets libavcodec och hårdvaruacceleration (drm = V4L2 stateless,
    HEVC på Raspberry Pi 5). Ingen extern process, så RTSP-adressen med inloggning syns aldrig i processlistan.
    Rutorna hämtas som YUV 4:2:0 och färgkonverteras i OpenCV (billigare än swscale till BGR)."""

    HWACCEL = os.environ.get('LPR_HWACCEL', 'drm')

    def __init__(self, cam):
        super().__init__(cam)
        self.container = None

    def stop(self):
        self.stop_ev.set()
        c = self.container
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def _open(self):
        import av
        kw = {}
        if self.HWACCEL and self.HWACCEL != 'none':
            from av.codec.hwaccel import HWAccel
            kw['hwaccel'] = HWAccel(device_type=self.HWACCEL, allow_software_fallback=True)
        return av.open(self.url, options={'rtsp_transport': 'tcp', 'timeout': '10000000'}, timeout=(10, 10), **kw)

    def run(self):
        backoff = 2
        while not self.stop_ev.is_set():
            try:
                self.container = self._open()
                vs = self.container.streams.video[0]
                vs.thread_type = 'AUTO'
            except Exception as e:
                self.container = None
                self.last_error = 'Kan inte ansluta till strömmen'
                log.warning('RTSP: kan inte öppna "%s" (%s): %s, nytt försök om %ss', self.label, describe(self.cam),
                            redact(str(e))[:200], backoff)
                self.stop_ev.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            first = True
            err = ''
            try:
                for f in self.container.decode(vs):
                    if self.stop_ev.is_set():
                        break
                    ts = time.time()
                    yuv = f.to_ndarray(format='yuv420p') if f.format.name != 'yuv420p' else f.to_ndarray()
                    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
                    if first:
                        h, w = bgr.shape[:2]
                        self.resolution = (w, h)
                        log.info('RTSP: ansluten till "%s" (%dx%d, %s, PyAV hwaccel %s)', self.label, w, h,
                                 vs.codec_context.name, self.HWACCEL)
                        self.connected, self.last_error, backoff, first = True, '', 2, False
                        self.connected_since = time.time()
                    with self.lock:
                        self.frame, self.ts = bgr, ts
                        self.seq += 1
                    self.frames_in += 1
            except Exception as e:
                err = redact(str(e))[:200]
            finally:
                try:
                    self.container.close()
                except Exception:
                    pass
                self.container = None
            self.connected = False
            self.connected_since = None
            if self.stop_ev.is_set():
                break
            self.reconnects += 1
            self.last_error = 'Strömmen tappades'
            log.warning('RTSP: strömmen tappades för "%s", återansluter%s', self.label, f' ({err})' if err else '')
            self.stop_ev.wait(2)


def make_reader(cam, decoder='opencv'):
    d = str(decoder).lower()
    if d == 'pyav':
        return PyAVReader(cam)
    if d.startswith('ffmpeg'):
        return FFmpegReader(cam)
    return Reader(cam)
