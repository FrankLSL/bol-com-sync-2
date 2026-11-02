"""Loungesetland-login (SSO) — gedeeld bestand.

Dit bestand staat in de portal én (als kopie) in elke app (planner, dashboard, facturen, ...).
Pas je hier iets aan, kopieer het dan naar alle apps.

Hoe het werkt
- De portal (loungesetland-webportal.com) logt je in en zet de cookie `lsl_sso` op het hele domein
  (.loungesetland-webportal.com). Daarin staan je gebruikersnaam, naam en je rol per app,
  ondertekend met SSO_SECRET.
- Een app leest die cookie met `user(request)` en `role(request, 'planner')`. Klopt de handtekening
  en is hij niet verlopen, dan ben je ingelogd met die rol.
- De cookie is 12 uur geldig. Is hij verlopen, dan stuurt de app je naar de portal; heb je daar nog
  een sessie, dan krijg je meteen een nieuwe en ga je zonder inloggen terug. Zo werkt intrekken van
  rechten of uitschakelen van een gebruiker binnen 12 uur door in alle apps.

Railway-variabelen (in de portal én in elke app gelijk):
  SSO_SECRET     lange willekeurige tekst (zelfde waarde overal)
  PORTAL_URL     https://loungesetland-webportal.com
  COOKIE_DOMAIN  .loungesetland-webportal.com   (alleen nodig in de portal)
Zonder SSO_SECRET doet dit bestand niets en werkt de app zoals voorheen.

Apps buiten het portaldomein (bv. tijdelijk op *.up.railway.app): de portal stuurt dan een kortlevende,
ondertekende sleutel mee in de link (?lsl_t=...). De app wisselt die met `handoff(request)` in voor een
eigen lsl_sso-cookie op zijn eigen adres. Zo blijft het één keer inloggen.
"""
import base64, hashlib, hmac, json, os, time
from urllib.parse import quote, urlparse, urlencode, parse_qsl, urlunparse

COOKIE = 'lsl_sso'
HOURS = 12


def enabled():
    return bool(os.getenv('SSO_SECRET'))


def portal_url():
    return (os.getenv('PORTAL_URL') or '').rstrip('/')


def _key():
    return hashlib.sha256(('lsl-sso:' + os.getenv('SSO_SECRET', '')).encode()).digest()


HANDOFF = 'lsl_t'


def make(username, name, apps, admin=False, hours=HOURS, seconds=None):
    ttl = seconds if seconds is not None else hours * 3600
    body = base64.urlsafe_b64encode(json.dumps(
        {'u': username, 'n': name or username, 'apps': apps or {}, 'a': bool(admin), 'x': int(time.time()) + ttl},
        separators=(',', ':')).encode()).decode()
    return body + '.' + hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()


def read(token):
    if not enabled() or not token:
        return None
    try:
        body, sig = token.rsplit('.', 1)
        if not hmac.compare_digest(sig, hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()):
            return None
        d = json.loads(base64.urlsafe_b64decode(body.encode()))
        return d if d.get('x', 0) > time.time() else None
    except Exception:
        return None


def user(request):
    """{'u','n','apps','a','x'} of None."""
    return read(request.cookies.get(COOKIE, ''))


def role(request, app):
    d = user(request)
    return (d or {}).get('apps', {}).get(app) or None


def current_url(request):
    proto = request.headers.get('x-forwarded-proto', request.url.scheme)
    host = request.headers.get('x-forwarded-host') or request.headers.get('host') or request.url.netloc
    q = ('?' + request.url.query) if request.url.query else ''
    return f'{proto}://{host}{request.url.path}{q}'


def login_url(request=None):
    nxt = current_url(request) if request is not None else ''
    return portal_url() + '/login' + (('?next=' + quote(nxt, safe='')) if nxt else '')


def logout_url():
    return portal_url() + '/logout'


SHELL_LOADER = """(function(){var p=%s;if(!p)return;var s=document.createElement('script');s.src=p+'/static/shell.js';s.defer=true;document.head.appendChild(s);})();"""


def shell_loader_js():
    """JS die de vaste Loungesetland-balk van de portal laadt (leeg als SSO uit staat)."""
    return SHELL_LOADER % json.dumps(portal_url() if enabled() else '')


def _on_cookie_domain(host):
    d = (os.getenv('COOKIE_DOMAIN') or '').lstrip('.').lower()
    host = (host or '').lower()
    return bool(d) and (host == d or host.endswith('.' + d))


def handoff_url(url, username, name, apps, admin=False):
    """Portal: link naar een app buiten het portaldomein krijgt een sleutel van 2 minuten mee."""
    if not enabled() or not url:
        return url
    p = urlparse(url)
    if not p.hostname or _on_cookie_domain(p.hostname):
        return url
    q = [(k, v) for k, v in parse_qsl(p.query) if k != HANDOFF] + [(HANDOFF, make(username, name, apps, admin, seconds=120))]
    return urlunparse(p._replace(query=urlencode(q)))


