from playwright.sync_api import sync_playwright
from urllib.parse import urlencode
from datetime import datetime, timedelta
from dotenv import load_dotenv
from html import escape
import sqlite3
import time
import os
import re
import unicodedata
import requests
from distance import distance_from_home

from database import (
    init_search_tables,
    get_searches,
    get_blacklist,
)

DATABASE = "marketplace.db"
FACEBOOK_STATE = "facebook_state.json"

UPDATE_TELEGRAM_FOR_HOURS = 24
MAX_RESULTS_PER_SEARCH = 30

load_dotenv(".env")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# =========================================================
# DATABASE
# =========================================================

def get_conn():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_monitor_tables():
    conn = get_conn()

    # Tabella per sapere quali annunci sono già stati visti
    # PER SINGOLA RICERCA.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS search_seen (
            search_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            PRIMARY KEY (search_id, item_id)
        )
    """)

    # Notifiche Telegram inviate.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            item_id TEXT NOT NULL,
            title TEXT,
            price TEXT,
            location TEXT,
            url TEXT,
            photo_url TEXT,
            description TEXT,
            published_at TEXT,
            telegram_message_id INTEGER,
            telegram_message_type TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(search_id, item_id)
        )
    """)

    conn.commit()
    conn.close()


def bootstrap_existing_nintendo():
    """
    Evita che la ricerca Nintendo Switch già esistente
    notifichi nuovamente tutti gli annunci della vecchia baseline.
    """

    conn = get_conn()

    seen_count = conn.execute(
        "SELECT COUNT(*) FROM search_seen"
    ).fetchone()[0]

    if seen_count > 0:
        conn.close()
        return

    searches = get_searches()

    if len(searches) != 1:
        conn.close()
        return

    search_id = searches[0]["id"]

    try:
        old_rows = conn.execute("""
            SELECT item_id, first_seen
            FROM listings
        """).fetchall()
    except sqlite3.OperationalError:
        old_rows = []

    for row in old_rows:
        conn.execute("""
            INSERT OR IGNORE INTO search_seen (
                search_id,
                item_id,
                first_seen
            )
            VALUES (?, ?, ?)
        """, (
            search_id,
            row["item_id"],
            row["first_seen"] or datetime.now().isoformat(),
        ))

    conn.commit()
    conn.close()

    if old_rows:
        print(
            f"✅ Baseline precedente importata: "
            f"{len(old_rows)} annunci"
        )


# =========================================================
# TESTO / BLACKLIST
# =========================================================

def normalize_text(text):
    if not text:
        return ""

    text = text.lower()

    text = unicodedata.normalize("NFKD", text)

    text = "".join(
        char for char in text
        if not unicodedata.combining(char)
    )

    text = re.sub(r"\s+", " ", text)

    return text.strip()


def blacklist_match(title, description, blacklist):
    combined = normalize_text(
        f"{title or ''} {description or ''}"
    )

    for row in blacklist:
        word = normalize_text(row["word"])

        if word and word in combined:
            return row["word"]

    return None


SUGGESTION_MARKERS = {
    "more options",
    "altre opzioni",
    "more results",
    "altri risultati",
}


def is_suggestion_card(text):
    """Scarta le card che Facebook aggiunge come suggerimenti fuori ricerca."""
    lines = [
        normalize_text(line)
        for line in (text or "").splitlines()
        if line.strip()
    ]

    return any(
        line in SUGGESTION_MARKERS
        for line in lines
    )


