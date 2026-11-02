"""Loungesetland bol.com — prijs-sync (site +10%), EAN-beheer en producten op bol zetten.

Lokaal:  pip install -r requirements.txt && NO_SCHEDULER=1 uvicorn app:app --reload   (zonder SSO_SECRET open, alleen lokaal)
Online:  zie README.md.
"""
import csv, html, io, json, os, threading
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import bol, content, lsl_backup, lsl_icons, lsl_sso, shopify, store, sync

E = lambda s: html.escape(str(s if s is not None else ''))
BASE = Path(__file__).parent
APPKEY = 'bol'
ROLES = {'beheerder': 'Beheerder', 'bekijker': 'Bekijker'}
OPEN = ('/healthz', '/sso/', '/static/', '/favicon.ico', '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png', '/manifest.webmanifest')

store.init()
content.init()
app = FastAPI(title='Loungesetland bol.com', docs_url=None, redoc_url=None, openapi_url=None)
lsl_sso.csrf_middleware(app)
lsl_sso.speed_middleware(app)
app.mount('/static', StaticFiles(directory=BASE / 'static'), name='static')
lsl_icons.install(app, BASE / 'static', 'Loungesetland bol.com', 'bol.com')


def current(request):
    if not lsl_sso.enabled():
        return None if lsl_sso.online() else {'u': 'lokaal', 'n': 'Lokaal', 'r': 'beheerder'}
    s = lsl_sso.user(request)
    if not s:
        return None
    r = (s.get('apps') or {}).get(APPKEY)
    if s.get('a'):
        r = 'beheerder'
    return {'u': s['u'], 'n': s.get('n') or s['u'], 'r': r} if r in ROLES else {'u': s['u'], 'n': s.get('n'), 'r': None}


@app.middleware('http')
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith(OPEN):
        return await call_next(request)
    h = lsl_sso.handoff(request)
    if h is not None:
        return h
    u = current(request)
    if not u:
        if path.startswith('/api/'):
            return JSONResponse({'detail': 'Niet ingelogd'}, status_code=401)
        return RedirectResponse(lsl_sso.login_url(request), status_code=303)
    if not u['r']:
        return HTMLResponse(page('Geen toegang', '<h1>Geen toegang</h1><p>Je account heeft geen rechten voor bol.com. Vraag een beheerder dit in de portal aan te zetten.</p>'), status_code=403)
    request.state.user = u
    return await call_next(request)


def admin(request):
    if request.state.user['r'] != 'beheerder':
        raise HTTPException(403, 'Alleen voor beheerders')


@app.on_event('startup')
def _start():
    sync.start_scheduler()
    content.start_checker()
    lsl_backup.start('bol', store.DATA)
    if bol.configured() and shopify.configured() and not store.kv_get('cache_bol'):
        threading.Thread(target=_refresh_bg, daemon=True).start()
    # onderhoud: RUN_ON_START=proef (proefrun bij opstarten) of catalog:<EAN> (bol-content van één EAN naar de log)
    for ros in [x.strip() for x in os.getenv('RUN_ON_START', '').split(',') if x.strip()]:
        if ros == 'proef':
            sync.run_async('opstart', True)
        elif ros.startswith('catalog:'):
            def _cat(e=ros.split(':', 1)[1]):
                try:
                    print('CATALOG', e, json.dumps(bol.catalog_product(e), ensure_ascii=False)[:6000], flush=True)
                except Exception as ex:
                    print('CATALOG fout', e, ex, flush=True)
            threading.Thread(target=_cat, daemon=True).start()


lsl_backup.mount(app, 'bol', store.DATA, lambda r: r.state.user['r'] == 'beheerder')


@app.get('/healthz')
def healthz(request: Request):
    if not lsl_sso.intern_ok(request, 'healthz'):
        return {'ok': True}
    last = (store.runs(1) or [{}])[0]
    return {'ok': True, 'volume': os.path.ismount(str(store.DATA)), 'bol': bol.configured(), 'shopify': shopify.configured(),
            'runs': [{'job': 'prijs-sync', 'ok': bool(last.get('ok')), 'at': last.get('klaar') or last.get('gestart'), 'msg': json.dumps(last.get('samenvatting') or {})[:160]}] if last else [],
            'backup': lsl_backup.status(), 'speed': lsl_sso.speed_stats()}


@app.get('/sso/shell.js')
def sso_shell():
    return Response(lsl_sso.shell_loader_js(), media_type='application/javascript', headers={'Cache-Control': 'no-cache'})


