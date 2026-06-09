import sqlite3
import os
from datetime import date

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
            scans_date TEXT DEFAULT ''
        )
    """)
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
    """Returns (allowed, scans_used, limit). Increments scan count if allowed."""
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return False, 0, 0

    user = dict(user)
    today = str(date.today())
    FREE_LIMIT = 20

    # Pro users — unlimited
    if user['subscription_status'] == 'pro':
        conn.close()
        return True, 0, -1

    # Reset daily count if new day
    scans_today = user['scans_today'] if user['scans_date'] == today else 0

    if scans_today >= FREE_LIMIT:
        conn.close()
        return False, scans_today, FREE_LIMIT

    # Increment
    conn.execute(
        "UPDATE users SET scans_today = ?, scans_date = ? WHERE id = ?",
        (scans_today + 1, today, user_id)
    )
    conn.commit()
    conn.close()
    return True, scans_today + 1, FREE_LIMIT
