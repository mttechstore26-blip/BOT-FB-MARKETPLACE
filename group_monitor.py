from playwright.sync_api import sync_playwright
from datetime import datetime
from dotenv import load_dotenv
from html import escape
from urllib.parse import urljoin
import json
import os
import re
import sqlite3
import time
import unicodedata
import requests

from database import init_search_tables, get_searches, get_blacklist
from distance import distance_from_home

DATABASE = "marketplace.db"
FACEBOOK_STATE = "facebook_state.json"

load_dotenv(".env")

GROUP_ID = os.getenv("FACEBOOK_GROUP_ID", "327948723950502")
GROUP_URL = f"https://www.facebook.com/groups/{GROUP_ID}/"
CHECK_INTERVAL_SECONDS = int(os.getenv("FB_GROUP_CHECK_INTERVAL", "300"))
MAX_POSTS_PER_CYCLE = int(os.getenv("FB_GROUP_MAX_POSTS", "25"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def get_conn():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_group_tables():
    conn = get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_seen (
            group_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            post_url TEXT,
            PRIMARY KEY (group_id, post_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id TEXT NOT NULL,
            post_id TEXT NOT NULL,
            search_id INTEGER NOT NULL,
            post_url TEXT,
            post_text TEXT,
            detected_price REAL,
            created_at TEXT NOT NULL,
            UNIQUE(group_id, post_id, search_id)
        )
    """)
    conn.commit()
    conn.close()


def normalize_text(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text).strip()


def query_matches(query, text):
    query_n = normalize_text(query)
    text_n = normalize_text(text)

    aliases = {
        "ps5": ("ps5", "playstation 5", "play station 5", "play 5"),
        "playstation 5": ("ps5", "playstation 5", "play station 5", "play 5"),
        "ps portal": ("ps portal", "playstation portal", "play station portal"),
        "playstation portal": ("ps portal", "playstation portal", "play station portal"),
        "nintendo switch": ("nintendo switch", "switch oled", "switch lite", "switch"),
    }

    if query_n in aliases:
        return any(alias in text_n for alias in aliases[query_n])

    if query_n.startswith("iphone "):
        tokens = [t for t in query_n.split() if t != "apple"]
        return all(t in text_n for t in tokens)

    if query_n in text_n:
        return True

    tokens = [t for t in query_n.split() if len(t) >= 2]
    if len(tokens) == 1:
        return tokens[0] in text_n
    return sum(1 for t in tokens if t in text_n) >= 2


def blacklist_match(search_id, text):
    text_n = normalize_text(text)
    for row in get_blacklist(search_id):
        word = normalize_text(row["word"])
        if word and word in text_n:
            return row["word"]
    return None


def extract_price(text):
    patterns = [
        r"(?:€\s*|eur\s*)(\d{1,5}(?:[.,]\d{1,2})?)",
        r"(\d{1,5}(?:[.,]\d{1,2})?)\s*(?:€|eur)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text or "", re.I)
        if m:
            try:
                return float(m.group(1).replace(".", "").replace(",", "."))
            except ValueError:
                pass
    return None


def extract_listing_fields(text, link_text=""):
    source = (link_text or text or "").replace("\xa0", " ")
    compact = re.sub(r"\s+", " ", source).strip()

    price = extract_price(source)

    location = ""
    m = re.search(
        r"[·•]?\s*([A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý '\\-]+,\s*ITALIA)",
        compact,
        re.I,
    )
    if m:
        location = m.group(1).strip()

    title = ""

    if location:
        # Negli annunci Commerce il formato tipico è:
        # PREZZO · CITTÀ, ITALIA TITOLO Messaggio
        after_location = compact.split(location, 1)[1].strip(" ·-")
        after_location = re.sub(
            r"\b(?:Invia di nuovo messaggio|Invia messaggio|Messaggio|Post condiviso)\b.*$",
            "",
            after_location,
            flags=re.I,
        ).strip()
        if after_location:
            title = after_location

    if not title:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in source.splitlines()
            if line.strip()
        ]
        ignored = (
            "messaggio",
            "post condiviso",
            "invia di nuovo messaggio",
        )
        for line in reversed(lines):
            low = line.lower()
            if any(x in low for x in ignored):
                continue
            if "€" in line or "italia" in low:
                continue
            if line:
                title = line
                break

    if not title:
        title = "Annuncio Facebook"

    return title[:180], location, price


def price_matches(search, price):
    if price is None:
        # Se il post non contiene un prezzo, non lo escludiamo:
        # può comunque essere un annuncio interessante.
        return True
    if search["min_price"] is not None and price < float(search["min_price"]):
        return False
    if search["max_price"] is not None and price > float(search["max_price"]):
        return False
    return True


def canonical_post_url(href):
    if not href:
        return ""
    url = urljoin("https://www.facebook.com", href)

    # Per gli annunci condivisi nei gruppi Facebook l'URL più stabile
    # è spesso /commerce/listing/<id>/ invece del permalink del post.
    m = re.search(r"/commerce/listing/(\d+)", url)
    if m:
        return f"https://www.facebook.com/commerce/listing/{m.group(1)}/"

    url = url.split("?")[0]
    return url.rstrip("/")


def extract_post_id(url):
    patterns = [
        r"/commerce/listing/(\d+)",
        rf"/groups/{re.escape(GROUP_ID)}/posts/(\d+)",
        r"/permalink/(\d+)",
        r"/posts/(\d+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url or "")
        if m:
            return m.group(1)
    return None


def is_seen(post_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM group_seen WHERE group_id=? AND post_id=?",
        (GROUP_ID, post_id),
    ).fetchone()
    conn.close()
    return bool(row)


def mark_seen(post_id, url):
    conn = get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO group_seen(group_id, post_id, first_seen, post_url)
        VALUES(?,?,?,?)
        """,
        (GROUP_ID, post_id, datetime.now().isoformat(), url),
    )
    conn.commit()
    conn.close()


def save_notification(post_id, search_id, url, text, price):
    conn = get_conn()
    conn.execute(
        """
        INSERT OR IGNORE INTO group_notifications(
            group_id, post_id, search_id, post_url, post_text, detected_price, created_at
        ) VALUES(?,?,?,?,?,?,?)
        """,
        (
            GROUP_ID, post_id, search_id, url, text,
            price, datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def send_telegram(searches, post_id, url, text, price, title, location, photo_url=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID mancanti")
        return False

    price_line = f"{price:g} €" if price is not None else "non rilevato"
    location_line = location or "Località non rilevata"

    distance_km = distance_from_home(location) if location else None

    if distance_km is None:
        distance_line = "🚗 Distanza da Palmi: non disponibile"
    elif distance_km <= 20:
        distance_line = f"🔥 <b>{distance_km} km da Palmi</b>"
    else:
        distance_line = f"🚗 Circa <b>{distance_km} km da Palmi</b>"

    match_names = []
    for search in searches:
        name = search["query"]
        if name not in match_names:
            match_names.append(name)

    match_line = " / ".join(match_names)

    safe_url = escape(url, quote=True)
    safe_title = escape(title or "Annuncio Facebook")

    message = (
        "🔵 <b>GRUPPO FACEBOOK</b>\n\n"
        f'🔗 <b><a href="{safe_url}">{safe_title}</a></b>\n\n'
        f"💰 Prezzo: <b>{escape(price_line)}</b>\n"
        f"📍 {escape(location_line)}\n"
        f"{distance_line}\n"
        f"🔎 Match: <b>{escape(match_line)}</b>"
    )

    endpoint = "sendPhoto" if photo_url else "sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "parse_mode": "HTML",
    }

    if photo_url:
        payload["photo"] = photo_url
        payload["caption"] = message[:1024]
    else:
        payload["text"] = message
        payload["disable_web_page_preview"] = False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{endpoint}",
            data=payload,
            timeout=30,
        )
        if response.ok and response.json().get("ok"):
            print(f"📨 Telegram: {match_line} | post {post_id}")
            return True
        print("❌ Telegram:", response.text)
    except Exception as exc:
        print("❌ Telegram:", exc)

    return False


def validate_session(page):
    current = page.url.lower()
    if "/login" in current or "/checkpoint" in current or "login.php" in current:
        return False

    try:
        body = page.locator("body").inner_text(timeout=5000).lower()
    except Exception:
        body = ""

    return not any(x in body for x in (
        "accedi a facebook",
        "email o numero di telefono",
        "conferma la tua identità",
    ))


def set_newest_sort(page):
    """
    Imposta il feed del gruppo su 'Nuovi annunci' prima della lettura.
    Facebook apre spesso i gruppi su 'Annunci più popolari'.
    """
    try:
        current = page.get_by_text("Annunci più popolari", exact=True)
        if current.count() == 0:
            print("⚠️ Selettore ordinamento non trovato")
            return False

        current.first.click(timeout=5000)
        page.wait_for_timeout(800)

        newest = page.get_by_text("Nuovi annunci", exact=True)
        if newest.count() == 0:
            print("⚠️ Voce 'Nuovi annunci' non trovata")
            return False

        newest.first.click(timeout=5000)
        page.wait_for_timeout(2500)

        print("🆕 Ordinamento: Nuovi annunci")
        return True

    except Exception as exc:
        print("⚠️ Impossibile impostare 'Nuovi annunci':", exc)
        return False


def collect_posts(page):
    """
    Raccoglie gli annunci condivisi nel gruppo.

    Facebook, per i post di compravendita, spesso non espone un permalink
    /groups/.../posts/... nel DOM. Espone invece uno o più link
    /commerce/listing/<id>/ all'interno del contenitore del post.
    """

    posts = []
    seen_ids = set()

    # Partiamo direttamente dai link Commerce: sono l'elemento più stabile
    # osservato nel DOM del gruppo.
    commerce_links = page.locator('a[href*="/commerce/listing/"]')

    for i in range(min(commerce_links.count(), MAX_POSTS_PER_CYCLE * 10)):
        link_el = commerce_links.nth(i)

        try:
            href = link_el.get_attribute("href") or ""
        except Exception:
            continue

        url = canonical_post_url(href)
        post_id = extract_post_id(url)

        if not post_id or post_id in seen_ids:
            continue

        # Risaliamo fino al contenitore che include testo annuncio, prezzo
        # e intestazione del post. Il DOM Facebook è molto annidato.
        try:
            data = link_el.evaluate("""
            (node) => {
                let current = node;
                let best = null;

                for (let level = 0; level < 30 && current; level++, current = current.parentElement) {
                    const text = (current.innerText || "").trim();
                    if (!text) continue;

                    const hasPrice = /(?:€|EUR)/i.test(text);
                    const hasCommerce = !!current.querySelector('a[href*="/commerce/listing/"]');

                    if (hasCommerce && hasPrice) {
                        best = current;

                        // Fermati quando il blocco sembra contenere il post completo
                        // ma non l'intero feed.
                        const role = current.getAttribute("role");
                        if (role === "article" || text.length > 80) break;
                    }
                }

                if (!best) return null;

                const text = (best.innerText || "").trim();

                let photo = "";
                for (const img of best.querySelectorAll("img")) {
                    const src = img.src || "";
                    if (src.includes("scontent") || src.includes("fbcdn")) {
                        photo = src;
                        break;
                    }
                }

                return {
                    text,
                    photo,
                    linkText: (node.innerText || "").trim()
                };
            }
            """)
        except Exception:
            data = None

        if not data or not data.get("text"):
            continue

        text = data["text"].strip()

        # Evita di prendere blocchi giganteschi (es. intero feed/sidebar).
        if len(text) > 8000:
            continue

        seen_ids.add(post_id)
        posts.append({
            "post_id": post_id,
            "url": url,
            "text": text,
            "link_text": data.get("linkText") or "",
            "photo_url": data.get("photo") or "",
        })

        if len(posts) >= MAX_POSTS_PER_CYCLE:
            break

    return posts


def run_cycle(browser):
    if not os.path.exists(FACEBOOK_STATE):
        print(f"❌ Manca {FACEBOOK_STATE}: serve una sessione Facebook valida")
        return

    context = browser.new_context(
        storage_state=FACEBOOK_STATE,
        locale="it-IT",
        viewport={"width": 1365, "height": 900},
    )
    page = context.new_page()

    try:
        print(f"🔵 Apro gruppo {GROUP_ID}")
        page.goto(GROUP_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(6)

        if not validate_session(page):
            print("❌ Sessione Facebook non valida")
            return

        set_newest_sort(page)

        # Piccolo scroll per permettere al feed di caricare alcuni post.
        for _ in range(2):
            page.mouse.wheel(0, 1200)
            time.sleep(2)

        posts = collect_posts(page)
        searches = get_searches(active_only=True)

        print(f"📄 Post letti: {len(posts)} | ricerche attive: {len(searches)}")

        new_count = 0
        notify_count = 0

        for post in posts:
            post_id = post["post_id"]

            if is_seen(post_id):
                continue

            new_count += 1
            text = post["text"]
            title, location, price = extract_listing_fields(
                text,
                post.get("link_text", ""),
            )

            matches = []
            for search in searches:
                if not query_matches(search["query"], text):
                    continue
                blocked = blacklist_match(search["id"], text)
                if blocked:
                    print(f"🚫 {post_id}: blacklist '{blocked}'")
                    continue
                if not price_matches(search, price):
                    continue
                matches.append(search)

            # Segniamo visto dopo aver valutato tutte le ricerche.
            mark_seen(post_id, post["url"])

            if matches:
                if send_telegram(
                    matches,
                    post_id,
                    post["url"],
                    text,
                    price,
                    title,
                    location,
                    post["photo_url"],
                ):
                    notify_count += 1

                    for search in matches:
                        save_notification(
                            post_id,
                            search["id"],
                            post["url"],
                            text,
                            price,
                        )

        print(f"✅ Ciclo: nuovi={new_count} notifiche={notify_count}")

    except Exception as exc:
        print("❌ Errore ciclo gruppo:", exc)

    finally:
        try:
            page.close()
        except Exception:
            pass
        context.close()


def main():
    init_search_tables()
    init_group_tables()

    print(f"🚀 Monitor gruppo Facebook {GROUP_ID}")
    print(f"⏱️ Intervallo: {CHECK_INTERVAL_SECONDS}s")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        while True:
            started = time.time()
            run_cycle(browser)
            elapsed = time.time() - started
            sleep_for = max(5, CHECK_INTERVAL_SECONDS - int(elapsed))
            print(f"😴 Prossimo controllo tra {sleep_for}s")
            time.sleep(sleep_for)


if __name__ == "__main__":
    main()
