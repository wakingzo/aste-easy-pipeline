"""Context-aware PVP sale-attempt parser, recovered from March 2026 enrichment.

Candidates are accepted only when an ordinal is locally related to a sale
event (avviso, bando, vendita, esperimento, tentativo, gara, incanto, tornata).
No stand-alone Roman numeral extraction. With multiple prior notices, select
the highest *contextual* attempt as the March parser did, but report conflicts
and evidence; don't imply that this heuristic is legally verified.

Priority: structured PVP numeEsperimento > filename fallback > blank.
"""
from __future__ import annotations

import re
import unicodedata

_ROMAN = {
    1: "I", 2: "II", 3: "III", 4: "IV", 5: "V",
    6: "VI", 7: "VII", 8: "VIII", 9: "IX", 10: "X",
    11: "XI", 12: "XII", 13: "XIII", 14: "XIV", 15: "XV",
    16: "XVI", 17: "XVII", 18: "XVIII", 19: "XIX", 20: "XX",
}
_REVERSE_ROMAN = {v: k for k, v in _ROMAN.items()}
_ORDINAL_WORDS = {
    "PRIMO": 1, "PRIMA": 1,
    "SECONDO": 2, "SECONDA": 2,
    "TERZO": 3, "TERZA": 3,
    "QUARTO": 4, "QUARTA": 4,
    "QUINTO": 5, "QUINTA": 5,
    "SESTO": 6, "SESTA": 6,
    "SETTIMO": 7, "SETTIMA": 7,
    "OTTAVO": 8, "OTTAVA": 8,
    "NONO": 9, "NONA": 9,
    "DECIMO": 10, "DECIMA": 10,
    "UNDICESIMO": 11, "UNDICESIMA": 11,
    "DODICESIMO": 12, "DODICESIMA": 12,
}
_CONTEXT = {
    "AVVISO", "AVVISI", "VENDITA", "VENDITE", "ESPERIMENTO",
    "ESPERIMENTI", "TENTATIVO", "TENTATIVI", "GARA", "GARE",
    "INCANTO", "INCANTI", "TORNATA", "TORNATE", "ASTA", "ASTE",
    "BANDO", "BANDI", "ESP", "EXP", "ESPERIM", "ESPER",
}
_STRONG = {
    "ESPERIMENTO", "ESPERIMENTI", "TENTATIVO", "TENTATIVI",
    "GARA", "GARE", "INCANTO", "INCANTI", "TORNATA", "TORNATE",
    # ESP/EXP are widespread abbreviations; ES is often "esecuzione", so exclude it.
    "ESP", "EXP", "ESPERIM", "ESPER",
}
_EXCLUDE_PRECEDING = {
    "LOTTO", "LOTTI", "FOGLIO", "PARTICELLA", "PART", "SUB",
    "SUBALTERNO", "PROCEDURA", "RGE", "RG", "RGEN", "PVP",
    "PAG", "PAGINA", "ART", "ARTICOLO",
}
_CONNECTORS = {"DI", "DELLA", "DELLE", "DEL", "N", "NR", "NUM", "NUMERO"}
# These markers label numbering explicitly. DEL/DI/DELLA are not number
# qualifiers; they often introduce the *date* of a sale instead.
_NUMBER_LABELS = {"N", "NR", "NUM", "NUMERO"}
_MONTHS = {
    "GENNAIO", "FEBBRAIO", "MARZO", "APRILE", "MAGGIO", "GIUGNO",
    "LUGLIO", "AGOSTO", "SETTEMBRE", "OTTOBRE", "NOVEMBRE", "DICEMBRE",
    "GEN", "FEB", "MAR", "APR", "MAG", "GIU", "LUG", "AGO", "SET",
    "OTT", "NOV", "DIC",
}
_TOKEN = re.compile(r"[A-Z]+|\d+(?:[°ºª^])?")


