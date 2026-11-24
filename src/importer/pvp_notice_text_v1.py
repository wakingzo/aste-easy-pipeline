"""First-page PVP notice PDF text extraction. NO OCR, AI, DB or network access.

This module handles *bytes already fetched by a caller*; it does not make
assumptions that a missing experiment means the first attempt.
"""
from __future__ import annotations

import io
import re
import unicodedata
from pypdf import PdfReader

_ROMANS = {
    1:"I", 2:"II", 3:"III", 4:"IV", 5:"V", 6:"VI", 7:"VII",
    8:"VIII", 9:"IX", 10:"X", 11:"XI", 12:"XII", 13:"XIII",
    14:"XIV", 15:"XV", 16:"XVI", 17:"XVII", 18:"XVIII",
    19:"XIX", 20:"XX",
}
_BY_ROMAN = {v:k for k,v in _ROMANS.items()}
_WORDS = {
    "PRIMO": 1, "PRIMA": 1, "SECONDO": 2, "SECONDA": 2,
    "TERZO": 3, "TERZA": 3, "QUARTO": 4, "QUARTA": 4,
    "QUINTO": 5, "QUINTA": 5, "SESTO": 6, "SESTA": 6,
    "SETTIMO": 7, "SETTIMA": 7, "OTTAVO": 8, "OTTAVA": 8,
    "NONO": 9, "NONA": 9, "DECIMO": 10, "DECIMA": 10,
    "UNDICESIMO": 11, "UNDICESIMA": 11, "DODICESIMO": 12,
    "DODICESIMA": 12,
}
# Longest first, so XVI is never read as VI.
_ORD = (r"(?:UNDICESIM[OA]|DODICESIM[OA]|SETTIM[OA]|SECON[DOA]{2}|"
        r"QUART[OA]|QUINT[OA]|OTTAV[OA]|DECIM[OA]|TERZ[OA]|"
        r"SEST[OA]|PRIM[OA]|NON[OA]|"
        r"X{0,2}(?:IX|IV|V?I{0,3})|"
        r"20|1[0-9]|[1-9])")
# Require explicit label, not "art. 12" or "lotto IV".
_AFTER = re.compile(
    rf"(?<![A-Z0-9])(?P<token>{_ORD})\s*[°ºª^O]?\s+"
    rf"(?P<label>ESPERIMENTO|TENTATIVO)(?:\s+DI\s+VENDITA)?\b",
    re.I,
)
_BEFORE = re.compile(
    rf"\b(?P<label>ESPERIMENTO|TENTATIVO)\s*"
    rf"(?:N[.°º]?\s*|NUMERO\s+)?(?P<token>{_ORD})(?![A-Z0-9])",
    re.I,
)
# No "GARA" in PDF text extraction by default: a document can contain many
# legal references to a gara; the heading uses ESPERIMENTO or TENTATIVO.

def _fold(value):
    return unicodedata.normalize("NFKD", str(value or "")).upper()

def _attempt(token):
    s = str(token).upper()
    if s in _WORDS:
        return _WORDS[s]
    if s in _BY_ROMAN:
        return _BY_ROMAN[s]
    if s.isdigit() and 1 <= int(s) <= 20 and not s.startswith("0"):
        return int(s)
    return None

def _type(text):
    t = re.sub(r"\s+", " ", _fold(text))
    if "CONCORDATO PREVENTIVO" in t:
        return "concordato_preventivo"
    if "LIQUIDAZIONE GIUDIZIALE" in t or "FALLIMENTO" in t:
        return "procedura_concorsuale"
    if ("PROCEDURA ESECUTIVA" in t or "ESEC. IMMOBILIARI" in t
            or "ESECUZIONE IMMOBILIARE" in t or "R.G.E." in t):
        return "esecuzione_immobiliare"
    return "da_verificare"

def extract_attempt_from_first_page_text(text: str) -> dict:
    """Use just first-page heading, preserve source line and ambiguity.

    Scan at most the first 65 nonempty lines. If conflicting ordinals are
    found in the heading, DO NOT select the highest; mark for manual review.
    """
    lines = [re.sub(r"\s+", " ", _fold(line)).strip()
             for line in str(text or "").splitlines() if line.strip()]
    # Some PDFs wrap ordinal and label across two lines: retain line windows.
    head = lines[:65]
    evidence = []
    seen = set()
    for index, line in enumerate(head):
        candidates = [(line, index+1)]
        if index+1 < len(head):
            candidates.append((line+" "+head[index+1], index+1))
        for candidate, line_no in candidates:
            for pattern in (_AFTER, _BEFORE):
                for m in pattern.finditer(candidate):
                    value = _attempt(m.group("token"))
                    if value is None:
                        continue
                    marker = (index, value, m.group(0))
                    if marker in seen:
                        continue
                    seen.add(marker)
                    evidence.append({"number": value, "roman": _ROMANS[value],
                                     "text": m.group(0)[:150],
                                     "line": line_no})
    numbers = set(x["number"] for x in evidence)
    result = {
        "status": ("ambiguous" if len(numbers)>1 else
                   "explicit" if len(numbers)==1 else "not_found"),
        "roman": _ROMANS[next(iter(numbers))] if len(numbers)==1 else None,
        "number": next(iter(numbers)) if len(numbers)==1 else None,
        "evidence": evidence[:8],
        "document_type": _type(text[:6500]),
    }
    return result

def read_first_page_pdf(pdf_bytes: bytes) -> dict:
    """Read page 1 only, record metadata; input bytes are never saved."""
    if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("Not a PDF document")
    reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted PDF")
    n_pages = len(reader.pages)
    if n_pages < 1 or n_pages > 400:
        raise ValueError("Unexpected PDF page count")
    text = reader.pages[0].extract_text() or ""
    if len(text.strip()) < 50:
        return {
            "status": "unreadable_text", "roman": None, "number": None,
            "evidence": [], "document_type": "da_verificare",
            "extracted_chars": len(text), "pages_total": n_pages,
        }
    parsed = extract_attempt_from_first_page_text(text)
    parsed["extracted_chars"] = len(text)
    parsed["pages_total"] = n_pages
    parsed["page"] = 1 if parsed["evidence"] else None
    return parsed
