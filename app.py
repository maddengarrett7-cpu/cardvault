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
from datetime import datetime
from flask import Flask, render_template, Response, jsonify, request
import cv2
import google.generativeai as genai
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.environ.get("GEMINI_API_KEY", "")
GOOGLE_CREDS_FILE = os.path.join(os.path.dirname(__file__), "google_creds.json")
SPREADSHEET_ID    = os.environ.get("SPREADSHEET_ID", "")
EBAY_APP_ID       = os.environ.get("EBAY_APP_ID", "")
SHEET_TAB         = "Cards"
# ───────────────────────────────────────────────────────────────────────────

app = Flask(__name__)

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

def analyze_card(frame):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.5-flash")
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    image_data = buf.tobytes()
    prompt = (
        "This is a sports card. Extract the following fields and "
        "return ONLY valid JSON with these exact keys:\n"
        "  name   - player's full name (string)\n"
        "  year   - card year as a 4-digit number (integer or null)\n"
        "  brand  - card brand/set name, e.g. 'Prizm', 'Topps', 'Bowman', 'Donruss', 'Select' (string or null)\n"
        "  grade  - grading label, e.g. 'PSA 9', 'BGS 8.5', 'SGC 10', 'Raw' if ungraded (string)\n"
        "  cert   - the certification/serial number on the grading label (string or null)\n"
        "  card   - a single description formatted EXACTLY as: "
        "YEAR BRAND PLAYER_NAME CARD_DETAIL GRADE. "
        "Example: '2026 Prizm Cam Ward Red Sparkle PSA 10'. "
        "If ungraded use 'Raw' at the end. Skip parts that cannot be determined.\n\n"
        "If a field cannot be determined, use null."
    )
    response = model.generate_content([
        prompt,
        {"mime_type": "image/jpeg", "data": image_data}
    ])
    text = response.text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
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

def append_to_sheet(data, custom_sheet_id=None):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = get_creds()
    svc    = build("sheets", "v4", credentials=creds)
    sheet_id = custom_sheet_id or SPREADSHEET_ID
    ebay_avg = data.get("ebay_avg")
    value = f"${ebay_avg:.2f}" if ebay_avg else ""
    row = [[
        value,
        data.get("name")  or "",
        data.get("year")  or "",
        data.get("grade") or "",
        data.get("cert")  or "",
        data.get("card")  or "",
        "", "",
        "",  # Paid
        "",  # Tracking Number
    ]]
    svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=f"{SHEET_TAB}!A1",
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
    return render_template('index.html')

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
    """Scrape eBay completed/sold listings — no API key needed."""
    from bs4 import BeautifulSoup
    import re
    url = (
        "https://www.ebay.com/sch/i.html"
        f"?_nkw={requests.utils.quote(query)}"
        "&LH_Complete=1&LH_Sold=1&_sop=13&_ipg=25"
    )
    try:
        resp = requests.get(url, headers=_EBAY_HEADERS, timeout=12)
        resp.raise_for_status()
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
            price_match = re.search(r"[\d,]+\.?\d*", price_text.replace(",", ""))
            if not price_match:
                continue
            price = float(price_match.group().replace(",", ""))
            prices.append(price)

            date = date_el.get_text(strip=True) if date_el else None
            sales.append({
                "title": title,
                "price": price,
                "date": date,
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
            "search_url": url,
        }, None
    except Exception as e:
        return None, str(e)


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
def scan():
    try:
        # Accept image from browser camera
        body = request.get_json()
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

        # Auto-fetch values
        cl_token = body.get("cl_token", "") if body else ""
        query_parts = [p for p in [str(data.get("year", "")), data.get("name", ""), data.get("grade", "")] if p]
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

        custom_sheet = body.get("sheet_id", "") if body else ""
        custom_sheet_id = extract_sheet_id(custom_sheet) if custom_sheet else None
        append_to_sheet(data, custom_sheet_id)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
