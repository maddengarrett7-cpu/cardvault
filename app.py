#!/usr/bin/env python3
"""
Sports Card Scanner - Web App
Run this and open http://localhost:5000 in your browser
"""

import os
import json
import time
import uuid
import tempfile
import threading
import base64
import requests
import stripe
import psycopg2.extras
from datetime import datetime
from functools import wraps
from collections import defaultdict
from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for, stream_with_context, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
import cv2
from google import genai
from google.genai import types as genai_types
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from rookies import get_rookie_hint, get_player_draft_year
from database import init_db, get_user_by_email, get_user_by_id, create_user, \
    update_stripe_customer, update_subscription, check_and_increment_scans, \
    save_google_tokens, save_google_sheet_id, clear_google_tokens, \
    create_session, validate_session, delete_session, \
    save_reset_token, get_reset_token, delete_reset_token, update_password, \
    DATABASE_URL, save_scan, get_scan_history

# ── Config ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")

# ── Gemini API key pool — add GEMINI_API_KEY_2, _3 etc in Railway env vars ─
_GEMINI_KEY_POOL = [k for k in [
    os.environ.get("GEMINI_API_KEY"),
    os.environ.get("GEMINI_API_KEY_2"),
    os.environ.get("GEMINI_API_KEY_3"),
    os.environ.get("GEMINI_API_KEY_4"),
] if k]
# Always ensure the primary key is in the pool
if GEMINI_API_KEY and GEMINI_API_KEY not in _GEMINI_KEY_POOL:
    _GEMINI_KEY_POOL.insert(0, GEMINI_API_KEY)
_key_index = 0

def _get_next_gemini_key():
    """Round-robin through available API keys, always falls back to primary."""
    global _key_index
    if not _GEMINI_KEY_POOL:
        return GEMINI_API_KEY
    key = _GEMINI_KEY_POOL[_key_index % len(_GEMINI_KEY_POOL)]
    _key_index += 1
    return key or GEMINI_API_KEY
GOOGLE_CREDS_FILE    = os.path.join(os.path.dirname(__file__), "google_creds.json")
SPREADSHEET_ID       = os.environ.get("SPREADSHEET_ID", "")
SHEET_SERVICE_EMAIL  = os.environ.get("SHEET_SERVICE_EMAIL", "")
EBAY_APP_ID       = os.environ.get("EBAY_APP_ID", "")
SHEET_TAB         = "Sheet1"  # fallback, auto-detected at runtime
STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_ANNUAL_PRICE_ID = os.environ.get("STRIPE_ANNUAL_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
stripe.api_key = STRIPE_SECRET_KEY

GOOGLE_CLIENT_ID     = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_OAUTH_SCOPES  = [
    "https://www.googleapis.com/auth/spreadsheets",
]
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://cardscan.live")
GMAIL_USER         = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# RevenueCat -- REVENUECAT_SECRET_KEY is the server-side "Secret API Key" (sk_...) used to
# verify subscriber status; REVENUECAT_WEBHOOK_SECRET is the Authorization header value you
# set when configuring the webhook in the RevenueCat dashboard, so we can trust its calls.
REVENUECAT_SECRET_KEY     = os.environ.get("REVENUECAT_SECRET_KEY", "")
REVENUECAT_WEBHOOK_SECRET = os.environ.get("REVENUECAT_WEBHOOK_SECRET", "")
# ───────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "slabscan-dev-secret")
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024  # 20MB max upload

# ── User-uploaded files (marketplace photos, profile pics) ──────────────────
# Railway's filesystem is ephemeral -- anything written to local disk is wiped
# on every redeploy. If a Railway Volume is attached to this service, Railway
# sets RAILWAY_VOLUME_MOUNT_PATH automatically and we store uploads there so
# they survive deploys. Falls back to a local folder for local dev, where
# that's not an issue since nothing redeploys out from under you.
UPLOAD_ROOT = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.join(app.root_path, "uploads")


@app.route('/uploads/<path:subpath>')
def serve_upload(subpath):
    return send_from_directory(UPLOAD_ROOT, subpath)

# ── Email ────────────────────────────────────────────────────────────────────
import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta, timezone

def send_reset_email(to_email, reset_url):
    msg = MIMEMultipart('alternative')
    msg['Subject'] = 'CardScan — Reset Your Password'
    msg['From'] = f'CardScan <{GMAIL_USER}>'
    msg['To'] = to_email
    body = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0a0a0a;color:#fff;">
      <h2 style="font-size:22px;font-weight:800;margin-bottom:8px;">Card<span style="color:#00e676;">Scan</span></h2>
      <p style="color:#aaa;margin-bottom:24px;">Password reset request</p>
      <p style="color:#ccc;margin-bottom:24px;">Click the button below to reset your password. This link expires in <strong style="color:#fff;">1 hour</strong>.</p>
      <a href="{reset_url}" style="display:inline-block;background:#00e676;color:#000;font-weight:800;padding:14px 28px;border-radius:10px;text-decoration:none;font-size:15px;">Reset Password</a>
      <p style="color:#666;font-size:12px;margin-top:32px;">If you didn't request this, you can safely ignore this email. Your password won't change.</p>
    </div>
    """
    msg.attach(MIMEText(body, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, to_email, msg.as_string())

# ── Rate limiting ────────────────────────────────────────────────────────────
_login_attempts = defaultdict(list)  # ip -> [timestamps]

def check_rate_limit(ip, max_attempts=10, window=300):
    """Allow max_attempts per window (seconds). Returns True if blocked."""
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < window]
    _login_attempts[ip] = attempts
    if len(attempts) >= max_attempts:
        return True
    _login_attempts[ip].append(now)
    return False
# ─────────────────────────────────────────────────────────────────────────────

init_db()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        # Validate session token if present
        token = session.get('session_token')
        if token and not validate_session(session['user_id'], token):
            session.clear()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def check_admin(secret):
    """Validate admin secret — must match env var and be non-empty."""
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    return admin_secret and secret == admin_secret and len(admin_secret) >= 8

# Shared camera instance
camera = None
camera_lock = threading.Lock()

def get_camera():
    global camera
    if camera is None or not camera.isOpened():
        camera = cv2.VideoCapture(0)
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        time.sleep(1)
    return camera

def generate_frames():
    while True:
        with camera_lock:
            cap = get_camera()
            ret, frame = cap.read()
        if ret:
            _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buf.tobytes() + b'\r\n')
        time.sleep(0.03)

_FALLBACK_MODEL = "gemini-2.5-flash"

def gemini_generate(client, model, contents, retries=3):
    """Call Gemini with exponential backoff, key rotation, and model fallback on overload."""
    import time as _time
    last_err = None
    for attempt in range(retries + 1):
        # After first retry, fall back to the lighter model
        active_model = _FALLBACK_MODEL if attempt >= 2 else model
        # Rotate API key on each retry
        active_key = _get_next_gemini_key()
        active_client = genai.Client(api_key=active_key)
        try:
            return active_client.models.generate_content(
                model=active_model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    http_options=genai_types.HttpOptions(timeout=25000)
                )
            )
        except Exception as e:
            last_err = e
            err_str = str(e)
            is_overload = any(x in err_str for x in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "quota"))
            if attempt < retries and is_overload:
                wait = min(2 ** attempt, 8)
                _time.sleep(wait)
                continue
            raise
    raise last_err

def analyze_label(image_data):
    """Second pass focused specifically on reading PSA/BGS/SGC label text."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This is a graded sports trading card in a PSA, BGS, SGC, or CGC slab. "
        "Zoom in mentally on the grading label sticker and read EVERY word carefully. "
        "PSA labels typically show: YEAR BRAND SET PLAYER NAME PARALLEL/VARIATION CARD# GEM MT GRADE CERT#\n\n"
        "Example PSA label text: '2020 Panini Prizm Silver Jordan Love #306 GEM MT 10 Cert# 12345678'\n\n"
        "Common brands on PSA labels: Panini, Topps, Upper Deck, Bowman, Donruss, Select, Mosaic, Optic\n"
        "Common sets: Prizm, Chrome, Select, Mosaic, Optic, Donruss, Contenders, Bowman, Heritage\n"
        "Common parallels: Silver, Gold, Red, Blue, Green, Purple, Orange, Pink, Holo, Refractor, Shimmer\n\n"
        "Read the CERT NUMBER carefully — it is a 7-9 digit number on the label.\n"
        "Read the YEAR carefully — it is a 4-digit number like 2018, 2019, 2020, 2021, 2022, 2023, 2024.\n\n"
        "Return ONLY valid JSON with these keys (null if truly unreadable):\n"
        "  name     - player full name from label\n"
        "  year     - 4-digit year\n"
        "  brand    - manufacturer e.g. 'Panini', 'Topps'\n"
        "  set      - set name e.g. 'Prizm', 'Chrome'\n"
        "  parallel - parallel/variation e.g. 'Silver', 'Gold Refractor'\n"
        "  grade    - full grade e.g. 'PSA 10', 'BGS 9.5'\n"
        "  cert     - cert number digits only e.g. '12345678'\n"
        "  card     - full description: 'YEAR BRAND SET PLAYER PARALLEL GRADE'\n"
        "Return ONLY the JSON object — no markdown, no code fences."
    )
    response = gemini_generate(client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())

def analyze_card(frame, quality=85, year_hint=None, sport_hint=None, is_raw=True):
    client = genai.Client(api_key=GEMINI_API_KEY)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    image_data = buf.tobytes()
    rookie_hint = get_rookie_hint(year_hint, sport_hint) if is_raw else None
    prompt = (
        "Identify this trading card by reading the PRINTED TEXT on it. "
        "Do not guess the player name from the photo — read the name that is printed on the card.\n\n"
        "Return ONLY a valid JSON object with these keys (null if not visible):\n"
        "  card_type   - 'sports' or 'tcg'\n"
        "  name        - player name exactly as printed (read the text, ignore the photo)\n"
        "  year        - integer year ONLY if you can read the copyright line e.g. '© 2024-25' = 2025. null if not visible — do NOT guess from the card design or player.\n"
        "  brand       - 'Panini', 'Topps', 'Upper Deck', etc\n"
        "  set         - set name e.g. 'Prizm', 'Chrome', 'Select', 'Mosaic', 'Obsidian', 'Silhouette'\n"
        "  parallel    - color/finish only e.g. 'Silver', 'Gold', 'Aqua'. null for base. Do NOT add a print run unless you can see a stamped number on the card.\n"
        "  serial      - ONLY if a stamped number like '089/299' or '12/25' is physically visible on the card, return the print run as '/299'. null if no stamp is visible — do NOT infer from parallel color.\n"
        "  grade       - 'PSA 10', 'BGS 9.5', 'CGC 10' if in slab, else 'Raw'\n"
        "  cert        - cert number from grading label, null if raw\n"
        "  rarity      - TCG rarity only e.g. 'Rare Holo', null for sports\n"
        "  card_number - TCG set number e.g. '4/102', null for sports\n"
        "  hp          - TCG HP integer, null for sports\n"
        "  card        - full description e.g. '2022 Topps Chrome Luther Burden III Aqua /299'\n\n"
        "Brand/set guide:\n"
        "  Panini sets: Prizm, Select, Donruss, Mosaic, Optic, Obsidian, Silhouette, Contenders\n"
        "  Topps sets: Chrome, Finest, Heritage, Bowman, Stadium Club, Series 1, Series 2\n"
        "  Upper Deck sets: SP Authentic, Exquisite, Young Guns\n"
        "Parallel colors by set (color only — do NOT use these to infer print runs):\n"
        "  Prizm: Silver, Gold, Red, Blue, Green, Purple, Orange, Pink, Rainbow, Hyper, Disco, Cracked Ice\n"
        "  Chrome: Refractor, Gold, Orange, Red, Pink, Purple, Blue, Atomic, Prism\n"
        "  Select: Silver, Gold, Tie-Dye, Blue, Red, White Sparkle, Courtside\n"
        "  Mosaic: Silver, Gold, Pink, Blue, Green, Red, Reactive Blue, Reactive Yellow\n"
        "Return ONLY the JSON object — no markdown, no code fences, no extra text."
    )
    if rookie_hint:
        prompt += f"\n\nROOKIE CARD REFERENCE — if you see an RC symbol or 'Rookie Card' text, match the player name against this list:\n{rookie_hint}"
    response = gemini_generate(
        client, model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_card_back(image_data, year_hint=None, sport_hint=None):
    """Read the back of a raw card for details the front scan may have missed."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This is the BACK of a raw (ungraded) sports or trading card. "
        "Read every line of text carefully and extract the following details:\n\n"
        "WHERE TO LOOK:\n"
        "  YEAR        — Copyright line, usually at the very bottom: '© 2021 Panini' or '2022 Topps'. "
        "                Return just the 4-digit number.\n"
        "  CARD NUMBER — Printed clearly on the back, often '# 301', 'Card No. 301', or just '301' near "
        "                the bottom. Do NOT include set print run totals.\n"
        "  BRAND       — Company name in the copyright line or logo on the back.\n"
        "  SET         — Set/product name if printed (e.g. 'Prizm', 'Chrome', 'Select').\n"
        "  NAME        — Player's full name from the bio or stats header.\n"
        "  TEAM        — Player's team name.\n"
        "  ROOKIE      — true if 'RC', 'Rookie', or 'Rookie Card' appears anywhere on the back.\n"
        "  SERIAL      — If the card is numbered (e.g. '045/199'), return the print run as a string "
        "                like '/199'. Null if not numbered.\n\n"
        "Return ONLY valid JSON with these exact keys (null if not found):\n"
        "  year, card_number, brand, set, name, team, rookie, serial\n"
        "Return ONLY the JSON object — no markdown, no code fences, no extra text."
    )
    rookie_hint = get_rookie_hint(year_hint, sport_hint)
    if rookie_hint:
        prompt += f"\n\nROOKIE CARD REFERENCE — if you see an RC symbol, match the player name against this list:\n{rookie_hint}"
    response = gemini_generate(
        client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def extract_year_from_copyright(image_data):
    """Crop the bottom 12% of the card image and run a focused year-only read."""
    import numpy as np
    client = genai.Client(api_key=GEMINI_API_KEY)
    # Decode, crop bottom strip, re-encode
    arr = np.frombuffer(image_data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    h = img.shape[0]
    strip = img[int(h * 0.88):, :]   # bottom 12%
    _, buf = cv2.imencode(".jpg", strip, [cv2.IMWRITE_JPEG_QUALITY, 95])
    strip_bytes = buf.tobytes()
    prompt = (
        "This is a cropped strip from the very bottom of a sports trading card. "
        "It contains the copyright line, e.g. '© 2021 Panini America' or '2022 Topps'. "
        "Read the 4-digit year from this copyright text. "
        "Return ONLY a JSON object with one key: {\"year\": 2021} or {\"year\": null} if unreadable. "
        "No markdown, no extra text."
    )
    try:
        response = gemini_generate(
            client, model="gemini-2.5-flash",
            contents=[prompt, genai_types.Part.from_bytes(data=strip_bytes, mime_type="image/jpeg")],
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip()).get("year")
    except Exception:
        return None


def analyze_raw_card(image_data, year_hint=None, sport_hint=None):
    """Second pass for raw (ungraded) cards — focused on fine-print details
    that the general first pass tends to miss: year, set, parallel, card number."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This is a raw (ungraded) sports or trading card. "
        "Your ONLY job is to read the fine-print details that are easy to miss. "
        "Study every millimeter of text carefully.\n\n"

        "WHERE TO FIND EACH FIELD:\n"
        "  YEAR  — Look at the very bottom of the card for a copyright line like '© 2021 Panini America' "
        "or '2022 Topps'. It is usually tiny. Return ONLY the 4-digit number. "
        "CRITICAL: Do NOT use any knowledge about when a player played in college, was drafted, or their career history. "
        "Do NOT guess from the card design, player age, set style, or anything other than the physical copyright text printed on the card. "
        "If the copyright says 2025 but the player looks young, still return 2025. "
        "If you cannot clearly read the copyright text, return null.\n"
        "  BRAND — Read the manufacturer name from the logo or copyright line. "
        "Topps and Panini are different companies. "
        "Topps sets include: Chrome, Finest, Heritage, Bowman, Stadium Club, Series 1/2. "
        "Panini sets include: Prizm, Select, Mosaic, Optic, Donruss, Contenders, Obsidian, Chronicles.\n"
        "  SET   — The product/set name, e.g. 'Prizm', 'Chrome', 'Select', 'Mosaic', 'Optic', 'Bowman'.\n"
        "  PARALLEL — Look ONLY at the card border color. Return the color name only (e.g. 'Gold', 'Red', 'Blue', 'Green', 'Purple', 'Orange', 'Pink', 'Black', 'White').\n"
        "    If the border has a clearly visible solid color → return that color (e.g. 'Gold', 'Red').\n"
        "    If the card has no colored border — just silver foil, rainbow shimmer, or the standard base finish → return null.\n"
        "    Do NOT return 'Silver', 'Refractor', 'Base', 'Rainbow' or any product name — COLORS ONLY or null.\n"
        "  NUMBERED CARDS — Look for a physically stamped or foil-printed number like '045/099' or '12/25' on the card face.\n"
        "    ONLY if you can actually see this stamp: serial = '/99' (print run only). Do NOT guess from parallel color.\n"
        "    The first number (e.g. 045) is which copy this is — ignore it.\n"
        "    Do NOT put this stamp in card_number.\n"
        "  CARD NUMBER — A plain card number like '#301' printed in a corner (not the serial stamp).\n"
        "  PLAYER/CARD NAME — The large name printed on the front of the card.\n"
        "  SPORT — Basketball, Football, Baseball, Hockey, Soccer etc.\n\n"

        "Return ONLY valid JSON with these keys (null if truly cannot determine):\n"
        "  name        - player full name\n"
        "  year        - 4-digit year as integer\n"
        "  brand       - manufacturer\n"
        "  set         - set/product name\n"
        "  parallel    - color/finish only e.g. 'Gold', 'Green', 'Silver'. Do NOT add a print run unless a stamped number is physically visible on the card.\n"
        "  serial      - print run only e.g. '/99', '/10'. null if not numbered\n"
        "  card_number - plain card number e.g. '301' (NOT the serial stamp) or null\n"
        "  sport       - sport name or null\n"
        "Return ONLY the JSON object — no markdown, no code fences, no extra text."
    )
    response = gemini_generate(
        client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def analyze_bulk_bbox(image_data, num_cards):
    """Second pass — get bounding boxes for each card in a bulk image."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        f"This photo contains {num_cards} sports cards.\n"
        "For EACH card, return its bounding box as [x, y, w, h] fractions (0.0-1.0) of the image.\n"
        "x,y = top-left corner, w,h = full width/height. Cover the ENTIRE card including borders.\n"
        "List cards LEFT TO RIGHT, TOP TO BOTTOM.\n"
        "Return ONLY a JSON array of bbox arrays e.g. [[0.01,0.05,0.30,0.88],[0.35,0.05,0.30,0.88]]\n"
        "No markdown, no extra text."
    )
    try:
        response = gemini_generate(client,
            model="gemini-2.5-flash",
            contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"): text = text[4:]
        return json.loads(text.strip())
    except Exception:
        return []


def analyze_bulk(image_data):
    """Detect multiple cards — fast identification only."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This photo contains multiple sports cards laid out flat. Identify every visible card.\n\n"
        "RULES:\n"
        "- Include a card only if you can read the player name from printed text on the card\n"
        "- You may identify up to 15 cards per image — scan the entire image systematically left-to-right, top-to-bottom\n"
        "- Do NOT skip cards just because they are partially overlapping — include any card where the name is readable\n"
        "- Do NOT include blurry/unreadable cards, plain card backs, or backgrounds\n"
        "- If only one card is visible, return an array with one element\n"
        "- If no cards meet the criteria, return []\n\n"
        "FOR EACH CARD READ:\n"
        "- name: player name exactly as printed on the card (required)\n"
        "- year: 4-digit year from the copyright line at the bottom of the card (e.g. '© 2022 Panini' = 2022). "
        "Read it carefully — look for tiny text at the very bottom edge. null only if completely unreadable.\n"
        "- brand: Panini / Topps / Upper Deck / Bowman etc from logo or copyright\n"
        "- set: product name e.g. Prizm, Chrome, Select, Mosaic, Optic, Donruss, Bowman\n"
        "- parallel: color finish e.g. Silver, Gold, Blue, Green. null for plain base\n"
        "- grade: 'Raw' for ungraded, or 'PSA 10' / 'BGS 9.5' etc for slabs\n"
        "- cert: cert number from grading label, null if raw\n"
        "- card: short description e.g. '2022 Panini Prizm Patrick Mahomes Silver'\n\n"
        "Return ONLY a valid JSON array. No markdown, no code fences, no extra text."
    )
    response = gemini_generate(client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    cards = json.loads(text.strip())
    # Filter out any cards without a real player name (hallucinations)
    JUNK_NAMES = {'blurry', 'unknown', 'unreadable', 'football card', 'basketball card',
                  'baseball card', 'card', 'sports card', 'n/a', 'none', ''}
    filtered = [
        c for c in cards
        if isinstance(c, dict)
        and c.get('name')
        and str(c.get('name', '')).strip().lower() not in JUNK_NAMES
        and len(str(c.get('name', '')).strip()) > 2
    ]
    return filtered


def analyze_prices(image_data):
    """Read price tags from the back of cards and return a list of price strings."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This photo shows the backs of sports card slabs or cards with price stickers or handwritten prices. "
        "Read EVERY price visible. Return ONLY a valid JSON array of price strings in the order they appear "
        "LEFT TO RIGHT, TOP TO BOTTOM in the image. "
        "Format each price as a dollar string e.g. '$25.00', '$150', '$4.99'. "
        "If no price is visible for a position use null. "
        "Return ONLY the JSON array — no markdown, no code fences, no explanation."
    )
    response = gemini_generate(client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def get_creds():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    b64 = os.environ.get("GOOGLE_CREDS_B64", "")
    if b64:
        import json, tempfile
        b64 += "==" # fix padding
        creds_json = base64.b64decode(b64).decode("utf-8")
        creds_dict = json.loads(creds_json)
        from google.oauth2.service_account import Credentials as SACredentials
        return SACredentials.from_service_account_info(creds_dict, scopes=scopes)
    return Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)

def extract_sheet_id(sheet_url_or_id):
    """Accept a full Google Sheets URL or raw ID and return just the ID."""
    import re
    match = re.search(r'/spreadsheets/d/([a-zA-Z0-9_-]+)', sheet_url_or_id)
    if match:
        return match.group(1)
    return sheet_url_or_id.strip()

# Keyword map: field -> (exact_words, substring_words)
# exact_words  — must match the full header word-for-word (e.g. "set" won't match "asset")
# substring_kw — safe substrings that are unambiguous regardless of surrounding text
FIELD_KEYWORDS = {
    "card":     (["card", "description", "listing", "title", "full name", "full card"],
                 ["card desc", "card title"]),
    "name":     (["name", "player", "athlete", "player name"],
                 ["player"]),
    "year":     (["year", "yr", "season"],
                 ["year"]),
    "brand":    (["brand", "manufacturer", "company", "make"],
                 ["brand", "manuf"]),
    "set":      (["set", "set name", "product", "series", "product name"],
                 ["set name", "product"]),
    "parallel": (["parallel", "variant", "variation", "color", "finish", "refractor"],
                 ["parallel", "variant"]),
    "serial":   (["serial", "print run", "numbered", "serial #", "serial number", "#/", "print"],
                 ["serial", "print run", "numbered"]),
    "grade":    (["grade", "condition", "psa", "bgs", "sgc", "cgc", "slab", "graded"],
                 ["grade", "psa", "bgs", "sgc"]),
    "cert":     (["cert", "cert #", "cert number", "certification", "slab #", "cert no"],
                 ["cert#", "certno"]),
    "sport":    (["sport", "league", "category"],
                 ["sport"]),
    "team":     (["team", "franchise"],
                 ["team"]),
    "card_number": (["card #", "card number", "card no", "#"],
                    ["card#", "cardno"]),
    "value":    (["value", "ebay avg", "market value", "est value", "worth", "ebay value", "current value"],
                 ["ebay", "market val", "est. val"]),
    "paid":     (["paid", "cost", "bought for", "purchase price", "buy price", "my cost"],
                 ["paid", "cost"]),
    "notes":    (["notes", "note", "memo", "comments", "comment"],
                 ["notes"]),
    "tracking": (["tracking", "tracking #", "ship", "shipment"],
                 ["tracking"]),
}

import re as _re

def _header_matches(header_raw, exact_words, substring_kw):
    """Return True if header matches any exact word or safe substring keyword."""
    h = header_raw.lower().strip()
    # Exact whole-header match first
    if h in exact_words:
        return True
    # Word-boundary match for each exact word (so "set" ≠ "asset")
    for kw in exact_words:
        pattern = r'(?<![a-z])' + _re.escape(kw) + r'(?![a-z])'
        if _re.search(pattern, h):
            return True
    # Safe substring match
    for kw in substring_kw:
        if kw in h:
            return True
    return False

def detect_column_mapping(headers):
    """Map field names to column indices based on header keywords.
    Uses word-boundary matching so 'set' won't match 'asset' or 'reset'."""
    mapping = {}
    for col_idx, header in enumerate(headers):
        for field, (exact_words, substring_kw) in FIELD_KEYWORDS.items():
            if field not in mapping and _header_matches(header, exact_words, substring_kw):
                mapping[field] = col_idx
    return mapping

def build_row(data, mapping, num_cols):
    """Build a row array aligned to the sheet's existing columns."""
    ebay_avg = data.get("ebay_avg")
    grade = data.get("grade") or ""
    is_raw = grade.lower() == "raw" or not grade
    values = {
        "card":        data.get("card")        or "",
        "name":        data.get("name")        or "",
        "year":        str(data.get("year")    or ""),
        "brand":       data.get("brand")       or "",
        "set":         data.get("set")         or "",
        "parallel":    data.get("parallel")    or "",
        "serial":      data.get("serial")      or "",
        "grade":       grade,
        "cert":        data.get("cert")        or ("Raw" if is_raw else ""),
        "sport":       data.get("sport")       or "",
        "team":        data.get("team")        or "",
        "card_number": data.get("card_number") or "",
        "value":       f"${ebay_avg:.2f}"      if ebay_avg else "",
        "paid":        data.get("paid")        or "",
        "notes":       data.get("notes")       or "",
        "tracking":    "",
    }
    row = [""] * num_cols
    for field, col_idx in mapping.items():
        if col_idx < num_cols and field in values:
            row[col_idx] = values[field]
    return row

def get_all_sheet_tabs(sheet_id, svc):
    """Return list of all tab names in the spreadsheet."""
    try:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        return [s["properties"]["title"] for s in meta.get("sheets", [])]
    except Exception:
        return []

def get_first_sheet_tab(sheet_id, svc, preferred_tab=None):
    """Get the tab to write to — preferred tab if set, else first tab."""
    tabs = get_all_sheet_tabs(sheet_id, svc)
    if preferred_tab and preferred_tab in tabs:
        return preferred_tab
    return tabs[0] if tabs else SHEET_TAB

def get_sheet_headers(sheet_id, svc):
    """Read the first row of the sheet to detect headers."""
    tab = get_first_sheet_tab(sheet_id, svc)
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!1:1"
        ).execute()
        rows = result.get("values", [])
        return rows[0] if rows else []
    except Exception:
        return []

