#!/usr/bin/env python3
"""One-time provisioning script for SlabVault starter sheet templates.

Creates the Google Sheets defined in sheet_templates.py under whichever
Google account the credentials belong to, formats them, and shares each one
as "anyone with the link can view" so the app can offer a `.../copy` link —
clicking it makes a private copy in the *user's* own Drive, it does not grant
them any access to the original.

This does NOT run automatically and is NOT called by app.py. Run it by hand,
once, whenever templates are added or changed:

    python3 create_sheet_templates.py            # create/update + print URLs
    python3 create_sheet_templates.py --check     # only validate header mapping

Needs the same Google service-account credentials app.py uses (either
GOOGLE_CREDS_B64 env var or a google_creds.json file next to this script),
but with Drive scope added so it can set the public-view permission.
"""

import argparse
import base64
import json
import os
import sys

from sheet_templates import TEMPLATES, CARDS_TAB, SUMMARY_TAB

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
CREDS_FILE = os.path.join(os.path.dirname(__file__), "google_creds.json")


def get_creds():
    from google.oauth2.service_account import Credentials
    b64 = os.environ.get("GOOGLE_CREDS_B64", "")
    if b64:
        creds_dict = json.loads(base64.b64decode(b64 + "==").decode("utf-8"))
        return Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    if os.path.exists(CREDS_FILE):
        return Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    print("No credentials found (set GOOGLE_CREDS_B64 or add google_creds.json).", file=sys.stderr)
    sys.exit(1)


def check_mapping():
    """Confirm every header in every template is recognized the way we expect."""
    from sheet_field_mapping import detect_column_mapping
    ok = True
    for tpl in TEMPLATES:
        headers = tpl["tabs"][CARDS_TAB]
        mapping = detect_column_mapping(headers)
        by_col = {}
        for field, idx in mapping.items():
            by_col.setdefault(idx, []).append(field)
        print(f"\n{tpl['name']} ({CARDS_TAB} tab):")
        for i, h in enumerate(headers):
            fields = by_col.get(i, [])
            if len(fields) > 1:
                ok = False
                print(f"  ✗ col {i} {h!r} matched MULTIPLE fields {fields} — one will silently overwrite another")
            elif not fields:
                print(f"  · col {i} {h!r} -> (unmapped, manual entry)")
            else:
                print(f"  ✓ col {i} {h!r} -> {fields[0]}")
    print("\n" + ("All templates map cleanly." if ok else "Fix the collisions above before provisioning."))
    return ok


def build_requests_for_cards_tab(sheet_id, headers):
    return [
        {"updateCells": {
            "rows": [{"values": [{"userEnteredValue": {"stringValue": h},
                                   "userEnteredFormat": {"textFormat": {"bold": True}}} for h in headers]}],
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
    ]


def build_requests_for_summary_tab(sheet_id, rows):
    values = []
    for label, formula in rows:
        values.append({"values": [
            {"userEnteredValue": {"stringValue": label}, "userEnteredFormat": {"textFormat": {"bold": True}}},
            {"userEnteredValue": {"formulaValue": formula}},
        ]})
    return [
        {"updateCells": {
            "rows": values,
            "fields": "userEnteredValue,userEnteredFormat.textFormat.bold",
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 180},
            "fields": "pixelSize",
        }},
    ]


def create_template(sheets_svc, drive_svc, tpl):
    tab_names = list(tpl["tabs"].keys())
    body = {
        "properties": {"title": f"SlabVault — {tpl['name']} Template"},
        "sheets": [{"properties": {"title": name}} for name in tab_names],
    }
    spreadsheet = sheets_svc.spreadsheets().create(body=body).execute()
    spreadsheet_id = spreadsheet["spreadsheetId"]
    sheet_id_by_name = {s["properties"]["title"]: s["properties"]["sheetId"] for s in spreadsheet["sheets"]}

    requests = []
    for tab_name, content in tpl["tabs"].items():
        gid = sheet_id_by_name[tab_name]
        if tab_name == SUMMARY_TAB:
            requests += build_requests_for_summary_tab(gid, content)
        else:
            requests += build_requests_for_cards_tab(gid, content)
    sheets_svc.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()

    # Anyone-with-link can VIEW (read-only) — needed for the public "/copy" flow.
    # They can never write to this original; a copy is a brand-new file they own.
    drive_svc.permissions().create(
        fileId=spreadsheet_id,
        body={"type": "anyone", "role": "reader"},
        fields="id",
    ).execute()

    return spreadsheet_id


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Only validate header->field mapping, don't create anything")
    args = parser.parse_args()

    if args.check:
        ok = check_mapping()
        sys.exit(0 if ok else 1)

    check_mapping()
    confirm = input("\nCreate/share the templates above under this service account's Drive? [y/N] ")
    if confirm.strip().lower() != "y":
        print("Aborted.")
        return

    from googleapiclient.discovery import build
    creds = get_creds()
    sheets_svc = build("sheets", "v4", credentials=creds)
    drive_svc = build("drive", "v3", credentials=creds)

    print()
    for tpl in TEMPLATES:
        spreadsheet_id = create_template(sheets_svc, drive_svc, tpl)
        copy_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/copy"
        edit_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        print(f"{tpl['name']}:")
        print(f"  edit (original, don't share this one): {edit_url}")
        print(f"  copy link (put this in the app):        {copy_url}")
        print(f"  -> set env var {tpl['env_var']}={copy_url}")
        print()


if __name__ == "__main__":
    main()
