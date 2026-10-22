# Forgesmith

Asynchronous B2B lead pipeline: discover e-commerce merchants, enrich them with
contact data and business context, generate a personalized Polish pitch, and
send it at a human pace.

Status: **Phase 0 (scaffold) + Phase 1 (extraction) implemented.** Enrichment,
LLM personalization, and the mailer are designed but not wired yet — see
[Roadmap](#roadmap).

## Quick start

```powershell
uv sync
uv run playwright install chromium      # only needed for the OLX source
uv run forgesmith db init
uv run forgesmith source shopify        # probe data/shopify_seeds.txt (37 verified stores)
uv run forgesmith source olx -q meble --city warszawa --pages 1
uv run forgesmith leads
uv run forgesmith stats
```

## Commands

| Command | What it does |
|---|---|
| `forgesmith db init` | Create tables (idempotent) |
| `forgesmith source shopify [--seeds FILE ...] [--limit N]` | Probe Shopify storefronts, store catalogue metadata |
| `forgesmith source olx -q meble --city warszawa --pages 2 [--limit N] [--engine http]` | Crawl OLX listing pages into business leads |
| `forgesmith leads [--status new] [--source shopify] [--limit 50]` | Inspect stored leads |
| `forgesmith stats` | Counts by source and status |
| `forgesmith --log-level debug <command>` | Verbose run |

## Architecture

```
data/shopify_seeds.txt ──► ShopifySource ──┐
OLX listing pages ───────► OlxSource ──────┴─► LeadCandidate ──► LeadRepo
                                                                  │
                                                        (Phase 2) enrichment
                                                                  │
                                                    Lead + Contact + evidence
                                                                  │
                                              (Phase 3) Ollama pitch generation
                                                                  │
                                                              Message
                                                                  │
                                                 (Phase 4) SMTP with jitter
                                                       → Send / Suppression
```

### Why two stages

Marketplaces (OLX, Allegro) are a *lead source*, not a *contact source*: sellers
hide their email behind a contact form, and the offer page is not an outbound
target. So a lead is an identity (`name`, `city`, `category`, `domain`), and a
separate enrichment stage visits the merchant's own storefront to find a company
email and the product context the pitch needs.

### Source notes (measured, not assumed)

**Shopify** — `/products.json` returns the real catalogue, so the lead carries
product titles, types and prices for personalization. The `name` comes from
`og:site_name`/`<title>` (product `vendor` is usually the goods brand, not the
shop). `data/shopify_seeds.txt` ships 37 stores verified live; a dead or
non-Shopify domain is skipped silently, so appending candidates is safe.

**OLX** — the listing page embeds `window.__PRERENDERED_STATE__`, a
double-encoded JSON string with full ad objects. It sits behind a CloudFront WAF
that rejects plain HTTP clients (403 at any User-Agent), so the source drives
headless Chromium through `forgesmith.browser.BrowserFetcher`. Only
`isBusiness` ads are kept, and ads are keyed by **seller id**, not ad id, so a
shop posting twenty near-identical listings yields one lead. Most business ads
expose no website (`externalUrl` is null), so OLX leads land with no domain and
are resolved during enrichment.

**Allegro** — deliberately disabled: listings are behind DataDome and the ToS
forbids scraping. The source interface exists so it can be enabled later through
the official API.

### Lead model

`leads` — one row per prospect, unique on `(source, source_id)`, deduped by
`domain` (one outbound target per domain). `raw` keeps the evidence a source
collected (product sample, types, prices) for later personalization.

`contacts` · `messages` · `sends` · `suppressions` · `events` complete the
schema and are created up front so the pipeline stages can be added without
migrations churn.

## Database

Development defaults to SQLite (`data/forgesmith.db`) so the pipeline runs with
zero infrastructure. For Postgres:

```powershell
docker compose up -d db
$env:FORGESMITH_DATABASE_URL = "postgresql+asyncpg://forgesmith:forgesmith@localhost:5432/forgesmith"
uv run forgesmith db init
```

Adminer is exposed on <http://localhost:8080>.

## Configuration

Copy `.env.example` to `.env`. All keys are prefixed `FORGESMITH_`; the crawler
knobs that matter are `REQUEST_DELAY` (per-host seconds between requests,
default 1.5), `MAX_CONCURRENCY`, and `MAX_RETRIES`.

## Tests

```powershell
uv run pytest
```

Tests never touch the network: sources are exercised against HTML/JSON fixtures,
and the DB runs on a temporary SQLite file.

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 0 | Scaffold, config, schema, CLI | done |
| 1 | Extraction: Shopify + OLX | done |
| 2 | Enrichment: async Playwright, email extraction, platform detection | next |
| 3 | LLM: Ollama, JSON-schema output, Polish prompt, eval set | planned |
| 4 | Mailer: dry-run `.eml` → Gmail SMTP, jitter, caps, opt-out, IMAP bounce/reply scan | planned |
| 5 | Docker packaging + unattended scheduler | planned |

### Legal / deliverability notes

Outbound B2B email in Poland sits under RODO and the e-services act. The design
keeps to company addresses (not named individuals), always includes sender
identity plus a one-line opt-out, honours `suppressions` before every send, and
rate-limits per domain. Gmail app-password SMTP is a development crutch with low
caps and real suspension risk; the mailer defaults to `--dry-run`, which writes
`.eml` files to `data/outbox/`, and volume caps are enforced in code. Before a
real campaign, move to a dedicated domain with SPF/DKIM/DMARC.
