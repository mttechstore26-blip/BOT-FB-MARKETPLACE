import sqlite3

DATABASE = "marketplace.db"


def get_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_search_tables():
    conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            query TEXT NOT NULL,
            min_price REAL,
            max_price REAL,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(searches)").fetchall()
    }

    if "baseline_done" not in columns:
        conn.execute("""
            ALTER TABLE searches
            ADD COLUMN baseline_done INTEGER NOT NULL DEFAULT 0
        """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS blacklist_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            search_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            FOREIGN KEY(search_id)
                REFERENCES searches(id)
                ON DELETE CASCADE
        )
    """)

    conn.commit()
    conn.close()


def add_search(name, query, min_price=None, max_price=None):
    conn = get_connection()

    cursor = conn.execute("""
        INSERT INTO searches (
            name,
            query,
            min_price,
            max_price,
            active
        )
        VALUES (?, ?, ?, ?, 1)
    """, (
        name,
        query,
        min_price,
        max_price,
    ))

    search_id = cursor.lastrowid

    conn.commit()
    conn.close()

    return search_id


def get_searches(active_only=False):
    conn = get_connection()

    if active_only:
        rows = conn.execute("""
            SELECT *
            FROM searches
            WHERE active = 1
            ORDER BY id
        """).fetchall()

    else:
        rows = conn.execute("""
            SELECT *
            FROM searches
            ORDER BY id
        """).fetchall()

    conn.close()

    return rows


def get_search(search_id):
    conn = get_connection()

    row = conn.execute("""
        SELECT *
        FROM searches
        WHERE id = ?
    """, (search_id,)).fetchone()

    conn.close()

    return row


def update_search(
    search_id,
    name=None,
    query=None,
    min_price=None,
    max_price=None,
):
    conn = get_connection()

    current = conn.execute("""
        SELECT *
        FROM searches
        WHERE id = ?
    """, (search_id,)).fetchone()

    if not current:
        conn.close()
        return False

    conn.execute("""
        UPDATE searches
        SET
            name = ?,
            query = ?,
            min_price = ?,
            max_price = ?
        WHERE id = ?
    """, (
        name if name is not None else current["name"],
        query if query is not None else current["query"],
        min_price if min_price is not None else current["min_price"],
        max_price if max_price is not None else current["max_price"],
        search_id,
    ))

    conn.commit()
    conn.close()

    return True


def set_search_active(search_id, active):
    conn = get_connection()

    conn.execute("""
        UPDATE searches
        SET active = ?
        WHERE id = ?
    """, (
        1 if active else 0,
        search_id,
    ))

    conn.commit()
    conn.close()


def delete_search(search_id):
    conn = get_connection()

    conn.execute("""
        DELETE FROM blacklist_words
        WHERE search_id = ?
    """, (search_id,))

    conn.execute("""
        DELETE FROM searches
        WHERE id = ?
    """, (search_id,))

    conn.commit()
    conn.close()


def add_blacklist_word(search_id, word):
    word = word.strip()

    if not word:
        return

    conn = get_connection()

    existing = conn.execute("""
        SELECT id
        FROM blacklist_words
        WHERE search_id = ?
          AND LOWER(word) = LOWER(?)
    """, (
        search_id,
        word,
    )).fetchone()

    if not existing:
        conn.execute("""
            INSERT INTO blacklist_words (
                search_id,
                word
            )
            VALUES (?, ?)
        """, (
            search_id,
            word,
        ))

    conn.commit()
    conn.close()


def get_blacklist(search_id):
    conn = get_connection()

    rows = conn.execute("""
        SELECT *
        FROM blacklist_words
        WHERE search_id = ?
        ORDER BY word
    """, (search_id,)).fetchall()

    conn.close()

    return rows


def delete_blacklist_word(word_id):
    conn = get_connection()

    conn.execute("""
        DELETE FROM blacklist_words
        WHERE id = ?
    """, (word_id,))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_search_tables()
    print("✅ Tabelle ricerche inizializzate")


def set_dm_message(search_id, message):
    conn = get_connection()

    conn.execute("""
        UPDATE searches
        SET dm_message = ?
        WHERE id = ?
    """, (
        message.strip(),
        search_id,
    ))

    conn.commit()
    conn.close()


def get_notification(search_id, item_id):
    conn = get_connection()

    row = conn.execute("""
        SELECT *
        FROM notifications
        WHERE search_id = ?
          AND item_id = ?
    """, (
        search_id,
        item_id,
    )).fetchone()

    conn.close()

    return row


def mark_dm_sent(search_id, item_id, sent_at):
    conn = get_connection()

    conn.execute("""
        UPDATE notifications
        SET dm_sent_at = ?
        WHERE search_id = ?
          AND item_id = ?
    """, (
        sent_at,
        search_id,
        item_id,
    ))

    conn.commit()
    conn.close()
