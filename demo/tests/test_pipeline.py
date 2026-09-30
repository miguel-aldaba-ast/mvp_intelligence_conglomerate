"""Run:  python3 -m unittest discover -s demo/tests -v     (stdlib only, no network)"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from carmarket import db, dgt, listings, traffic  # noqa: E402


def make_line(**over):
    """Build a syntactically valid 714-char DGT record from the layout."""
    vals = {"FEC_MATRICULA": "28092026", "MARCA_ITV": "TOYOTA", "MODELO_ITV": "TOYOTA COROLLA",
            "BASTIDOR_ITV": "JTDKN3DU5*************", "COD_PROVINCIA_VEH": "M", "IND_NUEVO_USADO": "N",
            "CATEGORIA_HOMOLOGACION_EUROPEA_ITV": "M1", "RENTING": "N", "KW_ITV": "72.00", "CO2_ITV": "98"}
    vals.update(over)
    return "".join(str(vals.get(n, "")).ljust(w)[:w] for n, w in dgt.LAYOUT)


class DgtParsing(unittest.TestCase):
    def test_line_length_and_fields(self):
        line = make_line()
        self.assertEqual(len(line), 714)
        f = dgt.split_fields(line)
        self.assertEqual(f["MARCA_ITV"], "TOYOTA")
        self.assertEqual(f["FEC_MATRICULA"], "28092026")

    def test_clean_types_and_placeholders(self):
        rec = dgt.clean(dgt.split_fields(make_line(VERSION_ITV="ND", CILINDRADA_ITV="0")))
        self.assertEqual(rec["registration_date"], "2026-09-28")        # DDMMYYYY -> ISO
        self.assertEqual(rec["model"], "COROLLA")                       # brand prefix stripped
        self.assertIsNone(rec["version_raw"])                           # 'ND' -> NULL
        self.assertIsNone(rec["engine_cc"])                             # 0 -> NULL
        self.assertEqual(rec["vin_prefix"], "JTDKN3DU5")                # '*' padding removed
        self.assertEqual(rec["segment"], "car")
        self.assertEqual(rec["is_new"], 1)

    def test_invalid_date_rejected(self):
        self.assertIsNone(dgt.clean(dgt.split_fields(make_line(FEC_MATRICULA="31022026"))))

    def test_load_is_idempotent_and_rejects_bad_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt = Path(tmp) / "day.txt"
            good, other = make_line(), make_line(MODELO_ITV="TOYOTA YARIS")
            txt.write_text("Vehículos matriculados. banner\n" + good + "\n" + other + "\n" + "short line\n", encoding="latin-1")
            con = db.connect(Path(tmp) / "t.sqlite")
            dims = db.Dims(con)
            self.assertEqual(dgt.load_file(con, txt, dims), (3, 2, 1))
            self.assertEqual(dgt.load_file(con, txt, dims), (3, 0, 1))  # second run inserts nothing
            self.assertEqual(con.execute("SELECT COUNT(*) FROM fact_registration").fetchone()[0], 2)


class Matching(unittest.TestCase):
    def test_longest_model_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.connect(Path(tmp) / "t.sqlite")
            d = db.Dims(con)
            b = d.brand("TOYOTA")
            yaris, cross = d.model(b, "YARIS"), d.model(b, "YARIS CROSS")
            m = listings.ModelMatcher(con)
            self.assertEqual(m.match("Toyota", "Yaris Cross", "Toyota Yaris Cross Hybrid 2022"), (b, cross))
            self.assertEqual(m.match(None, None, "Toyota Yaris 1.5 2019"), (b, yaris))

    def test_private_seller_data_not_stored_in_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            con = db.connect(Path(tmp) / "t.sqlite")
            d = db.Dims(con)
            d.model(d.brand("TOYOTA"), "COROLLA")
            item = {"id": 1, "title": "Toyota Corolla 2020", "price": {"amount": 15000},
                    "location": {"city": "Madrid"}, "type_attributes": {"brand": "Toyota", "model": "Corolla", "year": 2020, "km": 40000},
                    "seller": {"kind": "private", "name": "Maria Lopez"}}
            listings.load_snapshot(con, "wallapop", [item], "2026-09-29", listings.ModelMatcher(con))
            raw = con.execute("SELECT payload FROM raw_apify").fetchone()[0]
            self.assertNotIn("Maria", raw)
            self.assertEqual(con.execute("SELECT seller_type, dealer_name FROM listing").fetchone()[:], ("private", None))

    def test_invalid_prices_discarded(self):
        self.assertIsNone(listings._valid({"price": 1, "year": 2020, "km": 10}))
        self.assertIsNone(listings._valid({"price": 9000, "year": 1900, "km": 10}))
        self.assertEqual(listings._valid({"price": 9000, "year": 2020, "km": 10}), (9000.0, 2020, 10))


class SimilarwebProtection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = db.connect(Path(self.tmp.name) / "t.sqlite")
        self.guard = traffic.CreditGuard(Path(self.tmp.name) / "ledger.json", reserve=1000, monthly_cap=100, run_cap=50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ranges(self):
        self.assertEqual(traffic.contiguous_ranges(["2026-06", "2026-07", "2026-09"]),
                         [("2026-06", "2026-07"), ("2026-09", "2026-09")])
        self.assertEqual(traffic.last_closed_months(date(2026, 1, 15), 3), ["2025-10", "2025-11", "2025-12"])

    def test_cache_makes_second_plan_empty(self):
        sw = traffic.SimilarwebClient(self.con, "mock", guard=self.guard)
        months = ["2026-06", "2026-07", "2026-08"]
        plan = sw.plan(["a.example"], months)
        self.assertEqual(sum(r["credits"] for r in plan), 6)           # 2 endpoints x 3 months x 1 credit
        sw.execute(plan)
        self.assertEqual(sw.plan(["a.example"], months), [])            # nothing to pay for twice
        self.assertEqual(sw.plan(["a.example"], months + ["2026-09"])[0]["start"], "2026-09")  # only the new month

    def test_dry_run_fetches_nothing(self):
        sw = traffic.SimilarwebClient(self.con, "dry-run", guard=self.guard)
        sw.execute(sw.plan(["a.example"], ["2026-08"]))
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM raw_traffic").fetchone()[0], 0)

    def test_brakes(self):
        g = self.guard
        g.authorize(40, remaining_live=5000)                            # ok
        with self.assertRaises(traffic.BudgetExceeded):
            g.authorize(60, remaining_live=5000)                        # > run cap
        g.record(80, date(2026, 9, 1))
        with self.assertRaises(traffic.BudgetExceeded):
            g.authorize(30, remaining_live=5000, today=date(2026, 9, 20))   # monthly cap 80+30 > 100
        g.authorize(30, remaining_live=5000, today=date(2026, 10, 2))       # new month resets the local ledger
        with self.assertRaises(traffic.BudgetExceeded):
            g.authorize(10, remaining_live=1005)                        # would leave < 1000 for other reports

    def test_live_without_key_refuses(self):
        sw = traffic.SimilarwebClient(self.con, "live", api_key=None, guard=self.guard)
        with self.assertRaises(traffic.BudgetExceeded):
            sw.execute(sw.plan(["a.example"], ["2026-08"]))

    def test_channel_parser_tolerates_both_shapes(self):
        self.assertEqual(traffic.parse_channel_row({"date": "2026-08-01", "paid_search": 0.1})["paid_search"], 0.1)
        self.assertEqual(traffic.parse_channel_row({"source_type": "Paid Search", "share": 0.2}), {"paid_search": 0.2})


if __name__ == "__main__":
    unittest.main()
