"""
Price alert job — runs periodically to detect significant eBay value changes
and send push notifications to users.
"""
import os
import time
import threading
import requests
import logging
from database import get_db, DATABASE_URL

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
ALERT_THRESHOLD = 0.10  # 10% change triggers alert

# Arbitrary constant used as a Postgres advisory lock key. gunicorn runs multiple
# worker processes, each of which starts its own copy of the background scheduler
# thread below -- the advisory lock makes sure that when two workers' timers land
# on the same tick, only one of them actually runs the check (and sends pushes).
_ADVISORY_LOCK_KEY = 918273645


def send_expo_push(token, title, body, data=None):
    """Send a single Expo push notification."""
    if not token or not token.startswith("ExponentPushToken"):
        return
    try:
        requests.post(EXPO_PUSH_URL, json={
            "to": token,
            "title": title,
            "body": body,
            "data": data or {},
            "sound": "default",
            "priority": "high",
        }, timeout=10)
    except Exception as e:
        logger.warning(f"Push send failed: {e}")


def check_price_changes():
    """
    For each user with a push token, check their collection for cards
    whose eBay value has moved 10%+ since last check. Send alerts.
    """
    from app import search_ebay_sold  # import here to avoid circular

    try:
        conn = get_db()
        cur = conn.cursor()

        # Get all users with push tokens who haven't opted out of price alerts
        cur.execute("""
            SELECT id, push_token FROM users
            WHERE push_token IS NOT NULL AND push_token != ''
            AND COALESCE(price_alerts_enabled, TRUE) = TRUE
        """)
        users = cur.fetchall()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f"Price alert DB error: {e}")
        return

    for user_row in users:
        user_id = user_row[0]
        push_token = user_row[1]

        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, name, card, ebay_avg, year, grade
                FROM scan_history
                WHERE user_id = %s AND ebay_avg IS NOT NULL AND ebay_avg > 0
                ORDER BY ebay_avg DESC LIMIT 20
            """, (user_id,))
            cards = cur.fetchall()
            cur.close()
            conn.close()
        except Exception:
            continue

        for card_row in cards:
            card_id, name, card_desc, old_avg, year, grade = card_row
            try:
                query = f"{year or ''} {name or ''} {card_desc or ''} {grade or 'Raw'}".strip()
                result, err = search_ebay_sold(query)
                new_avg = result.get('avg') if result else None
                if err or not new_avg or not old_avg:
                    continue

                change_pct = (new_avg - old_avg) / old_avg

                if abs(change_pct) >= ALERT_THRESHOLD:
                    direction = "📈 up" if change_pct > 0 else "📉 down"
                    pct_str = f"{abs(change_pct)*100:.0f}%"
                    title = f"Price Alert: {name or 'Your card'}"
                    body = f"{card_desc or name} is {direction} {pct_str} — now ~${new_avg:.0f}"
                    send_expo_push(push_token, title, body, {"card_id": card_id})

                    # Update the stored value
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute("UPDATE scan_history SET ebay_avg = %s WHERE id = %s", (new_avg, card_id))
                    conn.commit(); cur.close(); conn.close()

            except Exception as e:
                logger.warning(f"Price check failed for card {card_id}: {e}")
                continue


def run_price_check_job():
    """Run check_price_changes(), guarded by a Postgres advisory lock so that if
    multiple gunicorn workers each have the scheduler thread running, only one
    of them actually executes the check (and sends pushes) per tick."""
    if not DATABASE_URL:
        # No Postgres (local SQLite dev) -- nothing to lock against, just run it.
        check_price_changes()
        return

    conn = None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_ADVISORY_LOCK_KEY,))
        got_lock = cur.fetchone()[0]
        cur.close()
        if not got_lock:
            conn.close()
            return  # another worker already has this tick
    except Exception as e:
        logger.warning(f"Price alert lock error: {e}")
        if conn:
            conn.close()
        return

    try:
        check_price_changes()
    except Exception as e:
        logger.error(f"Price alert job crashed: {e}")
    finally:
        try:
            cur = conn.cursor()
            cur.execute("SELECT pg_advisory_unlock(%s)", (_ADVISORY_LOCK_KEY,))
            conn.commit()
            cur.close()
        except Exception:
            pass
        conn.close()


def start_price_alert_scheduler(interval_hours=6):
    """Kick off a daemon thread that runs the price-check job on a fixed
    interval for the lifetime of this process. Safe to call from every
    gunicorn worker -- see the advisory lock in run_price_check_job()."""
    def loop():
        while True:
            time.sleep(interval_hours * 3600)
            run_price_check_job()

    threading.Thread(target=loop, daemon=True).start()
    logger.info(f"Price alert scheduler started (every {interval_hours}h)")


if __name__ == "__main__":
    check_price_changes()