# ---------- opmaak ----------
CSS = """
:root{--green:#1f4b47;--g2:#2c625c;--lime:#84bd3f;--bg:#f6f3ee;--line:#e4dfd6;--fg:#1d2b29;--mut:#5f6f6c;--bad:#c0392b;--ok:#3d8b2f;--warn:#8a5a00}
*{box-sizing:border-box}body{margin:0;font-family:Poppins,system-ui,sans-serif;background:var(--bg);color:var(--fg);font-size:14px}
main{max-width:1280px;margin:0 auto;padding:18px 16px 60px}h1{color:var(--green);font-size:22px;margin:4px 0 6px}h2{color:var(--green);font-size:16px;margin:0 0 10px}
.box{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:16px;overflow:auto}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin:10px 0 16px}.kpi{background:#fff;border:1px solid var(--line);border-top:3px solid var(--lime);border-radius:12px;padding:12px 14px;text-decoration:none;color:inherit}
.kpi .v{font-size:22px;font-weight:700;color:var(--green)}.kpi .l{color:var(--mut);font-size:12px}.kpi.bad{border-top-color:var(--bad)}.kpi.on{outline:2px solid var(--green)}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{background:var(--green);color:#fff;font-weight:600;font-size:12.5px;position:sticky;top:0}
.r{text-align:right}.mut{color:var(--mut)}.bad{color:var(--bad)}.ok{color:var(--ok)}.small{font-size:12px}a{color:var(--g2)}
.pill{display:inline-block;font-size:11px;padding:1px 8px;border-radius:999px;background:#e9f3dc;color:var(--green);margin:1px;white-space:nowrap}.pill.bad{background:#fbe6e3;color:var(--bad)}.pill.warn{background:#fdf3d7;color:var(--warn)}.pill.grey{background:#eee;color:#555}
button,.btn{font:inherit;font-weight:600;background:var(--green);color:#fff;border:0;border-radius:999px;padding:7px 14px;cursor:pointer;text-decoration:none;display:inline-block}
button:disabled{opacity:.5;cursor:default}.btn.sec,button.sec{background:#fff;color:var(--green);border:1px solid var(--green)}button.s{padding:4px 10px;font-size:12px}
input,select{font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px}
.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}.sp{flex:1}.tabs a{padding:6px 12px}
img.t{width:44px;height:32px;object-fit:cover;border-radius:6px;background:#eee}
"""
TABS = [('/', 'Producten'), ('/productinfo', 'Productinfo'), ('/sync', 'Prijs-sync'), ('/ean', 'EAN-codes'), ('/log', 'Logboek')]


def page(title, body):
    return (f'<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{lsl_icons.LINKS}<title>{E(title)} · bol.com · Loungesetland</title>'
            f'<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">'
            f'<script src="/sso/shell.js"></script><style>{CSS}</style></head><body><main>{body}</main></body></html>')


def nav(cur):
    return '<div class="bar tabs">' + ''.join(f'<a class="btn {"" if h == cur else "sec"}" href="{h}">{l}</a>' for h, l in TABS) + '</div>'


def eur(v):
    return '' if v in (None, '') else '€ ' + f'{float(v):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')


def notice():
    msgs = []
    if not bol.configured():
        msgs.append('Zet <b>BOL_CLIENT_ID</b> en <b>BOL_CLIENT_SECRET</b> in Railway.')
    if not shopify.configured():
        msgs.append('Shopify-koppeling ontbreekt (SHOPIFY_SHOP + SHOPIFY_CLIENT_ID/SECRET).')
    return ''.join(f'<div class="box bad">{m}</div>' for m in msgs)


# ---------- verversen ----------
_ref = {'busy': False, 'err': ''}


def _refresh_bg():
    if _ref['busy']:
        return
    _ref.update(busy=True, err='')
    try:
        sync.refresh()
    except Exception as e:
        _ref['err'] = str(e)[:300]
    finally:
        _ref['busy'] = False


@app.post('/ververs')
def ververs(request: Request):
    threading.Thread(target=_refresh_bg, daemon=True).start()
    return RedirectResponse(request.headers.get('referer') or '/', status_code=303)


def stand():
    s, b = sync.cached('shop'), sync.cached('bol')
    t = f'Shopify: {E(s["t"] or "nog niet")} · bol: {E(b["t"] or "nog niet")}'
    if _ref['busy']:
        t += ' · <b>bezig met ophalen…</b> <script>setTimeout(()=>location.reload(),8000)</script>'
    if _ref['err']:
        t += f' · <span class="bad">{E(_ref["err"])}</span>'
    return t


# ---------- producten ----------
def product_rows():
    shop, offers = sync.cached('shop')['rows'], sync.cached('bol')['rows']
    by_ref, by_ean = {}, {}
    for o in offers:
        if o.get('reference'):
            by_ref.setdefault(o['reference'], o)
        if o.get('ean'):
            by_ean[store.norm_ean(o['ean'])] = o
    plan = {a['sku']: a for a in sync.plan(offers, shop)} if offers else {}
    eans = {r['sku']: r for r in store.q("SELECT * FROM ean WHERE sku IS NOT NULL AND sku<>''")}
    skus = {}
    for v in shop:
        if v['sku']:
            skus.setdefault(v['sku'], []).append(v)
    rows = []
    for v in shop:
        sku = v['sku']
        e = eans.get(sku)
        o = by_ref.get(sku) if sku else None
        if not o and e:
            o = by_ean.get(e['ean'])
        p = plan.get(sku) if o else None
        st = 'op_bol' if o else ('geen_sku' if not sku else ('dubbel' if len(skus.get(sku, [])) > 1 else 'niet'))
        rows.append({**v, 'ean': (o or {}).get('ean') or (e or {}).get('ean') or '', 'offer': o, 'st': st,
                     'doel': sync.target_price(v['prijs']) if v['prijs'] else None, 'plan': p})
    return rows