def query_matches_listing(query, title, description=""):
    """
    Controllo prudente di pertinenza.
    Evita risultati totalmente estranei senza bloccare annunci validi
    come "PS5 con controller".
    """
    query_n = normalize_text(query)
    combined = normalize_text(
        f"{title or ''} {description or ''}"
    )

    if not query_n or not combined:
        return True

    aliases = {
        "ps portal": (
            "ps portal",
            "playstation portal",
            "play station portal",
        ),
        "playstation portal": (
            "ps portal",
            "playstation portal",
            "play station portal",
        ),
        "ps5": (
            "ps5",
            "playstation 5",
            "play station 5",
            "play 5",
        ),
        "playstation 5": (
            "ps5",
            "playstation 5",
            "play station 5",
            "play 5",
        ),
        "nintendo switch": (
            "nintendo switch",
            "switch oled",
            "switch lite",
            "switch",
        ),
    }

    if query_n in aliases:
        return any(
            alias in combined
            for alias in aliases[query_n]
        )

    # Per iPhone richiediamo modello e varianti indicate nella query.
    if query_n.startswith("iphone "):
        required = [
            token
            for token in query_n.split()
            if token not in {"apple"}
        ]
        return all(token in combined for token in required)

    # Match esatto/frase: prima scelta per le altre ricerche.
    if query_n in combined:
        return True

    # Fallback generico: per query composte servono almeno due termini
    # significativi; per una query di una parola deve comparire quella parola.
    tokens = [
        token
        for token in query_n.split()
        if len(token) >= 2
    ]

    if not tokens:
        return True

    hits = sum(
        1 for token in tokens
        if token in combined
    )

    if len(tokens) == 1:
        return hits == 1

    return hits >= 2


# =========================================================
# URL FACEBOOK
# =========================================================

def build_search_url(search):
    params = {
        "query": search["query"],
        "sortBy": "creation_time_descend",
    }

    if search["min_price"] is not None:
        params["minPrice"] = int(search["min_price"])

    if search["max_price"] is not None:
        params["maxPrice"] = int(search["max_price"])

    return (
        "https://www.facebook.com/marketplace/search/?"
        + urlencode(params)
    )



def get_app_state(key, default=None):
    conn = get_conn()

    row = conn.execute(
        "SELECT value FROM app_state WHERE key = ?",
        (key,)
    ).fetchone()

    conn.close()

    if not row:
        return default

    return row["value"]


def set_app_state(key, value):
    conn = get_conn()

    conn.execute("""
        INSERT INTO app_state (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """, (
        key,
        str(value),
    ))

    conn.commit()
    conn.close()


# =========================================================
# CONTROLLO SESSIONE FACEBOOK
# =========================================================

def send_session_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=20,
        )
    except Exception as exc:
        print("⚠️ Errore invio alert sessione:", exc)


def save_facebook_state(context):
    """Salva cookie e storage aggiornati durante la navigazione."""
    try:
        context.storage_state(path=FACEBOOK_STATE)
        print("💾 Sessione Facebook aggiornata e salvata")
        return True
    except Exception as exc:
        print("⚠️ Impossibile salvare la sessione Facebook:", exc)
        return False


def check_facebook_session(context):
    page = context.new_page()

    try:
        page.goto(
            "https://www.facebook.com/marketplace/",
            wait_until="domcontentloaded",
            timeout=60000,
        )

        time.sleep(3)

        current_url = page.url.lower()

        if (
            "/login" in current_url
            or "/checkpoint" in current_url
            or "login.php" in current_url
        ):
            return False, f"Redirect Facebook: {page.url}"

        login_fields = page.locator(
            'input[name="email"], input[name="pass"]'
        )

        if login_fields.count() > 0:
            return False, "Pagina di login rilevata"

        marketplace_links = page.locator(
            'a[href*="/marketplace/"]'
        )

        if marketplace_links.count() == 0:
            body_text = ""

            try:
                body_text = page.locator("body").inner_text().lower()
            except Exception:
                pass

            suspicious = [
                "accedi a facebook",
                "password",
                "email o numero di telefono",
                "conferma la tua identità",
            ]

            if any(word in body_text for word in suspicious):
                return False, "Contenuto login/verifica rilevato"

        return True, "Sessione valida"

    except Exception as exc:
        return False, f"Errore controllo sessione: {exc}"

    finally:
        page.close()


    if search["min_price"] is not None:
        params["minPrice"] = int(search["min_price"])

    if search["max_price"] is not None:
        params["maxPrice"] = int(search["max_price"])

    return (
        "https://www.facebook.com/marketplace/search/?"
        + urlencode(params)
    )


