# app.py — Mobile-First Web-UI für autos.db (Redesign)
# Start: python3 app.py  →  http://127.0.0.1:5000/
import os
import sqlite3
import json, time
from pywebpush import webpush, WebPushException
from py_vapid import Vapid
from flask import Flask, request, redirect, url_for, render_template_string
from providers.links import build_autoscout_search_url, build_similar_search_url
from providers.ka_stats import fetch_ka_stats
from providers.autoscout_stats import fetch_autoscout_stats
from providers.carwow_stats import fetch_carwow_stats, build_carwow_search_url
from flask import send_from_directory, make_response

VAPID_PUBLIC = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_PEM = os.environ.get("VAPID_PRIVATE_KEY_PEM", "")
_pem_file = os.environ.get("VAPID_PRIVATE_KEY_PEM_FILE", "")
if _pem_file and os.path.exists(_pem_file):
    with open(_pem_file) as _f:
        VAPID_PRIVATE_PEM = _f.read().strip()
PUSH_SUBJECT = os.environ.get("PUSH_SUBJECT", "mailto:noreply@example.com")

APP_TITLE = "Autoscan"
DB_PATH = os.environ.get("AUTOS_DB", "autos.db")
PER_PAGE_DEFAULT = 50

app = Flask(__name__)

import logging, traceback, sys
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    tb = traceback.format_exc()
    app.logger.error(f"Unhandled exception: {e}\n{tb}")
    return "Internal Server Error", 500

app.jinja_env.globals['build_autoscout_search_url'] = build_autoscout_search_url
app.jinja_env.globals['build_similar_search_url']  = build_similar_search_url
app.jinja_env.globals['build_carwow_search_url']   = build_carwow_search_url

@app.route("/manifest.webmanifest")
def manifest():
    resp = make_response(send_from_directory("static", "manifest.webmanifest"))
    resp.headers["Content-Type"] = "application/manifest+json"
    return resp

@app.route("/sw.js")
def sw():
    resp = make_response(send_from_directory("static", "sw.js"))
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

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

KA_AREA_SLUG  = os.environ.get("KA_AREA_SLUG", "bayern")
KA_AREA_CODE  = os.environ.get("KA_AREA_CODE", "l5510")
KA_RADIUS_KM  = int(os.environ.get("KA_RADIUS", "100"))