FILTERS = [('niet', 'Nog niet op bol', 'live, met SKU'), ('op_bol', 'Op bol', ''), ('probleem', 'Aandacht', 'sync slaat over / SKU dubbel'),
           ('geen_sku', 'Zonder SKU', ''), ('alles', 'Alles', '')]


def match(r, f):
    if f == 'alles':
        return True
    if f == 'niet':
        return r['st'] == 'niet' and r['live']
    if f == 'op_bol':
        return r['st'] == 'op_bol'
    if f == 'probleem':
        return r['st'] == 'dubbel' or (r['st'] == 'op_bol' and r['plan'] and r['plan']['status'] == 'overgeslagen')
    return r['st'] == f


@app.get('/', response_class=HTMLResponse)
def producten(request: Request, f: str = 'niet', q: str = ''):
    u = request.state.user
    rows = product_rows()
    counts = {k: sum(1 for r in rows if match(r, k)) for k, _, _ in FILTERS}
    ql = q.strip().lower()
    sel = [r for r in rows if match(r, f) and (not ql or ql in (r['titel'] + ' ' + r['sku'] + ' ' + r['ean']).lower())]
    sel.sort(key=lambda r: (r['titel']))
    ec = store.ean_counts()
    kp = ''.join(f'<a class="kpi {"on" if k == f else ""}" href="/?f={k}"><div class="v">{counts[k]}</div><div class="l">{l}{(" · " + h) if h else ""}</div></a>' for k, l, h in FILTERS)
    kp += f'<a class="kpi" href="/ean"><div class="v">{ec.get("vrij", 0)}</div><div class="l">vrije EAN-codes</div></a>'
    can = u['r'] == 'beheerder' and bol.configured()
    body = []
    for r in sel[:1500]:
        o, p = r['offer'], r['plan']
        if r['st'] == 'op_bol':
            bp = (o or {}).get('price')
            stt = '<span class="pill">op bol</span>'
            if p and p['status'] == 'overgeslagen':
                stt += f'<div class="small bad">{E(p["reden"])}</div>'
            elif p and p['status'] == 'wijzigen':
                stt += '<div class="small" style="color:var(--warn)">prijs gaat mee bij volgende sync</div>'
            if o and not o.get('forSale'):
                stt += '<span class="pill warn">niet te koop</span>'
            act = f'<a class="btn sec s" href="/content/{E(r["sku"])}">Productinfo</a>' if r['sku'] else ''
        else:
            bp = None
            stt = {'niet': '<span class="pill grey">niet op bol</span>', 'geen_sku': '<span class="pill bad">geen SKU</span>',
                   'dubbel': '<span class="pill bad">SKU dubbel in Shopify</span>'}[r['st']]
            if not r['live']:
                stt += '<span class="pill warn">niet live</span>'
            act = (f'<button class="s" onclick="zet(this,{json.dumps(r["sku"])})">Zet op bol</button>' if can and r['st'] == 'niet' and r['live'] else '')
        body.append(f'<tr><td>{f"<img class=t src={E(r["foto"])}&width=88 loading=lazy>" if r["foto"] else ""}</td>'
                    f'<td><a href="{E(r["url"])}" target="_blank" rel="noopener">{E(r["titel"])}</a><div class="small mut">{E(r["merk"])}</div></td>'
                    f'<td class="small">{E(r["sku"])}</td><td class="small">{E(r["ean"])}</td><td class="r">{eur(r["prijs"])}</td>'
                    f'<td class="r">{eur(r["doel"])}</td><td class="r">{eur(bp)}</td><td>{stt}</td><td class="r" data-act>{act}</td></tr>')
    meer = f'<p class="mut small">Eerste 1500 van {len(sel)} getoond — zoek om te filteren.</p>' if len(sel) > 1500 else ''
    js = """<script>
async function zet(b, sku){
  const st = document.getElementById('stock').value || '10';
  if(!confirm('Zet ' + sku + ' op bol met prijs site +10% en voorraad ' + st + '?\\nEr wordt automatisch een vrije EAN-code toegewezen.')) return;
  b.disabled = true; b.textContent = 'Bezig…';
  try{
    const r = await fetch('/api/zet-op-bol', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({sku, stock: +st})});
    const d = await r.json();
    if(!r.ok) throw new Error(d.detail || r.status);
    b.parentElement.innerHTML = '<span class="pill">aangemaakt</span><div class="small">EAN ' + d.ean + ' · € ' + d.prijs + '</div>' + (d.melding ? '<div class="small" style="color:#8a5a00">' + d.melding + '</div>' : '') + '<a class="btn sec s" href="/content/' + encodeURIComponent(sku) + '">Productinfo</a>';
  }catch(e){ b.disabled = false; b.textContent = 'Opnieuw'; alert('Mislukt: ' + e.message); }
}
</script>"""
    html_ = (nav('/') + '<h1>Producten op bol.com</h1>' + notice() +
             f'<div class="bar small mut"><span>{stand()}</span><span class="sp"></span>'
             + ('<form method="post" action="/ververs" style="margin:0"><button class="sec s">↻ Ververs Shopify + bol</button></form>' if u['r'] == 'beheerder' else '') + '</div>'
             f'<div class="kpis">{kp}</div>'
             f'<div class="box"><form class="bar" method="get"><input type="hidden" name="f" value="{E(f)}"><input name="q" value="{E(q)}" placeholder="Zoek op naam, SKU of EAN" style="min-width:280px">'
             f'<button class="sec s">Zoek</button><span class="sp"></span>'
             + (f'<label class="small mut">Voorraad bij aanmaken <input id="stock" type="number" min="0" max="999" value="{E(os.getenv("BOL_STOCK", "10"))}" style="width:70px"></label>' if can else '<input id="stock" type="hidden" value="10">') +
             f'</form><table><tr><th></th><th>Product</th><th>SKU</th><th>EAN</th><th class="r">Site</th><th class="r">Doel bol</th><th class="r">Nu op bol</th><th>Status</th><th></th></tr>{"".join(body)}</table>{meer}</div>' + js)
    return page('Producten', html_)


