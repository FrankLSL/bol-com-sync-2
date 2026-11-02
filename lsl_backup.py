"""Versies en back-ups — gedeeld bestand (zelfde kopie in portal, dashboard, planner en facturen).

Elke nacht (standaard 03:15 Nederlandse tijd) maakt elke app een versie van al zijn gegevens:
  1. alle SQLite-databases in de datamap worden veilig gekopieerd (sqlite backup-API, ook als de app draait)
     en samen met de overige kleine bestanden in één archief <jjjj-mm-dd>.tar.gz gezet;
  2. dat archief komt op de eigen schijf (datamap/.versies, laatste 10 dagen) — werkt ook zonder R2;
  3. als R2 is ingesteld gaat het ook naar Cloudflare R2: <app>/<jjjj-mm-dd>.tar.gz
     (bewaren: dagelijks 14 dagen, zondag 12 weken, de 1e van de maand altijd), en mappen met vaste bestanden
     (facturen-pdf's, bezorgfoto's) gaan los en incrementeel naar <app>/<map>/<pad> — alleen nieuwe bestanden, nooit verwijderd;
  4. de code van de draaiende versie gaat één keer per deploy naar <app>/code/<commit>.tar.gz (nooit opgeruimd), met de
     namen (niet de waarden) van de Railway-variabelen erbij. Zo kun je de app ook zonder GitHub of Railway opnieuw opzetten.

Terugzetten kan in de app zelf: <app-adres>/versies (alleen beheerders). Vóór het terugzetten wordt automatisch
een versie 'voor-terugzetten' van de huidige stand gemaakt, zodat je het terugzetten ook weer ongedaan kunt maken.
Daarna herstart de app zichzelf.

Proefterugzetting: de 1e van de maand (na de nachtelijke versie) en op de knop "Nu testen" op /versies wordt de nieuwste versie
uit R2 (anders lokaal) in een tijdelijke map uitgepakt en gecontroleerd: databases heel (integrity check), tabellen en aantallen
regels vergeleken met de live gegevens, leeftijd van de versie, en of de losse bestanden (pdf's, foto's) ook in R2 staan.
Er wordt niets aan de live gegevens veranderd. Uitkomst in /versies en in /healthz (backup.test) voor de bewaking in het portaal.

Railway-variabelen (als gedeelde variabelen in het project):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
Optioneel: BACKUP_TIJD (03:15), BACKUP_UIT=1, VERSIES_LOKAAL (10).
"""
import datetime, html, io, json, os, shutil, sqlite3, tarfile, tempfile, threading, time
from pathlib import Path

_state = {'last_ok': None, 'last_error': None, 'last_try': None, 'detail': ''}
_cfg = {'app': '', 'data_dir': None, 'sync_dirs': ()}
LOCAL = '.versies'
_SKIP_NAMES = ('.backup_status.json', '.backup_test.json')


