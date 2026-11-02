"""Shopify Admin API (alleen lezen). Zelfde app/variabelen als dashboard en planner:
  SHOPIFY_SHOP + SHOPIFY_CLIENT_ID/SHOPIFY_CLIENT_SECRET (client credentials) — of — SHOPIFY_ACCESS_TOKEN.
Nodig: read_products."""
import json, os, threading, time

import requests

API_VERSION = os.getenv('SHOPIFY_API_VERSION', '2026-07')
_tok = {'t': None, 'x': 0}
_lock = threading.Lock()


def shop():
    return os.getenv('SHOPIFY_SHOP', '').strip()


def configured():
    return bool(shop() and (os.getenv('SHOPIFY_ACCESS_TOKEN') or (os.getenv('SHOPIFY_CLIENT_ID') and os.getenv('SHOPIFY_CLIENT_SECRET'))))


def token():
    static = os.getenv('SHOPIFY_ACCESS_TOKEN', '').strip()
    if static:
        return static
    if _tok['t'] and time.time() < _tok['x']:
        return _tok['t']
    r = requests.post(f'https://{shop()}/admin/oauth/access_token', timeout=60, data={
        'grant_type': 'client_credentials', 'client_id': os.getenv('SHOPIFY_CLIENT_ID', '').strip(),
        'client_secret': os.getenv('SHOPIFY_CLIENT_SECRET', '').strip()})
    if not r.ok:
        raise RuntimeError(f'Shopify-token mislukt (HTTP {r.status_code}): {r.text[:200]}')
    j = r.json()
    _tok['t'], _tok['x'] = j['access_token'], time.time() + int(j.get('expires_in', 86399)) - 300
    return _tok['t']


def graphql(query, variables=None):
    with _lock:
        for attempt in range(8):
            r = requests.post(f'https://{shop()}/admin/api/{API_VERSION}/graphql.json', timeout=90,
                              headers={'X-Shopify-Access-Token': token(), 'Content-Type': 'application/json'},
                              json={'query': query, 'variables': variables or {}})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(4 * (attempt + 1))
                continue
            if not r.ok:
                raise RuntimeError(f'Shopify HTTP {r.status_code}: {r.text[:300]}')
            j = r.json()
            if j.get('errors') and 'THROTTLED' in json.dumps(j['errors']) and attempt < 7:
                time.sleep(4 * (attempt + 1))
                continue
            if j.get('errors'):
                raise RuntimeError('Shopify: ' + json.dumps(j['errors'])[:400])
            return j['data']
    raise RuntimeError('Shopify: te vaak afgeremd')


PQ = '''query($c:String){ products(first:50, after:$c, query:"status:active OR status:draft"){ pageInfo{ hasNextPage endCursor }
  nodes{ id title handle status vendor productType onlineStoreUrl featuredImage{ url }
         variants(first:30){ nodes{ id title sku price compareAtPrice } } } } }'''


def products():
    """Alle actieve + concept-producten met varianten. 'live' = actief én zichtbaar in de webwinkel."""
    out, cur = [], None
    while True:
        d = graphql(PQ, {'c': cur})['products']
        for p in d['nodes']:
            live = p['status'] == 'ACTIVE' and bool(p.get('onlineStoreUrl'))
            for v in p['variants']['nodes']:
                out.append({'product_id': p['id'], 'variant_id': v['id'], 'titel': p['title'] + ('' if v['title'] in ('Default Title', '') else f" ({v['title']})"),
                            'handle': p['handle'], 'status': p['status'], 'live': live, 'merk': p.get('vendor') or '',
                            'type': p.get('productType') or '', 'foto': (p.get('featuredImage') or {}).get('url') or '',
                            'url': p.get('onlineStoreUrl') or '', 'sku': (v.get('sku') or '').strip(),
                            'prijs': float(v['price']) if v.get('price') else 0.0})
        if not d['pageInfo']['hasNextPage']:
            break
        cur = d['pageInfo']['endCursor']
    return out