@app.post('/api/zet-op-bol')
async def api_zet(request: Request):
    admin(request)
    d = await request.json()
    sku, stock = (d.get('sku') or '').strip(), int(d.get('stock') or 10)
    door = request.state.user['u']
    shop = [v for v in sync.cached('shop')['rows'] if v['sku'] == sku]
    if len(shop) != 1:
        raise HTTPException(400, 'SKU niet (uniek) gevonden in Shopify — ververs eerst')
    v = shop[0]
    if not v['live']:
        raise HTTPException(400, 'Product staat niet live in de webwinkel')
    if any(o.get('reference') == sku for o in sync.cached('bol')['rows']):
        raise HTTPException(400, 'Deze SKU staat al op bol')
    tpl = store.kv_get('bol_template')
    if not tpl:
        raise HTTPException(400, 'Nog geen bol-gegevens opgehaald — klik eerst op Ververs')
    ean = store.claim_ean(sku, v['titel'], door)
    if not ean:
        raise HTTPException(400, 'Geen vrije EAN-codes meer — voeg nieuwe toe onder EAN-codes')
    prijs = sync.target_price(v['prijs'])
    try:
        res = bol.create_offer(ean, sku, prijs, stock, tpl) or {}
    except Exception as e:
        store.log(door, 'zet op bol', sku, ean, False, str(e))
        raise HTTPException(502, f'bol weigerde: {str(e)[:400]}')
    oid = res.get('offerId')
    store.x("UPDATE ean SET status='op_bol', bijgewerkt=?, door=? WHERE ean=?", (store.now(), door, ean))
    melding = ''
    try:
        if oid:
            nfs = bol.not_for_sale_reasons(oid) or {}
            rs = nfs.get('reasons') or nfs.get('notForSaleReasons') or []
            if rs:
                melding = 'Nog niet te koop: ' + '; '.join(str(r.get('description') or r.get('code') or r) for r in rs)[:300]
    except Exception:
        pass
    try:  # bol kent het product nog niet: meteen productinfo meesturen als de productgroep bekend is
        if content.current(ean) is None:
            chunk = content.guess_chunk(v.get('type') or '')
            if chunk:
                content.send(ean, sku, content.proposal(v), ['titel', 'beschrijving', 'kleur', 'afmetingen', 'fotos'], door, chunk)
                melding = (melding + ' · ' if melding else '') + 'productinfo + foto\'s naar bol gestuurd (productgroep ' + chunk + ')'
            else:
                melding = (melding + ' · ' if melding else '') + 'bol kent dit product nog niet: open Productinfo om titel, tekst en foto\'s in te sturen'
    except Exception as e:
        melding = (melding + ' · ' if melding else '') + 'productinfo niet verstuurd: ' + str(e)[:150]
    cache = sync.cached('bol')
    cache['rows'].append({'offerId': oid, 'ean': ean, 'reference': sku, 'price': prijs, 'bundles': 1, 'stock': stock, 'forSale': not melding})
    store.kv_set('cache_bol', cache)
    store.log(door, 'zet op bol', sku, ean, True, json.dumps({'offerId': oid, 'prijs': prijs, 'voorraad': stock, 'melding': melding}))
    return {'ok': True, 'ean': ean, 'prijs': f'{prijs:.2f}'.replace('.', ','), 'offerId': oid, 'melding': melding}


# ---------- productinfo (content) ----------
VELDEN = [('titel', 'Titel'), ('beschrijving', 'Beschrijving'), ('kleur', 'Kleur'), ('afmetingen', 'Afmetingen'), ('fotos', "Foto's")]


def _variant(sku):
    m = [v for v in sync.cached('shop')['rows'] if v['sku'] == sku]
    if not m:
        raise HTTPException(404, 'SKU niet gevonden in Shopify — ververs eerst')
    return m[0]


