# Car market demo

Runs the whole chain end to end with **zero installs** (Python 3.10+, standard library, SQLite).

| Source | In the demo | Real? |
|---|---|---|
| DGT daily registrations | downloaded from dgt.es, parsed, cleaned, loaded | **Real** |
| Wallapop / coches.net (Apify) | fixtures in `data/fixtures/` written by `carmarket/fixtures.py` | **Synthetic**: prices, dealers and stock are invented; dealer domains use `.example` |
| Similarweb traffic | deterministic mock in `carmarket/traffic.py` | **Synthetic**, spends 0 credits |

```bash
python3 demo/run_demo.py --reset                    # everything (downloads ~10 DGT files, ~1 s each)
python3 demo/run_demo.py --traffic-mode dry-run     # print the Similarweb credit plan only
python3 demo/run_demo.py --apify wallapop=<datasetId> --apify coches.net=<datasetId>   # needs APIFY_TOKEN
SIMILARWEB_API_KEY=... python3 demo/run_demo.py --traffic-mode live                    # brakes apply, see below
python3 -m unittest discover -s demo/tests -v
```

Output: `demo/data/carmarket.sqlite` (open it with the `sqlite3` CLI, DBeaver or DB Browser for SQLite) and `demo/out/summary.json`.

## Similarweb credit protection
Live calls are refused unless all pass: balance (free `/capabilities` call) minus the plan stays above
`SW_RESERVE_CREDITS` (5000), this pipeline's spend this month stays under `SW_MONTHLY_CAP` (300), and one run stays under
`SW_RUN_CAP` (120). Everything fetched is cached raw, so a closed month is never bought twice.

## Assumptions to calibrate (`carmarket/config.py`)
`ASSUMED_CPC_EUR` is a placeholder (0.55 €). Cost per car scales linearly with it.

## Known gaps
- DGT code lists (fuel, service, body) are kept raw until mapped from the official layout PDF.
- The exact JSON of Similarweb's `traffic-sources/overview-share` is unverified; raw responses are stored so the parser can be fixed for free.
- Apify field names in `carmarket/listings.py` (`MAPPERS`) are expectations; adjust to the real actors' output.
