"""Productinfo naar bol (Product Content API v10): titel, beschrijving, kleur, afmetingen en foto's uit Shopify.
bol beslist zelf of het voorstel wordt overgenomen; het uploadrapport (na ±1 uur) laat per veld PUBLISHED/DECLINED zien."""
import html as _html, json, re, threading, time

import bol, shopify, store

ALLOWED = ('b', 'br', 'h3', 'li', 'ol', 'ul', 'p', 'strong')

def init():
    c = store.conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS upload(
      id INTEGER PRIMARY KEY AUTOINCREMENT, t TEXT, door TEXT, sku TEXT, ean TEXT, velden TEXT,
      process_id TEXT, upload_id TEXT, status TEXT, rapport TEXT, gecontroleerd TEXT);''')
    c.commit()
    c.close()


# ---------- Shopify ----------
DQ = '''query($id:ID!){ product(id:$id){ id title handle descriptionHtml productType vendor onlineStoreUrl
  images(first:15){ nodes{ url altText } }
  pi: metafield(namespace:"global", key:"Productinfo_left"){ value }
  specs: metafield(namespace:"global", key:"Alle_specificaties_table"){ value }
  kleur: metafield(namespace:"mm-google-shopping", key:"color"){ value } } }'''


def shop_detail(product_id):
    return shopify.graphql(DQ, {'id': product_id})['product']


def clean_html(h):
    """Alleen tags die bol toestaat; koppen → h3, links → tekst, scripts/tabellen weg."""
    h = re.sub(r'(?is)<(script|style|table|figure|iframe|img)[^>]*>.*?</\1>', '', h or '')
    h = re.sub(r'(?is)<img[^>]*>', '', h)
    h = re.sub(r'(?is)<h1[^>]*>.*?</h1>', '', h)  # titel staat al apart
    h = re.sub(r'(?is)<(/?)h[2-6][^>]*>', r'<\1h3>', h)
    h = re.sub(r'(?is)<(/?)em[^>]*>', '', h)
    h = re.sub(r'(?is)<hr[^>]*>', '', h)

    def tag(m):
        close, name = m.group(1), m.group(2).lower()
        return f'<{close}{name}>' if name in ALLOWED else ''
    h = re.sub(r'(?is)<(/?)([a-z0-9]+)(?:\s[^>]*)?/?>', tag, h)
    h = re.sub(r'(?is)<(p|h3|li|ul|ol|strong|b)>\s*</\1>', '', h)
    h = re.sub(r'&nbsp;', ' ', h)
    h = re.sub(r'\s+', ' ', h).strip()
    return h


def parse_specs(table_html):
    rows = {}
    for tr in re.findall(r'(?is)<tr[^>]*>(.*?)</tr>', table_html or ''):
        tds = [re.sub(r'\s+', ' ', _html.unescape(re.sub(r'(?s)<[^>]+>', '', td))).strip() for td in re.findall(r'(?is)<t[dh][^>]*>(.*?)</t[dh]>', tr)]
        if len(tds) >= 2 and tds[0]:
            rows[tds[0].rstrip(': ').strip()] = tds[1]
    return rows


def _cm(s):
    m = re.search(r'(\d+(?:[.,]\d+)?)', s or '')
    return round(float(m.group(1).replace(',', '.'))) if m else None


def dims(specs, titel=''):
    """{'Product Width':cm,'Product Depth':cm,'Product Height':cm} uit de specs-tabel (alleen wat er echt staat)."""
    out, low = {}, {k.lower(): v for k, v in specs.items()}
    for key, attr in (('breedte', 'Product Width'), ('diepte', 'Product Depth'), ('hoogte', 'Product Height')):
        k = next((k for k in low if k == key or k.startswith(key + ' ')), None)
        if k and _cm(low[k]):
            out[attr] = _cm(low[k])
    if 'Product Width' not in out:
        k = next((k for k in low if k.startswith('afmeting')), None)
        m = re.search(r'(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)(?:\s*[x×]\s*(\d+(?:[.,]\d+)?))?', low.get(k, '') if k else '')
        if m:
            out['Product Width'] = round(float(m.group(1).replace(',', '.')))
            out['Product Depth'] = round(float(m.group(2).replace(',', '.')))
            if m.group(3) and 'Product Height' not in out:
                out['Product Height'] = round(float(m.group(3).replace(',', '.')))
    return out


def proposal(variant):
    """Voorstel uit Shopify voor één variant (dict uit shopify.products())."""
    p = shop_detail(variant['product_id'])
    body = (p.get('pi') or {}).get('value') or p.get('descriptionHtml') or ''
    specs = parse_specs((p.get('specs') or {}).get('value'))
    kleur = ((p.get('kleur') or {}).get('value') or specs.get('Kleur') or '').strip()
    imgs = [i['url'] for i in (p.get('images') or {}).get('nodes', []) if i.get('url')][:12]
    return {'titel': variant['titel'][:250], 'beschrijving': clean_html(body), 'kleur': kleur, 'afmetingen': dims(specs),
            'fotos': imgs, 'type': p.get('productType') or '', 'merk': p.get('vendor') or '', 'specs': specs}


# ---------- bol ----------
def current(ean):
    c = bol.catalog_product(ean)
    if not c:
        return None
    if isinstance(c, dict) and 'products' in c:
        c = (c.get('products') or [None])[0]
    if not c:
        return None
    attrs = {a['id']: [v.get('value') for v in a.get('values', [])] for a in c.get('attributes', [])}
    return {'published': c.get('published'), 'enrichment': (c.get('enrichment') or {}).get('status'),
            'chunk': (c.get('gpc') or {}).get('chunkId'), 'attrs': attrs,
            'titel': (attrs.get('Title') or [''])[0], 'beschrijving': (attrs.get('Description') or [''])[0],
            'merk': next((p['name'] for p in c.get('parties', []) if p.get('role') == 'BRAND'), '')}


def learn_chunk(product_type, chunk):
    if product_type and chunk:
        m = store.kv_get('chunk_by_type') or {}
        m.setdefault(product_type, {})
        m[product_type][chunk] = m[product_type].get(chunk, 0) + 1
        store.kv_set('chunk_by_type', m)


def guess_chunk(product_type):
    m = (store.kv_get('chunk_by_type') or {}).get(product_type) or {}
    return max(m, key=m.get) if m else ''


def build_body(ean, prop, velden, chunk=''):
    a = [{'id': 'EAN', 'values': [{'value': ean}]}]
    if chunk:
        a.append({'id': 'GPC Code', 'values': [{'value': str(chunk)}]})
    if 'titel' in velden and prop['titel']:
        a.append({'id': 'Title', 'values': [{'value': prop['titel']}]})
    if 'beschrijving' in velden and prop['beschrijving']:
        a.append({'id': 'Description', 'values': [{'value': prop['beschrijving']}]})
    if 'kleur' in velden and prop['kleur']:
        a.append({'id': 'Colour', 'values': [{'value': prop['kleur']}]})
    if 'afmetingen' in velden:
        for k, v in prop['afmetingen'].items():
            a.append({'id': k, 'values': [{'value': str(v), 'unitId': 'unece.unit.CMT'}]})
    body = {'language': 'nl', 'attributes': a}
    if 'fotos' in velden and prop['fotos']:
        body['assets'] = [{'url': u, 'labels': ['FRONT']} if i == 0 else {'url': u, 'labels': []} for i, u in enumerate(prop['fotos'])]
        for x in body['assets']:
            if not x['labels']:
                x.pop('labels')
    return body


def send(ean, sku, prop, velden, door, chunk=''):
    body = build_body(ean, prop, velden, chunk)
    res = bol.post_content(body) or {}
    pid = str(res.get('processStatusId') or res.get('id') or '')
    store.x('INSERT INTO upload(t,door,sku,ean,velden,process_id,status) VALUES(?,?,?,?,?,?,?)',
            (store.now(), door, sku, ean, ','.join(velden), pid, 'ingediend'))
    store.log(door, 'content naar bol', sku, ean, True, json.dumps({'velden': velden, 'process': pid, 'chunk': chunk}))
    return pid


def uploads(n=200):
    return store.q('SELECT * FROM upload ORDER BY id DESC LIMIT ?', (n,))


def check_uploads():
    """Process-status → upload-id → rapport (rapport pas ±1 uur na indienen)."""
    for u in store.q("SELECT * FROM upload WHERE status NOT IN ('klaar','fout') ORDER BY id"):
        try:
            if not u['upload_id'] and u['process_id']:
                ps = bol.process_status(u['process_id']) or {}
                st = ps.get('status')
                if st == 'SUCCESS' and ps.get('entityId'):
                    store.x("UPDATE upload SET upload_id=?, status='verwerkt', gecontroleerd=? WHERE id=?", (str(ps['entityId']), store.now(), u['id']))
                elif st in ('FAILURE', 'TIMEOUT'):
                    store.x("UPDATE upload SET status='fout', rapport=?, gecontroleerd=? WHERE id=?", (json.dumps(ps)[:4000], store.now(), u['id']))
                continue
            if u['upload_id'] and time.time() - time.mktime(time.strptime(u['t'], '%Y-%m-%d %H:%M:%S')) > 3600:
                rep = bol.upload_report(u['upload_id'])
                if rep and str(rep.get('status', '')).upper() == 'COMPLETED':
                    store.x("UPDATE upload SET status='klaar', rapport=?, gecontroleerd=? WHERE id=?", (json.dumps(rep, ensure_ascii=False)[:20000], store.now(), u['id']))
                else:
                    store.x('UPDATE upload SET gecontroleerd=? WHERE id=?', (store.now(), u['id']))
        except Exception as e:
            store.x('UPDATE upload SET gecontroleerd=?, rapport=? WHERE id=?', (store.now(), json.dumps({'fout': str(e)[:500]}), u['id']))


def summary(rep_json):
    """Telt PUBLISHED/DECLINED per attribuut/foto + redenen."""
    try:
        rep = json.loads(rep_json or '{}')
    except Exception:
        return {}
    out = {'ok': [], 'nee': []}
    for a in (rep.get('attributes') or []):
        (out['ok'] if a.get('status') == 'PUBLISHED' else out['nee']).append(f"{a.get('id')}" + ('' if a.get('status') == 'PUBLISHED' else f" ({a.get('subStatusDescription') or a.get('subStatus') or a.get('status')})"))
    for a in (rep.get('assets') or []):
        (out['ok'] if a.get('status') == 'PUBLISHED' else out['nee']).append('foto' + ('' if a.get('status') == 'PUBLISHED' else f" ({a.get('subStatusDescription') or a.get('subStatus') or a.get('status')})"))
    return out


def start_checker():
    def loop():
        time.sleep(120)
        while True:
            try:
                if bol.configured():
                    check_uploads()
            except Exception as e:
                print('content-checker:', e, flush=True)
            time.sleep(600)
    threading.Thread(target=loop, daemon=True).start()