def init_db():
    try:
        print(f"[i] DB_PATH = {DB_PATH}, exists = {os.path.exists(DB_PATH)}, cwd = {os.getcwd()}", file=sys.stderr, flush=True)
        conn = get_db(); cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS listings(
            id TEXT PRIMARY KEY,
            title TEXT,
            price_eur INTEGER,
            km INTEGER,
            postal_code TEXT,
            city TEXT,
            posted_at TEXT,
            pics INTEGER,
            url TEXT,
            platform TEXT,
            last_seen TEXT,
            ez_text TEXT,
            brand TEXT,
            model TEXT,
            fuel TEXT,
            gearbox TEXT,
            first_reg TEXT,
            description TEXT,
            image_urls_json TEXT
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS listing_prices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id TEXT,
            price_eur INTEGER,
            seen_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions(
            id INTEGER PRIMARY KEY,
            endpoint TEXT UNIQUE,
            p256dh TEXT, auth TEXT,
            filters TEXT,
            max_price INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS push_sent(
            endpoint TEXT NOT NULL,
            listing_id TEXT NOT NULL,
            sent_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (endpoint, listing_id)
        )""")
        # --- Favorites ---
        cur.execute("""CREATE TABLE IF NOT EXISTS favorites(
            listing_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'interessant',
            note TEXT DEFAULT '',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        # --- Deal Score Cache ---
        cur.execute("""CREATE TABLE IF NOT EXISTS deal_scores(
            listing_id TEXT PRIMARY KEY,
            score INTEGER DEFAULT 0,
            ka_avg INTEGER,
            as_avg INTEGER,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        # --- Mobile.de Preis-Cache (vom Bookmarklet befüllt) ---
        cur.execute("""CREATE TABLE IF NOT EXISTS mobile_price_cache(
            url_hash TEXT PRIMARY KEY,
            search_url TEXT,
            count INTEGER,
            avg_price INTEGER,
            prices_json TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.commit(); conn.close()
        print("[i] Datenbank-Tabellen initialisiert", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[!] DB-Init Fehler: {e}", file=sys.stderr, flush=True)
init_db()


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

    if params.get("q"):
        where.append("title LIKE ?")
        args.append(f"%{params['q']}%")

    pmin = parse_int(params.get("price_min"))
    pmax = parse_int(params.get("price_max"))
    if pmin is not None:
        where.append("price_eur >= ?")
        args.append(pmin)
    if pmax is not None:
        where.append("price_eur <= ?")
        args.append(pmax)

    ez_min = parse_int(params.get("ez_min"))
    ez_max = parse_int(params.get("ez_max"))
    if ez_min is not None or ez_max is not None:
        ez_year_sql = """
        CASE
          WHEN first_reg IS NOT NULL AND length(first_reg) >= 4 THEN
            CASE
              WHEN substr(first_reg, 5, 1) = '-' THEN CAST(substr(first_reg, 1, 4) AS INTEGER)
              ELSE CAST(substr(first_reg, length(first_reg)-3, 4) AS INTEGER)
            END
          WHEN ez_text IS NOT NULL AND length(ez_text) >= 4 THEN
            CAST(substr(ez_text, length(ez_text)-3, 4) AS INTEGER)
          ELSE NULL
        END
        """
        if ez_min is not None:
            where.append(f"({ez_year_sql}) >= ?")
            args.append(ez_min)
        if ez_max is not None:
            where.append(f"({ez_year_sql}) <= ?")
            args.append(ez_max)

    km_max = parse_int(params.get("km_max"))
    if km_max is not None:
        where.append("km <= ?")
        args.append(km_max)

    plz = params.get("postal_prefix")
    if plz:
        where.append("postal_code LIKE ?")
        args.append(plz.rstrip("%") + "%")

    city = params.get("city")
    if city:
        where.append("city LIKE ?")
        args.append(f"%{city}%")

    pics_min = parse_int(params.get("pics_min"))
    if pics_min is not None:
        where.append("IFNULL(pics, 0) >= ?")
        args.append(pics_min)

    posted_days = parse_int(params.get("posted_days"))
    if posted_days is not None and posted_days >= 0:
        where.append("posted_at IS NOT NULL AND posted_at >= date('now', ?) ")
        args.append(f"-{posted_days} day")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

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
  try:
    q = request.args.get("q", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "9000")
    ez_min = request.args.get("ez_min", "2012")
    ez_max = request.args.get("ez_max", "")
    km_max = request.args.get("km_max", "100000")
    postal_prefix = request.args.get("postal_prefix", "")
    city = request.args.get("city", "")
    pics_min = request.args.get("pics_min", "")
    posted_days = request.args.get("posted_days", "")
    sort = request.args.get("sort", "posted_desc")

    page = max(parse_int(request.args.get("page"), 1) or 1, 1)
    per_page = max(parse_int(request.args.get("per_page"), PER_PAGE_DEFAULT) or PER_PAGE_DEFAULT, 1)
    offset = (page - 1) * per_page

    params = {
        "q": q, "price_min": price_min, "price_max": price_max,
        "ez_min": ez_min, "ez_max": ez_max, "km_max": km_max,
        "postal_prefix": postal_prefix, "city": city,
        "pics_min": pics_min, "posted_days": posted_days, "sort": sort,
    }

    where_sql, args, order_sql = build_query(params)

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

    def page_url(p):
        if p < 1: p = 1
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
        "VAPID_PUBLIC": VAPID_PUBLIC,
    })
  except Exception as e:
    import traceback as tb
    err = tb.format_exc()
    print(f"[!] Index route error: {e}\n{err}", file=sys.stderr, flush=True)
    return f"<h1>Fehler</h1><pre>{e}</pre>", 500

@app.get("/api/table")
def api_table():
    q = request.args.get("q", "")
    price_min = request.args.get("price_min", "")
    price_max = request.args.get("price_max", "9000")
    ez_min = request.args.get("ez_min", "2012")
    ez_max = request.args.get("ez_max", "")
    km_max = request.args.get("km_max", "100000")
    postal_prefix = request.args.get("postal_prefix", "")
    city = request.args.get("city", "")
    pics_min = request.args.get("pics_min", "")
    posted_days = request.args.get("posted_days", "")
    sort = request.args.get("sort", "posted_desc")

    params = {
        "q": q, "price_min": price_min, "price_max": price_max,
        "ez_min": ez_min, "ez_max": ez_max, "km_max": km_max,
        "postal_prefix": postal_prefix, "city": city,
        "pics_min": pics_min, "posted_days": posted_days, "sort": sort,
    }

    where_sql, args, order_sql = build_query(params)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        f"""SELECT id, title, price_eur, km, postal_code, city, posted_at, pics, url, ez_text,
                   brand, model, fuel, gearbox, first_reg
            FROM listings {where_sql} {order_sql} LIMIT ? OFFSET ?""",
        args + [50, 0],
    )
    rows = cur.fetchall()
    conn.close()

    return render_template_string(CARDS_TPL, rows=rows)

@app.get("/api/prompt")
def api_prompt():
    lid = (request.args.get("id") or "").strip()
    if not lid:
        return {"ok": False, "error": "missing id"}, 400

    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, title, price_eur, km, city, posted_at, url, ez_text,
               brand, model, fuel, gearbox, first_reg,
               description, image_urls_json
        FROM listings WHERE id = ?
    """, (lid,))
    r = cur.fetchone()
    conn.close()

    if not r:
        return {"ok": False, "error": "not found"}, 404

    image_urls = []
    try:
        if r["image_urls_json"]:
            image_urls = json.loads(r["image_urls_json"])
            if not isinstance(image_urls, list): image_urls = []
    except Exception:
        image_urls = []

    listing = {
        "title": r["title"] or "",
        "price_text": (f"{r['price_eur']} €" if r["price_eur"] is not None else "k.A."),
        "locality": r["city"] or "k.A.",
        "date_posted": r["posted_at"] or "k.A.",
        "ad_id": r["id"],
        "details": {
            "Marke": (r["brand"] or ""), "Modell": (r["model"] or ""),
            "EZ": (r["first_reg"] or r["ez_text"] or ""),
            "KM": (str(r["km"]) if r["km"] is not None else ""),
            "Kraftstoff": (r["fuel"] or ""), "Getriebe": (r["gearbox"] or ""),
        },
        "features": [], "description": r["description"] or "",
        "image_urls": image_urls,
    }

    from scrape_ebay import build_haendler_prompt
    prompt = build_haendler_prompt(listing, max_images=15)
    return {"ok": True, "prompt": prompt}, 200


# ============================================================
# TEMPLATE — Mobile-First Card Design
# ============================================================
TPL = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#0f172a">
  <title>{{ app_title }}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300..700;1,9..40,300..700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #0f172a;
      --bg-card: #1e293b;
      --bg-card-hover: #273548;
      --bg-input: #1e293b;
      --bg-sheet: #162032;
      --border: #334155;
      --border-light: #475569;
      --text: #f1f5f9;
      --text-dim: #94a3b8;
      --text-muted: #64748b;
      --accent: #38bdf8;
      --accent-hover: #7dd3fc;
      --green: #34d399;
      --green-bg: rgba(52,211,153,0.12);
      --red: #fb7185;
      --red-bg: rgba(251,113,133,0.12);
      --amber: #fbbf24;
      --amber-bg: rgba(251,191,36,0.12);
      --fuchsia: #e879f9;
      --radius: 16px;
      --radius-sm: 10px;
      --shadow: 0 4px 24px rgba(0,0,0,0.3);
      --font: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
      --mono: 'JetBrains Mono', monospace;
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }

    html { font-family: var(--font); background: var(--bg); color: var(--text); -webkit-text-size-adjust: 100%; }
    body { min-height: 100dvh; padding-bottom: calc(80px + var(--safe-bottom)); }

    /* Scrollbar */
    ::-webkit-scrollbar { width: 6px; height: 6px; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 10px; }
    ::-webkit-scrollbar-track { background: transparent; }

    /* Header */
    .app-header {
      position: sticky; top: 0; z-index: 50;
      padding: calc(var(--safe-top) + 12px) 16px 12px;
      background: rgba(15,23,42,0.85);
      backdrop-filter: blur(20px) saturate(1.4);
      -webkit-backdrop-filter: blur(20px) saturate(1.4);
      border-bottom: 1px solid var(--border);
    }
    .header-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .logo { display: flex; align-items: center; gap: 10px; }
    .logo-icon {
      width: 36px; height: 36px; border-radius: 10px;
      background: linear-gradient(135deg, #38bdf8, #818cf8);
      display: grid; place-items: center;
      font-weight: 700; font-size: 14px; color: #fff;
    }
    .logo-text { font-size: 20px; font-weight: 700; letter-spacing: -0.5px; }
    .logo-sub { font-size: 11px; color: var(--text-dim); font-weight: 400; }
    .header-actions { display: flex; gap: 8px; }
    .icon-btn {
      width: 38px; height: 38px; border-radius: 10px;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-dim); display: grid; place-items: center;
      cursor: pointer; transition: all 0.15s;
      font-size: 18px;
    }
    .icon-btn:active { transform: scale(0.93); }
    .icon-btn:hover { background: var(--bg-card-hover); color: var(--text); }

    /* Stats bar */
    .stats-bar {
      display: flex; align-items: center; gap: 12px;
      padding: 10px 16px; overflow-x: auto; -webkit-overflow-scrolling: touch;
    }
    .stat-chip {
      flex-shrink: 0; padding: 6px 14px; border-radius: 20px;
      background: var(--bg-card); border: 1px solid var(--border);
      font-size: 13px; font-weight: 500; white-space: nowrap;
      display: flex; align-items: center; gap: 6px;
    }
    .stat-chip .num { color: var(--accent); font-family: var(--mono); font-weight: 600; }

    /* Filter sheet */
    .filter-toggle {
      display: flex; align-items: center; gap: 8px;
      margin: 0 16px; padding: 12px 16px; border-radius: var(--radius);
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-weight: 500;
      cursor: pointer; transition: all 0.2s;
    }
    .filter-toggle:active { transform: scale(0.98); }
    .filter-toggle .count {
      margin-left: auto; padding: 2px 10px; border-radius: 12px;
      background: var(--accent); color: var(--bg); font-size: 12px; font-weight: 700;
    }

    .filter-sheet {
      display: none; margin: 12px 16px 0;
      padding: 20px; border-radius: var(--radius);
      background: var(--bg-sheet); border: 1px solid var(--border);
    }
    .filter-sheet.open { display: block; animation: slideDown 0.25s ease; }
    @keyframes slideDown { from { opacity:0; transform: translateY(-8px); } to { opacity:1; transform: translateY(0); } }

    .filter-grid {
      display: grid; grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .filter-grid .full { grid-column: 1 / -1; }
    .field-label { display: block; font-size: 11px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 5px; }
    .field-input {
      width: 100%; padding: 10px 14px; border-radius: var(--radius-sm);
      background: var(--bg-input); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-family: var(--font);
      transition: border-color 0.15s;
      -webkit-appearance: none; appearance: none;
    }
    .field-input:focus { outline: none; border-color: var(--accent); }
    .field-input::placeholder { color: var(--text-muted); }
    select.field-input { background-image: url("data:image/svg+xml,%3Csvg width='12' height='8' viewBox='0 0 12 8' fill='none' xmlns='http://www.w3.org/2000/svg'%3E%3Cpath d='M1 1.5L6 6.5L11 1.5' stroke='%2394a3b8' stroke-width='1.5' stroke-linecap='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 14px center; padding-right: 36px; }

    .filter-actions { display: flex; gap: 10px; margin-top: 16px; }
    .btn {
      flex: 1; padding: 12px; border-radius: var(--radius-sm);
      font-size: 14px; font-weight: 600; font-family: var(--font);
      cursor: pointer; transition: all 0.15s; border: none;
      text-align: center; text-decoration: none;
    }
    .btn:active { transform: scale(0.97); }
    .btn-primary { background: var(--accent); color: var(--bg); }
    .btn-primary:hover { background: var(--accent-hover); }
    .btn-ghost { background: transparent; border: 1px solid var(--border); color: var(--text-dim); }
    .btn-ghost:hover { border-color: var(--border-light); color: var(--text); }
    .btn-sm { padding: 8px 14px; font-size: 12px; flex: 0; }

    /* Mobile.de input row */
    .mobile-row {
      margin: 12px 16px 0; padding: 14px 16px; border-radius: var(--radius);
      background: var(--bg-card); border: 1px solid var(--border);
    }
    .mobile-row .field-label { margin-bottom: 8px; }
    .mobile-input-row { display: flex; gap: 8px; }
    .mobile-input-row input { flex: 1; }
    .mobile-msg { font-size: 12px; color: var(--text-dim); margin-top: 8px; min-height: 16px; }

    /* Card list */
    .card-list { padding: 12px 16px; display: flex; flex-direction: column; gap: 10px; }

    .listing-card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--radius); padding: 16px;
      transition: all 0.2s; position: relative; overflow: hidden;
    }
    .listing-card:active { transform: scale(0.99); }
    .listing-card.deal-good { border-left: 3px solid var(--green); background: var(--green-bg); }
    .listing-card.deal-bad { border-left: 3px solid var(--red); background: var(--red-bg); }

    .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
    .card-title { font-size: 15px; font-weight: 600; line-height: 1.3; flex: 1; }
    .card-price {
      font-family: var(--mono); font-size: 16px; font-weight: 700;
      color: var(--accent); white-space: nowrap;
    }

    .card-meta {
      display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;
    }
    .tag {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 4px 10px; border-radius: 8px;
      background: rgba(255,255,255,0.05); border: 1px solid var(--border);
      font-size: 12px; color: var(--text-dim);
      white-space: nowrap;
    }
    .tag .icon { font-size: 13px; }
    .tag-accent { color: var(--accent); border-color: rgba(56,189,248,0.3); }
    .tag-green { color: var(--green); border-color: rgba(52,211,153,0.3); }

    /* Comparison badges */
    .card-comparisons {
      display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;
    }
    .cmp-badge {
      display: inline-flex; align-items: center; gap: 5px;
      padding: 5px 10px; border-radius: 8px;
      font-size: 11px; font-weight: 600;
      text-decoration: none; transition: all 0.15s;
    }
    .cmp-badge:active { transform: scale(0.96); }
    .cmp-ka { background: rgba(251,191,36,0.12); color: var(--amber); border: 1px solid rgba(251,191,36,0.25); }
    .cmp-as { background: rgba(56,189,248,0.12); color: var(--accent); border: 1px solid rgba(56,189,248,0.25); }
    .cmp-cw { background: rgba(232,121,249,0.12); color: var(--fuchsia); border: 1px solid rgba(232,121,249,0.25); }
    .cmp-mob { background: rgba(251,146,60,0.12); color: #fb923c; border: 1px solid rgba(251,146,60,0.25); }
    .cmp-stats { font-weight: 400; opacity: 0.8; }

    /* Deal Score - inline in card-top row */
    .deal-score {
      width: 40px; height: 40px; border-radius: 50%;
      display: grid; place-items: center; flex-shrink: 0;
      font-family: var(--mono); font-size: 13px; font-weight: 700;
      border: 2px solid;
    }
    .deal-score.loading { border-color: var(--border); color: var(--text-muted); font-size: 9px; }
    .deal-score.s-great { background: rgba(52,211,153,0.2); border-color: var(--green); color: var(--green); }
    .deal-score.s-good { background: rgba(56,189,248,0.15); border-color: var(--accent); color: var(--accent); }
    .deal-score.s-ok { background: rgba(251,191,36,0.15); border-color: var(--amber); color: var(--amber); }
    .deal-score.s-bad { background: rgba(251,113,133,0.15); border-color: var(--red); color: var(--red); }

    /* Favorite button */
    .fav-btn {
      background: none; border: none; cursor: pointer;
      font-size: 20px; padding: 4px; transition: transform 0.2s;
      line-height: 1;
    }
    .fav-btn:active { transform: scale(1.3); }
    .fav-btn.saved { animation: favPop 0.3s ease; }
    @keyframes favPop { 0% { transform: scale(1); } 50% { transform: scale(1.4); } 100% { transform: scale(1); } }

    /* Fav status badge */
    .fav-status {
      display: inline-flex; align-items: center; gap: 4px;
      padding: 3px 8px; border-radius: 6px; font-size: 10px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.3px;
    }
    .fav-status.s-interessant { background: rgba(56,189,248,0.15); color: var(--accent); }
    .fav-status.s-anrufen { background: rgba(251,191,36,0.15); color: var(--amber); }
    .fav-status.s-besichtigt { background: rgba(232,121,249,0.15); color: var(--fuchsia); }
    .fav-status.s-gekauft { background: rgba(52,211,153,0.2); color: var(--green); }
    .fav-status.s-abgelehnt { background: rgba(251,113,133,0.12); color: var(--red); }

    /* Price change tag */
    .tag-price-down { color: var(--green); border-color: rgba(52,211,153,0.3); font-weight: 600; }
    .tag-price-up { color: var(--red); border-color: rgba(251,113,133,0.3); font-weight: 600; }

    /* Fav filter tabs */
    .fav-tabs {
      display: flex; gap: 6px; padding: 0 16px; margin: 20px 0 12px;
      overflow-x: auto; -webkit-overflow-scrolling: touch;
    }
    .fav-tab {
      flex-shrink: 0; padding: 6px 14px; border-radius: 20px;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-muted); font-size: 12px; font-weight: 600;
      cursor: pointer; transition: all 0.15s; white-space: nowrap;
      font-family: var(--font);
    }
    .fav-tab:active { transform: scale(0.95); }
    .fav-tab.active { background: var(--accent); color: var(--bg); border-color: var(--accent); }

    /* Card actions */
    .card-actions {
      display: flex; gap: 8px; margin-top: 12px; padding-top: 12px;
      border-top: 1px solid var(--border);
    }
    .card-btn {
      flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
      padding: 10px; border-radius: var(--radius-sm);
      background: rgba(255,255,255,0.04); border: 1px solid var(--border);
      color: var(--text-dim); font-size: 12px; font-weight: 600;
      text-decoration: none; cursor: pointer; transition: all 0.15s;
      font-family: var(--font);
    }
    .card-btn:active { transform: scale(0.96); }
    .card-btn:hover { background: rgba(255,255,255,0.08); color: var(--text); }
    .card-btn-accent { color: var(--accent); border-color: rgba(56,189,248,0.3); }

    /* Pagination */
    .pagination {
      display: flex; align-items: center; justify-content: center; gap: 12px;
      padding: 16px; margin-bottom: 20px;
    }
    .page-btn {
      padding: 10px 20px; border-radius: var(--radius-sm);
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text); font-size: 14px; font-weight: 500;
      text-decoration: none; transition: all 0.15s;
    }
    .page-btn:active { transform: scale(0.96); }
    .page-btn.disabled { opacity: 0.3; pointer-events: none; }
    .page-info { font-size: 13px; color: var(--text-dim); }

    /* Bottom nav (iOS style) */
    .bottom-nav {
      position: fixed; bottom: 0; left: 0; right: 0;
      z-index: 50;
      padding: 8px 16px calc(var(--safe-bottom) + 8px);
      background: rgba(15,23,42,0.92);
      backdrop-filter: blur(20px) saturate(1.4);
      -webkit-backdrop-filter: blur(20px) saturate(1.4);
      border-top: 1px solid var(--border);
      display: flex; justify-content: space-around; gap: 4px;
    }
    .nav-item {
      display: flex; flex-direction: column; align-items: center; gap: 2px;
      padding: 6px 12px; border-radius: 10px;
      color: var(--text-muted); font-size: 10px; font-weight: 600;
      cursor: pointer; transition: all 0.15s;
      background: none; border: none; font-family: var(--font);
    }
    .nav-item:active { transform: scale(0.92); }
    .nav-item.active { color: var(--accent); }
    .nav-icon { font-size: 22px; }

    /* Modals */
    .modal-overlay {
      display: none; position: fixed; inset: 0; z-index: 100;
      background: rgba(0,0,0,0.6);
      backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px);
    }
    .modal-overlay.open { display: flex; align-items: flex-end; justify-content: center; animation: fadeIn 0.2s; }
    @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

    .modal-sheet {
      width: 100%; max-width: 560px; max-height: 85dvh;
      background: var(--bg-sheet); border-radius: var(--radius) var(--radius) 0 0;
      overflow: hidden; display: flex; flex-direction: column;
      animation: slideUp 0.3s cubic-bezier(0.32, 0.72, 0, 1);
      padding-bottom: var(--safe-bottom);
    }
    @keyframes slideUp { from { transform: translateY(100%); } to { transform: translateY(0); } }

    .modal-handle {
      width: 36px; height: 4px; border-radius: 2px;
      background: var(--border-light); margin: 10px auto;
    }
    .modal-header {
      display: flex; align-items: center; justify-content: space-between;
      padding: 0 20px 16px; border-bottom: 1px solid var(--border);
    }
    .modal-title { font-size: 17px; font-weight: 700; }
    .modal-close {
      width: 32px; height: 32px; border-radius: 50%;
      background: var(--bg-card); border: 1px solid var(--border);
      color: var(--text-dim); font-size: 18px;
      display: grid; place-items: center; cursor: pointer;
    }
    .modal-body { flex: 1; overflow-y: auto; -webkit-overflow-scrolling: touch; padding: 20px; }
    .modal-footer { padding: 16px 20px; border-top: 1px solid var(--border); display: flex; gap: 10px; }

    /* Prompt textarea */
    .prompt-area {
      width: 100%; min-height: 200px; padding: 14px;
      background: var(--bg-input); border: 1px solid var(--border);
      border-radius: var(--radius-sm); color: var(--text);
      font-family: var(--mono); font-size: 12px; line-height: 1.5;
      resize: vertical;
    }
    .prompt-area:focus { outline: none; border-color: var(--accent); }

    /* Push subscription items */
    .push-item {
      padding: 14px; border-radius: var(--radius-sm);
      background: var(--bg-card); border: 1px solid var(--border);
      margin-bottom: 10px;
    }
    .push-item-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
    .push-item-val { font-size: 13px; color: var(--text-dim); word-break: break-all; }

    /* Toast */
    .toast {
      position: fixed; top: calc(var(--safe-top) + 70px); left: 50%; transform: translateX(-50%);
      padding: 10px 20px; border-radius: 12px;
      font-size: 13px; font-weight: 600; z-index: 200;
      animation: toastIn 0.3s ease, toastOut 0.3s ease 2.5s forwards;
      pointer-events: none;
    }
    .toast-success { background: var(--green); color: var(--bg); }
    .toast-info { background: var(--accent); color: var(--bg); }
    .toast-error { background: var(--red); color: #fff; }
    @keyframes toastIn { from { opacity:0; transform: translateX(-50%) translateY(-12px); } to { opacity:1; transform: translateX(-50%) translateY(0); } }
    @keyframes toastOut { to { opacity: 0; transform: translateX(-50%) translateY(-12px); } }

    /* Loading indicator */
    .sync-indicator {
      position: fixed; top: calc(var(--safe-top) + 4px); right: 16px;
      z-index: 60; padding: 6px 12px; border-radius: 8px;
      background: var(--accent); color: var(--bg);
      font-size: 11px; font-weight: 700;
      opacity: 0; transition: opacity 0.2s;
    }
    .sync-indicator.active { opacity: 1; }

    /* Empty state */
    .empty-state {
      text-align: center; padding: 48px 24px; color: var(--text-muted);
    }
    .empty-state .icon { font-size: 48px; margin-bottom: 12px; }
    .empty-state .msg { font-size: 15px; font-weight: 500; }

    /* iOS hint */
    .ios-hint {
      margin: 12px 16px; padding: 12px 16px;
      border-radius: var(--radius-sm);
      background: rgba(56,189,248,0.1); border: 1px solid rgba(56,189,248,0.2);
      font-size: 12px; color: var(--accent);
    }

    /* Desktop: slightly wider cards */
    @media (min-width: 768px) {
      .card-list { max-width: 680px; margin: 0 auto; }
      .stats-bar { justify-content: center; }
      .modal-sheet { border-radius: var(--radius); margin-bottom: 20px; max-height: 80dvh; }
      .modal-overlay.open { align-items: center; }
      body { padding-bottom: 0; }
      .bottom-nav { display: none; }
    }
  </style>
</head>
<body>

<!-- Sync indicator -->
<div class="sync-indicator" id="syncIndicator">Synchronisiere…</div>

<!-- Header -->
<header class="app-header">
  <div class="header-row">
    <div class="logo">
      <div class="logo-icon">AS</div>
      <div>
        <div class="logo-text">{{ app_title }}</div>
        <div class="logo-sub">Gebrauchtwagen-Scanner</div>
      </div>
    </div>
    <div class="header-actions">
      <a class="icon-btn" href="/bookmarklet" title="Bookmarklet installieren" style="text-decoration:none">📱</a>
      <button class="icon-btn" id="pushBtn" title="Push-Abo">🔔</button>
      <button class="icon-btn" id="pushManageBtn" title="Abos verwalten">⚙️</button>
    </div>
  </div>
</header>

<!-- Stats bar -->
<div class="stats-bar">
  <div class="stat-chip"><span class="num">{{ total }}</span> Treffer</div>
  <div class="stat-chip">Seite <span class="num">{{ page }}</span></div>
  <div class="stat-chip" id="favCount">⭐ —</div>
  <div class="stat-chip" id="lastSyncChip">⏳ Sync…</div>
</div>

<!-- Fav filter tabs -->
<div class="fav-tabs" id="favTabs">
  <button class="fav-tab active" data-fav-filter="all">Alle</button>
  <button class="fav-tab" data-fav-filter="favs">⭐ Favoriten</button>
  <button class="fav-tab" data-fav-filter="interessant">Interessant</button>
  <button class="fav-tab" data-fav-filter="anrufen">Anrufen</button>
  <button class="fav-tab" data-fav-filter="besichtigt">Besichtigt</button>
  <button class="fav-tab" data-fav-filter="gekauft">Gekauft</button>
</div>

<!-- Filter Toggle -->
<div class="filter-toggle" id="filterToggle" onclick="toggleFilters()">
  <span>🔍</span>
  <span>Filter &amp; Suche</span>
  {% set active_count = 0 %}
  {% if params.q %}{% set active_count = active_count + 1 %}{% endif %}
  {% if params.price_min %}{% set active_count = active_count + 1 %}{% endif %}
  {% if params.price_max %}{% set active_count = active_count + 1 %}{% endif %}
  {% if params.ez_min %}{% set active_count = active_count + 1 %}{% endif %}
  {% if params.km_max %}{% set active_count = active_count + 1 %}{% endif %}
  <span class="count" id="filterCount">{{ active_count }}</span>
</div>

<!-- Filter Sheet -->
<form method="get" id="filterForm">
<div class="filter-sheet" id="filterSheet">
  <div class="filter-grid">
    <div class="full">
      <label class="field-label">Suche im Titel</label>
      <input class="field-input" name="q" value="{{ params.q }}" placeholder="z.B. Polo, Yaris, Golf…">
    </div>
    <div>
      <label class="field-label">Preis min €</label>
      <input class="field-input" type="number" name="price_min" value="{{ params.price_min }}" min="0">
    </div>
    <div>
      <label class="field-label">Preis max €</label>
      <input class="field-input" type="number" name="price_max" value="{{ params.price_max }}" min="0">
    </div>
    <div>
      <label class="field-label">EZ min (Jahr)</label>
      <input class="field-input" type="number" name="ez_min" value="{{ params.ez_min }}" placeholder="2013">
    </div>
    <div>
      <label class="field-label">EZ max (Jahr)</label>
      <input class="field-input" type="number" name="ez_max" value="{{ params.ez_max }}" placeholder="2025">
    </div>
    <div>
      <label class="field-label">km max</label>
      <input class="field-input" type="number" name="km_max" value="{{ params.km_max }}" min="0">
    </div>
    <div>
      <label class="field-label">PLZ Prefix</label>
      <input class="field-input" name="postal_prefix" value="{{ params.postal_prefix }}" placeholder="85">
    </div>
    <div>
      <label class="field-label">Stadt</label>
      <input class="field-input" name="city" value="{{ params.city }}" placeholder="München">
    </div>
    <div>
      <label class="field-label">Min. Bilder</label>
      <input class="field-input" type="number" name="pics_min" value="{{ params.pics_min }}" min="0">
    </div>
    <div>
      <label class="field-label">Letzte Tage</label>
      <input class="field-input" type="number" name="posted_days" value="{{ params.posted_days }}" min="0">
    </div>
    <div class="full">
      <label class="field-label">Sortierung</label>
      <select name="sort" class="field-input">
        <option value="posted_desc" {% if params.sort=='posted_desc' %}selected{% endif %}>Neueste zuerst</option>
        <option value="price_asc"  {% if params.sort=='price_asc'  %}selected{% endif %}>Preis ↑</option>
        <option value="price_desc" {% if params.sort=='price_desc' %}selected{% endif %}>Preis ↓</option>
        <option value="km_asc"     {% if params.sort=='km_asc'     %}selected{% endif %}>Kilometer ↑</option>
        <option value="km_desc"    {% if params.sort=='km_desc'    %}selected{% endif %}>Kilometer ↓</option>
        <option value="seen_desc"  {% if params.sort=='seen_desc'  %}selected{% endif %}>Zuletzt gesehen</option>
        <option value="title_asc"  {% if params.sort=='title_asc'  %}selected{% endif %}>Titel A–Z</option>
      </select>
    </div>
  </div>
  <div class="filter-actions">
    <a href="/" class="btn btn-ghost">Zurücksetzen</a>
    <button type="submit" class="btn btn-primary">Anwenden</button>
  </div>
</div>
</form>

<!-- Mobile.de -->
<div class="mobile-row">
  <label class="field-label">Mobile.de Ø-Preis</label>
  <div class="mobile-input-row">
    <input class="field-input" id="mobileUrl" placeholder="Such-URL einfügen…">
    <button class="btn btn-primary btn-sm" type="button" id="mobileFetchBtn">Ø</button>
  </div>
  <div class="mobile-msg" id="mobileMsg"></div>
</div>

<!-- iOS hint -->
<div id="iosHint" class="ios-hint" style="display:none">
  💡 Für Push-Benachrichtigungen: Seite zum Home-Bildschirm hinzufügen (Teilen → „Zum Home-Bildschirm")
</div>

<!-- Card List -->
<div class="card-list" id="cardList">
  {% for r in rows %}
  <div class="listing-card"
       data-row-id="{{ r['id'] }}"
       data-price-eur="{{ r['price_eur'] or '' }}">

    <div class="card-top">
      <button class="fav-btn" data-fav-id="{{ r['id'] }}" title="Favorit">🤍</button>
      <div class="card-title">{{ r['title'] or '—' }}</div>
      <div class="card-price">
        {% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}
      </div>
      <div class="deal-score loading" data-score-id="{{ r['id'] }}">…</div>
    </div>

    <!-- Fav status (hidden by default, shown by JS) -->
    <div class="fav-status-wrap" data-favstatus-id="{{ r['id'] }}" style="display:none; margin-top:6px;"></div>

    <div class="card-meta">
      {% if r['km'] %}<span class="tag"><span class="icon">🛣</span> {{ '{:,}'.format(r['km']).replace(',', '.') }} km</span>{% endif %}
      {% if r['ez_text'] %}<span class="tag"><span class="icon">📅</span> {{ r['ez_text'] }}</span>{% endif %}
      {% if r['city'] %}<span class="tag"><span class="icon">📍</span> {{ r['city'] }}</span>{% endif %}
      {% if r['postal_code'] %}<span class="tag">{{ r['postal_code'] }}</span>{% endif %}
      {% if r['pics'] is not none %}<span class="tag"><span class="icon">📸</span> {{ r['pics'] }}</span>{% endif %}
      {% if r['posted_at'] %}<span class="tag tag-green"><span class="icon">🕐</span> {{ r['posted_at'] }}</span>{% endif %}
      <!-- Price history tag (filled by JS) -->
      <span class="tag" data-pricehist-id="{{ r['id'] }}" style="display:none"></span>
      <span class="tag" style="opacity:0.5">{{ r['platform'] }} · {{ r['id'] }}</span>
    </div>

    <!-- Comparison badges -->
    <div class="card-comparisons">
      {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
        {% set sim = build_similar_search_url(r) %}
        {% if sim %}
        <a class="cmp-badge cmp-ka" href="{{ sim }}" target="_blank" rel="noopener">
          KA <span class="cmp-stats" data-sim-id="{{ r['id'] }}">…</span>
        </a>
        {% endif %}

        {% set as_url = build_autoscout_search_url(r) %}
        {% if as_url %}
        <a class="cmp-badge cmp-as" href="{{ as_url }}" target="_blank" rel="noopener">
          AS24 <span class="cmp-stats" data-as-id="{{ r['id'] }}">…</span>
        </a>
        {% endif %}

        {% set cw_url = build_carwow_search_url(r) %}
        {% if cw_url %}
        <a class="cmp-badge cmp-cw" href="{{ cw_url }}" target="_blank" rel="noopener">
          CW <span class="cmp-stats" data-cw-id="{{ r['id'] }}">…</span>
        </a>
        {% endif %}

        <a class="cmp-badge cmp-mob" href="#" data-mob-link
           data-brand="{{ (r['brand'] or '')|e }}"
           data-model="{{ (r['model'] or '')|e }}"
           data-km="{{ r['km'] or '' }}"
           data-ez="{{ (r['first_reg'] or r['ez_text'] or '')|e }}"
           target="_blank" rel="noopener">
          Mobile <span class="cmp-stats" data-mob-stats>…</span>
        </a>
      {% endif %}
    </div>

    <div class="card-actions">
      <a class="card-btn card-btn-accent" href="{{ r['url'] }}" target="_blank" rel="noopener">
        ↗ Öffnen
      </a>
      <button type="button" class="card-btn"
              data-prompt-btn
              data-row-id="{{ r['id'] }}"
              data-title="{{ (r['title'] or '')|e }}"
              data-url="{{ (r['url'] or '')|e }}"
              data-price="{{ r['price_eur'] or '' }}"
              data-km="{{ r['km'] or '' }}"
              data-city="{{ (r['city'] or '')|e }}"
              data-posted="{{ (r['posted_at'] or '')|e }}">
        📋 Prompt
      </button>
    </div>
  </div>
  {% endfor %}

  {% if not rows %}
  <div class="empty-state">
    <div class="icon">🚗</div>
    <div class="msg">Keine Treffer für diese Filter</div>
  </div>
  {% endif %}
</div>

<!-- Pagination -->
<div class="pagination">
  <a class="page-btn {% if not has_prev %}disabled{% endif %}" href="{{ prev_url }}">← Zurück</a>
  <span class="page-info">Seite {{ page }}</span>
  <a class="page-btn {% if not has_next %}disabled{% endif %}" href="{{ next_url }}">Weiter →</a>
</div>

<!-- Bottom Nav (mobile only) -->
<nav class="bottom-nav">
  <button class="nav-item active" onclick="window.scrollTo({top:0,behavior:'smooth'})">
    <span class="nav-icon">🏠</span> Start
  </button>
  <button class="nav-item" onclick="toggleFilters()">
    <span class="nav-icon">🔍</span> Filter
  </button>
  <button class="nav-item" id="navPush">
    <span class="nav-icon">🔔</span> Push
  </button>
  <button class="nav-item" id="navManage">
    <span class="nav-icon">⚙️</span> Abos
  </button>
</nav>

<!-- Prompt Modal -->
<div class="modal-overlay" id="promptModal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-header">
      <div>
        <div class="modal-title">Händler-Prompt</div>
        <div id="promptMeta" style="font-size:12px; color:var(--text-muted); margin-top:4px;"></div>
      </div>
      <button class="modal-close" id="promptClose">✕</button>
    </div>
    <div class="modal-body">
      <textarea class="prompt-area" id="promptText" placeholder="Prompt wird geladen…"></textarea>
    </div>
    <div class="modal-footer">
      <span id="promptCopyMsg" style="flex:1; font-size:12px; color:var(--text-dim);"></span>
      <button class="btn btn-primary btn-sm" id="promptCopyBtn">⧉ Kopieren</button>
      <button class="btn btn-ghost btn-sm" id="promptDoneBtn">Schließen</button>
    </div>
  </div>
</div>

<!-- Push Manage Modal -->
<div class="modal-overlay" id="pushModal">
  <div class="modal-sheet">
    <div class="modal-handle"></div>
    <div class="modal-header">
      <div>
        <div class="modal-title">Push-Abos</div>
        <div style="font-size:12px; color:var(--text-muted); margin-top:4px;">Gespeicherte Benachrichtigungen</div>
      </div>
      <button class="modal-close" id="pushClose">✕</button>
    </div>
    <div class="modal-body" id="pushList">
      <div style="color:var(--text-muted)">Lade…</div>
    </div>
    <div class="modal-footer">
      <span id="pushMsg" style="flex:1; font-size:12px; color:var(--text-dim);"></span>
      <button class="btn btn-ghost btn-sm" id="pushReloadBtn">Aktualisieren</button>
      <button class="btn btn-primary btn-sm" id="pushDoneBtn">Schließen</button>
    </div>
  </div>
</div>

<script>
document.addEventListener('DOMContentLoaded', () => {
  const REFRESH_MS = 120000;
  const VAPID_PUBLIC = "{{ VAPID_PUBLIC|default('')|safe }}".trim();
  console.log('[Push] VAPID Key Länge:', VAPID_PUBLIC.length, 'Anfang:', VAPID_PUBLIC.slice(0,8));

  // --- Toast ---
  function toast(msg, type='info') {
    const el = document.createElement('div');
    el.className = `toast toast-${type}`;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  // --- Error catcher ---
  window.addEventListener('error', (e) => toast('JS-Fehler: ' + (e.message || ''), 'error'));

  // --- Sync indicator ---
  const syncInd = document.getElementById('syncIndicator');
  function showSync() { syncInd.classList.add('active'); }
  function hideSync() { syncInd.classList.remove('active'); }

  // --- Filter toggle ---
  window.toggleFilters = function() {
    const sheet = document.getElementById('filterSheet');
    sheet.classList.toggle('open');
  };

  // --- Reload cards ---
  async function reloadCards() {
    showSync();
    const params = new URLSearchParams(window.location.search);
    params.set('_', Date.now());
    const r = await fetch('/api/table?' + params, { cache: 'no-store' });
    if (r.ok) {
      const html = await r.text();
      const list = document.getElementById('cardList');
      if (list) {
        list.innerHTML = html;
        bindPromptButtons(list);
        initBadges(list);
      }
    }
    hideSync();
  }

  // --- Comparison badges ---
  async function loadBadge(el, endpoint, idAttr) {
    const id = el.getAttribute(idAttr);
    try {
      const r = await fetch(`/api/${endpoint}?id=` + encodeURIComponent(id), { cache: 'no-store' });
      if (!r.ok) { el.textContent = ''; return null; }
      const j = await r.json();
      if (j.error === 'blocked') { el.textContent = 'blockiert'; return null; }
      if (!j.ok || !j.url) { el.textContent = ''; return null; }
      if (j.count > 0) {
        const avg = (j.avg_price_eur || 0).toLocaleString('de-DE');
        el.textContent = `${j.count}× · Ø ${avg} €`;
        return j;
      } else {
        el.textContent = 'keine';
        return null;
      }
    } catch { el.textContent = ''; return null; }
  }

  async function initBadges(scope) {
    const root = scope || document;

    // 1) Kleinanzeigen badges + row coloring
    for (const el of root.querySelectorAll('[data-sim-id]')) {
      const card = el.closest('.listing-card');
      const itemPrice = Number(card?.dataset?.priceEur || '');
      const j = await loadBadge(el, 'similar_stats', 'data-sim-id');
      if (j && card) {
        const avg = Number(j.avg_price_eur || 0);
        card.classList.remove('deal-good', 'deal-bad');
        if (avg > 0 && Number.isFinite(itemPrice)) {
          card.classList.add(avg > itemPrice ? 'deal-good' : 'deal-bad');
        }
      }
    }
    // 2) Mobile.de badges (direkt nach KA)
    initMobileLinks(root);
    // 3) AutoScout badges
    for (const el of root.querySelectorAll('[data-as-id]')) {
      await loadBadge(el, 'autoscout_stats', 'data-as-id');
    }
    // 4) Carwow badges
    for (const el of root.querySelectorAll('[data-cw-id]')) {
      await loadBadge(el, 'carwow_stats', 'data-cw-id');
    }
  }

  // --- Auto sync ---
  async function autosync() {
    try {
      let changed = false;
      const s = await fetch('/api/sync', { cache: 'no-store' });
      if (s.ok) {
        const d = await s.json();
        changed = !!d.changed;
      }
      await reloadCards();
      if (changed) toast('Neue Angebote gefunden', 'success');
      document.getElementById('lastSyncChip').textContent = '✓ ' + new Date().toLocaleTimeString('de-DE', {hour:'2-digit',minute:'2-digit'});
    } catch {}
  }

  // --- Mobile.de stats ---
  async function fetchMobileStats() {
    const inp = document.getElementById('mobileUrl');
    const msg = document.getElementById('mobileMsg');
    const url = (inp?.value || '').trim();
    msg.textContent = '';
    if (!url) { msg.textContent = 'Bitte eine mobile.de Such-URL einfügen.'; return; }
    try {
      const r = await fetch('/api/mobile_stats?url=' + encodeURIComponent(url), { cache: 'no-store' });
      const j = await r.json().catch(() => null);
      if (!r.ok || !j) { msg.textContent = (j?.error) || 'Fehler'; return; }
      if (j.ok && j.count > 0) {
        msg.textContent = `Ø ${j.avg_price_eur.toLocaleString('de-DE')} € aus ${j.count} Treffern`;
      } else {
        msg.textContent = j.error || 'Keine Preise gefunden.';
      }
    } catch { msg.textContent = 'Netzwerkfehler.'; }
  }
  document.getElementById('mobileFetchBtn')?.addEventListener('click', fetchMobileStats);
  document.getElementById('mobileUrl')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); fetchMobileStats(); }
  });

  // --- Push (exakt wie Original-Code der funktioniert hat) ---
  function urlBase64ToUint8Array(b64) {
    const padding = '='.repeat((4 - b64.length % 4) % 4);
    const base64 = (b64 + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) outputArray[i] = rawData.charCodeAt(i);
    return outputArray;
  }

  function determineEnvironment() {
    const isIOS = (/iPad|iPhone|iPod/.test(navigator.userAgent) && !window.MSStream) ||
                  (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches ||
                         (navigator.standalone === true);
    const hasSW = 'serviceWorker' in navigator;
    const hasPushManager = 'PushManager' in window;
    return { isIOS, isStandalone, pushSupported: hasSW && hasPushManager };
  }

  async function ensureSW() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
    try {
      const reg = await navigator.serviceWorker.register('/sw.js', {scope:'/'});
      await navigator.serviceWorker.ready;
      return reg;
    } catch (err) {
      console.error('Service Worker registration failed:', err);
      return null;
    }
  }

  async function subscribePush() {
    try {
      const reg = await ensureSW();
      if (!reg) { toast('SW/Push nicht verfügbar', 'error'); return; }

      const perm = await Notification.requestPermission();
      console.log('permission:', perm);
      if (perm !== 'granted') { toast('Benachrichtigungen nicht erlaubt', 'error'); return; }

      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_PUBLIC)
      });
      console.log('sub ok', sub);

      const form = document.getElementById('filterForm');
      const fd = new FormData(form);
      const params = new URLSearchParams();
      for (const [k, v] of fd.entries()) {
        const vv = (v || '').toString().trim();
        if (vv !== '') params.set(k, vv);
      }
      params.delete('page');
      params.delete('per_page');
      params.delete('_');
      const filters = params.toString();
      const max_price = form.querySelector('input[name="price_max"]')?.value || null;

      const r = await fetch('/api/push/subscribe', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ subscription: sub.toJSON(), filters, max_price })
      });
      const j = await r.json();
      if (!j.ok) throw new Error('Server sagt nein');

      await reg.showNotification('Autoscan aktiviert', { body: 'Push-Abo gespeichert.' });
      toast('Benachrichtigungen aktiviert ✓', 'success');
    } catch (e) {
      console.error(e);
      toast('Fehler: ' + (e && e.message ? e.message : e), 'error');
    }
  }

  document.getElementById('pushBtn')?.addEventListener('click', (e) => { e.preventDefault(); subscribePush(); });
  document.getElementById('navPush')?.addEventListener('click', (e) => { e.preventDefault(); subscribePush(); });

  // --- iOS hint ---
  const env = determineEnvironment();
  if (env.isIOS && !env.isStandalone) {
    document.getElementById('iosHint').style.display = 'block';
  }

  // --- Prompt Modal ---
  const promptModal = document.getElementById('promptModal');
  const promptText = document.getElementById('promptText');
  const promptMeta = document.getElementById('promptMeta');
  const promptCopyBtn = document.getElementById('promptCopyBtn');
  const promptCopyMsg = document.getElementById('promptCopyMsg');

  function openPrompt(meta) {
    if (promptMeta) promptMeta.textContent = meta || '';
    if (promptText) promptText.value = '';
    if (promptCopyMsg) promptCopyMsg.textContent = '';
    promptModal.classList.add('open');
  }
  function closePrompt() { promptModal.classList.remove('open'); }

  document.getElementById('promptClose')?.addEventListener('click', closePrompt);
  document.getElementById('promptDoneBtn')?.addEventListener('click', closePrompt);
  promptModal?.querySelector('.modal-overlay')?.addEventListener('click', (e) => { if (e.target === promptModal) closePrompt(); });
  promptModal?.addEventListener('click', (e) => { if (e.target === promptModal) closePrompt(); });

  promptCopyBtn?.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(promptText?.value || '');
      if (promptCopyMsg) { promptCopyMsg.textContent = 'Kopiert!'; setTimeout(() => promptCopyMsg.textContent = '', 1500); }
    } catch { if (promptCopyMsg) promptCopyMsg.textContent = 'Kopieren nicht möglich'; }
  });

  function bindPromptButtons(scope) {
    (scope || document).querySelectorAll('[data-prompt-btn]').forEach(btn => {
      if (btn.dataset.promptBound === '1') return;
      btn.dataset.promptBound = '1';
      btn.addEventListener('click', async () => {
        const title = btn.getAttribute('data-title') || '';
        const id = btn.getAttribute('data-row-id') || '';
        const price = btn.getAttribute('data-price') || '';
        const city = btn.getAttribute('data-city') || '';
        const meta = `${title} · ${price ? price+' €' : ''} ${city ? '· '+city : ''}`;
        openPrompt(meta);
        if (promptText) promptText.value = 'Lade…';
        try {
          const r = await fetch('/api/prompt?id=' + encodeURIComponent(id), { cache: 'no-store' });
          const j = await r.json().catch(() => null);
          if (r.ok && j?.ok && j.prompt) {
            if (promptText) promptText.value = j.prompt;
          } else {
            if (promptText) promptText.value = 'Konnte Prompt nicht laden.';
          }
        } catch { if (promptText) promptText.value = 'Netzwerkfehler.'; }
      });
    });
  }
  bindPromptButtons(document);

  // --- Push Manage Modal ---
  const pushModal = document.getElementById('pushModal');
  const pushList = document.getElementById('pushList');
  const pushMsg = document.getElementById('pushMsg');

  function openPushModal() { pushModal.classList.add('open'); loadPushSubs(); }
  function closePushModal() { pushModal.classList.remove('open'); }

  document.getElementById('pushManageBtn')?.addEventListener('click', openPushModal);
  document.getElementById('navManage')?.addEventListener('click', openPushModal);
  document.getElementById('pushClose')?.addEventListener('click', closePushModal);
  document.getElementById('pushDoneBtn')?.addEventListener('click', closePushModal);
  document.getElementById('pushReloadBtn')?.addEventListener('click', loadPushSubs);
  pushModal?.addEventListener('click', (e) => { if (e.target === pushModal) closePushModal(); });

  function esc(s) { return (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }

  function formatFilters(fs) {
    if (!fs) return 'Alle';
    let params;
    try { params = new URLSearchParams(fs.replace(/^\?/,'')); } catch { return fs; }
    const label = { q:'Suche', price_min:'Min€', price_max:'Max€', ez_min:'EZ ab', ez_max:'EZ bis', km_max:'KM max', postal_prefix:'PLZ', city:'Stadt', pics_min:'Bilder', posted_days:'Tage', sort:'Sort' };
    const out = [];
    for (const [k,v] of params.entries()) { const vv=(v||'').trim(); if(vv) out.push(`${label[k]||k}: ${vv}`); }
    return out.length ? out.join(' · ') : 'Alle';
  }

  async function loadPushSubs() {
    if (!pushList) return;
    pushList.innerHTML = '<div style="color:var(--text-muted)">Lade…</div>';
    try {
      const r = await fetch('/api/push/list', { cache: 'no-store' });
      const j = await r.json().catch(() => null);
      if (!r.ok || !j?.ok) { pushList.innerHTML = '<div style="color:var(--red)">Fehler beim Laden.</div>'; return; }
      const subs = j.subs || [];
      if (!subs.length) { pushList.innerHTML = '<div style="color:var(--text-muted)">Keine Abos vorhanden.</div>'; return; }

      pushList.innerHTML = subs.map(s => {
        const ep = (s.endpoint||'').slice(0,40)+'…';
        const f = formatFilters(s.filters);
        return `<div class="push-item">
          <div class="push-item-label">Endpoint</div>
          <div class="push-item-val">${esc(ep)}</div>
          <div style="margin-top:8px"><div class="push-item-label">Filter</div><div class="push-item-val">${esc(f)}</div></div>
          <div style="margin-top:8px;display:flex;justify-content:flex-end">
            <button class="btn btn-ghost btn-sm" style="color:var(--red);border-color:rgba(251,113,133,0.3)" data-push-del="${btoa(unescape(encodeURIComponent(s.endpoint||'')))}">Löschen</button>
          </div>
        </div>`;
      }).join('');

      pushList.querySelectorAll('[data-push-del]').forEach(btn => {
        btn.addEventListener('click', async () => {
          const b64 = btn.getAttribute('data-push-del') || '';
          const endpoint = b64 ? decodeURIComponent(escape(atob(b64))) : '';
          if (!endpoint) return;
          btn.disabled = true; btn.textContent = '…';
          try {
            const rr = await fetch('/api/push/unsubscribe', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({endpoint}) });
            const jj = await rr.json().catch(()=>null);
            if (rr.ok && jj?.ok) { if(pushMsg) pushMsg.textContent='Gelöscht.'; await loadPushSubs(); }
            else { btn.disabled=false; btn.textContent='Löschen'; if(pushMsg) pushMsg.textContent='Fehler.'; }
          } catch { btn.disabled=false; btn.textContent='Löschen'; if(pushMsg) pushMsg.textContent='Netzwerkfehler.'; }
        });
      });
    } catch { pushList.innerHTML = '<div style="color:var(--red)">Netzwerkfehler.</div>'; }
  }

  // Escape key for modals
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { closePrompt(); closePushModal(); }
  });

  // --- Mobile.de Direct Link Builder ---
  const _MOB_CODES = {"brands":{"bmw":"3500","mercedes-benz":"17200","audi":"1900","volkswagen":"25200","peugeot":"19300","skoda":"22900","renault":"20700","ford":"9000","opel":"19000","toyota":"24100","hyundai":"11600","citroën":"5900","seat":"22500","fiat":"8800","kia":"13200","porsche":"20100","volvo":"25100","mini":"17500","tesla":"135","mazda":"16800","nissan":"18700","dacia":"6600","cupra":"3","honda":"11000","suzuki":"23600","jaguar":"12400","land rover":"14800","mitsubishi":"17700","smart":"23000","lexus":"15200"},"models":{"bmw":{"1er reihe (alle)":"g20","1er reihe":"g20","114":"73","116":"2","118":"3","120":"4","123":"59","125":"61","128":"328","130":"5","135":"58","1er m coupé":"87","2002":"71","2er reihe (alle)":"g55","2er reihe":"g55","214 active tourer":"110","214 gran tourer":"116","216":"106","216 active tourer":"111","216 gran coupé":"345","216 gran tourer":"114","218":"90","218 active tourer":"107","218 gran coupé":"343","218 gran tourer":"112","220":"84","220 active tourer":"108","220 gran coupé":"344","220 gran tourer":"113","223":"351","223 active tourer":"333","223 gran coupé":"350","225":"91","225 active tourer":"109","228":"104","230":"125","230 active tourer":"334","2er gran coupé":"322","3er reihe (alle)":"g21","3er reihe":"g21","315":"7","316":"8","318":"9","318 gran turismo":"75","320":"10","320 gran turismo":"76","323":"11","324":"12","325":"13","325 gran turismo":"88","328":"14","328 gran turismo":"77","330":"15","330 gran turismo":"103","335":"56","335 gran turismo":"78","340":"118","340 gran turismo":"130","activehybrid 3":"72","4er reihe (alle)":"g53","4er reihe":"g53","418":"115","418 gran coupé":"98","420":"80","420 gran coupé":"99","425":"102","425 gran coupé":"124","428":"81","428 gran coupé":"100","430":"83","430 gran coupé":"105","435":"82","435 gran coupé":"101","440":"120","440 gran coupé":"121","5er reihe (alle)":"g22","5er reihe":"g22","518":"16","520":"17","520 gran turismo":"74","523":"18","524":"19","525":"20","528":"21","530":"22","530 gran turismo":"65","535":"23","535 gran turismo":"66","540":"24","545":"25","550":"26","550 gran turismo":"67","activehybrid 5":"70","6er reihe (alle)":"g23","6er reihe":"g23","620 gran turismo":"144","628":"27","630":"28","630 gran turismo":"127","633":"29","635":"30","640":"68","640 gran coupé":"94","640 gran turismo":"128","645":"31","650":"32","650 gran coupé":"95","7er reihe (alle)":"g24","7er reihe":"g24","725":"33","728":"34","730":"35","732":"36","735":"37","740":"38","745":"39","750":"40","760":"41","activehybrid 7":"63","840":"42","850":"43","i3":"79","i4":"330","i5":"341","i7":"336","i8":"89","ix":"331","ix1":"337","ix2":"346","ix3":"329","m-modelle (alle)":"g25","m-modelle":"g25","m135":"69","m140i":"122","m2":"117","m235":"85","m240i":"123","m3":"45","m340d":"342","m340i":"152","m4":"93","m440":"335","m5":"46","m550":"86","m6":"47","m760":"126","m8":"154","m850":"140","x-reihe (alle)":"g26","x-reihe":"g26","activehybrid x6":"64","x1":"6","x2":"129","x3":"48","x3 m":"145","x3 m40":"153","x3 m50":"348","x4":"92","x4 m":"146","x4 m40":"119","x5":"49","x5 m":"53","x5 m50":"96","x5 m60":"339","x6":"60","x6 m":"62","x6 m50":"97","x6 m60":"340","x7":"143","x7 m50":"332","x7 m60":"347","xm":"338","z-reihe (alle)":"g27","z-reihe":"g27","z1":"50","z3":"51","z3 m":"57","z4":"52","z4 m":"55","z4 m40":"349","z8":"54","andere":"1"},"mercedes-benz":{"190":"126","200":"127","220":"128","230":"129","240":"130","250":"131","260":"132","270":"133","280":"134","290":"135","300":"136","320":"137","350":"138","380":"139","400":"140","416":"141","420":"142","450":"143","500":"144","560":"145","600":"146","a-klasse (alle)":"g4","a-klasse":"g4","a 140":"2","a 150":"3","a 160":"4","a 170":"5","a 180":"6","a 190":"7","a 200":"8","a 210":"9","a 220":"221","a 250":"220","a 35 amg":"298","a 45 amg":"229","b-klasse (alle)":"g5","b-klasse":"g5","b 150":"12","b 160":"11","b 170":"13","b 180":"14","b 200":"15","b 220":"222","b 250":"223","b electric drive":"241","ce-klasse (alle)":"g7","ce-klasse":"g7","ce 200":"32","ce 220":"167","ce 230":"216","ce 280":"217","ce 300":"33","ce 320":"234","citan":"224","c-klasse (alle)":"g6","c-klasse":"g6","c 160":"16","c 180":"17","c 200":"18","c 220":"19","c 230":"20","c 240":"21","c 250":"22","c 270":"23","c 280":"24","c 300":"44","c 30 amg":"25","c 320":"27","c 32 amg":"26","c 350":"28","c 36 amg":"29","c 400":"245","c 43 amg":"30","c 450 amg":"246","c 55 amg":"31","c 63 amg":"198","cla-klasse (alle)":"g45","cla-klasse":"g45","cla 180":"225","cla 180 shooting brake":"255","cla 200":"226","cla 200 shooting brake":"256","cla 220":"227","cla 220 shooting brake":"257","cla 250":"228","cla 250 shooting brake":"258","cla 350":"375","cla 35 amg":"326","cla 35 amg shooting brake":"353","cla 45 amg":"230","cla 45 amg shooting brake":"259","cla shooting brake":"248","clc-klasse (alle)":"g30","clc-klasse":"g30","clc 160":"46","clc 180":"200","clc 200":"201","clc 220":"202","clc 230":"203","clc 250":"107","clc 350":"204","cle-klasse (alle)":"g69","cle-klasse":"g69","cle 180":"367","cle 200":"357","cle 220":"360","cle 300":"361","cle 450":"362","cle 53 amg":"363","clk-klasse (alle)":"g9","clk-klasse":"g9","clk 200":"168","clk 220":"169","clk 230":"186","clk 240":"187","clk 270":"188","clk 280":"170","clk 320":"171","clk 350":"172","clk 430":"173","clk 500":"174","clk 55 amg":"45","clk 63 amg":"189","cl-klasse (alle)":"g8","cl-klasse":"g8","cl 160":"210","cl 180":"35","cl 200":"36","cl 220":"37","cl 230":"38","cl 320":"211","cl 420":"39","cl 500":"40","cl 55 amg":"41","cl 600":"42","cl 63 amg":"197","cl 65 amg":"43","cls-klasse (alle)":"g10","cls-klasse":"g10","cls 220":"240","cls 220 shooting brake":"260","cls 250":"212","cls 250 shooting brake":"261","cls 280":"205","cls 300":"117","cls 320":"147","cls 350":"148","cls 350 shooting brake":"262","cls 400":"239","cls 400 shooting brake":"263","cls 450":"289","cls 500":"149","cls 500 shooting brake":"264","cls 53 amg":"297","cls 55 amg":"150","cls 63 amg":"176","cls 63 amg shooting brake":"265","cls shooting brake":"249","ecitan":"374","e-klasse (alle)":"g11","e-klasse":"g11","e 200":"47","e 220":"48","e 230":"49","e 240":"50","e 250":"51","e 260":"52","e 270":"53","e 280":"54","e 290":"55","e 300":"56","e 320":"57","e 350":"58","e 36 amg":"59","e 400":"60","e 420":"61","e 430":"62","e 43 amg":"272","e 450":"321","e 50":"177","e 500":"64","e 53 amg":"296","e 55 amg":"178","e 60 amg":"66","e 63 amg":"179","eqa":"346","eqb":"350","eqc":"322","eqe":"352","eqe suv":"358","eqs":"351","eqs suv":"359","eqt":"356","eqv":"344","esprinter":"373","evito":"370","g-klasse (alle)":"g12","g-klasse":"g12","g 230":"152","g 240":"151","g 250":"153","g 270":"154","g 280":"155","g 290":"156","g 300":"157","g 320":"158","g 350":"160","g 400":"159","g 450":"364","g 500":"161","g 55 amg":"68","g 580":"366","g 63 amg":"218","g 65 amg":"219","gla-klasse (alle)":"g54","gla-klasse":"g54","gla 180":"238","gla 200":"231","gla 220":"232","gla 250":"233","gla 35 amg":"343","gla 45 amg":"236","glb-klasse (alle)":"g66","glb-klasse":"g66","glb 180":"335","glb 200":"328","glb 220":"336","glb 250":"329","glb 35 amg":"339","glc-klasse (alle)":"g59","glc-klasse":"g59","glc 200":"325","glc 220":"253","glc 250":"254","glc 300":"284","glc 350":"278","glc 400":"334","glc 43 amg":"279","glc 450":"365","glc 63 amg":"283","gle-klasse (alle)":"g58","gle-klasse":"g58","gle 250":"266","gle 300":"324","gle 350":"251","gle 400":"252","gle 43 amg":"280","gle 450":"243","gle 500":"267","gle 53 amg":"337","gle 580":"341","gle 63 amg":"250","glk-klasse (alle)":"g31","glk-klasse":"g31","glk 200":"175","glk 220":"206","glk 250":"63","glk 280":"207","glk 300":"65","glk 320":"208","glk 350":"209","gl-klasse (alle)":"g13","gl-klasse":"g13","gl 320":"180","gl 350":"166","gl 400":"244","gl 420":"181","gl 450":"182","gl 500":"183","gl 55 amg":"196","gl 63 amg":"195","gls-klasse (alle)":"g60","gls-klasse":"g60","gls 350":"268","gls 400":"269","gls 450":"340","gls 500":"270","gls 580":"338","gls 600":"342","gls 63":"271","gt-klasse (alle)":"g64","gt-klasse":"g64","amg gt":"242","amg gt c":"282","amg gt r":"281","amg gt s":"247","mb 100":"70","ml-klasse (alle)":"g14","ml-klasse":"g14","ml 230":"71","ml 250":"215","ml 270":"72","ml 280":"73","ml 300":"67","ml 320":"74","ml 350":"75","ml 400":"76","ml 420":"192","ml 430":"77","ml 450":"69","ml 500":"78","ml 55 amg":"79","ml 63 amg":"162","r-klasse (alle)":"g15","r-klasse":"g15","r 280":"190","r 300":"92","r 320":"80","r 350":"81","r 500":"82","r 63 amg":"184","s-klasse (alle)":"g16","s-klasse":"g16","s 250":"213","s 260":"185","s 280":"83","s 300":"84","s 320":"85","s 350":"86","s 400":"87","s 420":"88","s 430":"89","s 450":"191","s 500":"90","s 55":"91","s 550":"193","s 560":"285","s 580":"348","s 600":"93","s 63 amg":"194","s 650":"294","s 65 amg":"94","s 680":"349","slc-klasse (alle)":"g62","slc-klasse":"g62","slc 180":"274","slc 200":"275","slc 250":"273","slc 280":"288","slc 300":"276","slc 43 amg":"277","slk-klasse (alle)":"g18","slk-klasse":"g18","slk 200":"108","slk 230":"109","slk 250":"214","slk 280":"110","slk 300":"10","slk 320":"112","slk 32 amg":"111","slk 350":"113","slk 55 amg":"114","sl-klasse (alle)":"g17","sl-klasse":"g17","sl 230":"371","sl 250":"369","sl 280":"95","sl 300":"96","sl 320":"97","sl 350":"98","sl 380":"99","sl 400":"237","sl 420":"100","sl 43 amg":"354","sl 450":"101","sl 500":"102","sl 55 amg":"103","sl 560":"104","sl 600":"105","sl 60 amg":"163","sl 63 amg":"199","sl 65 amg":"106","sl 680":"372","sl 70 amg":"164","sl 73 amg":"165","slr":"115","sls amg":"34","sprinter":"116","t-klasse":"355","vaneo":"122","vario":"123","viano":"124","vito":"125","v-klasse (alle)":"g19","v-klasse":"g19","v 200":"118","v 220":"119","v 230":"120","v 250":"235","v 280":"121","v 300":"323","x-klasse (alle)":"g65","x-klasse":"g65","x 220":"286","x 250":"287","x 350":"312","andere":"1"},"audi":{"100":"2","200":"3","80":"5","90":"6","a1":"25","a2":"7","a3":"8","a4":"9","a4 allroad":"33","a5":"31","a6":"10","a6 allroad":"12","a6 e-tron":"66","a7":"34","a8":"11","cabriolet":"13","coupé":"14","e-tron":"50","e-tron gt":"58","q2":"45","q3":"37","q4":"57","q4 e-tron":"61","q5":"32","q6 e-tron":"63","q7":"15","q8":"46","q8 e-tron":"65","quattro":"16","r8":"29","rs2":"26","rs3":"36","rs4":"27","rs5":"17","rs6":"28","rs7":"40","rs e-tron gt":"60","rsq3":"41","rsq8":"55","s1":"42","s2":"18","s3":"19","s4":"20","s5":"30","s6":"21","s6 e-tron":"67","s7":"38","s8":"22","sq2":"47","sq5":"39","sq6 e-tron":"62","sq7":"44","sq8":"54","sq8 e-tron":"64","tt (alle) (alle)":"g38","tt (alle)":"g38","tt":"23","tt rs":"35","tts":"4","v8":"24","andere":"1"},"volkswagen":{"181":"2","amarok":"5","arteon":"64","beetle":"10","bora":"6","buggy":"7","caddy":"9","caddy maxi":"98","cc":"19","corrado":"12","crafter":"3","eos":"41","e-up!":"100","fox":"13","golf (alle)":"g29","golf":"14","golf plus":"55","golf sportsvan":"40","id.3":"81","id.4":"82","id.5":"93","id.6":"96","id.7":"97","id. buzz":"94","iltis":"15","jetta":"16","käfer":"17","karmann ghia":"18","lt":"20","lupo":"21","new beetle":"24","passat (alle) (alle)":"g37","passat (alle)":"g37","passat":"25","passat alltrack":"62","passat cc":"4","passat variant":"63","phaeton":"26","polo":"27","routan":"8","santana":"28","scirocco":"29","sharan":"30","t1":"42","t2":"31","t3 (alle)":"g1","t3":"g1","t3 andere":"46","t3 caravelle":"44","t3 kombi":"22","t3 multivan":"45","t4 (alle)":"g2","t4":"g2","t4 andere":"49","t4 california":"33","t4 caravelle":"47","t4 kombi":"23","t4 multivan":"48","t5 (alle)":"g3","t5":"g3","t5 andere":"53","t5 california":"34","t5 caravelle":"50","t5 kombi":"32","t5 multivan":"51","t5 shuttle":"52","t5 transporter":"61","t6 (alle)":"g57","t6":"g57","t6 andere":"59","t6 california":"58","t6 caravelle":"56","t6 kombi":"57","t6 multivan":"43","t6 transporter":"60","t7 (alle)":"g68","t7":"g68","t7 andere":"87","t7 california":"88","t7 caravelle":"89","t7 kombi":"90","t7 multivan":"91","t7 transporter":"92","taigo":"86","taro":"35","tayron":"99","t-cross":"75","tiguan":"54","tiguan allspace":"66","touareg":"36","touran":"37","t-roc":"65","up!":"11","vento":"39","xl1":"38","andere":"1"},"peugeot":{"1007":"2","104":"3","106":"4","107":"5","108":"47","2008":"46","204":"6","205":"7","206":"8","207":"30","208":"43","3008":"26","301":"45","304":"9","305":"10","306":"11","307":"12","308":"32","309":"13","4007":"31","4008":"44","404":"14","405":"15","406":"16","407":"17","408":"37","5008":"35","504":"18","505":"19","508":"42","604":"20","605":"21","607":"22","806":"23","807":"24","bipper":"33","bipper tepee":"39","boxer":"25","e-2008":"55","e-208":"51","e-3008":"56","e-308":"57","e-408":"58","e-5008":"54","e-rifter":"52","e-traveller":"53","expert":"27","expert tepee":"40","ion":"38","j5":"28","partner":"29","partner tepee":"41","rcz":"36","rifter":"49","tepee":"34","traveller":"48","andere":"1"},"skoda":{"105":"2","120":"3","130":"4","135":"5","citigo":"17","elroq":"26","enyaq":"25","fabia":"6","favorit":"7","felicia":"8","forman":"9","kamiq":"24","karoq":"20","kodiaq":"19","octavia":"10","pick-up":"14","praktik":"16","rapid":"18","roomster":"13","scala":"21","superb":"12","yeti":"15","andere":"1"},"renault":{"alaskan":"50","alpine a110":"40","alpine a310":"2","alpine v6":"4","arkana":"64","austral":"65","avantime":"5","captur":"47","clio":"6","coupe":"7","espace":"8","express":"9","fluence":"36","fuego":"10","grand espace":"11","grand kangoo":"68","grand kangoo e-tech":"70","grand modus":"42","grand scenic":"12","grand scenic e-tech":"73","kadjar":"48","kangoo":"13","kangoo e-tech":"69","koleos":"41","laguna":"14","latitude":"44","mascott":"15","master":"16","megane":"17","megane e-tech":"71","modus":"18","p 1400":"19","r 11":"20","r 14":"21","r 18":"22","r 19":"23","r 20":"24","r 21":"25","r 25":"26","r 30":"27","r 4":"28","r 5":"29","r 6":"30","r 9":"31","rafale":"66","rapid":"32","safrane":"33","scenic":"34","scenic e-tech":"72","spider":"35","symbioz":"67","talisman":"49","trafic":"37","twingo":"38","twizy":"45","vel satis":"39","wind":"43","zoe":"46","andere":"1"},"ford":{"aerostar":"2","b-max":"54","bronco":"3","bronco sport":"119","capri":"4","c-max":"52","cougar":"5","courier":"6","crown":"7","econoline":"8","econovan":"9","ecosport":"56","edge":"48","escape":"10","escort":"11","excursion":"12","expedition":"13","explorer":"14","express":"15","f 100":"63","f 150":"16","f 250":"17","f 350":"18","fairlane":"45","falcon":"46","fiesta":"19","flex":"53","focus":"20","fusion":"22","galaxy":"23","granada":"24","grand c-max":"50","grand tourneo":"59","gt":"44","ka/ka+":"25","kuga":"49","maverick":"27","mercury":"28","mondeo":"29","mustang":"30","mustang mach-e":"118","orion":"31","probe":"32","puma":"33","puma gen-e":"123","ranger":"34","raptor":"55","scorpio":"35","sierra":"36","s-max":"47","sportka":"26","streetka":"37","taunus":"38","taurus":"39","thunderbird":"40","tourneo (alle) (alle)":"g61","tourneo (alle)":"g61","tourneo":"41","tourneo connect":"61","tourneo courier":"60","tourneo custom":"62","transit (alle)":"g39","transit":"42","transit connect":"51","transit courier":"58","transit custom":"57","windstar":"43","andere":"1"},"opel":{"adam":"38","agila":"2","ampera":"28","ampera-e":"45","antara":"34","arena":"3","ascona":"4","astra":"5","astra electric":"51","calibra":"6","campo":"7","cascada":"39","cavalier":"32","combo":"8","combo electric":"50","combo life":"47","commodore":"9","corsa":"10","crossland (x)":"42","diplomat":"11","frontera":"12","grandland (x)":"43","gt":"13","insignia":"35","insignia ct":"40","kadett":"14","karl":"41","manta":"15","meriva":"16","mokka":"37","mokka-e":"49","mokka x":"44","monterey":"17","monza":"18","movano":"19","nova":"33","omega":"20","pick up sportscap":"21","rekord":"22","rocks-e":"48","senator":"23","signum":"24","sintra":"25","speedster":"26","tigra":"27","vectra":"29","vivaro":"30","zafira":"31","zafira electric":"52","zafira life":"46","zafira tourer":"36","andere":"1"},"toyota":{"4-runner":"2","alphard":"47","auris":"39","auris touring sports":"43","avalon":"3","avensis":"4","avensis verso":"14","aygo (x)":"5","bz4x":"87","camry":"6","carina":"7","celica":"8","c-hr":"48","corolla":"9","corolla cross":"88","corolla verso":"40","cressida":"10","crown":"11","dyna":"12","fcv":"45","fj":"38","fortuner":"49","gr86":"92","gt86":"31","hiace":"16","highlander":"15","hilux":"17","iq":"41","land cruiser":"19","lite-ace":"21","matrix":"18","mirai":"46","mr 2":"22","paseo":"23","picnic":"25","previa":"26","prius":"27","prius+":"42","proace (verso)":"44","proace city":"76","proace max":"99","proace verso electric":"98","rav 4":"28","sequoia":"29","sienna":"30","starlet":"32","supra":"33","tacoma":"37","tercel":"34","tundra":"35","urban cruiser":"13","verso":"20","verso-s":"24","yaris":"36","yaris cross":"78","andere":"1"},"hyundai":{"accent":"2","atos":"3","azera":"24","bayon":"56","coupe":"4","elantra":"5","excel":"6","galloper":"7","genesis":"15","getz":"8","grandeur":"9","grand santa fe":"38","h-1":"13","h 100":"10","h-1 starex":"14","h 200":"11","h350":"39","i10":"31","i20":"32","i30":"30","i40":"33","inster":"62","ioniq":"40","ioniq 5":"58","ioniq 6":"60","ioniq 9":"65","ix20":"36","ix35":"12","ix55":"35","kona":"41","kona elektro":"64","lantra":"16","matrix":"17","nexo":"49","pony":"18","santa fe":"19","santamo":"20","s-coupe":"22","sonata":"23","staria":"59","terracan":"25","trajet":"26","tucson":"27","veloster":"37","veracruz":"21","xg 30":"28","xg 350":"29","andere":"1"},"citroën":{"2 cv":"2","ami":"52","ax":"4","berlingo":"5","bx":"6","c1":"7","c2":"9","c3":"11","c3 aircross":"41","c3 picasso":"8","c4":"12","c4 aircross":"36","c4 cactus":"37","c4 picasso":"32","c4 spacetourer":"43","c4 x":"49","c5":"13","c5 aircross":"44","c5 x":"48","c6":"14","c8":"15","c-crosser":"33","c-elysée":"38","cx":"16","c-zero":"31","ds":"17","ds3":"20","ds4":"22","ds4 crossback":"40","ds5":"35","ë-berlingo":"50","ë-c3":"55","ë-c3 aircross":"51","ë-c4":"53","ë-c4 x":"54","e-mehari":"42","ë-spacetourer":"56","evasion":"18","grand c4 picasso / spacetourer":"34","gsa":"19","jumper":"10","jumpy":"21","nemo":"3","saxo":"23","sm":"24","spacetourer":"39","visa":"25","xantia":"26","xm":"27","xsara":"28","xsara picasso":"29","zx":"30","andere":"1"},"seat":{"alhambra":"2","altea":"3","arona":"16","arosa":"4","ateca":"13","cordoba":"5","exeo":"6","ibiza":"7","inca":"8","leon":"9","malaga":"10","marbella":"11","mii":"12","tarraco":"17","terra":"14","toledo":"15","andere":"1"},"fiat":{"124":"2","124 spider":"54","126":"4","127":"5","130":"6","131":"7","500":"9","500c":"49","500e":"74","500l":"48","500l cross":"60","500l living":"51","500l trekking":"52","500l urban":"59","500l wagon":"58","500s":"57","500x":"53","600":"64","600e":"75","albea":"25","barchetta":"10","brava":"11","bravo":"12","cinquecento":"14","coupe":"15","croma":"16","dino":"17","doblo":"18","ducato":"19","fiorino":"21","freemont":"47","fullback":"55","grande panda":"77","grande punto":"44","idea":"22","linea":"46","marea":"23","marengo":"24","multipla":"26","new panda":"50","palio":"27","panda":"28","punto":"30","punto evo":"38","qubo":"13","regata":"31","ritmo":"32","scudo":"33","sedici":"45","seicento":"34","siena":"29","spider europa":"35","stilo":"36","strada":"37","talento":"56","tempra":"39","tipo":"40","topolino":"76","ulysse":"41","uno":"42","x 1/9":"43","andere":"1"},"kia":{"besta":"2","borrego":"28","carens":"3","carnival":"4","cee'd / ceed":"26","cee'd sportswagon":"31","cerato":"5","clarus":"6","elan":"7","ev3":"62","ev4":"64","ev5":"65","ev6":"52","ev9":"61","joice":"8","k2500":"9","k2700":"10","k4":"66","leo":"11","magentis":"12","mentor":"13","mini":"30","niro":"34","niro ev":"54","opirus":"14","optima":"33","picanto":"15","pregio":"16","pride":"17","pro cee'd / proceed":"27","pv5":"63","retona":"18","rio":"19","roadster":"20","rocsta":"21","sephia":"22","shuma":"23","sorento":"24","soul":"29","sportage":"25","stinger":"35","stonic":"36","venga":"32","xceed":"49","andere":"1"},"porsche":{"356":"2","911er reihe (alle)":"g40","911er reihe":"g40","911 urmodell":"3","930":"24","964":"5","991":"21","992":"28","993":"6","996":"7","997":"17","912":"8","914":"9","918":"23","924":"10","928":"11","944":"12","959":"13","962":"14","968":"15","boxster":"16","carrera gt":"20","cayenne":"18","cayman":"19","macan":"22","panamera":"4","taycan":"30","andere":"1"},"volvo":{"240":"2","244":"3","245":"4","262":"5","264":"6","340":"7","360":"8","440":"9","460":"10","480":"11","740":"12","744":"13","745":"14","760":"15","780":"17","850":"18","855":"19","940":"20","944":"21","945":"22","960":"23","965":"24","amazon":"38","c30":"39","c40":"47","c70":"25","ec40":"51","es90":"52","ex30":"49","ex40":"50","ex60":"53","ex90":"48","polar":"26","s40":"27","s60":"28","s60 cross country":"42","s70":"29","s80":"30","s90":"31","v40":"32","v40 cross country":"41","v50":"33","v60":"16","v60 cross country":"43","v70":"34","v90":"35","v90 cross country":"44","xc40":"45","xc60":"40","xc70":"36","xc90":"37","andere":"1"},"mini":{"aceman":"55","aceman (alle)":"g73","aceman e":"56","aceman se":"57","john cooper works aceman":"58","cabrio serie (alle)":"g47","cabrio serie":"g47","cooper cabrio":"22","cooper d cabrio":"23","cooper s cabrio":"24","cooper sd cabrio":"25","john cooper works cabrio":"26","one cabrio":"21","clubman serie (alle)":"g48","clubman serie":"g48","cooper clubman":"8","cooper d clubman":"30","cooper s clubman":"31","cooper sd clubman":"32","john cooper works clubman":"33","one clubman":"27","one d clubman":"28","clubvan":"17","countryman serie (alle)":"g49","countryman serie":"g49","cooper countryman":"5","cooper sd countryman":"39","countryman c (cooper)":"53","countryman d (cooper)":"37","countryman e (cooper)":"51","countryman s (cooper)":"38","countryman se (cooper)":"50","john cooper works countryman":"40","one countryman":"34","one d countryman":"35","coupe serie (alle)":"g50","coupe serie":"g50","cooper coupé":"11","cooper s coupé":"12","cooper sd coupé":"18","john cooper works coupé":"13","mini (alle)":"g46","mini":"g46","1000":"2","1300":"3","cooper":"4","cooper c":"54","cooper d":"20","cooper e":"52","cooper s":"7","cooper sd":"10","cooper se":"49","john cooper works":"9","one":"6","one d":"19","one first":"29","paceman serie (alle)":"g51","paceman serie":"g51","cooper d paceman":"42","cooper paceman":"41","cooper sd paceman":"44","cooper s paceman":"43","john cooper works paceman":"45","roadster serie (alle)":"g52","roadster serie":"g52","cooper roadster":"14","cooper sd roadster":"46","cooper s roadster":"15","john cooper works roadster":"16","andere":"1"},"tesla":{"model 3":"5","model s":"3","model x":"4","model y":"6","roadster":"2","andere":"1"},"mazda":{"121":"2","2":"3","2 hybrid":"67","3":"4","323":"5","5":"6","6":"7","626":"8","6e":"69","929":"9","bongo":"23","b series":"10","bt-50":"28","cx-3":"34","cx-30":"42","cx-5":"33","cx-60":"63","cx‑6e":"70","cx-7":"26","cx-80":"68","cx-9":"27","demio":"11","e series":"12","millenia":"24","mpv":"13","mx-3":"14","mx-30":"60","mx-5":"15","mx-6":"16","premacy":"17","protege":"25","rx-6":"18","rx-7":"19","rx-8":"20","tribute":"21","xedos":"22","andere":"1"},"nissan":{"100 nx":"42","200 sx":"43","240 sx":"44","280 zx":"4","300 zx":"5","350z":"6","370z":"26","almera":"7","almera tino":"8","altima":"45","ariya":"87","armada":"9","bluebird":"10","cabstar":"11","cargo":"12","cherry":"13","cube":"3","e-nv200":"59","evalia":"57","frontier":"14","gt-r":"49","interstar":"15","juke":"52","king cab":"16","kubistar":"17","laurel":"18","leaf":"53","maxima":"19","micra":"20","murano":"21","navara":"22","note":"23","np 300":"2","nv200":"54","nv250":"82","nv300":"60","nv400":"56","pathfinder":"24","patrol":"25","pickup":"55","pixo":"36","prairie":"27","primastar":"28","primera":"29","pulsar":"58","qashqai":"47","qashqai+2":"50","quest":"30","sentra":"46","serena":"31","silvia":"32","skyline":"33","sunny":"34","terrano":"35","tiida":"48","titan":"37","townstar":"86","trade":"38","urvan":"39","vanette":"40","x-trail":"41","andere":"1"},"dacia":{"bigster":"27","dokker":"5","duster":"2","jogger":"26","lodgy":"3","logan":"6","logan pick-up":"4","pick up":"7","sandero":"24","spring":"25","andere":"1"},"cupra":{"arona":"4","ateca":"2","born":"7","formentor":"5","ibiza":"3","leon":"6","tavascan":"8","terramar":"9","andere":"1"},"honda":{"accord":"2","aerodeck":"21","city":"22","civic":"3","clarity":"25","concerto":"4","cr-v":"5","crx":"6","cr-z":"24","e":"39","e:ny1":"557","element":"7","fr-v":"8","hr-v":"9","insight":"10","integra":"11","jazz":"12","legend":"13","logo":"14","nsx":"15","odyssey":"16","pilot":"23","prelude":"17","ridgeline":"26","s2000":"18","shuttle":"19","stream":"20","zr-v":"558","andere":"1"},"suzuki":{"(sx4) s-cross":"12","across":"342","alto":"2","baleno":"3","cappuccino":"4","carry":"5","celerio":"13","e vitara":"375","grand vitara":"6","ignis":"7","ik-2":"14","jimny":"8","kizashi":"11","liana":"9","lj":"10","sj samurai":"17","splash":"24","super-carry":"18","swace":"341","swift":"19","sx4":"23","vitara":"20","wagon r+":"21","x-90":"22","andere":"1"},"jaguar":{"daimler":"2","e-pace":"8","e-type":"20","f-pace":"6","f-type":"3","i-pace":"18","mk ii":"5","s-type":"7","xe":"4","xf":"24","xj":"9","xj12":"10","xj40":"11","xj6":"12","xj8":"13","xjr":"14","xjs":"15","xjsc":"16","xk":"23","xk8":"17","xkr":"22","x-type":"19","andere":"1"},"land rover":{"defender":"2","discovery":"3","discovery sport":"11","freelander":"4","range rover":"5","range rover evoque":"10","range rover sport":"6","range rover velar":"12","serie i":"7","serie ii":"8","serie iii":"9","andere":"1"},"mitsubishi":{"3000 gt":"2","asx":"21","canter":"4","carisma":"5","colt":"6","cordia":"7","cosmos":"8","diamante":"32","eclipse":"9","eclipse cross":"36","galant":"10","galloper":"11","grandis":"12","i-miev":"25","l200":"13","l300":"14","l400":"15","lancer":"16","mirage":"34","montero":"17","outlander":"18","pajero":"19","pajero pinin":"20","pick-up":"33","plug-in hybrid outlander":"35","santamo":"22","sapporo":"23","sigma":"24","space gear":"26","space runner":"27","space star":"28","space wagon":"29","starion":"30","tredia":"31","andere":"1"},"smart":{"#1":"6","#3":"7","#5":"8","crossblade":"2","forfour":"3","fortwo":"4","roadster":"5","andere":"1"},"lexus":{"ct 200h":"30","es-serie (alle)":"g36","es-serie":"g36","es 300":"17","es 330":"26","es 350":"23","gs-serie (alle)":"g32","gs-serie":"g32","gs 250":"31","gs 300":"2","gs 350":"27","gs 430":"3","gs 450":"18","gs 460":"24","gs f":"37","gx series (alle)":"g67","gx series":"g67","gx 460":"61","gx 470":"4","gx 550":"73","is-serie (alle)":"g28","is-serie":"g28","is 200":"5","is 220":"15","is 250":"16","is 300":"6","is 350":"28","is-f":"21","lbx":"72","lc 500":"41","lc 500h":"42","lfa":"36","lm":"69","ls-serie (alle)":"g33","ls-serie":"g33","ls 400":"7","ls 430":"8","ls 460":"19","ls 500":"43","ls 600":"20","lx-serie (alle)":"g34","lx-serie":"g34","lx 450":"60","lx 470":"9","lx 500":"70","lx 570":"25","lx 600":"71","lx 700":"74","nx-serie (alle)":"g56","nx-serie":"g56","nx 200":"32","nx 300":"33","nx 350h":"62","nx 450h":"63","rc-serie (alle)":"g63","rc-serie":"g63","rc 200":"35","rc 300":"38","rc 350":"40","rc f":"34","rx-serie (alle)":"g35","rx-serie":"g35","rx 200":"39","rx 300":"10","rx 330":"11","rx 350":"22","rx 400":"12","rx 450":"29","rx 500":"64","rz":"65","sc 400":"13","sc 430":"14","ux":"49","andere":"1"}}};

  function buildMobileDeUrl(brand, model, km, ez) {
    const bl = (brand || '').trim().toLowerCase();
    const ml = (model || '').trim().toLowerCase();
    const brandCode = _MOB_CODES.brands[bl];
    if (!brandCode) return null;
    const brandModels = _MOB_CODES.models[bl];
    if (!brandModels) return null;
    // Fuzzy model match: try exact, then partial
    let modelCode = brandModels[ml];
    if (!modelCode) {
      for (const [k, v] of Object.entries(brandModels)) {
        if (ml.includes(k) || k.includes(ml)) { modelCode = v; break; }
      }
    }
    if (!modelCode) return null;

    // Year from ez_text or first_reg
    let year = null;
    const ym = (ez || '').match(/(19\d{2}|20\d{2})/);
    if (ym) year = parseInt(ym[1]);

    // KM ±10%
    let kmMin = '', kmMax = '';
    const kmInt = parseInt(km);
    if (kmInt > 0) {
      kmMin = Math.round(kmInt * 0.9);
      kmMax = Math.round(kmInt * 1.1);
    }

    let url = 'https://suchen.mobile.de/fahrzeuge/search.html?isSearchRequest=true&s=Car&vc=Car&dam=false';
    url += `&ms=${brandCode}%3B${modelCode}%3B%3B`;
    if (year) url += `&fr=${year}%3A${year}`;
    if (kmMin && kmMax) url += `&ml=${kmMin}%3A${kmMax}`;
    url += '&ref=dsp';
    return url;
  }

  function initMobileLinks(scope) {
    (scope || document).querySelectorAll('[data-mob-link]').forEach(el => {
      if (el.dataset.mobBound === '1') return;
      el.dataset.mobBound = '1';

      const brand = el.getAttribute('data-brand');
      const model = el.getAttribute('data-model');
      const km = el.getAttribute('data-km');
      const ez = el.getAttribute('data-ez');
      const url = buildMobileDeUrl(brand, model, km, ez);
      const statsEl = el.querySelector('[data-mob-stats]');

      if (url) {
        el.href = url;
        if (statsEl) {
          statsEl.textContent = '↗';
          // Check cache first
          fetch('/api/mobile_price?url=' + encodeURIComponent(url), { cache: 'no-store' })
            .then(r => r.json())
            .then(j => {
              if (j.ok && j.cached && j.count > 0) {
                const avg = (j.avg_price || 0).toLocaleString('de-DE');
                statsEl.textContent = `${j.count}× · Ø ${avg} €`;
              } else {
                statsEl.textContent = '↗';
              }
            })
            .catch(() => { statsEl.textContent = '↗'; });
        }
      } else {
        el.style.opacity = '0.3';
        el.style.pointerEvents = 'none';
        if (statsEl) statsEl.textContent = '—';
      }
    });
  }

  // --- Initial load ---
  autosync();
  initBadges();
  initFavorites();
  initDealScores();
  initPriceHistory();
  initMobileLinks();
  setInterval(autosync, REFRESH_MS);

  // --- Favorites ---
  let _favCache = {}; // {id: {status, note}}
  let _favFilter = 'all';

  async function loadFavs() {
    try {
      const r = await fetch('/api/favs', { cache: 'no-store' });
      const j = await r.json();
      if (j.ok) { _favCache = {}; (j.favs||[]).forEach(f => _favCache[f.listing_id] = f); }
    } catch {}
  }

  function applyFavUI(scope) {
    const root = scope || document;
    // Update heart buttons
    root.querySelectorAll('[data-fav-id]').forEach(btn => {
      const id = btn.getAttribute('data-fav-id');
      const fav = _favCache[id];
      btn.textContent = fav ? '⭐' : '🤍';
    });
    // Update status badges
    root.querySelectorAll('[data-favstatus-id]').forEach(el => {
      const id = el.getAttribute('data-favstatus-id');
      const fav = _favCache[id];
      if (fav && fav.status) {
        el.style.display = '';
        el.innerHTML = `<span class="fav-status s-${fav.status}">${fav.status}</span>` +
          (fav.note ? ` <span style="font-size:11px;color:var(--text-dim);margin-left:4px">${fav.note}</span>` : '');
      } else {
        el.style.display = 'none';
        el.innerHTML = '';
      }
    });
    // Update fav count
    const cnt = Object.keys(_favCache).length;
    const cntEl = document.getElementById('favCount');
    if (cntEl) cntEl.textContent = `⭐ ${cnt}`;
    // Apply filter
    applyFavFilter();
  }

  function applyFavFilter() {
    document.querySelectorAll('.listing-card').forEach(card => {
      const id = card.getAttribute('data-row-id');
      const fav = _favCache[id];
      if (_favFilter === 'all') { card.style.display = ''; }
      else if (_favFilter === 'favs') { card.style.display = fav ? '' : 'none'; }
      else { card.style.display = (fav && fav.status === _favFilter) ? '' : 'none'; }
    });
  }

  async function toggleFav(id) {
    const fav = _favCache[id];
    if (fav) {
      // Cycle status: interessant → anrufen → besichtigt → gekauft → abgelehnt → remove
      const cycle = ['interessant','anrufen','besichtigt','gekauft','abgelehnt'];
      const idx = cycle.indexOf(fav.status);
      if (idx >= 0 && idx < cycle.length - 1) {
        // Next status
        const next = cycle[idx + 1];
        await fetch('/api/fav', { method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({id, status: next}) });
        _favCache[id] = {...fav, status: next};
      } else {
        // Remove
        await fetch('/api/fav', { method:'DELETE', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({id}) });
        delete _favCache[id];
      }
    } else {
      // Add as interessant
      await fetch('/api/fav', { method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({id, status:'interessant'}) });
      _favCache[id] = {listing_id: id, status:'interessant', note:''};
    }
    applyFavUI();
  }

  function bindFavButtons(scope) {
    (scope || document).querySelectorAll('[data-fav-id]').forEach(btn => {
      if (btn.dataset.favBound === '1') return;
      btn.dataset.favBound = '1';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        btn.classList.add('saved');
        setTimeout(() => btn.classList.remove('saved'), 300);
        toggleFav(btn.getAttribute('data-fav-id'));
      });
    });
  }

  async function initFavorites() {
    await loadFavs();
    applyFavUI();
    bindFavButtons();
    // Tab buttons
    document.querySelectorAll('[data-fav-filter]').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('[data-fav-filter]').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        _favFilter = tab.getAttribute('data-fav-filter');
        applyFavFilter();
      });
    });
  }

  // --- Deal Score ---
  async function loadDealScore(el) {
    const id = el.getAttribute('data-score-id');
    try {
      const r = await fetch('/api/deal_score?id=' + encodeURIComponent(id), { cache: 'no-store' });
      const j = await r.json();
      if (j.ok && j.score !== null && j.score !== undefined) {
        const s = j.score;
        el.textContent = s;
        el.classList.remove('loading');
        if (s >= 75) el.classList.add('s-great');
        else if (s >= 50) el.classList.add('s-good');
        else if (s >= 25) el.classList.add('s-ok');
        else el.classList.add('s-bad');
      } else {
        el.textContent = '—';
        el.classList.remove('loading');
      }
    } catch { el.textContent = '—'; el.classList.remove('loading'); }
  }

  async function initDealScores(scope) {
    const root = scope || document;
    for (const el of root.querySelectorAll('[data-score-id]')) {
      await loadDealScore(el);
    }
  }

  // --- Price History ---
  async function initPriceHistory(scope) {
    const root = scope || document;
    for (const el of root.querySelectorAll('[data-pricehist-id]')) {
      const id = el.getAttribute('data-pricehist-id');
      try {
        const r = await fetch('/api/price_history?id=' + encodeURIComponent(id), { cache: 'no-store' });
        const j = await r.json();
        if (j.ok && j.changes && j.changes.length > 0) {
          const last = j.changes[j.changes.length - 1];
          const diff = last.diff;
          if (diff < 0) {
            el.className = 'tag tag-price-down';
            el.innerHTML = `<span class="icon">↓</span> ${Math.abs(diff).toLocaleString('de-DE')} € gesenkt`;
            el.style.display = '';
          } else if (diff > 0) {
            el.className = 'tag tag-price-up';
            el.innerHTML = `<span class="icon">↑</span> ${diff.toLocaleString('de-DE')} € erhöht`;
            el.style.display = '';
          }
        }
      } catch {}
    }
  }

  // Override reloadCards to also init new features
  const _origReload = reloadCards;
  reloadCards = async function() {
    await _origReload();
    bindFavButtons();
    applyFavUI();
    initDealScores();
    initPriceHistory();
    initMobileLinks();
  };
});
</script>
</body>
</html>
"""

# ============================================================
# Cards partial (for AJAX reload)
# ============================================================
CARDS_TPL = r"""
{% for r in rows %}
<div class="listing-card"
     data-row-id="{{ r['id'] }}"
     data-price-eur="{{ r['price_eur'] or '' }}">

  <div class="card-top">
    <button class="fav-btn" data-fav-id="{{ r['id'] }}" title="Favorit">🤍</button>
    <div class="card-title">{{ r['title'] or '—' }}</div>
    <div class="card-price">
      {% if r['price_eur'] %}{{ '{:,}'.format(r['price_eur']).replace(',', '.') }} €{% else %}—{% endif %}
    </div>
    <div class="deal-score loading" data-score-id="{{ r['id'] }}">…</div>
  </div>

  <div class="fav-status-wrap" data-favstatus-id="{{ r['id'] }}" style="display:none; margin-top:6px;"></div>

  <div class="card-meta">
    {% if r['km'] %}<span class="tag"><span class="icon">🛣</span> {{ '{:,}'.format(r['km']).replace(',', '.') }} km</span>{% endif %}
    {% if r['ez_text'] %}<span class="tag"><span class="icon">📅</span> {{ r['ez_text'] }}</span>{% endif %}
    {% if r['city'] %}<span class="tag"><span class="icon">📍</span> {{ r['city'] }}</span>{% endif %}
    {% if r['postal_code'] %}<span class="tag">{{ r['postal_code'] }}</span>{% endif %}
    {% if r['pics'] is not none %}<span class="tag"><span class="icon">📸</span> {{ r['pics'] }}</span>{% endif %}
    {% if r['posted_at'] %}<span class="tag tag-green"><span class="icon">🕐</span> {{ r['posted_at'] }}</span>{% endif %}
    <span class="tag" data-pricehist-id="{{ r['id'] }}" style="display:none"></span>
    <span class="tag" style="opacity:0.5">{{ r['id'] }}</span>
  </div>

  <div class="card-comparisons">
    {% if r['brand'] and r['brand']|trim and r['model'] and r['model']|trim %}
      {% set sim = build_similar_search_url(r) %}
      {% if sim %}
      <a class="cmp-badge cmp-ka" href="{{ sim }}" target="_blank" rel="noopener">
        KA <span class="cmp-stats" data-sim-id="{{ r['id'] }}">…</span>
      </a>
      {% endif %}

      {% set as_url = build_autoscout_search_url(r) %}
      {% if as_url %}
      <a class="cmp-badge cmp-as" href="{{ as_url }}" target="_blank" rel="noopener">
        AS24 <span class="cmp-stats" data-as-id="{{ r['id'] }}">…</span>
      </a>
      {% endif %}

      {% set cw_url = build_carwow_search_url(r) %}
      {% if cw_url %}
      <a class="cmp-badge cmp-cw" href="{{ cw_url }}" target="_blank" rel="noopener">
        CW <span class="cmp-stats" data-cw-id="{{ r['id'] }}">…</span>
      </a>
      {% endif %}

      <a class="cmp-badge cmp-mob" href="#" data-mob-link
         data-brand="{{ (r['brand'] or '')|e }}"
         data-model="{{ (r['model'] or '')|e }}"
         data-km="{{ r['km'] or '' }}"
         data-ez="{{ (r['first_reg'] or r['ez_text'] or '')|e }}"
         target="_blank" rel="noopener">
        Mobile <span class="cmp-stats" data-mob-stats>…</span>
      </a>
    {% endif %}
  </div>

  <div class="card-actions">
    <a class="card-btn card-btn-accent" href="{{ r['url'] }}" target="_blank" rel="noopener">
      ↗ Öffnen
    </a>
    <button type="button" class="card-btn"
            data-prompt-btn
            data-row-id="{{ r['id'] }}"
            data-title="{{ (r['title'] or '')|e }}"
            data-url="{{ (r['url'] or '')|e }}"
            data-price="{{ r['price_eur'] or '' }}"
            data-km="{{ r['km'] or '' }}"
            data-city="{{ (r['city'] or '')|e }}"
            data-posted="{{ (r['posted_at'] or '')|e }}">
      📋 Prompt
    </button>
  </div>
</div>
{% endfor %}

{% if not rows %}
<div class="empty-state">
  <div class="icon">🚗</div>
  <div class="msg">Keine Treffer</div>
</div>
{% endif %}
"""

# ============================================================
# API routes (unchanged logic, same as original)
# ============================================================
import threading
from time import monotonic
_sync_lock = threading.Lock()
_last_sync_ts = 0.0
from datetime import datetime, timedelta
import re

def _rval(r, k):
    try: return r[k]
    except: return None

def _extract_year_from_row(r):
    s = (_rval(r, "first_reg") or _rval(r, "ez_text") or "")
    m = re.search(r"(19\d{2}|20\d{2})", s)
    return int(m.group(1)) if m else None

def _parse_posted_at(r):
    s = (_rval(r, "posted_at") or "").strip()
    if not s: return None
    try: return datetime.fromisoformat(s.replace("T", " "))
    except: return None


def _notify_matches(new_rows):
    conn = get_db(); cur = conn.cursor()
    subs = list(cur.execute("SELECT endpoint,p256dh,auth,filters,max_price FROM push_subscriptions"))

    for r in new_rows:
        rid = str(r["id"])
        price = r["price_eur"]

        for (endpoint,p256dh,auth,filters,max_price) in subs:
            chk = cur.execute("SELECT 1 FROM push_sent WHERE endpoint=? AND listing_id=?", (endpoint, rid)).fetchone()
            if chk: continue

            params = {}
            if filters:
                from urllib.parse import parse_qs
                for k, v in parse_qs(filters, keep_blank_values=True).items():
                    params[k] = v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else "")

            ok = True

            if "price_max" in params and price is not None:
                try:
                    if int(price) > int(params["price_max"] or 0): ok = False
                except: pass

            if max_price and price is not None:
                try:
                    if int(price) > int(max_price): ok = False
                except: pass

            if ok and "km_max" in params and r["km"] is not None:
                try:
                    if int(r["km"]) > int(params["km_max"] or 0): ok = False
                except: pass

            if ok and params.get("postal_prefix"):
                if not (r["postal_code"] or "").startswith(params["postal_prefix"]): ok = False

            if ok and params.get("city"):
                if params["city"].lower() not in (r["city"] or "").lower(): ok = False

            if ok and params.get("q"):
                if params["q"].lower() not in (r["title"] or "").lower(): ok = False

            if ok and params.get("price_min") and r["price_eur"] is not None:
                try:
                    if int(r["price_eur"]) < int(params["price_min"] or 0): ok = False
                except: pass

            if ok and (params.get("price_max") or max_price) and r["price_eur"] is None:
                ok = False

            if ok and (params.get("ez_min") or params.get("ez_max")):
                y = _extract_year_from_row(r)
                if y is None: ok = False
                else:
                    try:
                        if params.get("ez_min") and y < int(params["ez_min"]): ok = False
                        if params.get("ez_max") and y > int(params["ez_max"]): ok = False
                    except: pass

            if ok and params.get("pics_min"):
                try:
                    if int(r["pics"] if r["pics"] is not None else 0) < int(params["pics_min"]): ok = False
                except: pass

            if ok and params.get("posted_days"):
                try:
                    pd = int(params["posted_days"])
                    dt = _parse_posted_at(r)
                    if dt is None: ok = False
                    elif dt < (datetime.now() - timedelta(days=pd)): ok = False
                except: pass

            if not ok: continue

            payload = {
                "title": "Neues Angebot",
                "body": f"{r['title']} — {r['price_eur'] or '—'} € • {r['city'] or ''}",
                "url": r["url"]
            }
            try:
                webpush(
                    subscription_info={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}},
                    data=json.dumps(payload),
                    vapid_private_key=Vapid.from_pem(VAPID_PRIVATE_PEM.encode() if isinstance(VAPID_PRIVATE_PEM, str) else VAPID_PRIVATE_PEM),
                    vapid_claims={"sub": PUSH_SUBJECT},
                )
                cur.execute("INSERT OR IGNORE INTO push_sent(endpoint, listing_id) VALUES(?,?)", (endpoint, rid))
                conn.commit()
            except WebPushException:
                cur.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,))
                conn.commit()
    conn.close()


