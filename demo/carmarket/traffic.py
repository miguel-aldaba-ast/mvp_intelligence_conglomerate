"""Domain B: Similarweb traffic, pulled *incrementally* and *under a hard credit budget*.

Why this file is careful: the Similarweb account is shared with other critical reports and is billed per
data point. So this pipeline never fetches something twice and can never spend the shared credits:

  1. CACHE     every (domain, endpoint, country, month) is stored raw in `raw_traffic`. A closed month never
               changes, so it is fetched once, ever. Re-parsing later is free.
  2. PLAN      before any call the plan (what is missing, how many credits) is printed. `dry-run` stops there.
  3. 3 BRAKES  a live call is refused unless ALL hold:
                 a) live balance (free /capabilities call) - planned >= SW_RESERVE_CREDITS
                 b) this pipeline's spend this calendar month + planned <= SW_MONTHLY_CAP   (local ledger)
                 c) planned <= SW_RUN_CAP
  4. MODES     'mock' (default, offline, synthetic) | 'dry-run' (plan only) | 'live' (needs SIMILARWEB_API_KEY).

Verified from the Similarweb docs: GET /v1/website/{domain}/total-traffic-and-engagement/visits
(params api_key, country, granularity, start_date, end_date YYYY-MM, main_domain_only; 1 credit per result)
and the free GET /capabilities?api_key= that returns `remaining_hits`.
NOT verified: exact JSON of traffic-sources/overview-share. The parser is tolerant and raw JSON is kept,
so adjust `parse_channel_row` on the first live response without spending more credits.
"""
import hashlib
import json
import urllib.parse
import urllib.request
from datetime import date

from . import config

BASE = "https://api.similarweb.com"
ENDPOINTS = {
    "visits": "/v1/website/{domain}/total-traffic-and-engagement/visits",
    "channels": "/v1/website/{domain}/traffic-sources/overview-share",
}


class BudgetExceeded(RuntimeError):
    pass


# ── month helpers ───────────────────────────────────────────────────────
def last_closed_months(as_of: date, n=3):
    y, m, out = as_of.year, as_of.month, []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        out.append(f"{y:04d}-{m:02d}")
    return sorted(out)


def contiguous_ranges(months):
    """['2026-06','2026-07','2026-09'] -> [('2026-06','2026-07'), ('2026-09','2026-09')] (1 request per range)."""
    idx = lambda s: int(s[:4]) * 12 + int(s[5:]) - 1
    out, start, prev = [], None, None
    for mth in sorted(months):
        if start is None:
            start = prev = mth
        elif idx(mth) == idx(prev) + 1:
            prev = mth
        else:
            out.append((start, prev))
            start = prev = mth
    if start:
        out.append((start, prev))
    return out


# ── budget ledger ───────────────────────────────────────────────────────
class CreditGuard:
    def __init__(self, path=config.LEDGER_PATH, reserve=config.SW_RESERVE_CREDITS,
                 monthly_cap=config.SW_MONTHLY_CAP, run_cap=config.SW_RUN_CAP):
        self.path, self.reserve, self.monthly_cap, self.run_cap = path, reserve, monthly_cap, run_cap

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def spent_this_month(self, today=None):
        return self._load().get((today or date.today()).strftime("%Y-%m"), 0)

    def record(self, credits, today=None):
        led = self._load()
        k = (today or date.today()).strftime("%Y-%m")
        led[k] = led.get(k, 0) + credits
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(led, indent=1))

    def authorize(self, planned, remaining_live, today=None):
        spent = self.spent_this_month(today)
        if planned > self.run_cap:
            raise BudgetExceeded(f"planned {planned} > per-run cap {self.run_cap}")
        if spent + planned > self.monthly_cap:
            raise BudgetExceeded(f"monthly cap: spent {spent} + planned {planned} > {self.monthly_cap}")
        if remaining_live is not None and remaining_live - planned < self.reserve:
            raise BudgetExceeded(f"reserve: {remaining_live} left - {planned} planned < {self.reserve} kept for other reports")


# ── mock (deterministic, offline) ───────────────────────────────────────
def mock_profile(domain):
    """Stable pseudo-random traffic profile per domain. SYNTHETIC: not real Similarweb data."""
    h = int(hashlib.md5(domain.encode()).hexdigest()[:8], 16)
    return {"base_visits": 6_000 + (h % 60_000),                      # visits / month
            "paid": 0.04 + ((h >> 8) % 100) / 100 * 0.22,              # paid search share 4%-26%
            "display": 0.01 + ((h >> 16) % 100) / 100 * 0.05,
            "social": 0.03 + ((h >> 4) % 100) / 100 * 0.08}


def mock_response(domain, endpoint, months):
    p = mock_profile(domain)
    rows = []
    for i, mth in enumerate(months):
        jitter = 1 + (int(hashlib.md5(f"{domain}{mth}".encode()).hexdigest()[:4], 16) % 21 - 10) / 100
        if endpoint == "visits":
            rows.append({"date": f"{mth}-01", "visits": round(p["base_visits"] * jitter)})
        else:
            rows.append({"date": f"{mth}-01", "paid_search": round(p["paid"] * jitter, 4),
                         "display_ads": round(p["display"], 4), "social": round(p["social"], 4),
                         "organic_search": round(0.35, 4), "direct": round(0.30, 4)})
    return {"meta": {"status": "Success", "mock": True}, "visits": rows}