def _ean_of(sku):
    o = next((o for o in sync.cached('bol')['rows'] if o.get('reference') == sku), None)
    if o and o.get('ean'):
        return store.norm_ean(o['ean'])
    e = store.ean_for_sku(sku)
    return e['ean'] if e else ''


@app.get('/content/{sku:path}', response_class=HTMLResponse)
def content_page(sku: str, request: Request):
    v = _variant(sku)
    ean = _ean_of(sku)
    prop = content.proposal(v)
    cur, err = None, ''
    if ean:
        try:
            cur = content.current(ean)
            if cur:
                content.learn_chunk(prop['type'], cur['chunk'])
        except Exception as e:
            err = str(e)[:300]
    chunk = (cur or {}).get('chunk') or content.guess_chunk(prop['type'])
    fotos_nu = len((cur or {}).get('attrs', {}).get('Image', []) or [])
    afm = ', '.join(f'{k.replace("Product ", "").replace("Width", "breedte").replace("Depth", "diepte").replace("Height", "hoogte")} {v} cm' for k, v in prop['afmetingen'].items()) or '<span class="mut">niet gevonden in specs</span>'
    cur_afm = ', '.join(f'{k.replace("Product ", "")} {", ".join(map(str, cur["attrs"][k]))}' for k in ('Product Width', 'Product Depth', 'Product Height') if cur and cur['attrs'].get(k)) if cur else ''
    def cell(x):
        return x if x else '<span class="mut">–</span>'
    rows = [
        ('Titel', E((cur or {}).get('titel')), E(prop['titel'])),
        ('Kleur', E(', '.join((cur or {}).get('attrs', {}).get('Colour', []) or [])), E(prop['kleur'])),
        ('Afmetingen', E(cur_afm), afm),
        ("Foto's", '', ''.join(f'<img class=t src="{E(u)}&width=120" style="width:70px;height:50px;margin:2px">' for u in prop['fotos']) + f'<div class="small mut">{len(prop["fotos"])} foto\'s uit Shopify, eerste als hoofdfoto</div>'),
        ('Beschrijving', f'<div style="max-height:360px;overflow:auto" class="small">{(cur or {}).get("beschrijving") or ""}</div>', f'<div style="max-height:360px;overflow:auto" class="small">{prop["beschrijving"]}</div>'),
    ]
    tr = ''.join(f'<tr><th style="position:static;width:110px">{l}</th><td style="width:45%">{cell(a)}</td><td>{cell(b)}</td></tr>' for l, a, b in rows)
    can = request.state.user['r'] == 'beheerder' and bool(ean)
    status = ('<span class="pill bad">bol kent dit product nog niet</span>' if ean and not cur and not err else
              (f'<span class="pill">{"gepubliceerd" if cur["published"] else "niet gepubliceerd"}</span> <span class="pill grey">verrijking {cur["enrichment"]}/2</span> <span class="pill grey">productgroep {E(cur["chunk"])}</span> <span class="pill grey">merk {E(cur["merk"])}</span>' if cur else ''))
    form = ''
    if can:
        checks = ''.join(f'<label style="display:inline-block;margin-right:14px"><input type="checkbox" name="v" value="{k}" checked style="width:auto"> {l}</label>' for k, l in VELDEN)
        form = (f'<form method="post" action="/content/{E(sku)}" class="box" onsubmit="return confirm(\'Dit voorstel naar bol sturen? bol beslist zelf of het wordt overgenomen.\')">'
                f'<h2>Naar bol sturen</h2><div>{checks}</div>'
                f'<label class="small mut">Productgroep (GPC-code){" — verplicht, bol kent het product nog niet" if not cur else ""}</label>'
                f'<input name="chunk" value="{E(chunk if not cur else "")}" placeholder="{E(chunk or "bv. 80006655")}" style="width:160px" {"" if not cur else "disabled"}>'
                f'<p class="small mut">Productgroep {"blijft " + E(cur["chunk"]) + " (bestaand product)" if cur else ("voorgesteld op basis van andere " + E(prop["type"]) + "-producten" if chunk else "onbekend: zoek de code op in het datamodel van bol")}. '
                'Na ±1 uur staat onder Productinfo per veld of bol het heeft overgenomen.</p><button>Verstuur naar bol</button></form>')
    body = (nav('/productinfo') + f'<h1>{E(v["titel"])}</h1><div class="bar small"><span>SKU {E(sku)}</span><span>EAN {E(ean or "—")}</span>{status}</div>'
            + (f'<div class="box bad">{E(err)}</div>' if err else '') + ('' if ean else '<div class="box bad">Nog geen EAN: zet het product eerst op bol.</div>')
            + f'<div class="box"><table><tr><th style="position:static"></th><th style="position:static">Nu op bol</th><th style="position:static">Voorstel uit Shopify</th></tr>{tr}</table></div>' + form)
    return page('Productinfo', body)


