"""PVP sale fields: only structured API data, never attachment filename guesses.

No I/first-sale default: an absent API field stays absent. Promotional catalog
identifiers (Quimmo "Codice annuncio") are not valid lot labels when identical
to the reported 'numeroLotto'. Only the two dedicated numeroLotto fields are
eligible; codiLotto is an internal code, not a human lot number.
"""
from __future__ import annotations

import re
from typing import Any


def _clean(value: Any) -> str:
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    raw = " ".join(str(value).replace("\u00a0", " ").split()).strip()
    return "" if raw.upper() in {"NULL", "NONE", "-", "NON_SPECIFICATO", "NON_SPECIFICATA"} else raw


def pvp_sale_attempt(detail_body: dict | None, search_item: dict | None) -> str:
    """Read the two PVP numeEsperimento fields; blank is never assumed to be I."""
    detail_body = detail_body if isinstance(detail_body, dict) else {}
    search_item = search_item if isinstance(search_item, dict) else {}
    for parent in (detail_body, search_item):
        value = _clean(parent.get("numeEsperimento"))
        if not value:
            continue
        # Retain the legacy treatment of values such as "II/III"; no filename parsing.
        if re.fullmatch(r"[IVXLC]+/[IVXLC]+", value, flags=re.I):
            value = value.split("/")[0]
        return value
    return ""


def _is_promotional_code(number: str, description: str) -> bool:
    """Suppress only an exact, explicitly labelled advertisement code.

    Never discard a real numeric lot merely for having many digits.
    """
    if not re.fullmatch(r"\d{6,12}", number):
        return False
    return bool(re.search(
        r"\bcodice\s+annuncio\s*(?:n\.?\s*|n[°º]\s*|[:#-]\s*)?"
        + re.escape(number) + r"(?!\d)",
        description or "",
        flags=re.I,
    ))


def pvp_lot_number(detail_body: dict | None, search_item: dict | None, description: str = "") -> str:
    """Trust numeroLotto only; reject exact Quimmo advertising-code collisions."""
    detail_body = detail_body if isinstance(detail_body, dict) else {}
    search_item = search_item if isinstance(search_item, dict) else {}
    nested_lotto = detail_body.get("lotto") or {}
    if not isinstance(nested_lotto, dict):
        nested_lotto = {}
    for raw in (search_item.get("numeroLotto"), nested_lotto.get("numeroLotto")):
        value = _clean(raw)
        if value and not _is_promotional_code(value, description):
            return value
    return ""
