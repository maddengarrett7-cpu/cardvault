"""Starter Google Sheet templates offered to SlabVault users.

Each template defines the header row for the "Cards" tab that the scanner
writes to (see append_to_sheet/build_row in app.py). Headers here are chosen
so every column is recognized by FIELD_KEYWORDS in app.py — run
`python3 create_sheet_templates.py --check` after editing to confirm the
mapping is still 100%.

The actual Google Sheet documents are provisioned once (offline, with real
Google credentials) by create_sheet_templates.py, which prints a
`.../copy` URL for each template. Those URLs are then set as env vars
(SHEET_TEMPLATE_<ID>_URL) — this module never talks to Google itself.
"""

import os

CARDS_TAB = "Cards"
SUMMARY_TAB = "Summary"

TEMPLATES = [
    {
        "id": "basic",
        "env_var": "SHEET_TEMPLATE_BASIC_URL",
        "name": "Basic Collection",
        "emoji": "📋",
        "description": "Simple log of every card you scan — card, year, set, grade, and value.",
        "tabs": {
            CARDS_TAB: [
                "Card", "Year", "Set", "Parallel", "Grade", "Cert #", "Value", "Date Added",
            ],
        },
    },
    {
        "id": "reseller",
        "env_var": "SHEET_TEMPLATE_RESELLER_URL",
        "name": "Reseller / Inventory",
        "emoji": "💰",
        "description": "Track cost, listing platform, and sale price for cards you flip — plus a Summary tab with totals and profit.",
        "tabs": {
            CARDS_TAB: [
                "Card", "Year", "Set", "Grade", "Cert #", "Est. Value",
                "Date Acquired", "Cost", "Platform", "Listed Price",
                "Date Sold", "Sold Price",
            ],
            # Summary tab is a dashboard only — the scanner never writes to it,
            # so formulas here are safe from being clobbered by values.append().
            # Column refs: F=Est. Value, G=Date Acquired, H=Cost, I=Platform,
            # J=Listed Price, K=Date Sold, L=Sold Price.
            SUMMARY_TAB: [
                ("Cards in inventory", f'=COUNTA({CARDS_TAB}!A2:A)-COUNTA({CARDS_TAB}!K2:K)'),
                ("Total invested", f'=SUM({CARDS_TAB}!H2:H)'),
                ("Total sold revenue", f'=SUM({CARDS_TAB}!L2:L)'),
                ("Realized profit", f'=SUMPRODUCT(({CARDS_TAB}!L2:L<>"")*{CARDS_TAB}!L2:L)-SUMPRODUCT(({CARDS_TAB}!L2:L<>"")*{CARDS_TAB}!H2:H)'),
            ],
        },
    },
]


def get_template_url(template_id):
    tpl = next((t for t in TEMPLATES if t["id"] == template_id), None)
    if not tpl:
        return None
    return os.environ.get(tpl["env_var"], "").strip() or None


def list_templates_for_api():
    """Templates whose copy URL has been configured (i.e. actually provisioned)."""
    out = []
    for tpl in TEMPLATES:
        url = get_template_url(tpl["id"])
        if not url:
            continue
        out.append({
            "id": tpl["id"],
            "name": tpl["name"],
            "emoji": tpl["emoji"],
            "description": tpl["description"],
            "copy_url": url,
        })
    return out