def configured():
    """R2 ingesteld (cloudkopie). Lokale versies werken altijd."""
    return all(os.getenv(k) for k in ('R2_ACCOUNT_ID', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET')) and os.getenv('BACKUP_UIT') != '1'


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client('s3', endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
                        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'], aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
                        region_name='auto', config=Config(retries={'max_attempts': 5, 'mode': 'standard'}))


def _nl_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Europe/Amsterdam'))
    except Exception:
        return datetime.datetime.utcnow() + datetime.timedelta(hours=1)


def _is_sqlite(p):
    try:
        with open(p, 'rb') as f:
            return f.read(16) == b'SQLite format 3\x00'
    except Exception:
        return False


def make_archive(data_dir, skip_dirs=(), max_file=50 * 1024 * 1024):
    """tar.gz met veilige kopieën van alle databases + overige bestanden (zonder de losse mappen en zonder oude versies)."""
    data_dir = Path(data_dir)
    skip = set(skip_dirs) | {LOCAL, 'lost+found', '__pycache__'}
    tmp = tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False)
    tmp.close()
    n, skipped = 0, []
    with tarfile.open(tmp.name, 'w:gz') as tar:
        for p in sorted(data_dir.rglob('*')):
            rel = p.relative_to(data_dir)
            if not p.is_file() or (rel.parts and rel.parts[0] in skip) or rel.name.endswith(('-wal', '-shm', '-journal')) \
                    or rel.name in _SKIP_NAMES or rel.name.endswith('.tar.gz'):
                continue
            if _is_sqlite(p):
                fd, copy = tempfile.mkstemp(suffix='.db')
                os.close(fd)
                src, dst = sqlite3.connect(str(p), timeout=30), sqlite3.connect(copy)
                with dst:
                    src.backup(dst)
                src.close()
                dst.close()
                tar.add(copy, arcname=str(rel))
                os.unlink(copy)
                n += 1
            elif p.stat().st_size <= max_file:
                tar.add(str(p), arcname=str(rel))
                n += 1
            else:
                skipped.append(f'{rel} ({p.stat().st_size / 1e6:.0f} MB)')
    return tmp.name, n, skipped


CODE_SKIP = {'.git', '__pycache__', 'data', LOCAL, 'node_modules', '.venv', 'venv', 'tests'}


def make_code_archive(code_dir, data_dir):
    """tar.gz van de code van de app (zonder gegevens, .env of git-map) + RAILWAY_VARIABELEN.txt met alleen de namen."""
    code_dir, data_dir = Path(code_dir).resolve(), Path(data_dir).resolve()
    tmp = tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False)
    tmp.close()
    with tarfile.open(tmp.name, 'w:gz') as tar:
        for p in sorted(code_dir.rglob('*')):
            rel = p.relative_to(code_dir)
            if not p.is_file() or set(rel.parts) & CODE_SKIP or data_dir == p.resolve() or data_dir in p.resolve().parents \
                    or rel.name.startswith('.env') or rel.suffix in ('.db', '.pyc') or rel.name.endswith('.tar.gz'):
                continue
            tar.add(str(p), arcname=str(rel))
        names = sorted(k for k in os.environ if not k.startswith(('RAILWAY_', 'PATH', 'HOME', 'HOSTNAME', 'PYTHON', 'LANG', 'GPG_', 'PWD', 'SHLVL', 'TERM', '_')))
        info = ('Railway-variabelen van deze app (alleen namen; de waarden staan in Railway en in de wachtwoordkluis):\n\n'
                + '\n'.join(names) + f"\n\nCommit: {os.getenv('RAILWAY_GIT_COMMIT_SHA', '?')}\nRepo: {os.getenv('RAILWAY_GIT_REPO_OWNER', '?')}/{os.getenv('RAILWAY_GIT_REPO_NAME', '?')}\n").encode()
        ti = tarfile.TarInfo('RAILWAY_VARIABELEN.txt')
        ti.size, ti.mtime = len(info), int(time.time())
        tar.addfile(ti, io.BytesIO(info))
    return tmp.name


def _code_backup(s3, bucket, app, data_dir):
    """Eén keer per deploy (commit) de code naar R2. Geeft een korte omschrijving terug."""
    sha = (os.getenv('RAILWAY_GIT_COMMIT_SHA') or '')[:12] or _nl_now().strftime('%Y-%m-%d')
    key = f'{app}/code/{sha}.tar.gz'
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return 'code al bewaard'
    except Exception:
        pass
    path = make_code_archive(Path(__file__).parent, data_dir)
    try:
        s3.upload_file(path, bucket, key)
    finally:
        os.unlink(path)
    return f'code {sha} bewaard'


def keep(day, today):
    age = (today - day).days
    return age < 14 or (day.weekday() == 6 and age < 7 * 12) or day.day == 1


def _local_dir(data_dir):
    d = Path(data_dir) / LOCAL
    d.mkdir(parents=True, exist_ok=True)
    return d


def _local_prune(data_dir):
    n = int(os.getenv('VERSIES_LOKAAL', '10') or 10)
    files = sorted((f for f in _local_dir(data_dir).glob('*.tar.gz') if not f.name.startswith('voor-terugzetten')), key=lambda f: f.name, reverse=True)
    for f in files[n:]:
        f.unlink(missing_ok=True)
    pre = sorted(_local_dir(data_dir).glob('voor-terugzetten-*.tar.gz'), key=lambda f: f.name, reverse=True)
    for f in pre[5:]:
        f.unlink(missing_ok=True)


