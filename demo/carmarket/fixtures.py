"""SYNTHETIC scraper snapshots so the demo runs without Apify credentials.

Everything generated here is fake: prices, dealers, stock. Dealers use the reserved `.example` TLD so no
real business is implied. The *models* are taken from the real DGT data loaded in the database, so the two
domains join. Files are written once and then reused (deterministic seed).
"""
import hashlib
import json
import random
from datetime import date, timedelta

from .config import FIXTURE_DIR

CITIES = ["Madrid", "Barcelona", "Valencia", "Sevilla", "Málaga", "Bilbao"]
FUELS = ["Hybrid", "Diesel", "Gasolina", "Hybrid", "Eléctrico"]


def _base_price(brand, model):
    h = int(hashlib.md5(f"{brand}{model}".encode()).hexdigest()[:6], 16)
    return 14_000 + h % 30_000                       # synthetic: 14k-44k


def _top_models(con, n=8):
    rows = con.execute("""SELECT b.brand_name, m.model_name, COUNT(*) c FROM fact_registration f
        JOIN dim_brand b USING(brand_id) JOIN dim_model m USING(model_id)
        WHERE f.segment='car' AND f.is_new=1 GROUP BY 1,2 ORDER BY c DESC LIMIT ?""", (n,)).fetchall()
    return [(r[0], r[1]) for r in rows]


def _as_wallapop(c, lid):
    pro = c["dealer"] is not None
    return {"id": f"wp-{lid}", "title": f'{c["brand"].title()} {c["model"].title()} {c["fuel"]} {c["year"]}',
            "price": {"amount": c["price"], "currency": "EUR"},
            "location": {"city": c["city"], "postal_code": "00000"},
            "type_attributes": {"brand": c["brand"].title(), "model": c["model"].title(), "year": c["year"],
                                "km": c["km"], "engine": c["fuel"]},
            "seller": {"kind": "professional" if pro else "private",
                       "name": c["dealer"]["name"] if pro else "Vendedor particular",
                       "web": f'https://www.{c["dealer"]["domain"]}' if pro else None}}


def _as_cochesnet(c, lid):
    pro = c["dealer"] is not None
    return {"id": int(lid.replace("-", "")) if lid.replace("-", "").isdigit() else abs(hash(lid)) % 10**9,
            "title": f'{c["brand"]} {c["model"]} {c["fuel"]}', "price": c["price"], "year": c["year"], "km": c["km"],
            "make": c["brand"], "model": c["model"], "province": c["city"],
            "sellerType": "professional" if pro else "particular",
            "dealerName": c["dealer"]["name"] if pro else None,
            "dealerWebsite": f'{c["dealer"]["domain"]}' if pro else None}


def build(con, today: date, seed=7):
    """Return {(portal, iso_date): [items]} and the dealer list; write JSON files to data/fixtures."""
    rnd = random.Random(seed)
    models = _top_models(con)
    dealers = [{"name": f"Demo Motor {CITIES[i % len(CITIES)]} {i + 1:02d}", "domain": f"demomotor{i + 1:02d}.example",
                "city": CITIES[i % len(CITIES)], "stock": rnd.randint(10, 28)} for i in range(14)]
    snap_a, snap_b = today - timedelta(days=30), today

    def new_car(dealer=None):
        brand, model = rnd.choice(models)
        age = rnd.randint(0, 6)
        km = int(age * rnd.randint(9_000, 18_000) + rnd.randint(500, 6_000))
        price = _base_price(brand, model) * (0.87 ** age) * rnd.uniform(0.93, 1.07) * (1 - min(km, 150_000) / 900_000)
        return {"brand": brand, "model": model, "year": 2026 - age, "km": km, "fuel": rnd.choice(FUELS),
                "price": int(round(price, -1)), "city": dealer["city"] if dealer else rnd.choice(CITIES), "dealer": dealer}

    stock = []
    for d in dealers:
        stock += [new_car(d) for _ in range(d["stock"])]
    stock += [new_car() for _ in range(120)]                                # private sellers
    for i, c in enumerate(stock):
        c["uid"] = f"{i:05d}"
        c["portals"] = ["wallapop", "coches.net"] if (c["dealer"] and rnd.random() < 0.35) else \
            [rnd.choice(["wallapop", "coches.net"])]

    snaps = {("wallapop", snap_a): [], ("coches.net", snap_a): [], ("wallapop", snap_b): [], ("coches.net", snap_b): []}
    conv = {"wallapop": _as_wallapop, "coches.net": _as_cochesnet}
    for c in stock:
        for p in c["portals"]:
            snaps[(p, snap_a)].append(conv[p](c, f'{c["uid"]}'))
    # 30 days later: ~35% delisted (sold proxy), ~12% drop price 3-7%, ~15% new stock
    for c in stock:
        if rnd.random() < 0.35:
            continue
        c2 = dict(c)
        if rnd.random() < 0.12:
            c2["price"] = int(round(c["price"] * rnd.uniform(0.93, 0.97), -1))
        for p in c["portals"]:
            snaps[(p, snap_b)].append(conv[p](c2, f'{c["uid"]}'))
    for j in range(int(len(stock) * 0.15)):
        d = rnd.choice(dealers + [None] * 3)
        c = new_car(d)
        c["uid"] = f"9{j:04d}"
        p = rnd.choice(["wallapop", "coches.net"])
        snaps[(p, snap_b)].append(conv[p](c, c["uid"]))

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = {}
    for (portal, d), items in snaps.items():
        path = FIXTURE_DIR / f"synthetic_{portal.replace('.', '')}_{d.isoformat()}.json"
        path.write_text(json.dumps(items, ensure_ascii=False, indent=1))
        out[(portal, d.isoformat())] = path
    return out, dealers
