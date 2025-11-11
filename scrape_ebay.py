# scrape_ebay.py
import re
import time
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from db import init_db, upsert_listing
from typing import Optional, List, Dict, Tuple
import os  # neu

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
    return data

# ------------------------------------------------------------
# Such-URLs
# ------------------------------------------------------------
def build_ka_search_url(page: int = 1) -> str:
    """
    Baut eine SRP-URL:
    https://www.kleinanzeigen.de/s-autos/<area>/anzeige:angebote/ [preis:min:max/]
    c216<l-code>r<radius> [+ autos.km_i:,:<km_max>]
    und optional /seite:2/ für page=2
    """
    base = "https://www.kleinanzeigen.de/s-autos/"
    path = ""
    if KA_AREA_SLUG:
        path += f"{KA_AREA_SLUG}/"
    path += "anzeige:angebote/"

    if KA_PRICE_MIN or KA_PRICE_MAX:
        path += f"preis:{KA_PRICE_MIN}:{KA_PRICE_MAX}/"

    # Pagehandling: „/seite:2/“ wird direkt hinter dem Pfad eingeschoben
    if page >= 2:
        path += "seite:2/"

    cblock = f"c216{KA_AREA_CODE}r{KA_RADIUS_KM}"

    parts = [cblock]
    if KA_KM_MAX:
        parts.append(f"autos.km_i:%2C{KA_KM_MAX}")  # nur Obergrenze

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
