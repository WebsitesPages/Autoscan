# providers/carwow_stats.py
import re
import html
import requests
from urllib.parse import urlencode

# If you don't have brotli installed, don't claim you accept br.
HDRS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Referer": "https://angebote.carwow.de/",
    # Let requests handle gzip/deflate automatically; avoid br unless brotli is installed.
    "Accept-Encoding": "gzip, deflate",
}
_BLOCK_HINTS = (
    "captcha", "are you human", "access denied", "temporarily blocked",
    "robot", "forbidden", "not authorized", "verify you are", "cloudflare"
)
def _looks_blocked(text: str) -> bool:
    if not text: 
        return True
    t = text.lower()
    return any(h in t for h in _BLOCK_HINTS)
# -------- helpers -------------------------------------------------------------

_WS_RE = re.compile(r"\s+", re.UNICODE)

def _clean(s: str) -> str:
    """Normalize HTML fragments: unescape, strip tags, collapse whitespace, remove exotic spaces."""
    if not s:
        return ""
    s = html.unescape(s)
    # strip tags
    s = re.sub(r"<[^>]+>", " ", s)
    # replace exotic spaces often used on SRPs
    s = (s
         .replace("\xa0", " ")    # NBSP
         .replace("\u202f", " ")  # NARROW NBSP
         .replace("\u2009", " ")  # THIN SPACE
         .replace("\u2007", " ")) # FIGURE SPACE
    s = _WS_RE.sub(" ", s).strip()
    return s

def _to_int(num_str: str) -> int:
    """Parse European-formatted ints '10.715' or '10 715' robustly."""
    if not num_str:
        return 0
    num_str = (num_str
               .replace(".", "")
               .replace(" ", "")
               .replace("\xa0", "")
               .replace("\u202f", "")
               .replace("\u2009", "")
               .replace("\u2007", ""))
    try:
        return int(num_str)
    except Exception:
        return 0

_NUM_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})*|\d+)")

# -------- count ---------------------------------------------------------------

# Primary: explicit id
_DEALS_COUNT_RE = re.compile(
    r'id=["\']deals-count-header["\'][^>]*>\s*([0-9][0-9.\s]*)\s*(?:<!--.*?-->)*\s*Angebot(?:e)?\b',
    re.IGNORECASE | re.DOTALL
)

# Fallback: class contains "contains-deals-count"
_DEALS_COUNT_FALLBACK = re.compile(
    r'class=["\'][^"\']*contains-deals-count[^"\']*["\'][^>]*>\s*([0-9][0-9.\s]*)\s*(?:<!--.*?-->)*\s*Angebot(?:e)?\b',
    re.IGNORECASE | re.DOTALL
)

# Very generic safety net (avoid false positives by requiring "Angebot/Angebote")
_GENERIC_COUNT_RE = re.compile(
    r'([0-9][0-9.\s]*)\s*(?:<!--.*?-->)*\s*Angebot(?:e)?\b',
    re.IGNORECASE | re.DOTALL
)

def _extract_count(html_text: str) -> int:
    if not html_text:
        return 0
    m = _DEALS_COUNT_RE.search(html_text) or _DEALS_COUNT_FALLBACK.search(html_text)
    if not m:
        m = _GENERIC_COUNT_RE.search(html_text)
        if not m:
            return 0
    return _to_int(m.group(1) or "")

# -------- prices --------------------------------------------------------------

# Prices are rendered like:
#   <div class="deal-card__price">10.715 €</div>
# but sometimes spans appear inside; we parse the full inner HTML then strip tags.
_PRICE_BLOCK_RE = re.compile(
    r'<div[^>]*class=["\']deal-card__price["\'][^>]*>(?P<inner>.*?)</div>',
    re.IGNORECASE | re.DOTALL
)

# JSON fallback (Next.js data blobs sometimes include price or price_in_cents)
_JSON_PRICE_RE = re.compile(
    r'"(?:discounted_)?price(?:_in_cents)?"\s*:\s*(\d{3,9})',
    re.IGNORECASE
)
_JSON_CENTS_HINT = re.compile(r'"price_in_cents"', re.IGNORECASE)

