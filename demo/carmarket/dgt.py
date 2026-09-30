"""DGT daily registrations: discover -> download -> parse fixed-width -> clean -> load (idempotent).

Layout verified against a real file (export_mat_20260925.txt): 714 chars/record, 69 fields, ISO-8859-1.
"""
import hashlib
import re
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from .config import RAW_DIR
from .db import Dims

LISTING_URL = ("https://www.dgt.es/menusecundario/dgt-en-cifras/matraba-listados/"
               "matriculaciones-automoviles-diario.html")
ZIP_RE = re.compile(r'href="(https://www\.dgt\.es/microdatos/salida/\d{4}/\d{1,2}/vehiculos/'
                    r'matriculaciones/export_mat_(\d{8})\.zip)"')
# NOTE: dgt.es' edge filter times out on some custom User-Agent strings; the stdlib default works.
# Do not disguise the client as a browser: keep the default and retry politely.
def _get(url, timeout, tries=3):
    last = None
    for i in range(tries):
        try:
            return urllib.request.urlopen(url, timeout=timeout).read()
        except OSError as e:                      # timeouts, resets, HTTP errors
            last = e
            time.sleep(2 * (i + 1))
    raise last


RECORD_LEN = 714
LAYOUT = [
    ("FEC_MATRICULA", 8), ("COD_CLASE_MAT", 1), ("FEC_TRAMITACION", 8), ("MARCA_ITV", 30), ("MODELO_ITV", 22),
    ("COD_PROCEDENCIA_ITV", 1), ("BASTIDOR_ITV", 21), ("COD_TIPO", 2), ("COD_PROPULSION_ITV", 1),
    ("CILINDRADA_ITV", 5), ("POTENCIA_ITV", 6), ("TARA", 6), ("PESO_MAX", 6), ("NUM_PLAZAS", 3),
    ("IND_PRECINTO", 2), ("IND_EMBARGO", 2), ("NUM_TRANSMISIONES", 2), ("NUM_TITULARES", 2),
    ("LOCALIDAD_VEHICULO", 24), ("COD_PROVINCIA_VEH", 2), ("COD_PROVINCIA_MAT", 2), ("CLAVE_TRAMITE", 1),
    ("FEC_TRAMITE", 8), ("CODIGO_POSTAL", 5), ("FEC_PRIM_MATRICULACION", 8), ("IND_NUEVO_USADO", 1),
    ("PERSONA_FISICA_JURIDICA", 1), ("CODIGO_ITV", 9), ("SERVICIO", 3), ("COD_MUNICIPIO_INE_VEH", 5),
    ("MUNICIPIO", 30), ("KW_ITV", 7), ("NUM_PLAZAS_MAX", 3), ("CO2_ITV", 5), ("RENTING", 1),
    ("COD_TUTELA", 1), ("COD_POSESION", 1), ("IND_BAJA_DEF", 1), ("IND_BAJA_TEMP", 1),
    ("IND_SUSTRACCION", 1), ("BAJA_TELEMATICA", 11), ("TIPO_ITV", 25), ("VARIANTE_ITV", 25),
    ("VERSION_ITV", 35), ("FABRICANTE_ITV", 70), ("MASA_ORDEN_MARCHA_ITV", 6),
    ("MASA_MAXIMA_TECNICA_ADMISIBLE_ITV", 6), ("CATEGORIA_HOMOLOGACION_EUROPEA_ITV", 4), ("CARROCERIA", 4),
    ("PLAZAS_PIE", 3), ("NIVEL_EMISIONES_EURO_ITV", 8), ("CONSUMO_WH/KM_ITV", 4),
    ("CLASIFICACION_REGLAMENTO_VEHICULOS_ITV", 4), ("CATEGORIA_VEHICULO_ELECTRICO", 4),
    ("AUTONOMIA_VEHICULO_ELECTRICO", 6), ("MARCA_VEHICULO_BASE", 30), ("FABRICANTE_VEHICULO_BASE", 50),
    ("TIPO_VEHICULO_BASE", 35), ("VARIANTE_VEHICULO_BASE", 25), ("VERSION_VEHICULO_BASE", 35),
    ("DISTANCIA_EJES_12_ITV", 4), ("VIA_ANTERIOR_ITV", 4), ("VIA_POSTERIOR_ITV", 4),
    ("TIPO_ALIMENTACION_ITV", 1), ("CONTRASENA_HOMOLOGACION_ITV", 25), ("ECO_INNOVACION_ITV", 1),
    ("REDUCCION_ECO_ITV", 4), ("CODIGO_ECO_ITV", 25), ("FEC_PROCESO", 8),
]
assert sum(n for _, n in LAYOUT) == RECORD_LEN and len(LAYOUT) == 69

