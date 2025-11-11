# app.py — einfache Web-UI für deine autos.db
# Voraussetzungen: pip install flask
# Start: python3 app.py  →  http://127.0.0.1:5000/

import os
import sqlite3
from flask import Flask, request, redirect, url_for, render_template_string
from providers.links import build_autoscout_search_url, build_similar_search_url
from providers.ka_stats import fetch_ka_stats
from providers.autoscout_stats import fetch_autoscout_stats
from providers.carwow_stats import fetch_carwow_stats, build_carwow_search_url


APP_TITLE = "Autoscan – Listings"
DB_PATH = os.environ.get("AUTOS_DB", "autos.db")
PER_PAGE_DEFAULT = 50

app = Flask(__name__)

# Jinja: Namen bleiben exakt gleich
app.jinja_env.globals['build_autoscout_search_url'] = build_autoscout_search_url
app.jinja_env.globals['build_similar_search_url']  = build_similar_search_url
app.jinja_env.globals['build_carwow_search_url']   = build_carwow_search_url




@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
# --- Kleinanzeigen-Defaults (per ENV überschreibbar) ---
KA_AREA_SLUG  = os.environ.get("KA_AREA_SLUG", "bayern")   # Pfadsegment
KA_AREA_CODE  = os.environ.get("KA_AREA_CODE", "l5510")    # Regionscode im c216-Block
KA_RADIUS_KM  = int(os.environ.get("KA_RADIUS", "100"))    # r100

def build_similar_search_url(r):
    def val(k):
        try: return r[k]
        except Exception: return None
    import re
    def slug(v: str) -> str:
        v = (v or "").lower()
        v = re.sub(r"[^a-z0-9]+", "_", v)
        v = re.sub(r"_+", "_", v).strip("_")
        return v

    brand = slug(val("brand") or "")
    model = slug(val("model") or "")
    fuel  = slug(val("fuel") or "")
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

    # Basis + Region im Pfad
    base = "https://www.kleinanzeigen.de/s-autos/"
    path = ""
    if KA_AREA_SLUG:
        path += f"{KA_AREA_SLUG}/"
    # nur Angebote
    path += "anzeige:angebote/"

    # c216 + Regionscode + Radius
    cblock = f"c216{KA_AREA_CODE}r{KA_RADIUS_KM}"

    parts = [cblock]
    if brand: parts.append(f"autos.marke_s:{brand}")
    if model: parts.append(f"autos.model_s:{model}")
    if km_min != "" and km_max != "": parts.append(f"autos.km_i:{km_min},{km_max}")
    if ez_min != "" and ez_max != "": parts.append(f"autos.ez_i:{ez_min},{ez_max}")
    if fuel: parts.append(f"autos.fuel_s:{fuel}")
    if gear: parts.append(f"autos.shift_s:{gear}")

    # Ergebnis: .../bayern/anzeige:angebote/<c216+l5510+r100>+autos.model_s:...
    return base + path + "+".join(parts)


def build_autoscout_search_url(r):
    """Baut eine AutoScout24-Suchergebnis-URL aus den Listing-Feldern (nur Privat)."""
    def val(k):
        try: return r[k]
        except Exception: return None
    import re
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
    year = None
    for cand in (first_reg, ez_text):
        m = re.search(r"(20\d{2}|19\d{2})", cand or "")
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

    q = {
        "atype": "C",
        "cy": "D",
        "custtype": "P",               # << nur Privatverkäufer
        "damaged_listing": "exclude",
        "desc": "0",
        "sort": "standard",
        "source": "detailsearch",
    }
    if y_from != "" and y_to != "":
        q["fregfrom"] = str(y_from); q["fregto"] = str(y_to)
    if km_from != "" and km_to != "":
        q["kmfrom"] = str(km_from); q["kmto"] = str(km_to)
    if fuel: q["fuel"] = fuel
    if gear: q["gear"] = gear

    from urllib.parse import urlencode
    return base + path + "?" + urlencode(q)


app.jinja_env.globals['build_autoscout_search_url'] = build_autoscout_search_url

app.jinja_env.globals['build_similar_search_url'] = build_similar_search_url






def parse_int(v, default=None):
    try:
        if v is None or v == "":
            return default
        return int(v)
    except Exception:
        return default


def build_query(params):
    where = []
    args = []

    # Textsuche im Titel
    if params.get("q"):
        where.append("title LIKE ?")
        args.append(f"%{params['q']}%")

    # Preisbereich
    pmin = parse_int(params.get("price_min"))
    pmax = parse_int(params.get("price_max"))
    if pmin is not None:
        where.append("price_eur >= ?")
        args.append(pmin)
    if pmax is not None:
        where.append("price_eur <= ?")
        args.append(pmax)

    # Kilometer max
    km_max = parse_int(params.get("km_max"))
    if km_max is not None:
        where.append("km <= ?")
        args.append(km_max)

    # PLZ-Prefix (z. B. 85%)
    plz = params.get("postal_prefix")
    if plz:
        where.append("postal_code LIKE ?")
        args.append(plz.rstrip("%") + "%")

    # Stadt enthält
    city = params.get("city")
    if city:
        where.append("city LIKE ?")
        args.append(f"%{city}%")

    # Mindestanzahl Bilder
    pics_min = parse_int(params.get("pics_min"))
    if pics_min is not None:
        # pics kann NULL sein → behandle NULL als 0
        where.append("IFNULL(pics, 0) >= ?")
        args.append(pics_min)

    # Neueste: posted_at in letzten X Tagen
    posted_days = parse_int(params.get("posted_days"))
    if posted_days is not None and posted_days >= 0:
        where.append("posted_at IS NOT NULL AND posted_at >= date('now', ?) ")
        args.append(f"-{posted_days} day")

    # Nur Angebote (optional – hängt von deiner Quelle ab). Hier lassen wir alles durch.

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    # Sortierung
    sort = params.get("sort", "posted_desc")
    order_sql = {
    "price_asc":  "ORDER BY (price_eur IS NULL),  price_eur ASC,  last_seen DESC",
    "price_desc": "ORDER BY (price_eur IS NULL),  price_eur DESC, last_seen DESC",
    "km_asc":     "ORDER BY (km IS NULL),         km ASC,         last_seen DESC",
    "km_desc":    "ORDER BY (km IS NULL),         km DESC,        last_seen DESC",
    "posted_desc":"ORDER BY (posted_at IS NULL),  posted_at DESC, last_seen DESC",
    "seen_desc":  "ORDER BY last_seen DESC",
    "title_asc":  "ORDER BY title COLLATE NOCASE ASC, last_seen DESC",
}.get(sort, "ORDER BY (posted_at IS NULL), posted_at DESC, last_seen DESC")



    return where_sql, args, order_sql


