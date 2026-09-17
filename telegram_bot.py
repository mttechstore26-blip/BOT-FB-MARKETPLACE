import os
import asyncio
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from facebook_dm import send_facebook_dm

from database import (
    init_search_tables,
    add_search,
    get_searches,
    get_search,
    get_blacklist,
    add_blacklist_word,
    delete_blacklist_word,
    set_search_active,
    delete_search,
    get_connection,
    set_dm_message,
    get_notification,
    mark_dm_sent,
)

load_dotenv(".env")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))


# =========================================================
# SICUREZZA
# =========================================================

def authorized(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.id == AUTHORIZED_CHAT_ID)


# =========================================================
# MENU
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "➕ Aggiungi ricerca",
                callback_data="add_search"
            )
        ],
        [
            InlineKeyboardButton(
                "✏️ Modifica ricerca",
                callback_data="edit_searches"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 Le mie ricerche",
                callback_data="list_searches"
            )
        ],
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "📊 MARKETPLACE MONITOR\n\n"
        "Gestisci le ricerche Facebook Marketplace:",
        reply_markup=main_keyboard(),
    )


async def show_main(query):
    await query.edit_message_text(
        "📊 MARKETPLACE MONITOR\n\n"
        "Gestisci le ricerche Facebook Marketplace:",
        reply_markup=main_keyboard(),
    )


# =========================================================
# FORMATTAZIONE
# =========================================================

def format_price(value):
    if value is None:
        return "nessun limite"

    if float(value).is_integer():
        return f"{int(value)} €"

    return f"{value:.2f} €"


def parse_price(text):
    text = text.strip().lower().replace("€", "").replace(",", ".")

    if text in (
        "no",
        "nessuno",
        "nessuna",
        "skip",
        "-",
        "0",
    ):
        return None

    value = float(text)

    if value < 0:
        raise ValueError

    return value


def search_text(search):
    blacklist = get_blacklist(search["id"])

    if blacklist:
        bl_text = ", ".join(row["word"] for row in blacklist)
    else:
        bl_text = "nessuna"

    status = "✅ Attiva" if search["active"] else "⏸ Disattivata"

    return (
        f"🔎 {search['query']}\n\n"
        f"💰 Min: {format_price(search['min_price'])}\n"
        f"💰 Max: {format_price(search['max_price'])}\n"
        f"🚫 Blacklist: {bl_text}\n"
        f"📡 Stato: {status}"
    )


# =========================================================
# AGGIUNGI RICERCA
# =========================================================

async def begin_add_search(query, context):
    context.user_data.clear()
    context.user_data["pending_action"] = "add_query"
    context.user_data["new_search"] = {}

    await query.edit_message_text(
        "➕ NUOVA RICERCA\n\n"
        "🔎 Cosa vuoi cercare?\n\n"
        "Esempio:\n"
        "PS5"
    )


async def handle_add_query(update, context, text):
    text = text.strip()

    if not text:
        await update.message.reply_text("Inserisci una ricerca valida.")
        return

    context.user_data["new_search"]["query"] = text
    context.user_data["pending_action"] = "add_min"

    await update.message.reply_text(
        f"🔎 Ricerca: {text}\n\n"
        "💶 Prezzo minimo?\n\n"
        "Esempio: 200\n"
        "Scrivi 0 se non vuoi un minimo."
    )


async def handle_add_min(update, context, text):
    try:
        value = parse_price(text)
    except Exception:
        await update.message.reply_text(
            "❌ Prezzo non valido.\n"
            "Scrivi ad esempio 200 oppure 0."
        )
        return

    context.user_data["new_search"]["min_price"] = value
    context.user_data["pending_action"] = "add_max"

    await update.message.reply_text(
        "💶 Prezzo massimo?\n\n"
        "Esempio: 400\n"
        "Scrivi 0 se non vuoi un massimo."
    )


async def handle_add_max(update, context, text):
    try:
        value = parse_price(text)
    except Exception:
        await update.message.reply_text(
            "❌ Prezzo non valido.\n"
            "Scrivi ad esempio 400 oppure 0."
        )
        return

    new_search = context.user_data["new_search"]

    min_price = new_search.get("min_price")

    if (
        min_price is not None
        and value is not None
        and value < min_price
    ):
        await update.message.reply_text(
            "❌ Il prezzo massimo non può essere "
            "inferiore al prezzo minimo."
        )
        return

    new_search["max_price"] = value
    context.user_data["pending_action"] = "add_blacklist"

    await update.message.reply_text(
        "🚫 BLACKLIST\n\n"
        "Scrivi le parole da escludere separate da virgola.\n\n"
        "La blacklist verrà controllata sia nel TITOLO "
        "sia nella DESCRIZIONE.\n\n"
        "Esempio:\n"
        "rotto, ricambi, controller, giochi\n\n"
        "Scrivi NO se non vuoi blacklist."
    )


