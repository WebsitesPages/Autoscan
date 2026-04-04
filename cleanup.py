import sqlite3
conn = sqlite3.connect('/opt/autoscan/autos.db')
cur = conn.cursor()
# Lösche Listings älter als 14 Tage (außer Favoriten)
cur.execute("""DELETE FROM listings WHERE id NOT IN (SELECT listing_id FROM favorites) 
               AND last_seen < datetime('now', '-14 days')""")
deleted = cur.rowcount
cur.execute("DELETE FROM listing_prices WHERE listing_id NOT IN (SELECT id FROM listings)")
cur.execute("DELETE FROM deal_scores WHERE listing_id NOT IN (SELECT id FROM listings)")
conn.commit()
conn.execute("VACUUM")
conn.close()
print(f"Cleanup: {deleted} alte Listings gelöscht")