# =========================================================
# PUBBLICAZIONE
# =========================================================

def parse_relative_time(text):
    if not text:
        return None

    now = datetime.now()
    value = text.lower().strip()

    if "appena pubblicato" in value:
        return now

    match = re.search(r"(\d+)\s*minut", value)

    if match:
        return now - timedelta(
            minutes=int(match.group(1))
        )

    match = re.search(r"(\d+)\s*or", value)

    if match:
        return now - timedelta(
            hours=int(match.group(1))
        )

    match = re.search(r"(\d+)\s*giorn", value)

    if match:
        return now - timedelta(
            days=int(match.group(1))
        )

    return None


def human_age(published_at):
    if not published_at:
        return "orario non disponibile"

    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(
                published_at
            )
        except Exception:
            return "orario non disponibile"

    delta = datetime.now() - published_at

    minutes = max(
        0,
        int(delta.total_seconds() // 60)
    )

    if minutes < 1:
        return "appena pubblicato"

    if minutes < 60:
        return f"{minutes} min fa"

    hours = minutes // 60
    remaining_minutes = minutes % 60

    if hours < 24:
        if remaining_minutes:
            return (
                f"{hours}h "
                f"{remaining_minutes}m fa"
            )

        return f"{hours}h fa"

    days = hours // 24
    return f"{days} giorni fa"


# =========================================================
# PARSER CARD
# =========================================================

def parse_listing_card(text):
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return "", "", "", ""

    prices = []
    relative_time = ""

    for line in lines:
        if "€" in line:
            prices.append(line)

        lower = line.lower()

        if (
            "appena pubblicato" in lower
            or "minuto fa" in lower
            or "minuti fa" in lower
            or "ora fa" in lower
            or "ore fa" in lower
            or "giorno fa" in lower
            or "giorni fa" in lower
        ):
            relative_time = line

    price = prices[0] if prices else ""

    location = lines[-1]

    ignored = set(prices)

    if relative_time:
        ignored.add(relative_time)

    title_parts = []

    for line in lines:
        if line == location:
            continue

        if line in ignored:
            continue

        title_parts.append(line)

    title = " ".join(title_parts).strip()

    return (
        price,
        title,
        location,
        relative_time,
    )


# =========================================================
# DETTAGLIO ANNUNCIO
# =========================================================

def get_item_details(context, url, fallback_photo=""):
    """
    Apre soltanto gli annunci NUOVI per recuperare
    descrizione e foto principale.
    """

    page = context.new_page()

    description = ""
    photo_url = fallback_photo

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        time.sleep(2)

        # Foto principale: prima proviamo OpenGraph.
        try:
            og_image = page.locator(
                'meta[property="og:image"]'
            )

            if og_image.count():
                candidate = (
                    og_image.first.get_attribute("content")
                    or ""
                )

                if candidate:
                    photo_url = candidate
        except Exception:
            pass

        # Descrizione OpenGraph.
        try:
            og_description = page.locator(
                'meta[property="og:description"]'
            )

            if og_description.count():
                description = (
                    og_description.first.get_attribute(
                        "content"
                    )
                    or ""
                ).strip()
        except Exception:
            pass

        # Tentativo più specifico sulla sezione "Descrizione".
        try:
            labels = page.get_by_text(
                "Descrizione",
                exact=True,
            )

            if labels.count():
                block = labels.first.locator(
                    "xpath=.."
                ).inner_text()

                block = block.strip()

                if len(block) > len(description):
                    description = block
        except Exception:
            pass

    except Exception as exc:
        print(
            f"⚠️ Impossibile aprire dettaglio {url}: "
            f"{exc}"
        )

    finally:
        page.close()

    return description, photo_url


# =========================================================
# TELEGRAM
# =========================================================

def build_notification_text(data):
    title = escape(
        data.get("title")
        or "Annuncio Marketplace"
    )

    url = escape(data.get("url") or "")

    price = escape(
        data.get("price")
        or "Prezzo non disponibile"
    )

    raw_location = (
        data.get("location")
        or ""
    )

    location = escape(
        raw_location
        or "Località non disponibile"
    )

    distance_km = (
        distance_from_home(raw_location)
        if raw_location
        else None
    )

    if distance_km is None:
        distance_line = ""

    elif distance_km <= 20:
        distance_line = (
            f"🔥🔥 <b>VICINISSIMO — "
            f"{distance_km} km da Palmi</b>\n"
        )

    else:
        distance_line = (
            f"🚗 Circa {distance_km} km da Palmi\n"
        )

    query_name = escape(
        data.get("query_name")
        or ""
    )

    published_at = data.get("published_at")

    if isinstance(published_at, str):
        try:
            published_at = datetime.fromisoformat(
                published_at
            )
        except Exception:
            published_at = None

    if published_at:
        date_text = published_at.strftime(
            "%d/%m/%Y %H:%M"
        )

        age = human_age(published_at)

        publication = (
            f"🕐 <b>{date_text}</b> · "
            f"{escape(age)}"
        )
    else:
        publication = (
            "🕐 Orario pubblicazione non disponibile"
        )

    return (
        f'🔗 <b><a href="{url}">{title}</a></b>\n\n'
        f"💰 <b>{price}</b>\n"
        f"📍 {location}\n"
        f"{distance_line}"
        f"{publication}\n"
        f"🔎 Query: <b>{query_name}</b>"
    )


def send_notification(data):
    text = build_notification_text(data)

    reply_markup = json.dumps({
        "inline_keyboard": [
            [
                {
                    "text": "✉️ Invia DM",
                    "callback_data": (
                        f"dm:{data['search_id']}:{data['item_id']}"
                    )
                }
            ]
        ]
    })

    photo_url = data.get("photo_url")

    if photo_url:
        try:
            response = requests.post(
                f"https://api.telegram.org/"
                f"bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                data={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "photo": photo_url,
                    "caption": text,
                    "parse_mode": "HTML",
                    "reply_markup": reply_markup,
                },
                timeout=30,
            )

            result = response.json()

            if response.ok and result.get("ok"):
                return (
                    result["result"]["message_id"],
                    "photo",
                )

        except Exception as exc:
            print(
                "⚠️ Foto Telegram non inviata:",
                exc,
            )

    response = requests.post(
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
            "reply_markup": reply_markup,
        },
        timeout=20,
    )

    result = response.json()

    if response.ok and result.get("ok"):
        return (
            result["result"]["message_id"],
            "text",
        )

    print(
        "❌ Errore Telegram:",
        response.text,
    )

    return None, None


