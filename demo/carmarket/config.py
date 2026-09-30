"""Paths and tunable assumptions for the demo. Everything here can be overridden with env vars."""
import os
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = DEMO_DIR / "data"
RAW_DIR = DATA_DIR / "raw" / "dgt"
FIXTURE_DIR = DATA_DIR / "fixtures"
OUT_DIR = DEMO_DIR / "out"
DB_PATH = Path(os.getenv("CARMARKET_DB", DATA_DIR / "carmarket.sqlite"))
LEDGER_PATH = DATA_DIR / "similarweb_ledger.json"

# ── Similarweb credit protection ─────────────────────────────────────────────
# The same Similarweb account feeds other critical reports, so this pipeline
# must never drain it. Three independent brakes (all must pass before a live call):
SW_RESERVE_CREDITS = int(os.getenv("SW_RESERVE_CREDITS", "5000"))   # always left untouched for other reports
SW_MONTHLY_CAP = int(os.getenv("SW_MONTHLY_CAP", "300"))            # max credits this pipeline may spend per calendar month
SW_RUN_CAP = int(os.getenv("SW_RUN_CAP", "120"))                    # max credits in a single run
SW_CREDITS_PER_RESULT = int(os.getenv("SW_CREDITS_PER_RESULT", "1"))  # docs: 1 data credit per result

# ── Cost model assumptions (PLACEHOLDERS: calibrate with your own Google Ads data) ──
ASSUMED_CPC_EUR = float(os.getenv("ASSUMED_CPC_EUR", "0.55"))  # average cost per paid click, auto retail, Spain
