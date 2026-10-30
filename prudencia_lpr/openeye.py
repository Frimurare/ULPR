"""OpenEye: Event Receiver API (Analytics), kamera-id och hjälp mot lokal recorder."""
import base64
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

EVENT_PATH = '/api/monitoring/integration/event'


def build_camera_id(recorder_id, camera_uuid):
    raw = f'{recorder_id.strip()}|{camera_uuid.strip()}'.encode()
    return '_OE_' + base64.b64encode(raw).decode()


def decode_camera_id(ext_id):
    """'_OE_<b64>' -> (recorderId, cameraUuid) eller None."""
    if not ext_id or not ext_id.startswith('_OE_'):
        return None
    s = ext_id[4:]
    try:
        txt = base64.b64decode(s + '=' * (-len(s) % 4)).decode()
    except Exception:
        return None
    if '|' not in txt:
        return None
    a, b = txt.split('|', 1)
    return a, b


class EventClient:
    def __init__(self, url, aki, aks):
        self.url = url.rstrip('/') + EVENT_PATH
        self.auth = f'!EXT-INTEGRATOR AKI={aki},AKS={aks}'
        self.seq = int(time.time()) % 100000

    def post_plate(self, camera_id, plate_text, when):
        """Skickar ANALYTIC_LICENSE_PLATE_DETECTED. Returnerar (http-status, svarstext). -1 = nätverksfel."""
        self.seq += 1
        ev = {'eventId': f'{int(time.time())}{self.seq % 100000:05d}',
              'eventDate': str(int(when * 1_000_000)),
              'eventType': 'ANALYTIC_LICENSE_PLATE_DETECTED',
              'attributes': {'sourceObject': {'type': 'CAMERA', 'id': camera_id}, 'linkedObjects': [],
                             'props': [{'name': 'licensePlate', 'value': plate_text}]}}
        body = {'requestDateMicros': str(int(time.time() * 1_000_000)), 'events': [ev]}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method='POST',
                                     headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                                              'Authorization': self.auth})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read(300).decode(errors='replace')
        except urllib.error.HTTPError as e:
            return e.code, e.read(300).decode(errors='replace')
        except Exception as e:  # nätverk, DNS, TLS
            return -1, str(e)[:300]


def recorder_cameras(host, user, password, timeout=10):
    """Hämtar kameralistan från en lokal OpenEye-recorder (GET https://<host>/api/cameras, basic auth).
    Recordern har normalt självsignerat certifikat, därför ingen certifikatkontroll här."""
    host = host.strip().rstrip('/')
    if not host.startswith('http'):
        host = 'https://' + host
    url = host + '/api/cameras'
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    tok = base64.b64encode(f'{user}:{password}'.encode()).decode()
    req = urllib.request.Request(url, headers={'Authorization': 'Basic ' + tok, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        data = json.loads(r.read().decode())
    items = data.get('content', data) if isinstance(data, dict) else data
    out = []
    for c in items or []:
        streams = []
        for s in c.get('namedCameraStreams') or []:
            res = s.get('resolution') or {}
            streams.append({'name': s.get('logicalName') or s.get('streamName'), 'url': s.get('streamUrl', ''),
                            'w': res.get('width'), 'h': res.get('height')})
        out.append({'uuid': c.get('id'), 'name': c.get('name'), 'ip': c.get('ipAddress'), 'model': c.get('model'),
                    'manufacturer': c.get('manufacturer'), 'state': c.get('state'), 'streams': streams})
    out.sort(key=lambda x: (x['name'] or '').lower())
    return out