@app.route("/")
def index():
    # Query-Parameter lesen
    q = request.args.get("q", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "")
    km_max = request.args.get("km_max", "")
    postal_prefix = request.args.get("postal_prefix", "")
    city = request.args.get("city", "")
    pics_min = request.args.get("pics_min", "")
    posted_days = request.args.get("posted_days", "")
    sort = request.args.get("sort", "posted_desc")

    page = max(parse_int(request.args.get("page"), 1) or 1, 1)
    per_page = max(parse_int(request.args.get("per_page"), PER_PAGE_DEFAULT) or PER_PAGE_DEFAULT, 1)
    offset = (page - 1) * per_page

    params = {
        "q": q,
        "price_min": price_min,
        "price_max": price_max,
        "km_max": km_max,
        "postal_prefix": postal_prefix,
        "city": city,
        "pics_min": pics_min,
        "posted_days": posted_days,
        "sort": sort,
    }

    where_sql, args, order_sql = build_query(params)

    # Count + Daten holen
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"SELECT COUNT(*) AS n FROM listings {where_sql}", args)
    total = cur.fetchone()[0]

    cur.execute(
    f"""SELECT id, title, price_eur, km, postal_code, city, posted_at, pics, url, platform, last_seen, ez_text,
               brand, model, fuel, gearbox, first_reg
        FROM listings {where_sql} {order_sql} LIMIT ? OFFSET ?""",
    args + [per_page, offset],
)

    rows = cur.fetchall()
    conn.close()

    # Pagination-URLs
    def page_url(p):
        if p < 1:
            p = 1
        qs = request.args.to_dict(flat=True)
        qs["page"] = str(p)
        return url_for("index", **qs)

    return render_template_string(TPL, **{
        "app_title": APP_TITLE,
        "rows": rows,
        "total": total,
        "page": page,
        "per_page": per_page,
        "has_prev": page > 1,
        "has_next": (offset + len(rows)) < total,
        "prev_url": page_url(page - 1),
        "next_url": page_url(page + 1),
        "params": params,
    })

@app.get("/api/table")
def api_table():
    # gleiche Query-Logik wie im index(), aber nur Tabelle rendern
    q = request.args.get("q", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "")
    km_max = request.args.get("km_max", "")
    postal_prefix = request.args.get("postal_prefix", "")
    city = request.args.get("city", "")
    pics_min = request.args.get("pics_min", "")
    posted_days = request.args.get("posted_days", "")
    sort = request.args.get("sort", "posted_desc")

    page = 1  # bei Autorefresh immer Seite 1 zeigen
    per_page = 50

    params = {
        "q": q,
        "price_min": price_min,
        "price_max": price_max,
        "km_max": km_max,
        "postal_prefix": postal_prefix,
        "city": city,
        "pics_min": pics_min,
        "posted_days": posted_days,
        "sort": sort,
    }

    where_sql, args, order_sql = build_query(params)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
    f"""SELECT id, title, price_eur, km, postal_code, city, posted_at, pics, url, ez_text,
               brand, model, fuel, gearbox, first_reg
        FROM listings {where_sql} {order_sql} LIMIT ? OFFSET ?""",
    args + [per_page, 0],
)

    rows = cur.fetchall()
    conn.close()

    return render_template_string(ROWS_ONLY_TPL, rows=rows)


