"""Excel worksheet-name rules."""

import re

BAD_SHEET_NAME = re.compile(r"[\[\]:*?/\\]")
SHEET_NAME_MAX = 31


def check_sheet_name(name: str) -> str:
    if not name or len(name) > SHEET_NAME_MAX or BAD_SHEET_NAME.search(name) or name.strip("'") != name:
        raise ValueError(f"invalid sheet name {name!r}: 1-{SHEET_NAME_MAX} chars, none of []:*?/\\, no edge quotes")
    if name.lower() == "history":
        raise ValueError("sheet name 'History' is reserved by Excel")
    return name


def sanitize_sheet_name(name: str) -> str:
    """Closest valid sheet name: bad characters replaced, edge quotes stripped, truncated."""
    return BAD_SHEET_NAME.sub("_", name).strip("'")[:SHEET_NAME_MAX] or "_"
