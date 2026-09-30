#!/usr/bin/env python3
"""End-to-end demo:  DGT (real)  +  Apify listings (synthetic fixtures)  +  Similarweb traffic (mock)  ->  marts.

    python3 demo/run_demo.py                       # downloads the last 10 DGT daily files, everything else offline
    python3 demo/run_demo.py --traffic-mode dry-run   # print the Similarweb credit plan, fetch nothing
    python3 demo/run_demo.py --traffic-mode live      # real Similarweb calls (needs SIMILARWEB_API_KEY + budget checks)
    python3 demo/run_demo.py --apify wallapop=<datasetId> --apify coches.net=<datasetId>   # real Apify data (APIFY_TOKEN)
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from carmarket import config, db, dgt, fixtures, listings, marts, traffic  # noqa: E402

AS_OF = date(2026, 9, 29)          # fixed so the synthetic fixtures are reproducible
fmt_eur = lambda x: "-" if x is None else f"{x:,.0f} €".replace(",", ".")


def step(n, title):
    print(f"\n[{n}] {title}\n" + "─" * 78)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10, help="newest N DGT daily files to load")
    ap.add_argument("--reset", action="store_true", help="delete the demo database first")
    ap.add_argument("--traffic-mode", choices=["mock", "dry-run", "live"], default="mock")
    ap.add_argument("--apify", action="append", default=[], metavar="PORTAL=DATASET_ID")
    a = ap.parse_args()

    if a.reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    con = db.connect()
    dims = db.Dims(con)
    config.OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── 1. DGT (REAL) ───────────────────────────────────────────────────
    step(1, "DGT registrations  [REAL open data]  discover → download → parse → clean → load")
    dgt.load_provinces(con)
    links = dgt.discover()
    days = sorted(links)[-a.days:]
    print(f"  listing shows {len(links)} daily files; loading the newest {len(days)}: {days[0]} … {days[-1]}")
    for d in days:
        read, ins, rej = dgt.load_file(con, dgt.download(d, links[d]), dims)
        print(f"  {d}: read {read:>6,}  inserted {ins:>6,}  rejected {rej}")
    dq = dgt.data_quality(con)
    print("  data quality:", ", ".join(f"{k}={v:,.2f}" for k, v in dq.items()))

    # ── 2. Listings (SYNTHETIC unless --apify) ──────────────────────────
    step(2, "Listings  [SYNTHETIC fixtures unless --apify]  raw JSON → normalise → dedupe → price history")
    matcher = listings.ModelMatcher(con)
    if a.apify:
        token = os.environ["APIFY_TOKEN"]
        for spec in a.apify:
            portal, ds = spec.split("=")
            st = listings.load_snapshot(con, portal, listings.fetch_apify_dataset(ds, token), AS_OF.isoformat(), matcher)
            print(f"  {portal}: {st}")
        synthetic, snap_dates = False, [AS_OF.isoformat()]
    else:
        files, _ = fixtures.build(con, AS_OF) if not list(config.FIXTURE_DIR.glob("synthetic_*.json")) else (None, None)
        snap_dates = sorted({p.stem.rsplit("_", 1)[1] for p in config.FIXTURE_DIR.glob("synthetic_*.json")})
        for d in snap_dates:
            for portal, tag in (("wallapop", "wallapop"), ("coches.net", "cochesnet")):
                items = json.loads((config.FIXTURE_DIR / f"synthetic_{tag}_{d}.json").read_text())
                print(f"  {d} {portal:<10}", listings.load_snapshot(con, portal, items, d, matcher, synthetic=True))
        synthetic = True
    listings.finalize(con, snap_dates[-1])
    n_rows, n_groups = listings.dedupe(con)
    print(f"  cross-portal dedupe: {n_rows} listing rows → {n_groups} distinct cars")
    listings.build_price_bands(con)

    # ── 3. Traffic (MOCK by default) ────────────────────────────────────
    step(3, f"Similarweb traffic  [{a.traffic_mode.upper()}]  plan → budget brakes → incremental pull")
    domains = [r[0] for r in con.execute(
        "SELECT DISTINCT dealer_domain FROM listing WHERE seller_type='dealer' AND dealer_domain IS NOT NULL ORDER BY 1")]
    months = traffic.last_closed_months(AS_OF, 3)
    sw = traffic.SimilarwebClient(con, a.traffic_mode, os.getenv("SIMILARWEB_API_KEY"))
    plan = sw.plan(domains, months)
    print(f"  {len(domains)} dealer domains × {len(months)} closed months × 2 endpoints → "
          f"{len(plan)} requests missing from cache, {sum(r['credits'] for r in plan)} credits if live")
    print(f"  brakes: keep ≥{config.SW_RESERVE_CREDITS} credits for other reports · ≤{config.SW_MONTHLY_CAP}/month · ≤{config.SW_RUN_CAP}/run")
    print("  result:", sw.execute(plan, AS_OF))
    print("  second run would plan:", len(sw.plan(domains, months)), "requests (cache hit)" if a.traffic_mode != "dry-run" else "requests")
    traffic.build_fact_dealer_traffic(con)

    # ── 4. Marts ────────────────────────────────────────────────────────
    step(4, "Marts  price range · dealer ad cost · cost added per vehicle")
    marts.dealer_ad_cost(con)
    marts.model_ad_cost(con)
    sens = marts.cpc_sensitivity(con)
    tag = " [SYNTHETIC INPUTS]" if synthetic else ""

    print(f"\n  Price range per model (P10 – median – P90){tag}")
    for r in con.execute("SELECT * FROM mart_price_band ORDER BY n_listings DESC LIMIT 8"):
        print(f"    {r['brand_name']:<11}{r['model_name']:<18} n={r['n_listings']:<3} {fmt_eur(r['p10']):>10} – {fmt_eur(r['median']):>10} – {fmt_eur(r['p90']):>10}")

    print(f"\n  Dealer ad cost per vehicle sold (CPC assumption {config.ASSUMED_CPC_EUR} €){tag}")
    for r in con.execute("SELECT * FROM mart_dealer_ad_cost ORDER BY cost_per_unit_eur DESC NULLS LAST LIMIT 6"):
        print(f"    {r['dealer_domain']:<22} visits/mo {r['avg_monthly_visits']:>8,.0f}  paid {r['paid_share']*100:>4.1f}%  "
              f"media ≈{fmt_eur(r['est_media_cost_eur']):>9}  sold {r['units_sold_proxy']:>2}  → {fmt_eur(r['cost_per_unit_eur']):>8}/car = {(r['cost_pct_of_price'] or 0):.1f}% of price")
    print("\n  Sensitivity of portfolio cost/unit to the CPC assumption:")
    for f, s in sens.items():
        print(f"    CPC {s['cpc']:.2f} € → {fmt_eur(s['cost_per_unit'])} per car sold ({(s['pct_of_price'] or 0):.1f}% of price)")

    print(f"\n  Cost added per model vs real DGT registrations{tag.replace('INPUTS', 'LISTING+TRAFFIC INPUTS')}")
    for r in con.execute("SELECT * FROM mart_model_ad_cost ORDER BY dgt_registrations DESC LIMIT 8"):
        print(f"    {r['brand_name']:<11}{r['model_name']:<18} DGT new regs {r['dgt_registrations']:>4}  dealer sales proxy {r['units_sold_proxy']:>3}  "
              f"ad cost/car {fmt_eur(r['avg_cost_per_unit_eur']):>8} ({r['cost_pct_of_price']:.1f}% of median price)")

    # ── 5. Export ───────────────────────────────────────────────────────
    summary = {
        "generated": AS_OF.isoformat(), "dgt": marts.dgt_summary(con), "dq": dq,
        "synthetic_inputs": synthetic, "traffic_mode": a.traffic_mode, "assumed_cpc": config.ASSUMED_CPC_EUR,
        "cpc_sensitivity": {str(k): v for k, v in sens.items()},
        "price_bands": [dict(r) for r in con.execute("SELECT * FROM mart_price_band ORDER BY n_listings DESC LIMIT 8")],
        "dealers": [dict(r) for r in con.execute("SELECT * FROM mart_dealer_ad_cost ORDER BY cost_per_unit_eur DESC")],
        "models": [dict(r) for r in con.execute("SELECT * FROM mart_model_ad_cost ORDER BY dgt_registrations DESC")],
        "tables": {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (
            "raw_dgt", "fact_registration", "dim_brand", "dim_model", "raw_apify", "listing", "listing_price",
            "raw_traffic", "fact_dealer_traffic", "mart_price_band", "mart_dealer_ad_cost", "mart_model_ad_cost")},
    }
    (config.OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str))
    print(f"\n  wrote {config.OUT_DIR / 'summary.json'}   |   database: {config.DB_PATH}")


if __name__ == "__main__":
    main()