def update_recent_messages():
    conn = get_conn()

    threshold = (
        datetime.now()
        - timedelta(hours=UPDATE_TELEGRAM_FOR_HOURS)
    ).isoformat()

    rows = conn.execute("""
        SELECT *
        FROM notifications
        WHERE telegram_message_id IS NOT NULL
          AND published_at IS NOT NULL
          AND published_at >= ?
    """, (threshold,)).fetchall()

    for row in rows:
        data = dict(row)

        text = build_notification_text(data)

        if row["telegram_message_type"] == "photo":
            endpoint = "editMessageCaption"

            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": row[
                    "telegram_message_id"
                ],
                "caption": text,
                "parse_mode": "HTML",
            }

        else:
            endpoint = "editMessageText"

            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "message_id": row[
                    "telegram_message_id"
                ],
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }

        try:
            response = requests.post(
                f"https://api.telegram.org/"
                f"bot{TELEGRAM_BOT_TOKEN}/{endpoint}",
                data=payload,
                timeout=20,
            )

            if not response.ok:
                result = response.json()

                description = result.get(
                    "description",
                    ""
                ).lower()

                if (
                    "message is not modified"
                    not in description
                ):
                    print(
                        "⚠️ Errore aggiornamento:",
                        result,
                    )

        except Exception as exc:
            print(
                "⚠️ Errore aggiornamento Telegram:",
                exc,
            )

    conn.close()