@app.post('/content/{sku:path}')
async def content_send(sku: str, request: Request):
    admin(request)
    f = await request.form()
    velden = [x for x in f.getlist('v') if x in dict(VELDEN)]
    v = _variant(sku)
    ean = _ean_of(sku)
    if not ean or not velden:
        raise HTTPException(400, 'Geen EAN of geen velden gekozen')
    cur = content.current(ean)
    chunk = '' if cur else (f.get('chunk') or '').strip()
    if not cur and not chunk:
        raise HTTPException(400, 'bol kent dit product nog niet: vul de productgroep (GPC-code) in')
    try:
        content.send(ean, sku, content.proposal(v), velden, request.state.user['u'], chunk)
    except Exception as e:
        store.log(request.state.user['u'], 'content naar bol', sku, ean, False, str(e))
        return HTMLResponse(page('Mislukt', nav('/productinfo') + f'<h1>bol weigerde het voorstel</h1><div class="box bad">{E(str(e)[:800])}</div>'
                                 f'<a class="btn sec" href="/content/{E(sku)}">← terug naar {E(sku)}</a>'), status_code=502)
    if chunk:
        content.learn_chunk(v.get('type') or '', chunk)
    return RedirectResponse('/productinfo?msg=Verstuurd: ' + sku, status_code=303)


@app.get('/productinfo', response_class=HTMLResponse)
def productinfo_page(request: Request, msg: str = ''):
    ups = content.uploads()
    tr = ''
    for u in ups:
        sm = content.summary(u['rapport']) if u['status'] == 'klaar' else {}
        res = ''
        if sm:
            res = (f'<span class="ok">{len(sm["ok"])} overgenomen</span>' + (f' · <span class="bad">{len(sm["nee"])} geweigerd: {E("; ".join(sm["nee"][:6]))}</span>' if sm['nee'] else ''))
        elif u['status'] == 'fout' or (u['rapport'] and '"fout"' in (u['rapport'] or '')):
            res = f'<span class="bad small">{E((u["rapport"] or "")[:300])}</span>'
        else:
            res = '<span class="mut small">rapport volgt ±1 uur na indienen</span>'
        tr += (f'<tr><td class="small">{E(u["t"])}</td><td>{E(u["door"])}</td><td><a href="/content/{E(u["sku"])}">{E(u["sku"])}</a></td><td class="small">{E(u["ean"])}</td>'
               f'<td class="small">{E(u["velden"])}</td><td><span class="pill {"" if u["status"] == "klaar" else ("bad" if u["status"] == "fout" else "warn")}">{E(u["status"])}</span></td><td>{res}</td></tr>')
    knop = '<form method="post" action="/productinfo/check" style="margin:0"><button class="sec s">↻ Status nu ophalen</button></form>' if request.state.user['r'] == 'beheerder' else ''
    body = (nav('/productinfo') + '<h1>Productinfo naar bol</h1>' + (f'<div class="box ok">{E(msg)}</div>' if msg else '') +
            '<div class="box"><p>Kies bij <a href="/?f=op_bol">Producten</a> een product en klik op <b>Productinfo</b>: links wat er nu op bol staat, rechts het voorstel uit Shopify '
            '(titel, beschrijving, kleur, afmetingen, foto\'s). bol beslist zelf of het wordt overgenomen; bij merkproducten kan de info van het merk voorgaan.</p>'
            f'<div class="bar">{knop}<span class="small mut">Status wordt elke 10 minuten automatisch bijgewerkt.</span></div></div>'
            f'<div class="box"><table><tr><th>Ingediend</th><th>Door</th><th>SKU</th><th>EAN</th><th>Velden</th><th>Status</th><th>Resultaat</th></tr>{tr}</table></div>')
    return page('Productinfo', body)


@app.post('/productinfo/check')
def productinfo_check(request: Request):
    admin(request)
    threading.Thread(target=content.check_uploads, daemon=True).start()
    return RedirectResponse('/productinfo?msg=Status wordt opgehaald — ververs de pagina over een minuut', status_code=303)


# ---------- prijs-sync ----------
@app.get('/sync', response_class=HTMLResponse)
def sync_page(request: Request):
    u, c = request.state.user, sync.cfg()
    runs = store.runs(40)
    rows = ''.join(
        f'<tr><td><a href="/sync/{r["id"]}">{E(r["gestart"])}</a></td><td>{"proef" if r["proef"] else "<b>live</b>"}</td><td>{E(r["door"])}</td>'
        f'<td>{"<span class=pill>ok</span>" if r["ok"] else ("<span class=\"pill warn\">bezig</span>" if r["ok"] is None else "<span class=\"pill bad\">fout</span>")}</td>'
        f'<td class="small">{E(", ".join(f"{k}: {v}" for k, v in r["samenvatting"].items()))}{E(r["fout"] or "")}</td></tr>' for r in runs)
    dag = ['maandag', 'dinsdag', 'woensdag', 'donderdag', 'vrijdag', 'zaterdag', 'zondag'][c['dag']]
    knoppen = ''
    if u['r'] == 'beheerder':
        knoppen = ('<form method="post" action="/sync/run" class="bar" style="margin:0"><input type="hidden" name="proef" value="1"><button class="sec">Proefrun (wijzigt niets)</button></form>'
                   '<form method="post" action="/sync/run" class="bar" style="margin:0" onsubmit="return confirm(\'Nu echt alle bol-prijzen bijwerken?\')"><input type="hidden" name="proef" value="0"><button>Nu doorvoeren</button></form>')
    body = (nav('/sync') + '<h1>Prijs-sync</h1>' + notice() +
            f'<div class="box"><p>bol-prijs = <b>siteprijs × {c["markup"]:g}</b>, vanaf €100 naar boven afgerond op een bedrag eindigend op 9, daaronder op ,95. '
            f'Koppeling: <b>reference op bol = SKU in Shopify</b>. Alleen de prijs wordt aangepast; voorraad en levertijd niet.</p>'
            f'<p>Automatisch: elke <b>{dag} {E(c["tijd"])}</b> · stand: <b>{"live" if c["live"] else "proef (DRY_RUN)"}</b> · sprongen groter dan {c["max_change"] * 100:.0f}% worden overgeslagen'
            + (f' · FORCE_REFS: {E(", ".join(sorted(c["force"])))}' if c['force'] else '') + (f' · SKIP_REFS: {E(", ".join(sorted(c["skip"])))}' if c['skip'] else '') + '</p>'
            f'<div class="bar">{knoppen}{"<b>Er loopt nu een sync…</b><script>setTimeout(()=>location.reload(),6000)</script>" if sync.running() else ""}</div></div>'
            f'<div class="box"><h2>Runs</h2><table><tr><th>Gestart</th><th>Soort</th><th>Door</th><th>Status</th><th>Resultaat</th></tr>{rows}</table></div>')
    return page('Prijs-sync', body)


@app.post('/sync/run')
def sync_run(request: Request, proef: str = Form('1')):
    admin(request)
    if sync.running():
        return RedirectResponse('/sync', status_code=303)
    sync.run_async(request.state.user['u'], proef == '1')
    return RedirectResponse('/sync', status_code=303)


ST_ORDER = {'fout': 0, 'overgeslagen': 1, 'gewijzigd': 2, 'zou-wijzigen': 2, 'al-goed': 3}


@app.get('/sync/{rid}.csv')
def sync_csv(rid: int):
    lines = store.q('SELECT status, sku, ean, site, bol_oud, bol_nieuw, reden FROM runregel WHERE run_id=?', (rid,))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['status', 'sku', 'ean', 'siteprijs', 'bol_oud', 'bol_nieuw', 'reden'])
    for l in lines:
        w.writerow([l['status'], l['sku'], l['ean'], *(('' if l[k] is None else f'{l[k]:.2f}'.replace('.', ',')) for k in ('site', 'bol_oud', 'bol_nieuw')), l['reden']])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': f'attachment; filename=bol-prijssync-{rid}.csv'})


@app.get('/sync/{rid}', response_class=HTMLResponse)
def sync_detail(rid: int, request: Request, s: str = ''):
    r = store.q('SELECT * FROM run WHERE id=?', (rid,))
    if not r:
        raise HTTPException(404)
    r = r[0]
    lines = store.q('SELECT * FROM runregel WHERE run_id=?', (rid,))
    lines.sort(key=lambda l: (ST_ORDER.get(l['status'], 9), l['reden'] or '', l['sku'] or ''))
    cnt = {}
    for l in lines:
        cnt[l['status']] = cnt.get(l['status'], 0) + 1
    tabs = ' '.join(f'<a class="btn {"" if s == k else "sec"} s" href="?s={k}">{k} ({v})</a>' for k, v in sorted(cnt.items(), key=lambda kv: ST_ORDER.get(kv[0], 9)))
    sel = [l for l in lines if not s or l['status'] == s]
    tr = ''.join(f'<tr><td><span class="pill {"bad" if l["status"] == "fout" else ("warn" if l["status"] == "overgeslagen" else "")}">{E(l["status"])}</span></td>'
                 f'<td>{E(l["sku"])}</td><td class="small">{E(l["ean"])}</td><td class="r">{eur(l["site"])}</td><td class="r">{eur(l["bol_oud"])}</td><td class="r">{eur(l["bol_nieuw"])}</td>'
                 f'<td class="small">{E(l["reden"])}</td></tr>' for l in sel)
    body = (nav('/sync') + f'<h1>Run {E(r["gestart"])} · {"proef" if r["proef"] else "live"}</h1>'
            f'<div class="bar"><a class="btn sec s" href="/sync">← terug</a>{tabs}<span class="sp"></span><a class="btn sec s" href="/sync/{rid}.csv">Download (csv)</a></div>'
            + (f'<div class="box bad">{E(r["fout"])}</div>' if r['fout'] else '') +
            f'<div class="box"><table><tr><th>Status</th><th>SKU</th><th>EAN</th><th class="r">Site</th><th class="r">bol oud</th><th class="r">bol nieuw</th><th>Reden</th></tr>{tr}</table></div>')
    return page('Run', body)


