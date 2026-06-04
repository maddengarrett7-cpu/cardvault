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
    model = genai.GenerativeModel("gemini-flash-latest")
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
        append_to_sheet(data)
        return jsonify({'success': True, 'data': data})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    print("\n🚀 Card Scanner Web App")
    print("   Open this in your browser: http://localhost:5000\n")
    app.run(debug=False, host='0.0.0.0', port=5000)