def _tokens(filename: str) -> list[str]:
    # In Italian filenames a lowercase "x" commonly abbreviates "per":
    # "avviso x pubblicazione", "ordinanza x asta". It is NOT Roman X.
    # Preserve uppercase X as a possible ordinal ("X AVVISO").
    s = unicodedata.normalize("NFKD", str(filename or ""))
    s = re.sub(r"(?<![A-Za-z0-9])x(?![A-Za-z0-9])", " ", s)
    s = s.upper()
    # Actual PVP titles sometimes use "6o ESPERIMENTO" (ordinal suffix o).
    # Normalize only with an immediate sales context, never in arbitrary codes.
    s = re.sub(
        r"(?<![A-Z0-9])(\d{1,2})O(?=[\s_.-]*(?:ESPERIMENTO|ESPERIM|ESP|EXP|BANDO|AVVISO|VENDITA|GARA|ASTA|TENTATIVO|INCANTO)\b)",
        r"\1°", s,
    )
    # PVP can concatenate the RGE identifier, Roman attempt and notice:
    # "RGE1062024IIAvvisodivendita" -> "RGE1062024 II AVVISO ..."
    # Only split when a complete numeral is followed by explicit AVVISO DI VENDITA.
    s = re.sub(
        r"(?<=\d)([IVX]{1,6})(?=AVVISO(?:DI)?VENDITA)",
        r" \1 AVVISO ", s,
    )
    # PVP's automatically generated "attoprincipale_1" refers to attachment
    # sequence, not necessarily to the sale attempt.
    s = re.sub(r"(?:\d{8,})?ATTOPRINCIPALE[\s_-]*\d+", " ", s)
    # "(1)" at the very end is commonly an OS duplicate-file suffix,
    # not "first experiment" after "secondo esperimento".
    s = re.sub(r"\(\s*\d{1,2}\s*\)(?=(?:\.(?:PDF|DOCX?|ODT|RTF|TXT|P7M))+$)", " ", s)
    s = re.sub(r"(?:\.(?:PDF|DOCX?|ODT|RTF|TXT|P7M))+$", " ", s)
    # Remove dates and numerical case references *before* splitting separators:
    # "27-16 avviso 4 asta 10-02-27" -> only "4 asta" is a candidate.
    # "AVVISO VENDITA DEL 19-02-2027" -> no candidate.
    # Keep standalone ordinal + context, including "2-avviso" and "VI_gara".
    s = re.sub(r"(?<!\d)\d{1,4}(?:\s*[-/._]\s*\d{1,4}){1,3}(?!\d)", " ", s)
    s = re.sub(r"[_/\-]+", " ", s)
    return _TOKEN.findall(s)


def _ordinal(token: str):
    if token in _ORDINAL_WORDS:
        return _ORDINAL_WORDS[token], "word"
    if token in _REVERSE_ROMAN:
        return _REVERSE_ROMAN[token], "roman"
    m = re.fullmatch(r"(\d{1,2})([°ºª^])?", token)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 20 and not token.startswith("0"):
            return n, ("ordinal" if m.group(2) else "cardinal")
        if 1 <= n <= 9 and re.fullmatch(r"0[1-9]", token):
            # Accept 01..09 only when explicitly coupled with ESP/EXP etc.
            return n, "padded"
    return None


def _context_for(tokens: list[str], i: int, kind: str):
    """Require adjacency or short links, never an arbitrary filename-wide match."""
    left = tokens[:i]
    right = tokens[i + 1:]
    if left and left[-1] in _EXCLUDE_PRECEDING:
        return None
    if kind == "padded" and not (
        (left and left[-1] in _STRONG)
        or (right and right[0] in _STRONG)
    ):
        return None
    # "secondo esperimento 14 gennaio": 14 is a calendar day, not an
    # additional fourteenth sale. Both can occur in the same filename.
    if kind == "cardinal" and right and right[0] in _MONTHS:
        return None
    # "29 10 AVVISO": day + month written with spaces.
    if (kind == "cardinal" and left and left[-1].isdigit()
            and right and right[0] in _CONTEXT):
        previous_day = int(left[-1])
        if 21 <= previous_day <= 31 and 1 <= int(tokens[i]) <= 12:
            return None
    # "avviso vendita nr 10 2024 R.G.": "10/2024" case number.
    if (kind == "cardinal" and right and right[0].isdigit()
            and 1900 <= int(right[0]) <= 2100 and len(left) > 1
            and left[-1] in _NUMBER_LABELS):
        return None
    # Roman X can mean the tenth sale, but it also means Italian "per"
    # (sometimes uppercase), or a calendar month. Require explicit nearby
    # sale context, not "vendita X DIC" or "incanto X comune".
    if tokens[i] == "X" and kind == "roman":
        if right and right[0] in _MONTHS:
            return None
        if right and right[0] not in _CONTEXT and right[0] not in {
            "SIGNED", "FIRMATO", "FTO", "REDACTED", "FINAL", "DEFINITIVO"
        }:
            return None

    # 6° gara; VI esperimento; SESTA GARA; IV AVVISO VENDITA
    after = right[:3]
    if after:
        if after[0] in _CONTEXT:
            return after[0], "number_before_context"
        if after[0] in _CONNECTORS and len(after) > 1 and after[1] in _CONTEXT:
            return after[1], "number_before_context"

    # esperimento 3, gara VI, secondo tentativo, avviso n. 4
    before = left[-3:]
    if before:
        if before[-1] in _STRONG:
            return before[-1], "context_before_number"
        if len(before) > 1 and before[-1] in _NUMBER_LABELS and before[-2] in _CONTEXT:
            return before[-2], "numbered_context"
        if before[-1] in {"AVVISO", "AVVISI", "VENDITA", "VENDITE", "ASTA", "ASTE", "BANDO", "BANDI"}:
            # A plain "avviso 2" / "vendita 2" can be a version/copy suffix.
            # The ordinal sign, a Roman numeral or a written ordinal makes it explicit.
            if kind != "cardinal":
                return before[-1], "explicit_ordinal_after_context"
            if right and right[0] in {"VENDITA", "VENDITE"} and before[-1] in {"AVVISO", "AVVISI"}:
                return before[-1], "avviso_number_vendita"
    return None


