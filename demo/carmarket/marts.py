"""Marts: where the three domains are mixed.

  DGT registrations (A) ──┐
  Listings + prices  (C) ─┼─►  brand × model  ─►  price range, units sold proxy, ad cost per vehicle
  Similarweb traffic (B) ─┘    dealer_domain ─►  est. media cost  ─►  cost added to each vehicle

DEALER AD COST MODEL (all assumptions are explicit, calibrate them before quoting numbers):
    paid_visits      = visits x (paid_search_share + display_share)        # social excluded: Similarweb social = organic+paid
    est_media_cost   = paid_visits x ASSUMED_CPC_EUR                       # PLACEHOLDER cpc
    units_sold_proxy = distinct cars that were listed in the first snapshot and are gone in the latest
    cost_per_unit    = est_media_cost / units_sold_proxy
This is an estimate of *traffic-implied* media spend, not an invoice. Compare it with any dealer's real Google
Ads data you can get to fit the CPC.
"""
import statistics

from . import config

# One row per physical dealer car (cross-portal duplicates collapsed): active flag + its last observed price.
DEALER_CARS = """
WITH g AS (SELECT group_key, MIN(listing_id) lid, MAX(is_active) any_active FROM listing
           WHERE seller_type='dealer' AND dealer_domain IS NOT NULL GROUP BY group_key)
SELECT l.dealer_domain d, l.dealer_name n, l.brand_id b, l.model_id m, l.is_synthetic syn, g.any_active,
       (SELECT price_eur FROM listing_price p WHERE p.listing_id=g.lid ORDER BY observed_on DESC LIMIT 1) price
FROM g JOIN listing l ON l.listing_id=g.lid"""


def dealer_ad_cost(con, cpc=None):
    cpc = config.ASSUMED_CPC_EUR if cpc is None else cpc
    con.execute("DELETE FROM mart_dealer_ad_cost")
    # units sold proxy per dealer: cars (groups) whose every listing is inactive, priced at last seen price
    sold = {}
    for r in con.execute(DEALER_CARS):
        e = sold.setdefault(r["d"], {"name": r["n"], "syn": r["syn"], "units": 0, "prices": []})
        if not r["any_active"]:
            e["units"] += 1
            e["prices"].append(r["price"])
    for t in con.execute("""SELECT domain, AVG(visits) v, AVG(COALESCE(paid_search_share,0)+COALESCE(display_share,0)) s,
                            AVG(visits*(COALESCE(paid_search_share,0)+COALESCE(display_share,0))) pv
                            FROM fact_dealer_traffic GROUP BY domain"""):
        e = sold.get(t["domain"])
        if not e:
            continue
        cost = t["pv"] * cpc
        u = e["units"]
        avg_p = statistics.mean(e["prices"]) if e["prices"] else None
        con.execute("INSERT INTO mart_dealer_ad_cost VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (t["domain"], e["name"], t["v"], t["s"], t["pv"], cost, u,
                     cost / u if u else None, avg_p, (cost / u / avg_p * 100) if (u and avg_p) else None, e["syn"]))
    con.commit()


def model_ad_cost(con):
    """Unit-weighted average of the selling dealer's cost/unit, per model, next to real DGT volume."""
    con.execute("DELETE FROM mart_model_ad_cost")
    cost = {r["dealer_domain"]: r["cost_per_unit_eur"] for r in con.execute(
        "SELECT dealer_domain, cost_per_unit_eur FROM mart_dealer_ad_cost WHERE cost_per_unit_eur IS NOT NULL")}
    per_model = {}
    for r in con.execute(DEALER_CARS):
        if r["any_active"] or r["d"] not in cost:
            continue
        e = per_model.setdefault((r["b"], r["m"]), {"costs": [], "prices": [], "syn": r["syn"]})
        e["costs"].append(cost[r["d"]])
        e["prices"].append(r["price"])
    for (b, m), e in per_model.items():
        names = con.execute("SELECT b.brand_name, m.model_name FROM dim_model m JOIN dim_brand b USING(brand_id) "
                            "WHERE m.model_id=?", (m,)).fetchone()
        reg = con.execute("SELECT COUNT(*) FROM fact_registration WHERE model_id=? AND is_new=1", (m,)).fetchone()[0]
        avg_c, med_p = statistics.mean(e["costs"]), statistics.median(e["prices"])
        con.execute("INSERT INTO mart_model_ad_cost VALUES (?,?,?,?,?,?,?,?)",
                    (names[0], names[1], len(e["costs"]), avg_c, med_p, avg_c / med_p * 100, reg, e["syn"]))
    con.commit()


def cpc_sensitivity(con, factors=(0.5, 1.0, 2.0)):
    """How much the portfolio-average cost per unit moves if the CPC assumption is wrong."""
    out = {}
    for f in factors:
        dealer_ad_cost(con, config.ASSUMED_CPC_EUR * f)
        r = con.execute("SELECT SUM(est_media_cost_eur), SUM(units_sold_proxy), AVG(cost_pct_of_price) FROM mart_dealer_ad_cost").fetchone()
        out[f] = {"cpc": round(config.ASSUMED_CPC_EUR * f, 2), "cost_per_unit": (r[0] / r[1]) if r[1] else None, "pct_of_price": r[2]}
    dealer_ad_cost(con)                               # restore base case
    return out


# ── real-data summaries (DGT only) ──────────────────────────────────────
def dgt_summary(con):
    q = lambda sql, *a: [dict(r) for r in con.execute(sql, a)]
    return {
        "days": q("SELECT registration_date d, COUNT(*) n FROM fact_registration GROUP BY 1 ORDER BY 1"),
        "by_segment": q("SELECT segment, COUNT(*) n FROM fact_registration GROUP BY 1 ORDER BY n DESC"),
        "top_brands_new_cars": q("""SELECT b.brand_name brand, COUNT(*) n FROM fact_registration f JOIN dim_brand b USING(brand_id)
            WHERE f.segment='car' AND f.is_new=1 GROUP BY 1 ORDER BY n DESC LIMIT 12"""),
        "top_models_new_cars": q("""SELECT b.brand_name brand, m.model_name model, COUNT(*) n FROM fact_registration f
            JOIN dim_brand b USING(brand_id) JOIN dim_model m USING(model_id)
            WHERE f.segment='car' AND f.is_new=1 GROUP BY 1,2 ORDER BY n DESC LIMIT 10"""),
        "top_provinces_new_cars": q("""SELECT COALESCE(p.province_name, f.province_veh) province, COUNT(*) n FROM fact_registration f
            LEFT JOIN dim_province p ON p.province_code=f.province_veh WHERE f.segment='car' AND f.is_new=1
            GROUP BY 1 ORDER BY n DESC LIMIT 8"""),
        "new_vs_used": q("SELECT is_new, COUNT(*) n FROM fact_registration WHERE segment='car' GROUP BY 1"),
        "renting_share_new_cars": q("SELECT is_renting, COUNT(*) n FROM fact_registration WHERE segment='car' AND is_new=1 GROUP BY 1"),
        "propulsion_codes_new_cars": q("SELECT propulsion_code code, COUNT(*) n FROM fact_registration WHERE segment='car' AND is_new=1 GROUP BY 1 ORDER BY n DESC"),
    }
