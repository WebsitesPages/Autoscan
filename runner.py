import time
import subprocess

INTERVAL_SEC = 30 * 60  # 30 Minuten

while True:
    print("== Lauf startet ==")
    subprocess.run(["python3", "scrape_ebay.py"], check=False)
    print(f"== Pause {INTERVAL_SEC//60} min ==")
    time.sleep(INTERVAL_SEC)
