"""bol-orders → voorstel → (na goedkeuring) Shopify-order.

- Ophalen: Retailer API v10 /retailer/orders (FBR, open) elke 10 minuten + op verzoek; details per order.
- Voorstel: regels gekoppeld via offer-reference = Shopify-SKU (anders via EAN-tabel), prijs = bol-verkoopprijs,
  klant/adres uit bol, commissie per regel.
- Goedkeuren: draft order met bol-prijzen (priceOverride) → afronden als betaald (bol int het geld).
  Tags 'bol.com' + 'bol-<ordernr>', bron 'bol.com', PO-nummer = bol-ordernummer, commissie en netto in de notitie
  en als extra velden. Geen e-mail op de order (het bol-adres is een doorstuuradres; Shopify mailt de klant dus niet).
"""
import datetime, json, threading, time

import bol, shopify, store

DAGEN = ['Maandag', 'Dinsdag', 'Woensdag', 'Donderdag', 'Vrijdag', 'Zaterdag', 'Zondag']
MAANDEN = ['Januari', 'Februari', 'Maart', 'April', 'Mei', 'Juni', 'Juli', 'Augustus', 'September', 'Oktober', 'November', 'December']


def init():
    c = store.conn()
    c.executescript('''CREATE TABLE IF NOT EXISTS bolorder(
      order_id TEXT PRIMARY KEY, geplaatst TEXT, data TEXT, status TEXT NOT NULL DEFAULT 'nieuw',
      shopify_id TEXT, shopify_naam TEXT, door TEXT, t TEXT, reden TEXT, totaal REAL, commissie REAL, opgehaald TEXT);''')
    c.commit()
    c.close()


# ---------- bol ----------
def fetch(status='OPEN'):
    """Nieuwe/gewijzigde FBR-orders ophalen en details opslaan. Geeft aantal nieuwe."""
    nieuw, page = 0, 1
    known = {r['order_id'] for r in store.q('SELECT order_id FROM bolorder')}
    while page < 50:
        j = bol.call10('GET', f'/retailer/orders?fulfilment-method=FBR&status={status}&page={page}') or {}
        lst = j.get('orders') or []
        for o in lst:
            oid = str(o.get('orderId'))
            if oid in known:
                continue
            d = bol.call10('GET', f'/retailer/orders/{oid}') or o
            tot = sum(float(i.get('unitPrice') or 0) * int(i.get('quantity') or 0) for i in d.get('orderItems', []))
            com = sum(float(i.get('commission') or 0) for i in d.get('orderItems', []))
            store.x('INSERT OR IGNORE INTO bolorder(order_id,geplaatst,data,status,totaal,commissie,opgehaald) VALUES(?,?,?,?,?,?,?)',
                    (oid, (d.get('orderPlacedDateTime') or '')[:19].replace('T', ' '), json.dumps(d, ensure_ascii=False), 'nieuw', tot, com, store.now()))
            known.add(oid)
            nieuw += 1
            time.sleep(0.3)
        if len(lst) < 50:
            break
        page += 1
    store.kv_set('orders_opgehaald', store.now())
    return nieuw


def refresh_detail(oid):
    d = bol.call10('GET', f'/retailer/orders/{oid}')
    if d:
        store.x('UPDATE bolorder SET data=? WHERE order_id=?', (json.dumps(d, ensure_ascii=False), oid))
    return d


def get(oid):
    r = store.q('SELECT * FROM bolorder WHERE order_id=?', (oid,))
    if not r:
        return None
    r = r[0]
    r['d'] = json.loads(r['data'] or '{}')
    return r


def lijst(status=None, n=300):
    if status and status != 'alles':
        return store.q('SELECT order_id,geplaatst,status,shopify_naam,totaal,commissie,door,t,reden,data FROM bolorder WHERE status=? ORDER BY geplaatst DESC LIMIT ?', (status, n))
    return store.q('SELECT order_id,geplaatst,status,shopify_naam,totaal,commissie,door,t,reden,data FROM bolorder ORDER BY geplaatst DESC LIMIT ?', (n,))


def counts():
    return {r['status']: r['n'] for r in store.q('SELECT status, COUNT(*) n FROM bolorder GROUP BY status')}


# ---------- voorstel ----------
def _variant_for(sku, ean, shop_rows):
    by_sku = {}
    for v in shop_rows:
        if v['sku']:
            by_sku.setdefault(v['sku'], []).append(v)
    if sku and by_sku.get(sku):
        m = by_sku[sku]
        return sorted(m, key=lambda v: (not v['live'], v['status'] != 'ACTIVE'))[0], ('' if len(m) == 1 else f'SKU staat {len(m)}x in Shopify')
    e = store.q('SELECT sku FROM ean WHERE ean=?', (store.norm_ean(ean),))
    if e and e[0]['sku'] and by_sku.get(e[0]['sku']):
        return by_sku[e[0]['sku']][0], 'gekoppeld via EAN-lijst'
    return None, 'geen Shopify-product gevonden voor deze SKU/EAN'


