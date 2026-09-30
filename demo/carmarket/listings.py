"""Domain C: Apify scraper output -> raw JSON -> normalised listings + price history.

The two portal mappers below are written for the *shape we expect*; the real actors your boss connected may
name fields differently. Only `MAPPERS` needs adjusting: raw payloads are always stored untouched, so
re-normalising after a mapper fix costs nothing (no re-scrape).
"""
import hashlib
import json
import re
import statistics
import unicodedata
import urllib.request

from .dgt import BRAND_ALIAS, canonical_brand

CITY_TO_PROVINCE = {"MADRID": "M", "BARCELONA": "B", "VALENCIA": "V", "SEVILLA": "SE", "MALAGA": "MA",
                    "BILBAO": "BI", "BIZKAIA": "BI", "ZARAGOZA": "Z", "ALICANTE": "A", "MURCIA": "MU"}


def _ascii(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().upper().strip()


def _domain(url):
    if not url:
        return None
    d = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    return d[4:] if d.startswith("www.") else d


# ── portal mappers: raw item -> common dict ─────────────────────────────
def map_wallapop(it):
    ta, seller, loc = it.get("type_attributes") or {}, it.get("seller") or {}, it.get("location") or {}
    pro = seller.get("kind") == "professional"
    return {
        "external_id": str(it["id"]), "title": it.get("title"),
        "price": (it.get("price") or {}).get("amount"),
        "brand": ta.get("brand"), "model": ta.get("model"), "year": ta.get("year"), "km": ta.get("km"),
        "province": CITY_TO_PROVINCE.get(_ascii(loc.get("city"))),
        "seller_type": "dealer" if pro else "private",
        # GDPR: personal names of private sellers are never stored, only business info of dealers.
        "dealer_name": seller.get("name") if pro else None,
        "dealer_domain": _domain(seller.get("web")) if pro else None,
    }


def map_cochesnet(it):
    pro = it.get("sellerType") == "professional"
    return {
        "external_id": str(it["id"]), "title": it.get("title"), "price": it.get("price"),
        "brand": it.get("make"), "model": it.get("model"), "year": it.get("year"), "km": it.get("km"),
        "province": CITY_TO_PROVINCE.get(_ascii(it.get("province"))),
        "seller_type": "dealer" if pro else "private",
        "dealer_name": it.get("dealerName") if pro else None,
        "dealer_domain": _domain(it.get("dealerWebsite")) if pro else None,
    }


MAPPERS = {"wallapop": map_wallapop, "coches.net": map_cochesnet}

# Keys that can identify a private person. Removed from the payload BEFORE it is stored, so the raw layer
# never holds them (GDPR data minimisation). Dealer (business) fields are kept.
PII_KEYS = ("seller", "sellerName", "dealerName", "dealerWebsite", "phone", "email")


def redact_private(item, rec):
    return item if rec["seller_type"] != "private" else {k: v for k, v in item.items() if k not in PII_KEYS}


# ── extract (live) ──────────────────────────────────────────────────────
def fetch_apify_dataset(dataset_id, token):
    """Read a finished Apify run's dataset (does not start a scrape, so it costs no actor compute)."""
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?format=json&clean=true"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


# ── clean / match ───────────────────────────────────────────────────────
class ModelMatcher:
    """Map free text ('Toyota Corolla 1.8 Hybrid') to the DGT brand/model dimensions."""

    def __init__(self, con):
        self.brands = {r["brand_name"]: r["brand_id"] for r in con.execute("SELECT * FROM dim_brand")}
        self.models = {}
        for r in con.execute("SELECT model_id, brand_id, model_name FROM dim_model"):
            self.models.setdefault(r["brand_id"], []).append((r["model_name"], r["model_id"]))
        for b in self.models:
            self.models[b].sort(key=lambda x: -len(x[0]))       # longest name first: 'YARIS CROSS' before 'YARIS'

    def match(self, brand, model, title):
        b = canonical_brand(_ascii(brand)) if brand else None
        if b not in self.brands:
            b = next((n for n in self.brands if _ascii(title).startswith(n)), None)
        if b is None:
            return None, None
        text = f" {_ascii(model)} {_ascii(title)} "
        for name, mid in self.models.get(self.brands[b], []):
            if f" {name} " in text:
                return self.brands[b], mid
        return self.brands[b], None


def _valid(rec):
    """Discard obviously wrong values instead of letting them poison price statistics."""
    try:
        price, year, km = float(rec["price"]), int(rec["year"]), int(rec["km"])
    except (TypeError, ValueError):
        return None
    if not (500 <= price <= 500_000 and 1995 <= year <= 2027 and 0 <= km <= 1_000_000):
        return None
    return price, year, km


# ── load ────────────────────────────────────────────────────────────────
def load_snapshot(con, portal, items, scraped_on, matcher, synthetic=False):
    """Idempotent: same payload twice -> ignored. A new price -> new row in listing_price."""
    mapper = MAPPERS[portal]
    stats = {"items": len(items), "loaded": 0, "invalid": 0, "unmatched_model": 0, "price_changes": 0}
    for it in items:
        rec = mapper(it)
        payload = json.dumps(redact_private(it, rec), sort_keys=True, ensure_ascii=False)
        ph = hashlib.sha256(payload.encode()).hexdigest()[:16]
        con.execute("INSERT OR IGNORE INTO raw_apify VALUES (?,?,?,?,?,?)",
                    (portal, str(it.get("id")), scraped_on, ph, payload, int(synthetic)))
        v = _valid(rec)
        if v is None:
            stats["invalid"] += 1
            continue
        price, year, km = v
        brand_id, model_id = matcher.match(rec["brand"], rec["model"], rec["title"])
        if model_id is None:
            stats["unmatched_model"] += 1
            continue
        row = con.execute("SELECT listing_id FROM listing WHERE portal=? AND external_id=?",
                          (portal, rec["external_id"])).fetchone()
        if row is None:
            lid = con.execute(
                """INSERT INTO listing(portal, external_id, brand_id, model_id, title, year, km, province_code,
                   seller_type, dealer_name, dealer_domain, first_seen, last_seen, is_active, is_synthetic)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (portal, rec["external_id"], brand_id, model_id, rec["title"], year, km, rec["province"],
                 rec["seller_type"], rec["dealer_name"], rec["dealer_domain"], scraped_on, scraped_on,
                 int(synthetic))).lastrowid
        else:
            lid = row["listing_id"]
            con.execute("UPDATE listing SET last_seen=MAX(last_seen,?), km=? WHERE listing_id=?", (scraped_on, km, lid))
        last = con.execute("SELECT price_eur FROM listing_price WHERE listing_id=? ORDER BY observed_on DESC LIMIT 1",
                           (lid,)).fetchone()
        if last is None or last["price_eur"] != price:      # store changes only
            con.execute("INSERT OR REPLACE INTO listing_price VALUES (?,?,?)", (lid, scraped_on, price))
            stats["price_changes"] += 1 if last else 0
        stats["loaded"] += 1
    con.commit()
    return stats


def finalize(con, latest_scrape):
    """Listing not seen in the latest scrape -> inactive (delisted)."""
    con.execute("UPDATE listing SET is_active = (last_seen >= ?)", (latest_scrape,))
    con.commit()


def dedupe(con):
    """Same car on several portals -> one group. Never deletes rows, only labels them.
    Key for dealer stock: dealer + brand + model + year + km. Private ads keep their own key."""
    con.execute("""UPDATE listing SET group_key = CASE WHEN seller_type='dealer' AND dealer_domain IS NOT NULL
        THEN dealer_domain||'|'||brand_id||'|'||model_id||'|'||year||'|'||km
        ELSE portal||':'||external_id END""")
    con.commit()
    n_rows = con.execute("SELECT COUNT(*) FROM listing").fetchone()[0]
    n_groups = con.execute("SELECT COUNT(DISTINCT group_key) FROM listing").fetchone()[0]
    return n_rows, n_groups


# ── mart: price range per model ─────────────────────────────────────────
def latest_price_per_group(con):
    """One price per physical car (group), from its most recent observation. Active listings only."""
    sql = """
    SELECT l.group_key, l.brand_id, l.model_id, l.year, l.km, l.is_synthetic,
           (SELECT price_eur FROM listing_price p WHERE p.listing_id=l.listing_id ORDER BY observed_on DESC LIMIT 1) AS price
    FROM listing l WHERE l.is_active=1 GROUP BY l.group_key"""
    return con.execute(sql).fetchall()


def build_price_bands(con, min_n=8):
    con.execute("DELETE FROM mart_price_band")
    by_model = {}
    for r in latest_price_per_group(con):
        by_model.setdefault((r["brand_id"], r["model_id"]), []).append((r["price"], r["is_synthetic"]))
    for (b, m), vals in by_model.items():
        prices = sorted(p for p, _ in vals)
        if len(prices) < min_n:
            continue
        q = statistics.quantiles(prices, n=10, method="inclusive")        # deciles
        names = con.execute("SELECT b.brand_name, m.model_name FROM dim_model m JOIN dim_brand b USING(brand_id) "
                            "WHERE m.model_id=?", (m,)).fetchone()
        con.execute("INSERT INTO mart_price_band VALUES (?,?,?,?,?,?,?)",
                    (names[0], names[1], len(prices), q[0], statistics.median(prices), q[-1], int(any(s for _, s in vals))))
    con.commit()