def _sync_files(folder):
    """Alle bestanden in een map, ook in submappen (bv. fotos/2026-10/2026-10-27/x.jpg)."""
    return [f for f in sorted(folder.rglob('*')) if f.is_file()]


def run(app, data_dir, sync_dirs=(), name=None):
    """Eén versie maken (lokaal + R2 als ingesteld). Geeft een korte omschrijving terug."""
    today = _nl_now().date()
    name = name or today.isoformat()
    path, n, skipped = make_archive(data_dir, skip_dirs=sync_dirs)
    _state['skipped'] = skipped
    size = os.path.getsize(path)
    local = _local_dir(data_dir) / f'{name}.tar.gz'
    shutil.copyfile(path, local)
    _local_prune(data_dir)
    if not configured():
        os.unlink(path)
        return f'{n} bestanden ({size / 1e6:.1f} MB) — lokale versie (R2 niet ingesteld)'
    s3, bucket = _client(), os.environ['R2_BUCKET']
    try:
        s3.upload_file(path, bucket, f'{app}/{name}.tar.gz')
    finally:
        os.unlink(path)
    new = 0
    for d in sync_dirs:
        folder = Path(data_dir) / d
        if not folder.is_dir():
            continue
        have = set()
        for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=f'{app}/{d}/'):
            have |= {o['Key'] for o in page.get('Contents', [])}
        for f in _sync_files(folder):
            key = f'{app}/{d}/' + f.relative_to(folder).as_posix()
            if key not in have:
                s3.upload_file(str(f), bucket, key)
                new += 1
    removed = 0
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=f'{app}/', Delimiter='/'):
        for o in page.get('Contents', []):
            fname = o['Key'].split('/')[-1]
            try:
                day = datetime.date.fromisoformat(fname.replace('.tar.gz', ''))
            except ValueError:
                continue
            if not keep(day, today):
                try:  # met een R2-bucketslot (bucket lock) mag wissen pas na de bewaartermijn: dan morgen opnieuw
                    s3.delete_object(Bucket=bucket, Key=o['Key'])
                    removed += 1
                except Exception:
                    pass
    try:
        code = _code_backup(s3, bucket, app, data_dir)
    except Exception as e:
        code = f'code-back-up mislukt: {type(e).__name__}'
        _state['code_error'] = f'{type(e).__name__}: {str(e)[:200]}'
    else:
        _state['code_error'] = None
    return (f'{n} bestanden ({size / 1e6:.1f} MB) + {new} nieuwe losse bestanden, {removed} oude archieven opgeruimd, {code}'
            + (f' — LET OP: {len(skipped)} te groot bestand(en) niet meegenomen: ' + ', '.join(skipped[:5]) if skipped else ''))


def schijf():
    """Hoe vol de opslag (volume) van deze app is."""
    try:
        d = _cfg.get('data_dir')
        if not d:
            return None
        u = shutil.disk_usage(str(d))
        return {'gebruikt_gb': round(u.used / 1e9, 2), 'totaal_gb': round(u.total / 1e9, 2), 'pct': round(100 * u.used / u.total, 1)}
    except Exception:
        return None


def status():
    return {'configured': configured(), **_state, 'schijf': schijf()}


def run_safe(app, data_dir, sync_dirs=()):
    _state['last_try'] = _nl_now().strftime('%Y-%m-%d %H:%M')
    try:
        _state['detail'] = run(app, data_dir, sync_dirs)
        _state['last_ok'] = _state['last_try']
        _state['last_error'] = None
    except Exception as e:
        _state['last_error'] = f'{type(e).__name__}: {str(e)[:300]}'
    _save(data_dir)
    return _state


def _save(data_dir):
    try:
        (Path(data_dir) / '.backup_status.json').write_text(json.dumps(_state))
    except Exception:
        pass


def _load(data_dir):
    try:
        _state.update(json.loads((Path(data_dir) / '.backup_status.json').read_text()))
    except Exception:
        pass