# Spelling variants seen across sources -> canonical brand (extend as unmapped brands show up in dq_result)
BRAND_ALIAS = {"VW": "VOLKSWAGEN", "MERCEDES": "MERCEDES-BENZ", "MERCEDES BENZ": "MERCEDES-BENZ",
               "CITROËN": "CITROEN", "SKODA": "SKODA", "ŠKODA": "SKODA", "LAND ROVER": "LAND-ROVER"}

# DGT province codes are licence-plate letters (verified on real data), not INE numbers.
PROVINCES = {
    "A": "Alicante", "AB": "Albacete", "AL": "Almería", "AV": "Ávila", "B": "Barcelona", "BA": "Badajoz",
    "BI": "Bizkaia", "BU": "Burgos", "C": "A Coruña", "CA": "Cádiz", "CC": "Cáceres", "CE": "Ceuta",
    "CO": "Córdoba", "CR": "Ciudad Real", "CS": "Castellón", "CU": "Cuenca", "GC": "Las Palmas", "GI": "Girona",
    "GR": "Granada", "GU": "Guadalajara", "H": "Huelva", "HU": "Huesca", "IB": "Illes Balears", "J": "Jaén",
    "L": "Lleida", "LE": "León", "LO": "La Rioja", "LU": "Lugo", "M": "Madrid", "MA": "Málaga", "ML": "Melilla",
    "MU": "Murcia", "NA": "Navarra", "O": "Asturias", "OR": "Ourense", "OU": "Ourense", "P": "Palencia", "PM": "Illes Balears",
    "PO": "Pontevedra", "S": "Cantabria", "SA": "Salamanca", "SE": "Sevilla", "SG": "Segovia", "SO": "Soria",
    "SS": "Gipuzkoa", "T": "Tarragona", "TE": "Teruel", "TF": "Santa Cruz de Tenerife", "TO": "Toledo",
    "V": "Valencia", "VA": "Valladolid", "VI": "Álava", "Z": "Zaragoza", "ZA": "Zamora",
}


# ── extract ─────────────────────────────────────────────────────────────
def discover(listing_url=LISTING_URL):
    """Return {YYYYMMDD: zip_url} for every daily file on the listing page."""
    html = _get(listing_url, timeout=30).decode("utf-8", "replace")
    return {m.group(2): m.group(1) for m in ZIP_RE.finditer(html)}


