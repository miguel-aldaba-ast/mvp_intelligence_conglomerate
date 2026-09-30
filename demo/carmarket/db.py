"""SQLite schema. Mirrors the Postgres design in docs/data-platform-plan.md (raw -> core -> marts)
so the SQL moves to Supabase with only type changes."""
import sqlite3

from .config import DB_PATH

SCHEMA = """
-- ── meta ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingest_log (
  source TEXT NOT NULL, file_name TEXT NOT NULL,
  rows_read INT, rows_inserted INT, rows_rejected INT,
  finished_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (source, file_name)
);
CREATE TABLE IF NOT EXISTS dq_result (
  run_at TEXT DEFAULT CURRENT_TIMESTAMP, check_name TEXT, value REAL, detail TEXT
);

-- ── RAW ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_dgt (
  row_hash TEXT PRIMARY KEY,          -- sha256 of the full 714-char line = idempotency key
  source_file TEXT, line_no INT, line TEXT
);
CREATE TABLE IF NOT EXISTS raw_apify (
  portal TEXT, external_id TEXT, scraped_on TEXT, payload_hash TEXT, payload TEXT,
  is_synthetic INT DEFAULT 0,
  PRIMARY KEY (portal, external_id, payload_hash)
);
CREATE TABLE IF NOT EXISTS raw_traffic (
  domain TEXT, endpoint TEXT, country TEXT, month TEXT,
  payload TEXT, fetched_at TEXT DEFAULT CURRENT_TIMESTAMP, source TEXT,   -- 'live' | 'mock'
  PRIMARY KEY (domain, endpoint, country, month)
);

-- ── CORE: dimensions ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dim_brand (brand_id INTEGER PRIMARY KEY, brand_name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS dim_model (
  model_id INTEGER PRIMARY KEY, brand_id INT NOT NULL REFERENCES dim_brand,
  model_name TEXT NOT NULL, UNIQUE (brand_id, model_name)
);
CREATE TABLE IF NOT EXISTS dim_province (province_code TEXT PRIMARY KEY, province_name TEXT);

-- ── CORE: registrations (Domain A) ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact_registration (
  row_hash TEXT PRIMARY KEY REFERENCES raw_dgt,
  registration_date TEXT NOT NULL, first_registration_date TEXT, is_new INT,
  brand_id INT REFERENCES dim_brand, model_id INT REFERENCES dim_model, version_raw TEXT,
  segment TEXT, eu_category TEXT, body_code TEXT, propulsion_code TEXT,
  engine_cc INT, power_kw REAL, co2_gkm INT, euro_norm TEXT, ev_range_km INT, seats INT,
  province_veh TEXT, province_mat TEXT, municipality_ine TEXT, municipality TEXT, postal_code TEXT,
  buyer_code TEXT, service_code TEXT, is_renting INT, vin_prefix TEXT
);
CREATE INDEX IF NOT EXISTS ix_reg_date ON fact_registration (registration_date);
CREATE INDEX IF NOT EXISTS ix_reg_bm ON fact_registration (brand_id, model_id);

-- ── CORE: listings & prices (Domain C) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS listing (
  listing_id INTEGER PRIMARY KEY, portal TEXT NOT NULL, external_id TEXT NOT NULL,
  group_key TEXT,                     -- cross-portal duplicate group
  brand_id INT, model_id INT, title TEXT, year INT, km INT,
  province_code TEXT, seller_type TEXT, dealer_name TEXT, dealer_domain TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, is_active INT, is_synthetic INT DEFAULT 0,
  UNIQUE (portal, external_id)
);
CREATE TABLE IF NOT EXISTS listing_price (
  listing_id INT REFERENCES listing, observed_on TEXT, price_eur REAL,
  PRIMARY KEY (listing_id, observed_on)
);

-- ── CORE: traffic (Domain B) ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fact_dealer_traffic (
  domain TEXT, month TEXT, visits REAL, paid_search_share REAL, display_share REAL,
  social_share REAL, source TEXT, PRIMARY KEY (domain, month)
);

-- ── MARTS (rebuilt on every run) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS mart_price_band (
  brand_name TEXT, model_name TEXT, n_listings INT, p10 REAL, median REAL, p90 REAL, is_synthetic INT
);
CREATE TABLE IF NOT EXISTS mart_dealer_ad_cost (
  dealer_domain TEXT, dealer_name TEXT, avg_monthly_visits REAL, paid_share REAL,
  est_paid_visits REAL, est_media_cost_eur REAL, units_sold_proxy INT,
  cost_per_unit_eur REAL, avg_sold_price_eur REAL, cost_pct_of_price REAL, is_synthetic INT
);
CREATE TABLE IF NOT EXISTS mart_model_ad_cost (
  brand_name TEXT, model_name TEXT, units_sold_proxy INT, avg_cost_per_unit_eur REAL,
  median_price_eur REAL, cost_pct_of_price REAL, dgt_registrations INT, is_synthetic INT
);
"""


def connect(path=DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


class Dims:
    """Get-or-create for brand/model dimensions (in-memory cache, one round trip per new value)."""

    def __init__(self, con):
        self.con = con
        self._brand = {r["brand_name"]: r["brand_id"] for r in con.execute("SELECT * FROM dim_brand")}
        self._model = {(r["brand_id"], r["model_name"]): r["model_id"] for r in con.execute("SELECT * FROM dim_model")}

    def brand(self, name):
        if name is None:
            return None
        if name not in self._brand:
            self._brand[name] = self.con.execute("INSERT INTO dim_brand(brand_name) VALUES (?)", (name,)).lastrowid
        return self._brand[name]

    def model(self, brand_id, name):
        if brand_id is None or name is None:
            return None
        key = (brand_id, name)
        if key not in self._model:
            self._model[key] = self.con.execute(
                "INSERT INTO dim_model(brand_id, model_name) VALUES (?, ?)", key).lastrowid
        return self._model[key]