async def handle_add_blacklist(update, context, text):
    new_search = context.user_data["new_search"]

    words = []

    if text.strip().lower() not in (
        "no",
        "nessuna",
        "nessuno",
        "0",
        "-",
    ):
        words = [
            word.strip()
            for word in text.split(",")
            if word.strip()
        ]

    search_id = add_search(
        name=new_search["query"],
        query=new_search["query"],
        min_price=new_search.get("min_price"),
        max_price=new_search.get("max_price"),
    )

    for word in words:
        add_blacklist_word(search_id, word)

    context.user_data.clear()

    search = get_search(search_id)

    await update.message.reply_text(
        "✅ RICERCA CREATA\n\n" + search_text(search),
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✏️ Gestisci",
                    callback_data=f"search:{search_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Menu",
                    callback_data="main"
                )
            ],
        ]),
    )


# =========================================================
# LISTA RICERCHE
# =========================================================

async def show_search_list(query, mode="view"):
    searches = get_searches()

    if not searches:
        await query.edit_message_text(
            "📋 Non hai ancora ricerche.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "➕ Aggiungi ricerca",
                        callback_data="add_search"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⬅️ Indietro",
                        callback_data="main"
                    )
                ],
            ]),
        )
        return

    buttons = []

    for search in searches:
        icon = "✅" if search["active"] else "⏸"

        buttons.append([
            InlineKeyboardButton(
                f"{icon} {search['query']}",
                callback_data=f"search:{search['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Indietro",
            callback_data="main"
        )
    ])

    title = (
        "✏️ MODIFICA RICERCA"
        if mode == "edit"
        else "📋 LE MIE RICERCHE"
    )

    await query.edit_message_text(
        title + "\n\nSeleziona una ricerca:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# =========================================================
# DETTAGLIO RICERCA
# =========================================================

async def show_search(query, search_id):
    search = get_search(search_id)

    if not search:
        await query.answer("Ricerca non trovata.")
        return

    toggle_text = (
        "⏸ Disattiva"
        if search["active"]
        else "▶️ Attiva"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔎 Modifica query",
                callback_data=f"edit_query:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💶 Modifica prezzo minimo",
                callback_data=f"edit_min:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💶 Modifica prezzo massimo",
                callback_data=f"edit_max:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🚫 Gestisci blacklist",
                callback_data=f"blacklist:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "💬 Modifica messaggio DM",
                callback_data=f"edit_dm:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                toggle_text,
                callback_data=f"toggle:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🗑 Elimina ricerca",
                callback_data=f"delete_confirm:{search_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "⬅️ Ricerche",
                callback_data="edit_searches"
            )
        ],
        [
            InlineKeyboardButton(
                "🏠 Menu",
                callback_data="main"
            )
        ],
    ])

    await query.edit_message_text(
        search_text(search),
        reply_markup=keyboard,
    )


# =========================================================
# MODIFICA QUERY / PREZZI
# =========================================================

async def begin_edit_field(query, context, search_id, field):
    search = get_search(search_id)

    if not search:
        return

    context.user_data.clear()
    context.user_data["edit_search_id"] = search_id
    context.user_data["pending_action"] = field

    if field == "edit_query":
        text = (
            f"🔎 Query attuale: {search['query']}\n\n"
            "Scrivi la nuova ricerca:"
        )

    elif field == "edit_min":
        text = (
            f"💶 Minimo attuale: "
            f"{format_price(search['min_price'])}\n\n"
            "Scrivi il nuovo minimo.\n"
            "Scrivi 0 per rimuovere il limite."
        )

    else:
        text = (
            f"💶 Massimo attuale: "
            f"{format_price(search['max_price'])}\n\n"
            "Scrivi il nuovo massimo.\n"
            "Scrivi 0 per rimuovere il limite."
        )

    await query.edit_message_text(text)


