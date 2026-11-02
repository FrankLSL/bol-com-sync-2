"""SQLite op /data: EAN-voorraad, prijs-sync-runs en een logboek van acties."""
import csv, json, os, sqlite3, threading, time
from pathlib import Path

DATA = Path(os.getenv('DATA_DIR') or (Path(__file__).parent / 'data'))
DB = DATA / 'bol.db'
SEED = Path(__file__).parent / 'seed'
_lock = threading.RLock()

# EAN-status: vrij (nog niet gebruikt) · toegewezen (aan een SKU, nog niet op bol) · op_bol (staat als offer op bol)
SCHEMA = '''
CREATE TABLE IF NOT EXISTS ean(
  ean TEXT PRIMARY KEY, sku TEXT, titel TEXT, status TEXT NOT NULL DEFAULT 'vrij', bron TEXT,
  bijgewerkt TEXT, door TEXT);
CREATE INDEX IF NOT EXISTS ean_sku ON ean(sku);
CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY AUTOINCREMENT, soort TEXT, gestart TEXT, klaar TEXT, ok INTEGER, proef INTEGER,
  door TEXT, samenvatting TEXT, fout TEXT);
CREATE TABLE IF NOT EXISTS runregel(
  run_id INTEGER, status TEXT, sku TEXT, ean TEXT, site REAL, bol_oud REAL, bol_nieuw REAL, reden TEXT);
CREATE INDEX IF NOT EXISTS runregel_run ON runregel(run_id);
CREATE TABLE IF NOT EXISTS log(
  t TEXT, door TEXT, actie TEXT, sku TEXT, ean TEXT, ok INTEGER, details TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
'''


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c


def q(sql, args=()):
    with _lock:
        c = conn()
        try:
            return [dict(r) for r in c.execute(sql, args).fetchall()]
        finally:
            c.close()


def x(sql, args=(), many=False):
    with _lock:
        c = conn()
        try:
            cur = c.executemany(sql, args) if many else c.execute(sql, args)
            c.commit()
            return cur.lastrowid
        finally:
            c.close()


def init():
    with _lock:
        c = conn()
        c.executescript(SCHEMA)
        c.commit()
        c.close()
    if not q('SELECT 1 FROM ean LIMIT 1') and (SEED / 'eans.csv').exists():
        import_eans(list(csv.DictReader(open(SEED / 'eans.csv', encoding='utf-8'))), 'startlijst', 'import')


def kv_get(k, default=None):
    r = q('SELECT v FROM kv WHERE k=?', (k,))
    return json.loads(r[0]['v']) if r else default


def kv_set(k, v):
    x('INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v', (k, json.dumps(v, ensure_ascii=False)))


def log(door, actie, sku='', ean='', ok=True, details=''):
    x('INSERT INTO log VALUES(?,?,?,?,?,?,?)', (now(), door, actie, sku, ean, 1 if ok else 0, str(details)[:2000]))


# ---------- EAN ----------
def norm_ean(e):
    e = ''.join(ch for ch in str(e or '') if ch.isdigit())
    return e.zfill(13) if 8 <= len(e) <= 13 else ''


def ean_ok(e):
    """GTIN-13 controlecijfer."""
    if len(e) != 13 or not e.isdigit():
        return False
    s = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(e[:12]))
    return (10 - s % 10) % 10 == int(e[12])


