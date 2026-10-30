"""SQLite-lager: inställningar, kameror och läsningar (data/lpr.db, WAL, filrättigheter 600)."""
import datetime
import json
import os
import secrets
import sqlite3
import threading

from werkzeug.security import generate_password_hash

from . import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, 'lpr.db')
HITS_DIR = os.path.join(DATA_DIR, 'hits')
LOG_DIR = os.path.join(DATA_DIR, 'logs')
BRAND_DIR = os.path.join(DATA_DIR, 'branding')
STATUS_PATH = os.path.join(DATA_DIR, 'status.json')
MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models')

# Inställningar som sparas som text; typ bestäms av standardvärdet.
DEFAULTS = {
    # allmänt / webb
    'web_port': 2028,
    'admin_hash': '',
    'must_change_pw': 1,
    'secret_key': '',
    'company_name': '',
    'logo_file': '',
    'log_level': 'INFO',
    'retention_days': 30,
    'max_data_gb': 0.0,
    'save_rejects': 1,
    # analys
    'detector': os.path.join(MODELS_DIR, 'plates-v1m.pt'),
    'ocr_model': 'cct-xs-v2-global-model',
    'det_backend': '',          # '' = välj efter filändelse (.hef -> hailo), annars ultralytics | hailo
    'det_imgsz': 1280,
    'hailo_tile': 832,          # rutstorlek i källbildens pixlar (0 = nätverkets egen indatastorlek)
    'hailo_overlap': 220,       # överlapp mellan rutor, minst bredaste skylten i px
    'decoder': 'opencv',        # opencv | pyav (hårdvaruavkodning i processen, t.ex. HEVC på Raspberry Pi 5) | ffmpeg
    'det_conf': 0.4,
    'min_char_conf': 0.45,
    'early_votes': 3,
    'min_votes': 2,
    'track_timeout_s': 2.0,
    'dedup_s': 60,
    'default_min_plate_w': 70,
    'default_fps': 8,
    # OpenEye
    'oe_enabled': 0,
    'oe_aki': '',
    'oe_aks': '',
    'oe_url': '',
    'rl_min': 20,
    'rl_hour': 300,
    'rl_day': 3000,
    'oe_recorder_host': '',
    'oe_recorder_user': '',
    'oe_recorder_id': '',
    # intern drift
    'config_rev': 1,
    'restart_request': 0.0,
    'last_prune': '',
    'last_prune_info': '',
    'last_vacuum': '',
}
SECRET_KEYS = {'admin_hash', 'secret_key', 'oe_aks'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS cameras(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER DEFAULT 1,
  source_mode TEXT DEFAULT 'host', host TEXT DEFAULT '', port INTEGER DEFAULT 554, path TEXT DEFAULT '',
  username TEXT DEFAULT '', password TEXT DEFAULT '', rtsp_url TEXT DEFAULT '',
  external_id TEXT DEFAULT '', min_plate_w INTEGER, target_fps REAL, roi TEXT,
  created TEXT, updated TEXT);
CREATE TABLE IF NOT EXISTS readings(
  id INTEGER PRIMARY KEY, ts_utc TEXT, ts_local TEXT, camera_id INTEGER, camera TEXT, plate TEXT,
  confidence REAL, votes INTEGER, reads INTEGER, frames INTEGER, plate_w INTEGER,
  crop TEXT, context TEXT, track_id INTEGER, status TEXT, ows_status INTEGER, ows_detail TEXT,
  frame_to_post_ms INTEGER, raw_reads TEXT);
CREATE INDEX IF NOT EXISTS ix_readings_ts ON readings(ts_local);
CREATE INDEX IF NOT EXISTS ix_readings_tsutc ON readings(ts_utc);
CREATE INDEX IF NOT EXISTS ix_readings_plate ON readings(plate);
CREATE INDEX IF NOT EXISTS ix_readings_cam ON readings(camera_id, ts_local);
"""

CAMERA_FIELDS = ('name', 'enabled', 'source_mode', 'host', 'port', 'path', 'username', 'password', 'rtsp_url',
                 'external_id', 'min_plate_w', 'target_fps', 'roi')


def _now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _cast(key, raw):
    d = DEFAULTS.get(key)
    if raw is None:
        return d
    try:
        if isinstance(d, bool) or isinstance(d, int):
            return int(float(raw))
        if isinstance(d, float):
            return float(raw)
    except (TypeError, ValueError):
        return d
    return raw


class Store:
    def __init__(self, path=DB_PATH):
        for d in (DATA_DIR, HITS_DIR, LOG_DIR, BRAND_DIR):
            os.makedirs(d, mode=0o700, exist_ok=True)
        old = os.umask(0o077)
        try:
            self.db = sqlite3.connect(path, check_same_thread=False, timeout=15)
        finally:
            os.umask(old)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA busy_timeout=15000')
            self.db.executescript(SCHEMA)
            self.db.commit()
        self._init_defaults()

    def _init_defaults(self):
        if not self.get('secret_key'):
            self.set('secret_key', secrets.token_hex(32), bump=False)
        if not self.get('admin_hash'):
            self.set('admin_hash', generate_password_hash('Prudencia'), bump=False)
            self.set('must_change_pw', 1, bump=False)

    # ---------------------------------------------------------- inställningar
    def get(self, key):
        with self.lock:
            r = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return _cast(key, r['value'] if r else None)

    def all_settings(self):
        with self.lock:
            rows = self.db.execute('SELECT key, value FROM settings').fetchall()
        out = {k: v for k, v in DEFAULTS.items()}
        for r in rows:
            out[r['key']] = _cast(r['key'], r['value'])
        return out

    def set(self, key, value, bump=True):
        self.set_many({key: value}, bump=bump)

    def set_many(self, values, bump=True):
        with self.lock:
            for k, v in values.items():
                self.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                                (k, str(v)))
            if bump:
                self.db.execute("INSERT INTO settings(key,value) VALUES('config_rev','2') ON CONFLICT(key) DO UPDATE "
                                "SET value=CAST(value AS INTEGER)+1")
            self.db.commit()

    def bump(self):
        self.set_many({}, bump=True)

    # ---------------------------------------------------------- kameror
    def cameras(self, only_enabled=False):
        q = 'SELECT * FROM cameras' + (' WHERE enabled=1' if only_enabled else '') + ' ORDER BY name COLLATE NOCASE'
        with self.lock:
            rows = self.db.execute(q).fetchall()
        return [self._cam(r) for r in rows]

    def camera(self, cid):
        with self.lock:
            r = self.db.execute('SELECT * FROM cameras WHERE id=?', (cid,)).fetchone()
        return self._cam(r) if r else None

    @staticmethod
    def _cam(r):
        c = dict(r)
        try:
            c['roi'] = json.loads(c['roi']) if c.get('roi') else None
        except ValueError:
            c['roi'] = None
        return c

    def save_camera(self, data, cid=None):
        data = {k: data[k] for k in CAMERA_FIELDS if k in data}
        if 'roi' in data:
            data['roi'] = json.dumps(data['roi']) if data['roi'] else None
        with self.lock:
            if cid:
                data['updated'] = _now()
                self.db.execute(f"UPDATE cameras SET {','.join(k + '=?' for k in data)} WHERE id=?",
                                list(data.values()) + [cid])
            else:
                data['created'] = data['updated'] = _now()
                cur = self.db.execute(f"INSERT INTO cameras({','.join(data)}) VALUES({','.join('?' * len(data))})",
                                      list(data.values()))
                cid = cur.lastrowid
            self.db.commit()
        self.bump()
        return cid

    def delete_camera(self, cid):
        with self.lock:
            self.db.execute('DELETE FROM cameras WHERE id=?', (cid,))
            self.db.commit()
        self.bump()

    # ---------------------------------------------------------- läsningar
    def insert_reading(self, **kw):
        with self.lock:
            cur = self.db.execute(f"INSERT INTO readings({','.join(kw)}) VALUES({','.join('?' * len(kw))})",
                                  list(kw.values()))
            self.db.commit()
            return cur.lastrowid

    def update_reading(self, rid, **kw):
        with self.lock:
            self.db.execute(f"UPDATE readings SET {','.join(k + '=?' for k in kw)} WHERE id=?", list(kw.values()) + [rid])
            self.db.commit()

    def query(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount
