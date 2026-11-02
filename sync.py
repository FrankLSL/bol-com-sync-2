"""Prijs-sync: bol-prijs = siteprijs × MARKUP (afgerond), koppeling bol-reference = Shopify-SKU.
Raakt alleen de prijs aan. Draait wekelijks (SYNC_DAG/SYNC_TIJD, NL-tijd) en op verzoek."""
import datetime, math, os, threading, time, traceback
from collections import defaultdict
from zoneinfo import ZoneInfo

import bol, shopify, store

NL = ZoneInfo('Europe/Amsterdam')
_running = threading.Lock()


def cfg():
    return {'markup': float(os.getenv('MARKUP', '1.10')), 'max_change': float(os.getenv('MAX_CHANGE', '0.6')),
            'live': os.getenv('DRY_RUN', 'false').lower() == 'false',
            'force': {s.strip() for s in os.getenv('FORCE_REFS', '').split(',') if s.strip()},
            'skip': {s.strip() for s in os.getenv('SKIP_REFS', '').split(',') if s.strip()},
            'dag': int(os.getenv('SYNC_DAG', '0')),  # 0 = maandag
            'tijd': os.getenv('SYNC_TIJD', '06:15')}


def target_price(site, markup=None):
    """Vanaf €100 naar boven op een bedrag eindigend op 9, daaronder op x,95."""
    m = cfg()['markup'] if markup is None else markup
    x = site * m
    if x < 99.95:
        return round(math.ceil(x - 0.95 - 1e-9) + 0.95, 2)
    t = math.ceil(x - 1e-9)
    while t % 10 != 9:
        t += 1
    return float(min(t, 9999))


# ---------- gegevens ophalen (met cache voor de pagina's) ----------
def refresh(what=('shop', 'bol')):
    res = {}
    if 'shop' in what:
        res['shop'] = shopify.products()
        store.kv_set('cache_shop', {'t': store.now(), 'rows': res['shop']})
    if 'bol' in what:
        offers = bol.offers()
        store.kv_set('cache_bol', {'t': store.now(), 'rows': [{k: v for k, v in o.items() if k != 'raw'} for o in offers]})
        tpl = bol.template(offers)
        if tpl:
            store.kv_set('bol_template', tpl)
        store.sync_bol_eans(offers)
        res['bol'] = offers
    return res


def cached(name):
    return store.kv_get('cache_' + name) or {'t': None, 'rows': []}


def site_by_sku(shop_rows):
    m = defaultdict(list)
    for v in shop_rows:
        if v['live'] and v['sku']:
            m[v['sku']].append(v)
    return m


# ---------- plan ----------
def plan(offers, shop_rows, c=None):
    c = c or cfg()
    site = site_by_sku(shop_rows)
    out = []
    for o in offers:
        ref = (o.get('reference') or '').strip()
        a = {'offer': o, 'sku': ref, 'ean': o.get('ean'), 'oud': o.get('price'), 'site': None, 'nieuw': None, 'reden': ''}
        if not ref:
            a.update(status='overgeslagen', reden='geen reference (SKU) op bol')
        elif ref in c['skip']:
            a.update(status='overgeslagen', reden='staat in SKIP_REFS')
        elif not site.get(ref):
            a.update(status='overgeslagen', reden='SKU niet zichtbaar in de webwinkel (concept, archief of niet gepubliceerd)')
        else:
            prijzen = sorted({v['prijs'] for v in site[ref]})
            if len(prijzen) > 1:
                a.update(status='overgeslagen', reden=f"SKU staat {len(site[ref])}x op de site met verschillende prijzen ({' / '.join(f'{p:g}' for p in prijzen)})")
            elif prijzen[0] <= 0:
                a.update(status='overgeslagen', reden='siteprijs is 0')
            else:
                a['site'] = prijzen[0]
                a['nieuw'] = target_price(prijzen[0], c['markup'])
                if a['oud'] is not None and abs(a['oud'] - a['nieuw']) < 0.005 and o.get('bundles', 1) <= 1:
                    a['status'] = 'al-goed'
                elif a['oud'] and abs(a['nieuw'] / a['oud'] - 1) > c['max_change'] and ref not in c['force']:
                    a.update(status='overgeslagen', reden=f"sprong te groot ({a['oud']:g} → {a['nieuw']:g}); zet de SKU in FORCE_REFS als dit klopt")
                else:
                    a['status'] = 'wijzigen'
        out.append(a)
    return out


# ---------- run ----------
def run(door='planner', proef=None):
    """Voert de prijs-sync uit. proef=None → volgens DRY_RUN."""
    c = cfg()
    proef = (not c['live']) if proef is None else proef
    if not _running.acquire(blocking=False):
        raise RuntimeError('Er loopt al een sync')
    rid = store.run_start('prijs', proef, door)
    t0 = time.time()
    run_tag = datetime.datetime.now(NL).strftime('%Y-%m-%dT%H:%M')
    try:
        data = refresh()
        acts = plan(data['bol'], data['shop'], c)
        lines, tel = [], defaultdict(int)
        for a in acts:
            st = a['status']
            if st == 'wijzigen':
                if proef:
                    st = 'zou-wijzigen'
                else:
                    try:
                        bol.set_price(a['offer']['offerId'], a['nieuw'])
                        st = 'gewijzigd'
                        time.sleep(0.06)
                    except Exception as e:
                        st, a['reden'] = 'fout', str(e)[:300]
            tel[st] += 1
            lines.append((st, a['sku'], a['ean'], a['site'], a['oud'], a['nieuw'], a['reden']))
            print(';'.join(['RAPPORT', run_tag, st, a['sku'] or '', a['ean'] or '', *(('' if v is None else f'{v:g}') for v in (a['site'], a['oud'], a['nieuw'])),
                            (a['reden'] or '').replace(';', ',')]), flush=True)
        store.run_lines(rid, lines)
        if not proef:
            refresh(('bol',))  # cache met nieuwe prijzen
        samen = {'offers': len(acts), **tel, 'sec': round(time.time() - t0)}
        store.run_finish(rid, tel['fout'] == 0, samen)
        print(f"KLAAR {run_tag} {'PROEF' if proef else 'LIVE'} — " + ', '.join(f'{k}: {v}' for k, v in samen.items()), flush=True)
        return rid
    except Exception as e:
        store.run_finish(rid, False, {}, f'{type(e).__name__}: {e}'[:1000])
        print(f'FOUT prijs-sync: {e}\n{traceback.format_exc()}', flush=True)
        raise
    finally:
        _running.release()


def running():
    return _running.locked()


def run_async(door, proef=None):
    threading.Thread(target=lambda: _safe(door, proef), daemon=True).start()


def _safe(door, proef):
    try:
        run(door, proef)
    except Exception:
        pass


# ---------- wekelijkse planner ----------
def start_scheduler():
    if os.getenv('NO_SCHEDULER') == '1':
        return

    def loop():
        time.sleep(30)
        while True:
            try:
                c = cfg()
                now = datetime.datetime.now(NL)
                hh, mm = (c['tijd'] + ':0').split(':')[:2]
                week = now.strftime('%G-W%V')
                due = now.weekday() == c['dag'] and (now.hour, now.minute) >= (int(hh), int(mm))
                if due and store.kv_get('laatste_weekrun') != week and bol.configured() and shopify.configured():
                    store.kv_set('laatste_weekrun', week)
                    _safe('planner', None)
            except Exception as e:
                print('planner:', e, flush=True)
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()