def get_user_sheets_service(user):
    """Build a Sheets service using the user's OAuth tokens if available, else service account."""
    if user and user.get("google_access_token"):
        creds = OAuthCredentials(
            token=user["google_access_token"],
            refresh_token=user["google_refresh_token"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            scopes=GOOGLE_OAUTH_SCOPES,
        )
        # Refresh if expired — access tokens expire after ~1 hour
        if creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
                creds.refresh(Request())
                # Save the new access token so next request doesn't need to refresh again
                if user.get("id"):
                    try:
                        conn = get_db()
                        cur = conn.cursor()
                        cur.execute("UPDATE users SET google_access_token = %s WHERE id = %s",
                                    (creds.token, user["id"]))
                        conn.commit(); cur.close(); conn.close()
                    except Exception:
                        pass
            except Exception as refresh_err:
                app.logger.warning(f"Token refresh failed for user {user.get('id')}: {refresh_err}")
                # Fall through to service account
                return build("sheets", "v4", credentials=get_creds())
        return build("sheets", "v4", credentials=creds)
    return build("sheets", "v4", credentials=get_creds())

def append_to_sheet(data, custom_sheet_id=None, user=None):
    user = user or {}

    # Use user's saved sheet, then custom passed in, then fallback
    sheet_id = (
        custom_sheet_id
        or user.get("google_sheet_id")
        or SPREADSHEET_ID
    )
    if not sheet_id:
        raise Exception("No Google Sheet connected. Tap 'Connect Sheets' in the menu to set one up.")

    try:
        svc = get_user_sheets_service(user)
    except Exception as e:
        raise Exception(f"Could not connect to Google Sheets: {str(e)}")

    try:
        preferred_tab = user.get("sheet_tab") if user else None
        tab = get_first_sheet_tab(sheet_id, svc, preferred_tab=preferred_tab)
        headers = get_sheet_headers(sheet_id, svc)
    except Exception as e:
        err = str(e)
        if "403" in err or "permission" in err.lower():
            if user and user.get("google_access_token"):
                raise Exception("Sheet permission denied — your Google session may have expired. Go to Settings → Reconnect Google Sheets to fix this.")
            else:
                raise Exception("Sheet permission denied — share your sheet with card-scanner@lithe-grid-498217-i6.iam.gserviceaccount.com as Editor.")
        if "404" in err:
            raise Exception("Sheet not found — check that the Google Sheet URL is correct.")
        raise Exception(f"Could not read sheet: {err}")

    ebay_avg = data.get("ebay_avg")
    value_str = f"${ebay_avg:.2f}" if ebay_avg else ""

    # Default row order used when no headers or insufficient column matches
    default_row = [
        data.get("card")  or "",
        data.get("name")  or "",
        str(data.get("year") or ""),
        data.get("brand") or "",
        data.get("set")   or "",
        data.get("parallel") or "",
        data.get("grade") or "",
        data.get("cert")  or "",
        value_str,
        data.get("paid")  or "",
    ]

    if headers:
        mapping = detect_column_mapping(headers)
        if mapping:
            # Use column mapping whenever at least 1 header was recognized —
            # unrecognized columns simply stay blank rather than misaligning data
            row = [build_row(data, mapping, len(headers))]
        else:
            # No headers matched at all — fall back to default column order
            row = [default_row[:len(headers)] + [""] * max(0, len(headers) - len(default_row))]
    else:
        row = [default_row]

    try:
        svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": row},
        ).execute()
    except Exception as e:
        err = str(e)
        if "403" in err or "permission" in err.lower():
            if user and user.get("google_access_token"):
                raise Exception("Sheet permission denied — your Google session may have expired. Go to Settings → Reconnect Google Sheets to fix this.")
            else:
                raise Exception("Sheet permission denied — share your sheet with card-scanner@lithe-grid-498217-i6.iam.gserviceaccount.com as Editor.")
        if "404" in err:
            raise Exception("Sheet not found — check that the Google Sheet URL is correct.")
        raise Exception(f"Could not write to sheet: {err}")

def lookup_psa_cert(cert_number):
    """Fetch PSA cert info from PSA's public cert verification page."""
    from bs4 import BeautifulSoup
    cert = cert_number.replace(" ", "").strip()
    url = f"https://www.psacard.com/cert/{cert}"
    try:
        resp = requests.get(url, headers=_EBAY_HEADERS, timeout=8)
        if not resp.ok:
            return None, f"PSA returned {resp.status_code}"
        soup = BeautifulSoup(resp.text, "lxml")
        result = {}
        # PSA cert page has labeled fields
        for row in soup.select("tr, .cert-row, [class*='cert']"):
            text = row.get_text(" ", strip=True)
            for label, key in [("Grade", "grade"), ("Subject", "subject"),
                                ("Year", "year"), ("Brand", "brand"),
                                ("Card Number", "card_number"), ("Variety", "variety")]:
                if label in text:
                    parts = text.split(label, 1)
                    if len(parts) > 1:
                        result[key] = parts[1].strip().split()[0] if parts[1].strip() else None
        result["cert_url"] = url
        return result if result else None, None
    except Exception as e:
        return None, str(e)


@app.route('/sheet/headers', methods=['POST'])
def sheet_headers():
    body = request.get_json()
    sheet_url = body.get("sheet_id", "")
    if not sheet_url:
        return jsonify({"success": False, "error": "No sheet URL provided"})
    sheet_id = extract_sheet_id(sheet_url)
    try:
        creds = get_creds()
        svc   = build("sheets", "v4", credentials=creds)
        headers = get_sheet_headers(sheet_id, svc)
        mapping = detect_column_mapping(headers)
        return jsonify({"success": True, "headers": headers, "mapping": mapping})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


@app.route('/psa', methods=['POST'])
def psa_lookup():
    body = request.get_json()
    cert = body.get("cert", "").strip()
    if not cert:
        return jsonify({"success": False, "error": "No cert number provided"})
    result, err = lookup_psa_cert(cert)
    if err:
        return jsonify({"success": False, "error": err})
    return jsonify({"success": True, "psa": result, "cert_url": f"https://www.psacard.com/cert/{cert}"})


@app.route('/')
def index():
    if 'user_id' not in session:
        return render_template('landing.html')
    user = get_user_by_id(session['user_id'])
    # Build full sheet URL from saved sheet_id if available
    saved_sheet_id = user.get('google_sheet_id') if user else None
    saved_sheet_url = f"https://docs.google.com/spreadsheets/d/{saved_sheet_id}" if saved_sheet_id else ""
    return render_template('index.html', user=user, saved_sheet_url=saved_sheet_url)

@app.route('/home')
def landing():
    return render_template('landing.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        if check_rate_limit(ip, max_attempts=10, window=300):
            return render_template('login.html', error='Too many attempts. Please wait 5 minutes.', mode='login')
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        user = get_user_by_email(email)
        if user and check_password_hash(user['password_hash'], password):
            import secrets
            token = secrets.token_hex(32)
            create_session(user['id'], token)
            session['user_id'] = user['id']
            session['session_token'] = token
            remember = request.form.get('remember_me') == '1'
            if remember:
                session.permanent = True
                app.permanent_session_lifetime = timedelta(days=30)
            return redirect(url_for('index'))
        return render_template('login.html', error='Invalid email or password', mode='login')
    return render_template('login.html', mode='login')

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
        if check_rate_limit(ip, max_attempts=5, window=300):
            return render_template('login.html', error='Too many attempts. Please wait 5 minutes.', mode='signup')
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not email or not password or len(password) < 6:
            return render_template('login.html', error='Please enter a valid email and password (min 6 chars)', mode='signup')
        ref_code = request.form.get('ref', '').strip().upper()
        user = create_user(email, generate_password_hash(password))
        if not user:
            return render_template('login.html', error='An account with that email already exists', mode='signup')

        # Generate unique referral code for this user
        user_ref_code = email.split('@')[0].upper()[:6] + str(user['id'])
        _db_set_referral_code(user['id'], user_ref_code)

        # Apply referral bonus if valid code used
        if ref_code:
            referrer = _db_get_user_by_referral_code(ref_code)
            if referrer and referrer['id'] != user['id']:
                _db_apply_referral(user['id'], referrer['id'], ref_code)

        import secrets
        token = secrets.token_hex(32)
        create_session(user['id'], token)
        session['user_id'] = user['id']
        session['session_token'] = token
        return redirect(url_for('index'))
    ref = request.args.get('ref', '')
    return render_template('login.html', mode='signup', ref=ref)

@app.route('/logout')
def logout():
    token = session.get('session_token')
    if token:
        delete_session(token)
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin/test-email/<secret>')
def admin_test_email(secret):
    if not check_admin(secret):
        return "Forbidden", 403
    if not GMAIL_USER or not GMAIL_APP_PASSWORD:
        return f"❌ Missing env vars — GMAIL_USER='{GMAIL_USER}' GMAIL_APP_PASSWORD={'set' if GMAIL_APP_PASSWORD else 'NOT SET'}"
    try:
        send_reset_email(GMAIL_USER, "https://cardscan.live/test")
        return f"✅ Test email sent to {GMAIL_USER} — check your inbox"
    except Exception as e:
        return f"❌ Email failed: {str(e)}"

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'GET':
        return render_template('forgot_password.html')
    try:
        email = request.form.get('email', '').strip().lower()
        user = get_user_by_email(email)
        # Always show success message to avoid user enumeration
        if user:
            token = secrets.token_urlsafe(32)
            expires_at = datetime.utcnow() + timedelta(hours=1)
            try:
                save_reset_token(email, token, expires_at)
            except Exception:
                pass
            reset_url = f"{APP_BASE_URL}/reset-password/{token}"
            try:
                send_reset_email(email, reset_url)
            except Exception:
                return render_template('forgot_password.html', error='Could not send email. Please try again or contact us on Instagram.')
        return render_template('forgot_password.html', success=True)
    except Exception as e:
        return render_template('forgot_password.html', error='Something went wrong. Please try again.')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    row = get_reset_token(token)
    if not row:
        return render_template('reset_password.html', error='This reset link is invalid or has already been used.')
    # Check expiry
    if DATABASE_URL:
        from datetime import timezone
        expires_at = row['expires_at']
        if expires_at.tzinfo:
            now = datetime.now(timezone.utc)
        else:
            now = datetime.utcnow()
        if now > expires_at:
            delete_reset_token(token)
            return render_template('reset_password.html', error='This reset link has expired. Please request a new one.')
    else:
        expires_at = datetime.fromisoformat(str(row['expires_at']))
        if datetime.utcnow() > expires_at:
            delete_reset_token(token)
            return render_template('reset_password.html', error='This reset link has expired. Please request a new one.')

    if request.method == 'GET':
        return render_template('reset_password.html', token=token)

    password = request.form.get('password', '')
    confirm = request.form.get('confirm', '')
    if len(password) < 6:
        return render_template('reset_password.html', token=token, error='Password must be at least 6 characters.')
    if password != confirm:
        return render_template('reset_password.html', token=token, error='Passwords do not match.')

    from werkzeug.security import generate_password_hash
    update_password(row['email'], generate_password_hash(password))
    delete_reset_token(token)
    return render_template('reset_password.html', success=True)

# ── Google OAuth ─────────────────────────────────────────────────────────────

def make_oauth_flow():
    return Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{APP_BASE_URL}/oauth/callback"],
            }
        },
        scopes=GOOGLE_OAUTH_SCOPES,
        redirect_uri=f"{APP_BASE_URL}/oauth/callback",
    )

@app.route('/connect-sheets')
@login_required
def connect_sheets():
    flow = make_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )
    session["oauth_state"] = state
    return redirect(auth_url)

@app.route('/oauth/callback')
@login_required
def oauth_callback():
    flow = make_oauth_flow()
    try:
        flow.fetch_token(authorization_response=request.url.replace("http://", "https://"))
        creds = flow.credentials
        save_google_tokens(session["user_id"], creds.token, creds.refresh_token)
        return redirect("/?sheets=connected")
    except Exception as e:
        return redirect(f"/?sheets=error&msg={str(e)}")

def _db_set_referral_code(user_id, code):
    from database import get_db, DATABASE_URL
    db = get_db()
    try:
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE users SET referral_code = %s WHERE id = %s", (code, user_id))
            db.commit(); cur.close()
        else:
            db.execute("UPDATE users SET referral_code = ? WHERE id = ?", (code, user_id)); db.commit()
    except: pass
    finally: db.close()

def _db_get_user_by_referral_code(code):
    from database import get_db, DATABASE_URL
    db = get_db()
    try:
        if DATABASE_URL:
            import psycopg2.extras
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("SELECT * FROM users WHERE referral_code = %s", (code,))
            row = cur.fetchone(); cur.close(); db.close()
            return dict(row) if row else None
        else:
            row = db.execute("SELECT * FROM users WHERE referral_code = ?", (code,)).fetchone()
            db.close()
            return dict(row) if row else None
    except: db.close(); return None

def _db_apply_referral(new_user_id, referrer_id, code):
    from database import get_db, DATABASE_URL
    db = get_db()
    try:
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE users SET referred_by = %s, bonus_scans = COALESCE(bonus_scans,0) + 20 WHERE id = %s", (code, new_user_id))
            cur.execute("UPDATE users SET bonus_scans = COALESCE(bonus_scans,0) + 20 WHERE id = %s", (referrer_id,))
            db.commit(); cur.close()
        else:
            db.execute("UPDATE users SET referred_by = ?, bonus_scans = COALESCE(bonus_scans,0) + 20 WHERE id = ?", (code, new_user_id)); db.commit()
            db.execute("UPDATE users SET bonus_scans = COALESCE(bonus_scans,0) + 20 WHERE id = ?", (referrer_id,)); db.commit()
    except: pass
    finally: db.close()

@app.route('/referral-info')
@login_required
def referral_info():
    user = get_user_by_id(session['user_id'])
    ref_code = user.get('referral_code') or ''
    ref_url = f"{APP_BASE_URL}/signup?ref={ref_code}" if ref_code else ''
    return jsonify({'code': ref_code, 'url': ref_url, 'bonus_scans': user.get('bonus_scans', 0)})

@app.route('/change-password', methods=['POST'])
@login_required
def change_password():
    body = request.get_json()
    current  = body.get('current', '')
    new_pw   = body.get('new_password', '')
    if not current or not new_pw or len(new_pw) < 6:
        return jsonify({'success': False, 'error': 'Invalid input'})
    user = get_user_by_id(session['user_id'])
    if not check_password_hash(user['password_hash'], current):
        return jsonify({'success': False, 'error': 'Current password is incorrect'})
    update_password(user['email'], generate_password_hash(new_pw))
    return jsonify({'success': True})

@app.route('/save-sheet-url', methods=['POST'])
@login_required
def save_sheet_url():
    """Save the user's sheet URL to their account so it persists across devices."""
    body = request.get_json()
    sheet_url = body.get('sheet_url', '').strip()
    sheet_id = extract_sheet_id(sheet_url) if sheet_url else None
    if not sheet_id:
        return jsonify({'success': False, 'error': 'Invalid sheet URL'})
    save_google_sheet_id(session['user_id'], sheet_id)
    return jsonify({'success': True, 'sheet_id': sheet_id})

@app.route('/toggle-auto-sheet', methods=['POST'])
@login_required
def toggle_auto_sheet():
    from database import get_db, DATABASE_URL
    body = request.get_json()
    enabled = bool(body.get('enabled', True))
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("UPDATE users SET auto_sheet = %s WHERE id = %s", (enabled, session['user_id']))
        conn.commit(); cur.close(); conn.close()
    else:
        conn.execute("UPDATE users SET auto_sheet = ? WHERE id = ?", (enabled, session['user_id'])); conn.commit(); conn.close()
    return jsonify({'success': True, 'auto_sheet': enabled})

@app.route('/disconnect-sheets', methods=['POST'])
@login_required
def disconnect_sheets():
    clear_google_tokens(session["user_id"])
    return jsonify({"success": True})

@app.route('/sheets-status')
@login_required
def sheets_status():
    user = get_user_by_id(session["user_id"])
    connected = bool(user and user.get("google_access_token"))
    sheet_id = user.get("google_sheet_id") if user else None
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else None
    return jsonify({"connected": connected, "sheet_url": sheet_url})

# ─────────────────────────────────────────────────────────────────────────────

