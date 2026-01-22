# scrape_ebay.py
import re
import time
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from db import init_db, upsert_listing
from typing import Optional, List, Dict, Tuple
import os  # neu
import json
import re
from bs4 import BeautifulSoup
from typing import Any, Dict, List, Optional


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_kleinanzeigen_viewad_html(html: str) -> Dict[str, Any]:
    """
    Extrahiert Bilder, Titel, Preis, Ort, Datum, Details, Ausstattung, Beschreibung aus
    einem Kleinanzeigen 'viewad' HTML.
    """
    soup = BeautifulSoup(html, "lxml")

    # --- Titel
    title_el = soup.select_one("#viewad-title")
    title = _clean_text(title_el.get_text(" ", strip=True)) if title_el else None

    # --- Preis (sichtbar)
    price_el = soup.select_one("#viewad-price")
    price_text = _clean_text(price_el.get_text(" ", strip=True)) if price_el else None

    # --- Preis (meta)
    price_meta_el = soup.select_one('#viewad-main-info meta[itemprop="price"]')
    price_meta = price_meta_el.get("content") if price_meta_el else None

    # --- Ort
    loc_el = soup.select_one("#viewad-locality")
    locality = _clean_text(loc_el.get_text(" ", strip=True)) if loc_el else None

    # --- Datum (steht bei dir im #viewad-extra-info)
    date_text = None
    extra = soup.select_one("#viewad-extra-info")
    if extra:
        # erstes <span> nach dem kalender icon
        span = extra.select_one("div i.icon-calendar-gray-simple + span")
        if span:
            date_text = _clean_text(span.get_text(" ", strip=True))

    # --- Beschreibung
    desc_el = soup.select_one("#viewad-description-text")
    description = None
    if desc_el:
        # <br> in Zeilenumbrüche umwandeln
        description = desc_el.get_text("\n", strip=True).strip()

    # --- Details (Marke, Modell, km, EZ, ...)
    details: Dict[str, str] = {}
    for li in soup.select("#viewad-details .addetailslist--detail"):
        # Struktur ist: "Label" + <span class="...--value">Wert</span>
        value_el = li.select_one(".addetailslist--detail--value")
        if not value_el:
            continue
        value = _clean_text(value_el.get_text(" ", strip=True))

        # label = Text des <li> ohne value-span
        label_text = li.get_text(" ", strip=True)
        # entferne value am Ende, falls enthalten
        if value and label_text.endswith(value):
            label = _clean_text(label_text[: -len(value)])
        else:
            label = label_text
        label = _clean_text(label)

        if label:
            details[label] = value

    # --- Ausstattung / Features
    features = [_clean_text(x.get_text(" ", strip=True)) for x in soup.select("#viewad-configuration .checktag")]
    features = [f for f in features if f]

    # --- Bilder: 1) ld+json contentUrl  2) img data-imgsrc/src
    image_urls: List[str] = []

    # 1) ld+json
    for sc in soup.select('script[type="application/ld+json"]'):
        raw = sc.string or sc.get_text(strip=True)
        raw = raw.strip() if raw else ""
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        # manche Seiten haben Arrays; hier ist es ein dict pro Bild
        if isinstance(data, dict):
            cu = data.get("contentUrl")
            if isinstance(cu, str) and "img.kleinanzeigen.de" in cu:
                image_urls.append(cu)

    # 2) img tags (thumbnails + gallery)
    for img in soup.select("img"):
        u = img.get("data-imgsrc") or img.get("src")
        if isinstance(u, str) and "img.kleinanzeigen.de/api/v1/prod-ads/images/" in u:
            image_urls.append(u)

    # normalize + dedupe (prefer .JPG if present)
    def normalize(u: str) -> str:
        return u.strip()

    seen = set()
    deduped: List[str] = []
    for u in image_urls:
        u2 = normalize(u)
        if not u2 or u2 in seen:
            continue
        seen.add(u2)
        deduped.append(u2)

    # Optional: wenn du lieber die großen JPGs willst, ersetze rule-Parameter nicht blind,
    # sondern verwende vorrangig contentUrl (endet oft mit ?rule=$_59.JPG).
    # Wir lassen hier alles drin, weil du es in deinem Prompt als Links nutzen willst.

    # --- Anzeigen-ID
    ad_id = None
    # Versuch: aus "Anzeigen-ID" Box
    for li in soup.select("#viewad-ad-id-box li"):
        txt = _clean_text(li.get_text(" ", strip=True))
        if txt.isdigit() and len(txt) >= 8:
            ad_id = txt
            break

    # --- Verkäufername (optional)
    seller_name = None
    seller_el = soup.select_one("#viewad-profile-box .userprofile-vip a")
    if seller_el:
        seller_name = _clean_text(seller_el.get_text(" ", strip=True))

    return {
        "source": "kleinanzeigen",
        "ad_id": ad_id,
        "title": title,
        "price_text": price_text,
        "price_meta_eur": price_meta,
        "locality": locality,
        "date_posted": date_text,
        "seller_name": seller_name,
        "details": details,
        "features": features,
        "description": description,
        "image_urls": deduped,
    }


