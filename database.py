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

        # Create core tables
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
        conn.commit()

        # Create password_resets table
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Create deals table
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deals (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    card_name TEXT,
                    card_desc TEXT,
                    buyer_instagram TEXT,
                    buyer_name TEXT,
                    sale_price FLOAT,
                    fee_amount FLOAT,
                    stripe_session_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Create scan_history table separately
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scan_history (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    scanned_at TIMESTAMP DEFAULT NOW(),
                    card TEXT, name TEXT, year INTEGER, brand TEXT,
                    set_name TEXT, parallel TEXT, grade TEXT, cert TEXT,
                    serial TEXT, card_type TEXT, ebay_avg FLOAT,
                    ebay_high FLOAT, ebay_low FLOAT
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Migrate: add columns one by one, each in its own transaction
        for col, definition in [
            ("google_access_token", "TEXT"),
            ("google_refresh_token", "TEXT"),
            ("google_sheet_id", "TEXT"),
            ("total_scans", "INTEGER DEFAULT 0"),
            ("plan_type", "TEXT DEFAULT 'monthly'"),
            ("referral_code", "TEXT"),
            ("referred_by", "TEXT"),
            ("bonus_scans", "INTEGER DEFAULT 0"),
            ("auto_sheet", "BOOLEAN DEFAULT TRUE"),
            ("trial_end", "TEXT"),
            ("push_token", "TEXT"),
            ("price_alerts_enabled", "BOOLEAN DEFAULT TRUE"),
            ("sheet_tab", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
                conn.commit()
            except Exception:
                conn.rollback()

        for col, definition in [
            ("cl_value", "FLOAT"),
            ("cl_last_sale", "FLOAT"),
            ("ebay_sales", "TEXT"),
            ("notes", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS {col} {definition}")
                conn.commit()
            except Exception:
                conn.rollback()



        # Marketplace listings table
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketplace_listings (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT, year INTEGER, brand TEXT, set_name TEXT,
                    parallel TEXT, grade TEXT, cert TEXT, serial TEXT,
                    sport TEXT, price FLOAT, open_to_offers BOOLEAN DEFAULT TRUE,
                    description TEXT, image_urls TEXT,
                    boosted BOOLEAN DEFAULT FALSE,
                    boost_expires_at TIMESTAMP,
                    sold BOOLEAN DEFAULT FALSE,
                    views INTEGER DEFAULT 0,
                    likes INTEGER DEFAULT 0,
                    seller_instagram TEXT,
                    status TEXT DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT NOW(),
                    expires_at TIMESTAMP DEFAULT NOW() + INTERVAL '30 days'
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Bulk lot + edit support on marketplace_listings
        for col, definition in [
            ("is_bulk_lot", "BOOLEAN DEFAULT FALSE"),
            ("lot_card_count", "INTEGER"),
            ("lot_contents", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE marketplace_listings ADD COLUMN IF NOT EXISTS {col} {definition}")
                conn.commit()
            except Exception:
                conn.rollback()

        # Marketplace likes
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketplace_likes (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(user_id, listing_id)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Chat rooms (DMs + group chats)
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_rooms (
                    id SERIAL PRIMARY KEY,
                    name TEXT,
                    is_group BOOLEAN DEFAULT FALSE,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE SET NULL,
                    avatar_url TEXT,
                    last_message TEXT,
                    last_message_at TIMESTAMP DEFAULT NOW(),
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Chat room members
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_room_members (
                    id SERIAL PRIMARY KEY,
                    room_id INTEGER REFERENCES chat_rooms(id) ON DELETE CASCADE,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    unread_count INTEGER DEFAULT 0,
                    joined_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(room_id, user_id)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Chat messages
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id SERIAL PRIMARY KEY,
                    room_id INTEGER REFERENCES chat_rooms(id) ON DELETE CASCADE,
                    sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    offer_amount FLOAT,
                    image_url TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Keep old marketplace_messages for backwards compat
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS marketplace_messages (
                    id SERIAL PRIMARY KEY,
                    listing_id INTEGER REFERENCES marketplace_listings(id) ON DELETE CASCADE,
                    sender_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    receiver_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    message TEXT NOT NULL,
                    offer_amount FLOAT,
                    read BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Seller ratings
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS seller_ratings (
                    id SERIAL PRIMARY KEY,
                    seller_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    rater_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    listing_id INTEGER REFERENCES marketplace_listings(id),
                    rating INTEGER CHECK (rating BETWEEN 1 AND 5),
                    review TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(rater_id, listing_id)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Blocked users (Apple 1.2 — safety: users must be able to block each other)
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS blocked_users (
                    id SERIAL PRIMARY KEY,
                    blocker_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    blocked_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(blocker_id, blocked_id)
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # User reports (Apple 1.2 — safety: users must be able to report abuse)
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_reports (
                    id SERIAL PRIMARY KEY,
                    reporter_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    reported_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    room_id INTEGER REFERENCES chat_rooms(id) ON DELETE SET NULL,
                    reason TEXT,
                    status TEXT DEFAULT 'open',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()

        # Profile columns on users table
        for col, definition in [
            ("username", "TEXT"),
            ("profile_pic_url", "TEXT"),
            ("bio", "TEXT"),
            ("career_scans", "INTEGER DEFAULT 0"),
        ]:
            try:
                cur.execute(f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {definition}")
                conn.commit()
            except Exception:
                conn.rollback()

        # paid_price on scan_history
        try:
            cur.execute("ALTER TABLE scan_history ADD COLUMN IF NOT EXISTS paid_price FLOAT")
            conn.commit()
        except Exception:
            conn.rollback()

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

        # 7-day free trial: unlimited scans for new users
        from datetime import datetime, timedelta
        created_at = user.get('created_at')
        on_trial = False
        if created_at:
            if hasattr(created_at, 'date'):
                days_old = (datetime.utcnow() - created_at.replace(tzinfo=None)).days
            else:
                try:
                    days_old = (datetime.utcnow() - datetime.fromisoformat(str(created_at))).days
                except:
                    days_old = 999
            on_trial = days_old < 7

        FREE_LIMIT = 999999 if on_trial else (10 + bonus)

        # Reset scans_today if it's a new day
        scans_today = user['scans_today'] if user['scans_date'] == today else 0

        if user['subscription_status'] == 'pro' or on_trial:
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

    def save_scan(user_id, data):
        conn = get_db()
        cur = conn.cursor()
        year = data.get('year')
        try:
            year = int(year) if year else None
        except (ValueError, TypeError):
            year = None
        import json as _json
        ebay_sales = data.get('ebay_sales')
        if isinstance(ebay_sales, list):
            ebay_sales = _json.dumps(ebay_sales)
        cur.execute("""
            INSERT INTO scan_history (user_id, card, name, year, brand, set_name, parallel, grade, cert, serial, card_type, ebay_avg, ebay_high, ebay_low, cl_value, cl_last_sale, ebay_sales)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
        """, (
            user_id,
            data.get('card'), data.get('name'), year,
            data.get('brand'), data.get('set'), data.get('parallel'),
            data.get('grade'), data.get('cert'), data.get('serial'),
            data.get('card_type'), data.get('ebay_avg'),
            data.get('ebay_high'), data.get('ebay_low'),
            data.get('cl_value'), data.get('cl_last_sale'), ebay_sales,
        ))
        row = cur.fetchone()
        new_id = row[0] if row else None
        conn.commit(); cur.close(); conn.close()
        return new_id

    def get_scan_history(user_id, limit=100, offset=0, search='', grade_filter=''):
        conn = get_db()
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        where = "WHERE user_id = %s"
        params = [user_id]
        if search:
            where += " AND (LOWER(card) LIKE %s OR LOWER(name) LIKE %s OR LOWER(set_name) LIKE %s)"
            s = f"%{search.lower()}%"
            params += [s, s, s]
        if grade_filter == 'graded':
            where += " AND grade IS NOT NULL AND grade != 'Raw' AND grade != ''"
        elif grade_filter == 'raw':
            where += " AND (grade IS NULL OR grade = 'Raw' OR grade = '')"
        cur.execute(f"SELECT * FROM scan_history {where} ORDER BY scanned_at DESC LIMIT %s OFFSET %s",
                    params + [limit, offset])
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(f"SELECT COUNT(*) FROM scan_history {where}", params)
        row = cur.fetchone()
        total = list(row.values())[0] if row else 0
        cur.close(); conn.close()
        return rows, total

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                scanned_at TEXT DEFAULT (datetime('now')),
                card TEXT, name TEXT, year INTEGER, brand TEXT,
                set_name TEXT, parallel TEXT, grade TEXT, cert TEXT,
                serial TEXT, card_type TEXT, ebay_avg REAL,
                ebay_high REAL, ebay_low REAL
            )
        """)
        conn.commit()
        conn.close()

    def save_scan(user_id, data):
        conn = get_db()
        conn.execute("""
            INSERT INTO scan_history (user_id, card, name, year, brand, set_name, parallel, grade, cert, serial, card_type, ebay_avg, ebay_high, ebay_low)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            user_id,
            data.get('card'), data.get('name'), data.get('year'),
            data.get('brand'), data.get('set'), data.get('parallel'),
            data.get('grade'), data.get('cert'), data.get('serial'),
            data.get('card_type'), data.get('ebay_avg'),
            data.get('ebay_high'), data.get('ebay_low'),
        ))
        conn.commit(); conn.close()

    def get_scan_history(user_id, limit=100, offset=0):
        conn = get_db()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM scan_history WHERE user_id = ? ORDER BY scanned_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset)
        ).fetchall()]
        total = conn.execute("SELECT COUNT(*) FROM scan_history WHERE user_id = ?", (user_id,)).fetchone()[0]
        conn.close()
        return rows, total

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
        # Ensure table exists in its own commit first
        try:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS password_resets (
                    id SERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    token TEXT UNIQUE NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            conn.commit()
        except Exception:
            conn.rollback()
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