def _extract_prices(html_text: str):
    out = []
    if not html_text:
        return out

    # 1) DOM block(s)
    for m in _PRICE_BLOCK_RE.finditer(html_text):
        inner = _clean(m.group("inner") or "")
        m2 = _NUM_RE.search(inner)
        if not m2:
            continue
        val = _to_int(m2.group(1))
        if 500 <= val <= 300000:
            out.append(val)

    # 2) JSON fallback (if DOM parsing found nothing or to enrich sparse pages)
    if not out:
        is_cents = bool(_JSON_CENTS_HINT.search(html_text or ""))
        for m in _JSON_PRICE_RE.finditer(html_text or ""):
            try:
                v = int(m.group(1))
                if is_cents:
                    v = round(v / 100.0)
                if 500 <= v <= 300000:
                    out.append(int(v))
            except Exception:
                pass

    return out

# -------- fetch ---------------------------------------------------------------

def fetch_carwow_stats(url: str, timeout: float = 12.0) -> dict:
    try:
        r = requests.get(url, headers=HDRS, timeout=timeout, allow_redirects=True)
        html_text = r.text or ""
        # Wenn HTTP-Fehler oder offensichtliche Block-Seite → klar melden
        if r.status_code >= 400 or _looks_blocked(html_text):
            return {"ok": False, "count": 0, "avg_price_eur": 0, "url": url, "error": "blocked"}

        announced = _extract_count(html_text)
        prices = _extract_prices(html_text)

        if announced and len(prices) > announced:
            prices = prices[:announced]

        count = len(prices)
        avg = int(round(sum(prices) / count)) if count else 0
        return {"ok": True, "count": count, "avg_price_eur": avg, "url": url}
    except requests.RequestException as e:
        return {"ok": False, "count": 0, "avg_price_eur": 0, "url": url, "error": f"net:{type(e).__name__}"}
        

# -------- URL builder (unchanged) --------------------------------------------

def _slug(v: str) -> str:
    v = (v or "").strip().lower()
    v = re.sub(r"[^a-z0-9]+", "-", v).strip("-")
    return v

def build_carwow_search_url(r) -> str:
    def val(k, default=""):
        try: return (r[k] or "").strip()
        except Exception: return default

    brand = _slug(val("brand"))
    model = _slug(val("model"))
    if not brand or not model:
        return ""

    fuel_raw = (val("fuel") or "").lower()
    fuel_map = {
        "benzin": "petrol", "benziner": "petrol", "super": "petrol",
        "diesel": "diesel",
        "elektro": "electric", "electric": "electric", "bev": "electric",
        "hybrid": "hybrid", "plug-in-hybrid": "hybrid", "phev": "hybrid",
    }
    fuel = fuel_map.get(fuel_raw, "")

    gear_raw = (val("gearbox") or "").lower()
    trans = "automatic" if "auto" in gear_raw else ("manual" if ("schalt" in gear_raw or "man" in gear_raw) else "")

    km = None
    try:
        km = int(r["km"]) if r["km"] is not None else None
    except Exception:
        km = None
    gte = lte = None
    if isinstance(km, int) and km > 0:
        gte = max(0, int(round(km * 0.9)))
        lte = int(round(km * 1.1))

    base = "https://angebote.carwow.de/stock_cars"
    q = {
        "sort": "recommended",
        "vehicle_type": "car",
        "deal_type_group": "cash",
        "vehicle_state_group": "used",
        "brand_slug": brand,
        "model_slug": model,
    }
    if fuel:
        q["vehicle_fuel_category[]"] = fuel
    if trans:
        q["vehicle_transmission_category[]"] = trans
    if gte is not None:
        q["vehicle_distance_travelled[gte]"] = str(gte)
    if lte is not None:
        q["vehicle_distance_travelled[lte]"] = str(lte)

    return base + "?" + urlencode(q, doseq=True)