@app.get("/api/sync")
def api_sync():
    global _last_sync_ts
    now = monotonic()
    if (now - _last_sync_ts) < 90.0:
        return {"ok": True, "seen": 0, "stored": 0, "changed": False}

    if not _sync_lock.acquire(blocking=False):
        return {"ok": True, "seen": 0, "stored": 0, "changed": False}

    try:
        from db import init_db
        init_db()
        from scrape_ebay import sync_once
        res = sync_once()
        _last_sync_ts = monotonic()
        changed = (res.get("stored", 0) > 0)

        if changed:
            conn = get_db(); cur = conn.cursor()
            try:
                cur.execute("""SELECT id,title,price_eur,km,city,url,posted_at,postal_code,ez_text,first_reg,pics
                    FROM listings WHERE posted_at IS NOT NULL ORDER BY posted_at DESC, last_seen DESC LIMIT 50""")
            except:
                cur.execute("""SELECT id,title,price_eur,km,city,url,posted_at,postal_code,ez_text,first_reg,pics
                    FROM listings ORDER BY last_seen DESC LIMIT 50""")
            new_rows = cur.fetchall()
            conn.close()
            _notify_matches(new_rows)

        return {"ok": True, **res, "changed": changed}
    finally:
        _sync_lock.release()


# --- Kleinanzeigen Stats ---
@app.get("/api/similar_stats")
def api_similar_stats():
    lid = request.args.get("id")
    if not lid: return {"ok": False, "error": "missing id"}, 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone(); conn.close()
    if not row: return {"ok": False, "error": "not found"}, 404

    brand = (row["brand"] or "").strip()
    model = (row["model"] or "").strip()
    if not brand or not model:
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    url = build_similar_search_url(row)
    if not url: return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}
    return fetch_ka_stats(url), 200