def update_search_field(search_id, field, value):
    conn = get_connection()

    if field == "query":
        conn.execute("""
            UPDATE searches
            SET query = ?,
                name = ?,
                baseline_done = 0
            WHERE id = ?
        """, (
            value,
            value,
            search_id,
        ))

    elif field == "min_price":
        conn.execute("""
            UPDATE searches
            SET min_price = ?,
                baseline_done = 0
            WHERE id = ?
        """, (
            value,
            search_id,
        ))

    elif field == "max_price":
        conn.execute("""
            UPDATE searches
            SET max_price = ?,
                baseline_done = 0
            WHERE id = ?
        """, (
            value,
            search_id,
        ))

    conn.commit()
    conn.close()


async def handle_edit_field(update, context, action, text):
    search_id = context.user_data.get("edit_search_id")

    search = get_search(search_id)

    if not search:
        context.user_data.clear()
        return

    if action == "edit_query":
        value = text.strip()

        if not value:
            await update.message.reply_text(
                "❌ Query non valida."
            )
            return

        update_search_field(search_id, "query", value)

    elif action == "edit_min":
        try:
            value = parse_price(text)
        except Exception:
            await update.message.reply_text(
                "❌ Prezzo non valido."
            )
            return

        if (
            value is not None
            and search["max_price"] is not None
            and value > search["max_price"]
        ):
            await update.message.reply_text(
                "❌ Il minimo supera il massimo."
            )
            return

        update_search_field(
            search_id,
            "min_price",
            value,
        )

    elif action == "edit_max":
        try:
            value = parse_price(text)
        except Exception:
            await update.message.reply_text(
                "❌ Prezzo non valido."
            )
            return

        if (
            value is not None
            and search["min_price"] is not None
            and value < search["min_price"]
        ):
            await update.message.reply_text(
                "❌ Il massimo è inferiore al minimo."
            )
            return

        update_search_field(
            search_id,
            "max_price",
            value,
        )

    context.user_data.clear()

    search = get_search(search_id)

    await update.message.reply_text(
        "✅ Ricerca aggiornata.\n\n"
        + search_text(search),
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✏️ Continua modifica",
                    callback_data=f"search:{search_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Menu",
                    callback_data="main"
                )
            ],
        ]),
    )


# =========================================================
# BLACKLIST
# =========================================================

async def show_blacklist(query, search_id):
    search = get_search(search_id)

    if not search:
        return

    words = get_blacklist(search_id)

    buttons = []

    for row in words:
        buttons.append([
            InlineKeyboardButton(
                f"🗑 {row['word']}",
                callback_data=(
                    f"bl_del:{row['id']}:{search_id}"
                )
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "➕ Aggiungi parola",
            callback_data=f"bl_add:{search_id}"
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "⬅️ Ricerca",
            callback_data=f"search:{search_id}"
        )
    ])

    if words:
        words_text = "\n".join(
            f"• {row['word']}"
            for row in words
        )
    else:
        words_text = "Nessuna parola."

    await query.edit_message_text(
        f"🚫 BLACKLIST — {search['query']}\n\n"
        f"{words_text}\n\n"
        "Tocca una parola con 🗑 per eliminarla.\n\n"
        "La blacklist viene applicata a "
        "TITOLO + DESCRIZIONE.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def begin_add_blacklist(query, context, search_id):
    context.user_data.clear()

    context.user_data["pending_action"] = "bl_add"
    context.user_data["edit_search_id"] = search_id

    await query.edit_message_text(
        "🚫 Aggiungi parole alla blacklist\n\n"
        "Puoi scrivere una o più parole separate "
        "da virgola.\n\n"
        "Esempio:\n"
        "rotto, ricambi, controller"
    )


async def handle_blacklist_add(update, context, text):
    search_id = context.user_data.get("edit_search_id")

    words = [
        word.strip()
        for word in text.split(",")
        if word.strip()
    ]

    if not words:
        await update.message.reply_text(
            "❌ Inserisci almeno una parola."
        )
        return

    for word in words:
        add_blacklist_word(search_id, word)

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Blacklist aggiornata.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🚫 Apri blacklist",
                    callback_data=f"blacklist:{search_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "🏠 Menu",
                    callback_data="main"
                )
            ],
        ]),
    )



# =========================================================
# DM FACEBOOK
# =========================================================

async def begin_edit_dm(query, context, search_id):
    search = get_search(search_id)

    if not search:
        return

    current = (
        search["dm_message"]
        or "Ciao, è ancora disponibile?"
    )

    context.user_data.clear()
    context.user_data["pending_action"] = "edit_dm"
    context.user_data["edit_search_id"] = search_id

    await query.edit_message_text(
        "💬 MESSAGGIO DM\n\n"
        f"Messaggio attuale:\n\n{current}\n\n"
        "Scrivi il nuovo messaggio da utilizzare "
        "per questa ricerca."
    )