def proposal(row, shop_rows, overrides=None):
    """overrides: {orderItemId: sku} (handmatig gekozen)."""
    d, ov = row['d'], overrides or {}
    s = d.get('shipmentDetails') or {}
    b = d.get('billingDetails') or s
    regels = []
    for it in d.get('orderItems', []):
        iid = str(it.get('orderItemId'))
        ref = ((it.get('offer') or {}).get('reference') or '').strip()
        ean = (it.get('product') or {}).get('ean') or it.get('ean') or ''
        sku = ov.get(iid) or ref
        v, waarsch = _variant_for(sku, ean, shop_rows)
        q = int(it.get('quantity') or 0) - int(it.get('quantityCancelled') or 0)
        f = it.get('fulfilment') or {}
        regels.append({'id': iid, 'titel': (it.get('product') or {}).get('title') or '', 'ean': ean, 'ref': ref, 'sku': v['sku'] if v else sku,
                       'variant': v, 'aantal': q, 'prijs': float(it.get('unitPrice') or 0), 'commissie': float(it.get('commission') or 0),
                       'site': v['prijs'] if v else None, 'annulering': bool(it.get('cancellationRequest')), 'waarsch': waarsch,
                       'uiterlijk': (f.get('latestDeliveryDate') or '')[:10], 'exact': (f.get('exactDeliveryDate') or '')[:10]})
    totaal = round(sum(r['prijs'] * r['aantal'] for r in regels), 2)
    com = round(sum(r['commissie'] for r in regels if r['aantal'] > 0), 2)
    straat = ' '.join(x for x in [s.get('streetName'), str(s.get('houseNumber') or ''), s.get('houseNumberExtension') or ''] if x).strip()
    klant = {'voornaam': s.get('firstName') or '', 'achternaam': s.get('surname') or '', 'bedrijf': s.get('company') or '',
             'adres': straat, 'adres2': s.get('extraAddressInformation') or '', 'postcode': s.get('zipCode') or '', 'plaats': s.get('city') or '',
             'land': (s.get('countryCode') or 'NL').upper(), 'telefoon': s.get('deliveryPhoneNumber') or '', 'email': s.get('email') or '',
             'factuur_naam': ' '.join(x for x in [b.get('firstName'), b.get('surname')] if x), 'btw': b.get('vatNumber') or '',
             'afhaalpunt': bool(d.get('pickupPoint'))}
    problemen = [f"{r['titel'] or r['ean']}: {r['waarsch']}" for r in regels if not r['variant']]
    problemen += [f"Klant vraagt annulering: {r['titel']}" for r in regels if r['annulering']]
    if klant['afhaalpunt']:
        problemen.append('Levering naar een afhaalpunt (kan niet met eigen bezorging)')
    return {'regels': regels, 'klant': klant, 'totaal': totaal, 'commissie': com, 'netto': round(totaal - com, 2), 'problemen': problemen,
            'uiterlijk': min([r['uiterlijk'] for r in regels if r['uiterlijk']] or ['']), 'exact': next((r['exact'] for r in regels if r['exact']), '')}


def _phone(p, land='NL'):
    import re
    dg = re.sub(r'[^\d+]', '', p or '')
    if not dg:
        return ''
    if dg.startswith('00'):
        dg = '+' + dg[2:]
    elif dg.startswith('0'):
        dg = {'BE': '+32', 'DE': '+49'}.get(land, '+31') + dg[1:]
    elif not dg.startswith('+'):
        dg = '+31' + dg
    return dg if 10 <= len(dg) <= 16 else ''


CREATE_M = '''mutation($input:DraftOrderInput!){ draftOrderCreate(input:$input){ draftOrder{ id name totalPriceSet{ shopMoney{ amount } } } userErrors{ field message } } }'''
COMPLETE_M = '''mutation($id:ID!){ draftOrderComplete(id:$id, paymentPending:false){ draftOrder{ id order{ id name } } userErrors{ field message } } }'''
DELETE_M = '''mutation($id:ID!){ draftOrderDelete(input:{id:$id}){ deletedId userErrors{ field message } } }'''
FIND_Q = '''query($q:String!){ orders(first:5, query:$q){ nodes{ id name } } }'''