@app.route('/create-checkout-session', methods=['POST'])
@login_required
def create_checkout_session():
    user = get_user_by_id(session['user_id'])
    try:
        # Create or reuse Stripe customer
        if not user['stripe_customer_id']:
            customer = stripe.Customer.create(email=user['email'])
            update_stripe_customer(user['id'], customer.id)
            customer_id = customer.id
        else:
            customer_id = user['stripe_customer_id']

        plan = request.get_json().get('plan', 'monthly') if request.get_json() else 'monthly'
        price_id = STRIPE_ANNUAL_PRICE_ID if plan == 'annual' else STRIPE_PRICE_ID

        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=request.host_url + 'account?success=1',
            cancel_url=request.host_url + 'account?cancelled=1',
            consent_collection={'terms_of_service': 'required'},
            custom_text={'terms_of_service_acceptance': {'message': 'I agree to the [Terms of Service](https://cardscan.live/terms).'}},
        )
        return jsonify({'url': checkout.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/create-portal-session', methods=['POST'])
@login_required
def create_portal_session():
    user = get_user_by_id(session['user_id'])
    if not user['stripe_customer_id']:
        return jsonify({'error': 'No subscription found'}), 400
    portal = stripe.billing_portal.Session.create(
        customer=user['stripe_customer_id'],
        return_url=request.host_url + 'account',
    )
    return redirect(portal.url)

@app.route('/webhook', methods=['POST'])
def webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        else:
            event = json.loads(payload)
    except Exception:
        return 'Invalid', 400

    if event['type'] in ('customer.subscription.created', 'customer.subscription.updated'):
        sub = event['data']['object']
        status = 'pro' if sub['status'] == 'active' else 'free'
        # Detect plan type from the price ID
        price_id = sub.get('items', {}).get('data', [{}])[0].get('price', {}).get('id', '')
        plan_type = 'annual' if price_id == STRIPE_ANNUAL_PRICE_ID else 'monthly'
        update_subscription(sub['customer'], status, plan_type)
    elif event['type'] == 'customer.subscription.deleted':
        update_subscription(event['data']['object']['customer'], 'free')

    return 'OK', 200

OWNER_EMAIL = "maddengarrett7@gmail.com"

@app.route('/admin/set-pro/<secret>')
def admin_set_pro(secret):
    """One-time route to set the owner account to Pro."""
    if not check_admin(secret):
        return "Forbidden", 403
    user = get_user_by_email(OWNER_EMAIL)
    if not user:
        return "User not found — please sign up first", 404
    from database import get_db
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            # Postgres
            cur = db.cursor()
            cur.execute("UPDATE users SET subscription_status = 'pro' WHERE email = %s", (OWNER_EMAIL,))
            db.commit()
            cur.close()
        else:
            # SQLite
            db.execute("UPDATE users SET subscription_status = 'pro' WHERE email = ?", (OWNER_EMAIL,))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {str(e)}", 500
    return f"✅ {OWNER_EMAIL} is now Pro!"

@app.route('/admin/set-free/<secret>')
def admin_set_free(secret):
    """One-time route to temporarily drop the owner account to free (e.g. to
    screenshot the paywall for App Store subscription review) -- flip back
    with /admin/set-pro/<secret> afterward."""
    if not check_admin(secret):
        return "Forbidden", 403
    user = get_user_by_email(OWNER_EMAIL)
    if not user:
        return "User not found — please sign up first", 404
    from database import get_db
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("UPDATE users SET subscription_status = 'free' WHERE email = %s", (OWNER_EMAIL,))
            db.commit()
            cur.close()
        else:
            db.execute("UPDATE users SET subscription_status = 'free' WHERE email = ?", (OWNER_EMAIL,))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {str(e)}", 500
    return f"✅ {OWNER_EMAIL} is now Free (remember to flip back to Pro after your screenshot)."

@app.route('/mission')
def mission():
    return render_template('mission.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

@app.route('/history')
@login_required
def history():
    page = int(request.args.get('page', 1))
    search = request.args.get('search', '').strip()
    grade_filter = request.args.get('grade', '').strip()
    limit = 20
    offset = (page - 1) * limit
    scans, total = get_scan_history(session['user_id'], limit=limit, offset=offset, search=search, grade_filter=grade_filter)
    total_pages = (total + limit - 1) // limit
    return render_template('history.html', scans=scans, total=total, page=page, total_pages=total_pages, search=search, grade_filter=grade_filter)


@app.route('/history/delete/<int:scan_id>', methods=['POST'])
@login_required
def delete_scan(scan_id):
    from database import get_db, DATABASE_URL
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("DELETE FROM scan_history WHERE id = %s AND user_id = %s", (scan_id, session['user_id']))
        conn.commit(); cur.close(); conn.close()
    else:
        conn.execute("DELETE FROM scan_history WHERE id = ? AND user_id = ?", (scan_id, session['user_id']))
        conn.commit(); conn.close()
    return jsonify({'success': True})


@app.route('/history/export')
@login_required
def export_history():
    import csv, io
    scans, _ = get_scan_history(session['user_id'], limit=10000, offset=0)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Card', 'Name', 'Year', 'Brand', 'Set', 'Parallel', 'Grade', 'Cert', 'eBay Avg', 'Scanned At'])
    for s in scans:
        writer.writerow([
            s.get('card',''), s.get('name',''), s.get('year',''),
            s.get('brand',''), s.get('set_name',''), s.get('parallel',''),
            s.get('grade',''), s.get('cert',''),
            f"${s['ebay_avg']:.2f}" if s.get('ebay_avg') else '',
            str(s.get('scanned_at',''))[:16]
        ])
    output.seek(0)
    from flask import make_response
    resp = make_response(output.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = 'attachment; filename=cardscan_history.csv'
    return resp


@app.route('/admin/grant-trial', methods=['POST'])
def admin_grant_trial():
    from database import get_db, DATABASE_URL
    secret = request.form.get('secret') or (request.get_json() or {}).get('secret')
    if not check_admin(secret):
        return jsonify({'success': False, 'error': 'Forbidden'}), 403
    email = request.form.get('email') or (request.get_json() or {}).get('email')
    days = int(request.form.get('days') or (request.get_json() or {}).get('days') or 7)
    if not email:
        return jsonify({'success': False, 'error': 'Email required'})
    from datetime import date, timedelta
    trial_end = str(date.today() + timedelta(days=days))
    conn = get_db()
    if DATABASE_URL:
        cur = conn.cursor()
        cur.execute("UPDATE users SET subscription_status='pro', trial_end=%s WHERE email=%s", (trial_end, email.lower()))
        conn.commit(); cur.close(); conn.close()
    else:
        conn.execute("UPDATE users SET subscription_status='pro', trial_end=? WHERE email=?", (trial_end, email.lower()))
        conn.commit(); conn.close()
    return jsonify({'success': True, 'message': f'{email} granted {days}-day Pro trial until {trial_end}'})

@app.route('/account')
@login_required
def account():
    user = get_user_by_id(session['user_id'])
    return render_template('account.html', user=user)

@app.route('/video')
def video():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

_EBAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

def search_ebay_sold(query, limit=10):
    """Search eBay sold listings using the Browse API."""
    import re
    search_url = (
        "https://www.ebay.com/sch/i.html"
        f"?_nkw={requests.utils.quote(query)}"
        "&LH_Complete=1&LH_Sold=1&_sop=13&_ipg=25"
    )

    # Try official eBay Browse API first
    if EBAY_APP_ID:
        try:
            api_url = "https://api.ebay.com/buy/browse/v1/item_summary/search"
            params = {
                "q": query,
                "filter": "soldItems:true,conditionIds:{1000|1500|2000|2500|3000}",
                "sort": "endDateDesc",
                "limit": str(limit),
            }
            headers = {
                "Authorization": f"Bearer {get_ebay_token()}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Content-Type": "application/json",
            }
            resp = requests.get(api_url, params=params, headers=headers, timeout=10)
            if resp.ok:
                data = resp.json()
                items = data.get("itemSummaries", [])
                prices, sales = [], []
                for item in items:
                    price_info = item.get("price", {})
                    price = float(price_info.get("value", 0))
                    if not price:
                        continue
                    prices.append(price)
                    sales.append({
                        "title": item.get("title", ""),
                        "price": price,
                        "date": item.get("itemEndDate", "")[:10] if item.get("itemEndDate") else None,
                        "url": item.get("itemWebUrl"),
                    })
                if prices:
                    return {
                        "sales": sales[:5],
                        "avg": round(sum(prices) / len(prices), 2),
                        "high": round(max(prices), 2),
                        "low": round(min(prices), 2),
                        "count": len(prices),
                        "search_url": search_url,
                    }, None
        except Exception:
            pass

    # Fallback: scrape with improved headers
    from bs4 import BeautifulSoup
    import re as re2
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        }
        resp = requests.get(search_url, headers=headers, timeout=12)
        if not resp.ok:
            return {"sales": [], "avg": None, "high": None, "low": None, "count": 0}, f"eBay returned {resp.status_code}"
        soup = BeautifulSoup(resp.text, "lxml")
        prices, sales = [], []
        for item in soup.select(".s-item"):
            title_el = item.select_one(".s-item__title")
            price_el = item.select_one(".s-item__price")
            date_el  = item.select_one(".s-item__ended-date, .POSITIVE")
            link_el  = item.select_one("a.s-item__link")
            if not title_el or not price_el:
                continue
            title = title_el.get_text(strip=True)
            if title.lower().startswith("shop on ebay"):
                continue
            price_text = price_el.get_text(strip=True)
            price_match = re2.search(r"[\d,]+\.?\d*", price_text.replace(",", ""))
            if not price_match:
                continue
            price = float(price_match.group().replace(",", ""))
            prices.append(price)
            sales.append({
                "title": title,
                "price": price,
                "date": date_el.get_text(strip=True) if date_el else None,
                "url": link_el["href"] if link_el else None,
            })
            if len(sales) >= limit:
                break
        if not prices:
            return {"sales": [], "avg": None, "high": None, "low": None, "count": 0}, None
        return {
            "sales": sales[:5],
            "avg": round(sum(prices) / len(prices), 2),
            "high": round(max(prices), 2),
            "low": round(min(prices), 2),
            "count": len(prices),
            "search_url": search_url,
        }, None
    except Exception as e:
        return None, str(e)

_ebay_token_cache = {"token": None, "expires": 0}

def get_ebay_token():
    """Get an eBay OAuth app token, cached."""
    import time
    now = time.time()
    if _ebay_token_cache["token"] and now < _ebay_token_cache["expires"]:
        return _ebay_token_cache["token"]
    EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
    if not EBAY_APP_ID or not EBAY_CLIENT_SECRET:
        return ""
    import base64
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {credentials}", "Content-Type": "application/x-www-form-urlencoded"},
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=10,
    )
    if resp.ok:
        data = resp.json()
        _ebay_token_cache["token"] = data.get("access_token", "")
        _ebay_token_cache["expires"] = now + data.get("expires_in", 7200) - 60
        return _ebay_token_cache["token"]
    return ""


CL_SEARCH_URL = "https://search-zzvl7ri3bq-uc.a.run.app"


def search_cardladder(query, year="", cl_token=""):
    """Query the Card Ladder search API using the user's auth token."""
    if not cl_token:
        return None, "No Card Ladder token"
    params = {"query": query, "year": str(year) if year else "", "limit": "5"}
    headers = {"Authorization": f"Bearer {cl_token}", "Accept": "application/json"}
    try:
        resp = requests.get(f"{CL_SEARCH_URL}/search", params=params, headers=headers, timeout=8)
        if resp.status_code in (401, 403):
            return None, "Card Ladder token invalid or expired"
        resp.raise_for_status()
        data = resp.json()
        card = (data.get("results") or data.get("cards") or [None])[0]
        if not card:
            return None, "No results found"
        return {
            "clValue": card.get("clValue") or card.get("value"),
            "lastSalePrice": card.get("lastSalePrice") or card.get("lastPrice"),
            "lastSaleDate": card.get("lastSaleDate") or card.get("lastSoldAt"),
            "weeklyChange": card.get("weeklyPercentChange"),
            "recentSales": [
                {"date": s.get("date") or s.get("soldAt"), "price": s.get("price") or s.get("amount")}
                for s in (card.get("recentSales") or card.get("sales") or [])
            ],
            "cardUrl": f"https://www.cardladder.com{card['url']}" if card.get("url") else None,
        }, None
    except Exception as e:
        return None, str(e)


@app.route('/value', methods=['POST'])
def value():
    body = request.get_json()
    name  = body.get("name", "")
    year  = body.get("year", "")
    grade = body.get("grade", "")
    card  = body.get("card", "")
    cl_token = body.get("cl_token", "")

    query_parts = [p for p in [str(year) if year else "", name, grade] if p]
    query = " ".join(query_parts) if query_parts else card

    if not query.strip():
        return jsonify({"success": False, "error": "No card data to search"})

    ebay_result, ebay_err = search_ebay_sold(query)
    cl_result, cl_err = search_cardladder(query, year, cl_token) if cl_token else (None, None)

    return jsonify({
        "success": True,
        "query": query,
        "ebay": ebay_result,
        "ebay_error": ebay_err,
        "cardladder": cl_result,
        "cardladder_error": cl_err,
    })


@app.route('/undo-sheet', methods=['POST'])
@login_required
def undo_sheet():
    """Delete the last row added to the sheet."""
    try:
        body = request.get_json()
        custom_sheet = body.get('sheet_id', '')
        custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None
        user = get_user_by_id(session['user_id'])
        sheet_id = custom_sheet_id or (user.get('google_sheet_id') if user else None) or SPREADSHEET_ID
        if not sheet_id:
            return jsonify({'success': False, 'error': 'No Google Sheet connected.'})
        svc = get_user_sheets_service(user)
        # Get sheet metadata to find sheet ID for batchUpdate
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        first_sheet = meta['sheets'][0]
        sheet_gid = first_sheet['properties']['sheetId']
        tab = first_sheet['properties']['title']
        # Get row count
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!A:A"
        ).execute()
        num_rows = len(result.get('values', []))
        if num_rows < 2:
            return jsonify({'success': False, 'error': 'No rows to undo.'})
        # Delete the last row
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id,
            body={'requests': [{'deleteDimension': {'range': {
                'sheetId': sheet_gid,
                'dimension': 'ROWS',
                'startIndex': num_rows - 1,
                'endIndex': num_rows
            }}}]}
        ).execute()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/update-value', methods=['POST'])
@login_required
def update_value():
    """Update the value column on the most recently written sheet row."""
    try:
        body = request.get_json()
        value = body.get('value', '').strip()
        card_desc = body.get('card', '').strip()
        custom_sheet = body.get('sheet_id', '')
        custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None

        user = get_user_by_id(session['user_id'])
        sheet_id = custom_sheet_id or (user.get('google_sheet_id') if user else None) or SPREADSHEET_ID
        if not sheet_id:
            return jsonify({'success': False, 'error': 'No Google Sheet connected.'})

        svc = get_user_sheets_service(user)
        tab = get_first_sheet_tab(sheet_id, svc)
        headers = get_sheet_headers(sheet_id, svc)
        mapping = detect_column_mapping(headers) if headers else {}

        # Find the value column index
        value_col = mapping.get('value')
        if value_col is None:
            return jsonify({'success': False, 'error': 'No value/price column found in your sheet.'})

        # Find the last row by getting all data
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"{tab}!A:Z"
        ).execute()
        rows = result.get('values', [])
        last_row = len(rows)  # 1-indexed, includes header

        if last_row < 2:
            return jsonify({'success': False, 'error': 'No cards in sheet yet.'})

        # Write value to last row
        col_letter = chr(ord('A') + value_col)
        cell = f"{tab}!{col_letter}{last_row}"
        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=cell,
            valueInputOption='USER_ENTERED',
            body={'values': [[f'${value}']]}
        ).execute()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/scan', methods=['POST'])