def import_eans(rows, bron, door):
    """rows: dicts met ean, (sku), (titel), (status). Bestaande EAN's worden niet teruggezet naar vrij."""
    n_new = n_upd = n_bad = 0
    for r in rows:
        e = norm_ean(r.get('ean') or r.get('EAN') or r.get('EAN Code'))
        if not e:
            n_bad += 1
            continue
        sku = (r.get('sku') or r.get('SKU') or '').strip()
        titel = (r.get('titel') or r.get('Productnaam') or '').strip()
        st = 'toegewezen' if sku else 'vrij'
        old = q('SELECT * FROM ean WHERE ean=?', (e,))
        if not old:
            x('INSERT INTO ean(ean,sku,titel,status,bron,bijgewerkt,door) VALUES(?,?,?,?,?,?,?)', (e, sku, titel, st, bron, now(), door))
            n_new += 1
        elif sku and not old[0]['sku']:
            x("UPDATE ean SET sku=?, titel=?, status=CASE WHEN status='op_bol' THEN status ELSE 'toegewezen' END, bijgewerkt=?, door=? WHERE ean=?",
              (sku, titel, now(), door, e))
            n_upd += 1
    return {'nieuw': n_new, 'bijgewerkt': n_upd, 'ongeldig': n_bad}


def eans(status=None):
    if status:
        return q('SELECT * FROM ean WHERE status=? ORDER BY ean', (status,))
    return q('SELECT * FROM ean ORDER BY status, ean')


def ean_counts():
    return {r['status']: r['n'] for r in q('SELECT status, COUNT(*) n FROM ean GROUP BY status')}


def ean_for_sku(sku):
    r = q("SELECT * FROM ean WHERE sku=? ORDER BY CASE status WHEN 'op_bol' THEN 0 WHEN 'toegewezen' THEN 1 ELSE 2 END LIMIT 1", (sku,))
    return r[0] if r else None


def claim_ean(sku, titel, door):
    """Geeft de EAN die bij deze SKU hoort, of wijst de eerstvolgende vrije (geldige) EAN toe. Nooit dubbel."""
    with _lock:
        cur = ean_for_sku(sku)
        if cur:
            return cur['ean']
        for r in q("SELECT ean FROM ean WHERE status='vrij' AND (sku IS NULL OR sku='') ORDER BY ean"):
            if ean_ok(r['ean']):
                x("UPDATE ean SET sku=?, titel=?, status='toegewezen', bijgewerkt=?, door=? WHERE ean=? AND status='vrij'", (sku, titel, now(), door, r['ean']))
                return r['ean']
    return None


def sync_bol_eans(offers):
    """Alles wat op bol staat, telt als gebruikt: bekende EAN's naar op_bol, onbekende erbij als 'op_bol' (bron bol)."""
    with _lock:
        known = {r['ean']: r for r in q('SELECT ean, sku, status FROM ean')}
        for o in offers:
            e = norm_ean(o.get('ean'))
            if not e:
                continue
            ref = (o.get('reference') or '').strip()
            if e not in known:
                x("INSERT INTO ean(ean,sku,titel,status,bron,bijgewerkt,door) VALUES(?,?,?,?,?,?,?)", (e, ref, '', 'op_bol', 'bol', now(), 'bol'))
            elif known[e]['status'] != 'op_bol' or (ref and not known[e]['sku']):
                x("UPDATE ean SET status='op_bol', sku=COALESCE(NULLIF(sku,''), ?), bijgewerkt=? WHERE ean=?", (ref, now(), e))


# ---------- runs ----------
def run_start(soort, proef, door):
    return x('INSERT INTO run(soort,gestart,ok,proef,door) VALUES(?,?,NULL,?,?)', (soort, now(), 1 if proef else 0, door))


def run_finish(rid, ok, samenvatting=None, fout=''):
    x('UPDATE run SET klaar=?, ok=?, samenvatting=?, fout=? WHERE id=?', (now(), 1 if ok else 0, json.dumps(samenvatting or {}, ensure_ascii=False), fout, rid))


def run_lines(rid, lines):
    x('INSERT INTO runregel VALUES(?,?,?,?,?,?,?,?)', [(rid, *l) for l in lines], many=True)


def runs(n=30):
    out = q('SELECT * FROM run ORDER BY id DESC LIMIT ?', (n,))
    for r in out:
        r['samenvatting'] = json.loads(r['samenvatting'] or '{}')
    return out