TPL = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{{ app_title }}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    /* hübschere Scrollbar (webkit) */
    ::-webkit-scrollbar { height: 10px; width: 10px; }
    ::-webkit-scrollbar-thumb { background: #c7c9d1; border-radius: 8px; }
    ::-webkit-scrollbar-track { background: #eef0f4; }
  </style>
</head>
<body class="min-h-screen bg-gradient-to-b from-indigo-50 via-white to-white text-gray-900">
  <header class="sticky top-0 z-40 backdrop-blur bg-white/70 border-b">
    <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between">
      <div class="flex items-center gap-3">
        <div class="h-9 w-9 rounded-xl bg-indigo-600 text-white grid place-items-center font-semibold">AS</div>
        <div>
          <h1 class="text-xl font-semibold leading-tight">{{ app_title }}</h1>
          <p class="text-xs text-gray-500">Lokale Marktübersicht • Datenquelle: <span class="font-medium">autos.db</span></p>
        </div>
      </div>
      <a href="{{ url_for('index') }}" class="text-sm text-indigo-700 hover:text-indigo-900 underline decoration-2">Zurücksetzen</a>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 py-6 space-y-4">
    <!-- Filter -->
    <form method="get" class="bg-white/90 ring-1 ring-gray-200 rounded-2xl shadow-sm p-4 md:p-5">
      <div class="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-4 gap-4">
        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Suche (Titel)</span>
          <input name="q" value="{{ params.q }}" placeholder="z. B. Polo, Yaris, Octavia"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <div class="grid grid-cols-2 gap-3">
          <label class="space-y-1">
            <span class="text-xs font-medium text-gray-600">Preis min (€)</span>
            <input type="number" name="price_min" value="{{ params.price_min }}" min="0"
                   class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
          </label>
          <label class="space-y-1">
            <span class="text-xs font-medium text-gray-600">Preis max (€)</span>
            <input type="number" name="price_max" value="{{ params.price_max }}" min="0"
                   class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
          </label>
        </div>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Kilometer max</span>
          <input type="number" name="km_max" value="{{ params.km_max }}" min="0"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">PLZ beginnt mit</span>
          <input name="postal_prefix" value="{{ params.postal_prefix }}" placeholder="z. B. 80 oder 85"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Stadt enthält</span>
          <input name="city" value="{{ params.city }}" placeholder="z. B. München"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Min. Bilder</span>
          <input type="number" name="pics_min" value="{{ params.pics_min }}" min="0"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Neu in den letzten (Tagen)</span>
          <input type="number" name="posted_days" value="{{ params.posted_days }}" min="0"
                 class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
        </label>

        <label class="space-y-1">
          <span class="text-xs font-medium text-gray-600">Sortierung</span>
          <select name="sort" class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500">
            <option value="posted_desc" {% if params.sort=='posted_desc' %}selected{% endif %}>Neueste zuerst</option>
            <option value="price_asc"  {% if params.sort=='price_asc'  %}selected{% endif %}>Preis ↑</option>
            <option value="price_desc" {% if params.sort=='price_desc' %}selected{% endif %}>Preis ↓</option>
            <option value="km_asc"     {% if params.sort=='km_asc'     %}selected{% endif %}>Kilometer ↑</option>
            <option value="km_desc"    {% if params.sort=='km_desc'    %}selected{% endif %}>Kilometer ↓</option>
            <option value="seen_desc"  {% if params.sort=='seen_desc'  %}selected{% endif %}>Zuletzt gesehen</option>
            <option value="title_asc"  {% if params.sort=='title_asc'  %}selected{% endif %}>Titel A–Z</option>
          </select>
        </label>
      </div>
    <!-- Mobile.de-URL einfügen → Ø-Preis berechnen -->
<div class="mt-4 grid grid-cols-1 md:grid-cols-3 gap-3">
  <label class="space-y-1 md:col-span-2">
    <span class="text-xs font-medium text-gray-600">Mobile.de Such-URL</span>
    <input id="mobileUrl" placeholder="https://suchen.mobile.de/fahrzeuge/search.html?..."
           class="w-full rounded-xl border-gray-300 focus:border-indigo-500 focus:ring-indigo-500"/>
  </label>
  <div class="flex items-end">
    <button type="button" id="mobileFetchBtn"
            class="px-4 py-2 rounded-xl bg-amber-600 hover:bg-amber-700 text-white shadow-sm">
      Ø-Preis berechnen
    </button>
  </div>
</div>
<p id="mobileMsg" class="mt-2 text-xs text-gray-600"></p>

      <div class="mt-4 flex items-center justify-end gap-2">
        <a href="{{ url_for('index') }}" class="px-4 py-2 rounded-xl border border-gray-300 hover:bg-gray-50">Zurücksetzen</a>
        <button class="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-700 text-white shadow-sm">Filtern</button>
      </div>
    </form>

    <!-- Summary / Pagination -->
    <div class="flex items-center justify-between">
      <div class="text-sm text-gray-600">Treffer: <span class="font-semibold">{{ total }}</span></div>
      <nav class="flex items-center gap-2">
        {% if has_prev %}
          <a class="px-3 py-1 rounded-lg border hover:bg-gray-50" href="{{ prev_url }}">« Zurück</a>
        {% else %}
          <span class="px-3 py-1 rounded-lg border opacity-40">« Zurück</span>
        {% endif %}
        <span class="text-sm text-gray-600">Seite <span class="font-medium">{{ page }}</span></span>
        {% if has_next %}
          <a class="px-3 py-1 rounded-lg border hover:bg-gray-50" href="{{ next_url }}">Weiter »</a>
        {% else %}
          <span class="px-3 py-1 rounded-lg border opacity-40">Weiter »</span>
        {% endif %}
      </nav>
    </div>

    <!-- Results -->
    <div class="bg-white/90 ring-1 ring-gray-200 rounded-2xl shadow-sm overflow-hidden">
      <div class="overflow-auto">
        <table class="min-w-full text-sm">
         <thead class="bg-gray-50 sticky top-0 z-20">

            <tr class="text-gray-700">
              <th class="px-4 py-3 text-left">Titel</th>
              <th class="px-3 py-3 text-right">Preis</th>
              <th class="px-3 py-3 text-right">km</th>
              <th class="px-3 py-3">PLZ</th>
              <th class="px-3 py-3">Stadt</th>
              <th class="px-3 py-3">EZ</th>
              <th class="px-3 py-3">Bilder</th>
              <th class="px-3 py-3">Zeit</th>
              <th class="px-3 py-3">Link</th>
              <th class="px-3 py-3">Vergleichbar</th>
              <th class="px-3 py-3">AutoScout</th>
              <th class="px-3 py-3">Carwow</th>


            </tr>
          </thead>
          <tbody class="[&>tr:nth-child(even)]:bg-gray-50/40">
            {% for r in rows %}
<tr class="hover:bg-indigo-50/50 transition"
    data-row-id="{{ r['id'] }}"
    data-price-eur="{{ r['price_eur'] or '' }}">


    <td class="px-4 py-3">
      <div class="font-medium">{{ r['title'] or '—' }}</div>
      <div class="text-xs text-gray-500">{{ r['platform'] }} • {{ r['id'] }}</div>
    </td>
    <td class="px-3 py-3 text-right">{% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}</td>
    <td class="px-3 py-3 text-right">{% if r['km'] %}{{ '{:,}'.format(r['km']).replace(',', '.') }}{% else %}—{% endif %}</td>
    <td class="px-3 py-3">{{ r['postal_code'] or '—' }}</td>
    <td class="px-3 py-3">{{ r['city'] or '—' }}</td>
    <td class="px-3 py-3">{{ r['ez_text'] or '—' }}</td>
    <td class="px-3 py-3 text-center">
      {% if r['pics'] is not none %}
        <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-indigo-100 text-indigo-800">{{ r['pics'] }}</span>
      {% else %}—{% endif %}
    </td>
    <td class="px-3 py-3">
      {% if r['posted_at'] %}
        <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-emerald-100 text-emerald-800">{{ r['posted_at'] }}</span>
      {% else %}
        <span class="text-gray-400">—</span>
      {% endif %}
    </td>
    <td class="px-3 py-3">
      <a class="text-indigo-700 hover:text-indigo-900 underline decoration-2" href="{{ r['url'] }}" target="_blank" rel="noopener">Öffnen</a>
    </td>

    <!-- NEU: Vergleichbar – nur wenn Marke & Modell vorhanden (build_similar_search_url != "") -->
    <td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set sim = build_similar_search_url(r) %}
    {% if sim %}
      <a class="text-amber-700 hover:text-amber-900 underline decoration-2 text-xs"
         href="{{ sim }}" target="_blank" rel="noopener">
         Kleinanzeigen
      </a>
      <span class="text-gray-500 text-xs ml-1" data-sim-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>
<td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set as_url = build_autoscout_search_url(r) %}
    {% if as_url %}
      <a class="text-sky-700 hover:text-sky-900 underline decoration-2 text-xs"
         href="{{ as_url }}" target="_blank" rel="noopener">
         AutoScout
      </a>
      <span class="text-gray-500 text-xs ml-1" data-as-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>