@login_required
def scan():
    try:
        # Accept image from browser camera
        body = request.get_json()
        is_upload = body.get('is_upload', False) if body else False
        raw_image_bytes = None
        if body and 'image' in body:
            import numpy as np
            raw_image_bytes = base64.b64decode(body['image'])
            nparr = np.frombuffer(raw_image_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            # Resize large images to max 1600px on longest side to avoid Railway timeout
            if frame is not None:
                h, w = frame.shape[:2]
                max_dim = 1600
                if max(h, w) > max_dim:
                    scale = max_dim / max(h, w)
                    frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
                raw_image_bytes = buf.tobytes()
        else:
            # Fall back to Mac camera
            with camera_lock:
                cap = get_camera()
                for _ in range(5):
                    cap.read()
                ret, frame = cap.read()
            if not ret:
                return jsonify({'success': False, 'error': 'Could not capture image'})
            # Encode frame so second/third passes can use it
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
            raw_image_bytes = buf.tobytes()

        scan_mode = body.get('scan_mode', 'raw') if body else 'raw'  # 'raw' or 'graded'

        if scan_mode == 'graded' and raw_image_bytes:
            # Graded mode — only run label analysis, skip general card scan
            data = analyze_label(raw_image_bytes)
            data['grade'] = data.get('grade') or 'Unknown'
            is_raw_card = False

        elif is_upload and raw_image_bytes:
            # Upload — high quality first pass
            data = analyze_card(frame, quality=95)
            is_raw_card = True

        else:
            # Camera scan — first pass
            data = analyze_card(frame, quality=85)
            is_raw_card = (not data.get("grade") or data.get("grade", "").lower() == "raw")

        # Second pass for ALL raw cards (upload or camera)
        if is_raw_card and raw_image_bytes:
            try:
                raw_data = analyze_raw_card(
                    raw_image_bytes,
                    year_hint=data.get("year"),
                    sport_hint=data.get("sport")
                )
                # Always trust the second pass for year and brand — it looks harder
                for field in ["year", "brand"]:
                    if raw_data.get(field):
                        data[field] = raw_data[field]
                # Fill blanks for everything else
                for field in ["set", "parallel", "serial", "card_number", "sport"]:
                    if raw_data.get(field) and not data.get(field):
                        data[field] = raw_data[field]
            except Exception as raw_err:
                app.logger.warning(f"Raw second pass failed: {raw_err}")

            # Third pass — crop bottom strip for year if still missing
            if not data.get("year"):
                try:
                    year = extract_year_from_copyright(raw_image_bytes)
                    if year:
                        data["year"] = year
                except Exception as yr_err:
                    app.logger.warning(f"Year crop pass failed: {yr_err}")

        # Sanity check: if player is a known rookie, their card year can't be
        # before their draft year. Catches Gemini hallucinating college years.
        if data.get("name"):
            draft_year = get_player_draft_year(data["name"])
            if draft_year:
                if data.get("year") and int(data["year"]) < draft_year:
                    app.logger.warning(
                        f"Year sanity fail: {data['name']} got year {data['year']} but draft year is {draft_year}. Setting to draft year."
                    )
                    data["year"] = draft_year  # Use draft year instead of nulling
                elif not data.get("year"):
                    # Year missing for a known rookie — fill it in from draft class
                    app.logger.info(f"Year missing for rookie {data['name']}, filling with draft year {draft_year}")
                    data["year"] = draft_year

        # Only count the scan after Gemini succeeds
        allowed, scans_used, limit = check_and_increment_scans(session['user_id'])
        if not allowed:
            return jsonify({
                'success': False,
                'limit_reached': True,
                'error': f'Free limit reached ({limit} scans/day). Upgrade to CardScan Pro for unlimited scans.'
            })

        # Duplicate cert detection for graded cards
        duplicate_warning = None
        cert = data.get('cert')
        if cert and cert != 'Raw':
            from database import get_db, DATABASE_URL
            try:
                conn = get_db()
                if DATABASE_URL:
                    cur = conn.cursor()
                    cur.execute("SELECT card FROM scan_history WHERE user_id = %s AND cert = %s LIMIT 1", (session['user_id'], cert))
                    dup = cur.fetchone()
                    cur.close(); conn.close()
                else:
                    dup = conn.execute("SELECT card FROM scan_history WHERE user_id = ? AND cert = ? LIMIT 1", (session['user_id'], cert)).fetchone()
                    conn.close()
                if dup:
                    duplicate_warning = f"⚠️ Cert #{cert} already in your history"
            except Exception:
                pass

        # Normalize all text fields to Title Case (Gemini often returns ALL CAPS)
        # Also strip exclamation points and extra whitespace from all text fields
        for field in ["name", "brand", "set", "parallel", "card", "sport", "team"]:
            val = data.get(field)
            if val and isinstance(val, str):
                val = val.replace("!", "").strip()
                if val.isupper():
                    val = val.title()
                data[field] = val

        # Merge serial into parallel exactly once, cleanly
        _merge_serial(data)

        # Always rebuild card description from individual fields
        # so it's never just the grade or a partial label read
        if data.get("card_type") != "tcg":
            if is_raw_card:
                parts = [p for p in [
                    str(data.get("year") or ""),
                    data.get("brand") or "",
                    data.get("set") or "",
                    data.get("name") or "",
                    data.get("parallel") or "",
                ] if p]
            else:
                # Graded — include grade in description
                parts = [p for p in [
                    str(data.get("year") or ""),
                    data.get("brand") or "",
                    data.get("set") or "",
                    data.get("name") or "",
                    data.get("parallel") or "",
                    data.get("grade") or "",
                ] if p]
            if parts:
                data["card"] = " ".join(parts)

        # Auto-fetch values
        cl_token = body.get("cl_token", "") if body else ""
        is_raw = (data.get("grade", "").lower() == "raw" or not data.get("grade"))
        card_type = data.get("card_type", "sports")

        if card_type == "tcg":
            if is_raw:
                # TCG raw: name + set + card_number + rarity
                query_parts = [p for p in [
                    data.get("name", ""),
                    data.get("set", ""),
                    data.get("card_number", ""),
                    data.get("rarity", ""),
                ] if p]
            else:
                # TCG graded: name + set + card_number + grade
                query_parts = [p for p in [
                    data.get("name", ""),
                    data.get("set", ""),
                    data.get("card_number", ""),
                    data.get("grade", ""),
                ] if p]
        elif is_raw:
            # Sports raw: year + brand + set + player + parallel
            query_parts = [p for p in [
                str(data.get("year", "")),
                data.get("brand", ""),
                data.get("set", ""),
                data.get("name", ""),
                data.get("parallel", ""),
            ] if p]
        else:
            # Sports graded: year + player + grade
            query_parts = [p for p in [
                str(data.get("year", "")),
                data.get("name", ""),
                data.get("grade", ""),
            ] if p]

        if query_parts:
            q = " ".join(query_parts)
            ebay_result, _ = search_ebay_sold(q)
            if ebay_result and ebay_result.get("avg"):
                data["ebay_avg"]   = ebay_result["avg"]
                data["ebay_high"]  = ebay_result["high"]
                data["ebay_low"]   = ebay_result["low"]
                data["ebay_count"] = ebay_result["count"]
                data["ebay_sales"] = ebay_result["sales"]
            if cl_token:
                cl_result, _ = search_cardladder(q, data.get("year", ""), cl_token)
                if cl_result:
                    data["cl_value"]     = cl_result.get("clValue")
                    data["cl_last_sale"] = cl_result.get("lastSalePrice")
                    data["cl_sales"]     = cl_result.get("recentSales", [])

        user = get_user_by_id(session['user_id'])
        custom_sheet = body.get("sheet_id", "") if body else ""
        custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None
        sheet_warning = None
        if user.get('auto_sheet', True):
            try:
                append_to_sheet(data, custom_sheet_id, user=user)
            except Exception as sheet_err:
                app.logger.warning(f"Sheet write failed: {sheet_err}")
                sheet_warning = str(sheet_err)

        # Save to scan history
        try:
            save_scan(session['user_id'], data)
        except Exception as e:
            app.logger.warning(f"Scan history save failed: {e}")

        return jsonify({'success': True, 'data': data, 'sheet_warning': sheet_warning, 'duplicate_warning': duplicate_warning})
    except Exception as e:
        err = str(e)
        app.logger.error(f"Scan error: {err}")
        if any(x in err for x in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")):
            return jsonify({'success': False, 'error': 'Scanner is busy right now — your scan was not counted. Please try again in a moment.'})
        if "JSONDecodeError" in err or "json" in err.lower():
            return jsonify({'success': False, 'error': 'Could not read the card — please retake the photo with better lighting and try again. Scan was not counted.'})
        return jsonify({'success': False, 'error': 'Something went wrong — your scan was not counted. Please try again.'})

@app.route('/resheet-card', methods=['POST'])
@login_required
def resheet_card():
    """Append an edited single card scan to the user's sheet."""
    user = get_user_by_id(session['user_id'])
    body = request.get_json()
    card = body.get('card', {})
    custom_sheet = body.get('sheet_id', '')
    custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None
    try:
        append_to_sheet(card, custom_sheet_id, user=user)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/scan-bulk', methods=['POST'])
@login_required
def scan_bulk():
    """Pro-only: detect all cards in a single image and return for review."""
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Bulk scanning is a Pro feature. Upgrade to unlock it.'})
    try:
        body = request.get_json()
        raw_image_bytes = base64.b64decode(body['image'])
        # Resize large images before sending to Gemini
        import numpy as np
        nparr = np.frombuffer(raw_image_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if frame is not None:
            h, w = frame.shape[:2]
            if max(h, w) > 1600:
                scale = 1600 / max(h, w)
                frame = cv2.resize(frame, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            raw_image_bytes = buf.tobytes()
        cards = analyze_bulk(raw_image_bytes)
        if not isinstance(cards, list):
            return jsonify({'success': False, 'error': 'Could not detect cards in image'})
        return jsonify({'success': True, 'cards': cards, 'count': len(cards)})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/scan-bulk-ebay', methods=['POST'])
@login_required
def scan_bulk_ebay():
    """Pro-only: fetch eBay sold prices for a list of cards in parallel."""
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        body = request.get_json()
        cards = body.get('cards', [])

        def fetch(i, card):
            query_parts = [p for p in [
                str(card.get('year') or ''),
                card.get('brand') or '',
                card.get('set') or '',
                card.get('name') or '',
                card.get('parallel') or '',
                card.get('grade') or '',
            ] if p]
            if not query_parts:
                return i, None
            result, _ = search_ebay_sold(' '.join(query_parts))
            return i, result

        results = [None] * len(cards)
        with ThreadPoolExecutor(max_workers=6) as ex:
            futures = {ex.submit(fetch, i, card): i for i, card in enumerate(cards)}
            for future in as_completed(futures):
                i, result = future.result()
                results[i] = result

        return jsonify({'success': True, 'results': results})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/scan-bulk-prices', methods=['POST'])
@login_required
def scan_bulk_prices():
    """Pro-only: read price tags from a photo and return ordered price list."""
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})
    try:
        body = request.get_json()
        raw_image_bytes = base64.b64decode(body['image'])
        prices = analyze_prices(raw_image_bytes)
        if not isinstance(prices, list):
            return jsonify({'success': False, 'error': 'Could not read prices from image'})
        return jsonify({'success': True, 'prices': prices})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/scan-bulk-confirm', methods=['POST'])
@login_required
def scan_bulk_confirm():
    """Pro-only: sheet all confirmed cards, deducting scan count per card."""
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})
    try:
        body = request.get_json()
        cards = body.get('cards', [])
        custom_sheet = body.get('sheet_id', '')
        custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None

        # Make sure there's actually a sheet to write to
        sheet_id = custom_sheet_id or user.get("google_sheet_id") or SPREADSHEET_ID
        if not sheet_id:
            return jsonify({'success': False, 'error': 'No Google Sheet connected. Go to Connect Sheets in the settings to set one up.'})

        sheeted = 0
        errors = []
        for card in cards:
            # Deduct one scan per card confirmed
            check_and_increment_scans(session['user_id'])
            if user.get('auto_sheet', True):
                try:
                    append_to_sheet(card, custom_sheet_id, user=user)
                    sheeted += 1
                except Exception as card_err:
                    errors.append(str(card_err))
            try:
                save_scan(session['user_id'], card)
            except Exception as e:
                app.logger.warning(f"Bulk scan history save failed: {e}")

        if sheeted == 0:
            return jsonify({'success': False, 'error': f'Could not write to sheet: {errors[0] if errors else "Unknown error"}'})

        return jsonify({
            'success': True,
            'sheeted': sheeted,
            'errors': len(errors),
            'message': f'{sheeted} cards added to your sheet!' + (f' ({len(errors)} failed)' if errors else '')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/admin/reset-user-password/<secret>/<email>')
def admin_reset_user_password(secret, email):
    if not check_admin(secret):
        return "Forbidden", 403
    from database import get_db
    from werkzeug.security import generate_password_hash
    new_password = "CardScan123!"
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("UPDATE users SET password_hash = %s WHERE email = %s",
                       (generate_password_hash(new_password), email.lower()))
            count = cur.rowcount
            db.commit()
            cur.close()
        else:
            cur = db.execute("UPDATE users SET password_hash = ? WHERE email = ?",
                      (generate_password_hash(new_password), email.lower()))
            count = cur.rowcount
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500
    if count == 0:
        return f"❌ No user found with email: {email}"
    return f"✅ Password reset for {email} — temp password: <strong>CardScan123!</strong> — tell them to change it after logging in."

@app.route('/admin/reset-password/<secret>')
def admin_reset_password(secret):
    if not check_admin(secret):
        return "Forbidden", 403
    from database import get_db
    from werkzeug.security import generate_password_hash
    new_password = "CardScan2024!"
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("UPDATE users SET password_hash = %s WHERE email = %s",
                       (generate_password_hash(new_password), OWNER_EMAIL))
            db.commit()
            cur.close()
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE email = ?",
                      (generate_password_hash(new_password), OWNER_EMAIL))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500
    return f"✅ Password reset! Login with: {OWNER_EMAIL} / {new_password}"

def _admin_set_plan(email, plan, secret):
    if not check_admin(secret):
        return "Forbidden", 403
    from database import get_db
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("UPDATE users SET subscription_status = %s WHERE email = %s", (plan, email))
            db.commit()
            cur.close()
        else:
            db.execute("UPDATE users SET subscription_status = ? WHERE email = ?", (plan, email))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500
    return redirect('/admin/dashboard')

@app.route('/admin/reset-user-pw', methods=['POST'])
def admin_reset_user_pw():
    # Gated by owner session — no URL secret needed
    if not session.get('user_id'):
        return "Forbidden", 403
    user = get_user_by_id(session['user_id'])
    if not user or user['email'] != OWNER_EMAIL:
        return "Forbidden", 403
    email = request.form.get('email', '').lower()
    from database import get_db
    from werkzeug.security import generate_password_hash
    new_password = "CardScan123!"
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("UPDATE users SET password_hash = %s WHERE email = %s",
                       (generate_password_hash(new_password), email))
            db.commit()
            cur.close()
        else:
            db.execute("UPDATE users SET password_hash = ? WHERE email = ?",
                      (generate_password_hash(new_password), email))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500
    return redirect('/admin/dashboard')

@app.route('/admin/delete-user', methods=['POST'])
def admin_delete_user():
    if not check_admin(request.form.get('secret')):
        return "Forbidden", 403
    email = request.form.get('email', '').lower()
    from database import get_db
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("DELETE FROM users WHERE email = %s", (email,))
            db.commit()
            cur.close()
        else:
            db.execute("DELETE FROM users WHERE email = ?", (email,))
            db.commit()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500
    return redirect('/admin/dashboard')

@app.route('/admin/send-email', methods=['POST'])
def admin_send_email():
    body = request.get_json()
    if not check_admin(body.get('secret')):
        return jsonify({'success': False, 'error': 'Forbidden'})
    to = body.get('to', '')
    subject = body.get('subject', '')
    message = body.get('body', '')
    if not to or not subject or not message:
        return jsonify({'success': False, 'error': 'Missing fields'})
    try:
        send_reset_email.__module__  # just to confirm import works
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'CardScan <{GMAIL_USER}>'
        msg['To'] = to
        html = f'<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0a0a0a;color:#fff;"><h2>Card<span style="color:#00e676;">Scan</span></h2><div style="color:#ccc;line-height:1.7;margin-top:16px;">{message.replace(chr(10), "<br>")}</div><p style="color:#555;font-size:12px;margin-top:32px;">Sent from CardScan Admin</p></div>'
        msg.attach(MIMEText(html, 'html'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, to, msg.as_string())
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/admin/broadcast', methods=['POST'])
def admin_broadcast():
    """Send an email to all users."""
    body = request.get_json()
    if not check_admin(body.get('secret')):
        return jsonify({'success': False, 'error': 'Forbidden'})
    subject = body.get('subject', '').strip()
    message = body.get('body', '').strip()
    if not subject or not message:
        return jsonify({'success': False, 'error': 'Missing subject or message'})
    from database import get_db
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("SELECT email FROM users")
            emails = [r[0] for r in cur.fetchall()]
            cur.close()
        else:
            emails = [r[0] for r in db.execute("SELECT email FROM users").fetchall()]
        db.close()
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

    sent, failed = 0, []
    html = f'''<div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0a0a0a;color:#fff;">
      <h2 style="font-size:22px;font-weight:800;">Card<span style="color:#00e676;">Scan</span></h2>
      <div style="color:#ccc;line-height:1.8;margin-top:16px;font-size:15px;">{message.replace(chr(10), "<br>")}</div>
      <a href="https://cardscan.live" style="display:inline-block;margin-top:24px;background:#00e676;color:#000;font-weight:800;padding:12px 24px;border-radius:10px;text-decoration:none;">Open CardScan</a>
      <p style="color:#555;font-size:12px;margin-top:32px;">You're receiving this because you have a CardScan account.</p>
    </div>'''

    for email in emails:
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = subject
            msg['From'] = f'CardScan <{GMAIL_USER}>'
            msg['To'] = email
            msg.attach(MIMEText(html, 'html'))
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_USER, email, msg.as_string())
            sent += 1
        except Exception as e:
            failed.append(f"{email}: {str(e)}")

    return jsonify({'success': True, 'sent': sent, 'failed': len(failed), 'errors': failed[:5]})

@app.route('/admin/upgrade', methods=['POST'])
def admin_upgrade():
    return _admin_set_plan(request.form.get('email'), 'pro', request.form.get('secret'))

@app.route('/admin/downgrade', methods=['POST'])
def admin_downgrade():
    return _admin_set_plan(request.form.get('email'), 'free', request.form.get('secret'))

@app.route('/admin/dashboard')
def admin_dashboard():
    if session.get('user_id'):
        user = get_user_by_id(session['user_id'])
        if not user or user['email'] != OWNER_EMAIL:
            return "Forbidden", 403
    else:
        return redirect(url_for('login'))

    from database import get_db
    from datetime import date, timedelta
    search = request.args.get('search', '').strip().lower()
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            today = str(date.today())
            week_ago = str(date.today() - timedelta(days=7))
            month_ago = str(date.today() - timedelta(days=30))
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE subscription_status = 'pro'")
            pro_users = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE scans_date = CURRENT_DATE::text")
            active_today = cur.fetchone()[0]
            cur.execute("SELECT SUM(scans_today) FROM users WHERE scans_date = CURRENT_DATE::text")
            scans_today = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM scan_history")
            total_scans_ever = cur.fetchone()[0] or 0
            cur.execute("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'")
            new_this_week = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '30 days'")
            new_this_month = cur.fetchone()[0]
            conversion_rate = round((pro_users / total_users * 100), 1) if total_users > 0 else 0
            cur.execute("SELECT COUNT(*) FROM users WHERE subscription_status='pro' AND COALESCE(plan_type,'monthly')='monthly'")
            monthly_pro = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM users WHERE subscription_status='pro' AND plan_type='annual'")
            annual_pro = cur.fetchone()[0]
            mrr = round(monthly_pro * 7.99 + annual_pro * (59 / 12), 2)
            # Pull total revenue from Stripe
            try:
                total_revenue = 0
                for charge in stripe.Charge.list(limit=100).auto_paging_iter():
                    if charge.status == 'succeeded' and not charge.refunded:
                        total_revenue += charge.amount
                total_revenue_dollars = round(total_revenue / 100, 2)
            except Exception:
                total_revenue_dollars = None
            if search:
                cur.execute("SELECT id, email, subscription_status, scans_today, created_at, COALESCE(total_scans, 0) FROM users WHERE LOWER(email) LIKE %s ORDER BY created_at DESC LIMIT 50", (f'%{search}%',))
            else:
                cur.execute("SELECT id, email, subscription_status, scans_today, created_at, COALESCE(total_scans, 0) FROM users ORDER BY created_at DESC LIMIT 50")
            recent_users = cur.fetchall()
            cur.execute("SELECT email, COALESCE(total_scans, 0) as ts FROM users ORDER BY ts DESC LIMIT 5")
            top_scanners = cur.fetchall()
            cur.close()
        else:
            today = str(date.today())
            week_ago = str(date.today() - timedelta(days=7))
            month_ago = str(date.today() - timedelta(days=30))
            total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pro_users = db.execute("SELECT COUNT(*) FROM users WHERE subscription_status = 'pro'").fetchone()[0]
            active_today = db.execute("SELECT COUNT(*) FROM users WHERE scans_date = ?", (today,)).fetchone()[0]
            scans_today = db.execute("SELECT SUM(scans_today) FROM users WHERE scans_date = ?", (today,)).fetchone()[0] or 0
            total_scans_ever = db.execute("SELECT SUM(COALESCE(total_scans,0)) FROM users").fetchone()[0] or 0
            new_this_week = db.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (week_ago,)).fetchone()[0]
            new_this_month = db.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (month_ago,)).fetchone()[0]
            conversion_rate = round((pro_users / total_users * 100), 1) if total_users > 0 else 0
            monthly_pro = db.execute("SELECT COUNT(*) FROM users WHERE subscription_status='pro' AND COALESCE(plan_type,'monthly')='monthly'").fetchone()[0]
            annual_pro = db.execute("SELECT COUNT(*) FROM users WHERE subscription_status='pro' AND plan_type='annual'").fetchone()[0]
            mrr = round(monthly_pro * 7.99 + annual_pro * (59 / 12), 2)
            try:
                total_revenue = 0
                for charge in stripe.Charge.list(limit=100).auto_paging_iter():
                    if charge.status == 'succeeded' and not charge.refunded:
                        total_revenue += charge.amount
                total_revenue_dollars = round(total_revenue / 100, 2)
            except Exception:
                total_revenue_dollars = None
            if search:
                recent_users = db.execute("SELECT id, email, subscription_status, scans_today, created_at, COALESCE(total_scans,0) FROM users WHERE LOWER(email) LIKE ? ORDER BY created_at DESC LIMIT 50", (f'%{search}%',)).fetchall()
            else:
                recent_users = db.execute("SELECT id, email, subscription_status, scans_today, created_at, COALESCE(total_scans,0) FROM users ORDER BY created_at DESC LIMIT 50").fetchall()
            top_scanners = db.execute("SELECT email, COALESCE(total_scans,0) as ts FROM users ORDER BY ts DESC LIMIT 5").fetchall()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500

    secret = os.environ.get("ADMIN_SECRET", "")

    def make_row(u):
        uid, email, plan, scans, joined, total = u[0], u[1], u[2], u[3], u[4], u[5]
        plan_label = '🟢 Pro' if plan == 'pro' else '⚪ Free'
        upgrade_btn = (
            f'<form method="POST" action="/admin/upgrade" style="display:inline">'
            f'<input type="hidden" name="email" value="{email}">'
            f'<input type="hidden" name="secret" value="{secret}">'
            f'<button style="background:#00ff87;color:#000;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:700;font-size:12px;">→ Pro</button>'
            f'</form>'
        ) if plan != 'pro' else (
            f'<form method="POST" action="/admin/downgrade" style="display:inline">'
            f'<input type="hidden" name="email" value="{email}">'
            f'<input type="hidden" name="secret" value="{secret}">'
            f'<button style="background:#333;color:#888;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;">→ Free</button>'
            f'</form>'
        )
        delete_btn = (
            f'<form method="POST" action="/admin/delete-user" style="display:inline" onsubmit="return confirm(\'Delete {email}?\')">'
            f'<input type="hidden" name="email" value="{email}">'
            f'<input type="hidden" name="secret" value="{secret}">'
            f'<button style="background:#3a1a1a;color:#ff6b6b;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-left:4px;">✕</button>'
            f'</form>'
        )
        email_btn = (
            f'<button onclick="openEmail(\'{email}\')" style="background:#1a1a2a;color:#88aaff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-left:4px;">✉</button>'
        )
        reset_pw_btn = (
            f'<form method="POST" action="/admin/reset-user-pw" style="display:inline" onsubmit="return confirm(\'Reset password for {email} to CardScan123! ?\')">'
            f'<input type="hidden" name="email" value="{email}">'
            f'<button style="background:#1a1a3a;color:#aaaaff;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-left:4px;">🔑</button>'
            f'</form>'
        )
        trial_btn = (
            f'<button onclick="grantTrial(\'{email}\')" style="background:#1a2e1a;color:#00ff87;border:1px solid #00ff87;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-left:4px;">🎁 Trial</button>'
        ) if plan != 'pro' else ''
        return f"<tr><td>{email}</td><td>{plan_label}</td><td>{scans}</td><td>{total}</td><td>{str(joined)[:10]}</td><td style='white-space:nowrap'>{upgrade_btn}{trial_btn}{delete_btn}{email_btn}{reset_pw_btn}</td></tr>"

    rows = ''.join([make_row(u) for u in recent_users])
    top_scanner_rows = ''.join([f"<tr><td>{u[0]}</td><td style='color:#00ff87;font-weight:700'>{u[1]}</td></tr>" for u in top_scanners])
    search_val = search or ''

    # Referral stats
    try:
        if hasattr(db, 'cursor'):
            db2 = get_db()
            cur2 = db2.cursor()
            cur2.execute("SELECT referred_by, COUNT(*) as cnt, SUM(CASE WHEN subscription_status='pro' THEN 1 ELSE 0 END) as converted FROM users WHERE referred_by IS NOT NULL GROUP BY referred_by ORDER BY cnt DESC LIMIT 10")
            referral_rows_data = cur2.fetchall()
            cur2.close(); db2.close()
        else:
            referral_rows_data = []
    except Exception:
        referral_rows_data = []
    referral_rows = ''.join([f"<tr><td>{r[0]}</td><td style='color:#00ff87'>{r[1]}</td><td style='color:#ffd700'>{r[2]}</td></tr>" for r in referral_rows_data])

    # CardConnect deals stats
    deal_total = 0
    deal_volume = 0.0
    deal_by_buyer = []
    deal_recent = []
    try:
        db3 = get_db()
        if hasattr(db3, 'cursor'):
            cur3 = db3.cursor()
            cur3.execute("SELECT COUNT(*), COALESCE(SUM(sale_price),0) FROM deals")
            r = cur3.fetchone(); deal_total = r[0] or 0; deal_volume = float(r[1] or 0)
            cur3.execute("SELECT buyer_name, buyer_instagram, COUNT(*) as cnt, COALESCE(SUM(sale_price),0) as vol FROM deals GROUP BY buyer_name, buyer_instagram ORDER BY cnt DESC LIMIT 10")
            deal_by_buyer = cur3.fetchall()
            cur3.execute("SELECT card_name, buyer_name, sale_price, created_at FROM deals ORDER BY created_at DESC LIMIT 10")
            deal_recent = cur3.fetchall()
            cur3.close(); db3.close()
    except Exception:
        pass
    deal_buyer_rows = ''.join([f"<tr><td>{r[0]}</td><td style='color:#aaa'>@{r[1]}</td><td style='color:#00ff87;font-weight:700'>{r[2]}</td><td style='color:#ffd700'>${float(r[3]):.0f}</td></tr>" for r in deal_by_buyer])
    deal_recent_rows = ''.join([f"<tr><td>{r[0]}</td><td style='color:#aaa'>{r[1]}</td><td style='color:#00ff87'>${float(r[2]):.0f}</td><td style='color:#555'>{str(r[3])[:10]}</td></tr>" for r in deal_recent])

    return f"""<!DOCTYPE html><html>
<head><title>CardScan Admin</title>
<style>
body{{font-family:system-ui;background:#0d0d0d;color:#fff;padding:40px;max-width:1000px;margin:0 auto}}
h1{{color:#00ff87;margin-bottom:4px}}
h2{{color:#888;font-size:13px;text-transform:uppercase;letter-spacing:1px;margin:28px 0 12px}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:24px 0}}
.stat{{background:#1a1a1a;border-radius:12px;padding:18px;text-align:center}}
.stat-num{{font-size:30px;font-weight:800;color:#00ff87}}
.stat-label{{color:#888;font-size:12px;margin-top:4px}}
.stat.money .stat-num{{color:#ffd700}}
.stat.blue .stat-num{{color:#88aaff}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;color:#888;font-size:12px;padding:8px;border-bottom:1px solid #333}}
td{{padding:9px 8px;border-bottom:1px solid #1a1a1a;font-size:13px}}
.search-bar{{display:flex;gap:10px;margin-bottom:20px}}
.search-bar input{{flex:1;padding:10px 14px;background:#1a1a1a;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px}}
.search-bar button{{padding:10px 20px;background:#00ff87;color:#000;border:none;border-radius:8px;font-weight:700;cursor:pointer}}
.two-col{{display:grid;grid-template-columns:2fr 1fr;gap:24px;margin-top:8px}}
.email-modal{{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.8);z-index:100;align-items:center;justify-content:center}}
.email-modal.open{{display:flex}}
.email-box{{background:#1a1a1a;border:1px solid #333;border-radius:14px;padding:28px;width:480px}}
.email-box input,.email-box textarea{{width:100%;padding:10px;background:#0d0d0d;border:1px solid #333;border-radius:8px;color:#fff;font-size:14px;margin-bottom:12px}}
.email-box textarea{{height:120px;resize:vertical}}
.email-box button{{padding:10px 20px;background:#00ff87;color:#000;border:none;border-radius:8px;font-weight:700;cursor:pointer}}
.email-box .cancel{{background:#333;color:#888;margin-left:10px}}
</style></head>
<body>
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
  <h1>📊 CardScan Admin</h1>
  <button onclick="document.getElementById('broadcastModal').classList.add('open')" style="background:#1a1a2a;color:#88aaff;border:1px solid #334;border-radius:8px;padding:10px 18px;cursor:pointer;font-weight:700;font-size:13px;">📢 Email All Users</button>
</div>
<p style="color:#555;font-size:13px;margin-bottom:24px;">
  Last refreshed: <span id="refreshTime"></span>
  <span style="margin-left:12px; color:#444;">Auto-refreshes every 60s</span>
  <button onclick="location.reload()" style="margin-left:12px;background:#1a1a1a;color:#888;border:1px solid #333;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;">↺ Refresh now</button>
</p>
<script>
  document.getElementById('refreshTime').textContent = new Date().toLocaleTimeString();
  setTimeout(() => location.reload(), 60000);
  async function grantTrial(email) {{
    const days = prompt(`Grant how many days of Pro trial to ${{email}}?`, '7');
    if (!days) return;
    const res = await fetch('/admin/grant-trial', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{secret: '{secret}', email, days: parseInt(days)}})
    }});
    const data = await res.json();
    alert(data.message || data.error);
    if (data.success) location.reload();
  }}
</script>

<div class="stats">
  <div class="stat"><div class="stat-num">{total_users}</div><div class="stat-label">Total Users</div></div>
  <div class="stat"><div class="stat-num">{pro_users}</div><div class="stat-label">Pro Users</div></div>
  <div class="stat money"><div class="stat-num">${mrr}</div><div class="stat-label">Est. MRR</div></div>
  <div class="stat money"><div class="stat-num">${'%.2f' % total_revenue_dollars if total_revenue_dollars is not None else '—'}</div><div class="stat-label">Total Revenue</div></div>
  <div class="stat blue"><div class="stat-num">{conversion_rate}%</div><div class="stat-label">Free → Pro Rate</div></div>
  <div class="stat"><div class="stat-num">{active_today}</div><div class="stat-label">Active Today</div></div>
  <div class="stat"><div class="stat-num">{scans_today}</div><div class="stat-label">Scans Today</div></div>
  <div class="stat"><div class="stat-num">{total_scans_ever}</div><div class="stat-label">Total Scans Ever</div></div>
  <div class="stat blue"><div class="stat-num">{new_this_week}</div><div class="stat-label">New This Week</div></div>
  <div class="stat blue"><div class="stat-num">{new_this_month}</div><div class="stat-label">New This Month</div></div>
</div>

<div class="two-col">
  <div>
    <h2>Users</h2>
    <form class="search-bar" method="GET">
      <input type="text" name="search" placeholder="Search by email..." value="{search_val}">
      <button type="submit">Search</button>
      {'<a href="/admin/dashboard" style="padding:10px 16px;background:#333;color:#888;border:none;border-radius:8px;text-decoration:none;font-size:14px;">Clear</a>' if search_val else ''}
    </form>
    <table>
      <tr><th>Email</th><th>Plan</th><th>Today</th><th>Total</th><th>Joined</th><th>Actions</th></tr>
      {rows}
    </table>
  </div>
  <div>
    <h2>Top Scanners</h2>
    <table>
      <tr><th>Email</th><th>Total Scans</th></tr>
      {top_scanner_rows}
    </table>
    <h2 style="margin-top:24px;">Referral Tracking</h2>
    <table>
      <tr><th>Referral Code</th><th>Referred</th><th>Converted</th></tr>
      {referral_rows if referral_rows else '<tr><td colspan=3 style="color:#555">No referrals yet</td></tr>'}
    </table>
  </div>
</div>

<!-- Email modal -->
<div class="email-modal" id="emailModal">
  <div class="email-box">
    <h3 style="margin-bottom:16px;">✉ Send Email</h3>
    <input type="text" id="emailTo" placeholder="To" readonly>
    <input type="text" id="emailSubject" placeholder="Subject">
    <textarea id="emailBody" placeholder="Message..."></textarea>
    <div>
      <button onclick="sendAdminEmail()">Send</button>
      <button class="cancel" onclick="document.getElementById('emailModal').classList.remove('open')">Cancel</button>
    </div>
    <div id="emailStatus" style="margin-top:10px;font-size:13px;color:#00ff87;"></div>
  </div>
</div>

<!-- Broadcast modal -->
<div class="email-modal" id="broadcastModal">
  <div class="email-box">
    <h3 style="margin-bottom:4px;">📢 Broadcast to All Users</h3>
    <p style="color:#888;font-size:12px;margin-bottom:16px;">Sends to every account. Double-check before sending.</p>
    <input type="text" id="broadcastSubject" placeholder="Subject">
    <textarea id="broadcastBody" placeholder="Message..." style="height:160px;"></textarea>
    <div>
      <button onclick="sendBroadcast()">Send to All</button>
      <button class="cancel" onclick="document.getElementById('broadcastModal').classList.remove('open')">Cancel</button>
    </div>
    <div id="broadcastStatus" style="margin-top:10px;font-size:13px;color:#00ff87;"></div>
  </div>
</div>

<script>
function openEmail(email) {{
  document.getElementById('emailTo').value = email;
  document.getElementById('emailSubject').value = '';
  document.getElementById('emailBody').value = '';
  document.getElementById('emailStatus').textContent = '';
  document.getElementById('emailModal').classList.add('open');
}}
async function sendAdminEmail() {{
  const to = document.getElementById('emailTo').value;
  const subject = document.getElementById('emailSubject').value;
  const body = document.getElementById('emailBody').value;
  const res = await fetch('/admin/send-email', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{to, subject, body, secret: '{secret}'}})
  }});
  const data = await res.json();
  document.getElementById('emailStatus').textContent = data.success ? '✓ Email sent!' : '✗ ' + data.error;
  if (data.success) setTimeout(() => document.getElementById('emailModal').classList.remove('open'), 1500);
}}
async function sendBroadcast() {{
  const subject = document.getElementById('broadcastSubject').value;
  const body = document.getElementById('broadcastBody').value;
  if (!subject || !body) {{ alert('Please fill in subject and message.'); return; }}
  document.getElementById('broadcastStatus').textContent = '⏳ Sending...';
  const res = await fetch('/admin/broadcast', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{subject, body, secret: '{secret}'}})
  }});
  const data = await res.json();
  document.getElementById('broadcastStatus').textContent = data.success
    ? `✓ Sent to ${{data.sent}} users! (${{data.failed}} failed)`
    : '✗ ' + data.error;
}}
</script>

  <!-- CardConnect Section -->
  <h2>CardConnect Deals</h2>
  <div class="stats" style="grid-template-columns:repeat(3,1fr)">
    <div class="stat"><div class="stat-num">{deal_total}</div><div class="stat-label">Deals Reported</div></div>
    <div class="stat money"><div class="stat-num">${deal_volume:,.0f}</div><div class="stat-label">Total Volume</div></div>
    <div class="stat money"><div class="stat-num">${deal_volume * 0.01:,.0f}</div><div class="stat-label">Potential Revenue (1%)</div></div>
  </div>

  <h2>Deals by Buyer</h2>
  <table style="width:100%;border-collapse:collapse">
    <tr style="color:#555;font-size:12px;text-align:left"><th style="padding:8px">Buyer</th><th>Handle</th><th>Deals</th><th>Volume</th></tr>
    {deal_buyer_rows if deal_buyer_rows else '<tr><td colspan=4 style="color:#555;padding:12px">No deals reported yet</td></tr>'}
  </table>

  <h2>Recent Deals</h2>
  <table style="width:100%;border-collapse:collapse">
    <tr style="color:#555;font-size:12px;text-align:left"><th style="padding:8px">Card</th><th>Buyer</th><th>Price</th><th>Date</th></tr>
    {deal_recent_rows if deal_recent_rows else '<tr><td colspan=4 style="color:#555;padding:12px">No deals reported yet</td></tr>'}
  </table>

</body></html>"""