def download(day, url, cache_dir=RAW_DIR) -> Path:
    """Download + unzip one day. Cached on disk: a file already present is never fetched twice."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    txt = cache_dir / f"export_mat_{day}.txt"
    if txt.exists():
        return txt
    zpath = cache_dir / f"export_mat_{day}.zip"
    zpath.write_bytes(_get(url, timeout=120))
    with zipfile.ZipFile(zpath) as z:
        name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
        txt.write_bytes(z.read(name))
    zpath.unlink()
    return txt


# ── parse ───────────────────────────────────────────────────────────────
def iter_records(txt_path):
    """Yield (line_no, line). Line 1 is a banner, not data. Lines of the wrong length are yielded as None."""
    with open(txt_path, encoding="latin-1", newline="") as f:
        for i, line in enumerate(f, 1):
            line = line.rstrip("\n").rstrip("\r")
            if i == 1 or not line:
                continue
            yield i, (line if len(line) == RECORD_LEN else None)


def split_fields(line):
    out, p = {}, 0
    for name, n in LAYOUT:
        out[name] = line[p:p + n].strip()
        p += n
    return out


# ── clean ───────────────────────────────────────────────────────────────
def _txt(v):
    return None if v in ("", "ND") else v          # 'ND' = "no disponible" placeholder used by DGT


def _date(v):
    try:
        return datetime.strptime(v, "%d%m%Y").date().isoformat()   # DDMMYYYY
    except (ValueError, TypeError):
        return None


def _num(v, zero_is_null=True):
    try:
        x = float(v)
    except (ValueError, TypeError):
        return None
    return None if (zero_is_null and x == 0) else x


def segment_of(eu_category):
    """Vehicle segment from the EU homologation category (M1 = passenger car, N1 = light goods, L = powered 2-3 wheelers...)."""
    c = (eu_category or "").upper()
    if c.startswith("M1"):
        return "car"
    if c.startswith("N1"):
        return "van"
    if c.startswith("L"):
        return "moto"
    if c.startswith(("M2", "M3")):
        return "bus"
    if c.startswith(("N2", "N3")):
        return "truck"
    if c.startswith("O"):
        return "trailer"
    return "other"


def canonical_brand(raw):
    b = (raw or "").upper().strip()
    return BRAND_ALIAS.get(b, b) or None


def canonical_model(brand, raw):
    """'TOYOTA COROLLA' -> 'COROLLA' (DGT often repeats the brand inside the model); collapse inner spaces."""
    m = re.sub(r"\s+", " ", (raw or "").upper()).strip()
    if brand and m.startswith(brand + " "):
        m = m[len(brand) + 1:]
    return m or None


def clean(f):
    """Raw text fields -> typed, trimmed record. Returns None if the mandatory date is invalid."""
    reg = _date(f["FEC_MATRICULA"])
    if reg is None:
        return None
    brand = canonical_brand(_txt(f["MARCA_ITV"]))
    cc, kw, co2 = _num(f["CILINDRADA_ITV"]), _num(f["KW_ITV"]), _num(f["CO2_ITV"])
    return {
        "registration_date": reg,
        "first_registration_date": _date(f["FEC_PRIM_MATRICULACION"]),
        "is_new": {"N": 1, "U": 0}.get(f["IND_NUEVO_USADO"]),
        "brand": brand,
        "model": canonical_model(brand, _txt(f["MODELO_ITV"])),
        "version_raw": _txt(f["VERSION_ITV"]),
        "eu_category": _txt(f["CATEGORIA_HOMOLOGACION_EUROPEA_ITV"]),
        "segment": segment_of(f["CATEGORIA_HOMOLOGACION_EUROPEA_ITV"]),
        "body_code": _txt(f["CARROCERIA"]),
        "propulsion_code": _txt(f["COD_PROPULSION_ITV"]),
        "engine_cc": int(cc) if cc else None,
        "power_kw": kw,
        "co2_gkm": int(co2) if co2 else None,
        "euro_norm": _txt(f["NIVEL_EMISIONES_EURO_ITV"]),
        "ev_range_km": int(_num(f["AUTONOMIA_VEHICULO_ELECTRICO"]) or 0) or None,
        "seats": int(_num(f["NUM_PLAZAS"]) or 0) or None,
        "province_veh": _txt(f["COD_PROVINCIA_VEH"]),
        "province_mat": _txt(f["COD_PROVINCIA_MAT"]),
        "municipality_ine": _txt(f["COD_MUNICIPIO_INE_VEH"]),
        "municipality": _txt(f["MUNICIPIO"]),
        "postal_code": _txt(f["CODIGO_POSTAL"]),
        "buyer_code": _txt(f["PERSONA_FISICA_JURIDICA"]),
        "service_code": _txt(f["SERVICIO"]),
        "is_renting": 1 if f["RENTING"] == "S" else 0,
        # VIN is truncated by DGT since 2025-02-01 ("*" padding): keep the visible prefix only, never as a key.
        "vin_prefix": f["BASTIDOR_ITV"].replace("*", "") or None,
    }


# ── load ────────────────────────────────────────────────────────────────
def load_file(con, txt_path, dims: Dims):
    """Idempotent load of one day. Re-running the same file inserts 0 rows (row_hash is the key)."""
    src = Path(txt_path).name
    read = inserted = rejected = 0
    for line_no, line in iter_records(txt_path):
        read += 1
        if line is None:
            rejected += 1
            continue
        h = hashlib.sha256(line.encode("latin-1")).hexdigest()
        cur = con.execute("INSERT OR IGNORE INTO raw_dgt VALUES (?,?,?,?)", (h, src, line_no, line))
        if cur.rowcount == 0:
            continue                                  # already loaded
        rec = clean(split_fields(line))
        if rec is None:
            rejected += 1
            con.execute("DELETE FROM raw_dgt WHERE row_hash=?", (h,))
            continue
        b = dims.brand(rec["brand"])
        m = dims.model(b, rec["model"])
        con.execute(
            """INSERT INTO fact_registration
               (row_hash, registration_date, first_registration_date, is_new, brand_id, model_id, version_raw,
                segment, eu_category, body_code, propulsion_code, engine_cc, power_kw, co2_gkm, euro_norm,
                ev_range_km, seats, province_veh, province_mat, municipality_ine, municipality, postal_code,
                buyer_code, service_code, is_renting, vin_prefix)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (h, rec["registration_date"], rec["first_registration_date"], rec["is_new"], b, m, rec["version_raw"],
             rec["segment"], rec["eu_category"], rec["body_code"], rec["propulsion_code"], rec["engine_cc"],
             rec["power_kw"], rec["co2_gkm"], rec["euro_norm"], rec["ev_range_km"], rec["seats"],
             rec["province_veh"], rec["province_mat"], rec["municipality_ine"], rec["municipality"],
             rec["postal_code"], rec["buyer_code"], rec["service_code"], rec["is_renting"], rec["vin_prefix"]))
        inserted += 1
    con.execute("INSERT OR REPLACE INTO ingest_log(source,file_name,rows_read,rows_inserted,rows_rejected) "
                "VALUES ('dgt_matr_daily',?,?,?,?)", (src, read, inserted, rejected))
    con.commit()
    return read, inserted, rejected