def parse_sale_attempt_from_filenames(filenames) -> dict:
    """Return a roman attempt plus per-file evidence (empty if no confident match).

    Multiple valid numbered notices: keep the maximum, as in the March 2026
    parser, and flag the ambiguity for manual review/auditing.
    """
    matches = []
    seen = set()
    for filename in filenames or ():
        filename = str(filename or "").strip()
        if not filename:
            continue
        tokens = _tokens(filename)
        for i, tok in enumerate(tokens):
            numeric = _ordinal(tok)
            if numeric is None:
                continue
            n, kind = numeric
            context = _context_for(tokens, i, kind)
            if not context:
                continue
            label, rule = context
            dedupe = (filename, n, label, rule)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            matches.append({
                "filename": filename, "value": _ROMAN[n], "number": n,
                "marker": tok, "context": label, "rule": rule,
            })

    if not matches:
        return {"value": "", "number": None, "evidence": [], "multiple_values": False}
    numbers = {m["number"] for m in matches}
    number = max(numbers)
    return {
        "value": _ROMAN[number],
        "number": number,
        "evidence": matches,
        "multiple_values": len(numbers) > 1,
    }


def attempt_number(value):
    raw = str(value or "").strip().upper()
    if raw in _REVERSE_ROMAN:
        return _REVERSE_ROMAN[raw]
    if re.fullmatch(r"(?:[1-9]|1\d|20)", raw):
        return int(raw)
    return None


def resolve_sale_attempt(detail_body, search_item, extra_filenames=()) -> dict:
    """API wins; filename parser is fallback; audit agreement independently."""
    detail = detail_body if isinstance(detail_body, dict) else {}
    search = search_item if isinstance(search_item, dict) else {}
    filenames = []
    for a in (detail.get("allegati") or []):
        if isinstance(a, dict):
            filenames.append(a.get("nomeFile") or a.get("filename") or a.get("nomeAllegato") or "")
    filenames += list(extra_filenames or [])
    # Deduplicate preserving order; don't accidentally treat duplicate filenames
    # as stronger evidence.
    filenames = list(dict.fromkeys(x for x in filenames if x))
    parsed = parse_sale_attempt_from_filenames(filenames)
    api_raw = detail.get("numeEsperimento")
    api_source = "api_detail"
    if api_raw is None or not str(api_raw).strip():
        api_raw = search.get("numeEsperimento")
        api_source = "api_search"
    api_value = str(api_raw).strip() if api_raw is not None else ""
    if re.fullmatch(r"[IVXLC]+/[IVXLC]+", api_value, flags=re.I):
        api_value = api_value.split("/")[0]
    api_number = attempt_number(api_value)
    parser_number = parsed["number"]
    comparable = api_number is not None and parser_number is not None
    if api_value:
        value, source = api_value, api_source
    elif parsed["value"]:
        value, source = parsed["value"], "filename"
    else:
        value, source = "", "missing"
    return {
        "value": value,
        "source": source,
        "api_value": api_value,
        "api_number": api_number,
        "parsed_value": parsed["value"],
        "parsed_number": parser_number,
        "api_parser_agree": (api_number == parser_number) if comparable else None,
        "multiple_filename_values": parsed["multiple_values"],
        "evidence": parsed["evidence"],
    }
