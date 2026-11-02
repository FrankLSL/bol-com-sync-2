"""Loungesetland-logo als tabblad-/bladwijzer-/app-icoon — gedeeld bestand (zelfde kopie in portal, dashboard, planner en facturen).
Bestanden staan in static/: favicon.svg, favicon-32.png, favicon.ico, apple-touch-icon.png, icon-192.png, icon-512.png."""
import json
from pathlib import Path

LINKS = ('<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">'
         '<link rel="icon" href="/static/favicon-32.png" sizes="32x32" type="image/png">'
         '<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">')
PATHS = ('/favicon.ico', '/apple-touch-icon.png', '/apple-touch-icon-precomposed.png', '/manifest.webmanifest')


def install(app, static_dir, name, short_name, manifest=True):
    """Routes voor /favicon.ico en /apple-touch-icon.png (browsers en iPhone vragen die zelf op) en een web-manifest."""
    from fastapi.responses import FileResponse, Response
    static_dir = Path(static_dir)
    cache = {'Cache-Control': 'public, max-age=86400'}

    @app.get('/favicon.ico', include_in_schema=False)
    def _favicon():
        return FileResponse(static_dir / 'favicon.ico', media_type='image/x-icon', headers=cache)

    @app.get('/apple-touch-icon.png', include_in_schema=False)
    @app.get('/apple-touch-icon-precomposed.png', include_in_schema=False)
    def _touch():
        return FileResponse(static_dir / 'apple-touch-icon.png', media_type='image/png', headers=cache)

    if manifest:
        m = {'name': name, 'short_name': short_name, 'start_url': '/', 'scope': '/', 'display': 'standalone',
             'background_color': '#f6f3ee', 'theme_color': '#1f4b47', 'lang': 'nl',
             'icons': [{'src': '/static/icon-192.png', 'sizes': '192x192', 'type': 'image/png'},
                       {'src': '/static/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any maskable'}]}

        @app.get('/manifest.webmanifest', include_in_schema=False)
        def _manifest():
            return Response(json.dumps(m), media_type='application/manifest+json')