def start(app, data_dir, sync_dirs=()):
    """Start de nachtelijke versie/back-up in een achtergrondthread (lokaal altijd, R2 als ingesteld)."""
    _cfg.update(app=app, data_dir=Path(data_dir), sync_dirs=tuple(sync_dirs))
    _load(data_dir)
    if os.getenv('BACKUP_UIT') == '1':
        return

    def loop():
        time.sleep(90)
        if configured():  # code van deze deploy meteen bewaren (niet pas om 03:15)
            try:
                _code_backup(_client(), os.environ['R2_BUCKET'], app, data_dir)
                _state['code_error'] = None
            except Exception as e:
                _state['code_error'] = f'{type(e).__name__}: {str(e)[:200]}'
        if not _state.get('last_ok') or _state['last_ok'][:10] != _nl_now().strftime('%Y-%m-%d'):
            hh, mm = (os.getenv('BACKUP_TIJD', '03:15') + ':0').split(':')[:2]
            if _nl_now().time() >= datetime.time(int(hh), int(mm)) or not _state.get('last_ok'):
                run_safe(app, data_dir, sync_dirs)
        _test_safe(app, data_dir, sync_dirs)  # nog nooit getest (of te lang geleden): meteen een keer
        while True:
            hh, mm = (os.getenv('BACKUP_TIJD', '03:15') + ':0').split(':')[:2]
            now = _nl_now()
            nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if nxt <= now:
                nxt += datetime.timedelta(days=1)
            time.sleep(max(60, (nxt - now).total_seconds()))
            run_safe(app, data_dir, sync_dirs)
            _test_safe(app, data_dir, sync_dirs)  # 1e van de maand: proefterugzetting
    threading.Thread(target=loop, daemon=True).start()


# ---------- proefterugzetting ----------

def _db_stats(path):
    c = sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=30)
    try:
        integ = c.execute('PRAGMA quick_check').fetchone()[0]
        tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        rows = {}
        for t in tabs:
            try:
                rows[t] = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except Exception:
                rows[t] = None
        return integ, rows
    finally:
        c.close()