# --- AutoScout Stats ---
@app.get("/api/autoscout_stats")
def api_autoscout_stats():
    lid = request.args.get("id")
    if not lid: return {"ok": False, "error": "missing id"}, 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone(); conn.close()
    if not row: return {"ok": False, "error": "not found"}, 404

    if not (row["brand"] or "").strip() or not (row["model"] or "").strip():
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}

    url = build_autoscout_search_url(row)
    if not url: return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}
    return fetch_autoscout_stats(url), 200


# --- Mobile.de Stats ---
import urllib.parse
import requests

def _is_mobile_search_url(u: str) -> bool:
    if not u: return False
    u = u.lower()
    return ("suchen.mobile.de/fahrzeuge/search.html" in u) or ("m.mobile.de/auto/search.html" in u)

def _page2_mobile(u: str) -> str:
    try:
        pr = urllib.parse.urlparse(u)
        qs = urllib.parse.parse_qs(pr.query, keep_blank_values=True)
        qs["pageNumber"] = ["2"]
        new_qs = urllib.parse.urlencode({k: v if isinstance(v, list) else [v] for k, v in qs.items()}, doseq=True)
        return urllib.parse.urlunparse((pr.scheme, pr.netloc, pr.path, pr.params, new_qs, pr.fragment))
    except: return u