# ── client ──────────────────────────────────────────────────────────────
class SimilarwebClient:
    def __init__(self, con, mode="mock", api_key=None, guard=None, country="es"):
        assert mode in ("mock", "dry-run", "live")
        self.con, self.mode, self.api_key, self.country = con, mode, api_key, country
        self.guard = guard or CreditGuard()

    # free call, does not consume credits
    def remaining_credits(self):
        url = f"{BASE}/capabilities?api_key={urllib.parse.quote(self.api_key)}"
        return json.loads(urllib.request.urlopen(url, timeout=30).read()).get("remaining_hits")

    def plan(self, domains, months, endpoints=("visits", "channels")):
        """Missing (domain, endpoint, [months]) work items -> list of requests with credit estimate."""
        reqs = []
        for d in domains:
            for ep in endpoints:
                have = {r[0] for r in self.con.execute(
                    "SELECT month FROM raw_traffic WHERE domain=? AND endpoint=? AND country=?", (d, ep, self.country))}
                for a, b in contiguous_ranges([m for m in months if m not in have]):
                    n = sum(1 for _ in _span(a, b))
                    reqs.append({"domain": d, "endpoint": ep, "start": a, "end": b, "credits": n * config.SW_CREDITS_PER_RESULT})
        return reqs

    def _fetch_live(self, r):
        q = urllib.parse.urlencode({"api_key": self.api_key, "country": self.country, "granularity": "monthly",
                                    "start_date": r["start"], "end_date": r["end"], "main_domain_only": "false"})
        url = BASE + ENDPOINTS[r["endpoint"]].format(domain=r["domain"]) + "?" + q
        return json.loads(urllib.request.urlopen(url, timeout=60).read())

    def _store(self, r, payload, source):
        for row in payload.get("visits", []):
            mth = row["date"][:7]
            if r["start"] <= mth <= r["end"]:
                self.con.execute("INSERT OR REPLACE INTO raw_traffic(domain,endpoint,country,month,payload,source) "
                                 "VALUES (?,?,?,?,?,?)",
                                 (r["domain"], r["endpoint"], self.country, mth, json.dumps(row), source))
        self.con.commit()

    def execute(self, reqs, today=None):
        total = sum(r["credits"] for r in reqs)
        if not reqs:
            return {"requests": 0, "credits": 0, "mode": self.mode, "note": "everything already cached"}
        if self.mode == "dry-run":
            return {"requests": len(reqs), "credits": total, "mode": "dry-run", "note": "nothing fetched"}
        if self.mode == "mock":
            for r in reqs:
                months = [f"{y:04d}-{m:02d}" for y, m in _span(r["start"], r["end"])]
                self._store(r, mock_response(r["domain"], r["endpoint"], months), "mock")
            return {"requests": len(reqs), "credits": 0, "mode": "mock", "note": f"synthetic; would cost {total} credits live"}
        # live
        if not self.api_key:
            raise BudgetExceeded("SIMILARWEB_API_KEY not set")
        self.guard.authorize(total, self.remaining_credits(), today)
        spent = 0
        for r in reqs:
            self._store(r, self._fetch_live(r), "live")
            spent += r["credits"]
            self.guard.record(r["credits"], today)      # written after each call: a crash never loses the count
        return {"requests": len(reqs), "credits": spent, "mode": "live", "note": "ok"}


def _span(a, b):
    y, m = int(a[:4]), int(a[5:])
    yb, mb = int(b[:4]), int(b[5:])
    while (y, m) <= (yb, mb):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ── parse raw -> fact ───────────────────────────────────────────────────
def parse_channel_row(row):
    """Return (paid_search, display, social) shares as fractions. Tolerant to wide ({'paid_search':..}) and
    long ({'source_type':'Paid Search','share':..}) shapes. VERIFY against the first live response."""
    norm = lambda s: s.lower().replace(" ", "_")
    if "source_type" in row:
        return {norm(row["source_type"]): row.get("share")}
    return {norm(k): v for k, v in row.items() if isinstance(v, (int, float))}


def build_fact_dealer_traffic(con):
    con.execute("DELETE FROM fact_dealer_traffic")
    visits = {(r["domain"], r["month"]): (json.loads(r["payload"]).get("visits"), r["source"])
              for r in con.execute("SELECT * FROM raw_traffic WHERE endpoint='visits'")}
    for r in con.execute("SELECT * FROM raw_traffic WHERE endpoint='channels'"):
        sh = parse_channel_row(json.loads(r["payload"]))
        v, src = visits.get((r["domain"], r["month"]), (None, r["source"]))
        if v is None:
            continue
        con.execute("INSERT INTO fact_dealer_traffic VALUES (?,?,?,?,?,?,?)",
                    (r["domain"], r["month"], v, sh.get("paid_search"), sh.get("display_ads"), sh.get("social"), src))
    con.commit()