# ---------- EAN ----------
@app.get('/ean', response_class=HTMLResponse)
def ean_page(request: Request, s: str = 'vrij', q: str = '', msg: str = ''):
    u = request.state.user
    c = store.ean_counts()
    rows = store.eans(None if s == 'alles' else s)
    ql = q.strip().lower()
    if ql:
        rows = [r for r in rows if ql in f"{r['ean']} {r['sku'] or ''} {r['titel'] or ''}".lower()]
    labels = {'vrij': 'Vrij', 'toegewezen': 'Toegewezen (nog niet op bol)', 'op_bol': 'Op bol', 'alles': 'Alles'}
    kp = ''.join(f'<a class="kpi {"on" if k == s else ""}" href="/ean?s={k}"><div class="v">{sum(c.values()) if k == "alles" else c.get(k, 0)}</div><div class="l">{l}</div></a>' for k, l in labels.items())
    tr = ''.join(f'<tr><td>{E(r["ean"])}{"" if store.ean_ok(r["ean"]) else " <span class=\"pill bad\">controlecijfer fout</span>"}</td><td>{E(r["sku"])}</td><td>{E(r["titel"])}</td>'
                 f'<td>{E(r["status"])}</td><td class="small mut">{E(r["bron"])} · {E(r["bijgewerkt"])} · {E(r["door"])}</td></tr>' for r in rows[:2000])
    up = ('<div class="box"><h2>EAN-codes toevoegen</h2><form method="post" action="/ean/import" enctype="multipart/form-data" class="bar">'
          '<input type="file" name="bestand" accept=".xlsx,.csv" required><button>Inlezen</button></form>'
          '<p class="small mut">Excel of csv met een kolom <b>EAN</b> (of "EAN Code"), optioneel <b>SKU</b> en <b>Productnaam</b>. Bestaande codes blijven zoals ze zijn; '
          'codes die al op bol staan worden nooit opnieuw uitgegeven.</p></div>') if u['r'] == 'beheerder' else ''
    body = (nav('/ean') + '<h1>EAN-codes</h1>' + (f'<div class="box ok">{E(msg)}</div>' if msg else '') + f'<div class="kpis">{kp}</div>' + up +
            f'<div class="box"><form class="bar" method="get"><input type="hidden" name="s" value="{E(s)}"><input name="q" value="{E(q)}" placeholder="Zoek EAN, SKU of naam" style="min-width:280px"><button class="sec s">Zoek</button>'
            f'<span class="sp"></span><a class="btn sec s" href="/ean.csv">Download alles (csv)</a></form>'
            f'<table><tr><th>EAN</th><th>SKU</th><th>Product</th><th>Status</th><th>Bron / bijgewerkt</th></tr>{tr}</table></div>')
    return page('EAN-codes', body)


@app.post('/ean/import')
async def ean_import(request: Request, bestand: UploadFile = File(...)):
    admin(request)
    raw = await bestand.read()
    rows = []
    if (bestand.filename or '').lower().endswith('.csv'):
        txt = raw.decode('utf-8-sig', errors='replace')
        dl = ';' if txt.count(';') > txt.count(',') else ','
        rows = list(csv.DictReader(io.StringIO(txt), delimiter=dl))
    else:
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True).active
        it = ws.iter_rows(values_only=True)
        head = [str(h or '').strip() for h in next(it)]
        rows = [dict(zip(head, ['' if v is None else str(v) for v in r])) for r in it]
    res = store.import_eans(rows, bestand.filename or 'upload', request.state.user['u'])
    store.log(request.state.user['u'], 'EAN-import', details=json.dumps(res))
    return RedirectResponse(f'/ean?msg=Ingelezen: {res["nieuw"]} nieuw, {res["bijgewerkt"]} bijgewerkt, {res["ongeldig"]} ongeldig', status_code=303)


@app.get('/ean.csv')
def ean_csv():
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=';')
    w.writerow(['ean', 'sku', 'productnaam', 'status', 'bron', 'bijgewerkt'])
    for r in store.eans():
        w.writerow([r['ean'], r['sku'], r['titel'], r['status'], r['bron'], r['bijgewerkt']])
    return Response('﻿' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename=ean-codes.csv'})


# ---------- logboek ----------
@app.get('/log', response_class=HTMLResponse)
def log_page(request: Request):
    rows = store.q('SELECT * FROM log ORDER BY t DESC LIMIT 500')
    tr = ''.join(f'<tr><td class="small">{E(r["t"])}</td><td>{E(r["door"])}</td><td>{E(r["actie"])}</td><td>{E(r["sku"])}</td><td class="small">{E(r["ean"])}</td>'
                 f'<td>{"<span class=pill>ok</span>" if r["ok"] else "<span class=\"pill bad\">mislukt</span>"}</td><td class="small mut">{E(r["details"])}</td></tr>' for r in rows)
    return page('Logboek', nav('/log') + '<h1>Logboek</h1><div class="box"><table><tr><th>Tijd</th><th>Wie</th><th>Actie</th><th>SKU</th><th>EAN</th><th></th><th>Details</th></tr>'
                + tr + '</table></div>')
