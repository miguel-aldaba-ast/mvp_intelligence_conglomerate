# Spanish Car Market Intelligence — Data Platform Plan

> Status: **DRAFT v0.2** (DGT layout and URLs verified on real data; see `demo/`) · Date: 2026-09-29 · Owner: Miguel
> Scope: research, sources, data types, storage, data models, ETL/cleaning rules and use cases for the three data domains below.

**Legend used in this document**
- ✅ **Verified** — confirmed in a source I read (linked in [§13](#13-sources)).
- ⚠️ **Unverified / estimate** — my inference or a number I could not confirm. Check before building on it.
- ❓ **Open question** — needs a decision or information from you / your boss.

---

## 1. Goal and the three data domains

We want to answer *"what is being sold in Spain, where, at what price, and is the marketing spend justified?"*

| # | Domain | Question it answers | Source |
|---|--------|---------------------|--------|
| A | **Registrations** (volume) | How many cars/which brands & models are registered, where (province/municipality), when, fuel type, new vs used, company vs private | DGT open data (microdata) |
| B | **Marketing spend** (cost) | How much is spent on campaigns, by whom, on which brand/model/channel | Ad platform APIs (own spend), Infoadex / estimators (competitor spend) |
| C | **Listings & prices** (price range) | Asking-price ranges, stock, time on market, depreciation | Apify scrapers (Wallapop, coches.net, others) — already set up by your boss |

The value is in **joining** them at `brand × model × province × month`:
- A + C → price vs volume, used-market depth per model, depreciation curves.
- A + B → marketing efficiency (spend per registration), over/under-performing models.
- A + B + C → margin proxy: does heavy marketing sustain price levels or just volume?

---

## 2. Executive summary (read this first)

1. **The DGT data is not xlsx/csv.** ✅ It is published as **fixed-width `.txt` inside `.zip` files** (ISO-8859-1, LF line endings, **714 characters per record, 69 fields**). Daily and monthly versions exist. There is also *bajas* (de-registrations), *transferencias* (ownership transfers) and *parque* (total fleet) microdata. Licence is open (CC BY 4.0 via datos.gob.es; check the legal notice).
2. **Since 2025-02-01 the VIN (bastidor) is truncated** ✅ (only the first 8 characters are usable; the rest is `*`). Full VIN needs a "legitimate interest" request. **Consequence: there is no unique vehicle key** — dedupe must use a row hash, not the VIN.
3. **Marketing spend by competitors is not freely available.** ✅ Meta's Ad Library API returns **no spend for commercial ads** (only EU reach; spend ranges only for political/issue ads). Real numbers exist only for **your own accounts** (Meta/Google/TikTok APIs). Competitor spend = paid (Infoadex) or estimated (third-party tools). This changes what question B can honestly answer — see [§6](#6-domain-b--marketing-spend).
4. **Recommended stack (matches your team notes):** private GitHub repo + **PostgreSQL on Supabase** + **GitHub Actions** as scheduler/ETL runner. BigQuery is a good later upgrade if volume/analytics outgrow Postgres. Azure Data Factory is overkill for this size.
5. **Storage size will exceed Supabase's free tier** ⚠️ (see [§4.3](#43-volume-estimate)). Plan for the Pro plan, or keep only curated columns.
6. **"Delta model"**: I read this as *incremental, append-only loading with idempotent upserts* (not necessarily Databricks Delta Lake). Design in [§5.3](#53-incremental-loading-strategy). ❓ Confirm.

---

## 3. Team notes (translated / structured)

Your notes (Spanish) turned into a checklist for this doc:

| Note | Where it's addressed |
|------|----------------------|
| Documentar el proceso, fuentes, tipología de datos, resultados | §5–§9 (sources, data types), §10 (use cases / results) |
| Currar en un repo privado de GitHub | §11 (repo layout) |
| Dónde guardarlos (SQL básicas) | §4, §5 (schemas + basic SQL) |
| Levantar una base de datos PostgreSQL | §4.2, §11 |
| Pasar la base (Supabase, GitHub Actions, "set functions" limitados) | §4.2 (Supabase limits), §11 (Actions). ⚠️ "set functions limitados" — I interpret as *Supabase Edge Functions / DB functions have limits*; see §4.2. ❓ confirm what was meant |
| Dónde guardar los datos, precios, modelo de tablas | §5, §7 |

---

## 4. Architecture

### 4.1 Layers (medallion-style, in one Postgres DB using schemas)

```
 SOURCES                  RAW (bronze)          CLEAN (silver)         MODEL (gold)
 ─────────                ────────────          ──────────────         ────────────
 DGT ZIP/TXT  ──►  raw.dgt_matriculaciones ─►  stg.registrations ─►  core.fact_registration
 Ad APIs      ──►  raw.ad_spend_*          ─►  stg.ad_spend      ─►  core.fact_ad_spend_daily      ─► marts.* (views /
 Apify JSON   ──►  raw.apify_listing       ─►  stg.listing       ─►  core.fact_listing_snapshot        materialized views)
                                                                     core.dim_* (brand, model, geo, date, channel…)
```

- **raw**: exactly what we downloaded, all `TEXT`/`JSONB`, plus `source_file`, `ingested_at`, `row_hash`. Never edited. Lets us re-run cleaning any time.
- **stg**: typed, trimmed, decoded, validated.
- **core**: star schema (facts + dimensions) — the stable model.
- **marts**: pre-joined views for dashboards / analysis.

### 4.2 Platform options

| Option | Fit | Pros | Cons |
|--------|-----|------|------|
| **Supabase (Postgres) + GitHub Actions** ✅ recommended for MVP | Matches team notes | Cheap, plain SQL, managed Postgres, easy dashboards (Metabase/Superset/Grafana), row-level security if needed | Storage caps (free tier ⚠️ ~500 MB DB — verify current limits), not a columnar warehouse, Actions job limits (6 h/job, ⚠️ verify), **Edge Functions are not the place for heavy ETL** (short execution limits) — run ETL in Actions/Python instead |
| **BigQuery** | Best for analytics scale | Serverless, columnar, cheap for this volume, partition/cluster, native scheduled queries | Another cloud account; less "transactional"; team notes prefer Postgres |
| **Azure Data Factory (+ Azure SQL / Fabric)** | Enterprise orchestration | Good connectors, monitoring | Heavy/expensive for a few files a day; overkill now |
| **Self-hosted Postgres (Docker, VPS)** | Full control | No storage caps | You maintain backups/security |

**Recommendation:** Start with **Supabase Postgres + GitHub Actions (Python + SQL)**. Keep SQL portable so a later move to BigQuery is a schema-translation job, not a rewrite. ❓ Is there a company cloud already (GCP/Azure)? That could tip the decision.

### 4.3 Volume estimate ⚠️

The DGT file covers **all vehicle types** (cars, motorcycles, trucks, etc.).
- Rows: I estimate roughly **1.5–2 M registrations/year** ⚠️ (Spain registers ~1 M new passenger cars/yr alone; the file also has motorcycles, vans, trucks, used imports). Verify against one real month.
- Raw width: 714 bytes/row → ~1–1.4 GB/yr uncompressed as text.
- If we keep ~25 curated columns in typed form: ~150–250 bytes/row → **~0.3–0.5 GB/yr** plus indexes.
- Apify listings: daily snapshots multiply quickly (e.g. 100k active listings × 365 days = 36 M rows/yr). Store **changes only** (see §7) or partition/purge raw JSON after N days.

→ Supabase free tier will not hold raw + history. Either upgrade, or keep raw files in object storage (Supabase Storage / S3 / GCS) and only load curated columns.

---

## 5. Domain A — DGT registrations

### 5.1 Where the data is ✅

| Dataset | Frequency | Contents | Catalogue page |
|---------|-----------|----------|----------------|
| **Microdatos de Matriculaciones (diarios)** | Daily (page shows last update 2026-09-27) | Vehicles registered that day + technical characteristics | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Matriculaciones-de-Vehiculos-diarios/) |
| **Microdatos de Matriculaciones (mensual)** | Monthly (last update 2026-08-31) | Same structure, monthly file | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Matriculaciones-de-Vehiculos-mensual/) · [datos.gob.es](https://datos.gob.es/en/catalogo/e00130502-microdatos-de-matriculaciones-de-vehiculos-mensual) |
| Microdatos de **Bajas** (mensual) | Monthly | De-registrations (scrapped/exported) | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Bajas-de-Vehiculos-mensual/) |
| Microdatos de **Transferencias** (mensual) | Monthly | Ownership transfers → **proxy for used-car sales** | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Transferencias-de-Vehiculos-mensual/) |
| Microdatos de **Parque** (mensual / anual) | Monthly / yearly | Total vehicle fleet stock, national & provincial | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-parque-de-vehiculos-mensual/) |
| Microdatos de **Distintivo Ambiental** (diarios) | Daily | Environmental label (0/ECO/B/C) per vehicle | [DGT page](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Distintivo-Ambiental-de-Vehiculos-diarios/) |