def handoff(request):
    """App: ?lsl_t=... in de link -> eigen lsl_sso-cookie zetten en door naar de schone link. Anders None."""
    tok = request.query_params.get(HANDOFF)
    if not tok:
        return None
    from fastapi.responses import RedirectResponse
    q = [(k, v) for k, v in request.query_params.multi_items() if k != HANDOFF]
    clean = '/' + request.url.path.lstrip('/\\') + (('?' + urlencode(q)) if q else '')  # altijd een pad op deze site, nooit //ander-domein
    resp = RedirectResponse(clean, status_code=303)
    d = read(tok)
    if d:
        secure = request.headers.get('x-forwarded-proto', request.url.scheme) == 'https'
        resp.set_cookie(COOKIE, make(d['u'], d.get('n'), d.get('apps'), d.get('a')), max_age=HOURS * 3600,
                        httponly=True, samesite='lax', secure=secure)
    return resp


# ---------- inlogbeveiliging (gedeeld door alle apps) ----------
def online():
    """Draait op Railway (dan nooit zonder login)."""
    return bool(os.getenv('RAILWAY_ENVIRONMENT') or os.getenv('RAILWAY_PROJECT_ID'))


def noodlogin():
    """NOODLOGIN=1 in Railway: de eigen login van een app tijdelijk weer aan (bijv. als de portal plat ligt)."""
    return os.getenv('NOODLOGIN') == '1'


def own_login_allowed():
    """Eigen login van een app (naast de portal) alleen als de portal-login uit staat, of in nood."""
    return not (enabled() and portal_url()) or noodlogin()


# Cloudflare-adressen (www.cloudflare.com/ips). Komt een verzoek via Cloudflare, dan staat de echte bezoeker in CF-Connecting-IP.
CLOUDFLARE_NETS = ('173.245.48.0/20', '103.21.244.0/22', '103.22.200.0/22', '103.31.4.0/22', '141.101.64.0/18', '108.162.192.0/18',
                   '190.93.240.0/20', '188.114.96.0/20', '197.234.240.0/22', '198.41.128.0/17', '162.158.0.0/15', '104.16.0.0/13',
                   '104.24.0.0/14', '172.64.0.0/13', '131.0.72.0/22', '2400:cb00::/32', '2606:4700::/32', '2803:f800::/32',
                   '2405:b500::/32', '2405:8100::/32', '2a06:98c0::/29', '2c0f:f248::/32')


def _from_cloudflare(ip):
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
        return any(a in ipaddress.ip_network(n) for n in CLOUDFLARE_NETS)
    except ValueError:
        return False


def client_ip(request):
    """IP van de bezoeker. Railway overschrijft X-Real-IP en X-Forwarded-For aan de rand; we nemen X-Real-IP,
    anders het laatste adres in X-Forwarded-For (dat zet de proxy zelf, het eerste kan de bezoeker verzinnen).
    Is dat adres van Cloudflare, dan nemen we CF-Connecting-IP (alleen dan: anders zou iemand die header kunnen verzinnen)."""
    ip = (request.headers.get('x-real-ip') or '').strip()
    if not ip:
        parts = [p.strip() for p in (request.headers.get('x-forwarded-for') or '').split(',') if p.strip()]
        ip = parts[-1] if parts else ''
    ip = ip or (request.client.host if getattr(request, 'client', None) else '')
    cf = (request.headers.get('cf-connecting-ip') or '').strip()
    return cf if cf and _from_cloudflare(ip) else ip


class Throttle:
    """Te vaak fout inloggen: hooguit PER_USER fouten per gebruiker en PER_IP per IP-adres binnen 15 minuten.
    Staat in een eigen SQLite-bestand op het volume, dus het telt door na een herstart."""

    def __init__(self, path, per_user=5, per_ip=20, window=900):
        import sqlite3, threading
        self._sqlite, self.path, self.per_user, self.per_ip, self.window = sqlite3, str(path), per_user, per_ip, window
        self._lock = threading.Lock()
        with self._db() as c:
            c.execute('CREATE TABLE IF NOT EXISTS fails (k TEXT, at REAL)')
            c.execute('CREATE INDEX IF NOT EXISTS fails_k ON fails (k, at)')

    def _db(self):
        return self._sqlite.connect(self.path, timeout=10)

    def _count(self, c, k):
        return c.execute('SELECT COUNT(*) FROM fails WHERE k=? AND at>?', (k, time.time() - self.window)).fetchone()[0]

    def blocked(self, ip='', user=''):
        with self._lock, self._db() as c:
            return bool((user and self._count(c, 'u:' + user.lower()) >= self.per_user) or (ip and self._count(c, 'ip:' + ip) >= self.per_ip))

    def fail(self, ip='', user=''):
        with self._lock, self._db() as c:
            c.executemany('INSERT INTO fails (k, at) VALUES (?,?)', [(k, time.time()) for k in (('ip:' + ip) if ip else '', ('u:' + user.lower()) if user else '') if k])
            c.execute('DELETE FROM fails WHERE at<?', (time.time() - 86400,))

    def ok(self, user=''):
        if user:
            with self._lock, self._db() as c:
                c.execute('DELETE FROM fails WHERE k=?', ('u:' + user.lower(),))


