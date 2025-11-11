# providers/ka_stats.py
import re, html as _html, requests as _req

_KA_BREADCRUMB_COUNT_RE = re.compile(
    r'<span[^>]*class=["\']breadcrump-summary["\'][^>]*>.*?von\s*([0-9][0-9\.\s]*)\s+[A-Za-zÄÖÜäöüß]',
    re.IGNORECASE | re.DOTALL
)
_KA_PRICE_P_RE = re.compile(
    r'<p[^>]*class=["\']aditem-main--middle--price-shipping--price["\'][^>]*>(?P<inner>.*?)</p>',
    re.IGNORECASE | re.DOTALL
)
_KA_EURO_NUM_RE = re.compile(r'(\d{1,3}(?:[.\s]\d{3})*|\d+)')

def _page2_url(u: str) -> str:
    if "/seite:" in u: return u
    import re as _re
    return _re.sub(r"(/s-autos/[^/]+/|/s-autos/)", r"\1seite:2/", u, count=1)

def _count(html: str) -> int:
    if not html: return 0
    m = _KA_BREADCRUMB_COUNT_RE.search(html)
    if not m: return 0
    raw = (m.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
    try: return int(raw)
    except: return 0

def _prices(html: str):
    out = []
    if not html: return out
    for m in _KA_PRICE_P_RE.finditer(html):
        inner = _html.unescape(m.group('inner') or '')
        inner = re.sub(r'<[^>]+>', ' ', inner)
        m2 = _KA_EURO_NUM_RE.search(inner)
        if not m2: continue
        num = (m2.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
        try:
            val = int(num)
            if 500 <= val <= 300000: out.append(val)
        except: pass
    return out

def fetch_ka_stats(url: str) -> dict:
    """Liefert {ok, count, avg_price_eur, url} für eine gegebene KA-SRP-URL."""
    try:
        hdrs = {"User-Agent": "Mozilla/5.0"}
        r1 = _req.get(url, headers=hdrs, timeout=12)
        html1 = r1.text if r1.ok else ""
        announced = _count(html1)
        prices = _prices(html1)
        if announced and len(prices) < announced:
            url2 = _page2_url(url)
            r2 = _req.get(url2, headers=hdrs, timeout=12)
            if r2.ok:
                prices += _prices(r2.text or "")
        if announced and announced > 0:
            prices = prices[:announced]
        count = len(prices)
        avg = int(round(sum(prices) / count)) if count else 0
        return {"ok": True, "count": count, "avg_price_eur": avg, "url": url}
    except Exception:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": url}