async def handle_edit_dm(update, context, text):
    search_id = context.user_data.get(
        "edit_search_id"
    )

    message = text.strip()

    if not message:
        await update.message.reply_text(
            "❌ Il messaggio non può essere vuoto."
        )
        return

    set_dm_message(
        search_id,
        message
    )

    context.user_data.clear()

    await update.message.reply_text(
        "✅ Messaggio DM salvato.",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "⬅️ Ricerca",
                    callback_data=f"search:{search_id}"
                )
            ]
        ])
    )


async def show_dm_confirmation(
    query,
    search_id,
    item_id
):
    search = get_search(search_id)
    notification = get_notification(
        search_id,
        item_id
    )

    if not search or not notification:
        await query.answer(
            "Annuncio non trovato.",
            show_alert=True
        )
        return

    message = (
        search["dm_message"]
        or "Ciao, è ancora disponibile?"
    )

    title = (
        notification["title"]
        or "Annuncio"
    )

    await query.message.reply_text(
        "✉️ INVIA DM\n\n"
        f"Annuncio:\n{title}\n\n"
        "Messaggio:\n"
        f"{message}\n\n"
        "Vuoi inviarlo al venditore?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Conferma invio",
                    callback_data=(
                        f"dm_confirm:"
                        f"{search_id}:"
                        f"{item_id}"
                    )
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ Annulla",
                    callback_data="dm_cancel"
                )
            ]
        ])
    )


async def confirm_dm(
    query,
    search_id,
    item_id
):
    search = get_search(search_id)

    notification = get_notification(
        search_id,
        item_id
    )

    if not search or not notification:
        await query.edit_message_text(
            "❌ Annuncio non trovato."
        )
        return

    # -------------------------------------------------
    # EVITA DOPPIO INVIO
    # -------------------------------------------------

    if notification["dm_sent_at"]:
        try:
            sent_at = datetime.fromisoformat(
                notification["dm_sent_at"]
            )

            sent_text = sent_at.strftime(
                "%d/%m/%Y alle %H:%M"
            )

        except Exception:
            sent_text = notification["dm_sent_at"]

        await query.edit_message_text(
            "⚠️ DM GIÀ INVIATO\n\n"
            f"Annuncio: {notification['title']}\n\n"
            f"Messaggio già inviato il {sent_text}."
        )

        return

    message = (
        search["dm_message"]
        or "Ciao, è ancora disponibile?"
    )

    item_url = notification["url"]

    await query.edit_message_text(
        "⏳ Invio messaggio Facebook..."
    )

    success, detail = await asyncio.to_thread(
        send_facebook_dm,
        item_url,
        message
    )

    if success:

        sent_at = datetime.now()

        mark_dm_sent(
            search_id,
            item_id,
            sent_at.isoformat()
        )

        await query.edit_message_text(
            "✅ DM INVIATO\n\n"
            f"{notification['title']}\n\n"
            f"🕐 {sent_at.strftime('%d/%m/%Y %H:%M')}"
        )

    else:

        await query.edit_message_text(
            "❌ Non sono riuscito a inviare il DM.\n\n"
            f"Dettaglio: {detail}"
        )


