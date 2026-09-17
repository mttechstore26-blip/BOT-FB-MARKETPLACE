import subprocess
import time
from datetime import datetime

INTERVAL = 300  # 5 minuti

while True:
    print(f"\n[{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}] Controllo Marketplace...")

    try:
        subprocess.run(["python", "monitor.py"], check=False)
    except Exception as e:
        print("Errore:", e)

    print(f"Prossimo controllo tra {INTERVAL // 60} minuti.")
    time.sleep(INTERVAL)
