#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Aste Easy - ISTAT municipality-boundary runtime v11.6

Permanent geocoder guard:
- official ISTAT 2026 municipality polygons;
- local point-in-polygon;
- ZERO Google calls;
- ZERO Supabase writes;
- standard library only.

The boundary ZIP is reused from tools/Limiti01012026.zip. If missing, it is
downloaded once from the official ISTAT URL. In production geocoding this
module is fail-closed for unresolved municipality/polygon states: it never
claims a coordinate is territorially valid unless the canonical municipality
polygon is resolved and the point is INSIDE or on BOUNDARY.
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys
import re
import struct
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
SRC_ROOT = BASE_DIR.parent
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
from aste_paths import REFERENCE_DIR, env_path

ADMIN_FILE = BASE_DIR / "geo_admin_canonical_v11_3.py"
ISTAT_ZIP = env_path("ISTAT_BOUNDARY_ZIP", REFERENCE_DIR / "Limiti01012026.zip", relative_to=REFERENCE_DIR)
ISTAT_ZIP_URL = "https://www.istat.it/storage/cartografia/confini_amministrativi/non_generalizzati/2026/Limiti01012026.zip"
EXTRACT_DIR = env_path("ISTAT_BOUNDARY_EXTRACT_DIR", REFERENCE_DIR / "istat_confini_2026", relative_to=REFERENCE_DIR)


def clean(v):
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v).replace("\u00a0", " ")).strip()