_MOBILE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

def _fetch_text_requests(u, timeout=12.0):
    s = requests.Session()
    s.headers.update(_MOBILE_HEADERS)
    r = s.get(u, timeout=timeout, allow_redirects=True)
    return r.text or ""

_MOBILE_SRP_COUNT_RE = re.compile(r'<h1[^>]*data-testid=[\'"]srp-title[\'"][^>]*>.*?([0-9][0-9\.\s]*)\s*(?:<!--.*?-->)*\s*(?:[A-Za-zÄÖÜäöüß0-9\.\-\s]*?)\s*(?:<!--.*?-->)*\s*Angebot', re.IGNORECASE|re.DOTALL)
_MOBILE_PRICE_SPAN_RE = re.compile(r'<span[^>]*data-testid=["\']price-label["\'][^>]*>(?P<val>.*?)</span>', re.IGNORECASE|re.DOTALL)
_MOBILE_EURO_NUM_RE = re.compile(r'(\d{1,3}(?:[.\s]\d{3})*|\d+)')
import html as _html

def _extract_srp_count_mobile(html_text):
    if not html_text: return 0
    m = _MOBILE_SRP_COUNT_RE.search(html_text)
    if not m: return 0
    raw = (m.group(1) or '').replace('.','').replace(' ','').replace('\xa0','')
    try: return int(raw)
    except: return 0

