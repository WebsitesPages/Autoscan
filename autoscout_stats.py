# providers/autoscout_stats.py
import re, requests

def _parse_prices_from_html_as(html: str):
    if not html: return []
    header_re = re.compile(
        r'<h1[^>]*data-testid="list-header-title"[^>]*>\s*([0-9]+)\s+Angebot(?:e)?\b',
        re.IGNORECASE | re.DOTALL,
    )
    p_tag_re = re.compile(
        r'<p[^>]*data-testid="regular-price"[^>]*>(.*?)</p>',
        re.IGNORECASE | re.DOTALL,
    )
    euro_re = re.compile(r'(?:€\s*)?(\d{1,3}(?:\.\d{3})+|\d+)', re.IGNORECASE)

    limit = None
    m_head = header_re.search(html)
    if m_head:
        try: limit = int(m_head.group(1))
        except: limit = None

    prices = []
    for m in p_tag_re.finditer(html):
        inner = m.group(1) or ''
        m2 = euro_re.search(inner)
        if not m2: continue
        raw = m2.group(1)
        try:
            val = int(raw.replace('.', '').replace(' ', ''))
            if 500 <= val <= 300000:
                prices.append(val)
        except: pass
        if isinstance(limit, int) and limit > 0 and len(prices) >= limit:
            break
    return prices

def fetch_autoscout_stats(url: str) -> dict:
    """Liefert {ok, count, avg_price_eur, url} für eine gegebene AutoScout-SRP-URL."""
    prices = []
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        r1 = requests.get(url, headers=headers, timeout=12)
        if r1.ok:
            prices.extend(_parse_prices_from_html_as(r1.text))

            # Header-Anzahl erkennen, ob Seite 2 gebraucht wird
            head_m = re.search(
                r'<h1[^>]*data-testid="list-header-title"[^>]*>\s*([0-9]+)\s+Angebot',
                r1.text or "", re.IGNORECASE
            )
            announced = int(head_m.group(1)) if head_m else None
            if announced and len(prices) < announced:
                url2 = url + ("&" if "?" in url else "?") + "page=2"
                r2 = requests.get(url2, headers=headers, timeout=12)
                if r2.ok:
                    more = _parse_prices_from_html_as(r2.text)
                    prices = (prices + more)[:announced]
    except Exception:
        pass

    count = len(prices)
    avg = int(round(sum(prices) / count)) if count else 0
    return {"ok": True, "count": count, "avg_price_eur": avg, "url": url}
