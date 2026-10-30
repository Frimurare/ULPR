"""Städning av driftdata: tidsregel (lagringstid i dagar) och storleksregel (maxstorlek, äldst raderas först).

Driftdata = använda sidor i data/lpr.db + alla filer under data/hits/ (skylt-, översikts- och avvisade bilder).
Storleken hålls i en cache: bildkatalogen räknas fullt vid start och en gång per timme, däremellan
justeras cachen när motorn skriver eller raderar bilder. Databasens del räknas med PRAGMA (billigt)."""
import datetime
import logging
import os
import re
import threading
import time

from .store import HITS_DIR

log = logging.getLogger('lpr')
DAY_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
GB = 1_000_000_000
INTERVAL_S = 600           # tids- och storleksregel minst var 10:e minut
RESCAN_S = 3600            # full omräkning av bildkatalogen


def _dir_bytes(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _remove(path):
    try:
        size = os.path.getsize(path)
        os.remove(path)
        return size
    except OSError:
        return 0


class Housekeeper:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.hits_bytes = _dir_bytes(HITS_DIR)
        self.last_scan = time.time()
        self.last_run = 0.0
        self.freed_since_vacuum = 0
        self.wanted = False

    # ---------------------------------------------------------- storlek
    def add_file(self, path):
        """Anropas efter att motorn skrivit en bild."""
        try:
            n = os.path.getsize(path)
        except OSError:
            return
        with self.lock:
            self.hits_bytes += n
        self.wanted = True

    def db_bytes(self):
        r = self.store.query('PRAGMA page_count')[0]
        pc = list(r.values())[0]
        fl = list(self.store.query('PRAGMA freelist_count')[0].values())[0]
        ps = list(self.store.query('PRAGMA page_size')[0].values())[0]
        return (pc - fl) * ps, fl * ps, pc * ps

    def usage(self):
        used, _free, _file = self.db_bytes()
        return used + self.hits_bytes

    def info(self, limit_gb):
        used, free, file_ = self.db_bytes()
        return {'data_bytes': used + self.hits_bytes, 'db_bytes': used, 'db_file_bytes': file_,
                'hits_bytes': self.hits_bytes, 'limit_bytes': int(limit_gb * GB) if limit_gb > 0 else 0}

    # ---------------------------------------------------------- städning
    def _delete_readings(self, rows):
        freed = 0
        for r in rows:
            for p in (r['crop'], r['context']):
                if p:
                    freed += _remove(os.path.join(HITS_DIR, p))
        if rows:
            ids = [r['id'] for r in rows]
            self.store.execute(f"DELETE FROM readings WHERE id IN ({','.join('?' * len(ids))})", ids)
        return freed

    def _reject_files(self):
        """Avvisade bilder, äldst först: [(sorteringsnyckel 'YYYY-MM-DDTHH:MM:SS', sökväg)]."""
        base = os.path.join(HITS_DIR, 'rejects')
        out = []
        if not os.path.isdir(base):
            return out
        for d in sorted(os.listdir(base)):
            if not DAY_RE.match(d):
                continue
            for f in sorted(os.listdir(os.path.join(base, d))):
                hhmmss = f[:6] if f[:6].isdigit() else '000000'
                out.append((f'{d}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}', os.path.join(base, d, f)))
        return out

    def _prune_age(self, days):
        days = max(1, int(days))
        cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
        rows = self.store.query('SELECT id, crop, context FROM readings WHERE ts_utc < ?', (cutoff,))
        freed = self._delete_readings(rows)
        limit_day = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
        n_rej = 0
        for key, path in self._reject_files():
            if key[:10] >= limit_day:
                break
            freed += _remove(path)
            n_rej += 1
        return len(rows), n_rej, freed

    def _prune_size(self, limit):
        """Raderar äldsta läsningar (med bilder) och avvisade bilder tills användningen är under gränsen."""
        n_read = n_rej = freed = 0
        rejects = self._reject_files()
        ri = 0
        while self.usage() > limit:
            batch = self.store.query('SELECT id, ts_local, crop, context FROM readings ORDER BY ts_utc, id LIMIT 20')
            if not batch and ri >= len(rejects):
                break
            # avvisade bilder som är äldre än äldsta läsningen går först
            oldest_reading = batch[0]['ts_local'] if batch else '9999'
            if ri < len(rejects) and rejects[ri][0] < oldest_reading:
                f = _remove(rejects[ri][1])
                ri += 1
                n_rej += 1
            else:
                r = batch[0]
                f = self._delete_readings([r])
                n_read += 1
            freed += f
            with self.lock:
                self.hits_bytes = max(0, self.hits_bytes - f)
        return n_read, n_rej, freed

    def _remove_empty_dirs(self):
        for base in (os.path.join(HITS_DIR, 'rejects'), HITS_DIR):
            if not os.path.isdir(base):
                continue
            for d in os.listdir(base):
                p = os.path.join(base, d)
                if DAY_RE.match(d) and os.path.isdir(p) and not os.listdir(p):
                    try:
                        os.rmdir(p)
                    except OSError:
                        pass

    def run(self, settings, force=False):
        """Kör tids- och storleksregeln. Returnerar sammanfattning (dict) om något gjordes, annars None."""
        now = time.time()
        limit_gb = float(settings.get('max_data_gb') or 0)
        limit = int(limit_gb * GB) if limit_gb > 0 else 0
        due = force or now - self.last_run >= INTERVAL_S
        over = limit and self.wanted and self.usage() > limit
        self.wanted = False
        if not (due or over):
            return None
        self.last_run = now
        if force or now - self.last_scan >= RESCAN_S:
            with self.lock:
                self.hits_bytes = _dir_bytes(HITS_DIR)
            self.last_scan = now
        before = self.usage()
        a_read, a_rej, a_freed = self._prune_age(settings['retention_days'])
        with self.lock:
            self.hits_bytes = max(0, self.hits_bytes - a_freed)
        s_read = s_rej = s_freed = 0
        if limit and self.usage() > limit:
            s_read, s_rej, s_freed = self._prune_size(limit)
        self._remove_empty_dirs()
        self.freed_since_vacuum += a_freed + s_freed
        vacuumed = self._maybe_vacuum(settings, force)
        after = self.usage()
        changed = a_read or a_rej or s_read or s_rej or vacuumed
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        summary = (f'{stamp}: tid {a_read} läsn./{a_rej} avvisade, storlek {s_read} läsn./{s_rej} avvisade, '
                   f'{(before - after) / 1e6:.2f} MB frigjort, nu {after / 1e6:.2f} MB'
                   + (f' av {limit / 1e6:.3g} MB' if limit else ' (ingen gräns)') + (', VACUUM' if vacuumed else ''))
        if changed:
            log.info('STÄDNING: tidsregel %d dagar: %d läsningar + %d avvisade bilder; storleksregel %s: %d läsningar + %d '
                     'avvisade bilder; frigjort %.2f MB; driftdata nu %.2f MB (db %.2f MB, bilder %.2f MB)%s',
                     settings['retention_days'], a_read, a_rej, f'{limit_gb:g} GB' if limit else 'av', s_read, s_rej,
                     (before - after) / 1e6, after / 1e6, self.db_bytes()[0] / 1e6, self.hits_bytes / 1e6,
                     ', VACUUM körd' if vacuumed else '')
        self.store.set_many({'last_prune': stamp, 'last_prune_info': summary}, bump=False)
        return {'summary': summary, 'changed': bool(changed)}

    def _maybe_vacuum(self, settings, force):
        _used, free, file_ = self.db_bytes()
        today = datetime.date.today().isoformat()
        daily = settings.get('last_vacuum') != today and free > 0
        if not (force and free > 0 or daily or (file_ and free > 0.2 * file_)):
            return False
        try:
            self.store.execute('VACUUM')
            self.store.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except Exception:
            log.exception('VACUUM misslyckades')
            return False
        self.store.set('last_vacuum', today, bump=False)
        settings['last_vacuum'] = today
        self.freed_since_vacuum = 0
        return True