def test_restore(app, data_dir, sync_dirs=()):
    """Nieuwste versie proef-terugzetten in een tijdelijke map en controleren. Verandert niets aan de live gegevens."""
    data_dir = Path(data_dir)
    t0 = time.time()
    res = {'at': _nl_now().strftime('%Y-%m-%d %H:%M'), 'ok': False, 'version': '', 'bron': '', 'dbs': [], 'problemen': [], 'info': []}
    path, tmpfile = None, False
    try:
        vers = [v for v in list_versions(app, data_dir) if len(v['id']) == len('2026-01-01.tar.gz') and v['id'][:4].isdigit()]
        if not vers:
            raise RuntimeError('geen enkele nachtelijke versie gevonden')
        v = vers[0]
        res['version'] = v['id'].replace('.tar.gz', '')
        if configured() and 'R2' in v['where']:  # juist de cloudkopie testen: die heb je nodig als de server weg is
            fd, path = tempfile.mkstemp(suffix='.tar.gz')
            os.close(fd)
            tmpfile = True
            _client().download_file(os.environ['R2_BUCKET'], f"{app}/{v['id']}", path)
            res['bron'] = 'Cloudflare R2'
        else:
            path, tmpfile = fetch(app, data_dir, v['id'])
            res['bron'] = 'lokaal' + (' (R2 niet ingesteld)' if not configured() else ' (staat niet in R2!)')
            if configured():
                res['problemen'].append('nieuwste versie staat niet in R2')
        oud = (_nl_now().date() - datetime.date.fromisoformat(res['version'])).days
        if oud > 2:
            res['problemen'].append(f'nieuwste versie is {oud} dagen oud')
        with tempfile.TemporaryDirectory() as td, tarfile.open(path, 'r:gz') as tar:
            base = Path(td).resolve()
            n = 0
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                target = (base / m.name).resolve()
                if base not in target.parents:
                    continue
                tar.extract(m, td)
                n += 1
            res['info'].append(f'{n} bestanden uitgepakt ({os.path.getsize(path) / 1e6:.1f} MB)')
            dbs = [p for p in sorted(base.rglob('*')) if p.is_file() and _is_sqlite(p)]
            if not dbs:
                res['problemen'].append('geen database in de versie')
            for p in dbs:
                rel = p.relative_to(base)
                d = {'naam': str(rel), 'integriteit': '', 'tabellen': 0, 'regels': 0, 'live_regels': None, 'kleiner': []}
                try:
                    integ, rows = _db_stats(p)
                    d['integriteit'] = integ
                    d['tabellen'], d['regels'] = len(rows), sum(x or 0 for x in rows.values())
                    if integ != 'ok':
                        res['problemen'].append(f'{rel}: database beschadigd ({integ[:80]})')
                    live = data_dir / rel
                    if live.exists() and _is_sqlite(live):
                        _, lrows = _db_stats(live)
                        d['live_regels'] = sum(x or 0 for x in lrows.values())
                        for t, ln in lrows.items():
                            bn = rows.get(t)
                            if ln and ln >= 50 and (bn is None or bn < ln * 0.5):
                                d['kleiner'].append(f'{t}: {bn or 0} i.p.v. {ln}')
                        if d['kleiner']:
                            res['problemen'].append(f'{rel}: tabel(len) veel kleiner dan live — ' + '; '.join(d['kleiner'][:4]))
                except Exception as e:
                    d['integriteit'] = f'fout: {type(e).__name__}'
                    res['problemen'].append(f'{rel}: niet te openen ({type(e).__name__}: {str(e)[:100]})')
                res['dbs'].append(d)
        if configured():
            s3, bucket = _client(), os.environ['R2_BUCKET']
            for dname in sync_dirs:
                folder = data_dir / dname
                if not folder.is_dir():
                    continue
                local = {f'{app}/{dname}/' + f.relative_to(folder).as_posix() for f in _sync_files(folder)}
                have = set()
                for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=f'{app}/{dname}/'):
                    have |= {o['Key'] for o in page.get('Contents', [])}
                mis = len(local - have)
                res['info'].append(f'map {dname}: {len(local)} bestanden, {len(local) - mis} in R2')
                if local and mis > max(20, len(local) * 0.02):
                    res['problemen'].append(f'map {dname}: {mis} van {len(local)} bestanden staan (nog) niet in R2')
        res['ok'] = not res['problemen']
    except Exception as e:
        res['problemen'].append(f'proefterugzetting mislukt: {type(e).__name__}: {str(e)[:200]}')
    finally:
        if tmpfile and path:
            try:
                os.unlink(path)
            except Exception:
                pass
    res['duur_s'] = round(time.time() - t0, 1)
    try:
        (data_dir / '.backup_test.json').write_text(json.dumps(res))
    except Exception:
        pass
    _state['test'] = {'at': res['at'], 'ok': res['ok'], 'version': res['version'], 'msg': '; '.join(res['problemen'])[:300]}
    _save(data_dir)
    return res


def last_test(data_dir):
    try:
        return json.loads((Path(data_dir) / '.backup_test.json').read_text())
    except Exception:
        return None


def _test_due():
    t = _state.get('test') or {}
    if not t.get('at'):
        return True
    try:
        age = (_nl_now().date() - datetime.date.fromisoformat(t['at'][:10])).days
    except ValueError:
        return True
    return _nl_now().day == 1 and age >= 1 or age >= 31


def _test_safe(app, data_dir, sync_dirs):
    try:
        if _test_due():
            test_restore(app, data_dir, sync_dirs)
    except Exception:
        pass


# ---------- versies bekijken en terugzetten ----------

def list_versions(app, data_dir):
    """Alle beschikbare versies: lokaal (op de schijf van de app) en in R2."""
    out = {}
    for f in _local_dir(data_dir).glob('*.tar.gz'):
        st = f.stat()
        out[f.name] = {'id': f.name, 'where': ['lokaal'], 'size': st.st_size,
                       'at': datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d %H:%M')}
    if configured():
        try:
            s3, bucket = _client(), os.environ['R2_BUCKET']
            for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=f'{app}/', Delimiter='/'):
                for o in page.get('Contents', []):
                    fname = o['Key'].split('/')[-1]
                    if not fname.endswith('.tar.gz'):
                        continue
                    v = out.setdefault(fname, {'id': fname, 'where': [], 'size': o['Size'],
                                               'at': o['LastModified'].astimezone().strftime('%Y-%m-%d %H:%M')})
                    v['where'].append('R2')
        except Exception as e:
            out['_fout'] = {'id': '', 'error': f'R2 niet bereikbaar: {e}'[:200]}
    return sorted([v for v in out.values() if v.get('id')], key=lambda v: v['id'], reverse=True)