def load_provinces(con):
    con.executemany("INSERT OR REPLACE INTO dim_province VALUES (?,?)", PROVINCES.items())


def data_quality(con):
    """Checks that fail loudly if DGT changes the layout or a mapping goes stale."""
    q = lambda sql: con.execute(sql).fetchone()[0]
    total = q("SELECT COUNT(*) FROM fact_registration") or 1
    checks = {
        "rows_total": total,
        "pct_bad_length_rejected": 100.0 * q("SELECT COALESCE(SUM(rows_rejected),0) FROM ingest_log") / max(
            q("SELECT COALESCE(SUM(rows_read),0) FROM ingest_log"), 1),
        "pct_null_brand": 100.0 * q("SELECT COUNT(*) FROM fact_registration WHERE brand_id IS NULL") / total,
        "pct_segment_other": 100.0 * q("SELECT COUNT(*) FROM fact_registration WHERE segment='other'") / total,
        "pct_unknown_province": 100.0 * q(
            "SELECT COUNT(*) FROM fact_registration f LEFT JOIN dim_province p ON p.province_code=f.province_veh "
            "WHERE p.province_code IS NULL") / total,
        "pct_used_no_first_reg_date": 100.0 * q(
            "SELECT COUNT(*) FROM fact_registration WHERE is_new=0 AND first_registration_date IS NULL") / total,
    }
    con.executemany("INSERT INTO dq_result(check_name,value) VALUES (?,?)", checks.items())
    con.commit()
    return checks