def approve(oid, prop, door):
    row = get(oid)
    if not row or row['status'] == 'goedgekeurd':
        raise RuntimeError('Order is al verwerkt')
    # dubbele orders voorkomen: staat hij al in Shopify?
    hit = (shopify.graphql(FIND_Q, {'q': f'tag:"bol-{oid}"'})['orders']['nodes'] or [])
    if hit:
        store.x("UPDATE bolorder SET status='goedgekeurd', shopify_id=?, shopify_naam=?, door=?, t=? WHERE order_id=?", (hit[0]['id'], hit[0]['name'], door, store.now(), oid))
        return hit[0]
    regels = [r for r in prop['regels'] if r['aantal'] > 0]
    if not regels or any(not r['variant'] for r in regels):
        raise RuntimeError('Niet alle regels zijn aan een Shopify-product gekoppeld')
    k = prop['klant']
    tel = _phone(k['telefoon'], k['land'])
    adr = {'firstName': k['voornaam'] or None, 'lastName': k['achternaam'] or k['voornaam'] or None, 'company': k['bedrijf'] or None,
           'address1': k['adres'] or None, 'address2': k['adres2'] or None, 'zip': k['postcode'] or None, 'city': k['plaats'] or None,
           'countryCode': k['land'], 'phone': tel or None}
    adr = {a: b for a, b in adr.items() if b}
    tags = ['bol.com', f'bol-{oid}']
    attrs = [{'key': 'bol-ordernummer', 'value': oid}, {'key': 'bol-omzet', 'value': f"{prop['totaal']:.2f}"},
             {'key': 'bol-commissie', 'value': f"{prop['commissie']:.2f}"}, {'key': 'bol-netto', 'value': f"{prop['netto']:.2f}"}]
    if prop['exact']:
        dd = datetime.date.fromisoformat(prop['exact'])
        tags.append(f'{DAGEN[dd.weekday()]} {dd.day} {MAANDEN[dd.month - 1]} {dd.year}')
        attrs.append({'key': 'Leverdatum', 'value': prop['exact']})
    if prop['uiterlijk']:
        attrs.append({'key': 'Uiterlijk leveren (bol)', 'value': prop['uiterlijk']})
    note = (f"bol.com-order {oid}\nOmzet € {prop['totaal']:.2f} · commissie bol € {prop['commissie']:.2f} · netto € {prop['netto']:.2f}"
            + (f"\nUiterlijk leveren: {prop['uiterlijk']}" if prop['uiterlijk'] else '') + (f"\nVaste leverdatum: {prop['exact']}" if prop['exact'] else '')
            + (f"\nTelefoon (bol): {k['telefoon']}" if k['telefoon'] and not tel else '')
            + (f"\nbol-mailadres klant (doorstuur): {k['email']}" if k['email'] else '')
            + (f"\nBTW-nummer: {k['btw']}" if k['btw'] else ''))
    inp = {'lineItems': [{'variantId': r['variant']['variant_id'], 'quantity': r['aantal'],
                          'priceOverride': {'amount': f"{r['prijs']:.2f}", 'currencyCode': 'EUR'},
                          'customAttributes': [{'key': 'bol-commissie', 'value': f"{r['commissie']:.2f}"}]} for r in regels],
           'shippingAddress': adr, 'billingAddress': dict(adr), 'tags': tags, 'note': note[:5000], 'customAttributes': attrs,
           'poNumber': oid}
    if tel:
        inp['phone'] = tel
    r = shopify.graphql(CREATE_M, {'input': inp})['draftOrderCreate']
    if r.get('userErrors'):
        raise RuntimeError('Shopify: ' + '; '.join(e.get('message', '') for e in r['userErrors'])[:400])
    did = r['draftOrder']['id']
    c = shopify.graphql(COMPLETE_M, {'id': did})['draftOrderComplete']
    if c.get('userErrors') or not ((c.get('draftOrder') or {}).get('order')):
        try:
            shopify.graphql(DELETE_M, {'id': did})
        except Exception:
            pass
        raise RuntimeError('Shopify kon de order niet afronden: ' + '; '.join(e.get('message', '') for e in c.get('userErrors') or [])[:300])
    o = c['draftOrder']['order']
    store.x("UPDATE bolorder SET status='goedgekeurd', shopify_id=?, shopify_naam=?, door=?, t=?, totaal=?, commissie=? WHERE order_id=?",
            (o['id'], o['name'], door, store.now(), prop['totaal'], prop['commissie'], oid))
    store.log(door, 'bol-order → Shopify', ','.join(r['sku'] for r in regels), '', True, json.dumps({'bol': oid, 'shopify': o['name'], 'omzet': prop['totaal'], 'commissie': prop['commissie']}))
    return o


def reject(oid, reden, door):
    store.x("UPDATE bolorder SET status='afgekeurd', reden=?, door=?, t=? WHERE order_id=?", (reden[:500], door, store.now(), oid))
    store.log(door, 'bol-order afgekeurd', '', '', True, json.dumps({'bol': oid, 'reden': reden}))


def reopen(oid, door):
    store.x("UPDATE bolorder SET status='nieuw', reden=NULL, door=?, t=? WHERE order_id=? AND status='afgekeurd'", (door, store.now(), oid))


# ---------- achtergrond ----------
def start_poller():
    def loop():
        time.sleep(60)
        while True:
            try:
                if bol.configured():
                    fetch()
            except Exception as e:
                store.kv_set('orders_fout', f'{store.now()} {str(e)[:300]}')
                print('orders:', e, flush=True)
            time.sleep(600)
    threading.Thread(target=loop, daemon=True).start()