@app.route('/scan-price', methods=['POST'])
@login_required
def scan_price():
    """Scan the back of a card to read a sticky note price."""
    try:
        body = request.get_json()
        # Use image bytes directly — no need to re-encode through cv2
        image_data = base64.b64decode(body['image'])
        client = genai.Client(api_key=GEMINI_API_KEY)

        prompt = (
            "Look carefully at this image for a price written on a sticky note, sticker, label, or piece of tape. "
            "This is the amount paid for a trading card. "
            "READ THE NUMBER VERY CAREFULLY — do not confuse digits. Common prices are $5-$5000. "
            "If you see a handwritten number, look at each digit individually: "
            "7 is not 4, 0 is not 5, 1 is not 7, 9 is not 4. "
            "If there is a decimal point, include it (e.g. $12.50). "
            "If there is no decimal, assume it is a whole dollar amount (e.g. $700 not $7.00). "
            "Return ONLY valid JSON: {\"paid\": \"$700\"} or {\"paid\": null} if no price is visible. "
            "No other text, no markdown."
        )
        response = gemini_generate(client,
            model="gemini-2.5-flash",
            contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
        )
        text = response.text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()
        result = json.loads(text)
        paid = result.get("paid")

        # Update the paid column in the last row of the sheet
        if paid:
            try:
                user = get_user_by_id(session['user_id'])
                custom_sheet = body.get("sheet_id", "")
                sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else (user.get("google_sheet_id") if user else None) or SPREADSHEET_ID
                if sheet_id:
                    svc = get_user_sheets_service(user or {})
                    tab = get_first_sheet_tab(sheet_id, svc)
                    # Get current data to find last row and paid column
                    result_data = svc.spreadsheets().values().get(
                        spreadsheetId=sheet_id,
                        range=f"{tab}!1:1000"
                    ).execute()
                    rows = result_data.get("values", [])
                    if rows:
                        headers = rows[0]
                        mapping = detect_column_mapping(headers)
                        paid_col = mapping.get("paid")
                        last_row = len(rows)
                        if paid_col is not None and last_row > 1:
                            col_letter = chr(ord('A') + paid_col)
                            svc.spreadsheets().values().update(
                                spreadsheetId=sheet_id,
                                range=f"{tab}!{col_letter}{last_row}",
                                valueInputOption="USER_ENTERED",
                                body={"values": [[paid]]}
                            ).execute()
            except Exception as e:
                return jsonify({"success": True, "paid": paid, "sheet_error": str(e)})

        return jsonify({"success": True, "paid": paid})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route('/scan-back-full', methods=['POST'])
@login_required
def scan_back_full():
    """Scan the back of a raw card — returns card detail updates AND any price sticker."""
    try:
        body = request.get_json(silent=True)
        if not body or 'image' not in body:
            return jsonify({'success': False, 'error': 'No image received'})
        try:
            image_data = base64.b64decode(body['image'])
        except Exception:
            return jsonify({'success': False, 'error': 'Invalid image data'})

        # Single Gemini call: card details + price in one pass
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            "This is the BACK of a sports trading card that has already been identified. "
            "DO NOT try to identify who the player is — that was done from the front. "
            "Your job is only to read specific printed details and any price sticker.\n\n"
            "1. READ FROM THE PRINTED CARD TEXT ONLY:\n"
            "   year        — The 4-digit year in the copyright line at the very bottom, "
            "                 e.g. '© 2021 Panini America' → 2021. Read it exactly.\n"
            "   card_number — The card's set number, e.g. '# 301' or '301' near the bottom corner. "
            "                 NOT a serial/numbered stamp.\n"
            "   brand       — Manufacturer name from copyright line: 'Panini', 'Topps', 'Upper Deck'.\n"
            "   set         — Set name if printed on the back: 'Prizm', 'Chrome', 'Select', etc.\n"
            "   rookie      — true ONLY if 'RC', 'Rookie Card', or 'Rookie' is explicitly printed.\n"
            "   serial      — If a foil/stamped number like '045/099' appears, return '/99'. "
            "                 null if not numbered.\n\n"
            "2. PRICE — Look for a sticker, sticky note, or handwritten price:\n"
            "   paid        — Dollar amount e.g. '$45', '$4.99'. null if nothing found.\n\n"
            "Return ONLY valid JSON (null for anything not found):\n"
            "  year, card_number, brand, set, rookie, serial, paid\n"
            "Return ONLY the JSON object — no markdown, no code fences."
        )
        response = gemini_generate(
            client,
            model="gemini-2.5-flash",
            contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
        )
        try:
            back = _parse_json_response(response.text)
        except Exception as parse_err:
            return jsonify({'success': False, 'error': f'Could not parse back scan response: {parse_err}'})

        update = {k: back[k] for k in
                  ('year', 'card_number', 'brand', 'set', 'rookie', 'serial')
                  if back.get(k) is not None}
        paid = back.get('paid')

        # If price found, also update the sheet's paid column
        if paid:
            try:
                user = get_user_by_id(session['user_id'])
                custom_sheet = body.get('sheet_id', '')
                sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else (
                    user.get('google_sheet_id') if user else None) or SPREADSHEET_ID
                if sheet_id:
                    svc = get_user_sheets_service(user or {})
                    tab = get_first_sheet_tab(sheet_id, svc)
                    result_data = svc.spreadsheets().values().get(
                        spreadsheetId=sheet_id, range=f'{tab}!1:1000').execute()
                    rows = result_data.get('values', [])
                    if rows:
                        headers = rows[0]
                        mapping = detect_column_mapping(headers)
                        paid_col = mapping.get('paid')
                        last_row = len(rows)
                        if paid_col is not None and last_row > 1:
                            col_letter = chr(ord('A') + paid_col)
                            svc.spreadsheets().values().update(
                                spreadsheetId=sheet_id,
                                range=f'{tab}!{col_letter}{last_row}',
                                valueInputOption='USER_ENTERED',
                                body={'values': [[paid]]}
                            ).execute()
            except Exception:
                pass

        return jsonify({'success': True, 'update': update, 'paid': paid})
    except Exception as e:
        err = str(e) or repr(e) or 'Back scan failed — please try again'
        return jsonify({'success': False, 'error': err})


@app.route('/delete-account', methods=['POST'])
@login_required
def delete_account():
    from database import get_db
    user_id = session['user_id']
    token = session.get('session_token')
    if token:
        delete_session(token)
    session.clear()
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            db.commit()
            cur.close()
        else:
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))
            db.commit()
        db.close()
    except Exception:
        pass
    return redirect('/?deleted=1')

# ── Whatnot Stream Analyzer ──────────────────────────────────────────────────

_whatnot_jobs = {}  # job_id -> {status, progress, total, cards, error}
WHATNOT_MAX_BYTES = 500 * 1024 * 1024  # 500 MB


def analyze_whatnot_frame(image_data):
    """Analyze a single Whatnot stream frame for card details, price, and sold state."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This is a frame from a Whatnot live stream selling sports cards or trading cards. "
        "Analyze the frame carefully and return ONLY valid JSON with these keys:\n\n"
        "  card_visible  - true if a trading card is clearly shown, false otherwise (boolean)\n"
        "  name          - player name (sports) or card name (TCG), null if not visible\n"
        "  year          - 4-digit year as integer, null if not visible\n"
        "  brand         - manufacturer e.g. 'Panini', 'Topps', null if not visible\n"
        "  set           - set name e.g. 'Prizm', 'Chrome', null if not visible\n"
        "  parallel      - parallel/variant e.g. 'Silver', 'Gold', null if not visible\n"
        "  grade         - grading if in slab e.g. 'PSA 10', 'BGS 9.5', or 'Raw', null if not visible\n"
        "  cert          - cert/serial number digits only, null if not visible\n"
        "  card          - full description: 'YEAR BRAND SET PLAYER PARALLEL GRADE', null if no card\n"
        "  price_shown   - any dollar amount visible as an overlay or on screen e.g. '$45', null if none\n"
        "  sold          - true if 'SOLD', 'WINNER', or a sold banner is clearly visible on screen\n"
        "  sold_price    - the final price shown at the moment of sale, null if not a sold frame\n\n"
        "Look for price overlays typically shown at the top or bottom of the Whatnot stream UI. "
        "Look for a green or red 'SOLD' banner or 'WINNER' text overlay. "
        "If no card is visible (transition screen, chat only, countdown, etc.) set card_visible to false. "
        "Return ONLY the JSON object — no markdown, no code fences, no extra text."
    )
    response = gemini_generate(
        client,
        model="gemini-2.5-flash",
        contents=[prompt, genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg")],
    )
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text.strip())


def _cards_match(a, b):
    """True if two card descriptions refer to the same card."""
    if not a or not b:
        return False
    # Compare key fields; fall back to fuzzy card string match
    for field in ("cert", "card"):
        va, vb = (a.get(field) or "").strip().lower(), (b.get(field) or "").strip().lower()
        if va and vb and va == vb:
            return True
    # Check name + grade similarity
    na = (a.get("name") or "").lower()
    nb = (b.get("name") or "").lower()
    ga = (a.get("grade") or "").lower()
    gb = (b.get("grade") or "").lower()
    if na and nb and na == nb and ga == gb:
        return True
    return False


def download_whatnot_url(job_id, url, user_id):
    """Background thread: download a replay URL with yt-dlp, then process it."""
    import yt_dlp

    job = _whatnot_jobs[job_id]
    tmp_path = None

    def progress_hook(d):
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            job['dl_bytes'] = downloaded
            job['dl_total'] = total
        elif d['status'] == 'finished':
            job['dl_bytes'] = job.get('dl_total', 0)

    tmp_dir = tempfile.gettempdir()
    tmp_base = os.path.join(tmp_dir, f'whatnot_{job_id}')

    ydl_opts = {
        # 720p or best available — good enough for card OCR, avoids huge 1080p files
        'format': 'bestvideo[height<=720][ext=mp4]+bestaudio/best[height<=720][ext=mp4]/best[height<=720]/best',
        'outtmpl': tmp_base + '.%(ext)s',
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [progress_hook],
        'noprogress': False,
        # Don't keep partial downloads on error
        'keepvideo': False,
    }

    try:
        job['phase'] = 'downloading'
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            # yt-dlp may merge into mkv/mp4 — find the output file
            ext = info.get('ext', 'mp4')
            tmp_path = tmp_base + '.' + ext
            # Fallback: scan temp dir for matching prefix
            if not os.path.exists(tmp_path):
                for fn in os.listdir(tmp_dir):
                    if fn.startswith(f'whatnot_{job_id}'):
                        tmp_path = os.path.join(tmp_dir, fn)
                        break

        if not tmp_path or not os.path.exists(tmp_path):
            job['status'] = 'error'
            job['error'] = 'Download completed but output file not found.'
            return

        job['phase'] = 'processing'
        process_whatnot_video(job_id, tmp_path, user_id)

    except Exception as e:
        job['status'] = 'error'
        job['error'] = f'Download failed: {str(e)}'
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                pass


import re as _re

def _merge_serial(data):
    """Ensure serial appears in parallel exactly once, and nowhere else."""
    serial = data.get("serial")
    if not serial:
        return
    # Normalise serial to '/NNN' format
    serial = serial.strip()
    if not serial.startswith("/"):
        serial = "/" + serial.lstrip("/")
    data["serial"] = serial

    parallel = data.get("parallel") or ""
    # Strip ALL existing '/NNN' patterns from parallel first
    parallel_clean = _re.sub(r'\s*/\d+', '', parallel).strip()
    # Reattach the single authoritative serial
    data["parallel"] = f"{parallel_clean} {serial}".strip() if parallel_clean else serial


def _frame_changed(prev, curr, threshold=0.96):
    """Return True if the frame looks meaningfully different from the previous one."""
    if prev is None:
        return True
    g1 = cv2.cvtColor(prev, cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(curr, cv2.COLOR_BGR2GRAY)
    h1 = cv2.calcHist([g1], [0], None, [256], [0, 256])
    h2 = cv2.calcHist([g2], [0], None, [256], [0, 256])
    cv2.normalize(h1, h1)
    cv2.normalize(h2, h2)
    correlation = cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL)
    return correlation < threshold


def process_whatnot_video(job_id, video_path, user_id):
    """Background thread: extract frames, run Gemini, build card list."""
    job = _whatnot_jobs[job_id]
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            job["status"] = "error"
            job["error"] = "Could not open video file"
            return

        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        frame_interval = max(1, int(fps * 3))  # sample every 3 seconds
        sample_count = max(1, total_frames // frame_interval)
        job["total"] = sample_count

        cards = []          # finalized card entries
        current = None      # card being tracked
        current_price = None
        frame_idx = 0
        processed = 0
        last_analyzed_frame = None  # last frame actually sent to Gemini
        last_result = {"card_visible": False}

        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                break

            processed += 1
            job["progress"] = processed

            # Skip Gemini if frame looks the same as last analyzed frame
            if not _frame_changed(last_analyzed_frame, frame):
                frame_idx += frame_interval
                continue

            last_analyzed_frame = frame.copy()
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            image_data = buf.tobytes()

            try:
                result = analyze_whatnot_frame(image_data)
            except Exception:
                result = {"card_visible": False}

            last_result = result

            if result.get("card_visible"):
                price = result.get("price_shown") or result.get("sold_price")
                if price:
                    current_price = price

                if result.get("sold"):
                    # Finalize current card with sold price
                    if current:
                        current["sold_price"] = result.get("sold_price") or current_price
                        current["price_shown"] = result.get("sold_price") or current_price
                        if not any(_cards_match(c, current) for c in cards):
                            cards.append(current)
                        job["cards"] = list(cards)
                    current = None
                    current_price = None
                elif _cards_match(result, current):
                    # Same card still showing — update price if we see one
                    if price:
                        current["price_shown"] = price
                else:
                    # New card detected — finalize previous if any
                    if current and not any(_cards_match(c, current) for c in cards):
                        cards.append(current)
                        job["cards"] = list(cards)
                    current = {k: result.get(k) for k in
                               ("name", "year", "brand", "set", "parallel", "grade", "cert", "card")}
                    current["price_shown"] = price
                    current["sold_price"] = None

            frame_idx += frame_interval

        cap.release()

        # Finalize any card still in progress
        if current and not any(_cards_match(c, current) for c in cards):
            cards.append(current)

        job["cards"] = cards
        job["status"] = "done"

    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
    finally:
        try:
            os.remove(video_path)
        except Exception:
            pass


@app.route('/whatnot')
@login_required
def whatnot():
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return redirect('/?upgrade=whatnot')
    return render_template('whatnot.html', user=user)


@app.route('/whatnot/upload', methods=['POST'])
@login_required
def whatnot_upload():
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})

    f = request.files.get('video')
    if not f:
        return jsonify({'success': False, 'error': 'No video file provided.'})

    # Save to temp file (streaming to avoid holding all bytes in RAM)
    suffix = os.path.splitext(f.filename or 'video.mp4')[1] or '.mp4'
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    size = 0
    try:
        for chunk in f.stream:
            size += len(chunk)
            if size > WHATNOT_MAX_BYTES:
                tmp.close()
                os.remove(tmp.name)
                return jsonify({'success': False, 'error': 'Video exceeds 500 MB limit.'})
            tmp.write(chunk)
        tmp.close()
    except Exception as e:
        try:
            tmp.close()
            os.remove(tmp.name)
        except Exception:
            pass
        return jsonify({'success': False, 'error': str(e)})

    job_id = str(uuid.uuid4())
    _whatnot_jobs[job_id] = {
        'status': 'processing',
        'phase': 'processing',
        'progress': 0,
        'total': 1,
        'dl_bytes': 0,
        'dl_total': 0,
        'cards': [],
        'error': None,
    }

    t = threading.Thread(
        target=process_whatnot_video,
        args=(job_id, tmp.name, session['user_id']),
        daemon=True,
    )
    t.start()

    return jsonify({'success': True, 'job_id': job_id})


@app.route('/whatnot/from-url', methods=['POST'])
@login_required
def whatnot_from_url():
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})
    body = request.get_json()
    url = (body or {}).get('url', '').strip()
    if not url:
        return jsonify({'success': False, 'error': 'No URL provided.'})

    job_id = str(uuid.uuid4())
    _whatnot_jobs[job_id] = {
        'status': 'processing',
        'phase': 'downloading',
        'progress': 0,
        'total': 1,
        'dl_bytes': 0,
        'dl_total': 0,
        'cards': [],
        'error': None,
    }

    t = threading.Thread(
        target=download_whatnot_url,
        args=(job_id, url, session['user_id']),
        daemon=True,
    )
    t.start()

    return jsonify({'success': True, 'job_id': job_id})


@app.route('/whatnot/progress/<job_id>')
@login_required
def whatnot_progress(job_id):
    """SSE stream for Whatnot processing progress."""
    def generate():
        while True:
            job = _whatnot_jobs.get(job_id)
            if not job:
                yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
                return
            payload = {
                'status': job['status'],
                'phase': job.get('phase', 'processing'),
                'progress': job['progress'],
                'total': job['total'],
                'dl_bytes': job.get('dl_bytes', 0),
                'dl_total': job.get('dl_total', 0),
                'cards': job['cards'],
                'error': job.get('error'),
            }
            yield f"data: {json.dumps(payload)}\n\n"
            if job['status'] in ('done', 'error'):
                return
            time.sleep(1)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


@app.route('/whatnot/confirm', methods=['POST'])
@login_required
def whatnot_confirm():
    """Sheet all confirmed Whatnot cards to Google Sheets."""
    user = get_user_by_id(session['user_id'])
    if not user or user.get('subscription_status') != 'pro':
        return jsonify({'success': False, 'error': 'Pro feature only.'})
    body = request.get_json()
    cards = body.get('cards', [])
    custom_sheet = body.get('sheet_id', '')
    custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None
    sheeted = 0
    for card in cards:
        # Map sold_price -> paid field for the sheet
        if card.get('sold_price'):
            card['paid'] = card['sold_price']
        elif card.get('price_shown'):
            card['paid'] = card['price_shown']
        try:
            append_to_sheet(card, custom_sheet_id, user=user)
            sheeted += 1
        except Exception:
            pass
    return jsonify({'success': True, 'sheeted': sheeted})


# ── Mobile API ───────────────────────────────────────────────────────────────

def mobile_auth(f):
    """Authenticate mobile requests via X-Session-Token header or session cookie."""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get('X-Session-Token')
        if token:
            user_id = None
            from database import get_db, DATABASE_URL
            db = get_db()
            try:
                if DATABASE_URL:
                    import psycopg2.extras
                    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                    cur.execute("SELECT user_id FROM user_sessions WHERE session_token = %s", (token,))
                    row = cur.fetchone(); cur.close()
                else:
                    row = db.execute("SELECT user_id FROM user_sessions WHERE session_token = ?", (token,)).fetchone()
                if row:
                    user_id = row['user_id']
            except Exception:
                pass
            finally:
                db.close()
            if not user_id:
                return jsonify({'success': False, 'error': 'Unauthorized'}), 401
            request.mobile_user_id = user_id
            return f(*args, **kwargs)
        # Fall back to session cookie
        if 'user_id' not in session:
            return jsonify({'success': False, 'error': 'Unauthorized'}), 401
        request.mobile_user_id = session['user_id']
        return f(*args, **kwargs)
    return decorated


@app.route('/api/mobile/debug', methods=['GET', 'POST'])
def mobile_debug():
    """Unprotected debug endpoint — returns token received and DB lookup result."""
    token = request.headers.get('X-Session-Token', '')
    result = {'token_received': token[:16] + '...' if token else 'NONE', 'user_id': None, 'error': None}
    if token:
        try:
            from database import get_db, DATABASE_URL
            db = get_db()
            if DATABASE_URL:
                import psycopg2.extras
                cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute("SELECT user_id FROM user_sessions WHERE session_token = %s", (token,))
                row = cur.fetchone(); cur.close()
            else:
                row = db.execute("SELECT user_id FROM user_sessions WHERE session_token = ?", (token,)).fetchone()
            db.close()
            result['user_id'] = row['user_id'] if row else None
            result['found'] = bool(row)
        except Exception as e:
            result['error'] = str(e)
    return jsonify(result)


@app.route('/api/mobile/send-otp', methods=['POST'])
def mobile_send_otp():
    """Send a 6-digit OTP to the user's email for password reset."""
    try:
        body = request.get_json() or {}
        email = body.get('email', '').strip().lower()
        if not email:
            return jsonify({'success': False, 'error': 'Email required'}), 400
        user = get_user_by_email(email)
        if not user:
            # Don't reveal whether account exists
            return jsonify({'success': True})
        import random as _random
        otp = str(_random.randint(100000, 999999))
        expires_at = datetime.utcnow() + timedelta(minutes=15)
        try:
            save_reset_token(email, otp, expires_at)
        except Exception:
            pass
        # Send email
        try:
            msg_body = f"""
            <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;background:#0a0a0a;color:#fff;">
              <h2 style="font-size:22px;font-weight:800;margin-bottom:8px;">Card<span style="color:#00e676;">Scan</span></h2>
              <p style="color:#aaa;margin-bottom:24px;">Password Reset Code</p>
              <p style="color:#ccc;margin-bottom:16px;">Enter this code in the app to reset your password:</p>
              <div style="background:#111;border:2px solid #00e676;border-radius:12px;padding:24px;text-align:center;margin-bottom:24px;">
                <span style="font-size:40px;font-weight:900;letter-spacing:8px;color:#00e676;">{otp}</span>
              </div>
              <p style="color:#666;font-size:12px;">Expires in 15 minutes. If you didn't request this, ignore this email.</p>
            </div>
            """
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            msg = MIMEMultipart('alternative')
            msg['Subject'] = 'CardScan — Your Reset Code'
            msg['From'] = f'CardScan <{GMAIL_USER}>'
            msg['To'] = email
            msg.attach(MIMEText(msg_body, 'html'))
            import smtplib
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.sendmail(GMAIL_USER, email, msg.as_string())
        except Exception:
            pass  # OTP is stored; email failure is silent
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/reset-password-otp', methods=['POST'])
def mobile_reset_password_otp():
    """Reset password using a 6-digit OTP."""
    try:
        body = request.get_json() or {}
        email = body.get('email', '').strip().lower()
        otp = body.get('otp', '').strip()
        new_password = body.get('new_password', '')
        if not email or not otp or not new_password:
            return jsonify({'success': False, 'error': 'Email, OTP, and new password required'}), 400
        if len(new_password) < 6:
            return jsonify({'success': False, 'error': 'Password must be at least 6 characters'}), 400
        row = get_reset_token(otp)
        if not row:
            return jsonify({'success': False, 'error': 'Invalid or expired code'}), 400
        stored_email = row.get('email', '') if isinstance(row, dict) else row[1]
        expires_at = row.get('expires_at') if isinstance(row, dict) else row[3]
        if stored_email.lower() != email:
            return jsonify({'success': False, 'error': 'Invalid or expired code'}), 400
        if expires_at:
            try:
                if isinstance(expires_at, str):
                    expires_at = datetime.fromisoformat(expires_at)
                if datetime.utcnow() > expires_at.replace(tzinfo=None):
                    delete_reset_token(otp)
                    return jsonify({'success': False, 'error': 'Code has expired, please request a new one'}), 400
            except Exception:
                pass
        update_password(email, generate_password_hash(new_password))
        delete_reset_token(otp)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/login', methods=['POST'])
