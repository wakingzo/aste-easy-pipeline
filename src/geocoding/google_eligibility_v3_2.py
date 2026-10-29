#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure offline Google eligibility gate — policy v3.4.

Principles:
- PVP coordinates inside the municipality are NOT address verification.
- ANNCSU is used first; Google is only eligible when a coherent, specific
  address/locality remains unresolved.
- Municipality-only, cadastral, placeholder and non-disambiguated multi-bene
  inputs do not spend Google quota.
- SS/SP are treated conservatively: a bare, plausible numeric route can be
  considered specific; a named/destination-style route that cannot be verified
  locally is held for review rather than silently corrected.
- Parsing never mutates the source row or invents a corrected road.
"""
from __future__ import annotations

import re
import unicodedata

# Keep the historical module filename because the consolidated v3 entrypoint imports it.
# Policy version is v3.4.
ROAD = re.compile(
    r"(?<!\w)(?:"
    r"piazzetta|piazzale|piazza|lungomare|"
    r"strada\s+(?:statale|provinciale|comunale|vicinale)|"
    r"str\.|strada|"
    r"sestiere|regione|rione|borgo|borgata|"
    r"localit(?:a|à|'|’)|loc\.|frazione|fraz\.|"
    r"contrada|c\s*/\s*da|c\s*\.\s*da|"
    r"viale|vicolo|vico|corso|largo|traversa|via|v\.|"
    r"s\.?\s*s\.?|s\.?\s*p\.?)\s+",
    re.I,
)
ADMIN_TAIL = re.compile(r"\b\d{5}\b|\b(?:provincia\s+di|comune\s+di)\b", re.I)
CADASTRAL = re.compile(r"\b(?:foglio|mappal[ei]|particell[ae]|subalterno|catastal[ei]|sub\.)\b", re.I)
BAD_NAME = re.compile(
    r"^(?:n\s*\.?\s*d\s*\.?|n\s*\.?\s*c\s*\.?|s\s*\.?\s*n\s*\.?\s*c\s*\.?|"
    r"non\s+(?:indicat[ao]|disponibile|nota)|senza\s+(?:nome|denominazione)|"
    r"da\s+(?:definire|accertare)|ignot[ao]|sconosciut[ao]|"
    r"generica?|zona|centro|periferia|diverse?|varie?|strada|via|localit[aà]|agro)$",
    re.I,
)
NUMERIC_ONLY = re.compile(r"^(?:\d+[a-z]?|s\s*\.?\s*n\s*\.?\s*c\s*\.?|km\s*[\d.,]+)$", re.I)
# Handles 12, 12A, 12/A, 128/23, 52-54, 12 bis. Five-digit CAPs are excluded.
CIVIC = r"\d{1,4}(?:\s*[a-z]|\s*(?:[-/]\s*[0-9a-z]{1,4})|\s+bis|\s+ter)?"
EXPLICIT_CIVIC = re.compile(
    r"(?:\b(?:n(?:um(?:ero)?)?|civ(?:ico)?)\s*\.?\s*)(" + CIVIC + r")(?=\s|[,;]|$)", re.I
)
BARE_CIVIC = re.compile(r"(?<![\w])(" + CIVIC + r")(?=\s|[,;]|$)", re.I)
NEXT_PIECE_CIVIC = re.compile(r"^(?:n\s*\.?\s*|civ(?:ico)?\s*\.?\s*)?(" + CIVIC + r")$", re.I)
# Aste can legitimately list more than one house number. For geocoding/map display
# we only need one representative civic; the original address string is preserved.
MULTI_CIVIC_PREFIX = re.compile(
    r"^(?:n\s*\.?\s*|civ(?:ico)?\s*\.?\s*)?(\d{1,4}[a-z]?)\s*(?:[-–—]|\be\b|\bed\b)\s*\d{1,4}[a-z]?",
    re.I,
)
NEXT_PIECE_CIVIC_PREFIX = re.compile(
    r"^(?:n\s*\.?\s*|civ(?:ico)?\s*\.?\s*)?(" + CIVIC + r")(?=\s|[,;]|$)",
    re.I,
)

LOCALITY_KINDS = {
    'localita', 'loc', 'frazione', 'fraz', 'borgata', 'contrada', 'cda',
    'regione', 'rione', 'borgo', 'sestiere',
}
EXTRAURBAN_KINDS = {'ss', 'sp', 'stradastatale', 'stradaprovinciale'}
# Route id only; free text after the id is intentionally treated as unverified.
ROUTE_ID = re.compile(r"^\s*(\d{1,4})(?:\s+(bis|ter|quater))?\s*$", re.I)
ROUTE_WITH_NAME = re.compile(r"^\s*(\d{1,4})(?:\s+(?:bis|ter|quater))?\s+.+$", re.I)


def canonical(value):
    text = unicodedata.normalize('NFKD', str(value or '').casefold())
    return ''.join(ch for ch in text if not unicodedata.combining(ch))


def _norm_name(value):
    return ' '.join(canonical(value).split()).strip(' ,.;:/-')


def _prefix_code(value):
    return re.sub(r'[^a-z0-9]+', '', canonical(value))


def _admin_values(row):
    values = []
    for key in ('comune', 'provincia', 'sigla_provincia', 'regione'):
        value = _norm_name(row.get(key))
        if value:
            values.append(value)
    return values


def _tail_is_admin(tail, row):
    """True only when text after a civic is clearly administrative repetition."""
    t = _norm_name(tail)
    if not t:
        return True
    # Remove CAP before comparing with known admin labels.
    t = re.sub(r'\b\d{5}\b', ' ', t)
    t = ' '.join(t.split()).strip(' ,.;:/-')
    if not t:
        return True
    values = _admin_values(row)
    if not values:
        return False
    # Repeated city/province strings are common in PVP exports.
    work = ' ' + t + ' '
    for value in sorted(values, key=len, reverse=True):
        work = work.replace(' ' + value + ' ', ' ')
        # Also tolerate repeated occurrences after one replacement.
        work = work.replace(' ' + value + ' ', ' ')
    work = re.sub(r'\b(?:provincia|comune|regione|di)\b', ' ', work)
    return not ' '.join(work.split())


def _extract_civic(segment, pieces, row, *, allow_bare=True):
    """Return (clean_name, civic). Never treats a five-digit CAP as a civic."""
    raw = segment.strip(' ,.;:/-')
    marker = EXPLICIT_CIVIC.search(raw)
    if marker:
        civic = re.sub(r'\s+', '', marker.group(1))
        tail = raw[marker.end():]
        # Even if trailing text is odd, the marker itself is explicit; name ends before it.
        return raw[:marker.start()].strip(' ,.;:/-'), civic

    if allow_bare:
        candidates = list(BARE_CIVIC.finditer(raw))
        for match in reversed(candidates):
            # Need meaningful text before the candidate; avoid route/CAP-only inputs.
            before = raw[:match.start()].strip(' ,.;:/-')
            tail = raw[match.end():].strip(' ,.;:/-')
            if not re.search(r'[A-Za-zÀ-ÿ]{2,}', before):
                continue
            if tail and not _tail_is_admin(tail, row):
                continue
            civic = re.sub(r'\s+', '', match.group(1))
            return before, civic

    # Civic may be in the next comma-separated piece: "Regione X, 12, 11100...".
    # PVP also emits multi-civic forms such as "9 e 29", "31-33-35" or
    # "66 – 68 – 70". One representative civic is enough for one map marker;
    # never rewrite the source address or invent coordinates for the other civics.
    if len(pieces) > 1:
        next_piece = pieces[1].strip()
        multi = MULTI_CIVIC_PREFIX.match(next_piece)
        if multi:
            return raw, re.sub(r'\s+', '', multi.group(1))
        next_civic = NEXT_PIECE_CIVIC.fullmatch(next_piece)
        if next_civic:
            return raw, re.sub(r'\s+', '', next_civic.group(1))
        prefix = NEXT_PIECE_CIVIC_PREFIX.match(next_piece)
        if prefix:
            civic = re.sub(r'\s+', '', prefix.group(1))
            tail = next_piece[prefix.end():].strip(' ,.;:/-')
            # Common export form: "6B 09045 Comune ...". Only accept trailing
            # text when it is clearly administrative repetition.
            if not tail or _tail_is_admin(tail, row):
                return raw, civic
    return raw, ''


def address_parts(row):
    """Interpret only the chosen bene's address; never invent a corrected road."""
    address = str(row.get('indirizzo') or '').strip()
    municipality = str(row.get('comune') or '').strip()
    fail = lambda reason: {'kind': '', 'name': '', 'civic': '', 'reason': reason}
    if not municipality:
        return fail('COMUNE_NON_IDENTIFICATO')
    if not address or not ROAD.search(address):
        return fail('INDIRIZZO_GENERICO_O_CATASTALE')

    raw_matches = list(ROAD.finditer(address))
    # Prefix words can legitimately occur inside a street name (e.g.
    # "Via Rione Cacia" or "Traversa di Via San Rocco"). Treat a second
    # prefix as a new toponym only after a separator, except when an SS/SP is
    # involved: "SP 28 ... Via Diaz" must remain an explicit mixed-address
    # review case.
    matches = []
    for match in raw_matches:
        if not matches:
            matches.append(match); continue
        prev = matches[-1]
        between = address[prev.end():match.start()]
        prev_code = _prefix_code(prev.group(0))
        cur_code = _prefix_code(match.group(0))
        connector = _norm_name(between)
        if (re.search(r'[,;]', between) or prev_code in EXTRAURBAN_KINDS or cur_code in EXTRAURBAN_KINDS
                or (connector and connector not in {'di','del','della','dei','degli','delle'})):
            matches.append(match)
    named = []
    extraurban_seen = False
    extraurban_bad_reason = ''

    for index, match in enumerate(matches):
        prefix_raw = match.group(0)
        prefix = _prefix_code(prefix_raw)
        extraurban = prefix in EXTRAURBAN_KINDS
        extraurban_seen |= extraurban
        kind = 'LOCALITA' if prefix in LOCALITY_KINDS else 'STRADA'

        remainder = address[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(address)]
        pieces = remainder.split(',')
        segment = pieces[0].strip()
        segment = ADMIN_TAIL.split(segment, maxsplit=1)[0].strip(' ,.;:/-')
        segment = CADASTRAL.split(segment, maxsplit=1)[0].strip(' ,.;:/-')
        if not segment:
            continue

        if extraurban:
            # Explicit "n. 45" after a route/name is preserved as civic, but the
            # route identity itself must be independently plausible.
            route_segment, civic = _extract_civic(segment, pieces, row, allow_bare=False)
            route_norm = _norm_name(route_segment)
            if ROUTE_ID.fullmatch(route_norm):
                route_label = re.sub(r'\s+', ' ', prefix_raw.strip()) + ' ' + route_segment.strip()
                named.append({'kind': 'STRADA', 'name': route_label.strip(),
                              'normalized': _norm_name(route_label), 'civic': civic,
                              'extraurban': True, 'route_numeric': True})
            elif ROUTE_WITH_NAME.fullmatch(route_norm):
                extraurban_bad_reason = 'STRADA_EXTRAURBANA_NUMERO_E_NOME_NON_VERIFICATI'
            else:
                extraurban_bad_reason = 'STRADA_EXTRAURBANA_NON_VERIFICATA'
            continue

        clean_segment, civic = _extract_civic(segment, pieces, row, allow_bare=True)
        norm = _norm_name(clean_segment)
        if not norm or BAD_NAME.fullmatch(norm) or NUMERIC_ONLY.fullmatch(norm):
            continue
        if not re.search(r'[a-z]{2,}', norm) or norm == _norm_name(municipality):
            continue
        named.append({'kind': kind, 'name': clean_segment.strip(), 'normalized': norm,
                      'civic': civic, 'extraurban': False, 'route_numeric': False})

    if extraurban_seen:
        non_route = [n for n in named if not n['extraurban']]
        numeric_routes = [n for n in named if n['extraurban'] and n.get('route_numeric')]
        if non_route:
            # Never stitch an SS/SP and another street/locality into one paid query.
            if any(n['kind'] == 'STRADA' for n in non_route):
                return fail('INDICAZIONI_STRADALI_MISTE_DA_VERIFICARE')
            return fail('STRADA_EXTRAURBANA_E_LOCALITA_DA_DISAMBIGUARE')
        if extraurban_bad_reason:
            return fail(extraurban_bad_reason)
        if len(numeric_routes) != 1:
            return fail('STRADA_EXTRAURBANA_NON_VERIFICATA')
        selected = numeric_routes[0]
        return {'kind': selected['kind'], 'name': selected['name'], 'civic': selected['civic'],
                'reason': 'TOPONIMO_EXTRAURBANO_NUMERICO_IDENTIFICABILE'}

    if not named:
        return fail('TOPONIMO_ASSENTE_O_SEGNAPOSTO')
    by_kind = {
        'STRADA': {n['normalized'] for n in named if n['kind'] == 'STRADA'},
        'LOCALITA': {n['normalized'] for n in named if n['kind'] == 'LOCALITA'},
    }
    if any(len(names) > 1 for names in by_kind.values()):
        return fail('PIU_TOPONIMI_NON_DISAMBIGUATI')
    selected = next((n for n in named if n['kind'] == 'STRADA'), named[0])
    return {'kind': selected['kind'], 'name': selected['name'], 'civic': selected['civic'],
            'reason': 'TOPONIMO_IDENTIFICABILE'}