- Files are reached through the **"Acceso a listados"** link on each page. ✅ **Verified URL pattern (daily):** listing page `https://www.dgt.es/menusecundario/dgt-en-cifras/matraba-listados/matriculaciones-automoviles-diario.html` links to `https://www.dgt.es/microdatos/salida/YYYY/M/vehiculos/matriculaciones/export_mat_YYYYMMDD.zip` (≈0.9 MB zipped, ≈6.7 MB text, ≈9k rows per working day; weekends/holidays are empty). The monthly listing page did not respond during testing. Note: dgt.es timed out for some custom User-Agent strings but not for the Python default. The community project [rivasjm/matriculas-dgt](https://github.com/rivasjm/matriculas-dgt) scrapes this listing and downloads only missing daily ZIPs — a good reference implementation.
- **Bajas and Transferencias:** I have confirmed the pages exist, but **not their record layouts**. Each has its own "diseño de registro" PDF; read it before modelling them.
- **Licence:** datos.gob.es cites its legal notice; the community mirror states CC BY 4.0. ⚠️ Confirm on the DGT page and **credit DGT** in outputs.
- **Record layout PDF (official):** [`MATRICULACIONES_MATRABA.pdf`](https://sedeapl.dgt.gob.es/IEST_INTER/pdfs/disenoRegistro/vehiculos/matriculaciones/MATRICULACIONES_MATRABA.pdf). I could not machine-read it (image-based PDF); the field list below comes from the community project and **must be checked against the PDF**, especially the **code lists** (fuel, service, body, class), which I have *not* verified.

### 5.2 File format ✅

| Property | Value |
|----------|-------|
| Container | `.zip` containing `.txt` |
| Layout | **Fixed-width**, no delimiter; each field has a fixed start/length |
| Encoding | ISO-8859-1 (Latin-1) — must be decoded explicitly or `ñ`/accents break |
| Line ending | LF |
| Record length | 714 characters, 69 fields |
| Header | First line is a text banner ("Vehículos matriculados. Letras de la serie…"), **not** column names → skip |
| Dates | `DDMMYYYY` (8 chars) |
| VIN | `BASTIDOR_ITV` — only the first ~9–11 chars are visible, the rest is `*` (policy since 2025-02-01; ✅ verified on a real file) |

#### Field layout (69 fields, in file order, lengths in chars) ✅

| # | Field | Len | # | Field | Len |
|---|-------|----:|---|-------|----:|
| 1 | FEC_MATRICULA | 8 | 36 | COD_TUTELA | 1 |
| 2 | COD_CLASE_MAT | 1 | 37 | COD_POSESION | 1 |
| 3 | FEC_TRAMITACION | 8 | 38 | IND_BAJA_DEF | 1 |
| 4 | MARCA_ITV | 30 | 39 | IND_BAJA_TEMP | 1 |
| 5 | MODELO_ITV | 22 | 40 | IND_SUSTRACCION | 1 |
| 6 | COD_PROCEDENCIA_ITV | 1 | 41 | BAJA_TELEMATICA | 11 |
| 7 | BASTIDOR_ITV | 21 | 42 | TIPO_ITV | 25 |
| 8 | COD_TIPO | 2 | 43 | VARIANTE_ITV | 25 |
| 9 | COD_PROPULSION_ITV | 1 | 44 | VERSION_ITV | 35 |
| 10 | CILINDRADA_ITV | 5 | 45 | FABRICANTE_ITV | 70 |
| 11 | POTENCIA_ITV | 6 | 46 | MASA_ORDEN_MARCHA_ITV | 6 |
| 12 | TARA | 6 | 47 | MASA_MAXIMA_TECNICA_ADMISIBLE_ITV | 6 |
| 13 | PESO_MAX | 6 | 48 | CATEGORIA_HOMOLOGACION_EUROPEA_ITV | 4 |
| 14 | NUM_PLAZAS | 3 | 49 | CARROCERIA | 4 |
| 15 | IND_PRECINTO | 2 | 50 | PLAZAS_PIE | 3 |
| 16 | IND_EMBARGO | 2 | 51 | NIVEL_EMISIONES_EURO_ITV | 8 |
| 17 | NUM_TRANSMISIONES | 2 | 52 | CONSUMO_WH/KM_ITV | 4 |
| 18 | NUM_TITULARES | 2 | 53 | CLASIFICACION_REGLAMENTO_VEHICULOS_ITV | 4 |
| 19 | LOCALIDAD_VEHICULO | 24 | 54 | CATEGORIA_VEHICULO_ELECTRICO | 4 |
| 20 | COD_PROVINCIA_VEH | 2 | 55 | AUTONOMIA_VEHICULO_ELECTRICO | 6 |
| 21 | COD_PROVINCIA_MAT | 2 | 56 | MARCA_VEHICULO_BASE | 30 |
| 22 | CLAVE_TRAMITE | 1 | 57 | FABRICANTE_VEHICULO_BASE | 50 |
| 23 | FEC_TRAMITE | 8 | 58 | TIPO_VEHICULO_BASE | 35 |
| 24 | CODIGO_POSTAL | 5 | 59 | VARIANTE_VEHICULO_BASE | 25 |
| 25 | FEC_PRIM_MATRICULACION | 8 | 60 | VERSION_VEHICULO_BASE | 35 |
| 26 | IND_NUEVO_USADO | 1 | 61 | DISTANCIA_EJES_12_ITV | 4 |
| 27 | PERSONA_FISICA_JURIDICA | 1 | 62 | VIA_ANTERIOR_ITV | 4 |
| 28 | CODIGO_ITV | 9 | 63 | VIA_POSTERIOR_ITV | 4 |
| 29 | SERVICIO | 3 | 64 | TIPO_ALIMENTACION_ITV | 1 |
| 30 | COD_MUNICIPIO_INE_VEH | 5 | 65 | CONTRASENA_HOMOLOGACION_ITV | 25 |
| 31 | MUNICIPIO | 30 | 66 | ECO_INNOVACION_ITV | 1 |
| 32 | KW_ITV | 7 | 67 | REDUCCION_ECO_ITV | 4 |
| 33 | NUM_PLAZAS_MAX | 3 | 68 | CODIGO_ECO_ITV | 25 |
| 34 | CO2_ITV | 5 | 69 | FEC_PROCESO | 8 |
| 35 | RENTING | 1 | | **Sum of lengths** | **714** |

**Fields we actually need for the analytics (~25):** FEC_MATRICULA, FEC_PRIM_MATRICULACION, IND_NUEVO_USADO, MARCA_ITV, MODELO_ITV, VERSION_ITV, TIPO_ITV/VARIANTE_ITV, FABRICANTE_ITV, COD_TIPO, COD_PROPULSION_ITV, CILINDRADA_ITV, KW_ITV, CO2_ITV, NIVEL_EMISIONES_EURO_ITV, CARROCERIA, NUM_PLAZAS, COD_PROVINCIA_VEH, COD_PROVINCIA_MAT, COD_MUNICIPIO_INE_VEH, MUNICIPIO, CODIGO_POSTAL, PERSONA_FISICA_JURIDICA, SERVICIO, RENTING, COD_PROCEDENCIA_ITV, CATEGORIA_VEHICULO_ELECTRICO, AUTONOMIA_VEHICULO_ELECTRICO, FEC_PROCESO. Keep the other fields only in the raw layer.

### 5.3 Incremental loading strategy

Because files are immutable per day/month, an **append + idempotent upsert** pattern works:

1. **Discover**: scrape/list the DGT listing page → set of available files with names + (if present) sizes/dates.
2. **Diff** against `meta.ingest_log` (file name + checksum) → download only new files.
3. **Parse** fixed-width → rows, with `source_file`, `source_line_no`.
4. **Row hash**: `row_hash = sha256(full 714-char line)`. This is the idempotency key because the VIN is truncated and there is no natural unique ID.
5. **Load raw**: `INSERT … ON CONFLICT (row_hash) DO NOTHING`.
6. **Transform** new rows into `stg` → `core` (dimension lookups, typed values).
7. **Log** the run: rows read/inserted/rejected, duration.

Notes:
- **Daily vs monthly overlap:** both describe the same registrations. Plan: use **daily for freshness**, and **monthly for historical backfill and reconciliation**. ⚠️ Whether a daily row and its monthly twin are byte-identical (e.g. `FEC_PROCESO`) is unverified — test on one month before assuming the hash dedupes across the two.
- **Late/corrected data:** re-loading a month must not double count → the row-hash key protects us; add a reconciliation query (row count per month vs. source).
- "Delta" semantic: if you want true *Delta Lake* (Databricks/Fabric), the same steps apply with `MERGE INTO`. On Postgres, `ON CONFLICT` is the equivalent. ❓

### 5.4 Cleaning rules (ETL)

| Issue | Rule |
|-------|------|
| Encoding | Decode ISO-8859-1 explicitly; store UTF-8 |
| Banner line | Drop first line; validate every other line has length 714, else send to `rejects` |
| Padding | `TRIM` all text fields; empty string → `NULL` |
| Dates `DDMMYYYY` | Parse to `DATE`; `00000000`/invalid → `NULL`; reject impossible dates |
| Numeric fields (`CILINDRADA`, `KW`, `CO2`, …) | Cast; blanks → `NULL`; sanity ranges (e.g. `CO2` 0–600, `KW` 0–1000) → flag outliers, don't delete |
| VIN | Keep first 8 chars only in a `vin_prefix` column; **do not** use as a key; never store `*` padding |
| Brand / model text | Free text with variants (`VOLKSWAGEN`, `VW`, `SEAT`, `CUPRA`, model strings with suffixes). Build `dim_brand` + `brand_alias` and `dim_model` + `model_alias` tables; normalise to `UPPER(TRIM(unaccent(...)))`, map aliases manually, log unmapped values for review |
| Version strings | Keep raw `VERSION_ITV` for detail; derive `model_family` (e.g. "Corolla") separately from marketing model names — expect mismatches with marketing/listing names |
| Provinces | ✅ Verified on real data: `COD_PROVINCIA_*` are **licence-plate letters** (`M`, `B`, `MA`, `IB`, `OU`…), *not* INE numbers. Map letters → province name; join municipality via the 5-digit INE code (`COD_MUNICIPIO_INE_VEH`) |
| `COD_PROVINCIA_VEH` vs `COD_PROVINCIA_MAT` | Two different provinces (vehicle location vs registration office). ⚠️ **Measured on 8 days of real data: 95% of renting-fleet registrations are in Madrid**, so Madrid is 35% of new cars but 20% once `RENTING='S'` is excluded. "Where sold" analysis must exclude or separate renting |
| New vs used | `IND_NUEVO_USADO` distinguishes new registrations from used vehicles registered (mostly imports). Analytics on "sales of new cars" must filter on it |
| Company / rental / fleet registrations | `PERSONA_FISICA_JURIDICA`, `RENTING`, `SERVICIO` let us separate private buyers from companies, renting and rent-a-car. ⚠️ Tactical "self-registrations" (km 0) inflate a brand's *sales* — flag them where detectable |
| Codes (fuel, body, service, class) | Load code-list tables from the official PDF into `ref.*`; do **not** guess mappings |
| Vehicle types | File includes motorcycles, trucks, etc. Add `vehicle_segment` (car/SUV/van/moto/truck…) from `COD_TIPO`/`CARROCERIA` mapping; filter for cars in most marts |
| Duplicates | Exact-line duplicates → the hash key; near-duplicates reported by check query (same prefix + date + model + province) but kept |
| Schema drift | Store layout version; validation test fails the pipeline if field boundaries produce implausible values (e.g. dates unparseable > 1%) |

### 5.5 Tables (PostgreSQL)

```sql
create schema if not exists raw;  create schema if not exists stg;
create schema if not exists core; create schema if not exists ref;
create schema if not exists meta; create schema if not exists marts;

-- ── Ingest bookkeeping ────────────────────────────────────────────────
create table meta.ingest_log (
  id            bigserial primary key,
  source        text not null,               -- 'dgt_matr_daily' | 'dgt_matr_monthly' | 'apify_wallapop' ...
  file_name     text not null,
  file_sha256   text,
  rows_read     int, rows_inserted int, rows_rejected int,
  started_at    timestamptz default now(),
  finished_at   timestamptz,
  status        text check (status in ('running','ok','failed')),
  unique (source, file_name)
);

-- ── RAW: DGT (text only, all 69 fields) ───────────────────────────────
create table raw.dgt_matriculaciones (
  row_hash      text primary key,            -- sha256 of full 714-char line
  source_file   text not null,
  source_line   int,
  ingested_at   timestamptz default now(),
  fec_matricula text, cod_clase_mat text, fec_tramitacion text,
  marca_itv text, modelo_itv text, cod_procedencia_itv text, bastidor_itv text,
  -- … remaining fields, one text column per DGT field (see §5.2) …
  fec_proceso   text
);

-- ── REF / DIMENSIONS ──────────────────────────────────────────────────
create table core.dim_date (
  date_id date primary key, year int, quarter int, month int, week int,
  day_of_week int, is_weekend bool, is_holiday_es bool
);

create table core.dim_geo (                  -- INE municipality grain
  municipality_ine char(5) primary key,
  municipality_name text,
  province_code     char(2) not null,
  province_name     text,
  autonomous_community text
);

create table core.dim_brand (
  brand_id   serial primary key,
  brand_name text unique not null,           -- canonical: 'TOYOTA'
  group_name text                            -- 'STELLANTIS', 'VOLKSWAGEN GROUP'…
);
create table ref.brand_alias (
  alias text primary key, brand_id int references core.dim_brand
);

create table core.dim_model (
  model_id    serial primary key,
  brand_id    int references core.dim_brand,
  model_name  text not null,                 -- canonical: 'COROLLA'
  segment     text,                          -- 'B','C','SUV-C'…
  unique (brand_id, model_name)
);
create table ref.model_alias (
  brand_id int, alias text, model_id int references core.dim_model,
  primary key (brand_id, alias)
);

create table core.dim_powertrain (
  powertrain_id serial primary key,
  dgt_code text unique,                      -- COD_PROPULSION_ITV (load from official PDF)
  fuel_group text                            -- 'GASOLINE','DIESEL','BEV','PHEV','HEV','LPG/CNG'…
);

-- ── FACT ──────────────────────────────────────────────────────────────
create table core.fact_registration (
  registration_id  bigserial primary key,
  row_hash         text unique not null references raw.dgt_matriculaciones,
  registration_date date not null references core.dim_date,
  first_registration_date date,
  is_new           boolean,                  -- IND_NUEVO_USADO
  brand_id         int references core.dim_brand,
  model_id         int references core.dim_model,
  version_raw      text,
  powertrain_id    int references core.dim_powertrain,
  municipality_ine char(5) references core.dim_geo,
  province_code_veh char(2), province_code_reg char(2),
  postal_code      char(5),
  buyer_type       text,                     -- PERSONA_FISICA_JURIDICA
  is_renting       boolean, service_code text,
  vehicle_segment  text,
  engine_cc int, power_kw numeric, co2_gkm int, euro_norm text,
  ev_range_km int, seats int,
  vin_prefix       char(8)                   -- truncated VIN, NOT a key
);
create index on core.fact_registration (registration_date);
create index on core.fact_registration (brand_id, model_id, registration_date);
create index on core.fact_registration (province_code_veh, registration_date);
```
> For volume, consider partitioning `fact_registration` by `registration_date` (yearly range partitions) once beyond ~10 M rows. In BigQuery: partition by `registration_date`, cluster by `brand_id, model_id, province_code_veh`.

---

## 6. Domain B — Marketing spend

### 6.1 The honest constraint ⚠️

"How much is spent on campaigns and by whom" has **two very different answers**:

| Who spent it | Data available | Accuracy |
|--------------|----------------|----------|
| **Our own company / clients** | Meta Marketing API, Google Ads API, TikTok Ads API, etc. — exact spend, impressions, clicks, campaign & ad-set structure | **Exact** |
| **Competitors / brands like Toyota** | **No free exact data.** Options: Infoadex (paid, media-level investment estimates by advertiser & sector, annual/periodic), IAB Spain digital observatory (aggregate, not per brand), 3rd-party estimators (Similarweb, SEMrush, Adbeat, Pathmatics — estimates), Meta Ad Library (EU: ads + **reach**, no spend for commercial ads) and Google Ads Transparency Center (creatives, no spend) | **Estimates / proxies** |

Proxies that are free and legitimate: **count of active ads / creatives per brand & model** from Meta Ad Library, plus **EU total reach** per ad. That measures *marketing intensity* (share of voice), not euros. It can still support "heavy marketing vs registrations" comparisons if we are transparent that it is a proxy.

❓ **Key question for you/boss:** is Domain B about *our own clients' campaigns* (exact data, feasible now) or *competitor brands in the market* (estimates/paid data)? This decides the entire data model's `source_type` and budget.

### 6.2 Candidate sources

| Source | Gives | Cost | Notes |
|--------|-------|------|-------|
| Meta Marketing API | Own spend/impressions/clicks by campaign/adset/ad | Free | Needs Business Manager access |
| Google Ads API | Same | Free | Developer token |
| TikTok / YouTube / LinkedIn Ads APIs | Same | Free | If used |
| [Meta Ad Library](https://www.facebook.com/ads/library/) / API | Competitor ad creatives, start dates, EU reach; **no commercial spend** | Free | Identity verification needed for API |
| Google Ads Transparency Center | Competitor creatives | Free | No spend |
| [Infoadex](https://www.infoadex.es/es) | Estimated investment by advertiser/brand/media in Spain | Paid | Aggregated studies public; granular data on sale |
| Similarweb / Semrush / Pathmatics | Digital ad spend estimates | Paid | Estimates, not audited |
| ANFAC / ACEA | Registrations (context) | Free | PDF/press, see §8 |

### 6.3 Tables

```sql
create table core.dim_advertiser (
  advertiser_id serial primary key,
  name text not null, type text,             -- 'own','client','competitor'
  brand_id int references core.dim_brand     -- nullable: agencies/dealers
);
create table core.dim_channel (
  channel_id serial primary key,
  platform text,                             -- 'meta','google','tiktok','tv','radio'...
  channel_group text                         -- 'social','search','display','video','offline'
);
create table core.dim_campaign (
  campaign_id bigserial primary key,
  advertiser_id int references core.dim_advertiser,
  channel_id int references core.dim_channel,
  external_id text, name text,
  brand_id int references core.dim_brand,
  model_id int references core.dim_model,    -- nullable: brand-level campaigns
  objective text, geo_target text,           -- province codes / 'ES'
  start_date date, end_date date,
  unique (channel_id, external_id)
);
create table core.fact_ad_spend_daily (
  date_id date references core.dim_date,
  campaign_id bigint references core.dim_campaign,
  spend_eur numeric(14,2),
  impressions bigint, clicks bigint, reach bigint,
  conversions numeric,                        -- leads / test drive requests
  source_type text check (source_type in ('first_party','third_party_estimate','infoadex','proxy_ad_count')),
  currency_original text, fx_rate numeric,
  primary key (date_id, campaign_id, source_type)
);
```

**Campaign → model mapping** is the hard part: campaign names are free text. Options: enforce a UTM/naming convention (`brand_model_objective_geo_yyyymm`) for own campaigns; for external data, a manual/LLM-assisted mapping table reviewed by a human.

**Attribution caveat:** we cannot claim "campaign X caused N registrations" from public data. We compare **spend/intensity vs registrations at brand×model×month(×province)** and look at lagged correlation (0–3 months). That shows association, not causation (see §10 limitations).

---

## 7. Domain C — Listings & prices (Apify)

### 7.1 What exists ✅/⚠️

Your boss connected Apify actors for Wallapop, coches.net and others. Public Apify actors for these portals return (per their store pages) price, city, seller type, images, URL, mileage, fuel, specs, seller info and — for Wallapop — a raw `type_attributes` dict. Sources: [Wallapop scraper](https://apify.com/rastriq/wallapop-scraper), [Coches.net actor](https://apify.com/corpusculus/coches-net-actor/api/openapi).

⚠️ **I don't know which actors your boss uses or their exact output schema.** ❓ **Action: export one sample dataset per actor (JSON) and share it** so the staging schema is built on real fields, not assumptions.

### 7.2 Storage design

Scraped listings are semi-structured and portal-specific → store **raw JSON as-is** (`JSONB`), then normalise.

```sql
create schema if not exists raw; -- already exists

create table raw.apify_listing (
  id           bigserial primary key,
  actor_id     text, run_id text, dataset_id text,
  portal       text,                         -- 'wallapop','coches.net',...
  scraped_at   timestamptz not null,
  external_id  text,                         -- portal's listing id
  payload      jsonb not null,               -- full item
  payload_hash text,
  unique (portal, external_id, payload_hash)
);

create table core.listing (                  -- one row per listing (SCD-ish)
  listing_id    bigserial primary key,
  portal        text not null, external_id text not null,
  url text, title text,
  brand_id int, model_id int, version_raw text,
  year int, km int, fuel_group text, gearbox text, power_cv int,
  body_type text, colour text,
  province_code char(2), municipality_ine char(5), postal_code text,
  seller_type text,                          -- 'private','dealer'
  first_seen date not null, last_seen date not null,
  is_active boolean,
  unique (portal, external_id)
);

create table core.fact_listing_price (       -- change history (append only when price changes)
  listing_id bigint references core.listing,
  observed_at date not null,
  price_eur numeric(12,2),
  primary key (listing_id, observed_at)
);
```

Storing **changes only** (new listing, price change, disappearance) keeps size manageable and directly gives:
- **Time on market** = `last_seen - first_seen` (a *proxy* for sold, since scraping cannot confirm a sale — a removed ad may be unsold or withdrawn).
- **Price drops** per model over time.

### 7.3 Cleaning rules

- Price: parse strings ("12.500 €"), drop `0`/`1`/placeholder prices and "consult" ads; flag outliers per brand/model/year using IQR, keep the flag.
- Km, year: validate (`year` between 1990 and current+1; `km` ≥ 0, cap at e.g. 1,000,000).
- Same car appearing on several portals or being reposted: fuzzy key `(brand, model, year, km±1%, province, price±5%)` → `listing_group_id`. Never delete duplicates automatically; flag them.
- Dealers vs private: keep `seller_type`; dealer inventory behaves very differently.
- Brand/model: reuse `ref.brand_alias` / `ref.model_alias` from §5.
- Currency/tax: some portals show prices incl. IVA, financed prices, or "desde" prices — record `price_type` if available.
- **Personal data:** seller names, phones, or profile IDs are personal data (GDPR). Don't store what we don't need; hash or drop seller identifiers.
- **Terms of service:** scraping Wallapop/coches.net may conflict with their ToS. ❓ Have legal/compliance confirm the acceptable use before any external publication.

---

## 8. Other supporting sources (optional, phase 2)

| Source | Use |
|--------|-----|
| [ANFAC](https://anfac.com/cifras-clave/matriculaciones-turismos-y-todoterreno/) | Monthly registrations by brand/model — use as **validation** of DGT aggregates |
| ACEA | Europe-wide registrations (context) |
| INE | Municipality codes, population, income → per-capita and affluence analysis |
| AEAT / IDAE incentives (MOVES) | Explain EV registration spikes |
| Macro (fuel prices, Euribor) | Covariates for price/volume models |

---

## 9. Data quality & monitoring

- Row counts per file vs. sum in `raw`; monthly totals vs. ANFAC (tolerance, e.g. ±2% ⚠️ arbitrary).
- % unmapped brands/models (target < 1% of rows, e.g. review queue).
- Null-rate and range checks per column, per load (fail the run above thresholds).
- Freshness: alert if no new daily DGT file for > 3 days (weekends/holidays exempt).
- Apify: alert on runs with 0 items or big drops in items per portal.
- Store test results in `meta.dq_result`; surface in a simple dashboard.

---

## 10. Use cases & example queries

| # | Use case | Data | Output |
|---|----------|------|--------|
| U1 | **Market share by brand/model** by month, province, fuel | A | Ranking, trend |
| U2 | **Where do models sell?** (province/municipality heat maps) | A + dim_geo | Regional index vs national share |
| U3 | **Electrification progress** (BEV/PHEV share by province) | A | EV penetration map |
| U4 | **New vs used dynamics** | A (+ transferencias) | Used market volume (transfers) per model |
| U5 | **Channel mix**: private vs company vs rent-a-car vs renting | A | Tactical-registration detection |
| U6 | **Price range per model** (median, P10–P90 by year/km/province) | C | Price bands |
| U7 | **Depreciation curve** (asking price vs age/km) | C (+A for launch date) | Residual-value curves |
| U8 | **Time on market** per model/price band | C | Demand/liquidity indicator |
| U9 | **Marketing efficiency**: spend (or ad intensity) per registration | A + B | Cost per registration by brand/model |
| U10 | **Over/under-performers**: heavily marketed models with weak sales vs light-marketing models with strong sales | A + B | Quadrant chart (spend vs volume) |
| U11 | **Margin proxy**: price level (C) + volume (A) vs spend (B) | A + B + C | Estimated revenue proxy ÷ marketing cost |
| U12 | **Lead indicators**: do listings/price drops precede registrations? | A + C | Early-warning signals |

### Example SQL

**U1 – Monthly market share of new passenger cars (top brands)**
```sql
select date_trunc('month', f.registration_date) as month,
       b.brand_name,
       count(*) as units,
       round(100.0 * count(*) / sum(count(*)) over (partition by date_trunc('month', f.registration_date)), 2) as share_pct
from core.fact_registration f
join core.dim_brand b using (brand_id)
where f.is_new and f.vehicle_segment = 'car'
group by 1,2
order by 1, units desc;
```

**U3 – EV share by province**
```sql
select f.province_code_veh,
       100.0 * count(*) filter (where p.fuel_group in ('BEV','PHEV')) / count(*) as ev_share_pct
from core.fact_registration f
join core.dim_powertrain p using (powertrain_id)
where f.is_new and f.registration_date >= date_trunc('year', current_date)
group by 1 order by 2 desc;
```

**U9/U10 – Spend vs registrations (model-month)**
```sql
with reg as (
  select model_id, date_trunc('month', registration_date)::date m, count(*) units
  from core.fact_registration where is_new group by 1,2),
spend as (
  select c.model_id, date_trunc('month', s.date_id)::date m, sum(s.spend_eur) eur
  from core.fact_ad_spend_daily s join core.dim_campaign c using (campaign_id)
  where s.source_type = 'first_party' group by 1,2)
select r.model_id, r.m, r.units, coalesce(s.eur,0) spend_eur,
       case when r.units>0 then s.eur / r.units end as spend_per_registration
from reg r left join spend s using (model_id, m);
```

**U6 – Price bands per model/year**
```sql
select b.brand_name, m.model_name, l.year,
       percentile_cont(0.1) within group (order by p.price_eur) p10,
       percentile_cont(0.5) within group (order by p.price_eur) median,
       percentile_cont(0.9) within group (order by p.price_eur) p90,
       count(*) n
from core.listing l
join lateral (select price_eur from core.fact_listing_price where listing_id=l.listing_id order by observed_at desc limit 1) p on true
join core.dim_brand b using (brand_id) join core.dim_model m using (model_id)
where l.is_active
group by 1,2,3 having count(*) >= 20;
```

### Limitations to state in every report
- Registration ≠ retail sale (dealer self-registrations, exports, rental fleets, timing lag between sale and registration).
- Listings measure **asking** prices, not transaction prices.
- Time on market is a proxy for sales, not proof.
- Public spend data for competitors is estimated or absent.
- Association (spend vs volume) ≠ causation: price, stock availability, incentives (MOVES), fuel-tax rules, launches and seasonality all confound.
- A Toyota-style hypothesis ("massive marketing → volume") needs controls (segment, price, hybrid demand). Compare within segment and consider per-price-band.

---

## 11. Repository & pipelines

### 11.1 Private GitHub repo layout (proposal)

```
mvp_intelligence_conglomerate/
├─ docs/
│  ├─ data-platform-plan.md        ← this file
│  ├─ data-dictionary.md           (generated from DB comments)
│  └─ decisions/                   (ADRs)
├─ db/
│  ├─ migrations/                  (numbered .sql: 001_schemas.sql, 002_raw_dgt.sql …)
│  └─ seeds/                       (ref data: provinces, brand aliases, DGT code lists)
├─ etl/
│  ├─ dgt/        discover.py · download.py · parse_fixed_width.py · load.py
│  ├─ marketing/  meta_ads.py · google_ads.py · ad_library.py
│  ├─ listings/   apify_pull.py · normalise.py
│  └─ common/     db.py · logging.py · hashing.py
├─ sql/           transform (stg→core), marts, data-quality checks
├─ tests/         parser tests with real sample lines, dq tests
├─ .github/workflows/
│  ├─ dgt_daily.yml       cron
│  ├─ dgt_monthly.yml     cron
│  ├─ apify_pull.yml      cron
│  └─ ci.yml              lint + tests + migration dry-run
└─ README.md
```

### 11.2 GitHub Actions
- Schedules (`on: schedule`, UTC cron): DGT daily ~ once/day after the source updates; monthly job around the 1st–5th; Apify pull per portal cadence (e.g. daily).
- Secrets: `SUPABASE_DB_URL` (use the **pooler/session** connection string ⚠️), `APIFY_TOKEN`, ad-platform tokens. Never commit them.
- Limits ⚠️ (verify current): scheduled workflows can be delayed/skipped under load and disabled after 60 days of repo inactivity on public repos; job time limit 6 h; private-repo minutes are quota-limited on free plans.
- Idempotent runs: each workflow can be re-run safely (row-hash key + ingest log).
- Failure notification: Slack/email on failed job.

### 11.3 Security & compliance
- Private repo; least-privilege DB roles (`etl_writer`, `analyst_reader`).
- Enable Supabase row-level security only on exposed schemas; do not expose `raw` via the API.
- DGT data: cite source; no VIN-level personal identification attempts. The data holds `PERSONA_FISICA_JURIDICA`, postal code and municipality — low re-identification risk in aggregate, but don't publish row-level data with small geographies.
- Apify: minimise/hash seller personal data; review portal ToS.

---

## 12. Roadmap

| Phase | Deliverable | Effort ⚠️ |
|-------|-------------|-----------|
| 0 | Decisions on open questions (§14); private repo + Supabase project | 1–2 days |
| 1 | **DGT ingestion**: parser + raw + core + 12-month backfill + daily job + DQ checks; dashboards U1–U5 | 1–2 weeks |
| 2 | **Listings**: import current Apify datasets, staging schema, price history, U6–U8 | 1 week (after sample data) |
| 3 | **Marketing**: first-party connectors (Meta/Google) + campaign→model mapping; U9–U10 | 1–2 weeks |
| 4 | Competitor intensity proxy (Ad Library) / evaluate paid sources; U11–U12 | 1–2 weeks |
| 5 | Hardening: partitions, alerts, docs, evaluate BigQuery move | ongoing |

---

## 13. Sources

- DGT – [Microdatos de Matriculaciones (diarios)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Matriculaciones-de-Vehiculos-diarios/)
- DGT – [Microdatos de Matriculaciones (mensual)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Matriculaciones-de-Vehiculos-mensual/)
- DGT – [Bajas (mensual)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Bajas-de-Vehiculos-mensual/) · [Transferencias (mensual)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Transferencias-de-Vehiculos-mensual/) · [Parque (mensual)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-parque-de-vehiculos-mensual/) · [Distintivo ambiental (diarios)](https://www.dgt.es/menusecundario/dgt-en-cifras/dgt-en-cifras-resultados/dgt-en-cifras-detalle/Microdatos-de-Distintivo-Ambiental-de-Vehiculos-diarios/)
- DGT – [Record design PDF (MATRICULACIONES_MATRABA)](https://sedeapl.dgt.gob.es/IEST_INTER/pdfs/disenoRegistro/vehiculos/matriculaciones/MATRICULACIONES_MATRABA.pdf)
- datos.gob.es – [Microdatos de Matriculaciones (mensual)](https://datos.gob.es/en/catalogo/e00130502-microdatos-de-matriculaciones-de-vehiculos-mensual) · [(diarios)](https://datos.gob.es/es/catalogo/e00130502-microdatos-de-matriculaciones-de-vehiculos-diarios)
- Community parser and field list: [rivasjm/matriculas-dgt](https://github.com/rivasjm/matriculas-dgt)
- Meta Ad Library API limits: [AdLemur](https://www.adlemur.com/research/meta-ad-library-api) · [adlibrary.com](https://adlibrary.com/posts/meta-ad-library-api-limitations) (third-party write-ups; check Meta's own docs before relying on them)
- Advertising investment: [InfoAdex](https://www.infoadex.es/es) · [Marketing Directo – top advertisers](https://www.marketingdirecto.com/anunciantes-general/publicaciones/mayores-anunciantes-inversion-publicitaria-infoadex)
- ANFAC registrations: [anfac.com](https://anfac.com/cifras-clave/matriculaciones-turismos-y-todoterreno/)
- Apify actors (examples): [Wallapop Scraper](https://apify.com/rastriq/wallapop-scraper) · [Coches.net Actor](https://apify.com/corpusculus/coches-net-actor/api/openapi) · [Wallapop Data API](https://apify.com/blackfalcondata/wallapop-scraper/api)

---

## 14. Open questions (need answers before Phase 1–3)

1. ❓ **Domain B scope:** own/client campaigns (exact) or competitor brands (estimates/paid)? Is there a budget for Infoadex or a spend-estimation tool?
2. ❓ **Platform:** Supabase Postgres confirmed? Any existing GCP/Azure commitment? Is a paid Supabase plan acceptable (data will exceed free tier)?
3. ❓ **"Delta model":** plain incremental tables (this doc) or actual Delta Lake / Databricks / Fabric?
4. ❓ **Scope of vehicles:** cars only, or motorcycles/vans/trucks too?
5. ❓ **"Where sold":** province of vehicle (`COD_PROVINCIA_VEH`) or registration office (`COD_PROVINCIA_MAT`)?
6. ❓ **Apify:** which actors/portals exactly, run cadence, and a sample JSON per actor.
7. ❓ **History depth:** how many years of DGT history to backfill?
8. ❓ **Legal:** approval for scraping portals and using the data internally vs externally.
9. ❓ "**set functions limitados**" in the team notes — Supabase Edge Function limits, or something else?
10. ❓ Do we need full VIN (would require a legitimate-interest request to DGT)? Recommended: no for MVP.

## 15. Immediate next steps
1. Answer §14 questions 1–3 and 6.
2. Download one daily and one monthly DGT ZIP; verify record length (714), the field boundaries in §5.2 and the code lists in the PDF; check daily-vs-monthly overlap.
3. Create the private repo and Supabase project; commit this document and the migration `001_schemas.sql`.
4. Build the DGT parser with unit tests using real sample lines.
