#!/usr/bin/env python3
"""
Sports Card Scanner - Web App
Run this and open http://localhost:5000 in your browser
"""

import os
import json
import time
import threading
import base64
import requests
import stripe
from datetime import datetime
from functools import wraps
from collections import defaultdict
from flask import Flask, render_template, Response, jsonify, request, session, redirect, url_for
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
    create_session, validate_session, delete_session

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
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://scanly-production-8403.up.railway.app")
# ───────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "slabscan-dev-secret")

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

def gemini_generate(client, model, contents, retries=2):
    """Call Gemini with automatic retry on 503."""
    import time as _time
    for attempt in range(retries + 1):
        try:
            return client.models.generate_content(model=model, contents=contents)
        except Exception as e:
            if attempt < retries and ("503" in str(e) or "UNAVAILABLE" in str(e)):
                _time.sleep(3)
                continue
            raise

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

def analyze_card(frame):
    client = genai.Client(api_key=GEMINI_API_KEY)
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
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
        "paid":  "",
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
        update_subscription(sub['customer'], status)
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

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')

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
        if body and 'image' in body:
            import numpy as np
            img_bytes = base64.b64decode(body['image'])
            nparr = np.frombuffer(img_bytes, np.uint8)
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

        data = analyze_card(frame)

        # Second label pass — only for uploads (high quality) not live camera (too slow)
        has_grade = data.get("grade") and data.get("grade").lower() != "raw"
        missing_details = not data.get("name") or not data.get("year") or not data.get("set")
        if is_upload and has_grade:
            try:
                _, buf2 = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
                label_data = analyze_label(buf2.tobytes())
                # Merge: fill in any missing fields from label pass
                for field in ["name", "year", "brand", "set", "parallel", "grade", "cert", "card"]:
                    if not data.get(field) and label_data.get(field):
                        data[field] = label_data[field]
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
    db = get_db()
    try:
        if hasattr(db, 'cursor'):
            cur = db.cursor()
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
            cur.execute("SELECT email, subscription_status, scans_today, created_at, COALESCE(total_scans, 0) FROM users ORDER BY created_at DESC LIMIT 20")
            recent_users = cur.fetchall()
            cur.close()
        else:
            from datetime import date
            today = str(date.today())
            total_users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            pro_users = db.execute("SELECT COUNT(*) FROM users WHERE subscription_status = 'pro'").fetchone()[0]
            active_today = db.execute("SELECT COUNT(*) FROM users WHERE scans_date = ?", (today,)).fetchone()[0]
            scans_today = db.execute("SELECT SUM(scans_today) FROM users WHERE scans_date = ?", (today,)).fetchone()[0] or 0
            recent_users = db.execute("SELECT email, subscription_status, scans_today, created_at FROM users ORDER BY created_at DESC LIMIT 20").fetchall()
        db.close()
    except Exception as e:
        return f"Error: {e}", 500

    secret = os.environ.get("ADMIN_SECRET", "")
    def make_row(u):
        email, plan, scans, joined = u[0], u[1], u[2], u[3]
        total = u[4] if len(u) > 4 else 0
        plan_label = '🟢 Pro' if plan == 'pro' else '⚪ Free'
        if plan != 'pro':
            action_html = (
                '<form method="POST" action="/admin/upgrade" style="display:inline">'
                '<input type="hidden" name="email" value="' + email + '">'
                '<input type="hidden" name="secret" value="' + secret + '">'
                '<button style="background:#00ff87;color:#000;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-weight:700;font-size:12px;">→ Pro</button>'
                '</form>'
            )
        else:
            action_html = (
                '<form method="POST" action="/admin/downgrade" style="display:inline">'
                '<input type="hidden" name="email" value="' + email + '">'
                '<input type="hidden" name="secret" value="' + secret + '">'
                '<button style="background:#333;color:#888;border:none;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;">→ Free</button>'
                '</form>'
            )
        return f"<tr><td>{email}</td><td>{plan_label}</td><td>{scans}</td><td>{total}</td><td>{joined}</td><td>{action_html}</td></tr>"
    rows = ''.join([make_row(u) for u in recent_users])
    return f"""<!DOCTYPE html><html>
<head><title>CardScan Admin</title>
<style>body{{font-family:system-ui;background:#0d0d0d;color:#fff;padding:40px;max-width:900px;margin:0 auto}}
h1{{color:#00ff87}}
.stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:24px 0}}
.stat{{background:#1a1a1a;border-radius:12px;padding:20px;text-align:center}}
.stat-num{{font-size:36px;font-weight:800;color:#00ff87}}
.stat-label{{color:#888;font-size:13px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;margin-top:24px}}
th{{text-align:left;color:#888;font-size:12px;padding:8px;border-bottom:1px solid #333}}
td{{padding:10px 8px;border-bottom:1px solid #1a1a1a;font-size:14px}}
</style></head>
<body>
<h1>📊 CardScan Dashboard</h1>
<div class="stats">
  <div class="stat"><div class="stat-num">{total_users}</div><div class="stat-label">Total Users</div></div>
  <div class="stat"><div class="stat-num">{pro_users}</div><div class="stat-label">Pro Users</div></div>
  <div class="stat"><div class="stat-num">{active_today}</div><div class="stat-label">Active Today</div></div>
  <div class="stat"><div class="stat-num">{scans_today}</div><div class="stat-label">Scans Today</div></div>
  <div class="stat"><div class="stat-num">{total_scans_ever}</div><div class="stat-label">Total Scans Ever</div></div>
</div>
<h2 style="color:#888;font-size:14px;text-transform:uppercase;letter-spacing:1px;">Recent Users</h2>
<table><tr><th>Email</th><th>Plan</th><th>Scans Today</th><th>Total Scans</th><th>Joined</th><th>Action</th></tr>{rows}</table>
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

if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