def build_haendler_prompt(listing: dict, max_images: int = 15) -> str:
    title = listing.get("title","")
    price_text = listing.get("price_text","")
    locality = listing.get("locality","")
    date_posted = listing.get("date_posted","")
    ad_id = listing.get("ad_id","")
    desc = (listing.get("description") or "").strip()

    details = listing.get("details") or {}
    brand = details.get("Marke","")
    model = details.get("Modell","")
    ez = details.get("EZ","")
    km = details.get("KM","")
    fuel = details.get("Kraftstoff","")
    gearbox = details.get("Getriebe","")
    power_ps = details.get("Leistung","") or listing.get("power_ps","")

    url = listing.get("url","")
    imgs = listing.get("image_urls") or []
    imgs = imgs[:max_images]

    img_block = "\n".join(imgs) if imgs else "(keine)"

    return f"""Du bist ein erfahrener Gebrauchtwagenhändler (Ankauf + Verkauf) in Deutschland.
Dein Ziel: Inserate schnell und kritisch bewerten, Ankauf-Risiken erkennen, realistische Kosten abschätzen und eine klare Entscheidung treffen.

WICHTIG:
- Denke wie ein Händler: Marge, Standzeit-Risiko, Reparaturrisiko, Prüfbarkeit, Verkaufbarkeit.
- Sei konkret. Wenn Infos fehlen: stelle präzise Rückfragen.
- Keine langen Erklärtexte. Arbeite strukturiert, knallhart, praxisnah.
- Nutze deutsche Begriffe (TÜV/HU, Scheckheft, Unfallfrei, NUR Export, Bastler, Gewährleistung, Ankaufspreis, VK-Preis).

INSERAT:
Titel: {title}
Preis: {price_text}
Ort: {locality}
Online seit: {date_posted}
ID: {ad_id}
URL: {url}

FAHRZEUGDATEN (falls vorhanden):
Marke: {brand}
Modell: {model}
EZ: {ez}
KM: {km}
Kraftstoff: {fuel}
Getriebe: {gearbox}
Leistung: {power_ps}

BESCHREIBUNG:
{desc if desc else "(keine)"}

BILDER (URLs, falls vorhanden):
{img_block}

AUFGABE:
Erstelle eine Händlerbewertung in genau diesem Format:

1) Kurzfazit (1–2 Zeilen)
- Kaufen / Prüfen / Lassen + Begründung in Stichpunkten

2) Händler-Score (0–100)
- Risiko (0–10)
- Verkaufbarkeit (0–10)
- Preisniveau (zu günstig / ok / zu teuer) + kurzer Grund

3) Red Flags (harte Ausschlusskriterien)
- Liste die wichtigsten Warnsignale aus Titel/Beschreibung/Daten. Wenn etwas fehlt, schreibe "Unbekannt".

4) Must-Check vor Ort (Priorität A/B/C)
A = Dealbreaker, B = wichtig, C = nice-to-have
- Technik (Motor/Getriebe/Kühlung/Ölverlust/Abgasanlage/Elektrik)
- Karosserie (Unfallspuren, Spaltmaße, Lack, Rost, Unterboden)
- Innenraum (Abnutzung vs. KM plausibel)
- Dokumente (Service, Rechnungen, HU, Schlüssel, CoC)
- Probefahrt-Checkliste (konkret)

5) Kostenabschätzung (grob, in €)
- Sofortmaßnahmen (z.B. HU, Reifen, Bremsen, Service)
- Typische Risikoposten (modellabhängig, falls ableitbar)
- Puffer/Reserve
=> Schätze einen sinnvollen Gesamtpuffer (min/realistisch/max)

6) Händler-Strategie
- Zielkunde (wer kauft das später?)
- Empfohlene Verkaufsplattform (Kleinanzeigen, mobile, AutoScout) + warum
- Erwartete Standzeit (kurz/mittel/lang) mit Begründung

7) Preis- & Verhandlungsplan
- Fairer Ankaufspreis (Zielbereich) aus Händlersicht
- Max. Ankaufspreis (hartes Limit)
- Empfohlener VK-Preis (Zielbereich)
- Verhandlungsargumente (5 konkrete Sätze)

8) Fragen an den Verkäufer (10 präzise Fragen)
- Fokus: Unfall, Wartung, Mängel, Kaltstart, Ölverbrauch, Fehlermeldungen, Besitz, Grund Verkauf, HU, Schlüssel, Reparaturen, Rechnungen.

Antworte nur im geforderten Format.
"""
    return prompt



