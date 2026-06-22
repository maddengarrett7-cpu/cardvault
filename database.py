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
                google_sheet_id TEXT,
                total_scans INTEGER DEFAULT 0
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_sessions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                session_token TEXT UNIQUE NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                last_seen TIMESTAMP DEFAULT NOW()
            )
        """)
        # Migrate: add columns if missing
        for col, definition in [
            ("google_access_token", "TEXT"),
            ("google_refresh_token", "TEXT"),
            ("google_sheet_id", "TEXT"),
            ("total_scans", "INTEGER DEFAULT 0"),
            ("plan_type", "TEXT DEFAULT 'monthly'"),
            ("referral_code", "TEXT"),
            ("referred_by", "TEXT"),
            ("bonus_scans", "INTEGER DEFAULT 0"),
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

    def update_subscription(customer_id, status, plan_type=None):
        conn = get_db()
        cur = conn.cursor()
        if plan_type:
            cur.execute(
                "UPDATE users SET subscription_status = %s, plan_type = %s WHERE stripe_customer_id = %s",
                (status, plan_type, customer_id)
            )
        else:
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
        bonus = user.get('bonus_scans') or 0
        FREE_LIMIT = 10 + bonus

        # Reset scans_today if it's a new day
        scans_today = user['scans_today'] if user['scans_date'] == today else 0

        if user['subscription_status'] == 'pro':
            # Pro users: always allowed, but still track counts
            cur.execute(
                "UPDATE users SET scans_today = %s, scans_date = %s, total_scans = COALESCE(total_scans, 0) + 1 WHERE id = %s",
                (scans_today + 1, today, user_id)
            )
            conn.commit()
            cur.close()
            conn.close()
            return True, scans_today + 1, -1

        if scans_today >= FREE_LIMIT:
            # Reset the DB value if date changed but we're still at limit (edge case)
            if user['scans_date'] != today:
                cur.execute(
                    "UPDATE users SET scans_today = 0, scans_date = %s WHERE id = %s",
                    (today, user_id)
                )
                conn.commit()
            cur.close()
            conn.close()
            return False, scans_today, FREE_LIMIT

        cur.execute(
            "UPDATE users SET scans_today = %s, scans_date = %s, total_scans = COALESCE(total_scans, 0) + 1 WHERE id = %s",
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

    def update_subscription(customer_id, status, plan_type=None):
        conn = get_db()
        if plan_type:
            conn.execute("UPDATE users SET subscription_status = ?, plan_type = ? WHERE stripe_customer_id = ?", (status, plan_type, customer_id))
        else:
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

        # Reset scans_today if it's a new day
        scans_today = user['scans_today'] if user['scans_date'] == today else 0

        if user['subscription_status'] == 'pro':
            # Pro users: always allowed, but still track counts
            conn.execute(
                "UPDATE users SET scans_today = ?, scans_date = ?, total_scans = COALESCE(total_scans, 0) + 1 WHERE id = ?",
                (scans_today + 1, today, user_id)
            )
            conn.commit()
            conn.close()
            return True, scans_today + 1, -1

        if scans_today >= FREE_LIMIT:
            conn.close()
            return False, scans_today, FREE_LIMIT

        conn.execute(
            "UPDATE users SET scans_today = ?, scans_date = ?, total_scans = COALESCE(total_scans, 0) + 1 WHERE id = ?",
            (scans_today + 1, today, user_id)
        )
        conn.commit()
        conn.close()
        return True, scans_today + 1, FREE_LIMIT


def save_reset_token(email, token, expires_at):
    """Store a password reset token."""
    if DATABASE_URL:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                id SERIAL PRIMARY KEY,
                email TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("DELETE FROM password_resets WHERE email = %s", (email.lower(),))
        cur.execute(
            "INSERT INTO password_resets (email, token, expires_at) VALUES (%s, %s, %s)",
            (email.lower(), token, expires_at)
        )
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS password_resets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("DELETE FROM password_resets WHERE email = ?", (email.lower(),))
        conn.execute(
            "INSERT INTO password_resets (email, token, expires_at) VALUES (?, ?, ?)",
            (email.lower(), token, expires_at)
        )
        conn.commit()
        conn.close()


def get_reset_token(token):
    """Look up a reset token, return the row or None."""
    if DATABASE_URL:
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM password_resets WHERE token = %s", (token,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        return dict(row) if row else None
    else:
        conn = get_db()
        row = conn.execute("SELECT * FROM password_resets WHERE token = ?", (token,)).fetchone()
        conn.close()
        return dict(row) if row else None


def delete_reset_token(token):
    """Delete a used reset token."""
    if DATABASE_URL:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM password_resets WHERE token = %s", (token,))
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = get_db()
        conn.execute("DELETE FROM password_resets WHERE token = ?", (token,))
        conn.commit()
        conn.close()


def update_password(email, new_hash):
    """Update a user's password hash."""
    if DATABASE_URL:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET password_hash = %s WHERE email = %s", (new_hash, email.lower()))
        conn.commit()
        cur.close()
        conn.close()
    else:
        conn = get_db()
        conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (new_hash, email.lower()))
        conn.commit()
        conn.close()


MAX_SESSIONS = 2

def create_session(user_id, session_token):
    """Create a new session, removing oldest if over limit."""
    if DATABASE_URL:
        conn = get_db()
        cur = conn.cursor()
        # Count existing sessions
        cur.execute("SELECT id FROM user_sessions WHERE user_id = %s ORDER BY last_seen ASC", (user_id,))
        sessions = cur.fetchall()
        # Remove oldest sessions if at limit
        while len(sessions) >= MAX_SESSIONS:
            cur.execute("DELETE FROM user_sessions WHERE id = %s", (sessions[0][0],))
            sessions.pop(0)
        cur.execute(
            "INSERT INTO user_sessions (user_id, session_token) VALUES (%s, %s)",
            (user_id, session_token)
        )
        conn.commit()
        cur.close()
        conn.close()

def validate_session(user_id, session_token):
    """Check if session token is valid for this user."""
    if not DATABASE_URL:
        return True  # Skip in SQLite dev mode
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id FROM user_sessions WHERE user_id = %s AND session_token = %s",
        (user_id, session_token)
    )
    valid = cur.fetchone() is not None
    if valid:
        cur.execute(
            "UPDATE user_sessions SET last_seen = NOW() WHERE user_id = %s AND session_token = %s",
            (user_id, session_token)
        )
        conn.commit()
    cur.close()
    conn.close()
    return valid

def delete_session(session_token):
    """Remove a session on logout."""
    if not DATABASE_URL:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM user_sessions WHERE session_token = %s", (session_token,))
    conn.commit()
    cur.close()
    conn.close()
