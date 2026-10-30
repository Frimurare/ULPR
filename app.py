#!/usr/bin/env python3
"""Prudencia LPR - startpunkt.

  app.py motor                 kör analysmotorn (systemd prudencia-lpr.service)
  app.py webb                  kör webbgränssnittet (systemd prudencia-lpr-web.service)
  app.py migrera --config F --plates F --hits D < keys.env
                               importerar prototypens konfiguration, nycklar (läses från stdin) och läsningar
  app.py losenord              sätter nytt administratörslösenord (frågar interaktivt)
  app.py stada                 kör lagringstidsstädning direkt
  app.py installning [NYCKEL [VÄRDE]]
                               visar eller ändrar en inställning (t.ex. detector, det_backend, hailo_tile, decoder);
                               hemliga värden visas aldrig, motorn läser in ändringen inom några sekunder
"""
import datetime
import getpass
import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def migrate(argv):
    from prudencia_lpr.store import HITS_DIR, Store
    args = dict(zip(argv[::2], argv[1::2]))
    st = Store()
    env = {}
    if not sys.stdin.isatty():
        for line in sys.stdin.read().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    cfg = {}
    if args.get('--config'):
        with open(args['--config']) as f:
            cfg = {k: v for k, v in json.load(f).items() if not k.startswith('_')}
    s = {}
    for k_old, k_new in (('det_imgsz', 'det_imgsz'), ('det_conf', 'det_conf'), ('min_char_conf', 'min_char_conf'),
                         ('early_votes', 'early_votes'), ('min_votes', 'min_votes'), ('track_timeout_s', 'track_timeout_s'),
                         ('dedup_s', 'dedup_s'), ('ocr_model', 'ocr_model')):
        if k_old in cfg:
            s[k_new] = cfg[k_old]
    if 'rate_limit' in cfg:
        s.update(rl_min=cfg['rate_limit']['per_min'], rl_hour=cfg['rate_limit']['per_hour'], rl_day=cfg['rate_limit']['per_day'])
    if env.get('OWS_AKI') and env.get('OWS_AKS') and env.get('OWS_EVENT_URL'):
        s.update(oe_aki=env['OWS_AKI'], oe_aks=env['OWS_AKS'], oe_url=env['OWS_EVENT_URL'].rstrip('/'),
                 oe_enabled=1 if cfg.get('send_enabled', True) else 0)
        print('OpenEye-nycklar importerade (värden visas inte).')
    if s:
        st.set_many(s)
    cam_id = None
    if cfg.get('camera_name'):
        existing = [c for c in st.cameras() if c['name'] == cfg['camera_name']]
        cam = {'name': cfg['camera_name'], 'enabled': 1, 'source_mode': 'host', 'host': cfg.get('camera_host', ''),
               'port': 554, 'path': cfg.get('rtsp_path', ''), 'username': env.get('CAM_USER', ''),
               'password': env.get('CAM_PASS', ''), 'external_id': cfg.get('external_camera_id', ''),
               'min_plate_w': cfg.get('min_plate_w'), 'target_fps': cfg.get('target_fps'), 'roi': cfg.get('roi')}
        cam_id = st.save_camera(cam, existing[0]['id'] if existing else None)
        print(f'Kamera "{cam["name"]}" importerad (id {cam_id}, inloggning {"ja" if cam["username"] else "nej"}).')
    if args.get('--plates') and os.path.exists(args['--plates']):
        old = sqlite3.connect(args['--plates'])
        old.row_factory = sqlite3.Row
        hits_src = args.get('--hits', '')
        have = {r['ts_utc'] for r in st.query('SELECT ts_utc FROM readings')}
        n = 0
        for r in old.execute('SELECT * FROM plates ORDER BY id'):
            if r['ts_utc'] in have:
                continue
            rel = {}
            for key in ('crop', 'context'):
                p = r[key]
                if p and hits_src and p.startswith(hits_src) and os.path.exists(p):
                    rp = os.path.relpath(p, hits_src)
                    os.makedirs(os.path.dirname(os.path.join(HITS_DIR, rp)), exist_ok=True)
                    shutil.copy2(p, os.path.join(HITS_DIR, rp))
                    rel[key] = rp
            status = r['status'] if r['status'] in ('sent', 'send_failed', 'rate_limited', 'logged') else 'logged'
            st.insert_reading(ts_utc=r['ts_utc'], ts_local=r['ts_local'], camera_id=cam_id, camera=r['camera'],
                              plate=r['plate'], confidence=r['confidence'], votes=r['votes'], reads=r['reads'],
                              frames=r['frames'], plate_w=r['plate_w'], crop=rel.get('crop'), context=rel.get('context'),
                              track_id=r['track_id'], status=status, ows_status=r['ows_status'],
                              frame_to_post_ms=r['frame_to_post_ms'], raw_reads=r['raw_reads'])
            n += 1
        print(f'{n} läsningar importerade från prototypen.')
    if args.get('--detector') and os.path.exists(args['--detector']):
        st.set('detector', args['--detector'])


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else ''
    if cmd == 'motor':
        from prudencia_lpr.engine import Engine
        Engine().run()
    elif cmd == 'webb':
        from prudencia_lpr.web import serve
        serve()
    elif cmd == 'migrera':
        migrate(a[1:])
    elif cmd == 'losenord':
        from werkzeug.security import generate_password_hash
        from prudencia_lpr.store import Store
        p1 = getpass.getpass('Nytt lösenord: ')
        if len(p1) < 8 or p1 != getpass.getpass('Upprepa: '):
            sys.exit('Lösenorden matchar inte eller är kortare än 8 tecken.')
        Store().set_many({'admin_hash': generate_password_hash(p1), 'must_change_pw': 0}, bump=False)
        print('Lösenordet är bytt.')
    elif cmd == 'installning':
        from prudencia_lpr.store import DEFAULTS, SECRET_KEYS, Store
        st = Store()
        hidden = SECRET_KEYS | {'oe_aki'}
        if len(a) == 1:
            for k, v in sorted(st.all_settings().items()):
                print(f'{k} = {"(dold)" if k in hidden and v else v}')
        elif len(a) == 2:
            print('(dold)' if a[1] in hidden else st.get(a[1]))
        else:
            if a[1] not in DEFAULTS or a[1] in hidden:
                sys.exit(f'Okänd eller skyddad inställning: {a[1]}')
            st.set(a[1], a[2])
            print(f'{a[1]} = {st.get(a[1])}')
    elif cmd == 'stada':
        from prudencia_lpr.engine import prune, setup_logging
        from prudencia_lpr.store import Store
        st = Store()
        setup_logging(st.get('log_level'))
        print(prune(st)['summary'])
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == '__main__':
    main()