def _safe_id(vid):
    if not vid.endswith('.tar.gz') or '/' in vid or '\\' in vid or vid.startswith('.'):
        raise ValueError('ongeldige versie')
    return vid


def fetch(app, data_dir, vid):
    """Pad naar het archief van een versie (lokaal, anders uit R2 gehaald naar een tijdelijk bestand)."""
    vid = _safe_id(vid)
    p = _local_dir(data_dir) / vid
    if p.is_file():
        return str(p), False
    if not configured():
        raise FileNotFoundError('versie niet gevonden')
    fd, tmp = tempfile.mkstemp(suffix='.tar.gz')
    os.close(fd)
    _client().download_file(os.environ['R2_BUCKET'], f'{app}/{vid}', tmp)
    return tmp, True


def restore(app, data_dir, vid, sync_dirs=()):
    """Zet een versie terug. Eerst wordt de huidige stand bewaard als 'voor-terugzetten-…'.
    Databases worden met de sqlite backup-API overschreven (veilig terwijl de app draait), andere bestanden gekopieerd.
    Bestanden die nu bestaan maar niet in de versie zitten (bv. nieuwere foto's) blijven staan."""
    data_dir = Path(data_dir)
    stamp = _nl_now().strftime('%Y-%m-%d_%H%M')
    pre = run(app, data_dir, sync_dirs, name=f'voor-terugzetten-{stamp}')
    path, tmpfile = fetch(app, data_dir, vid)
    n = 0
    try:
        with tempfile.TemporaryDirectory() as td, tarfile.open(path, 'r:gz') as tar:
            base = Path(td).resolve()
            for m in tar.getmembers():
                if not m.isfile():
                    continue
                target = (base / m.name).resolve()
                if base not in target.parents:
                    continue  # geen paden buiten de map
                tar.extract(m, td)
                rel = target.relative_to(base)
                if rel.parts and rel.parts[0] == LOCAL:
                    continue
                live = data_dir / rel
                live.parent.mkdir(parents=True, exist_ok=True)
                if _is_sqlite(target) and live.exists() and _is_sqlite(live):
                    src, dst = sqlite3.connect(str(target)), sqlite3.connect(str(live), timeout=60)
                    with dst:
                        src.backup(dst)
                    src.close()
                    dst.close()
                else:
                    shutil.copyfile(target, live)
                n += 1
    finally:
        if tmpfile:
            os.unlink(path)
    return {'restored': n, 'version': vid, 'safety': f'voor-terugzetten-{stamp}.tar.gz', 'safety_detail': pre}


def _restart_soon(sec=2):
    def bye():
        time.sleep(sec)
        os._exit(3)  # Railway herstart de service (restartPolicy ON_FAILURE) met de teruggezette gegevens
    threading.Thread(target=bye, daemon=True).start()


