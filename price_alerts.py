"""
Price alert job — runs periodically to detect significant eBay value changes
and send push notifications to users.
"""
import os
import requests
import logging
from database import get_db

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
ALERT_THRESHOLD = 0.10  # 10% change triggers alert


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
    from app import fetch_ebay_price  # import here to avoid circular

    try:
        conn = get_db()
        cur = conn.cursor()

        # Get all users with push tokens
        cur.execute("SELECT id, push_token FROM users WHERE push_token IS NOT NULL AND push_token != ''")
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
                new_avg = fetch_ebay_price(query)
                if not new_avg or not old_avg:
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


if __name__ == "__main__":
    check_price_changes()
