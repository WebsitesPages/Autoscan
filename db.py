import sqlite3

DB_PATH = "autos.db"

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def ensure_column(conn, table, col, type_sql):
    cur = conn.execute(f"PRAGMA table_info({table})")
    cols = {r[1] for r in cur.fetchall()}
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_sql}")

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
      CREATE TABLE IF NOT EXISTS listings (
        id TEXT PRIMARY KEY,
        platform TEXT,
        url TEXT,
        title TEXT,
        price_eur INTEGER,
        km INTEGER,
        ez_text TEXT,
        location TEXT,
        postal_code TEXT,
        city TEXT,
        posted_at TEXT,
        pics INTEGER,
        brand TEXT,
        model TEXT,
        fuel TEXT,
        power_ps INTEGER,
        gearbox TEXT,
        doors TEXT,
        hu_until TEXT,
        emission_class TEXT,
        color TEXT,
        upholstery TEXT,
        first_reg TEXT,
        first_seen TEXT DEFAULT (datetime('now')),
        last_seen  TEXT DEFAULT (datetime('now')),
        status TEXT DEFAULT 'active'
      )
    """)
    cur.execute("""
      CREATE TABLE IF NOT EXISTS listing_prices (
        listing_id TEXT NOT NULL,
        seen_at    TEXT DEFAULT (datetime('now')),
        price_eur  INTEGER,
        FOREIGN KEY(listing_id) REFERENCES listings(id)
      )
    """)
    # sinnvolle Indizes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_price   ON listings(price_eur)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_km      ON listings(km)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_postal  ON listings(postal_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_city    ON listings(city)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_listings_title   ON listings(title)")
        # --- MIGRATION: neue Spalten für Detaildaten (Beschreibung + Bilder) ---
    # idempotent: wenn Spalte existiert -> try/except verhindert Crash
    try:
        cur.execute("ALTER TABLE listings ADD COLUMN description TEXT")
    except Exception:
        pass

    try:
        cur.execute("ALTER TABLE listings ADD COLUMN image_urls_json TEXT")
    except Exception:
        pass

    conn.commit()
    conn.close()

def upsert_listing(row: dict) -> int:
    """
    Insert oder Update (nur wenn sich Felder wirklich ändern).
    Gibt 1 zurück, wenn Insert/Update ausgeführt wurde, sonst 0.
    Erwartete Keys: beliebige Teilmenge der Spalten; 'id' ist Pflicht.
    """
    assert "id" in row, "upsert_listing: 'id' fehlt"

    # dynamische Spaltenliste bauen
    cols = list(row.keys())
    placeholders = ",".join([":" + c for c in cols])

    # Vergleichswerte für WHERE (COALESCE auf Vergleichs-Typen)
    def diff_expr(c):
        # Strings → '' ; Integers → -1
        if c in ("price_eur", "km", "pics", "power_ps"):
            return f"COALESCE(listings.{c}, -1) <> COALESCE(excluded.{c}, -1)"
        else:
            return f"COALESCE(listings.{c}, '') <> COALESCE(excluded.{c}, '')"

    diff_clauses = " OR ".join(diff_expr(c) for c in cols if c != "id")

    # falls nur id gesetzt ist, trotzdem last_seen aktualisieren
    set_list = ", ".join([f"{c}=excluded.{c}" for c in cols if c != "id"])
    if set_list:
        set_list += ", "

    sql = f"""
      INSERT INTO listings ({",".join(cols)})
      VALUES ({placeholders})
      ON CONFLICT(id) DO UPDATE SET
        {set_list}last_seen = datetime('now')
      WHERE {diff_clauses}
    """

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, row)
    changed = cur.rowcount  # 1 = insert oder echtes update, 0 = keine Änderung

    # Preis-Historie nur loggen, wenn sich Preis geändert hat
    if "price_eur" in row and row.get("price_eur") is not None:
        cur.execute("SELECT price_eur FROM listings WHERE id = ?", (row["id"],))
        current = cur.fetchone()
        if current and current[0] == row["price_eur"] and changed:
            # schon aktueller Preis in Zeile – Historie trotzdem nur 1x pro Änderung schreiben:
            cur.execute("""
              INSERT INTO listing_prices(listing_id, price_eur)
              VALUES (?, ?)
            """, (row["id"], row["price_eur"]))

    conn.commit()
    conn.close()
    return 1 if changed else 0