def _extract_prices_mobile(html_text):
    prices = []
    if not html_text: return prices
    for s in _MOBILE_PRICE_SPAN_RE.finditer(html_text):
        raw = _html.unescape(s.group('val') or '')
        raw = re.sub(r'<[^>]+>', '', raw)
        m = _MOBILE_EURO_NUM_RE.search(raw)
        if not m: continue
        num = (m.group(1) or '').replace('.','').replace(' ','').replace('\xa0','')
        try:
            val = int(num)
            if 500 <= val <= 500000: prices.append(val)
        except: pass
    return prices

@app.get("/api/mobile_stats")
def api_mobile_stats():
    url = request.args.get("url", "").strip()
    if not _is_mobile_search_url(url):
        return {"ok": False, "error": "Bitte eine gültige mobile.de Such-URL übergeben."}, 200
    try:
        html1 = _fetch_text_requests(url)
        announced = _extract_srp_count_mobile(html1)
        prices = _extract_prices_mobile(html1)
        if announced and len(prices) < announced:
            html2 = _fetch_text_requests(_page2_mobile(url))
            prices += _extract_prices_mobile(html2)
            if announced > 0: prices = prices[:announced]
        count = len(prices)
        avg = int(round(sum(prices) / count)) if count else 0
        if count == 0 and announced > 0:
            return {"ok": False, "error": "Keine Preise lesbar (Bot-Schutz?)"}, 200
        return {"ok": True, "count": count, "avg_price_eur": avg, "url": url}, 200
    except requests.RequestException as e:
        return {"ok": False, "error": f"Netzwerkfehler ({type(e).__name__})"}, 200
    except Exception as e:
        return {"ok": False, "error": f"Fehler ({type(e).__name__})"}, 200