# =========================================================
# SINGOLA RICERCA
# =========================================================

def process_search(context, search, cycle_notified=None):
    if cycle_notified is None:
        cycle_notified = set()
    search_id = search["id"]
    query_name = search["query"]

    blacklist = get_blacklist(search_id)

    url = build_search_url(search)

    print()
    print("=" * 60)
    print(f"🔎 Ricerca: {query_name}")
    print(
        f"💰 {search['min_price']} - "
        f"{search['max_price']}"
    )

    page = context.new_page()

    try:
        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        time.sleep(5)

        links = page.locator(
            'a[href*="/marketplace/item/"]'
        )

        unique = set()
        candidates = []

        max_items = min(
            MAX_RESULTS_PER_SEARCH,
            links.count(),
        )

        for i in range(max_items):
            el = links.nth(i)

            try:
                href = el.get_attribute("href")
                text = el.inner_text().strip()
            except Exception:
                continue

            if not href:
                continue

            if "/marketplace/item/" not in href:
                continue

            # Facebook inserisce nella stessa pagina sezioni tipo
            # "More Options"/"Altre opzioni" con annunci non pertinenti.
            if is_suggestion_card(text):
                print("↪️ Suggerimento Facebook ignorato")
                continue

            try:
                item_id = (
                    href
                    .split("/marketplace/item/")[1]
                    .split("/")[0]
                    .split("?")[0]
                )
            except Exception:
                continue

            if not item_id:
                continue

            if item_id in unique:
                continue

            unique.add(item_id)

            (
                price,
                title,
                location,
                relative_time,
            ) = parse_listing_card(text)

            full_url = (
                "https://www.facebook.com/"
                f"marketplace/item/{item_id}/"
            )

            photo_url = ""

            try:
                image = el.locator("img").first

                if image.count():
                    photo_url = (
                        image.get_attribute("src")
                        or ""
                    )
            except Exception:
                pass

            candidates.append({
                "item_id": item_id,
                "title": title,
                "price": price,
                "location": location,
                "relative_time": relative_time,
                "url": full_url,
                "photo_url": photo_url,
            })

    finally:
        page.close()

    print(
        f"Annunci trovati: {len(candidates)}"
    )

    conn = get_conn()

    # -------------------------------------------------
    # BASELINE SILENZIOSA
    # -------------------------------------------------
    if not bool(search["baseline_done"]):
        now = datetime.now().isoformat()

        for item in candidates:
            conn.execute("""
                INSERT OR IGNORE INTO search_seen (
                    search_id,
                    item_id,
                    first_seen
                )
                VALUES (?, ?, ?)
            """, (
                search_id,
                item["item_id"],
                now,
            ))

        conn.execute("""
            UPDATE searches
            SET baseline_done = 1
            WHERE id = ?
        """, (search_id,))

        conn.commit()

        print(
            f"✅ BASELINE CREATA - "
            f"{len(candidates)} annunci salvati - "
            f"nessuna notifica"
        )

        conn.close()
        return

    new_items = []

    for item in candidates:
        already_seen = conn.execute("""
            SELECT 1
            FROM search_seen
            WHERE search_id = ?
              AND item_id = ?
        """, (
            search_id,
            item["item_id"],
        )).fetchone()

        if already_seen:
            continue

        # Lo marchiamo visto immediatamente.
        conn.execute("""
            INSERT OR IGNORE INTO search_seen (
                search_id,
                item_id,
                first_seen
            )
            VALUES (?, ?, ?)
        """, (
            search_id,
            item["item_id"],
            datetime.now().isoformat(),
        ))

        conn.commit()

        new_items.append(item)

    print(
        f"Nuovi annunci: {len(new_items)}"
    )

    for item in new_items:
        print()
        print(
            f"🆕 Controllo: {item['title']}"
        )

        description, photo_url = (
            get_item_details(
                context,
                item["url"],
                item["photo_url"],
            )
        )

        blocked_word = blacklist_match(
            item["title"],
            description,
            blacklist,
        )

        if blocked_word:
            print(
                f"🚫 Scartato dalla blacklist: "
                f"{blocked_word}"
            )
            continue

        if not query_matches_listing(
            query_name,
            item["title"],
            description,
        ):
            print(
                f"🚫 Scartato perché non pertinente "
                f"alla query: {query_name}"
            )
            continue

        if item["item_id"] in cycle_notified:
            print(
                "↪️ Annuncio già notificato da "
                "un'altra ricerca in questo ciclo"
            )
            continue

        published_at = parse_relative_time(
            item["relative_time"]
        )

        data = {
            "search_id": search_id,
            "item_id": item["item_id"],
            "title": item["title"],
            "price": item["price"],
            "location": item["location"],
            "url": item["url"],
            "photo_url": photo_url,
            "description": description,
            "published_at": published_at,
            "query_name": query_name,
        }

        message_id, message_type = (
            send_notification(data)
        )

        if message_id:
            conn.execute("""
                INSERT OR REPLACE INTO notifications (
                    search_id,
                    item_id,
                    title,
                    price,
                    location,
                    url,
                    photo_url,
                    description,
                    published_at,
                    telegram_message_id,
                    telegram_message_type,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                search_id,
                item["item_id"],
                item["title"],
                item["price"],
                item["location"],
                item["url"],
                photo_url,
                description,
                (
                    published_at.isoformat()
                    if published_at
                    else None
                ),
                message_id,
                message_type,
                datetime.now().isoformat(),
            ))

            conn.commit()

            cycle_notified.add(item["item_id"])

            print(
                "✅ Notifica Telegram inviata"
            )

    conn.close()


# =========================================================
# MAIN
# =========================================================

def main():
    init_search_tables()
    init_monitor_tables()
    bootstrap_existing_nintendo()

    searches = get_searches(
        active_only=True
    )

    print(
        f"Ricerche attive: {len(searches)}"
    )

    if not searches:
        print(
            "Nessuna ricerca attiva."
        )
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True
        )

        context = browser.new_context(
            storage_state=FACEBOOK_STATE,
            viewport={
                "width": 1400,
                "height": 900,
            },
        )

        session_ok, session_message = check_facebook_session(context)

        previous_status = get_app_state(
            "facebook_session_status",
            "unknown"
        )

        if not session_ok:
            print("❌ Sessione Facebook non valida:", session_message)

            if previous_status != "invalid":
                send_session_alert(
                    "⚠️ SESSIONE FACEBOOK PERSA\n\n"
                    "La sessione Marketplace non è più valida.\n"
                    f"Dettaglio: {session_message}\n\n"
                    "Il monitor non eseguirà le ricerche finché "
                    "la sessione non verrà ripristinata."
                )

            set_app_state(
                "facebook_session_status",
                "invalid"
            )

            browser.close()
            return

        print("✅ Sessione Facebook valida")

        # Facebook può rinnovare cookie/token durante il controllo.
        # Salviamo subito lo stato aggiornato per non ripartire
        # al prossimo ciclo con cookie ormai vecchi.
        save_facebook_state(context)

        if previous_status == "invalid":
            send_session_alert(
                "✅ SESSIONE FACEBOOK RIPRISTINATA\n\n"
                "Marketplace è nuovamente accessibile.\n"
                "Il monitor ha ripreso normalmente le ricerche."
            )

        set_app_state(
            "facebook_session_status",
            "valid"
        )

        cycle_notified = set()

        for search in searches:
            try:
                process_search(
                    context,
                    search,
                    cycle_notified,
                )
            except Exception as exc:
                print(
                    f"❌ Errore ricerca "
                    f"{search['query']}: {exc}"
                )

        # Durante le ricerche Facebook può aver ruotato altri cookie.
        # Persistiamo nuovamente lo stato prima di chiudere Chromium.
        save_facebook_state(context)

        browser.close()

    update_recent_messages()


if __name__ == "__main__":
    main()