def _test_html(t):
    if not t:
        return ('<div class="panel"><b>Proefterugzetting</b>: nog niet gedaan. Gebeurt automatisch de 1e van de maand, of klik op "Nu testen".'
                '<form method="post" action="versies/test" style="margin-top:10px"><button>✓ Nu testen</button></form></div>')
    e = html.escape
    num = lambda x: '' if x is None else f'{x:,}'.replace(',', '.')
    dbs = ''.join(f"<tr><td>{e(d['naam'])}</td><td>{'✓' if d['integriteit'] == 'ok' else '<b style=color:#c0392b>' + e(d['integriteit']) + '</b>'}</td>"
                  f"<td>{d['tabellen']}</td><td>{num(d['regels'])}</td><td>{num(d['live_regels'])}</td></tr>" for d in t.get('dbs') or [])
    kop = (f"<b style='color:#3d8b2f'>✓ Geslaagd</b>" if t.get('ok') else "<b style='color:#c0392b'>✗ Problemen gevonden</b>")
    return (f"<div class='panel {'msg' if t.get('ok') else 'err'}'><b>Proefterugzetting</b> {e(t.get('at') or '')}: {kop} — versie {e(t.get('version') or '?')} uit {e(t.get('bron') or '?')}, "
            f"{e(str(t.get('duur_s', '')))} s. Er is niets aan de live gegevens veranderd."
            + (''.join(f'<div>⚠ {e(p)}</div>' for p in t.get('problemen') or []))
            + (f"<div><small>{e(' · '.join(t.get('info') or []))}</small></div>" if t.get('info') else '')
            + (f"<table style='margin-top:8px;min-width:0'><thead><tr><th>Database</th><th>Heel</th><th>Tabellen</th><th>Regels in versie</th><th>Regels nu</th></tr></thead><tbody>{dbs}</tbody></table>" if dbs else '')
            + '<form method="post" action="versies/test" style="margin-top:10px"><button>✓ Nu opnieuw testen</button></form></div>')


def _page(app, rows, msg='', st=None, test=None):
    st = st or status()
    fmt = lambda b: f'{b / 1e6:.1f} MB' if b >= 1e5 else f'{b / 1e3:.0f} kB'
    tr = ''.join(
        f"<tr><td><b>{html.escape(v['id'].replace('.tar.gz', ''))}</b><br><small>{html.escape(v['at'])}</small></td>"
        f"<td>{html.escape(' + '.join(v['where']))}</td><td>{fmt(v['size'])}</td>"
        f"<td><a href='versies/download/{html.escape(v['id'])}'>⬇ download</a></td>"
        f"<td><form method='post' action='versies/terugzetten' onsubmit=\"return confirm('Alle gegevens van {html.escape(app)} terugzetten naar "
        f"{html.escape(v['id'].replace('.tar.gz', ''))}?\\n\\nDe huidige stand wordt eerst bewaard als versie, zodat je dit ongedaan kunt maken. "
        f"De app herstart daarna (± 30 seconden).')\"><input type='hidden' name='v' value='{html.escape(v['id'])}'>"
        f"<button>↺ Terugzetten</button></form></td></tr>" for v in rows) or "<tr><td colspan='5'>Nog geen versies.</td></tr>"
    cloud = 'Cloudflare R2 ✓' if st['configured'] else 'R2 niet ingesteld — alleen lokale versies op de schijf van de app'
    return f"""<!doctype html><html lang="nl"><head><meta charset="utf-8"><link rel="icon" href="/static/favicon.svg" type="image/svg+xml"><link rel="icon" href="/static/favicon-32.png" sizes="32x32" type="image/png"><link rel="apple-touch-icon" href="/static/apple-touch-icon.png"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Versies · {html.escape(app)}</title><link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>body{{font-family:Poppins,system-ui,sans-serif;background:#f6f3ee;color:#1d2b29;margin:0}}header{{background:#1f4b47;border-top:4px solid #84bd3f;color:#fff;padding:14px 4%}}
main{{max-width:1000px;margin:auto;padding:18px 14px}}.panel{{background:#fff;border:1px solid #e4dfd6;border-radius:12px;padding:16px;margin-bottom:14px;overflow:auto}}
table{{width:100%;border-collapse:collapse;min-width:560px}}th{{background:#1f4b47;color:#fff;text-align:left;padding:8px;font-weight:600;font-size:13px}}td{{padding:8px;border-bottom:1px solid #eee;vertical-align:top}}
button{{background:#1f4b47;color:#fff;border:0;border-radius:999px;padding:7px 14px;font:inherit;font-weight:600;cursor:pointer}}small{{color:#5f6f6c}}a{{color:#2c625c}}
.msg{{background:#e9f3dc;border-left:5px solid #84bd3f}}.err{{background:#fbe1e1;border-left:5px solid #c0392b}}</style></head><body>
<header><b>Loungesetland</b> · versies van <b>{html.escape(app)}</b></header><main>
{f"<div class='panel {'err' if msg.startswith('Fout') else 'msg'}'>{html.escape(msg)}</div>" if msg else ''}
<div class="panel"><b>Elke nacht om {html.escape(os.getenv('BACKUP_TIJD', '03:15'))}</b> wordt een volledige versie van de gegevens bewaard
(databases + instellingen). Laatste versie: <b>{html.escape(st.get('last_ok') or 'nog geen')}</b>{' · <span style="color:#c0392b">laatste poging mislukt: ' + html.escape(st['last_error']) + '</span>' if st.get('last_error') else ''}.<br>
Opslag: {cloud}. Terugzetten bewaart eerst de huidige stand als <i>voor-terugzetten</i>, dus je kunt het altijd weer ongedaan maken.<br>
<form method="post" action="versies/nu" style="margin-top:10px"><button>＋ Nu een versie maken</button></form></div>
{_test_html(test)}
<div class="panel"><table><thead><tr><th>Versie</th><th>Waar</th><th>Grootte</th><th></th><th></th></tr></thead><tbody>{tr}</tbody></table></div>
<p><small>De code van de app zelf heeft ook versies: elke wijziging staat in GitHub, in Railway (Deployments → oude deploy → Redeploy) en per deploy in Cloudflare R2 (map code/).</small></p>
</main></body></html>"""