def fnum(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def valid_pair(lat, lon):
    a, b = fnum(lat), fnum(lon)
    return (
        a is not None and b is not None
        and -90 <= a <= 90 and -180 <= b <= 180
        and not (a == 0 and b == 0)
    )


def _load(path: Path, name: str):
    if not path.exists():
        raise SystemExit(f"ERRORE: manca {path.name}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"ERRORE: impossibile caricare {path.name}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- WGS84 lon/lat -> UTM Zone 32N (EPSG:32632), dependency-free ----------

def wgs84_to_utm32(lat_deg, lon_deg):
    lat = math.radians(float(lat_deg))
    lon = math.radians(float(lon_deg))
    lon0 = math.radians(9.0)  # UTM zone 32 central meridian

    a = 6378137.0
    f = 1.0 / 298.257223563
    k0 = 0.9996
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)

    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    tan_lat = math.tan(lat)

    N = a / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    T = tan_lat * tan_lat
    C = ep2 * cos_lat * cos_lat
    A = cos_lat * (lon - lon0)

    e4 = e2 * e2
    e6 = e4 * e2
    M = a * (
        (1 - e2/4 - 3*e4/64 - 5*e6/256) * lat
        - (3*e2/8 + 3*e4/32 + 45*e6/1024) * math.sin(2*lat)
        + (15*e4/256 + 45*e6/1024) * math.sin(4*lat)
        - (35*e6/3072) * math.sin(6*lat)
    )

    x = 500000.0 + k0 * N * (
        A
        + (1 - T + C) * A**3 / 6
        + (5 - 18*T + T*T + 72*C - 58*ep2) * A**5 / 120
    )
    y = k0 * (
        M + N * tan_lat * (
            A*A/2
            + (5 - T + 9*C + 4*C*C) * A**4 / 24
            + (61 - 58*T + T*T + 600*C - 330*ep2) * A**6 / 720
        )
    )
    return x, y




def utm32_to_wgs84(x, y):
    """EPSG:32632 -> WGS84 lon/lat, dependency-free.

    Inverse companion of wgs84_to_utm32(), sufficient for Italian municipality
    polygons. Returns (lat, lon) in decimal degrees.
    """
    x = float(x) - 500000.0
    y = float(y)

    a = 6378137.0
    f = 1.0 / 298.257223563
    k0 = 0.9996
    e2 = f * (2.0 - f)
    ep2 = e2 / (1.0 - e2)
    e1 = (1.0 - math.sqrt(1.0 - e2)) / (1.0 + math.sqrt(1.0 - e2))

    M = y / k0
    mu = M / (a * (1.0 - e2/4.0 - 3.0*e2*e2/64.0 - 5.0*e2**3/256.0))

    phi1 = (
        mu
        + (3.0*e1/2.0 - 27.0*e1**3/32.0) * math.sin(2.0*mu)
        + (21.0*e1*e1/16.0 - 55.0*e1**4/32.0) * math.sin(4.0*mu)
        + (151.0*e1**3/96.0) * math.sin(6.0*mu)
        + (1097.0*e1**4/512.0) * math.sin(8.0*mu)
    )

    sin1 = math.sin(phi1)
    cos1 = math.cos(phi1)
    tan1 = math.tan(phi1)
    N1 = a / math.sqrt(1.0 - e2*sin1*sin1)
    R1 = a * (1.0 - e2) / (1.0 - e2*sin1*sin1)**1.5
    T1 = tan1*tan1
    C1 = ep2*cos1*cos1
    D = x / (N1*k0)

    lat = phi1 - (N1*tan1/R1) * (
        D*D/2.0
        - (5.0 + 3.0*T1 + 10.0*C1 - 4.0*C1*C1 - 9.0*ep2) * D**4 / 24.0
        + (61.0 + 90.0*T1 + 298.0*C1 + 45.0*T1*T1 - 252.0*ep2 - 3.0*C1*C1) * D**6 / 720.0
    )
    lon = math.radians(9.0) + (
        D
        - (1.0 + 2.0*T1 + C1) * D**3 / 6.0
        + (5.0 - 2.0*C1 + 28.0*T1 - 3.0*C1*C1 + 8.0*ep2 + 24.0*T1*T1) * D**5 / 120.0
    ) / cos1

    return math.degrees(lat), math.degrees(lon)

# ---------- Minimal DBF + SHP polygon reader ----------

def decode_text(b):
    for enc in ("utf-8", "cp1252", "latin1"):
        try:
            return b.decode(enc).strip().strip("\x00")
        except UnicodeDecodeError:
            pass
    return b.decode("latin1", errors="replace").strip().strip("\x00")


def read_dbf(path):
    raw = path.read_bytes()
    if len(raw) < 32:
        raise SystemExit(f"ERRORE: DBF troppo corto: {path}")
    nrec = struct.unpack_from("<I", raw, 4)[0]
    hlen = struct.unpack_from("<H", raw, 8)[0]
    rlen = struct.unpack_from("<H", raw, 10)[0]

    fields = []
    pos = 32
    while pos + 32 <= hlen and raw[pos] != 0x0D:
        fd = raw[pos:pos+32]
        name = decode_text(fd[:11]).split("\x00", 1)[0]
        ftype = chr(fd[11])
        flen = fd[16]
        dec = fd[17]
        fields.append((name, ftype, flen, dec))
        pos += 32

    records = []
    off = hlen
    for _ in range(nrec):
        rec = raw[off:off+rlen]
        off += rlen
        if len(rec) < rlen:
            break
        deleted = rec[:1] == b"*"
        cursor = 1
        d = {"_deleted": deleted}
        for name, ftype, flen, dec in fields:
            cell = rec[cursor:cursor+flen]
            cursor += flen
            d[name] = decode_text(cell)
        records.append(d)
    return fields, records


def read_shp_polygons(path):
    raw = path.read_bytes()
    if len(raw) < 100:
        raise SystemExit(f"ERRORE: SHP troppo corto: {path}")
    shape_type = struct.unpack_from("<i", raw, 32)[0]
    if shape_type not in {5, 15, 25}:
        raise SystemExit(f"ERRORE: shape type comuni inatteso: {shape_type}")

    out = []
    pos = 100
    while pos + 8 <= len(raw):
        _rec_no, content_words = struct.unpack_from(">2i", raw, pos)
        pos += 8
        content_len = content_words * 2
        content = raw[pos:pos+content_len]
        pos += content_len
        if len(content) < 44:
            out.append(None)
            continue

        st = struct.unpack_from("<i", content, 0)[0]
        if st == 0:
            out.append(None)
            continue
        if st not in {5, 15, 25}:
            out.append(None)
            continue

        xmin, ymin, xmax, ymax = struct.unpack_from("<4d", content, 4)
        num_parts, num_points = struct.unpack_from("<2i", content, 36)
        parts_off = 44
        points_off = parts_off + 4 * num_parts
        if points_off + 16 * num_points > len(content):
            out.append(None)
            continue

        parts = list(struct.unpack_from("<" + "i"*num_parts, content, parts_off))
        pts = [
            struct.unpack_from("<2d", content, points_off + 16*i)
            for i in range(num_points)
        ]
        rings = []
        for i, start in enumerate(parts):
            end = parts[i+1] if i+1 < len(parts) else num_points
            ring = pts[start:end]
            if len(ring) >= 3:
                rings.append(ring)

        out.append({
            "bbox": (xmin, ymin, xmax, ymax),
            "rings": rings,
        })
    return out


def normalize_code(v):
    d = re.sub(r"\D", "", clean(v))
    return d.zfill(6)[-6:] if d else ""


def identify_code_field(field_names):
    upper = {x.upper(): x for x in field_names}
    for candidate in ("PRO_COM_T", "PRO_COM", "COD_COM", "COD_COMUNE"):
        if candidate in upper:
            return upper[candidate]
    for name in field_names:
        n = name.upper()
        if "PRO_COM" in n or ("COD" in n and "COM" in n):
            return name
    raise SystemExit(
        "ERRORE: nel DBF non trovo un campo codice Comune (attesi PRO_COM_T/PRO_COM). "
        f"Campi: {', '.join(field_names)}"
    )


def ensure_boundary_files():
    if not ISTAT_ZIP.exists():
        print(f"[ISTAT] {ISTAT_ZIP.name} assente: download una tantum dalla fonte ufficiale ISTAT...")
        try:
            req = Request(
                ISTAT_ZIP_URL,
                method="GET",
                headers={"User-Agent": "AsteEasy/11.5 ISTAT-boundary-audit"},
            )
            with urlopen(req, timeout=120) as resp:
                data = resp.read()
            if len(data) < 1000:
                raise RuntimeError(f"download troppo piccolo: {len(data)} byte")
            tmp = ISTAT_ZIP.with_suffix(".zip.tmp")
            tmp.write_bytes(data)
            if not zipfile.is_zipfile(tmp):
                tmp.unlink(missing_ok=True)
                raise RuntimeError("il file scaricato non e' uno ZIP valido")
            tmp.replace(ISTAT_ZIP)
            print(f"[ISTAT] download completato: {ISTAT_ZIP.name} ({len(data):,} byte)")
        except Exception as exc:
            raise SystemExit(
                f"ERRORE: download confini ISTAT 2026 fallito: {exc}. "
                "Nessun dato DB e' stato modificato."
            )
    EXTRACT_DIR.mkdir(exist_ok=True)
    marker = EXTRACT_DIR / ".extracted_ok"
    if not marker.exists():
        with zipfile.ZipFile(ISTAT_ZIP, "r") as z:
            z.extractall(EXTRACT_DIR)
        marker.write_text("ok", encoding="utf-8")

    candidates = [
        p for p in EXTRACT_DIR.rglob("*.shp")
        if p.name.lower().startswith("com")
    ]
    if not candidates:
        # fallback: any shp whose paired dbf contains a PRO_COM-like field
        candidates = list(EXTRACT_DIR.rglob("*.shp"))

    for shp in candidates:
        dbf = shp.with_suffix(".dbf")
        if not dbf.exists():
            continue
        try:
            fields, _ = read_dbf(dbf)
            identify_code_field([x[0] for x in fields])
            return shp, dbf
        except Exception:
            continue

    raise SystemExit(
        f"ERRORE: shapefile Comuni non trovato dentro {ISTAT_ZIP.name}"
    )


def load_municipality_polygons(shp, dbf):
    fields, attrs = read_dbf(dbf)
    shapes = read_shp_polygons(shp)
    if len(attrs) != len(shapes):
        raise SystemExit(
            f"ERRORE QA: record DBF={len(attrs)} != shape={len(shapes)}"
        )
    code_field = identify_code_field([x[0] for x in fields])
    out = {}
    duplicate_codes = set()
    for attr, shape in zip(attrs, shapes):
        if attr.get("_deleted") or not shape:
            continue
        code = normalize_code(attr.get(code_field))
        if not code:
            continue
        if code in out:
            # Rare multipart split across records: merge rings and bbox.
            duplicate_codes.add(code)
            old = out[code]
            x1, y1, x2, y2 = old["bbox"]
            a1, b1, a2, b2 = shape["bbox"]
            old["bbox"] = (min(x1,a1), min(y1,b1), max(x2,a2), max(y2,b2))
            old["rings"].extend(shape["rings"])
        else:
            out[code] = shape
    return out, code_field, duplicate_codes


def point_on_segment(px, py, ax, ay, bx, by, eps=0.05):
    dx, dy = bx-ax, by-ay
    seg2 = dx*dx + dy*dy
    if seg2 == 0:
        return (px-ax)**2 + (py-ay)**2 <= eps*eps
    t = ((px-ax)*dx + (py-ay)*dy) / seg2
    if t < 0 or t > 1:
        return False
    qx, qy = ax + t*dx, ay + t*dy
    return (px-qx)**2 + (py-qy)**2 <= eps*eps


def point_in_ring(x, y, ring):
    inside = False
    n = len(ring)
    if n < 3:
        return False, False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if point_on_segment(x, y, xi, yi, xj, yj):
            return True, True
        crosses = ((yi > y) != (yj > y))
        if crosses:
            xcross = (xj-xi) * (y-yi) / (yj-yi) + xi
            if x < xcross:
                inside = not inside
        j = i
    return inside, False


def point_in_shape(x, y, shape):
    xmin, ymin, xmax, ymax = shape["bbox"]
    if x < xmin or x > xmax or y < ymin or y > ymax:
        return False, False

    # ESRI polygon rings follow an even/odd fill rule; toggling across all rings
    # correctly handles multipart polygons and holes.
    inside = False
    for ring in shape["rings"]:
        rin, boundary = point_in_ring(x, y, ring)
        if boundary:
            return True, True
        if rin:
            inside = not inside
    return inside, False



def _ring_centroid(ring):
    """Area centroid of one ring; may be outside when the ring is concave."""
    if not ring or len(ring) < 3:
        return None
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        cross = x1*y2 - x2*y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-9:
        return None
    return cx/(3.0*area2), cy/(3.0*area2)


def representative_xy(shape):
    """Return a point strictly inside an ESRI polygon shape when possible.

    The routine is deliberately conservative and dependency-free. It tries the
    bbox centre and ring centroids, then searches scan-line interior intervals;
    a dense grid is only the last fallback. Every candidate is verified with
    the same point_in_shape() used by the production boundary guard.
    """
    if not shape:
        return None
    xmin, ymin, xmax, ymax = shape["bbox"]
    if not (xmax > xmin and ymax > ymin):
        return None

    def accept(x, y):
        inside, boundary = point_in_shape(x, y, shape)
        return (x, y) if inside and not boundary else None

    c = accept((xmin+xmax)/2.0, (ymin+ymax)/2.0)
    if c:
        return c

    # Ring centroids are cheap and work for the great majority of polygons.
    for ring in sorted(shape.get("rings") or [], key=len, reverse=True):
        cen = _ring_centroid(ring)
        if cen:
            c = accept(cen[0], cen[1])
            if c:
                return c

    # Scan lines: choose the widest verified interior interval. This naturally
    # copes with concavity, holes and multipart municipalities.
    best = None
    height = ymax - ymin
    for steps in (17, 41, 101):
        for j in range(1, steps):
            y = ymin + height * (j / steps)
            xs = []
            for ring in shape.get("rings") or []:
                n = len(ring)
                for i in range(n):
                    x1, y1 = ring[i]
                    x2, y2 = ring[(i + 1) % n]
                    if y1 == y2:
                        continue
                    # Half-open rule avoids double-counting shared vertices.
                    if (y1 <= y < y2) or (y2 <= y < y1):
                        xs.append(x1 + (y-y1) * (x2-x1) / (y2-y1))
            xs.sort()
            for a, b in zip(xs, xs[1:]):
                if b <= a:
                    continue
                x = (a+b)/2.0
                c = accept(x, y)
                if c:
                    width = b-a
                    if best is None or width > best[0]:
                        best = (width, c)
        if best:
            return best[1]

    # Very defensive final fallback for extremely unusual geometry.
    for steps in (41, 101):
        for iy in range(1, steps):
            y = ymin + (ymax-ymin) * (iy/steps)
            for ix in range(1, steps):
                x = xmin + (xmax-xmin) * (ix/steps)
                c = accept(x, y)
                if c:
                    return c
    return None


def polygon_test(lat, lon, code, polygons):
    if not valid_pair(lat, lon):
        return "NO_COORDS", ""
    shape = polygons.get(code)
    if not shape:
        return "NO_POLYGON", ""
    x, y = wgs84_to_utm32(float(lat), float(lon))
    inside, boundary = point_in_shape(x, y, shape)
    if boundary:
        return "BOUNDARY", f"{x:.3f},{y:.3f}"
    return ("INSIDE" if inside else "OUTSIDE"), f"{x:.3f},{y:.3f}"



class BoundaryContext:
    def __init__(self, admin_ctx=None):
        self.admin_mod = _load(ADMIN_FILE, "asteeasy_admin_v113_boundary_runtime")
        self.admin_ctx = admin_ctx or self.admin_mod.AdminContext()
        shp, dbf = ensure_boundary_files()
        self.shp = shp
        self.dbf = dbf
        self.polygons, self.code_field, self.duplicate_codes = load_municipality_polygons(shp, dbf)

    def resolve_admin(self, row):
        return self.admin_ctx.resolve(row)

    def test(self, row, lat=None, lon=None, admin_meta=None):
        meta = admin_meta or self.resolve_admin(row)
        status = clean(meta.get("status"))
        code = clean(meta.get("canonical_code"))
        if status != "RESOLVED" or not code:
            return {
                "position": "ADMIN_UNRESOLVED",
                "safe": False,
                "canonical_code": code,
                "admin_meta": meta,
                "utm32_xy": "",
            }

        if lat is None:
            lat = row.get("latitudine")
        if lon is None:
            lon = row.get("longitudine")

        pos, xy = polygon_test(lat, lon, code, self.polygons)
        return {
            "position": pos,
            "safe": pos in {"INSIDE", "BOUNDARY"},
            "canonical_code": code,
            "admin_meta": meta,
            "utm32_xy": xy,
        }

    def representative_point(self, row, admin_meta=None):
        """Return a verified WGS84 point inside the canonical municipality.

        Intended only as the last-resort map fallback when no more precise
        coordinate can be established. It never claims street/civic precision.
        """
        meta = admin_meta or self.resolve_admin(row)
        status = clean(meta.get("status"))
        code = clean(meta.get("canonical_code"))
        if status != "RESOLVED" or not code:
            return {
                "ok": False,
                "reason": "ADMIN_UNRESOLVED",
                "canonical_code": code,
                "admin_meta": meta,
            }
        shape = self.polygons.get(code)
        if not shape:
            return {
                "ok": False,
                "reason": "NO_POLYGON",
                "canonical_code": code,
                "admin_meta": meta,
            }
        xy = representative_xy(shape)
        if not xy:
            return {
                "ok": False,
                "reason": "NO_INTERIOR_POINT",
                "canonical_code": code,
                "admin_meta": meta,
            }
        lat, lon = utm32_to_wgs84(xy[0], xy[1])
        check = self.test(row, lat=lat, lon=lon, admin_meta=meta)
        if not check.get("safe"):
            return {
                "ok": False,
                "reason": "INTERIOR_POINT_RECHECK_FAILED:" + clean(check.get("position")),
                "canonical_code": code,
                "admin_meta": meta,
            }
        return {
            "ok": True,
            "lat": lat,
            "lon": lon,
            "position": clean(check.get("position")),
            "canonical_code": code,
            "admin_meta": meta,
            "utm32_xy": f"{xy[0]:.3f},{xy[1]:.3f}",
        }

    def safe(self, row, lat=None, lon=None, admin_meta=None):
        return bool(self.test(row, lat=lat, lon=lon, admin_meta=admin_meta).get("safe"))

    def metadata(self):
        return {
            "istat_zip": ISTAT_ZIP.name,
            "istat_shapefile": self.shp.name,
            "istat_dbf": self.dbf.name,
            "istat_code_field": self.code_field,
            "istat_polygons_loaded": len(self.polygons),
            "duplicate_shape_codes": len(self.duplicate_codes),
        }


_CTX = None


def get_context(admin_ctx=None):
    global _CTX
    if _CTX is None:
        _CTX = BoundaryContext(admin_ctx=admin_ctx)
    return _CTX
