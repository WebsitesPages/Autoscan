# providers/links.py
import os, re
KA_AREA_SLUG = os.environ.get("KA_AREA_SLUG", "bayern")
KA_AREA_CODE = os.environ.get("KA_AREA_CODE", "l5510")
KA_RADIUS_KM = int(os.environ.get("KA_RADIUS", "100"))

def build_similar_search_url(r):
    def val(k):
        try: return r[k]
        except Exception: return None
    def slug(v: str) -> str:
        v = (v or "").lower()
        v = re.sub(r"[^a-z0-9]+", "_", v)
        v = re.sub(r"_+", "_", v).strip("_")
        return v

    brand = slug(val("brand") or "")
    model = slug(val("model") or "")
    fuel  = slug(val("fuel")  or "")
    gear  = slug(val("gearbox") or "")
    km    = val("km")
    if not brand or not model:
        return ""

    first_reg = (val("first_reg") or "")[:10]
    ez_text   = (val("ez_text") or "")
    year = None
    for cand in (first_reg, ez_text):
        m = re.search(r"(20\d{2}|19\d{2})", cand or "")
        if m:
            year = int(m.group(1)); break

    km_min = km_max = ""
    if isinstance(km, int):
        km_min = int(km * 0.9)
        km_max = int(km * 1.1)

    ez_min = ez_max = ""
    if isinstance(year, int):
        ez_min = year - 1
        ez_max = year + 1

    base = "https://www.kleinanzeigen.de/s-autos/"
    path = ""
    if KA_AREA_SLUG:
        path += f"{KA_AREA_SLUG}/"
    path += "anzeige:angebote/"

    cblock = f"c216{KA_AREA_CODE}r{KA_RADIUS_KM}"
    parts = [cblock]
    if brand: parts.append(f"autos.marke_s:{brand}")
    if model: parts.append(f"autos.model_s:{model}")
    if km_min != "" and km_max != "": parts.append(f"autos.km_i:{km_min},{km_max}")
    if ez_min != "" and ez_max != "": parts.append(f"autos.ez_i:{ez_min},{ez_max}")
    if fuel: parts.append(f"autos.fuel_s:{fuel}")
    if gear: parts.append(f"autos.shift_s:{gear}")

    return base + path + "+".join(parts)


def build_autoscout_search_url(r):
    """Baut eine AutoScout24-Suchergebnis-URL (nur Privat)."""
    def val(k):
        try: return r[k]
        except Exception: return None

    def slug_brand(v: str) -> str:
        v = (v or "").lower()
        v = re.sub(r"[^a-z0-9]+", "-", v).strip("-")
        return v

    def slug_model(v: str) -> str:
        v = (v or "").strip()
        v = re.sub(r"\s+", "-", v)
        v = re.sub(r"[^a-zA-Z0-9\-\(\)]+", "", v)
        return v

    brand_raw = (val("brand") or "")
    model_raw = (val("model") or "")
    if not brand_raw or not model_raw:
        return ""

    brand = slug_brand(brand_raw)
    model = slug_model(model_raw)

    first_reg = (val("first_reg") or "")[:10]
    ez_text   = (val("ez_text") or "")
    import re as _re
    year = None
    for cand in (first_reg, ez_text):
        m = _re.search(r"(20\d{2}|19\d{2})", cand or "")
        if m:
            year = int(m.group(1)); break

    y_from = y_to = ""
    if isinstance(year, int):
        y_from = year - 1
        y_to   = year + 1

    km = val("km")
    km_from = km_to = ""
    if isinstance(km, int):
        km_from = int(km * 0.9)
        km_to   = int(km * 1.1)

    fuel_raw = (val("fuel") or "").lower()
    fuel_map = {"benzin":"B","benziner":"B","super":"B","diesel":"D","elektro":"E","electric":"E","bev":"E","hybrid":"H","plug-in-hybrid":"H","phev":"H"}
    fuel = fuel_map.get(fuel_raw, "")

    gear_raw = (val("gearbox") or "").lower()
    gear = "A" if "auto" in gear_raw else ("M" if ("schalt" in gear_raw or "man" in gear_raw) else "")

    base = "https://www.autoscout24.de/lst/"
    path = brand + (f"/{model}" if model else "")

    from urllib.parse import urlencode
    q = {
        "atype": "C",
        "cy": "D",
        "custtype": "P",     # nur Privat
        "damaged_listing": "exclude",
        "desc": "0",
        "sort": "standard",
        "source": "detailsearch",
    }
    if y_from != "" and y_to != "": q["fregfrom"] = str(y_from); q["fregto"] = str(y_to)
    if km_from != "" and km_to != "": q["kmfrom"]  = str(km_from); q["kmto"]  = str(km_to)
    if fuel: q["fuel"] = fuel
    if gear: q["gear"] = gear

    return base + path + "?" + urlencode(q)
# providers/links.py (AUSSCHNITT – bestehende Builder bleiben)
import re
from urllib.parse import urlencode

def _slug(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9\- ]+", "", s).replace(" ", "-")
    return s

def build_carwow_search_url(r: dict) -> str:
    """
    Baut eine Carwow-URL ähnlich deiner Vorgaben:
    - used, cash, vehicle_type=car
    - km ±10% um r['km'] (falls vorhanden)
    - fuel mapping, gearbox mapping
    """
    brand = _slug((r.get("brand") or ""))
    model = _slug((r.get("model") or ""))
    if not brand or not model:
        return ""

    # km ±10%
    km = r.get("km")
    gte = lte = None
    if isinstance(km, int):
        gte = max(0, int(km * 0.9))
        lte = int(km * 1.1)

    # fuel -> carwow
    fuel_raw = (r.get("fuel") or "").lower()
    fuel_map = {
        "benzin": "petrol", "benziner": "petrol", "super": "petrol",
        "diesel": "diesel",
        "elektro": "electric", "bev": "electric", "electric": "electric",
        "hybrid": "hybrid", "plug-in-hybrid": "hybrid", "phev": "hybrid"
    }
    fuel = fuel_map.get(fuel_raw, "")

    # gearbox -> carwow
    g_raw = (r.get("gearbox") or "").lower()
    trans = "automatic" if "auto" in g_raw else ("manual" if "schalt" in g_raw or "man" in g_raw else "")

    base = "https://angebote.carwow.de/stock_cars"
    q = {
        "sort": "recommended",
        "vehicle_type": "car",
        "deal_type_group": "cash",
        "vehicle_state_group": "used",
        "brand_slug": brand,
        "model_slug": model,
    }
    if fuel: q["vehicle_fuel_category[]"] = fuel
    if trans: q["vehicle_transmission_category[]"] = trans
    if gte is not None: q["vehicle_distance_travelled[gte]"] = str(gte)
    if lte is not None: q["vehicle_distance_travelled[lte]"] = str(lte)

    return base + "?" + urlencode(q, doseq=True)
