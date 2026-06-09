#!/usr/bin/env python3
"""
Sports Card Scanner - Auto mode
- Hold card in front of camera
- Press S in the camera window to scan
- Press Q in the camera window to quit
"""

import os
import sys
import json
import time
from datetime import datetime
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


def analyze_card(frame) -> dict:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-flash-latest")
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
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
        "               TCG:    'POKEMON SET CARD_NUMBER RARITY GRADE' e.g. 'Charizard Base Set 4/102 Holo Rare PSA 9'\n\n"
        "For raw cards read the card face. For graded slabs read the label. Skip unknown parts."
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


def append_to_sheet(data: dict):
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    svc    = build("sheets", "v4", credentials=creds)
    row = [[
        data.get("name")  or "",
        data.get("year")  or "",
        data.get("grade") or "",
        data.get("cert")  or "",
        data.get("card")  or "",
        "", "",
        "",   # Paid
        "",   # Tracking Number
    ]]
    svc.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
        valueInputOption="USER_ENTERED",
        body={"values": row},
    ).execute()


def ensure_sheet_header():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds  = Credentials.from_service_account_file(GOOGLE_CREDS_FILE, scopes=scopes)
    svc    = build("sheets", "v4", credentials=creds)
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!A1",
    ).execute()
    if not result.get("values"):
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{SHEET_TAB}!A1",
            valueInputOption="RAW",
            body={"values": [["Timestamp", "Name", "Year", "Grade", "Card", "", "", "Paid", "Tracking Number"]]},
        ).execute()
        print("✓ Header row written to sheet.")


def check_config():
    errors = []
    if not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY environment variable is not set.")
    if not SPREADSHEET_ID:
        errors.append("SPREADSHEET_ID environment variable is not set.")
    if not os.path.exists(GOOGLE_CREDS_FILE):
        errors.append(f"Google credentials file not found: {GOOGLE_CREDS_FILE}")
    if errors:
        print("\n⚠️  Setup incomplete:\n")
        for e in errors:
            print(f"  • {e}")
        sys.exit(1)


def main():
    check_config()
    ensure_sheet_header()

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("✗ Could not open camera.")
        sys.exit(1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)

    print("\n📷  Card Scanner ready.")
    print("   Click the camera window then press S to scan, Q to quit.\n")

    status_msg = "Click this window, press S to scan"
    scanning = False

    while True:
        ret, frame = cap.read()
        if not ret:
            time.sleep(0.05)
            continue

        display = frame.copy()
        color = (0, 255, 0) if not scanning else (0, 165, 255)
        cv2.putText(display, status_msg,
                    (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
        cv2.imshow("Card Scanner", display)

        key = cv2.waitKey(30) & 0xFF

        if key == ord('q') or key == 27:
            break

        if key == ord('s') and not scanning:
            scanning = True
            status_msg = "Scanning..."
            print("📸 Scanning card...", flush=True)

            # Capture fresh frame
            for _ in range(5):
                cap.read()
            ret2, scan_frame = cap.read()

            try:
                data = analyze_card(scan_frame)
                append_to_sheet(data)
                card_label = data.get("card") or "Card logged!"
                status_msg = f"✓ {card_label} - press S for next"
                print(f"✓ {data}")
                print("✓ Logged to Google Sheet!\n")
            except Exception as e:
                status_msg = "Error - press S to try again"
                print(f"✗ Error: {e}\n")
            finally:
                scanning = False

    cap.release()
    cv2.destroyAllWindows()
    print("Goodbye!")


if __name__ == "__main__":
    main()
