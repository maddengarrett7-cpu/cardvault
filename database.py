import os
from datetime import date

DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Use PostgreSQL if DATABASE_URL is set, otherwise SQLite
if DATABASE_URL:
    import psycopg2
    import psycopg2.extras

    def get_db():
        conn = psycopg2.connect(DATABASE_URL)
        return conn

    def init_db():
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                stripe_customer_id TEXT,
                subscription_status TEXT DEFAULT 'free',
                scans_today INTEGER DEFAULT 0,
                scans_date TEXT DEFAULT '',
                google_access_token TEXT,
                google_refresh_token TEXT,
                google_sheet_id TEXT
            )
        """)
        # Migrate: add columns if missing
        for col, definition in [
            ("google_access_token", "TEXT"),
            ("google_refresh_token", "TEXT"),
            ("google_sheet_id", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
            except Exception:
                conn.rollback()
        conn.commit()
        cur.close()
        conn.close()

    def get_user_by_email(email):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE email = %s", (email.lower(),))
        user = cur.fetchone()
        cur.close()
        conn.close()
        return dict(user) if user else None

    def get_user_by_id(user_id):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        cur.close()
        conn.close()
        return dict(user) if user else None

    def create_user(email, password_hash):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            cur.execute(
                "INSERT INTO users (email, password_hash) VALUES (%s, %s) RETURNING *",
                (email.lower(), password_hash)
            )
            user = cur.fetchone()
            conn.commit()
            return dict(user)
        except Exception:
            conn.rollback()
            return None
        finally:
            cur.close()
            conn.close()

    def save_google_tokens(user_id, access_token, refresh_token):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET google_access_token = %s, google_refresh_token = %s WHERE id = %s",
            (access_token, refresh_token, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()

    def save_google_sheet_id(user_id, sheet_id):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET google_sheet_id = %s WHERE id = %s", (sheet_id, user_id))
        conn.commit()
        cur.close()
        conn.close()

    def clear_google_tokens(user_id):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET google_access_token = NULL, google_refresh_token = NULL, google_sheet_id = NULL WHERE id = %s",
            (user_id,)
        )
        conn.commit()
        cur.close()
        conn.close()

    def update_stripe_customer(user_id, customer_id):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET stripe_customer_id = %s WHERE id = %s", (customer_id, user_id))
        conn.commit()
        cur.close()
        conn.close()

    def update_subscription(customer_id, status):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET subscription_status = %s WHERE stripe_customer_id = %s",
            (status, customer_id)
        )
        conn.commit()
        cur.close()
        conn.close()

    def check_and_increment_scans(user_id):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        if not user:
            cur.close()
            conn.close()
            return False, 0, 0

        user = dict(user)
        today = str(date.today())
        FREE_LIMIT = 10

        if user['subscription_status'] == 'pro':
            cur.close()
            conn.close()
            return True, 0, -1

        scans_today = user['scans_today'] if user['scans_date'] == today else 0

        if scans_today >= FREE_LIMIT:
            cur.close()
            conn.close()
            return False, scans_today, FREE_LIMIT

        cur.execute(
            "UPDATE users SET scans_today = %s, scans_date = %s WHERE id = %s",
            (scans_today + 1, today, user_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return True, scans_today + 1, FREE_LIMIT

else:
    # SQLite fallback for local dev
    import sqlite3

    DB_PATH = os.environ.get("DB_PATH", "slabscan.db")

    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db():
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now')),
                stripe_customer_id TEXT,
                subscription_status TEXT DEFAULT 'free',
                scans_today INTEGER DEFAULT 0,
                scans_date TEXT DEFAULT '',
                google_access_token TEXT,
                google_refresh_token TEXT,
                google_sheet_id TEXT
            )
        """)
        for col, definition in [
            ("google_access_token", "TEXT"),
            ("google_refresh_token", "TEXT"),
            ("google_sheet_id", "TEXT"),
        ]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
            except Exception:
                pass
        conn.commit()
        conn.close()

    def get_user_by_email(email):
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
        conn.close()
        return dict(user) if user else None

    def get_user_by_id(user_id):
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        return dict(user) if user else None

    def create_user(email, password_hash):
        conn = get_db()
        try:
            conn.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email.lower(), password_hash))
            conn.commit()
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
            return dict(user)
        except sqlite3.IntegrityError:
            return None
        finally:
            conn.close()

    def save_google_tokens(user_id, access_token, refresh_token):
        conn = get_db()
        conn.execute(
            "UPDATE users SET google_access_token = ?, google_refresh_token = ? WHERE id = ?",
            (access_token, refresh_token, user_id)
        )
        conn.commit()
        conn.close()

    def save_google_sheet_id(user_id, sheet_id):
        conn = get_db()
        conn.execute("UPDATE users SET google_sheet_id = ? WHERE id = ?", (sheet_id, user_id))
        conn.commit()
        conn.close()

    def clear_google_tokens(user_id):
        conn = get_db()
        conn.execute(
            "UPDATE users SET google_access_token = NULL, google_refresh_token = NULL, google_sheet_id = NULL WHERE id = ?",
            (user_id,)
        )
        conn.commit()
        conn.close()

    def update_stripe_customer(user_id, customer_id):
        conn = get_db()
        conn.execute("UPDATE users SET stripe_customer_id = ? WHERE id = ?", (customer_id, user_id))
        conn.commit()
        conn.close()

    def update_subscription(customer_id, status):
        conn = get_db()
        conn.execute("UPDATE users SET subscription_status = ? WHERE stripe_customer_id = ?", (status, customer_id))
        conn.commit()
        conn.close()

    def check_and_increment_scans(user_id):
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            conn.close()
            return False, 0, 0

        user = dict(user)
        today = str(date.today())
        FREE_LIMIT = 10

        if user['subscription_status'] == 'pro':
            conn.close()
            return True, 0, -1

        scans_today = user['scans_today'] if user['scans_date'] == today else 0

        if scans_today >= FREE_LIMIT:
            conn.close()
            return False, scans_today, FREE_LIMIT

        conn.execute(
            "UPDATE users SET scans_today = ?, scans_date = ? WHERE id = ?",
            (scans_today + 1, today, user_id)
        )
        conn.commit()
        conn.close()
        return True, scans_today + 1, FREE_LIMIT