def mobile_login():
    body = request.get_json()
    email = body.get('email', '').strip().lower()
    password = body.get('password', '').strip()
    user = get_user_by_email(email)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'success': False, 'error': 'Invalid email or password'}), 401
    import secrets as _secrets
    token = _secrets.token_hex(32)
    create_session(user['id'], token)
    return jsonify({
        'success': True,
        'session_token': token,
        'user': {'id': user['id'], 'email': user['email'], 'subscription_status': user.get('subscription_status', 'free')},
    })


@app.route('/api/mobile/signup', methods=['POST'])
def mobile_signup():
    body = request.get_json()
    email = body.get('email', '').strip().lower()
    password = body.get('password', '').strip()
    ref_code = (body.get('ref_code') or '').strip().upper()
    if not email or not password or len(password) < 6:
        return jsonify({'success': False, 'error': 'Valid email and password (min 6 chars) required'}), 400
    user = create_user(email, generate_password_hash(password))
    if not user:
        return jsonify({'success': False, 'error': 'An account with that email already exists'}), 409

    # Give every new mobile account its own referral code (same scheme the
    # web /signup form uses), and apply a redeemed code's bonus if one was sent.
    user_ref_code = email.split('@')[0].upper()[:6] + str(user['id'])
    _db_set_referral_code(user['id'], user_ref_code)
    referral_applied = False
    if ref_code:
        referrer = _db_get_user_by_referral_code(ref_code)
        if referrer and referrer['id'] != user['id']:
            _db_apply_referral(user['id'], referrer['id'], ref_code)
            referral_applied = True

    import secrets as _secrets
    token = _secrets.token_hex(32)
    create_session(user['id'], token)
    return jsonify({
        'success': True,
        'session_token': token,
        'user': {'id': user['id'], 'email': user['email'], 'subscription_status': 'free'},
        'referral_applied': referral_applied,
    })


@app.route('/api/mobile/scan', methods=['POST'])
@mobile_auth
def mobile_scan():
    """Accept an image from the mobile app (multipart or base64 JSON) and return card data."""
    try:
        import numpy as np

        # Accept multipart file upload (preferred) or base64 JSON (fallback)
        if request.files.get('image'):
            raw_image_bytes = request.files['image'].read()
            scan_mode = request.form.get('scan_mode', 'raw')
        else:
            body = request.get_json(force=True) or {}
            raw_image_bytes = base64.b64decode(body.get('image', ''))
            scan_mode = body.get('scan_mode', 'raw')

        nparr = np.frombuffer(raw_image_bytes, np.uint8)
        frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR) if raw_image_bytes else None

        if scan_mode == 'front_and_back':
            # Accept front + back images, merge results
            front_bytes = raw_image_bytes
            back_b64 = (request.get_json(force=True) or {}).get('back_image', '')
            back_bytes = base64.b64decode(back_b64) if back_b64 else None

            # Scan front
            data = analyze_card(frame, quality=95)
            is_raw = (not data.get('grade') or data.get('grade', '').lower() == 'raw')

            # Run second pass on front
            if is_raw:
                try:
                    raw_data = analyze_raw_card(front_bytes, year_hint=data.get('year'), sport_hint=data.get('sport'))
                    for field in ['year', 'brand']:
                        if raw_data.get(field): data[field] = raw_data[field]
                    for field in ['set', 'parallel', 'serial', 'card_number', 'sport']:
                        if raw_data.get(field) and not data.get(field): data[field] = raw_data[field]
                except Exception: pass

            # Scan back and merge
            if back_bytes:
                try:
                    back_data = analyze_card_back(back_bytes, year_hint=data.get('year'), sport_hint=data.get('sport'))
                    # Back fills in missing fields
                    for field in ['year', 'brand', 'set', 'name']:
                        if back_data.get(field) and not data.get(field):
                            data[field] = back_data[field]
                    for field in ['card_number', 'team', 'serial']:
                        if back_data.get(field) and not data.get(field):
                            data[field] = back_data[field]
                    if back_data.get('rookie'):
                        data['rookie'] = True
                except Exception: pass

            # Fix year for known rookies
            if data.get("name"):
                draft_year = get_player_draft_year(data["name"])
                if draft_year:
                    if data.get("year") and int(data["year"]) < draft_year:
                        data["year"] = draft_year
                    elif not data.get("year"):
                        data["year"] = draft_year

            # Fix parallel — Prizm base cards show silver foil but it's NOT a parallel
            card_set = (data.get("set") or "").lower()
            card_parallel = (data.get("parallel") or "").lower()
            if "prizm" in card_set and card_parallel in ("silver", "base", ""):
                # Only null out if no colored border — silver is default Prizm finish
                data["parallel"] = None

            # Strip exclamation points from all text fields
            for field in ['name', 'brand', 'set', 'parallel', 'card', 'sport', 'team']:
                val = data.get(field)
                if val and isinstance(val, str):
                    data[field] = val.replace('!', '').strip()

            # Rebuild card description
            parts = [str(data.get('year') or ''), data.get('brand') or '', data.get('set') or '',
                     data.get('name') or '', data.get('parallel') or '']
            data['card'] = ' '.join(p for p in parts if p).strip()

            allowed, scans_used, limit = check_and_increment_scans(request.mobile_user_id)
            if not allowed:
                return jsonify({'success': False, 'limit_reached': True, 'error': f'Free limit reached ({limit} scans/day).'})

            try:
                query_parts = [p for p in [str(data.get('year') or ''), data.get('name', ''), data.get('grade', '')] if p]
                ebay_result, _ = search_ebay_sold(' '.join(query_parts))
                if ebay_result and ebay_result.get('avg'):
                    data['ebay_avg'] = ebay_result['avg']
            except Exception: pass

            save_scan(request.mobile_user_id, data)
            data['success'] = True
            data['scans_left'] = max(0, limit - scans_used) if limit else 999
            return jsonify(data)

        elif scan_mode == 'bulk':
            try:
                # Resize bulk image to max 1200px before sending to Gemini — reduces timeout risk
                if frame is not None:
                    h, w = frame.shape[:2]
                    max_dim = 1200
                    if max(h, w) > max_dim:
                        scale = max_dim / max(h, w)
                        frame_small = cv2.resize(frame, (int(w * scale), int(h * scale)))
                    else:
                        frame_small = frame
                    _, buf = cv2.imencode('.jpg', frame_small, [cv2.IMWRITE_JPEG_QUALITY, 82])
                    bulk_image_bytes = buf.tobytes()
                else:
                    bulk_image_bytes = raw_image_bytes
                cards = analyze_bulk(bulk_image_bytes)
                if isinstance(cards, dict):
                    cards = [cards]
                if not cards:
                    return jsonify({'success': False, 'error': 'No cards detected — try better lighting or fewer cards'})
            except Exception as e:
                app.logger.error(f'Bulk scan failed: {e}')
                return jsonify({'success': False, 'error': f'Bulk scan failed: {str(e)}'}), 500

            allowed, scans_used, limit = check_and_increment_scans(request.mobile_user_id)
            if not allowed:
                return jsonify({'success': False, 'limit_reached': True, 'error': f'Free limit reached ({limit} scans/day).'})
            # Save each card to the DB so they appear in collection with IDs
            for card in cards:
                try:
                    card_id = save_scan(request.mobile_user_id, card)
                    if card_id:
                        card['id'] = card_id
                except Exception as e:
                    app.logger.warning(f'Bulk save_scan failed: {e}')
            return jsonify({'success': True, 'bulk': True, 'cards': cards})
        elif scan_mode == 'graded':
            data = analyze_label(raw_image_bytes)
            data['grade'] = data.get('grade') or 'Unknown'
        else:
            data = analyze_card(frame, quality=95)
            is_raw = (not data.get('grade') or data.get('grade', '').lower() == 'raw')
            if is_raw:
                try:
                    raw_data = analyze_raw_card(raw_image_bytes, year_hint=data.get('year'), sport_hint=data.get('sport'))
                    for field in ['year', 'brand']:
                        if raw_data.get(field): data[field] = raw_data[field]
                    for field in ['set', 'parallel', 'serial', 'card_number', 'sport']:
                        if raw_data.get(field) and not data.get(field): data[field] = raw_data[field]
                except Exception: pass
                # Last resort: if year still missing, crop bottom strip and read copyright line directly
                if not data.get('year'):
                    try:
                        year = extract_year_from_copyright(raw_image_bytes)
                        if year: data['year'] = year
                    except Exception: pass

        # ── Post-scan corrections (all scan modes) ──────────────────────────

        # 1. Year fix — auto-fill from draft class for known rookies
        if data.get('name'):
            draft_year = get_player_draft_year(data['name'])
            if draft_year:
                current_year = data.get('year')
                if not current_year or (current_year and int(current_year) < draft_year):
                    data['year'] = draft_year

        # 2. Parallel fix — strip anything that's not a pure color word
        VALID_COLORS = {'gold','red','blue','green','purple','orange','pink','black','white','aqua','teal','yellow','brown','bronze','copper'}
        NON_COLORS = {'silver','refractor','base','rainbow','shimmer','foil','holo','prizm','chrome','cracked','hyper','disco','atomic','prism','press proof','courtside','tie-dye','sparkle'}
        raw_parallel = (data.get('parallel') or '').strip()
        if raw_parallel:
            p_lower = raw_parallel.lower()
            # Keep it only if it contains a valid color and no non-color junk
            has_color = any(c in p_lower for c in VALID_COLORS)
            has_junk = any(n in p_lower for n in NON_COLORS)
            if not has_color or has_junk:
                data['parallel'] = None
            else:
                # Clean it to just the color word(s)
                data['parallel'] = raw_parallel

        # Strip exclamation points and stray punctuation from all text fields
        for field in ['name', 'brand', 'set', 'parallel', 'card', 'sport', 'team']:
            val = data.get(field)
            if val and isinstance(val, str):
                data[field] = val.replace('!', '').strip()

        # Rebuild card description with corrected fields
        parts = [str(data.get('year') or ''), data.get('brand') or '', data.get('set') or '',
                 data.get('name') or '', data.get('parallel') or '']
        data['card'] = ' '.join(p for p in parts if p).strip()

        allowed, scans_used, limit = check_and_increment_scans(request.mobile_user_id)
        if not allowed:
            return jsonify({'success': False, 'limit_reached': True, 'error': f'Free limit reached ({limit} scans/day). Upgrade to Pro for unlimited scans.'})

        # eBay value lookup
        try:
            query_parts = [p for p in [str(data.get('year') or ''), data.get('name', ''), data.get('grade', '')] if p]
            ebay_result, _ = search_ebay_sold(' '.join(query_parts))
            if ebay_result and ebay_result.get('avg'):
                data['ebay_avg'] = ebay_result['avg']
                data['ebay_sales'] = ebay_result.get('sales', [])
        except Exception: pass

        save_scan(request.mobile_user_id, data)
        data['success'] = True
        data['scans_left'] = max(0, limit - scans_used) if limit else 999
        return jsonify(data)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/add-to-sheet', methods=['POST'])
@mobile_auth
def mobile_add_to_sheet():
    """Log a card to the user's Google Sheet."""
    try:
        body = request.get_json()
        user = get_user_by_id(request.mobile_user_id)
        append_to_sheet(body, user=user)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/mobile/collection', methods=['GET'])
@mobile_auth
def mobile_collection():
    """Return the user's scan history as a card collection."""
    try:
        scans, total = get_scan_history(request.mobile_user_id, limit=100, offset=0)
        cards = []
        for s in scans:
            cards.append({
                'id': s.get('id'),
                'name': s.get('name'),
                'card': s.get('card'),
                'year': s.get('year'),
                'brand': s.get('brand'),
                'set': s.get('set_name'),
                'parallel': s.get('parallel'),
                'grade': s.get('grade'),
                'cert': s.get('cert'),
                'ebay_avg': s.get('ebay_avg'),
                'paid_price': s.get('paid_price'),
                'scanned_at': str(s.get('scanned_at', ''))[:10],
            })
        return jsonify({'success': True, 'cards': cards, 'total': total})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/create-deal-payment', methods=['POST'])
@mobile_auth
def mobile_create_deal_payment():
    """Create a Stripe Checkout session for the 1% deal fee."""
    try:
        body = request.get_json() or {}
        sale_price = float(body.get('sale_price', 0))
        card_name  = body.get('card_name', 'Card')
        card_desc  = body.get('card_desc', '')
        buyer_instagram = body.get('buyer_instagram', '')
        buyer_name = body.get('buyer_name', '')

        if sale_price <= 0:
            return jsonify({'success': False, 'error': 'Invalid sale price'}), 400

        fee_cents = max(int(round(sale_price * 0.01 * 100)), 50)  # min $0.50 (Stripe minimum)

        # Save pending deal to DB
        db = get_db()
        deal_id = None
        try:
            if DATABASE_URL:
                cur = db.cursor()
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS deals (
                        id SERIAL PRIMARY KEY, user_id INTEGER,
                        card_name TEXT, card_desc TEXT,
                        buyer_instagram TEXT, buyer_name TEXT,
                        sale_price FLOAT, fee_amount FLOAT,
                        stripe_session_id TEXT, status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT NOW()
                    )
                """)
                cur.execute(
                    "INSERT INTO deals (user_id, card_name, card_desc, buyer_instagram, buyer_name, sale_price, fee_amount) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (request.mobile_user_id, card_name, card_desc, buyer_instagram, buyer_name, sale_price, fee_cents/100)
                )
                deal_id = cur.fetchone()[0]
                db.commit()
                cur.close()
            db.close()
        except Exception:
            pass

        session_obj = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'usd',
                    'product_data': {
                        'name': f'CardScan Deal Fee — {card_name}',
                        'description': f'1% fee on ${sale_price:.2f} sale via CardConnect to {buyer_name}',
                    },
                    'unit_amount': fee_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            success_url=f'{APP_BASE_URL}/deal-success?deal_id={deal_id}&session_id={{CHECKOUT_SESSION_ID}}',
            cancel_url=f'{APP_BASE_URL}/deal-cancel',
            metadata={
                'deal_id': str(deal_id or ''),
                'user_id': str(request.mobile_user_id),
                'buyer_instagram': buyer_instagram,
                'sale_price': str(sale_price),
            }
        )
        return jsonify({'success': True, 'checkout_url': session_obj.url, 'deal_id': deal_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/collection/<int:user_id>')
def public_collection(user_id):
    """Public collection page for sharing."""
    try:
        scans, total = get_scan_history(user_id, limit=20, offset=0)
        user = get_user_by_id(user_id)
        if not user: return "Not found", 404
        total_value = sum(float(s.get('ebay_avg') or 0) for s in scans)
        def card_row(s):
            val = f"${s['ebay_avg']}" if s.get('ebay_avg') else (s.get('grade') or 'Raw')
            return (
                f"<div style='background:#111;border:1px solid #1e1e1e;border-radius:12px;padding:14px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center'>"
                f"<div><div style='font-weight:800;color:#fff;font-size:14px'>{s.get('name','')}</div>"
                f"<div style='color:#555;font-size:11px;margin-top:2px'>{s.get('card','')}</div></div>"
                f"<div style='color:#00e676;font-weight:800;font-size:14px'>{val}</div></div>"
            )
        card_rows = ''.join([card_row(s) for s in scans])
        return f"""<!DOCTYPE html><html>