def useful_toponym(row):
    data = address_parts(row)
    return (data['kind'] or None, data['name'], data['reason'])


def eligibility(row, offline_decision):
    """Only unresolved questions about a coherent, specific address may go to Google."""
    src = str(row.get('pvp_geo_source') or '').lower()
    base = str(offline_decision.get('geocode_source') or '')
    prec = str(offline_decision.get('geocode_precision') or '')
    status = str(offline_decision.get('geocode_status') or '')
    result = {'google_eligible': False, 'google_reason': '', 'google_group': '',
              'address_kind': '', 'address_name': '', 'address_civic': ''}

    if 'multi' in src:
        result['google_reason'] = 'PIU_BENI_NON_DISAMBIGUATI'
        return result
    if (base.startswith('anncsu_') or base in {
            'pvp_anncsu_civico_compatibile', 'pvp_anncsu_strada_compatibile',
            'pvp_anncsu_localita_compatibile'}):
        result['google_reason'] = 'ANNCSU_GIA_RISOLUTIVO'
        return result
    if base == 'pvp_comune_alternativo' or base in {
            'review_admin', 'review_multi_bene_pvp', 'review_multi_bene_civico',
            'review_istat_boundary', 'review_pvp_invalid'}:
        result['google_reason'] = 'AMBIGUITA_GEOGRAFICA_DA_RISOLVERE_PRIMA'
        return result

    data = address_parts(row)
    result['address_kind'], result['address_name'], result['address_civic'] = (
        data['kind'], data['name'], data['civic'])
    if not data['kind']:
        result['google_reason'] = data['reason']
        return result

    # A truncated locality/frazione/contrada without a civic never needs paid
    # Google merely to stay visible on the map. Prefer ANNCSU locality geometry
    # when available; otherwise keep the safe PVP/ISTAT municipality fallback.
    if data['kind'] == 'LOCALITA' and not data['civic']:
        result['google_reason'] = 'LOCALITA_TRONCATA_USA_ANNCSU_O_COMUNE'
        return result

    if (prec not in {'COMUNE', 'PVP_IN_COMUNE', 'NESSUNA'}
            and base not in {'pvp_civico_non_conclusivo', 'pvp_strada_non_conclusiva',
                             'pvp_localita_non_conclusiva', 'review_pvp_civico_discordante',
                             'review_pvp_strada_discordante', 'review_pvp_localita_discordante'}):
        result['google_reason'] = 'NESSUN_DUBBIO_RESIDUO'
        return result
    if status not in {'DONE', 'REVIEW'}:
        result['google_reason'] = 'STATO_NON_PREVISTO'
        return result

    pvp_present = row.get('pvp_latitudine') is not None and row.get('pvp_longitudine') is not None
    result['google_eligible'] = True
    result['google_reason'] = ('PVP_IN_DISACCORDO_CON_ANNCSU' if base.startswith('review_pvp_')
                               else 'PVP_SENZA_CONFERMA_INDIRIZZO' if pvp_present
                               else 'ANNCSU_NON_RISOLVE_INDIRIZZO')
    result['google_group'] = ('REVIEW_DISCORDANZA' if base.startswith('review_pvp_')
                              else 'PVP_NON_CONFERMATO' if pvp_present
                              else 'SENZA_COORDINATE_PVP')
    return result