BLOCKED_MSG = 'Te vaak fout geprobeerd. Wacht een kwartier en probeer het dan opnieuw.'


# ---------- koppelingen tussen de apps en bescherming tegen verzoeken vanaf een andere site ----------
def intern_sig(scope):
    sec = os.getenv('SSO_SECRET', '')
    return hmac.new(sec.encode(), f'lsl-intern:{scope}'.encode(), hashlib.sha256).hexdigest() if len(sec) >= 16 else ''


def intern_ok(request, scope):
    want = intern_sig(scope)
    return bool(want) and hmac.compare_digest(want, request.headers.get('x-lsl-intern', ''))


SAFE_METHODS = ('GET', 'HEAD', 'OPTIONS')


def same_origin(request):
    """Wijzigingen (POST/PUT/PATCH/DELETE) vanuit een browser alleen vanaf dezelfde app. Browsers sturen bij zo'n verzoek
    Origin (of Referer) mee; komt die van een ander adres, ook een andere Loungesetland-app, dan weigeren we.
    Verzoeken zonder Origin en Referer (koppelingen tussen apps, scripts) laten we door; die hebben hun eigen sleutel of sessie."""
    if request.method in SAFE_METHODS:
        return True
    src = request.headers.get('origin') or request.headers.get('referer') or ''
    if not src:
        return True
    if src == 'null':
        return False
    host = (request.headers.get('x-forwarded-host') or request.headers.get('host') or '').split(':')[0].lower()
    return (urlparse(src).hostname or '').lower() == host


def csrf_middleware(app, exempt=()):
    """Zet de herkomstcontrole aan voor een FastAPI-app. exempt: padvoorvoegsels die bewust van buiten komen."""
    from fastapi.responses import JSONResponse

    @app.middleware('http')
    async def _lsl_same_origin(request, call_next):
        if not same_origin(request) and not request.url.path.startswith(tuple(exempt)):
            return JSONResponse({'detail': 'Geweigerd: dit verzoek komt niet van deze pagina.'}, status_code=403)
        return await call_next(request)


# ---------- snelheid: elke app houdt bij hoe lang verzoeken duren (voor de bewaking in de portal) ----------
import collections as _collections, threading as _threading
_SPEED = _collections.deque(maxlen=50000)  # (tijd, ms, pad)
_SPEED_LOCK = _threading.Lock()
_SPEED_SKIP = ('/healthz', '/static/', '/favicon', '/apple-touch-icon', '/manifest', '/sw.js', '/sso/shell.js')


def speed_middleware(app):
    """Meet de duur van elk verzoek (zonder statische bestanden en /healthz)."""
    @app.middleware('http')
    async def _lsl_speed(request, call_next):
        t = time.time()
        try:
            return await call_next(request)
        finally:
            p = request.url.path
            if not p.startswith(_SPEED_SKIP):
                with _SPEED_LOCK:
                    _SPEED.append((t, int((time.time() - t) * 1000), p))


def _pad_groep(p):
    import re as _re
    return _re.sub(r'/\d+', '/{id}', p)[:60]


def speed_stats(slow_ms=5000):
    """Laatste 10 en 60 minuten: aantal verzoeken, mediaan, 95%-grens, aantal trage (> slow_ms) en de traagste pagina's."""
    now = time.time()
    with _SPEED_LOCK:
        rows = [r for r in _SPEED if now - r[0] <= 3600]
    out = {}
    for label, sec in (('10m', 600), ('60m', 3600)):
        ms = sorted(r[1] for r in rows if now - r[0] <= sec)
        slow = [r for r in rows if now - r[0] <= sec and r[1] > slow_ms]
        worst = _collections.Counter(_pad_groep(r[2]) for r in slow).most_common(3)
        out[label] = {'n': len(ms), 'p50': ms[len(ms) // 2] if ms else None, 'p95': ms[int(len(ms) * 0.95)] if ms else None,
                      'traag': len(slow), 'max': ms[-1] if ms else None, 'traagste': [f'{p} ({n}×)' for p, n in worst]}
    return out