KA_AREA_SLUG = os.environ.get("KA_AREA_SLUG", "bayern")
KA_AREA_CODE = os.environ.get("KA_AREA_CODE", "l5510")
KA_RADIUS_KM = int(os.environ.get("KA_RADIUS", "100"))

# optionale Default-Filter (leer lassen, wenn „alle“ gewünscht ist)
KA_PRICE_MIN = os.environ.get("KA_PRICE_MIN", "")   # z.B. "3000"
KA_PRICE_MAX = os.environ.get("KA_PRICE_MAX", "")   # z.B. "7000"
KA_KM_MAX    = os.environ.get("KA_KM_MAX", "")      # z.B. "100000"

# ------------------------------------------------------------
# HTTP Headers
# ------------------------------------------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://www.kleinanzeigen.de/",
}

DETAIL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept-Language": HEADERS["Accept-Language"],
    "Referer": "https://www.kleinanzeigen.de/",
}

# ------------------------------------------------------------
# Parser-Helfer
# ------------------------------------------------------------
def parse_posted_at_de(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    now = datetime.now()
    m = re.search(r'Heute,\s*(\d{1,2}):(\d{2})', s, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        return now.replace(hour=hh, minute=mm, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    m = re.search(r'Gestern,\s*(\d{1,2}):(\d{2})', s, re.I)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        return (now - timedelta(days=1)).replace(hour=hh, minute=mm, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{4})?', s)
    if m:
        dd, mm_, yyyy = int(m.group(1)), int(m.group(2)), (int(m.group(3)) if m.group(3) else now.year)
        try:
            return datetime(yyyy, mm_, dd).strftime("%Y-%m-%d 00:00")
        except ValueError:
            return s
    return s

def norm_int(text: Optional[str]) -> Optional[int]:
    t = re.sub(r"[^\d]", "", text or "")
    return int(t) if t else None

def split_postal_city(location_text: str) -> Tuple[Optional[str], Optional[str]]:
    if not location_text:
        return (None, None)
    m = re.match(r"\s*(\d{4,5})\s+(.+)$", location_text.strip())
    if m:
        return (m.group(1), m.group(2).strip())
    return (None, location_text.strip())

# ------------------------------------------------------------
# SRP-Parsing (Listenansicht)
# ------------------------------------------------------------
def parse_article(article, base="https://www.kleinanzeigen.de"):
    adid = article.get("data-adid")
    if not adid:
        return None

    a_title = article.select_one("h2 a.ellipsis")
    title = a_title.get_text(strip=True) if a_title else None
    href = a_title["href"] if a_title and a_title.get("href") else (article.get("data-href") or "")
    url = base + href if href and href.startswith("/") else href

    price_el = article.select_one(".aditem-main--middle--price-shipping--price")
    price_eur = norm_int(price_el.get_text()) if price_el else None

    tags = article.select(".aditem-main--bottom .simpletag")
    km = norm_int(tags[0].get_text()) if len(tags) > 0 else None
    ez_text = tags[1].get_text(strip=True) if len(tags) > 1 else None

    loc_el = article.select_one(".aditem-main--top--left")
    location = loc_el.get_text(" ", strip=True) if loc_el else None
    postal_code, city = split_postal_city(location)

    time_el = article.select_one(".aditem-main--top--right")
    posted_raw = time_el.get_text(" ", strip=True) if time_el else None
    posted_at = parse_posted_at_de(posted_raw)

    pics_el = article.select_one(".galleryimage--counter")
    pics = norm_int(pics_el.get_text()) if pics_el else None

    return {
        "id": adid,
        "platform": "ebay-kleinanzeigen",
        "url": url,
        "title": title,
        "price_eur": price_eur,
        "km": km,
        "ez_text": ez_text,
        "location": location,
        "postal_code": postal_code,
        "city": city,
        "posted_at": posted_at,
        "pics": pics,
    }

def crawl_search_page(url: str) -> List[Dict]:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    out = []
    for art in soup.select("article.aditem[data-adid]"):
        row = parse_article(art)
        if row:
            out.append(row)
    return out

# ------------------------------------------------------------
# Detail-Parsing (Einzelseite)
# ------------------------------------------------------------
FIELD_MAP = {
    "marke": "brand",
    "modell": "model",
    "kilometerstand": "km",
    "erstzulassung": "first_reg",
    "kraftstoffart": "fuel",
    "leistung": "power_ps",
    "getriebe": "gearbox",
    "anzahl türen": "doors",
    "hu bis": "hu_until",
    "schadstoffklasse": "emission_class",
    "außenfarbe": "color",
    "material innenausstattung": "upholstery",
}

def _norm_int(s: Optional[str]) -> Optional[int]:
    s = re.sub(r"[^\d]", "", s or "")
    return int(s) if s else None

def parse_detail_page(url: str) -> dict:
    r = requests.get(url, headers=DETAIL_HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    data: dict = {}

    # --- Details (Marke/Modell/km/EZ/...) ---
    for li in soup.select("#viewad-details li.addetailslist--detail"):
        label = (li.contents[0].strip() if li.contents and isinstance(li.contents[0], str) else "").lower()
        value_el = li.select_one(".addetailslist--detail--value")
        value = value_el.get_text(" ", strip=True) if value_el else ""
        key = FIELD_MAP.get(label)
        if not key:
            continue

        if key == "km":
            data[key] = _norm_int(value)
        elif key == "power_ps":
            ps = re.search(r"(\d{2,3})\s*ps", value, re.I)
            if ps:
                data[key] = int(ps.group(1))
            else:
                kw = re.search(r"(\d{2,3})\s*kW", value, re.I)
                data[key] = int(round(int(kw.group(1)) * 1.3596)) if kw else None
        else:
            data[key] = value

    # --- Beschreibung ---
    desc_el = soup.select_one("#viewad-description-text")
    if desc_el:
        data["description"] = desc_el.get_text("\n", strip=True).strip()

    # --- Bilder sammeln (URLs) ---
    image_urls = []

    # 1) ld+json contentUrl
    for sc in soup.select('script[type="application/ld+json"]'):
        raw = (sc.string or sc.get_text(strip=True) or "").strip()
        if not raw:
            continue
        try:
            j = json.loads(raw)
        except Exception:
            continue
        if isinstance(j, dict):
            cu = j.get("contentUrl")
            if isinstance(cu, str) and "img.kleinanzeigen.de" in cu:
                image_urls.append(cu)

    # 2) img tags
    for img in soup.select("img"):
        u = img.get("data-imgsrc") or img.get("src")
        if isinstance(u, str) and "img.kleinanzeigen.de/api/v1/prod-ads/images/" in u:
            image_urls.append(u)

    # dedupe
    seen = set()
    deduped = []
    for u in image_urls:
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        deduped.append(u)

    if deduped:
        data["image_urls_json"] = json.dumps(deduped, ensure_ascii=False)

    return data

# ------------------------------------------------------------
# Such-URLs
# ------------------------------------------------------------
def build_ka_search_url(page: int = 1) -> str:
    base = "https://www.kleinanzeigen.de/s-autos/"
    path = ""

    # NEU: nur Privatverkäufer
    path += "anbieter:privat/"

    if KA_AREA_SLUG:
        path += f"{KA_AREA_SLUG}/"
    path += "anzeige:angebote/"

    if KA_PRICE_MIN or KA_PRICE_MAX:
        path += f"preis:{KA_PRICE_MIN}:{KA_PRICE_MAX}/"

    if page >= 2:
        path += "seite:2/"

    cblock = f"c216{KA_AREA_CODE}r{KA_RADIUS_KM}"

    parts = [cblock]
    if KA_KM_MAX:
        parts.append(f"autos.km_i:%2C{KA_KM_MAX}")

    return base + path + "+".join(parts)


# Statt fixer Liste dynamisch Page 1 + 2
SEARCH_URLS = [build_ka_search_url(1), build_ka_search_url(2)]


# ------------------------------------------------------------
# Sync
# ------------------------------------------------------------
def sync_once() -> dict:
    """
    Läuft alle SEARCH_URLS durch, schreibt SRP-Daten in DB und
    reichert anschließend per Detailseite Marke/Modell/… an.
    'stored' zählt NUR echte Inserts/Updates.
    """
    seen = 0
    stored = 0
    for url in SEARCH_URLS:
        try:
            rows = crawl_search_page(url)
            seen += len(rows)
            for row in rows:
                # 1) SRP-Daten schreiben
                stored += upsert_listing(row)

                # 2) Detaildaten ergänzen (immer platform/url/title mitgeben)
                if row.get("url"):
                    try:
                        det = parse_detail_page(row["url"])
                        if det:
                            payload = {
                                "id": row["id"],
                                "platform": row.get("platform") or "ebay-kleinanzeigen",
                                "url": row.get("url"),
                            }
                            if row.get("title"):
                                payload["title"] = row["title"]
                            payload.update(det)
                            stored += upsert_listing(payload)
                    except Exception as e:
                        print(f"[WARN] Detail bei {row.get('id')}: {e}")
                    time.sleep(0.8)  # höflich zur Gegenstelle
            time.sleep(2)  # kleine Pause zwischen Seiten
        except Exception as e:
            print(f"[WARN] Fehler bei {url}: {e}")
    return {"seen": seen, "stored": stored}


# ------------------------------------------------------------
# CLI
# ------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    res = sync_once()
    print(f"Fertig. Gesehen: {res['seen']}, geschrieben: {res['stored']}")