<td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set cw_url = build_carwow_search_url(r) %}
    {% if cw_url %}
      <a class="text-fuchsia-700 hover:text-fuchsia-900 underline decoration-2 text-xs"
         href="{{ cw_url }}" target="_blank" rel="noopener">Carwow</a>
      <span class="text-gray-500 text-xs ml-1" data-cw-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>

  </tr>
{% endfor %}

            {% if not rows %}
              <tr><td colspan="9" class="px-4 py-10 text-center text-gray-500">Keine Treffer für diese Filter.</td></tr>
            {% endif %}
          </tbody>
        </table>
      </div>
    </div>

    <footer class="pt-2 text-xs text-gray-500">
  © Autoscan • Demo-UI. Verbesserungen: Deal-Score, Favoriten, CSV-Export, Dark-Mode.
</footer>

  </main>
<script>
document.addEventListener('DOMContentLoaded', () => {
  const REFRESH_MS = 30000;

  // Sichtbarer Fehlercatcher, falls JS crasht (damit ist die Seite nicht "weiß & stumm")
  window.addEventListener('error', (e) => {
    const box = document.createElement('div');
    box.textContent = 'JS-Fehler: ' + (e.message || e.toString());
    box.className = 'fixed bottom-3 left-3 max-w-[80vw] bg-red-600 text-white px-3 py-2 rounded-xl shadow z-[99999]';
    document.body.appendChild(box);
    setTimeout(() => box.remove(), 8000);
  });

  // Indikator oben rechts
  const indicator = document.createElement('div');
  indicator.textContent = 'Aktualisiere…';
  indicator.className = 'fixed top-3 right-3 px-3 py-1 rounded-lg text-xs font-medium bg-indigo-600 text-white shadow z-[9999] opacity-0 transition-opacity';
  document.body.appendChild(indicator);

  function showIndicator() { indicator.style.opacity = '1'; }
  function hideIndicator() { indicator.style.opacity = '0'; }

  async function reloadTable() {
  showIndicator();
  const params = new URLSearchParams(window.location.search);
  params.set('_', Date.now().toString()); // Cache-Buster
  const r = await fetch('/api/table?' + params.toString(), { cache: 'no-store' });
  if (r.ok) {
    const html = await r.text();
    const tbody = document.querySelector('table tbody');
    if (tbody) {
      tbody.innerHTML = html;
      initSimilarBadges(tbody); // Badges im neuen DOM befüllen
      initAutoscoutBadges(tbody);
      initCarwowBadges(tbody);
    }
  }
  hideIndicator();
}
async function initAutoscoutBadges(scope) {
  const root = scope || document;
  const els = root.querySelectorAll('[data-as-id]');
  for (const el of els) {
    const id = el.getAttribute('data-as-id');
    try {
      const r = await fetch('/api/autoscout_stats?id=' + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) { el.textContent = ''; continue; }
      const j = await r.json();
      if (!j.ok || !j.url) { el.textContent = ''; continue; }
      if (j.count > 0) {
        const avgFmt = (j.avg_price_eur || 0).toLocaleString('de-DE');
        el.textContent = `(${j.count} Stk. · Ø ${avgFmt} €)`;
      } else {
        el.textContent = '';
      }
    } catch (e) {
      el.textContent = '';
    }
  }
}

async function initSimilarBadges(scope) {
  const root = scope || document;
  const els = root.querySelectorAll('[data-sim-id]');

  for (const el of els) {
    const rowEl = el.closest('tr');
    // Preis aus einheitlichem data-price-eur
    const itemPrice = Number(rowEl?.dataset?.priceEur || '');

    try {
      const id = el.getAttribute('data-sim-id');
      const r = await fetch('/api/similar_stats?id=' + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) { el.textContent = ''; continue; }

      const j = await r.json();

      // Ohne gültige URL/Marke+Modell keine Anzeige/Färbung
      if (!j.ok || !j.url) { el.textContent = ''; continue; }

      // Badge-Text nur bei Treffern
      if (j.count > 0) {
        const avgFmt = (j.avg_price_eur || 0).toLocaleString('de-DE');
        el.textContent = `(${j.count} Stk. · Ø ${avgFmt} €)`;
      } else {
        el.textContent = '';
      }

      // Zeile färben, wenn avg & itemPrice valide
      const avg = Number(j.avg_price_eur || 0);
      if (rowEl) {
        // alte Klassen/Styles zurücksetzen
        rowEl.classList.remove('bg-emerald-50', 'bg-rose-50', '!bg-emerald-50', '!bg-rose-50');
        rowEl.style.backgroundColor = ''; // Zebra zurücksetzen, wir setzen Inline-Farbe

        if (avg > 0 && Number.isFinite(itemPrice)) {
          if (avg > itemPrice) {
            // Grün (Tailwind bg-emerald-50 entspricht rgb(236 253 245))
            rowEl.style.backgroundColor = 'rgb(236 253 245)';
          } else {
            // Rot (Tailwind bg-rose-50 entspricht rgb(255 241 242))
            rowEl.style.backgroundColor = 'rgb(255 241 242)';
          }
        }
      }
    } catch (_) {
      el.textContent = ''; // still
    }
  }
}

  async function autosync() {
    try {
      // Scraper anstupsen (8s Cooldown im Backend – ist ok)
      let changed = false;
      const s = await fetch('/api/sync', { cache: 'no-store' });
      if (s.ok) {
        const d = await s.json();
        changed = !!d.changed;
      }
      await reloadTable();

      if (changed) {
        const flash = document.createElement('div');
        flash.textContent = 'Neue Angebote gefunden';
        flash.className = 'fixed top-4 right-4 bg-emerald-600 text-white px-4 py-2 rounded-xl shadow z-[9999]';
        document.body.appendChild(flash);
        setTimeout(() => flash.remove(), 2000);
      }
    } catch (err) {
      // Fehler werden über window.error sichtbar gemacht
    }
  }
function normalizeMobilePage2(url) {
  // fügt pageNumber=2 hinzu (ohne doppelt anzuhängen)
  const u = new URL(url);
  if (!u.searchParams.has('pageNumber')) u.searchParams.set('pageNumber', '2');
  return u.toString();
}

async function fetchMobileStatsFromInput() {
  const inp = document.getElementById('mobileUrl');
  const msg = document.getElementById('mobileMsg');
  const url = (inp?.value || '').trim();
  msg.textContent = '';
  if (!url) { msg.textContent = 'Bitte eine mobile.de Such-URL einfügen.'; return; }

  try {
    const r = await fetch('/api/mobile_stats?url=' + encodeURIComponent(url), { cache: 'no-store' });
    let j = null;
    try { j = await r.json(); } catch (_) {}

    if (!r.ok || !j) {
      msg.textContent = (j && j.error) ? j.error : 'Anfrage fehlgeschlagen.';
      return;
    }

    if (j.ok) {
      if (j.count > 0) {
        msg.textContent = `Ø-Preis aus ${j.count} Treffern: ${j.avg_price_eur.toLocaleString('de-DE')} €`;
      } else {
        msg.textContent = 'Keine Preise gefunden.';
      }
    } else {
      msg.textContent = j.error || 'Fehler.';
    }
  } catch (e) {
    msg.textContent = 'Netzwerkfehler.';
  }
}

// Mobile-Button & Enter auf dem Input
document.getElementById('mobileFetchBtn')?.addEventListener('click', fetchMobileStatsFromInput);
document.getElementById('mobileUrl')?.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); fetchMobileStatsFromInput(); }
});
async function initCarwowBadges(scope) {
  const root = scope || document;
  const els = root.querySelectorAll('[data-cw-id]');
  for (const el of els) {
    const id = el.getAttribute('data-cw-id');
    try {
      const r = await fetch('/api/carwow_stats?id=' + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) { el.textContent = ''; continue; }
      const j = await r.json();
      if (j.error === 'blocked') {
        el.textContent = '(blockiert)';
        el.style.color = '#9CA3AF'; // text-gray-400
        continue;
      }
      if (!j.ok || !j.url) { el.textContent = ''; continue; }
      if (j.count > 0) {
        const avgFmt = (j.avg_price_eur || 0).toLocaleString('de-DE');
        el.textContent = `(${j.count} Stk. · Ø ${avgFmt} €)`;
      } else {
        el.textContent = '';
      }
    } catch {
      el.textContent = '';
    }
  }
}

  // Erste Ladung + Intervall
  autosync();
    initSimilarBadges();
    initAutoscoutBadges();
    initCarwowBadges();

  setInterval(autosync, REFRESH_MS);
});
</script>



