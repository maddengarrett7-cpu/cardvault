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
from datetime import datetime
from functools import wraps
from collections import defaultdict
from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash
import cv2
from google import genai
from google.genai import types as genai_types
from google.oauth2.service_account import Credentials
from google.oauth2.credentials import Credentials as OAuthCredentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from database import init_db, get_user_by_email, get_user_by_id, create_user, \
    update_stripe_customer, update_subscription, check_and_increment_scans, \
    save_google_tokens, save_google_sheet_id, clear_google_tokens, \
    create_session, validate_session, delete_session, \
    save_reset_token, get_reset_token, delete_reset_token, update_password

# ── Config ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_CREDS_FILE = os.path.join(os.path.dirname(__file__), "google_creds.json")
SPREADSHEET_ID    = os.environ.get("SPREADSHEET_ID", "")
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
# ───────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "slabscan-dev-secret")

# ── Email ────────────────────────────────────────────────────────────────────
import smtplib
import secrets
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

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

_FALLBACK_MODEL = "gemini-2.0-flash"

def gemini_generate(client, model, contents, retries=5):
    """Call Gemini with exponential backoff and model fallback on overload."""
    import time as _time
    last_err = None
    for attempt in range(retries + 1):
        # After half the retries, fall back to the lighter model
        active_model = _FALLBACK_MODEL if attempt >= (retries // 2) else model
        try:
            return client.models.generate_content(model=active_model, contents=contents)
        except Exception as e:
            last_err = e
            err_str = str(e)
            is_overload = any(x in err_str for x in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded"))
            if attempt < retries and is_overload:
                wait = min(2 ** attempt, 30)  # 1s, 2s, 4s, 8s, 16s … cap at 30s
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

def analyze_card(frame, quality=85):
    client = genai.Client(api_key=GEMINI_API_KEY)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    image_data = buf.tobytes()
    prompt = (
        "You are scanning a trading card. First determine the card type:\n"
        "  - 'sports' — NBA, NFL, MLB, NHL player cards (Topps, Panini, Upper Deck, Bowman, etc.)\n"
        "  - 'tcg'    — Pokemon, Magic: The Gathering, Yu-Gi-Oh, or other trading card games\n\n"
        "Return ONLY valid JSON with these exact keys (use null for anything you cannot determine):\n\n"
        "  card_type  - 'sports' or 'tcg' (string)\n"
        "  name       - player name (sports) or Pokemon/card name (tcg) (string)\n"
        "  year       - card year as 4-digit number (integer or null)\n"
        "  brand      - manufacturer: e.g. 'Topps', 'Panini', 'Pokemon', 'Wizards of the Coast' (string or null)\n"
        "  set        - set/product name: e.g. 'Prizm', 'Chrome', 'Base Set', 'Scarlet & Violet' (string or null)\n"
        "  parallel   - parallel or variant: e.g. 'Silver', 'Holo', 'Reverse Holo', 'Gold Refractor' (string or null)\n"
        "  grade      - grading label if in a slab: 'PSA 9', 'BGS 8.5', 'CGC 10'. If raw use 'Raw' (string)\n"
        "  cert       - cert/serial number on grading label (string or null)\n"
        "  rarity     - TCG rarity symbol/text only: e.g. 'Rare Holo', 'Common', 'Ultra Rare', 'Secret Rare' (string or null, null for sports)\n"
        "  card_number - TCG card number printed on card e.g. '4/102', '025/198' (string or null, null for sports)\n"
        "  hp         - TCG HP value as integer e.g. 120 (integer or null, null for sports)\n"
        "  card       - single human-readable description:\n"
        "               Sports: 'YEAR BRAND SET PLAYER PARALLEL GRADE' e.g. '2021 Panini Prizm Silver Luka Doncic PSA 10'\n"
        "               TCG:    'POKEMON SET CARD_NUMBER RARITY GRADE' e.g. 'Charizard Base Set 4/102 Holo Rare PSA 9'\n"
        "               For raw cards, do NOT include 'Raw' at the end — just omit the grade entirely.\n\n"
        "IMPORTANT RULES:\n"
        "1. Read the YEAR from the card — look for a 4-digit number like 2018, 2019, 2020, 2021, 2022, 2023, 2024, 2025. "
        "It is often small text at the bottom of the card front, in the copyright line, or on the card back. "
        "Look carefully — do not guess or assume based on the player.\n"
        "2. Read the BRAND logo carefully — Topps and Panini are DIFFERENT companies:\n"
        "   - Topps sets: Chrome, Finest, Heritage, Stadium Club, Bowman, Allen & Ginter, Series 1/2\n"
        "   - Panini sets: Prizm, Select, Donruss, Mosaic, Optic, Contenders, Crown Royale, National Treasures\n"
        "   - Upper Deck sets: SP Authentic, Exquisite, Young Guns\n"
        "3. For graded slabs, read BOTH the PSA/BGS/SGC label AND the card visible through the case:\n"
        "   - The label has: player name, year, brand, set, card number, grade\n"
        "   - Common parallels on Select: Silver, Gold, Gold Vinyl, Tie-Dye, Blue, Red, Green, White Sparkle\n"
        "   - Common parallels on Prizm: Silver, Gold, Red, Blue, Green, Purple, Orange, Pink, Rainbow\n"
        "   - If you see a gold-colored card in a PSA slab, the parallel is likely 'Gold'\n"
        "4. For numbered cards (e.g. '89/99'), the second number is the print run — put it in parallel as 'Green /99'. The first number is the card number.\n"
        "5. Do NOT confuse Topps Chrome with Panini Prizm — look for the actual brand name on the card.\n"
        "6. For raw cards read ALL text on the card face carefully. Do not leave fields null if visible.\n"
        "For graded slabs: read the label for all fields including the cert number.\n"
        "Return ONLY the JSON object — no markdown, no code fences, no extra text."
    )
    response = gemini_generate(client,
        model="gemini-2.5-flash",
        contents=[
            prompt,
            genai_types.Part.from_bytes(data=image_data, mime_type="image/jpeg"),
        ],
    )
    text = response.text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()
    return json.loads(text)

def analyze_card_back(image_data):
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


def analyze_raw_card(image_data):
    """Second pass for raw (ungraded) cards — focused on fine-print details
    that the general first pass tends to miss: year, set, parallel, card number."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This is a raw (ungraded) sports or trading card. "
        "Your ONLY job is to read the fine-print details that are easy to miss. "
        "Study every millimeter of text carefully.\n\n"

        "WHERE TO FIND EACH FIELD:\n"
        "  YEAR  — Look at the very bottom of the card for a copyright line like '© 2021 Panini America' "
        "or '2022 Topps'. It is usually tiny. Also check the card back if visible. "
        "Return ONLY the 4-digit number.\n"
        "  BRAND — Read the manufacturer name from the logo or copyright line. "
        "Topps and Panini are different companies. "
        "Topps sets include: Chrome, Finest, Heritage, Bowman, Stadium Club, Series 1/2. "
        "Panini sets include: Prizm, Select, Mosaic, Optic, Donruss, Contenders, Obsidian, Chronicles.\n"
        "  SET   — The product/set name, e.g. 'Prizm', 'Chrome', 'Select', 'Mosaic', 'Optic', 'Bowman'.\n"
        "  PARALLEL — Look at the card border color, foil finish, and any color treatment:\n"
        "    Prizm parallels: Silver (default foil), Gold, Red, Blue, Green, Purple, Orange, Pink, "
        "Rainbow, Red White Blue, Carolina Blue, Hyper, Disco, Holo, Cracked Ice\n"
        "    Chrome parallels: Refractor (default), Gold Refractor, Pink Refractor, Blue Refractor, "
        "Purple Refractor, Orange Refractor, Atomic Refractor, Prism Refractor\n"
        "    Select parallels: Silver, Gold, Gold Vinyl, Tie-Dye, Blue, Red, Green, White Sparkle, Courtside\n"
        "    Mosaic parallels: Silver, Gold, Pink, Blue, Green, Red, Reactive Blue/Yellow/Orange\n"
        "    Donruss parallels: Press Proof, Gold Press Proof, Carolina Blue, Holo Pink, Diamond\n"
        "    If you see a foil/shimmer border → Silver. Gold border → Gold. Etc.\n"
        "    If it appears to be a standard base card with no special finish → null\n"
        "  CARD NUMBER — Look for a number like '#301' or '301' printed on the front, usually "
        "bottom corner. For numbered parallels (e.g. '45/99'), format as '/99' in the parallel field.\n"
        "  PLAYER/CARD NAME — The large name printed on the front of the card.\n"
        "  SPORT — Basketball, Football, Baseball, Hockey, Soccer etc.\n\n"

        "Return ONLY valid JSON with these keys (null if truly cannot determine):\n"
        "  name       - player full name\n"
        "  year       - 4-digit year as integer\n"
        "  brand      - manufacturer\n"
        "  set        - set/product name\n"
        "  parallel   - parallel variant or null for base\n"
        "  card_number - card number printed on card e.g. '301' or null\n"
        "  sport      - sport name or null\n"
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


def analyze_bulk(image_data):
    """Detect multiple cards in a single image and return a list of card dicts."""
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        "This photo contains multiple graded sports card slabs or trading cards. "
        "Identify EVERY card visible, reading each grading label carefully. "
        "Return ONLY a valid JSON array where each element has these keys (null if unreadable):\n"
        "  card_type  - 'sports' or 'tcg'\n"
        "  name       - player name (sports) or card name (tcg)\n"
        "  year       - 4-digit year as integer or null\n"
        "  brand      - manufacturer e.g. 'Panini', 'Topps'\n"
        "  set        - set name e.g. 'Prizm', 'Chrome'\n"
        "  parallel   - parallel/variant e.g. 'Silver', 'Gold Refractor'\n"
        "  grade      - full grade e.g. 'PSA 10', 'BGS 9.5', or 'Raw'\n"
        "  cert       - cert number digits only or null\n"
        "  card       - full description: 'YEAR BRAND SET PLAYER PARALLEL GRADE'\n"
        "List cards in the order they appear LEFT TO RIGHT, TOP TO BOTTOM in the image. "
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

# Keyword map: field -> list of header keywords that match it
FIELD_KEYWORDS = {
    "card":  ["card", "description", "title", "full", "listing"],
    "name":  ["name", "player", "athlete"],
    "year":  ["year", "yr", "season"],
    "brand": ["brand", "set", "series", "product"],
    "grade": ["grade", "condition", "psa", "bgs", "sgc", "slab"],
    "cert":  ["cert", "certification", "serial", "slab #", "cert #", "id"],
    "value": ["value", "ebay", "avg", "market", "worth", "$"],
    "paid":  ["paid", "cost", "bought", "purchase", "price"],
    "tracking": ["tracking", "track", "ship"],
}

def detect_column_mapping(headers):
    """Map field names to column indices based on header keywords."""
    mapping = {}
    for col_idx, header in enumerate(headers):
        h = header.lower().strip()
        for field, keywords in FIELD_KEYWORDS.items():
            if field not in mapping and any(kw in h for kw in keywords):
                mapping[field] = col_idx
    return mapping

def build_row(data, mapping, num_cols):
    """Build a row array aligned to the sheet's existing columns."""
    ebay_avg = data.get("ebay_avg")
    values = {
        "card":  data.get("card")  or "",
        "name":  data.get("name")  or "",
        "year":  str(data.get("year") or ""),
        "brand": data.get("brand") or "",
        "grade": data.get("grade") or "",
        "cert":  data.get("cert")  or "Raw",
        "value": f"${ebay_avg:.2f}" if ebay_avg else "",
        "paid":  data.get("paid") or "",
        "tracking": "",
    }
    row = [""] * num_cols
    for field, col_idx in mapping.items():
        if col_idx < num_cols and field in values:
            row[col_idx] = values[field]
    return row

def get_first_sheet_tab(sheet_id, svc):
    """Get the name of the first tab in the spreadsheet."""
    try:
        meta = svc.spreadsheets().get(spreadsheetId=sheet_id).execute()
        sheets = meta.get("sheets", [])
        if sheets:
            return sheets[0]["properties"]["title"]
    except Exception:
        pass
    return SHEET_TAB

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
        return build("sheets", "v4", credentials=creds)
    return build("sheets", "v4", credentials=get_creds())

def append_to_sheet(data, custom_sheet_id=None, user=None):
    user = user or {}
    svc = get_user_sheets_service(user)

    # Use user's saved sheet, then custom passed in, then fallback
    sheet_id = (
        custom_sheet_id
        or user.get("google_sheet_id")
        or SPREADSHEET_ID
    )
    if not sheet_id:
        return  # No sheet configured — skip silently

    tab = get_first_sheet_tab(sheet_id, svc)
    headers = get_sheet_headers(sheet_id, svc)

    if headers:
        mapping = detect_column_mapping(headers)
        row = [build_row(data, mapping, len(headers))]
    else:
        ebay_avg = data.get("ebay_avg")
        value = f"${ebay_avg:.2f}" if ebay_avg else ""
        row = [[
            data.get("card") or "",
            "",
            "",
            data.get("cert") or "Raw",
            value,
        ]]

    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{tab}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": row},
    ).execute()

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
    return render_template('index.html', user=user)

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
        user = create_user(email, generate_password_hash(password))
        if not user:
            return render_template('login.html', error='An account with that email already exists', mode='signup')
        import secrets
        token = secrets.token_hex(32)
        create_session(user['id'], token)
        session['user_id'] = user['id']
        session['session_token'] = token
        return redirect(url_for('index'))
    return render_template('login.html', mode='signup')

@app.route('/logout')
def logout():
    token = session.get('session_token')
    if token:
        delete_session(token)
    session.clear()
    return redirect(url_for('login'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'GET':
        return render_template('forgot_password.html')
    email = request.form.get('email', '').strip().lower()
    user = get_user_by_email(email)
    # Always show success message to avoid user enumeration
    if user:
        token = secrets.token_urlsafe(32)
        expires_at = datetime.utcnow() + timedelta(hours=1)
        save_reset_token(email, token, expires_at)
        reset_url = f"{APP_BASE_URL}/reset-password/{token}"
        try:
            send_reset_email(email, reset_url)
        except Exception as e:
            return render_template('forgot_password.html', error='Could not send email. Please try again or contact us on Instagram.')
    return render_template('forgot_password.html', success=True)

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

@app.route('/mission')
def mission():
    return render_template('mission.html')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

@app.route('/terms')
def terms():
    return render_template('terms.html')

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


@app.route('/scan', methods=['POST'])
@login_required
def scan():
    # Check scan limits
    allowed, scans_used, limit = check_and_increment_scans(session['user_id'])
    if not allowed:
        return jsonify({
            'success': False,
            'limit_reached': True,
            'error': f'Free limit reached ({limit} scans/day). Upgrade to SlabScan Pro for unlimited scans.'
        })
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
        else:
            # Fall back to Mac camera
            with camera_lock:
                cap = get_camera()
                for _ in range(5):
                    cap.read()
                ret, frame = cap.read()
            if not ret:
                return jsonify({'success': False, 'error': 'Could not capture image'})

        # Use higher quality for uploads so Gemini can read fine print
        data = analyze_card(frame, quality=95 if is_upload else 85)

        is_raw_card = (not data.get("grade") or data.get("grade", "").lower() == "raw")
        has_grade   = not is_raw_card

        # Second pass for graded slabs — read the slab label
        if is_upload and has_grade and raw_image_bytes:
            try:
                label_data = analyze_label(raw_image_bytes)
                for field in ["name", "year", "brand", "set", "parallel", "grade", "cert", "card"]:
                    if label_data.get(field):
                        data[field] = label_data[field]
            except Exception:
                pass

        # Second pass for raw cards — focused on year, set, parallel, fine print
        if is_raw_card and raw_image_bytes:
            try:
                raw_data = analyze_raw_card(raw_image_bytes)
                # Fill in any fields the first pass left null; don't overwrite confident values
                for field in ["name", "year", "brand", "set", "parallel", "card_number", "sport"]:
                    if raw_data.get(field) and not data.get(field):
                        data[field] = raw_data[field]
                # Year and brand are worth overwriting if the second pass found them — they're
                # the most commonly wrong fields on raw cards
                for field in ["year", "brand", "set"]:
                    if raw_data.get(field):
                        data[field] = raw_data[field]
                # Rebuild the card description with the improved data
                if data.get("card_type") != "tcg":
                    parts = [p for p in [
                        str(data.get("year") or ""),
                        data.get("brand") or "",
                        data.get("set") or "",
                        data.get("name") or "",
                        data.get("parallel") or "",
                    ] if p]
                    if parts:
                        data["card"] = " ".join(parts)
            except Exception:
                pass

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
        append_to_sheet(data, custom_sheet_id, user=user)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        err = str(e)
        if "503" in err or "UNAVAILABLE" in err:
            return jsonify({'success': False, 'error': 'Scanner is busy right now — please try again in a moment'})
        return jsonify({'success': False, 'error': err})

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
        cards = analyze_bulk(raw_image_bytes)
        if not isinstance(cards, list):
            return jsonify({'success': False, 'error': 'Could not detect cards in image'})
        return jsonify({'success': True, 'cards': cards, 'count': len(cards)})
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
        sheeted = 0
        for card in cards:
            append_to_sheet(card, custom_sheet_id, user=user)
            sheeted += 1
        return jsonify({'success': True, 'sheeted': sheeted})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


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
            cur.execute("SELECT SUM(COALESCE(total_scans, 0)) FROM users")
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
        return f"<tr><td>{email}</td><td>{plan_label}</td><td>{scans}</td><td>{total}</td><td>{str(joined)[:10]}</td><td style='white-space:nowrap'>{upgrade_btn}{delete_btn}{email_btn}</td></tr>"

    rows = ''.join([make_row(u) for u in recent_users])
    top_scanner_rows = ''.join([f"<tr><td>{u[0]}</td><td style='color:#00ff87;font-weight:700'>{u[1]}</td></tr>" for u in top_scanners])
    search_val = search or ''

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
<h1>📊 CardScan Admin</h1>
<p style="color:#555;font-size:13px;margin-bottom:24px;">Last refreshed: {str(date.today())}</p>

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
</script>
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
        body = request.get_json()
        image_data = base64.b64decode(body['image'])

        # Single Gemini call: card details + price in one pass
        client = genai.Client(api_key=GEMINI_API_KEY)
        prompt = (
            "This is the BACK of a raw (ungraded) sports card. "
            "Read every line of text and extract two things:\n\n"
            "1. CARD DETAILS from the printed card text:\n"
            "   year        — 4-digit year from copyright line e.g. '© 2021 Panini' → 2021\n"
            "   card_number — card number e.g. '# 301' or '301' near the bottom\n"
            "   brand       — manufacturer from copyright line e.g. 'Panini', 'Topps'\n"
            "   set         — set name if printed e.g. 'Prizm', 'Chrome', 'Select'\n"
            "   name        — player full name from stats/bio header\n"
            "   team        — player's team name\n"
            "   rookie      — true if 'RC', 'Rookie', or 'Rookie Card' appears\n"
            "   serial      — print run if numbered e.g. '045/199' → '/199', else null\n\n"
            "2. PRICE from any sticker, sticky note, handwritten label, or tape:\n"
            "   paid        — dollar amount e.g. '$45', '$4.99', '$700'. "
            "                 Look carefully — this could be a handwritten number. "
            "                 If no price is visible return null.\n\n"
            "Return ONLY valid JSON with these exact keys "
            "(null for anything not found):\n"
            "  year, card_number, brand, set, name, team, rookie, serial, paid\n"
            "Return ONLY the JSON object — no markdown, no code fences."
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
        back = json.loads(text.strip())

        update = {k: back[k] for k in
                  ('year', 'card_number', 'brand', 'set', 'name', 'team', 'rookie', 'serial')
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
        return jsonify({'success': False, 'error': str(e)})


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


# ─────────────────────────────────────────────────────────────────────────────

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404

@app.errorhandler(500)
def server_error(e):
    return render_template('500.html'), 500

if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