<head><meta charset=utf-8><meta name="viewport" content="width=device-width,initial-scale=1">
<title>CardScan Collection</title>
<style>body{{font-family:-apple-system,sans-serif;background:#0a0a0a;color:#fff;max-width:480px;margin:0 auto;padding:24px}}</style>
</head><body>
<div style='font-size:24px;font-weight:900;margin-bottom:4px'>Card<span style='color:#00e676'>Scan</span></div>
<div style='color:#555;font-size:12px;margin-bottom:24px'>Collection · {total} cards</div>
<div style='display:flex;gap:20px;margin-bottom:24px'>
  <div><div style='font-size:22px;font-weight:900;color:#00e676'>{total}</div><div style='font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.5px'>Cards</div></div>
  <div><div style='font-size:22px;font-weight:900;color:#00e676'>${total_value:,.0f}</div><div style='font-size:10px;color:#444;text-transform:uppercase;letter-spacing:0.5px'>Est. Value</div></div>
</div>
{card_rows}
<div style='text-align:center;margin-top:24px;color:#444;font-size:12px'>Powered by <a href="https://cardscan.live" style="color:#00e676">CardScan</a></div>
</body></html>"""
    except Exception as e:
        return f"Error: {e}", 500


@app.route('/deal-success')
def deal_success():
    deal_id = request.args.get('deal_id')
    session_id = request.args.get('session_id')
    if deal_id and session_id:
        try:
            db = get_db()
            if DATABASE_URL:
                cur = db.cursor()
                cur.execute("UPDATE deals SET status='completed', stripe_session_id=%s WHERE id=%s", (session_id, deal_id))
                db.commit()
                cur.close()
            db.close()
        except Exception:
            pass
    return '<html><body style="background:#0a0a0a;color:#00e676;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-size:24px;font-weight:900;">✓ Deal confirmed! Return to CardScan.</body></html>'


@app.route('/deal-cancel')
def deal_cancel():
    return '<html><body style="background:#0a0a0a;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;font-size:18px;">Payment cancelled. Return to CardScan.</body></html>'


@app.route('/api/mobile/report-deal', methods=['POST'])
@mobile_auth
def mobile_report_deal():
    """Log a self-reported deal for CardConnect analytics (free)."""
    try:
        body = request.get_json() or {}
        sale_price  = float(body.get('sale_price', 0))
        card_name   = body.get('card_name', '')
        card_desc   = body.get('card_desc', '')
        buyer_ig    = body.get('buyer_instagram', '')
        buyer_name  = body.get('buyer_name', '')
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS deals (
                    id SERIAL PRIMARY KEY, user_id INTEGER,
                    card_name TEXT, card_desc TEXT,
                    buyer_instagram TEXT, buyer_name TEXT,
                    sale_price FLOAT, fee_amount FLOAT,
                    stripe_session_id TEXT, status TEXT DEFAULT 'reported',
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
            cur.execute(
                "INSERT INTO deals (user_id,card_name,card_desc,buyer_instagram,buyer_name,sale_price,status) VALUES (%s,%s,%s,%s,%s,%s,'reported')",
                (request.mobile_user_id, card_name, card_desc, buyer_ig, buyer_name, sale_price)
            )
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/collection/<int:card_id>', methods=['DELETE'])
@mobile_auth
def mobile_delete_card(card_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("DELETE FROM scan_history WHERE id = %s AND user_id = %s", (card_id, request.mobile_user_id))
            db.commit()
            cur.close()
        else:
            db.execute("DELETE FROM scan_history WHERE id = ? AND user_id = ?", (card_id, request.mobile_user_id))
            db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/clear-collection', methods=['POST'])
@mobile_auth
def mobile_clear_collection():
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("DELETE FROM scan_history WHERE user_id = %s", (request.mobile_user_id,))
            db.commit()
            cur.close()
        else:
            db.execute("DELETE FROM scan_history WHERE user_id = ?", (request.mobile_user_id,))
            db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/sheet-preview', methods=['GET'])
@mobile_auth
def mobile_sheet_preview():
    """Return a live preview of the user's Google Sheet rows + stats."""
    try:
        user = get_user_by_id(request.mobile_user_id)
        sheet_id = user.get('google_sheet_id') or SPREADSHEET_ID
        if not sheet_id:
            return jsonify({'success': False, 'error': 'No sheet connected'})

        svc = get_user_sheets_service(user)
        tab = get_first_sheet_tab(sheet_id, svc)
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id, range=f"{tab}!A:Z"
        ).execute()
        all_rows = result.get('values', [])
        if not all_rows:
            return jsonify({'success': True, 'rows': [], 'stats': {}, 'sheet_url': f'https://docs.google.com/spreadsheets/d/{sheet_id}'})

        headers = all_rows[0]
        mapping = detect_column_mapping(headers)
        data_rows = all_rows[1:]

        def cell(row, field):
            idx = mapping.get(field)
            if idx is not None and idx < len(row):
                return row[idx]
            return ''

        rows = []
        today = datetime.utcnow().strftime('%Y-%m-%d')
        today_count = 0
        total_value = 0
        graded_count = 0

        for row in reversed(data_rows[-50:]):  # last 50 rows, newest first
            grade = cell(row, 'grade')
            value_str = cell(row, 'value')
            rows.append({
                'card': cell(row, 'card'),
                'name': cell(row, 'name'),
                'year': cell(row, 'year'),
                'set': cell(row, 'set'),
                'grade': grade,
                'value': value_str,
            })
            if grade and grade.lower() not in ('raw', ''):
                graded_count += 1
            if value_str:
                try:
                    total_value += float(value_str.replace('$', '').replace(',', ''))
                except Exception:
                    pass

        return jsonify({
            'success': True,
            'rows': rows[:10],
            'sheet_name': tab,
            'sheet_url': f'https://docs.google.com/spreadsheets/d/{sheet_id}',
            'stats': {
                'total_cards': len(data_rows),
                'total_value': round(total_value, 2),
                'graded_count': graded_count,
                'today_count': today_count,
            },
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/scan') or request.is_json:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/scan') or request.is_json:
        return jsonify({'success': False, 'error': 'Server error — please try again'}), 500
    return render_template('500.html'), 500

@app.route('/api/mobile/user', methods=['GET'])
@mobile_auth
def mobile_user():
    user = get_user_by_id(request.mobile_user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    email = user.get('email', '')
    is_pro = user.get('subscription_status') == 'pro'
    name = email.split('@')[0].capitalize() if email else 'User'

    # Backfill a referral code for accounts created before mobile signup
    # started generating one (or via the pre-fix code path).
    referral_code = user.get('referral_code') or ''
    if not referral_code and email:
        referral_code = email.split('@')[0].upper()[:6] + str(user['id'])
        try:
            _db_set_referral_code(user['id'], referral_code)
        except Exception:
            referral_code = ''  # don't surface a code we failed to persist

    return jsonify({
        'email': email,
        'name': name,
        'is_pro': is_pro,
        'subscription_status': user.get('subscription_status', 'free'),
        'referral_code': referral_code,
        'google_connected': bool(user.get('google_access_token')),
        'google_sheet_id': user.get('google_sheet_id') or '',
    })

@app.route("/api/mobile/register-push", methods=["POST"])
@mobile_auth
def mobile_register_push():
    """Store the user's Expo push token for price alert notifications."""
    try:
        body = request.get_json(force=True) or {}
        token = (body.get("token") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "No token"}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET push_token = %s WHERE id = %s", (token, request.mobile_user_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/mobile/sheet-tabs", methods=["GET"])
@mobile_auth
def mobile_sheet_tabs():
    """Return all tab names for the user's connected sheet."""
    try:
        user = get_user_by_id(request.mobile_user_id)
        sheet_id = user.get("google_sheet_id") if user else None
        if not sheet_id:
            return jsonify({"tabs": [], "current_tab": None})
        svc = get_user_sheets_service(user)
        tabs = get_all_sheet_tabs(sheet_id, svc)
        return jsonify({"tabs": tabs, "current_tab": user.get("sheet_tab") or (tabs[0] if tabs else None)})
    except Exception as e:
        return jsonify({"tabs": [], "current_tab": None, "error": str(e)})

@app.route("/api/mobile/set-sheet-tab", methods=["POST"])
@mobile_auth
def mobile_set_sheet_tab():
    """Set the user's preferred sheet tab."""
    try:
        body = request.get_json(force=True) or {}
        tab = (body.get("tab") or "").strip()
        if not tab:
            return jsonify({"success": False, "error": "No tab name provided"}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE users SET sheet_tab = %s WHERE id = %s", (tab, request.mobile_user_id))
        conn.commit(); cur.close(); conn.close()
        return jsonify({"success": True, "tab": tab})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/mobile/sheet-service-email", methods=["GET"])
@mobile_auth
def mobile_sheet_service_email():
    """Return the service account email so users know who to share their sheet with."""
    try:
        # Try env var first (fastest)
        email = SHEET_SERVICE_EMAIL
        if not email:
            b64 = os.environ.get("GOOGLE_CREDS_B64", "")
            if b64:
                creds_dict = json.loads(base64.b64decode(b64 + "==").decode("utf-8"))
                email = creds_dict.get("client_email", "")
            elif os.path.exists(GOOGLE_CREDS_FILE):
                creds_dict = json.load(open(GOOGLE_CREDS_FILE))
                email = creds_dict.get("client_email", "")
        return jsonify({"email": email})
    except Exception as e:
        app.logger.error(f"sheet-service-email error: {e}")
        return jsonify({"email": ""}), 500

@app.route("/api/mobile/set-sheet", methods=["POST"])
@mobile_auth
def mobile_set_sheet():
    """Validate sheet access and save the sheet ID for the logged-in user."""
    try:
        body = request.get_json(force=True) or {}
        sheet_input = (body.get("sheet_id") or "").strip()
        if not sheet_input:
            return jsonify({"success": False, "error": "No sheet ID provided"}), 400
        sheet_id = extract_sheet_id(sheet_input)
        if not sheet_id:
            return jsonify({"success": False, "error": "Invalid sheet URL or ID"}), 400
        # Verify the service account can actually access this sheet
        try:
            from googleapiclient.discovery import build
            creds = get_creds()
            svc = build("sheets", "v4", credentials=creds)
            meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
            sheet_title = meta.get("properties", {}).get("title", "Your Sheet")
        except Exception:
            return jsonify({"success": False, "error": "Could not access this sheet. Make sure you shared it with the CardScan service account email."}), 400
        save_google_sheet_id(request.mobile_user_id, sheet_id)
        return jsonify({"success": True, "sheet_id": sheet_id, "sheet_title": sheet_title})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/mobile/scan-back", methods=["POST"])
@app.route('/api/mobile/scan-back', methods=['POST'])
@mobile_auth
def mobile_scan_back():
    """Scan the back of a card to improve eBay price accuracy."""
    try:
        body = request.get_json(force=True) or {}
        image_b64 = body.get('image', '')
        if not image_b64:
            return jsonify({'success': False, 'error': 'No image provided'}), 400

        image_bytes = base64.b64decode(image_b64)

        # Hints from the front scan
        name  = body.get('name', '')
        year  = body.get('year')
        set_  = body.get('set', '')
        grade = body.get('grade', '')
        serial = body.get('serial', '')
        sport = body.get('sport', '')

        back_data = analyze_card_back(image_bytes, year_hint=year, sport_hint=sport)

        # Merge back data into the card metadata, back fills missing fields
        merged = {
            'name':   name or back_data.get('name', ''),
            'year':   year or back_data.get('year'),
            'set':    set_ or back_data.get('set', ''),
            'grade':  grade,
            'serial': serial or back_data.get('serial', ''),
        }
        if back_data.get('rookie'):
            merged['rookie'] = True

        # Build a richer eBay query using back data
        query_parts = [
            str(merged.get('year') or ''),
            merged.get('name', ''),
            merged.get('set', ''),
            merged.get('grade', ''),
            back_data.get('card_number', ''),
        ]
        q = ' '.join(p for p in query_parts if p).strip()

        ebay_avg = None
        if q:
            ebay_result, _ = search_ebay_sold(q)
            if ebay_result:
                ebay_avg = ebay_result.get('avg')

        return jsonify({
            'success': True,
            'ebay_avg': ebay_avg,
            'back_data': back_data,
            'merged': merged,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/collection/<int:card_id>/refresh', methods=['POST'])
@mobile_auth
def mobile_refresh_card_value(card_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT * FROM scan_history WHERE id = %s AND user_id = %s", (card_id, request.mobile_user_id))
            row = cur.fetchone()
        else:
            row = db.execute("SELECT * FROM scan_history WHERE id = ? AND user_id = ?", (card_id, request.mobile_user_id)).fetchone()
        if not row:
            return jsonify({'success': False, 'error': 'Card not found'}), 404
        card = dict(row) if hasattr(row, 'keys') else dict(zip([d[0] for d in (cur if DATABASE_URL else db).description], row))
        query_parts = [str(card.get('year') or ''), card.get('name') or '', card.get('set_name') or '', card.get('grade') or '']
        q = ' '.join(p for p in query_parts if p).strip()
        new_avg = None
        if q:
            ebay_result, _ = search_ebay_sold(q)
            if ebay_result:
                new_avg = ebay_result.get('avg')
        if new_avg and DATABASE_URL:
            cur.execute("UPDATE scan_history SET ebay_avg = %s WHERE id = %s", (new_avg, card_id))
            db.commit()
            cur.close()
        elif new_avg:
            db.execute("UPDATE scan_history SET ebay_avg = ? WHERE id = ?", (new_avg, card_id))
            db.commit()
        db.close()
        return jsonify({'success': True, 'ebay_avg': new_avg})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/collection/refresh-all', methods=['POST'])
@mobile_auth
def mobile_refresh_all_values():
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT id, name, year, set_name, grade FROM scan_history WHERE user_id = %s", (request.mobile_user_id,))
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
        else:
            rows = db.execute("SELECT id, name, year, set_name, grade FROM scan_history WHERE user_id = ?", (request.mobile_user_id,)).fetchall()
            cols = ['id', 'name', 'year', 'set_name', 'grade']
        updated = 0
        for row in rows:
            card = dict(zip(cols, row))
            query_parts = [str(card.get('year') or ''), card.get('name') or '', card.get('set_name') or '', card.get('grade') or '']
            q = ' '.join(p for p in query_parts if p).strip()
            if not q:
                continue
            try:
                ebay_result, _ = search_ebay_sold(q)
                if ebay_result and ebay_result.get('avg'):
                    avg = ebay_result['avg']
                    if DATABASE_URL:
                        cur.execute("UPDATE scan_history SET ebay_avg = %s WHERE id = %s", (avg, card['id']))
                    else:
                        db.execute("UPDATE scan_history SET ebay_avg = ? WHERE id = ?", (avg, card['id']))
                    updated += 1
            except Exception:
                continue
        if DATABASE_URL:
            db.commit()
            cur.close()
        else:
            db.commit()
        db.close()
        return jsonify({'success': True, 'updated': updated})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# PROFILE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/mobile/profile', methods=['GET'])
@mobile_auth
def get_profile():
    try:
        user = get_user_by_id(request.mobile_user_id)
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 404

        from database import get_db, DATABASE_URL
        db = get_db()

        # Get stats
        total_scans = 0
        collection_count = 0
        total_value = 0.0
        listings_count = 0

        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT COUNT(*), COALESCE(SUM(ebay_avg), 0) FROM scan_history WHERE user_id = %s", (request.mobile_user_id,))
            row = cur.fetchone()
            collection_count = row[0] or 0
            total_value = float(row[1] or 0)
            cur.execute("SELECT COUNT(*) FROM marketplace_listings WHERE user_id = %s AND status = 'active'", (request.mobile_user_id,))
            listings_count = cur.fetchone()[0] or 0
            cur.close()
        db.close()

        return jsonify({
            'success': True,
            'profile': {
                'id': user.get('id'),
                'email': user.get('email'),
                'username': user.get('username'),
                'profile_pic_url': user.get('profile_pic_url'),
                'bio': user.get('bio'),
                'created_at': str(user.get('created_at', '')),
                'total_scans': user.get('total_scans', 0),
                'career_scans': user.get('career_scans', user.get('total_scans', 0)),
                'collection_count': collection_count,
                'total_value': round(total_value, 2),
                'listings_count': listings_count,
                'subscription_status': user.get('subscription_status', 'free'),
            }
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _claim_buyer_placeholder_pg(cur, placeholder_id, real_user_id):
    """Move a placeholder Connect-buyer account's chat rooms/messages onto the
    real user_id claiming that username. Leaves the placeholder user row in
    place (unusable login, no username) rather than deleting it, so we don't
    have to worry about other tables that might still reference it."""
    cur.execute("SELECT room_id FROM chat_room_members WHERE user_id = %s", (placeholder_id,))
    placeholder_rooms = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT room_id FROM chat_room_members WHERE user_id = %s", (real_user_id,))
    real_user_rooms = {r[0] for r in cur.fetchall()}
    for room_id in placeholder_rooms:
        if room_id in real_user_rooms:
            # Real user is already in this room somehow -- just drop the stale placeholder row.
            cur.execute("DELETE FROM chat_room_members WHERE room_id = %s AND user_id = %s", (room_id, placeholder_id))
        else:
            cur.execute("UPDATE chat_room_members SET user_id = %s WHERE room_id = %s AND user_id = %s",
                        (real_user_id, room_id, placeholder_id))
    cur.execute("UPDATE chat_messages SET sender_id = %s WHERE sender_id = %s", (real_user_id, placeholder_id))
    cur.execute("UPDATE chat_rooms SET created_by = %s WHERE created_by = %s", (real_user_id, placeholder_id))
    cur.execute("UPDATE users SET username = NULL WHERE id = %s", (placeholder_id,))


def _claim_buyer_placeholder_sqlite(db, placeholder_id, real_user_id):
    """SQLite-fallback equivalent of _claim_buyer_placeholder_pg, for local dev."""
    placeholder_rooms = [r[0] for r in db.execute(
        "SELECT room_id FROM chat_room_members WHERE user_id = ?", (placeholder_id,)).fetchall()]
    real_user_rooms = {r[0] for r in db.execute(
        "SELECT room_id FROM chat_room_members WHERE user_id = ?", (real_user_id,)).fetchall()}
    for room_id in placeholder_rooms:
        if room_id in real_user_rooms:
            db.execute("DELETE FROM chat_room_members WHERE room_id = ? AND user_id = ?", (room_id, placeholder_id))
        else:
            db.execute("UPDATE chat_room_members SET user_id = ? WHERE room_id = ? AND user_id = ?",
                       (real_user_id, room_id, placeholder_id))
    db.execute("UPDATE chat_messages SET sender_id = ? WHERE sender_id = ?", (real_user_id, placeholder_id))
    db.execute("UPDATE chat_rooms SET created_by = ? WHERE created_by = ?", (real_user_id, placeholder_id))
    db.execute("UPDATE users SET username = NULL WHERE id = ?", (placeholder_id,))


@app.route('/api/mobile/profile/update', methods=['POST'])
@mobile_auth
def update_profile():
    try:
        body = request.get_json() or {}
        username = body.get('username', '').strip().lower().replace(' ', '_')
        bio = body.get('bio', '').strip()
        profile_pic_url = body.get('profile_pic_url', '').strip()

        # Validate username
        import re
        if username and not re.match(r'^[a-z0-9_\.]{3,30}$', username):
            return jsonify({'success': False, 'error': 'Username must be 3-30 characters, letters/numbers/underscores only'}), 400

        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            if username:
                cur.execute("SELECT id, email FROM users WHERE username = %s AND id != %s", (username, request.mobile_user_id))
                existing = cur.fetchone()
                if existing:
                    existing_id, existing_email = existing[0], existing[1]
                    if existing_email and existing_email.startswith('buyer+') and existing_email.endswith('@buyers.slabvault.internal'):
                        # This username belongs to a lazily-created Connect-buyer placeholder
                        # account, not a real user -- the real person is claiming their profile.
                        # Move the placeholder's chat history onto the real account instead of
                        # blocking the signup on "username taken".
                        _claim_buyer_placeholder_pg(cur, existing_id, request.mobile_user_id)
                    else:
                        cur.close(); db.close()
                        return jsonify({'success': False, 'error': 'Username already taken'}), 409
            fields, vals = [], []
            if username: fields.append("username = %s"); vals.append(username)
            if bio is not None: fields.append("bio = %s"); vals.append(bio)
            if profile_pic_url: fields.append("profile_pic_url = %s"); vals.append(profile_pic_url)
            if fields:
                vals.append(request.mobile_user_id)
                cur.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = %s", vals)
                db.commit()
            cur.close()
        else:
            # SQLite fallback
            if username:
                dup = db.execute("SELECT id, email FROM users WHERE username = ? AND id != ?", (username, request.mobile_user_id)).fetchone()
                if dup:
                    existing_id, existing_email = dup[0], dup[1]
                    if existing_email and existing_email.startswith('buyer+') and existing_email.endswith('@buyers.slabvault.internal'):
                        _claim_buyer_placeholder_sqlite(db, existing_id, request.mobile_user_id)
                    else:
                        db.close()
                        return jsonify({'success': False, 'error': 'Username already taken'}), 409
            fields, vals = [], []
            if username: fields.append("username = ?"); vals.append(username)
            if bio is not None: fields.append("bio = ?"); vals.append(bio)
            if profile_pic_url: fields.append("profile_pic_url = ?"); vals.append(profile_pic_url)
            if fields:
                vals.append(request.mobile_user_id)
                db.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", vals)
                db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/profile/upload-pic', methods=['POST'])
@mobile_auth
def upload_profile_pic():
    """Accept base64 image, store as static file, return URL."""
    try:
        body = request.get_json() or {}
        image_b64 = body.get('image', '')
        if not image_b64:
            return jsonify({'success': False, 'error': 'No image provided'}), 400

        import base64, uuid
        img_data = base64.b64decode(image_b64)
        filename = f"profile_{request.mobile_user_id}_{uuid.uuid4().hex[:8]}.jpg"
        path = os.path.join(UPLOAD_ROOT, 'profiles', filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(img_data)

        url = f"{APP_BASE_URL}/uploads/profiles/{filename}"
        # Save to user record
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE users SET profile_pic_url = %s WHERE id = %s", (url, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True, 'url': url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/profile/check-username', methods=['GET'])
@mobile_auth
def check_username():
    username = request.args.get('username', '').strip().lower()
    if not username:
        return jsonify({'available': False})
    from database import get_db, DATABASE_URL
    db = get_db()
    available = True
    if DATABASE_URL:
        cur = db.cursor()
        cur.execute("SELECT id, email FROM users WHERE username = %s AND id != %s", (username, request.mobile_user_id))
        row = cur.fetchone()
        if row is None:
            available = True
        else:
            existing_email = row[1]
            # A Connect-buyer placeholder holding this username doesn't count as "taken" --
            # claiming it (via profile/update) merges its chat history onto the real account.
            available = bool(existing_email and existing_email.startswith('buyer+') and existing_email.endswith('@buyers.slabvault.internal'))
        cur.close()
    db.close()
    return jsonify({'available': available})


# ─────────────────────────────────────────────────────────────────────────────
# CHAT ENDPOINTS (DMs + Group Chats)
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/mobile/connect/buyer-user', methods=['POST'])
@mobile_auth
def get_or_create_buyer_user():
    """Get (or lazily create) a placeholder account for a curated Connect buyer,
    so users can message them through the real in-app chat system. Buyers in the
    curated list aren't SlabVault users -- this gives each one a stable user_id
    to attach a chat room to, keyed by their Instagram handle."""
    try:
        body = request.get_json() or {}
        instagram = (body.get('instagram') or '').strip().lstrip('@').lower()
        name = (body.get('name') or instagram).strip()
        if not instagram:
            return jsonify({'success': False, 'error': 'instagram required'}), 400

        buyer_email = f"buyer+{instagram}@buyers.slabvault.internal"
        user = get_user_by_email(buyer_email)
        if not user:
            password_hash = generate_password_hash(secrets.token_hex(32))
            user = create_user(buyer_email, password_hash)
            if not user:
                return jsonify({'success': False, 'error': 'Could not create buyer account'}), 500
            from database import get_db, DATABASE_URL
            db = get_db()
            if DATABASE_URL:
                cur = db.cursor()
                cur.execute("UPDATE users SET username = %s WHERE id = %s", (instagram, user['id']))
                db.commit(); cur.close()
            db.close()

        return jsonify({'success': True, 'user_id': user['id'], 'username': instagram})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/subscription/sync', methods=['POST'])
@mobile_auth
def sync_subscription():
    """Re-check this user's subscriber status directly with RevenueCat's server
    (never trust a client-claimed status) and update our own subscription_status.
    Called right after a purchase/restore so the app reflects Pro instantly
    instead of waiting on the webhook."""
    try:
        if not REVENUECAT_SECRET_KEY:
            return jsonify({'success': False, 'error': 'RevenueCat not configured'}), 503

        user_id = request.mobile_user_id
        resp = requests.get(
            f'https://api.revenuecat.com/v1/subscribers/{user_id}',
            headers={'Authorization': f'Bearer {REVENUECAT_SECRET_KEY}'},
            timeout=10,
        )
        if resp.status_code != 200:
            return jsonify({'success': False, 'error': f'RevenueCat lookup failed ({resp.status_code})'}), 502

        entitlements = resp.json().get('subscriber', {}).get('entitlements', {})
        pro_entitlement = entitlements.get('SlabVault Pro')
        is_active = False
        if pro_entitlement:
            expires = pro_entitlement.get('expires_date')
            is_active = expires is None or datetime.fromisoformat(expires.replace('Z', '+00:00')) > datetime.now(timezone.utc)

        new_status = 'pro' if is_active else 'free'
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE users SET subscription_status = %s WHERE id = %s", (new_status, user_id))
            db.commit(); cur.close()
        db.close()

        return jsonify({'success': True, 'subscription_status': new_status})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/webhooks/revenuecat', methods=['POST'])
def revenuecat_webhook():
    """RevenueCat calls this on every subscription lifecycle event. Configure the
    same value in RevenueCat's dashboard (Project Settings -> Integrations ->
    Webhooks -> Authorization header) as REVENUECAT_WEBHOOK_SECRET here."""
    try:
        auth_header = request.headers.get('Authorization', '')
        if not REVENUECAT_WEBHOOK_SECRET or auth_header != f'Bearer {REVENUECAT_WEBHOOK_SECRET}':
            return jsonify({'error': 'Unauthorized'}), 401

        body = request.get_json(force=True) or {}
        event = body.get('event', {})
        event_type = event.get('type')
        app_user_id = event.get('app_user_id')
        if not app_user_id or not app_user_id.isdigit():
            return jsonify({'success': True}), 200  # ignore anonymous/test events

        ACTIVATING = {'INITIAL_PURCHASE', 'RENEWAL', 'UNCANCELLATION', 'PRODUCT_CHANGE'}
        DEACTIVATING = {'EXPIRATION'}
        new_status = 'pro' if event_type in ACTIVATING else ('free' if event_type in DEACTIVATING else None)

        if new_status:
            from database import get_db, DATABASE_URL
            db = get_db()
            if DATABASE_URL:
                cur = db.cursor()
                cur.execute("UPDATE users SET subscription_status = %s WHERE id = %s", (new_status, int(app_user_id)))
                db.commit(); cur.close()
            db.close()

        return jsonify({'success': True}), 200
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms', methods=['GET'])
@mobile_auth
def get_chat_rooms():
    """Get all chat rooms the user is a member of."""
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        rooms = []
        if DATABASE_URL:
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT r.*, m.unread_count,
                    (SELECT COUNT(*) FROM chat_room_members WHERE room_id = r.id) as member_count,
                    (SELECT json_agg(json_build_object(
                        'id', u.id, 'username', u.username,
                        'profile_pic_url', u.profile_pic_url, 'email', u.email
                    )) FROM chat_room_members cm
                     JOIN users u ON u.id = cm.user_id
                     WHERE cm.room_id = r.id AND cm.user_id != %s
                     LIMIT 3) as other_members
                FROM chat_rooms r
                JOIN chat_room_members m ON m.room_id = r.id AND m.user_id = %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM chat_room_members cm2
                    JOIN blocked_users b ON b.blocker_id = %s AND b.blocked_id = cm2.user_id
                    WHERE cm2.room_id = r.id
                )
                ORDER BY r.last_message_at DESC
            """, (request.mobile_user_id, request.mobile_user_id, request.mobile_user_id))
            rooms = [dict(r) for r in cur.fetchall()]
            cur.close()
        db.close()
        return jsonify({'success': True, 'rooms': rooms})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms', methods=['POST'])
@mobile_auth
def create_chat_room():
    """Create a DM or group chat room."""
    try:
        body = request.get_json() or {}
        member_ids = body.get('member_ids', [])
        name = body.get('name', '')
        is_group = body.get('is_group', False)
        listing_id = body.get('listing_id')
        avatar_url = body.get('avatar_url', '')

        if not member_ids:
            return jsonify({'success': False, 'error': 'member_ids required'}), 400

        all_members = list(set([request.mobile_user_id] + member_ids))
        is_group = is_group or len(all_members) > 2

        from database import get_db, DATABASE_URL
        db = get_db()
        room_id = None

        if DATABASE_URL:
            cur = db.cursor()
            # For DMs, check if room already exists
            if not is_group and len(all_members) == 2:
                other_id = [m for m in all_members if m != request.mobile_user_id][0]

                cur.execute("""
                    SELECT 1 FROM blocked_users
                    WHERE (blocker_id = %s AND blocked_id = %s)
                       OR (blocker_id = %s AND blocked_id = %s)
                """, (request.mobile_user_id, other_id, other_id, request.mobile_user_id))
                if cur.fetchone():
                    cur.close(); db.close()
                    return jsonify({'success': False, 'error': 'blocked'}), 403

                cur.execute("""
                    SELECT r.id FROM chat_rooms r
                    JOIN chat_room_members m1 ON m1.room_id = r.id AND m1.user_id = %s
                    JOIN chat_room_members m2 ON m2.room_id = r.id AND m2.user_id = %s
                    WHERE r.is_group = FALSE
                    LIMIT 1
                """, (request.mobile_user_id, other_id))
                existing = cur.fetchone()
                if existing:
                    cur.close(); db.close()
                    return jsonify({'success': True, 'room_id': existing[0], 'existing': True})

            cur.execute("""
                INSERT INTO chat_rooms (name, is_group, created_by, listing_id, avatar_url)
                VALUES (%s, %s, %s, %s, %s) RETURNING id
            """, (name or None, is_group, request.mobile_user_id, listing_id, avatar_url or None))
            room_id = cur.fetchone()[0]

            for uid in all_members:
                cur.execute("INSERT INTO chat_room_members (room_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, uid))

            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True, 'room_id': room_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/messages', methods=['GET'])
@mobile_auth
def get_chat_messages(room_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        messages = []
        if DATABASE_URL:
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # Verify membership
            cur.execute("SELECT 1 FROM chat_room_members WHERE room_id = %s AND user_id = %s", (room_id, request.mobile_user_id))
            if not cur.fetchone():
                cur.close(); db.close()
                return jsonify({'success': False, 'error': 'Not a member'}), 403
            cur.execute("""
                SELECT m.*, u.username, u.profile_pic_url, u.email
                FROM chat_messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.room_id = %s
                ORDER BY m.created_at ASC
                LIMIT 100
            """, (room_id,))
            messages = [dict(r) for r in cur.fetchall()]
            # Reset unread
            cur.execute("UPDATE chat_room_members SET unread_count = 0 WHERE room_id = %s AND user_id = %s", (room_id, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True, 'messages': messages})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/messages', methods=['POST'])
@mobile_auth
def send_chat_message(room_id):
    try:
        body = request.get_json() or {}
        message = body.get('message', '').strip()
        offer_amount = body.get('offer_amount')
        if not message:
            return jsonify({'success': False, 'error': 'Message required'}), 400

        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT 1 FROM chat_room_members WHERE room_id = %s AND user_id = %s", (room_id, request.mobile_user_id))
            if not cur.fetchone():
                cur.close(); db.close()
                return jsonify({'success': False, 'error': 'Not a member'}), 403

            # Blocked either direction? Don't let the message through.
            cur.execute("""
                SELECT 1 FROM chat_room_members cm
                JOIN blocked_users b ON
                    (b.blocker_id = %s AND b.blocked_id = cm.user_id) OR
                    (b.blocked_id = %s AND b.blocker_id = cm.user_id)
                WHERE cm.room_id = %s AND cm.user_id != %s
                LIMIT 1
            """, (request.mobile_user_id, request.mobile_user_id, room_id, request.mobile_user_id))
            if cur.fetchone():
                cur.close(); db.close()
                return jsonify({'success': False, 'error': 'blocked'}), 403

            cur.execute("""
                INSERT INTO chat_messages (room_id, sender_id, message, offer_amount)
                VALUES (%s, %s, %s, %s)
            """, (room_id, request.mobile_user_id, message, offer_amount))

            # Update room last message + increment unread for other members
            cur.execute("UPDATE chat_rooms SET last_message = %s, last_message_at = NOW() WHERE id = %s", (message[:100], room_id))
            cur.execute("""
                UPDATE chat_room_members SET unread_count = unread_count + 1
                WHERE room_id = %s AND user_id != %s
            """, (room_id, request.mobile_user_id))
            db.commit()

            # Push notify the other member(s) in the room
            try:
                cur.execute("SELECT COALESCE(username, email) FROM users WHERE id = %s", (request.mobile_user_id,))
                sender_row = cur.fetchone()
                sender_name = sender_row[0] if sender_row else 'Someone'
                cur.execute("""
                    SELECT push_token FROM users u
                    JOIN chat_room_members m ON m.user_id = u.id
                    WHERE m.room_id = %s AND u.id != %s AND u.push_token IS NOT NULL AND u.push_token != ''
                """, (room_id, request.mobile_user_id))
                recipient_tokens = [r[0] for r in cur.fetchall()]
                cur.close()
                if recipient_tokens:
                    from price_alerts import send_expo_push
                    title = f"{sender_name}"
                    body_text = (f"💰 Offer: ${offer_amount}" if offer_amount else message)[:120]
                    for token in recipient_tokens:
                        send_expo_push(token, title, body_text, {"room_id": room_id})
            except Exception as e:
                app.logger.warning(f"Chat push notify failed: {e}")
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/members', methods=['POST'])
@mobile_auth
def add_chat_member(room_id):
    """Add a member to a group chat."""
    try:
        body = request.get_json() or {}
        user_id = body.get('user_id')
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("INSERT INTO chat_room_members (room_id, user_id) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, user_id))
            cur.execute("UPDATE chat_rooms SET is_group = TRUE WHERE id = %s", (room_id,))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>', methods=['DELETE'])
@mobile_auth
def leave_chat_room(room_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("DELETE FROM chat_room_members WHERE room_id = %s AND user_id = %s", (room_id, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/block', methods=['POST'])
@mobile_auth
def block_chat_room_member(room_id):
    """Block every other member of this room for the current user. Hides the
    room from the blocker's inbox and stops the blocked user's messages from
    landing (both directions checked in send_chat_message)."""
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT 1 FROM chat_room_members WHERE room_id = %s AND user_id = %s", (room_id, request.mobile_user_id))
            if not cur.fetchone():
                cur.close(); db.close()
                return jsonify({'success': False, 'error': 'Not a member'}), 403

            cur.execute("SELECT user_id FROM chat_room_members WHERE room_id = %s AND user_id != %s", (room_id, request.mobile_user_id))
            other_ids = [r[0] for r in cur.fetchall()]
            for other_id in other_ids:
                cur.execute("""
                    INSERT INTO blocked_users (blocker_id, blocked_id) VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                """, (request.mobile_user_id, other_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/unblock', methods=['POST'])
@mobile_auth
def unblock_chat_room_member(room_id):
    """Undo a block for every other member of this room."""
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT user_id FROM chat_room_members WHERE room_id = %s AND user_id != %s", (room_id, request.mobile_user_id))
            other_ids = [r[0] for r in cur.fetchall()]
            for other_id in other_ids:
                cur.execute("DELETE FROM blocked_users WHERE blocker_id = %s AND blocked_id = %s", (request.mobile_user_id, other_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/chat/rooms/<int:room_id>/report', methods=['POST'])
@mobile_auth
def report_chat_room(room_id):
    """Log an abuse report against every other member of this room for manual review."""
    try:
        body = request.get_json() or {}
        reason = (body.get('reason') or 'Not specified').strip()[:500]

        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("SELECT user_id FROM chat_room_members WHERE room_id = %s AND user_id != %s", (room_id, request.mobile_user_id))
            other_ids = [r[0] for r in cur.fetchall()]
            for other_id in other_ids:
                cur.execute("""
                    INSERT INTO user_reports (reporter_id, reported_id, room_id, reason)
                    VALUES (%s, %s, %s, %s)
                """, (request.mobile_user_id, other_id, room_id, reason))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# MARKETPLACE ENDPOINTS
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/api/mobile/marketplace/listings', methods=['GET'])
@mobile_auth
def marketplace_get_listings():
    try:
        sport  = request.args.get('sport', '')
        sort   = request.args.get('sort', 'newest')
        grade  = request.args.get('grade', '')
        max_price = request.args.get('max_price', '')
        search = request.args.get('search', '')

        from database import get_db, DATABASE_URL
        db = get_db()

        if DATABASE_URL:
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            where = ["l.status = 'active'", "l.sold = FALSE", "(l.expires_at IS NULL OR l.expires_at > NOW())"]
            params = []
            if sport:
                where.append("l.sport ILIKE %s"); params.append(f'%{sport}%')
            if grade:
                where.append("l.grade ILIKE %s"); params.append(f'%{grade}%')
            if max_price:
                where.append("l.price <= %s"); params.append(float(max_price))
            if search:
                where.append("(l.name ILIKE %s OR l.set_name ILIKE %s)"); params.extend([f'%{search}%', f'%{search}%'])
            where_str = 'WHERE ' + ' AND '.join(where) if where else ''

            order = {
                'newest':     'l.boosted DESC, l.created_at DESC',
                'price_asc':  'l.boosted DESC, l.price ASC',
                'price_desc': 'l.boosted DESC, l.price DESC',
                'most_liked': 'l.boosted DESC, l.likes DESC',
                'ending':     'l.boosted DESC, l.expires_at ASC',
            }.get(sort, 'l.boosted DESC, l.created_at DESC')

            cur.execute(f"""
                SELECT l.*, u.email as seller_email,
                       EXISTS(SELECT 1 FROM marketplace_likes ml WHERE ml.listing_id = l.id AND ml.user_id = %s) as liked_by_me
                FROM marketplace_listings l
                JOIN users u ON u.id = l.user_id
                {where_str}
                ORDER BY {order}
                LIMIT 50
            """, [request.mobile_user_id] + params)
            listings = [dict(r) for r in cur.fetchall()]
            cur.close()
            db.close()
        else:
            listings = []

        return jsonify({'success': True, 'listings': listings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/upload-image', methods=['POST'])
@mobile_auth
def marketplace_upload_image():
    """Accept a base64 image, store as static file, return a public URL."""
    try:
        body = request.get_json() or {}
        image_b64 = body.get('image', '')
        if not image_b64:
            return jsonify({'success': False, 'error': 'No image provided'}), 400

        import base64, uuid
        img_data = base64.b64decode(image_b64)
        filename = f"listing_{request.mobile_user_id}_{uuid.uuid4().hex[:8]}.jpg"
        path = os.path.join(UPLOAD_ROOT, 'marketplace', filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(img_data)

        url = f"{APP_BASE_URL}/uploads/marketplace/{filename}"
        return jsonify({'success': True, 'url': url})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings', methods=['POST'])
@mobile_auth
def marketplace_create_listing():
    try:
        body = request.get_json() or {}
        from database import get_db, DATABASE_URL
        db = get_db()
        user = get_user_by_id(request.mobile_user_id)

        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("""
                INSERT INTO marketplace_listings
                (user_id, name, year, brand, set_name, parallel, grade, cert, serial,
                 sport, price, open_to_offers, description, seller_instagram, image_urls,
                 is_bulk_lot, lot_card_count, lot_contents)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                request.mobile_user_id,
                body.get('name'), body.get('year'), body.get('brand'),
                body.get('set_name'), body.get('parallel'), body.get('grade'),
                body.get('cert'), body.get('serial'), body.get('sport'),
                body.get('price'), body.get('open_to_offers', True),
                body.get('description'), body.get('seller_instagram'),
                body.get('image_urls'), body.get('is_bulk_lot', False),
                body.get('lot_card_count'), body.get('lot_contents'),
            ))
            listing_id = cur.fetchone()[0]
            db.commit(); cur.close(); db.close()
        else:
            listing_id = None

        return jsonify({'success': True, 'listing_id': listing_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings/<int:listing_id>', methods=['PATCH'])
@mobile_auth
def marketplace_update_listing(listing_id):
    try:
        body = request.get_json() or {}
        from database import get_db, DATABASE_URL
        db = get_db()

        if DATABASE_URL:
            editable = [
                'name', 'year', 'brand', 'set_name', 'parallel', 'grade', 'cert', 'serial',
                'sport', 'price', 'open_to_offers', 'description', 'seller_instagram',
                'image_urls', 'is_bulk_lot', 'lot_card_count', 'lot_contents',
            ]
            fields = [f for f in editable if f in body]
            if not fields:
                return jsonify({'success': False, 'error': 'No fields to update'}), 400

            set_clause = ', '.join(f"{f} = %s" for f in fields)
            values = [body.get(f) for f in fields] + [listing_id, request.mobile_user_id]

            cur = db.cursor()
            cur.execute(
                f"UPDATE marketplace_listings SET {set_clause} WHERE id = %s AND user_id = %s",
                values,
            )
            updated = cur.rowcount
            db.commit(); cur.close(); db.close()
            if not updated:
                return jsonify({'success': False, 'error': 'Listing not found'}), 404
        else:
            return jsonify({'success': False, 'error': 'Marketplace unavailable'}), 503

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/my-listings', methods=['GET'])
@mobile_auth
def marketplace_my_listings():
    try:
        from database import get_db, DATABASE_URL
        db = get_db()

        if DATABASE_URL:
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT * FROM marketplace_listings
                WHERE user_id = %s
                ORDER BY created_at DESC
            """, (request.mobile_user_id,))
            listings = [dict(r) for r in cur.fetchall()]
            cur.close(); db.close()
        else:
            listings = []

        return jsonify({'success': True, 'listings': listings})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings/<int:listing_id>', methods=['DELETE'])
@mobile_auth
def marketplace_delete_listing(listing_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("DELETE FROM marketplace_listings WHERE id = %s AND user_id = %s", (listing_id, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings/<int:listing_id>/sold', methods=['POST'])
@mobile_auth
def marketplace_mark_sold(listing_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE marketplace_listings SET sold = TRUE WHERE id = %s AND user_id = %s", (listing_id, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings/<int:listing_id>/like', methods=['POST'])
@mobile_auth
def marketplace_like(listing_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        liked = False
        if DATABASE_URL:
            cur = db.cursor()
            try:
                cur.execute("INSERT INTO marketplace_likes (user_id, listing_id) VALUES (%s, %s)", (request.mobile_user_id, listing_id))
                cur.execute("UPDATE marketplace_listings SET likes = likes + 1 WHERE id = %s", (listing_id,))
                liked = True
            except Exception:
                db.rollback()
                cur.execute("DELETE FROM marketplace_likes WHERE user_id = %s AND listing_id = %s", (request.mobile_user_id, listing_id))
                cur.execute("UPDATE marketplace_listings SET likes = GREATEST(likes - 1, 0) WHERE id = %s", (listing_id,))
                liked = False
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True, 'liked': liked})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/listings/<int:listing_id>/view', methods=['POST'])
@mobile_auth
def marketplace_view(listing_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE marketplace_listings SET views = views + 1 WHERE id = %s", (listing_id,))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/messages', methods=['POST'])
@mobile_auth
def marketplace_send_message():
    try:
        body = request.get_json() or {}
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            # Get listing owner
            cur.execute("SELECT user_id, name FROM marketplace_listings WHERE id = %s", (body.get('listing_id'),))
            row = cur.fetchone()
            if not row:
                return jsonify({'success': False, 'error': 'Listing not found'}), 404
            receiver_id, listing_name = row
            cur.execute("""
                INSERT INTO marketplace_messages (listing_id, sender_id, receiver_id, message, offer_amount)
                VALUES (%s, %s, %s, %s, %s)
            """, (body.get('listing_id'), request.mobile_user_id, receiver_id,
                  body.get('message'), body.get('offer_amount')))
            db.commit()

            # Push notify the listing owner
            try:
                cur.execute("SELECT push_token FROM users WHERE id = %s", (receiver_id,))
                prow = cur.fetchone()
                cur.close()
                if prow and prow[0]:
                    from price_alerts import send_expo_push
                    offer_amount = body.get('offer_amount')
                    title = "New offer 💰" if offer_amount else "New message"
                    body_text = (f"${offer_amount} offer on {listing_name or 'your listing'}" if offer_amount
                                 else (body.get('message') or f"New message about {listing_name or 'your listing'}"))[:120]
                    send_expo_push(prow[0], title, body_text, {"listing_id": body.get('listing_id')})
            except Exception as e:
                app.logger.warning(f"Marketplace push notify failed: {e}")
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/messages/<int:listing_id>', methods=['GET'])
@mobile_auth
def marketplace_get_messages(listing_id):
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        messages = []
        if DATABASE_URL:
            cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute("""
                SELECT m.*, u.email as sender_email
                FROM marketplace_messages m
                JOIN users u ON u.id = m.sender_id
                WHERE m.listing_id = %s AND (m.sender_id = %s OR m.receiver_id = %s)
                ORDER BY m.created_at ASC
            """, (listing_id, request.mobile_user_id, request.mobile_user_id))
            messages = [dict(r) for r in cur.fetchall()]
            cur.close()
        db.close()
        return jsonify({'success': True, 'messages': messages})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/boost/<int:listing_id>', methods=['POST'])
@mobile_auth
def marketplace_boost(listing_id):
    """Create a $2.99 Stripe charge to boost a listing for 30 days."""
    try:
        # Create Stripe payment intent
        intent = stripe.PaymentIntent.create(
            amount=299,  # $2.99 in cents
            currency='usd',
            metadata={'listing_id': listing_id, 'user_id': request.mobile_user_id, 'type': 'boost'},
        )
        return jsonify({'success': True, 'client_secret': intent.client_secret})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/boost/<int:listing_id>/confirm', methods=['POST'])
@mobile_auth
def marketplace_boost_confirm(listing_id):
    """Called after payment succeeds — activate the boost."""
    try:
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("""
                UPDATE marketplace_listings
                SET boosted = TRUE, boost_expires_at = NOW() + INTERVAL '30 days'
                WHERE id = %s AND user_id = %s
            """, (listing_id, request.mobile_user_id))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/mobile/marketplace/ratings', methods=['POST'])
@mobile_auth
def marketplace_rate_seller():
    try:
        body = request.get_json() or {}
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("""
                INSERT INTO seller_ratings (seller_id, rater_id, listing_id, rating, review)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (rater_id, listing_id) DO UPDATE SET rating = EXCLUDED.rating, review = EXCLUDED.review
            """, (body.get('seller_id'), request.mobile_user_id, body.get('listing_id'),
                  body.get('rating'), body.get('review')))
            db.commit(); cur.close()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── What did I pay? — update paid price ──────────────────────────────────────
@app.route('/api/mobile/collection/<int:card_id>/paid-price', methods=['POST'])
@mobile_auth
def update_paid_price(card_id):
    try:
        body = request.get_json() or {}
        paid_price = body.get('paid_price')
        from database import get_db, DATABASE_URL
        db = get_db()
        if DATABASE_URL:
            cur = db.cursor()
            cur.execute("UPDATE scan_history SET paid_price = %s WHERE id = %s AND user_id = %s",
                       (paid_price, card_id, request.mobile_user_id))
            db.commit(); cur.close()
        else:
            db.execute("UPDATE scan_history SET paid_price = ? WHERE id = ? AND user_id = ?",
                      (paid_price, card_id, request.mobile_user_id))
            db.commit()
        db.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ── Background jobs ──────────────────────────────────────────────────────────
# Runs at import time so it starts under gunicorn too, not just `python app.py`.
try:
    from price_alerts import start_price_alert_scheduler
    start_price_alert_scheduler(interval_hours=6)
except Exception as e:
    app.logger.warning(f"Could not start price alert scheduler: {e}")


if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