</body>
</html>
"""
ROWS_ONLY_TPL = r"""
{% for r in rows %}
<tr class="hover:bg-indigo-50/50 transition"
    data-row-id="{{ r['id'] }}"
    data-price-eur="{{ r['price_eur'] or '' }}">



  <td class="px-4 py-3">
    <div class="font-medium">{{ r['title'] or '—' }}</div>
    <div class="text-xs text-gray-500">{{ r['id'] }}</div>
  </td>
  <td class="px-3 py-3 text-right">{% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}</td>
  <td class="px-3 py-3 text-right">{% if r['km'] %}{{ '{:,}'.format(r['km']).replace(',', '.') }}{% else %}—{% endif %}</td>
  <td class="px-3 py-3">{{ r['postal_code'] or '—' }}</td>
  <td class="px-3 py-3">{{ r['city'] or '—' }}</td>
  <td class="px-3 py-3">{{ r['ez_text'] or '—' }}</td>
  <td class="px-3 py-3 text-center">{{ r['pics'] if r['pics'] is not none else '—' }}</td>
  <td class="px-3 py-3">{{ r['posted_at'] or '—' }}</td>
  <td class="px-3 py-3"><a href="{{ r['url'] }}" target="_blank" class="text-indigo-600 underline">Öffnen</a></td>