# --- Carwow Stats ---
@app.get("/api/carwow_stats")
def api_carwow_stats():
    lid = request.args.get("id")
    if not lid: return {"ok": False, "error": "missing id"}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id, title, km, ez_text, brand, model, fuel, gearbox, first_reg
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone(); conn.close()
    if not row: return {"ok": False, "error": "not found"}, 404
    if not (row["brand"] or "").strip() or not (row["model"] or "").strip():
        return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}
    url = build_carwow_search_url(row)
    if not url: return {"ok": True, "count": 0, "avg_price_eur": 0, "url": ""}
    return fetch_carwow_stats(url), 200

@app.get("/api/carwow_stats_url")
def api_carwow_stats_url():
    url = request.args.get("url", "").strip()
    if not url: return {"ok": False, "error": "missing url"}, 400
    return fetch_carwow_stats(url), 200


# --- Mobile.de Bookmarklet Price Cache API ---
import hashlib

def _url_hash(url):
    """Kurzer Hash einer URL für Cache-Key."""
    return hashlib.md5(url.encode()).hexdigest()[:16]

@app.post("/api/mobile_price")
def api_mobile_price_save():
    """Bookmarklet schickt Preise hierher."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    prices = data.get("prices") or []
    if not url or not prices:
        return {"ok": False, "error": "missing url or prices"}, 400

    count = len(prices)
    avg = int(round(sum(prices) / count)) if count else 0
    h = _url_hash(url)

    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO mobile_price_cache(url_hash, search_url, count, avg_price, prices_json, updated_at)
                   VALUES(?,?,?,?,?,datetime('now'))
                   ON CONFLICT(url_hash) DO UPDATE SET count=excluded.count, avg_price=excluded.avg_price,
                   prices_json=excluded.prices_json, updated_at=datetime('now')""",
                (h, url, count, avg, json.dumps(prices)))
    conn.commit(); conn.close()
    return {"ok": True, "count": count, "avg_price": avg}

@app.get("/api/mobile_price")
def api_mobile_price_get():
    """Holt gecachte Preise für eine mobile.de URL."""
    url = (request.args.get("url") or "").strip()
    if not url:
        return {"ok": False}, 400
    h = _url_hash(url)
    conn = get_db(); cur = conn.cursor()
    row = cur.execute("SELECT count, avg_price, updated_at FROM mobile_price_cache WHERE url_hash=?", (h,)).fetchone()
    conn.close()
    if not row:
        return {"ok": True, "cached": False, "count": 0, "avg_price": 0}
    return {"ok": True, "cached": True, "count": row["count"], "avg_price": row["avg_price"], "updated_at": row["updated_at"]}

@app.get("/bookmarklet")
def bookmarklet_page():
    """Seite zum Installieren des mobile.de Bookmarklets."""
    # Dynamisch die Autoscan-URL ermitteln
    base_url = request.url_root.rstrip('/')
    return render_template_string(BOOKMARKLET_TPL, base_url=base_url)