def mount(app, appname, data_dir, is_admin, sync_dirs=(), prefix='/versies'):
    """Voegt de pagina <prefix> toe: versies bekijken, downloaden, nu maken en terugzetten. is_admin(request) -> bool."""
    from fastapi import Request, HTTPException
    from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse
    from starlette.background import BackgroundTask

    def guard(request):
        try:
            ok = is_admin(request)
        except Exception:
            ok = False
        if not ok:
            raise HTTPException(403, 'Alleen voor beheerders')

    @app.get(prefix, response_class=HTMLResponse)
    def versies_page(request: Request, msg: str = ''):
        guard(request)
        return _page(appname, list_versions(appname, data_dir), msg, test=last_test(data_dir))

    @app.get(prefix + '/download/{vid}')
    def versies_download(vid: str, request: Request):
        guard(request)
        try:
            path, tmp = fetch(appname, data_dir, vid)
        except Exception as e:
            raise HTTPException(404, str(e))
        return FileResponse(path, filename=f'{appname}-{vid}', media_type='application/gzip',
                            background=BackgroundTask(os.unlink, path) if tmp else None)

    @app.post(prefix + '/nu')
    def versies_now(request: Request):
        guard(request)
        try:
            d = run(appname, data_dir, sync_dirs, name=_nl_now().strftime('%Y-%m-%d_%H%M'))
            m = 'Versie gemaakt: ' + d
        except Exception as e:
            m = f'Fout: {e}'[:300]
        from urllib.parse import quote
        return RedirectResponse(f'{prefix}?msg={quote(m)}', status_code=303)

    @app.post(prefix + '/test')
    def versies_test(request: Request):
        guard(request)
        r = test_restore(appname, data_dir, sync_dirs)
        from urllib.parse import quote
        m = 'Proefterugzetting geslaagd ✓' if r['ok'] else 'Fout: proefterugzetting vond problemen — zie hieronder'
        return RedirectResponse(f'{prefix}?msg={quote(m)}', status_code=303)

    @app.post(prefix + '/terugzetten')
    async def versies_restore(request: Request):
        guard(request)
        form = await request.form()
        from urllib.parse import quote
        try:
            r = restore(appname, data_dir, str(form.get('v') or ''), sync_dirs)
        except Exception as e:
            return RedirectResponse(f"{prefix}?msg={quote(f'Fout bij terugzetten: {e}'[:300])}", status_code=303)
        _restart_soon()
        m = (f"Teruggezet naar {r['version'].replace('.tar.gz', '')} ({r['restored']} bestanden). De app herstart nu — ververs over een halve minuut. "
             f"Ongedaan maken: zet versie {r['safety'].replace('.tar.gz', '')} terug.")
        return HTMLResponse(f"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='25;url={prefix}'>"
                            f"<body style='font-family:system-ui;padding:30px'><h3>✓ {html.escape(m)}</h3></body>")