# =========================================================
# CALLBACK BUTTONS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        return

    query = update.callback_query

    await query.answer()

    data = query.data

    if data.startswith("dm_confirm:"):
        _, search_id, item_id = data.split(":", 2)

        await confirm_dm(
            query,
            int(search_id),
            item_id
        )

        return

    if data.startswith("dm:"):
        _, search_id, item_id = data.split(":", 2)

        await show_dm_confirmation(
            query,
            int(search_id),
            item_id
        )

        return

    if data == "dm_cancel":
        await query.edit_message_text(
            "❌ Invio annullato."
        )
        return

    if data.startswith("edit_dm:"):
        search_id = int(data.split(":")[1])

        await begin_edit_dm(
            query,
            context,
            search_id
        )

        return

    if data == "main":
        context.user_data.clear()
        await show_main(query)

    elif data == "add_search":
        await begin_add_search(query, context)

    elif data == "list_searches":
        await show_search_list(query, "view")

    elif data == "edit_searches":
        await show_search_list(query, "edit")

    elif data.startswith("search:"):
        search_id = int(data.split(":")[1])
        await show_search(query, search_id)

    elif data.startswith("edit_query:"):
        search_id = int(data.split(":")[1])
        await begin_edit_field(
            query,
            context,
            search_id,
            "edit_query",
        )

    elif data.startswith("edit_min:"):
        search_id = int(data.split(":")[1])
        await begin_edit_field(
            query,
            context,
            search_id,
            "edit_min",
        )

    elif data.startswith("edit_max:"):
        search_id = int(data.split(":")[1])
        await begin_edit_field(
            query,
            context,
            search_id,
            "edit_max",
        )

    elif data.startswith("blacklist:"):
        search_id = int(data.split(":")[1])
        await show_blacklist(query, search_id)

    elif data.startswith("bl_add:"):
        search_id = int(data.split(":")[1])
        await begin_add_blacklist(
            query,
            context,
            search_id,
        )

    elif data.startswith("bl_del:"):
        _, word_id, search_id = data.split(":")

        delete_blacklist_word(int(word_id))

        await show_blacklist(
            query,
            int(search_id),
        )

    elif data.startswith("toggle:"):
        search_id = int(data.split(":")[1])

        search = get_search(search_id)

        if search:
            set_search_active(
                search_id,
                not bool(search["active"]),
            )

        await show_search(query, search_id)

    elif data.startswith("delete_confirm:"):
        search_id = int(data.split(":")[1])

        search = get_search(search_id)

        if not search:
            return

        await query.edit_message_text(
            f"⚠️ Eliminare definitivamente:\n\n"
            f"🔎 {search['query']} ?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🗑 Sì, elimina",
                        callback_data=f"delete:{search_id}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        "❌ Annulla",
                        callback_data=f"search:{search_id}"
                    )
                ],
            ]),
        )

    elif data.startswith("delete:"):
        search_id = int(data.split(":")[1])

        delete_search(search_id)

        await query.edit_message_text(
            "🗑 Ricerca eliminata.",
            reply_markup=main_keyboard(),
        )


# =========================================================
# MESSAGGI TESTUALI
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not authorized(update):
        return

    text = update.message.text

    action = context.user_data.get(
        "pending_action"
    )

    if action == "add_query":
        await handle_add_query(
            update,
            context,
            text,
        )

    elif action == "add_min":
        await handle_add_min(
            update,
            context,
            text,
        )

    elif action == "add_max":
        await handle_add_max(
            update,
            context,
            text,
        )

    elif action == "add_blacklist":
        await handle_add_blacklist(
            update,
            context,
            text,
        )

    elif action in (
        "edit_query",
        "edit_min",
        "edit_max",
    ):
        await handle_edit_field(
            update,
            context,
            action,
            text,
        )

    elif action == "bl_add":
        await handle_blacklist_add(
            update,
            context,
            text,
        )

    elif action == "edit_dm":
        await handle_edit_dm(
            update,
            context,
            text,
        )

    else:
        await update.message.reply_text(
            "Usa il menu:",
            reply_markup=main_keyboard(),
        )



# =========================================================
# COMANDI MENU TELEGRAM
# =========================================================

async def show_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    context.user_data.clear()

    await update.message.reply_text(
        "📊 MARKETPLACE MONITOR\n\n"
        "Gestisci le ricerche Facebook Marketplace:",
        reply_markup=main_keyboard(),
    )


async def list_searches_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    searches = get_searches()

    if not searches:
        await update.message.reply_text(
            "📋 Non hai ancora ricerche.",
            reply_markup=main_keyboard(),
        )
        return

    buttons = []

    for search in searches:
        icon = "✅" if search["active"] else "⏸"

        buttons.append([
            InlineKeyboardButton(
                f"{icon} {search['query']}",
                callback_data=f"search:{search['id']}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🏠 Menu",
            callback_data="main"
        )
    ])

    await update.message.reply_text(
        "📋 LE MIE RICERCHE\n\n"
        "Seleziona una ricerca:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def add_search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update):
        return

    context.user_data.clear()
    context.user_data["pending_action"] = "add_query"
    context.user_data["new_search"] = {}

    await update.message.reply_text(
        "➕ NUOVA RICERCA\n\n"
        "🔎 Cosa vuoi cercare?\n\n"
        "Esempio: PS5"
    )


# =========================================================
# AVVIO
# =========================================================

def main():
    init_search_tables()

    if not BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN mancante"
        )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "menu",
            show_menu_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "ricerche",
            list_searches_command,
        )
    )

    app.add_handler(
        CommandHandler(
            "aggiungi",
            add_search_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    print("🤖 Telegram menu avviato")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