BOOKMARKLET_TPL = r"""
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Autoscan – Mobile.de Preisscanner</title>
  <style>
    body { font-family: -apple-system, sans-serif; background: #0f172a; color: #f1f5f9; padding: 24px; max-width: 600px; margin: 0 auto; }
    h1 { font-size: 22px; margin-bottom: 8px; }
    .sub { color: #94a3b8; font-size: 14px; margin-bottom: 24px; }
    .tab-row { display: flex; gap: 8px; margin-bottom: 20px; }
    .tab { flex: 1; padding: 10px; border-radius: 10px; border: 1px solid #334155; background: #1e293b; color: #94a3b8; text-align: center; font-weight: 600; font-size: 14px; cursor: pointer; }
    .tab.active { background: #38bdf8; color: #0f172a; border-color: #38bdf8; }
    .panel { display: none; }
    .panel.active { display: block; }
    .step { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    .step-num { display: inline-block; width: 28px; height: 28px; border-radius: 50%; background: #38bdf8; color: #0f172a; text-align: center; line-height: 28px; font-weight: 700; font-size: 14px; margin-right: 8px; }
    .step-title { font-weight: 600; font-size: 15px; }
    .step-desc { color: #94a3b8; font-size: 13px; margin-top: 8px; line-height: 1.6; }
    .big-btn { display: block; width: 100%; margin-top: 12px; padding: 16px 20px; background: #fb923c; color: #0f172a; border-radius: 12px; font-weight: 700; font-size: 16px; text-align: center; border: none; cursor: pointer; font-family: inherit; text-decoration: none; }
    .big-btn:active { transform: scale(0.97); opacity: 0.9; }
    .big-btn-blue { background: #38bdf8; }
    .copy-msg { text-align: center; font-size: 13px; color: #34d399; margin-top: 8px; min-height: 20px; }
    .code-box { background: #334155; border-radius: 8px; padding: 12px; margin-top: 12px; font-family: monospace; font-size: 10px; color: #94a3b8; word-break: break-all; max-height: 60px; overflow: hidden; user-select: all; -webkit-user-select: all; }
    .warn { background: rgba(251,191,36,0.1); border: 1px solid rgba(251,191,36,0.3); border-radius: 10px; padding: 12px; color: #fbbf24; font-size: 13px; margin-top: 16px; line-height: 1.5; }
    .back-link { display: inline-block; margin-top: 20px; color: #38bdf8; font-size: 14px; }
  </style>
</head>
<body>
  <h1>📱 Mobile.de Preisscanner</h1>
  <div class="sub">Ø-Preise direkt von mobile.de scannen</div>

  <div class="tab-row">
    <div class="tab active" onclick="showPanel('iphone')">iPhone Shortcut</div>
    <div class="tab" onclick="showPanel('laptop')">Laptop Bookmarklet</div>
  </div>

  <!-- iPhone Panel -->
  <div class="panel active" id="panel-iphone">
    <div class="step">
      <span class="step-num">1</span>
      <span class="step-title">Shortcut installieren</span>
      <div class="step-desc">
        Tippe auf den Button unten. Die <strong>Kurzbefehle-App</strong> öffnet sich und fragt ob du den Shortcut hinzufügen willst. Tippe <strong>„Kurzbefehl hinzufügen"</strong>.
      </div>
      <a class="big-btn" id="shortcutLink" href="#" target="_blank">⚡ Shortcut installieren</a>
    </div>

    <div class="step">
      <span class="step-num">2</span>
      <span class="step-title">Benutzen</span>
      <div class="step-desc">
        1. In Autoscan: tippe auf <strong>„Mobile ↗"</strong> bei einem Auto<br>
        2. Mobile.de öffnet sich in Safari<br>
        3. Tippe auf <strong>Teilen</strong> (□↑ unten)<br>
        4. Scrolle runter und tippe auf <strong>„Preise scannen"</strong><br>
        5. Der Shortcut liest die Preise und zeigt dir den Ø-Preis
      </div>
    </div>

    <div class="warn">
      💡 Falls der Shortcut nicht im Teilen-Menü erscheint: Öffne die <strong>Kurzbefehle-App</strong> → halte „Preise scannen" gedrückt → <strong>Details</strong> → aktiviere <strong>„Im Share Sheet anzeigen"</strong>.
    </div>
  </div>

  <!-- Laptop Panel -->
  <div class="panel" id="panel-laptop">
    <div class="step">
      <span class="step-num">1</span>
      <span class="step-title">Bookmarklet speichern</span>
      <div class="step-desc">
        Ziehe den Button in deine <strong>Lesezeichenleiste</strong> (Chrome/Firefox/Safari):
      </div>
      <a class="big-btn" id="bmDragLink" href="#">⚡ Preise scannen</a>
      <div class="step-desc" style="margin-top:8px">
        Oder kopiere den Code und erstelle manuell ein Lesezeichen:
      </div>
      <button class="big-btn big-btn-blue" id="copyBtn" style="margin-top:8px">📋 Code kopieren</button>
      <div class="copy-msg" id="copyMsg"></div>
      <div class="code-box" id="codeBox"></div>
    </div>

    <div class="step">
      <span class="step-num">2</span>
      <span class="step-title">Benutzen</span>
      <div class="step-desc">
        Mobile.de Suche öffnen → Bookmarklet klicken → Preise werden gescannt und an Autoscan geschickt.
      </div>
    </div>
  </div>

  <a class="back-link" href="/">← Zurück zu Autoscan</a>

<script>
  const BASE = '{{ base_url }}';

  // --- Laptop Bookmarklet ---
  const bmCode = `javascript:void(function(){try{var lim=0;var h=document.querySelector('[data-testid=srp-title]');if(h){var txt=h.textContent||'';var nums=txt.match(/[0-9]+/g);if(nums)lim=parseInt(nums[0])}var all=document.querySelectorAll('[data-testid=price-label]');var ps=[];for(var i=0;i<all.length;i++){if(lim>0&&ps.length>=lim)break;var t=all[i].textContent.replace(/[^0-9]/g,'');if(t){var n=parseInt(t);if(n>=500&&n<=500000)ps.push(n)}}var c=ps.length;var a=c?Math.round(ps.reduce(function(s,v){return s+v},0)/c):0;var u=location.href;fetch('${BASE}/api/mobile_price',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url:u,prices:ps})}).then(function(r){return r.json()}).then(function(j){alert('Autoscan: '+c+' von '+lim+' Angeboten\\nØ-Preis: '+a.toLocaleString('de-DE')+' EUR')}).catch(function(){alert('Autoscan: '+c+' Preise, Ø '+a.toLocaleString('de-DE')+' EUR (offline)')})}catch(e){alert('Fehler: '+e.message)}})();`;

  document.getElementById('bmDragLink').href = bmCode;
  document.getElementById('codeBox').textContent = bmCode.slice(0, 100) + '…';

  document.getElementById('copyBtn').addEventListener('click', async () => {
    const msg = document.getElementById('copyMsg');
    try {
      await navigator.clipboard.writeText(bmCode);
      msg.textContent = '✓ Kopiert!';
      msg.style.color = '#34d399';
    } catch {
      const ta = document.createElement('textarea');
      ta.value = bmCode;
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.focus(); ta.select();
      try { document.execCommand('copy'); msg.textContent = '✓ Kopiert!'; msg.style.color = '#34d399'; }
      catch { msg.textContent = 'Bitte manuell kopieren'; msg.style.color = '#fb7185'; document.getElementById('codeBox').textContent = bmCode; document.getElementById('codeBox').style.maxHeight = 'none'; }
      document.body.removeChild(ta);
    }
  });

  // --- iOS Shortcut ---
  // Build the Shortcut iCloud link dynamically
  // The shortcut contains: "Run JavaScript on Webpage" action
  const jsForShortcut = `
var lim = 0;
var h = document.querySelector('[data-testid="srp-title"]');
if (h) {
  var nums = h.textContent.match(/[0-9]+/g);
  if (nums) lim = parseInt(nums[0]);
}
var all = document.querySelectorAll('[data-testid="price-label"]');
var ps = [];
for (var i = 0; i < all.length; i++) {
  if (lim > 0 && ps.length >= lim) break;
  var t = all[i].textContent.replace(/[^0-9]/g, '');
  if (t) {
    var n = parseInt(t);
    if (n >= 500 && n <= 500000) ps.push(n);
  }
}
var c = ps.length;
var avg = c ? Math.round(ps.reduce(function(s,v){return s+v},0)/c) : 0;
var u = location.href;
var result = {count: c, avg: avg, limit: lim};
fetch('${BASE}/api/mobile_price', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({url: u, prices: ps})
}).then(function() {
  completion(result);
}).catch(function() {
  completion(result);
});
`.trim();

  // Since we can't auto-generate iCloud shortcut links, show manual instructions
  document.getElementById('shortcutLink').addEventListener('click', function(e) {
    e.preventDefault();
    // Copy the JS code so user can paste it into Shortcuts
    navigator.clipboard.writeText(jsForShortcut).then(function() {
      alert('JavaScript-Code kopiert!\\n\\nJetzt:\\n1. Öffne Kurzbefehle-App\\n2. Neuer Kurzbefehl (+)\\n3. Aktion: „JavaScript auf Webseite ausführen"\\n4. Lösche den Beispielcode und füge ein\\n5. Benenne ihn „Preise scannen"\\n6. Im Share Sheet aktivieren');
    }).catch(function() {
      alert('Konnte nicht kopieren. Bitte manuell:\\n\\n' + jsForShortcut.slice(0, 200) + '...');
    });
  });

  // --- Tab switching ---
  function showPanel(id) {
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.getElementById('panel-' + id).classList.add('active');
    event.target.classList.add('active');
  }
  window.showPanel = showPanel;
</script>
</body>
</html>
"""


# --- Favorites API ---
@app.get("/api/favs")
def api_favs():
    conn = get_db(); cur = conn.cursor()
    rows = cur.execute("SELECT listing_id, status, note, created_at FROM favorites ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"ok": True, "favs": [{"listing_id": r["listing_id"], "status": r["status"], "note": r["note"] or ""} for r in rows]}

@app.post("/api/fav")
def api_fav_set():
    data = request.get_json(force=True, silent=True) or {}
    lid = (data.get("id") or "").strip()
    status = (data.get("status") or "interessant").strip()
    note = (data.get("note") or "").strip()
    if not lid: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO favorites(listing_id, status, note) VALUES(?,?,?) ON CONFLICT(listing_id) DO UPDATE SET status=excluded.status, note=excluded.note",
                (lid, status, note))
    conn.commit(); conn.close()
    return {"ok": True}

@app.delete("/api/fav")
def api_fav_del():
    data = request.get_json(force=True, silent=True) or {}
    lid = (data.get("id") or "").strip()
    if not lid: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM favorites WHERE listing_id=?", (lid,))
    conn.commit(); conn.close()
    return {"ok": True}


# --- Deal Score API ---
@app.get("/api/deal_score")
def api_deal_score():
    """
    Deal-Score (0-100) basierend auf:
    - Preis vs KA-Durchschnitt (40 Punkte)
    - Preis vs AS24-Durchschnitt (30 Punkte)
    - Anzahl Bilder (10 Punkte)
    - Standzeit/Alter des Inserats (10 Punkte)
    - Hat Marke+Modell (10 Punkte)
    """
    lid = request.args.get("id")
    if not lid: return {"ok": False, "error": "missing id"}, 400

    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT id, title, price_eur, km, ez_text, brand, model, fuel, gearbox, first_reg,
                          posted_at, pics, city
                   FROM listings WHERE id = ?""", (lid,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return {"ok": False, "error": "not found"}, 404

    price = row["price_eur"]
    if price is None or price <= 0:
        conn.close()
        return {"ok": True, "score": None, "detail": "kein Preis"}

    score = 0
    detail = {}

    # --- Datenqualität (10 Punkte) ---
    brand = (row["brand"] or "").strip()
    model = (row["model"] or "").strip()
    if brand and model:
        score += 10
        detail["data"] = 10
    elif brand or model:
        score += 5
        detail["data"] = 5
    else:
        detail["data"] = 0

    # --- Bilder (10 Punkte) ---
    pics = row["pics"] or 0
    if pics >= 8: score += 10
    elif pics >= 5: score += 7
    elif pics >= 3: score += 4
    elif pics >= 1: score += 2
    detail["pics"] = min(pics, 10)

    # --- Standzeit (10 Punkte) - frische Inserate = besser ---
    posted = row["posted_at"]
    if posted:
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(posted.replace("T", " "))
            age_days = (datetime.now() - dt).days
            if age_days <= 1: score += 10
            elif age_days <= 3: score += 8
            elif age_days <= 7: score += 5
            elif age_days <= 14: score += 3
            else: score += 1
            detail["age_days"] = age_days
        except:
            score += 3
    else:
        score += 3

    # --- KA Vergleich (40 Punkte) ---
    ka_avg = None
    if brand and model:
        try:
            url = build_similar_search_url(row)
            if url:
                stats = fetch_ka_stats(url)
                if stats.get("ok") and stats.get("count", 0) > 0:
                    ka_avg = stats["avg_price_eur"]
        except: pass

    if ka_avg and ka_avg > 0:
        ratio = price / ka_avg
        if ratio <= 0.70: score += 40    # 30%+ unter Markt
        elif ratio <= 0.80: score += 35  # 20-30% unter
        elif ratio <= 0.90: score += 28  # 10-20% unter
        elif ratio <= 0.95: score += 20  # 5-10% unter
        elif ratio <= 1.00: score += 15  # bis Marktpreis
        elif ratio <= 1.05: score += 10  # leicht drüber
        elif ratio <= 1.10: score += 5   # 10% drüber
        else: score += 0                  # zu teuer
        detail["ka_avg"] = ka_avg
        detail["ka_ratio"] = round(ratio, 2)
    else:
        score += 15  # Neutral wenn kein Vergleich
        detail["ka_avg"] = None

    # --- AS24 Vergleich (30 Punkte) ---
    as_avg = None
    if brand and model:
        try:
            url = build_autoscout_search_url(row)
            if url:
                stats = fetch_autoscout_stats(url)
                if stats.get("ok") and stats.get("count", 0) > 0:
                    as_avg = stats["avg_price_eur"]
        except: pass

    if as_avg and as_avg > 0:
        ratio = price / as_avg
        if ratio <= 0.70: score += 30
        elif ratio <= 0.80: score += 25
        elif ratio <= 0.90: score += 20
        elif ratio <= 0.95: score += 15
        elif ratio <= 1.00: score += 10
        elif ratio <= 1.05: score += 5
        else: score += 0
        detail["as_avg"] = as_avg
        detail["as_ratio"] = round(ratio, 2)
    else:
        score += 10
        detail["as_avg"] = None

    score = max(0, min(100, score))

    # Cache in DB
    try:
        cur.execute("INSERT INTO deal_scores(listing_id,score,ka_avg,as_avg) VALUES(?,?,?,?) ON CONFLICT(listing_id) DO UPDATE SET score=excluded.score,ka_avg=excluded.ka_avg,as_avg=excluded.as_avg,updated_at=datetime('now')",
                    (lid, score, ka_avg, as_avg))
        conn.commit()
    except: pass
    conn.close()

    return {"ok": True, "score": score, "detail": detail}


# --- Price History API ---
@app.get("/api/price_history")
def api_price_history():
    lid = request.args.get("id")
    if not lid: return {"ok": False, "error": "missing id"}, 400

    conn = get_db(); cur = conn.cursor()
    rows = cur.execute("SELECT price_eur, seen_at FROM listing_prices WHERE listing_id=? ORDER BY seen_at ASC", (lid,)).fetchall()
    conn.close()

    if not rows or len(rows) < 2:
        return {"ok": True, "changes": []}

    changes = []
    prev = rows[0]["price_eur"]
    for r in rows[1:]:
        p = r["price_eur"]
        if p is not None and prev is not None and p != prev:
            changes.append({"from": prev, "to": p, "diff": p - prev, "at": r["seen_at"]})
        if p is not None:
            prev = p

    return {"ok": True, "changes": changes}


# --- Push API ---
@app.post("/api/push/subscribe")
def api_push_sub():
    data = request.get_json(force=True, silent=True) or {}
    sub = data.get("subscription") or {}
    filt = data.get("filters") or ""
    maxp = data.get("max_price")
    if not (sub.get("endpoint") and sub.get("keys",{}).get("p256dh") and sub["keys"].get("auth")):
        return {"ok": False, "error": "bad subscription"}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO push_subscriptions(endpoint,p256dh,auth,filters,max_price)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh,auth=excluded.auth,filters=excluded.filters,max_price=excluded.max_price""",
                   (sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], filt, maxp))
    conn.commit(); conn.close()
    return {"ok": True}

@app.post("/api/push/unsubscribe")
def api_push_unsub():
    data = request.get_json(force=True, silent=True) or {}
    ep = (data.get("endpoint") or "").strip()
    if not ep: return {"ok": False}, 400
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM push_subscriptions WHERE endpoint=?", (ep,))
    cur.execute("DELETE FROM push_sent WHERE endpoint=?", (ep,))
    conn.commit(); conn.close()
    return {"ok": True}

@app.get("/api/push/list")
def api_push_list():
    conn = get_db(); cur = conn.cursor()
    rows = cur.execute("SELECT endpoint, filters, max_price, created_at FROM push_subscriptions ORDER BY created_at DESC").fetchall()
    conn.close()
    return {"ok": True, "subs": [{"endpoint": r["endpoint"], "filters": r["filters"] or "", "max_price": r["max_price"], "created_at": r["created_at"]} for r in rows]}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"[i] Datenbank: {DB_PATH}")
    print(f"[i] Start auf 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
