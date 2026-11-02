"""bol.com Retailer API — Offers v11 (synchroon). Variabelen: BOL_CLIENT_ID, BOL_CLIENT_SECRET."""
import base64, os, threading, time

import requests

BASE = 'https://api.bol.com'
MT = 'application/vnd.retailer.v11+json'
_tok = {'t': None, 'x': 0}
_lock = threading.Lock()


class BolError(RuntimeError):
    def __init__(self, msg, status=0, body=''):
        super().__init__(msg)
        self.status, self.body = status, body


def configured():
    return bool(os.getenv('BOL_CLIENT_ID') and os.getenv('BOL_CLIENT_SECRET'))


def token():
    with _lock:
        if _tok['t'] and time.time() < _tok['x']:
            return _tok['t']
        cid, sec = os.getenv('BOL_CLIENT_ID', '').strip(), os.getenv('BOL_CLIENT_SECRET', '').strip()
        if not cid or not sec:
            raise BolError('BOL_CLIENT_ID / BOL_CLIENT_SECRET ontbreken')
        r = requests.post('https://login.bol.com/token', timeout=30, data={'grant_type': 'client_credentials'},
                          headers={'Authorization': 'Basic ' + base64.b64encode(f'{cid}:{sec}'.encode()).decode(), 'Accept': 'application/json'})
        if not r.ok:
            raise BolError(f'bol-token mislukt (HTTP {r.status_code}): {r.text[:200]}', r.status_code, r.text)
        j = r.json()
        _tok['t'], _tok['x'] = j['access_token'], time.time() + int(j.get('expires_in', 299)) - 30
        return _tok['t']


def call(method, path, body=None, tries=6):
    for i in range(tries):
        h = {'Authorization': 'Bearer ' + token(), 'Accept': MT}
        if body is not None:
            h['Content-Type'] = MT
        r = requests.request(method, BASE + path, json=body, headers=h, timeout=60)
        if r.status_code in (409, 429) or r.status_code >= 500:
            time.sleep(float(r.headers.get('Retry-After') or 0) or 1.5 * 2 ** i)
            continue
        if r.status_code == 401 and i == 0:
            _tok['t'] = None
            continue
        if not r.ok:
            raise BolError(f'{method} {path}: HTTP {r.status_code} {r.text[:600]}', r.status_code, r.text)
        return r.json() if r.text else None
    raise BolError(f'{method} {path}: opgegeven na {tries} pogingen')


def _price(o):
    bp = (o.get('pricing') or {}).get('bundlePrices') or []
    q1 = next((b for b in bp if int(b.get('quantity', 0)) == 1), None)
    return (float(q1.get('unitPrice', q1.get('price'))) if q1 else None), len(bp)


def offers():
    """Alle offers (ruw + kerngegevens)."""
    out, cursor = [], None
    for _ in range(1000):
        p = f'/retailer/offers?page-size={os.getenv("BOL_PAGE_SIZE", "50")}' + (f'&cursor={requests.utils.quote(cursor)}' if cursor else '')
        j = call('GET', p) or {}
        lst = j.get('offers') or j.get('items') or next((v for v in j.values() if isinstance(v, list)), [])
        for o in lst:
            price, nb = _price(o)
            out.append({'offerId': o.get('offerId'), 'ean': o.get('ean') or '', 'reference': (o.get('reference') or '').strip(),
                        'price': price, 'bundles': nb, 'stock': (o.get('stock') or {}).get('amount'),
                        'onHold': bool(o.get('onHoldByRetailer')), 'condition': (o.get('condition') or {}).get('category'),
                        'forSale': any(c.get('forSale') for c in (o.get('countryAvailabilities') or [])), 'raw': o})
        cursor = (j.get('page') or {}).get('nextCursor') or j.get('nextCursor')
        if not cursor or not lst:
            break
    return out


def set_price(offer_id, price):
    return call('PATCH', f'/retailer/offers/{offer_id}', {'pricing': {'bundlePrices': [{'quantity': 1, 'unitPrice': round(price, 2)}]}})


def template(existing):
    """Neemt fulfilment en landen over van een bestaand eigen offer (zelfde levering als de rest van je aanbod)."""
    from collections import Counter
    import json
    nieuw = [o['raw'] for o in existing if o.get('condition') in (None, 'NEW') and (o['raw'].get('fulfilment') or {}).get('method') == 'FBR']
    if not nieuw:
        return None
    key = Counter(json.dumps({'f': o.get('fulfilment'), 'c': o.get('countryAvailabilities')}, sort_keys=True) for o in nieuw).most_common(1)[0][0]
    return json.loads(key)


def create_offer(ean, reference, price, stock, tpl):
    body = {'ean': ean, 'reference': reference[:100], 'onHoldByRetailer': False,
            'condition': {'category': 'NEW'},
            'pricing': {'bundlePrices': [{'quantity': 1, 'unitPrice': round(price, 2)}]},
            'stock': {'amount': int(stock), 'managedByRetailer': False}}
    if tpl:
        if tpl.get('f'):
            body['fulfilment'] = {k: v for k, v in tpl['f'].items() if v is not None}
        if tpl.get('c'):
            body['countryAvailabilities'] = tpl['c']
    else:
        body['fulfilment'] = {'method': 'FBR'}
    return call('POST', '/retailer/offers', body)


MT10 = 'application/vnd.retailer.v10+json'


def call10(method, path, body=None, tries=6, lang=None):
    """Retailer API v10 (content). Accept-Language voor catalogusvragen."""
    for i in range(tries):
        h = {'Authorization': 'Bearer ' + token(), 'Accept': MT10}
        if body is not None:
            h['Content-Type'] = MT10
        if lang:
            h['Accept-Language'] = lang
        r = requests.request(method, BASE + path, json=body, headers=h, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(float(r.headers.get('Retry-After') or 0) or 1.5 * 2 ** i)
            continue
        if r.status_code == 401 and i == 0:
            _tok['t'] = None
            continue
        if r.status_code == 404:
            return None
        if not r.ok:
            raise BolError(f'{method} {path}: HTTP {r.status_code} {r.text[:600]}', r.status_code, r.text)
        return r.json() if r.text else None
    raise BolError(f'{method} {path}: opgegeven na {tries} pogingen')


def catalog_product(ean, lang='nl'):
    """Huidige bol-content van een EAN (None als bol hem niet kent)."""
    return call10('GET', f'/retailer/content/catalog-products/{ean}', lang=lang)


def post_content(body):
    """Productcontent indienen (asynchroon). Geeft process-status terug (processStatusId)."""
    return call10('POST', '/retailer/content/products', body)


def process_status(pid):
    return call10('GET', f'/shared/process-status/{pid}')


def upload_report(upload_id):
    return call10('GET', f'/retailer/content/upload-report/{upload_id}')


def chunk_recommendations(titel, omschrijving=''):
    return call10('POST', '/retailer/content/chunk-recommendations',
                  {'productContents': [{'attributes': [{'id': 'Name', 'values': [{'value': titel}]},
                                                       {'id': 'Description', 'values': [{'value': omschrijving[:4000]}]}]}]})


def not_for_sale_reasons(offer_id):
    return call('GET', f'/retailer/offers/{offer_id}/not-for-sale-reasons')
