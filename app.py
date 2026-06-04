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
    model = genai.GenerativeModel("gemini-2.0-flash-lite")
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    image_data = buf.tobytes()
    prompt = (
        "This is a sports card. Extract the following fields and "
        "return ONLY valid JSON with these exact keys:\n"
        "  name   - player's full name (string)\n"
        "  year   - card year as a 4-digit number (integer or null)\n"
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

def append_to_sheet(data):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = get_creds()
    svc    = build("sheets", "v4", credentials=creds)
    row = [[
        data.get("name")  or "",
        data.get("year")  or "",
        data.get("grade") or "",
        data.get("cert")  or "",
        data.get("card")  or "",
        f"${data['ebay_avg']}" if data.get("ebay_avg") else "",
        "", "",
        "",  # Paid
        "",  # Tracking Number
    ]]
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": row},
    ).execute()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/video')
def video():
    return Response(generate_frames(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')

def search_ebay_sold(query, limit=10):
    """Query eBay Finding API for completed/sold listings."""
    if not EBAY_APP_ID:
        return None, "eBay App ID not configured"
    params = {
        "OPERATION-NAME": "findCompletedItems",
        "SERVICE-VERSION": "1.0.0",
        "APP-NAME": EBAY_APP_ID,
        "GLOBAL-ID": "EBAY-US",
        "RESPONSE-DATA-FORMAT": "JSON",
        "keywords": query,
        "itemFilter(0).name": "SoldItemsOnly",
        "itemFilter(0).value": "true",
        "itemFilter(1).name": "ListingType",
        "itemFilter(1).value": "AuctionWithBIN,FixedPrice,Auction",
        "sortOrder": "EndTimeSoonest",
        "paginationInput.entriesPerPage": str(limit),
    }
    try:
        resp = requests.get(
            "https://svcs.ebay.com/services/search/FindingService/v1",
            params=params, timeout=8
        )
        resp.raise_for_status()
        data = resp.json()
        results = (
            data
            .get("findCompletedItemsResponse", [{}])[0]
            .get("searchResult", [{}])[0]
            .get("item", [])
        )
        prices = []
        sales = []
        for item in results:
            price_str = (
                item.get("sellingStatus", [{}])[0]
                    .get("currentPrice", [{}])[0]
                    .get("__value__", None)
            )
            if price_str:
                try:
                    prices.append(float(price_str))
                except ValueError:
                    pass
            end_time = (
                item.get("listingInfo", [{}])[0]
                    .get("endTime", [None])[0]
            )
            title = item.get("title", [None])[0]
            url = item.get("viewItemURL", [None])[0]
            if price_str and title:
                sales.append({
                    "title": title,
                    "price": float(price_str) if price_str else None,
                    "date": end_time[:10] if end_time else None,
                    "url": url,
                })
        if not prices:
            return {"sales": [], "avg": None, "high": None, "low": None, "count": 0}, None
        return {
            "sales": sales[:5],
            "avg": round(sum(prices) / len(prices), 2),
            "high": round(max(prices), 2),
            "low": round(min(prices), 2),
            "count": len(prices),
        }, None
    except Exception as e:
        return None, str(e)


@app.route('/value', methods=['POST'])
def value():
    body = request.get_json()
    card_desc = body.get("card", "")
    name = body.get("name", "")
    year = body.get("year", "")
    grade = body.get("grade", "")

    # Build search query from card data
    query_parts = [p for p in [str(year) if year else "", name, grade] if p]
    query = " ".join(query_parts) if query_parts else card_desc

    if not query.strip():
        return jsonify({"success": False, "error": "No card data to search"})

    result, err = search_ebay_sold(query)
    if err:
        return jsonify({"success": False, "error": err})
    return jsonify({"success": True, "ebay": result, "query": query})


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

        # Auto-fetch eBay value
        query_parts = [p for p in [str(data.get("year", "")), data.get("name", ""), data.get("grade", "")] if p]
        if query_parts:
            ebay_result, _ = search_ebay_sold(" ".join(query_parts))
            if ebay_result and ebay_result.get("avg"):
                data["ebay_avg"] = ebay_result["avg"]
                data["ebay_high"] = ebay_result["high"]
                data["ebay_low"] = ebay_result["low"]
                data["ebay_count"] = ebay_result["count"]
                data["ebay_sales"] = ebay_result["sales"]

        append_to_sheet(data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
