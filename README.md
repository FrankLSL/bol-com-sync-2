# Loungesetland bol.com

Tool in de Loungesetland-omgeving (tegel **bol.com** in de portal) voor:

- **Prijs-sync** — bol-prijs = siteprijs × `MARKUP` (1,10), vanaf €100 naar boven afgerond op …9, daaronder op ,95.
  Koppeling: *reference* op bol = *SKU* in Shopify. Alleen de prijs wordt aangepast. Draait automatisch elke maandag 06:15 (NL)
  en op verzoek (proefrun of doorvoeren). Elke run met alle regels terug te zien en als csv te downloaden.
- **Producten** — alle Shopify-producten met hun bol-status; *Zet op bol* maakt het aanbod aan met de eerstvolgende vrije EAN,
  prijs site +10% en dezelfde levering/landen als je bestaande aanbod.
- **Productinfo** — per product links wat er nu op bol staat, rechts het voorstel uit Shopify (titel, beschrijving, kleur, afmetingen, foto's);
  versturen via de Product Content API v10. Rapport per veld (overgenomen/geweigerd) na ±1 uur. Nieuwe producten krijgen bij *Zet op bol*
  automatisch productinfo mee als de productgroep (GPC) bekend is van vergelijkbare producten.
- **EAN-codes** — eigen voorraad EAN's (startlijst `seed/eans.csv`), plus alles wat al op bol staat; een code wordt nooit dubbel uitgegeven.
  Nieuwe codes toevoegen via Excel/csv-upload.

## Railway
Service in hetzelfde project als het dashboard, volume op `/data`, domein `bol.loungesetland-webportal.com`.

| Variabele | Waarde |
|---|---|
| `BOL_CLIENT_ID`, `BOL_CLIENT_SECRET` | bol-partnerplatform → API-instellingen (Retailer API) |
| `SHOPIFY_SHOP`, `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` | zelfde als dashboard (referentie) — nodig: `read_products` |
| `SSO_SECRET`, `PORTAL_URL` | zelfde als portal/dashboard (referentie) |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | back-up (gedeeld) |
| `MARKUP` | `1.10` |
| `DRY_RUN` | `false` (= wekelijkse run voert echt door) |
| `MAX_CHANGE` | `0.6` — prijssprongen groter dan 60% worden overgeslagen |
| `FORCE_REFS` / `SKIP_REFS` | komma-gescheiden SKU's: sprongbeveiliging negeren / nooit aanpassen |
| `SYNC_DAG`, `SYNC_TIJD` | `0` (maandag), `06:15` |
| `BOL_STOCK` | standaardvoorraad bij aanmaken (10) |

Portal: regel `bol` in `APPS` (`env: BOL_URL`) en `BOL_URL` in de portal-service.

## Logregels
Elke sync schrijft per offer `RAPPORT;tijd;status;sku;ean;site;bol_oud;bol_nieuw;reden` en een slotregel `KLAAR …` naar de log
(voor het wekelijkse rapport).

Lokaal: `pip install -r requirements.txt && NO_SCHEDULER=1 BACKUP_UIT=1 uvicorn app:app --reload`.
