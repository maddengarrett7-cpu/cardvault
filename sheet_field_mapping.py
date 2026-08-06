"""Header -> field detection for user-connected Google Sheets.

Pulled out of app.py so this pure logic (stdlib-only) can be unit-tested and
validated by create_sheet_templates.py without importing the whole Flask app
and its heavier dependencies (cv2, stripe, psycopg2, ...).
"""

import re as _re

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
    # NOTE: no bare "#" here — it's too ambiguous and used to false-match any
    # header with a standalone "#", e.g. "Cert #" (a card_number match would
    # silently blank out that column since scans rarely populate card_number).
    "card_number": (["card #", "card number", "card no"],
                    ["card#", "cardno"]),
    "value":    (["value", "ebay avg", "market value", "est value", "worth", "ebay value", "current value"],
                 ["ebay", "market val", "est. val"]),
    "paid":     (["paid", "cost", "bought for", "purchase price", "buy price", "my cost"],
                 ["paid", "cost"]),
    "notes":    (["notes", "note", "memo", "comments", "comment"],
                 ["notes"]),
    "tracking": (["tracking", "tracking #", "ship", "shipment"],
                 ["tracking"]),
    "date":     (["date", "date acquired", "date added", "date scanned", "scan date"],
                 ["date"]),
}


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