<td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set sim = build_similar_search_url(r) %}
    {% if sim %}
      <a href="{{ sim }}" target="_blank" class="text-amber-700 underline text-xs">Kleinanzeigen</a>
      <span class="text-gray-500 text-xs ml-1" data-sim-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>

<td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set as_url = build_autoscout_search_url(r) %}
    {% if as_url %}
      <a class="text-sky-700 hover:text-sky-900 underline decoration-2 text-xs"
         href="{{ as_url }}" target="_blank" rel="noopener">
         AutoScout
      </a>
      <span class="text-gray-500 text-xs ml-1" data-as-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>
<td class="px-3 py-3">
  {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
    {% set cw_url = build_carwow_search_url(r) %}
    {% if cw_url %}
      <a class="text-fuchsia-700 hover:text-fuchsia-900 underline decoration-2 text-xs"
         href="{{ cw_url }}" target="_blank" rel="noopener">Carwow</a>
      <span class="text-gray-500 text-xs ml-1" data-cw-id="{{ r['id'] }}">(lädt…)</span>
    {% endif %}
  {% endif %}
</td>


</tr>
{% endfor %}
"""



# --- NEU: API-Endpoint, der den Scraper anstößt ---
import threading
from time import monotonic
_sync_lock = threading.Lock()
_last_sync_ts = 0.0

@app.get("/api/sync")
def api_sync():
    """
    Triggert einen leichten Sync. Schutz gegen Doppelaufrufe (Lock + 8s Cooldown).
    Antwort: {ok: bool, seen: int, stored: int, changed: bool}
    """
    global _last_sync_ts
    now = monotonic()
    if (now - _last_sync_ts) < 8.0:  # kleiner Cooldown
        return {"ok": True, "seen": 0, "stored": 0, "changed": False}

    if not _sync_lock.acquire(blocking=False):
        return {"ok": True, "seen": 0, "stored": 0, "changed": False}

    try:
        # init DB sicherstellen
        from db import init_db
        init_db()

        # einmal scrapen
        from scrape_ebay import sync_once
        res = sync_once()
        _last_sync_ts = monotonic()
        changed = (res.get("stored", 0) > 0)
        return {"ok": True, **res, "changed": changed}
    finally:
        _sync_lock.release()

# --- ÄHNLICHE-ANGEBOTE: Stats (Count + Ø-Preis) ------------------------------
from scrape_ebay import crawl_search_page  # nutzt dein vorhandenes Parsing

def _page2_url(u: str) -> str:
    # fügt "seite:2/" nach /s-autos/ oder nach der Marken-Slug-Position ein
    # Beispiele:
    # https://.../s-autos/mercedes_benz/c216+...  -> /s-autos/mercedes_benz/seite:2/c216+...
    # https://.../s-autos/c216+...                -> /s-autos/seite:2/c216+...
    import re
    if "/seite:" in u:
        return u  # schon paginiert
    return re.sub(r"(/s-autos/[^/]+/|/s-autos/)", r"\1seite:2/", u, count=1)

# --- KLEINANZEIGEN: Count (+ Ø-Preis auf die ersten N der SRP) ---------------
import re as _re
import html as _html
import requests as _req

_KA_BREADCRUMB_COUNT_RE = _re.compile(
    r'<span[^>]*class=["\']breadcrump-summary["\'][^>]*>.*?von\s*([0-9][0-9\.\s]*)\s+[A-Za-zÄÖÜäöüß]',
    _re.IGNORECASE | _re.DOTALL
)
_KA_PRICE_P_RE = _re.compile(
    r'<p[^>]*class=["\']aditem-main--middle--price-shipping--price["\'][^>]*>(?P<inner>.*?)</p>',
    _re.IGNORECASE | _re.DOTALL
)
_KA_EURO_NUM_RE = _re.compile(r'(\d{1,3}(?:[.\s]\d{3})*|\d+)')

def _ka_extract_count(html: str) -> int:
    if not html:
        return 0
    m = _KA_BREADCRUMB_COUNT_RE.search(html)
    if not m:
        return 0
    raw = (m.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
    try:
        return int(raw)
    except Exception:
        return 0

def _ka_extract_prices(html: str):
    """Liest alle sichtbaren Listenpreise aus den SRP-Karten (<p class="...price...">)."""
    out = []
    if not html:
        return out
    for m in _KA_PRICE_P_RE.finditer(html):
        inner = _html.unescape(m.group('inner') or '')
        inner = _re.sub(r'<[^>]+>', ' ', inner)
        m2 = _KA_EURO_NUM_RE.search(inner)
        if not m2:
            continue
        num = (m2.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
        try:
            val = int(num)
            if 500 <= val <= 300000:
                out.append(val)
        except Exception:
            pass
    return out

@app.get("/api/similar_stats")
def api_similar_stats():
    lid = request.args.get("id")
    if not lid:
        return {"ok": False, "error": "missing id"}, 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "not found"}, 404

    brand = (row["brand"] or "").strip()
    model = (row["model"] or "").strip()
    if not brand or not model:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    url = build_similar_search_url(row)
    if not url:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    return fetch_ka_stats(url), 200



    # --- AUTOSCOUT: Stats (Count + Ø-Preis) --------------------------------------
def _parse_prices_from_html_as(html: str):
    """
    Extrahiert NUR die regulären Listenpreise:
      <p data-testid="regular-price" ...>€ 11.990</p>
    und begrenzt auf die Anzahl aus:
      <h1 data-testid="list-header-title">3 Angebote …</h1>
    """
    if not html:
        return []

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
        try:
            limit = int(m_head.group(1))
        except Exception:
            limit = None

    prices = []
    for m in p_tag_re.finditer(html):
        inner = m.group(1) or ''
        m2 = euro_re.search(inner)
        if not m2:
            continue
        raw = m2.group(1)
        try:
            val = int(raw.replace('.', '').replace(' ', ''))
            if 500 <= val <= 300000:
                prices.append(val)
        except Exception:
            pass

        if isinstance(limit, int) and limit > 0 and len(prices) >= limit:
            break

    return prices



    # 2) Zielgerichtete HTML-Patterns (häufige Preis-Container in Listing-Karten)
    # Hinweis: AutoScout kann Klassen/IDs ändern; wir matchen bewusst tolerant.
    card_price_patterns = [
        r'data-testid="[^"]*(?:(?:result|listing).?price)[^"]*"[^>]*>([^<€]*)([\d\.\s]{2,})(?:\s*€)',
        r'class="[^"]*price[^"]*"[^>]*>(?:[^<€]*)([\d\.\s]{2,})(?:\s*€)',
        r'itemprop="price"[^>]*content="(\d+)"',  # strukturierte Microdata
    ]
    for pat in card_price_patterns:
        for m in re.finditer(pat, html, re.IGNORECASE):
            # nimm letzte Gruppe, die Zahlen enthält
            g = [x for x in m.groups() if x and re.search(r'\d', x)]
            if not g: continue
            raw = g[-1]
            raw = raw.replace('.', '').replace(' ', '').replace('\xa0', '')
            _add(raw)

    if prices:
        return prices

    # 3) Strenger Fallback: €-Regex mit Kontexthürden (kein mtl./Monat/Finanzierung/'ab')
    euro_re = re.compile(r'(\d{1,3}(?:[\.\s]\d{3})*|\d+)\s*€', re.IGNORECASE)
    for m in euro_re.finditer(html or ''):
        start = max(0, m.start()-30)
        ctx = (html[start:m.start()] or '').lower()
        # harte Filter: Monatsraten, Finanzierung, Leasing, "ab", "mtl"
        bad_ctx = ('monat', 'mtl', '/mo', 'finanz', 'leasing', 'rate', 'ab ')
        if any(b in ctx for b in bad_ctx):
            continue
        raw = m.group(1).replace('.', '').replace(' ', '')
        _add(raw)

    return prices



@app.get("/api/autoscout_stats")
def api_autoscout_stats():
    lid = request.args.get("id")
    if not lid:
        return {"ok": False, "error": "missing id"}, 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "not found"}, 404

    if not (row["brand"] or "").strip() or not (row["model"] or "").strip():
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    url = build_autoscout_search_url(row)
    if not url:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    return fetch_autoscout_stats(url), 200



# --- MOBILE.DE: Stats (Count + Ø-Preis aus einer Such-URL) -------------------
import re
import html as _html
import urllib.parse
import requests



def _is_mobile_search_url(u: str) -> bool:
    if not u:
        return False
    u = u.lower()
    return ("suchen.mobile.de/fahrzeuge/search.html" in u) or ("m.mobile.de/auto/search.html" in u)

def _page2_mobile(u: str) -> str:
    try:
        pr = urllib.parse.urlparse(u)
        qs = urllib.parse.parse_qs(pr.query, keep_blank_values=True)
        qs["pageNumber"] = ["2"]
        new_qs = urllib.parse.urlencode({k: v if isinstance(v, list) else [v] for k, v in qs.items()}, doseq=True)
        return urllib.parse.urlunparse((pr.scheme, pr.netloc, pr.path, pr.params, new_qs, pr.fragment))
    except Exception:
        return u


_MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
    "Referer": "https://suchen.mobile.de/",
}

def _fetch_text_requests(u: str, timeout: float = 12.0) -> str:
    s = requests.Session()
    s.headers.update(_MOBILE_HEADERS)
    r = s.get(u, timeout=timeout, allow_redirects=True)
    # selbst bei 403 den Body zurückgeben (Captcha/Block -> Count bleibt 0)
    return r.text or ""

# Strikte Selektoren nach deinem Beispiel:
#   <h1 class="..." data-testid="srp-title">61 … Angebote</h1>
#   <h1 data-testid="srp-title">61<!-- --> … Angebote</h1>
_MOBILE_SRP_COUNT_RE = re.compile(
    r'<h1[^>]*data-testid=[\'"]srp-title[\'"][^>]*>.*?([0-9][0-9\.\s]*)\s*(?:<!--.*?-->)*\s*(?:[A-Za-zÄÖÜäöüß0-9\.\-\s]*?)\s*(?:<!--.*?-->)*\s*Angebot',
    re.IGNORECASE | re.DOTALL
)

#   Stickybar: <div data-testid="srp-save-search-sticky-bar"> … >61<!-- --> Angebote</div>
_MOBILE_STICKY_COUNT_RE = re.compile(
    r'data-testid=[\'"]srp-save-search-sticky-bar[\'"][^>]*>.*?>\s*([0-9][0-9\.\s]*)\s*(?:<!--.*?-->)*\s*Angebot',
    re.IGNORECASE | re.DOTALL
)

#   Generischer Fallback irgendwo im HTML
_MOBILE_GENERIC_COUNT_RE = re.compile(
    r'([0-9][0-9\.\s]*)\s*(?:<!--.*?-->)*\s*Angebot(?:e)?\b',
    re.IGNORECASE | re.DOTALL
)

#   <div data-testid="main-price-label"> … <span data-testid="price-label">5.750&nbsp;€</span> … </div>
_MOBILE_PRICE_BLOCK_RE = re.compile(
    r'<div[^>]*data-testid=["\']main-price-label["\'][^>]*>(?P<block>.*?)</div>',
    re.IGNORECASE | re.DOTALL
)
_MOBILE_PRICE_SPAN_RE = re.compile(
    r'<span[^>]*data-testid=["\']price-label["\'][^>]*>(?P<val>.*?)</span>',
    re.IGNORECASE | re.DOTALL
)
_MOBILE_EURO_NUM_RE = re.compile(r'(\d{1,3}(?:[.\s]\d{3})*|\d+)', re.IGNORECASE)

def _extract_srp_count_mobile(html_text: str) -> int:
    def _norm(s: str) -> int:
        try:
            return int((s or '0').replace('.', '').replace(' ', '').replace('\xa0', ''))
        except Exception:
            return 0

    if not html_text:
        return 0

    # 1) Primär: H1 mit data-testid="srp-title"
    m = _MOBILE_SRP_COUNT_RE.search(html_text)
    if m:
        n = _norm(m.group(1))
        if n > 0:
            return n

    # 2) Stickybar
    m = _MOBILE_STICKY_COUNT_RE.search(html_text)
    if m:
        n = _norm(m.group(1))
        if n > 0:
            return n

    # 3) Generischer Fallback
    m = _MOBILE_GENERIC_COUNT_RE.search(html_text)
    if m:
        n = _norm(m.group(1))
        if n > 0:
            return n

    return 0

@app.get("/api/mobile_count")
def api_mobile_count():
    url = request.args.get("url", "").strip()
    if not _is_mobile_search_url(url):
        return {"ok": False, "error": "Bitte eine gültige mobile.de Such-URL übergeben.", "count": 0}, 200
    try:
        html1 = _fetch_text_requests(url)
        count = _extract_srp_count_mobile(html1)

        # Falls 0 → zweite Seite auch mal laden (manche Varianten zeigen Header erst dort konsistent)
        if count == 0:
            html2 = _fetch_text_requests(_page2_mobile(url))
            count = _extract_srp_count_mobile(html2)

        return {"ok": True, "count": count, "url": url}, 200
    except requests.RequestException as e:
        return {"ok": False, "error": f"Netzwerkfehler zu mobile.de ({type(e).__name__})", "count": 0}, 200
    except Exception as e:
        return {"ok": False, "error": f"Fehler beim Abruf/Parsing ({type(e).__name__})", "count": 0}, 200



def _extract_prices_mobile(html_text: str):
    """Extrahiert Preise aus der SRP.
    Primär: <span data-testid="price-label">…</span>
    Fallback: Preise innerhalb von <div data-testid="main-price-label">…</div>
    """
    prices = []
    if not html_text:
        return prices

    # 1) Direkt alle <span data-testid="price-label">…</span>
    for s in _MOBILE_PRICE_SPAN_RE.finditer(html_text):
        raw = s.group('val') or ''
        raw = _html.unescape(raw)              # <- konsistent den Alias benutzen
        raw = re.sub(r'<[^>]+>', '', raw)
        m = _MOBILE_EURO_NUM_RE.search(raw)
        if not m:
            continue
        num = (m.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
        try:
            val = int(num)
            if 500 <= val <= 500000:
                prices.append(val)
        except Exception:
            pass

    if prices:
        return prices

    # 2) Fallback: komplette Blöcke main-price-label parsen
    for blk in _MOBILE_PRICE_BLOCK_RE.finditer(html_text):
        inner = blk.group('block') or ''
        inner = _html.unescape(inner)          # <- ebenfalls Alias
        inner = re.sub(r'<[^>]+>', ' ', inner)
        m = _MOBILE_EURO_NUM_RE.search(inner)
        if not m:
            continue
        num = (m.group(1) or '').replace('.', '').replace(' ', '').replace('\xa0', '')
        try:
            val = int(num)
            if 500 <= val <= 500000:
                prices.append(val)
        except Exception:
            pass

    return prices


@app.get("/api/mobile_stats")
def api_mobile_stats():
    url = request.args.get("url", "").strip()
    if not _is_mobile_search_url(url):
        # WICHTIG: 200 mit ok=False zurückgeben, damit das Frontend den Text zeigt
        return {"ok": False, "error": "Bitte eine gültige mobile.de Such-URL übergeben."}, 200

    try:
        html1 = _fetch_text_requests(url)
        announced = _extract_srp_count_mobile(html1)
        prices = _extract_prices_mobile(html1)

        if announced and len(prices) < announced:
            html2 = _fetch_text_requests(_page2_mobile(url))
            prices += _extract_prices_mobile(html2)
            if announced > 0:
                prices = prices[:announced]

        count = len(prices)
        avg = int(round(sum(prices) / count)) if count else 0

        # Falls mobile.de eine Bot-/Captcha-Seite lieferte → keine Preise gefunden
        if count == 0 and announced > 0:
            return {"ok": False, "error": "Konnte keine Preise aus der Seite lesen (möglicher Bot-Schutz)."}, 200

        return {"ok": True, "count": count, "avg_price_eur": avg, "url": url}, 200

    except requests.RequestException as e:
        return {"ok": False, "error": f"Netzwerkfehler zu mobile.de ({type(e).__name__})"}, 200
    except Exception as e:
        return {"ok": False, "error": f"Fehler beim Abruf/Parsing ({type(e).__name__})"}, 200

@app.get("/api/carwow_stats")
def api_carwow_stats():
    lid = request.args.get("id")
    if not lid:
        return {"ok": False, "error": "missing id"}, 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "not found"}, 404

    # Marken/Modell Pflicht
    if not (row["brand"] or "").strip() or not (row["model"] or "").strip():
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    url = build_carwow_search_url(row)
    if not url:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    return fetch_carwow_stats(url), 200

@app.get("/api/carwow_stats_url")
def api_carwow_stats_url():
    url = request.args.get("url", "").strip()
    if not url:
        return {"ok": False, "error": "missing url"}, 400

    from providers.carwow_stats import fetch_carwow_stats
    res = fetch_carwow_stats(url)
    return res, 200


if __name__ == "__main__":
    print(f"[i] Datenbank: {DB_PATH}")
    print("[i] Start auf http://127.0.0.1:5000/")
    app.run(debug=True)
