# Forgesmith — Phase 5: Containerization + unattended scheduler (final plan)

**Status:** implemented. This file is the historical design record from before
implementation; the README is the current source of truth.
**Goal source:** `AGENTS.md` ("Package the script in Docker to run autonomously in the
background"), `README.md` roadmap ("Docker packaging + unattended scheduler"), `phase4.md`.
**Baseline:** Phases 0-4 implemented, 278 offline tests pass, ruff clean. Phase 4.5 (IMAP
ingest) is implemented separately in `phase4.5.md`; Phase 5 does not own it.
**Plan location:** `C:\Users\Nikita\.opencode\plan\phase5.md` (move to the repo root with the
other phase docs when implementation starts).
**Debate record:** §3. Two opposing topologies, an independent repo-verification pass, and a
second-round rebuttal. The advocates converged on the same synthesis: **one-shot containers,
host scheduler, plus four pre-container correctness fixes**.

**Owner decisions (2026-10-23):**
1. Deploy target: **this Windows desktop** (Docker Desktop + WSL2), not a VPS.
2. Unattended live sending: **allowed**, but only for leads a human already approved.
3. Production database: **SQLite on a named Docker volume** (Postgres stays supported and
   CI-smoked, not the production path).

---

## 1. Goal and definition of success

Make the existing pipeline run unattended on the Windows desktop without weakening the Phase 4
safety contract:

- the four stages (`source` → `enrich` → `compose` → `send`) run on a timetable, survive
  reboots and Docker Desktop restarts, and are idempotent when a tick is missed or repeated;
- **approval stays human** — automation stops at `composed`; only an operator moves a lead to
  `queued`;
- live delivery remains gated by the approval ledger, an explicit `--live`, a real
  `mail_dry_run` kill switch, and an explicit compose profile — four independent gates;
- a container kill never double-sends and never manufactures `uncertain` rows by routine
  shutdown;
- the SQLite DB and the `.eml` evidence survive container replacement, are inspectable from
  Windows, and are backed up with a tested restore;
- the default is still zero-infrastructure SQLite, offline tests, and no new paid service.

**Explicitly out of scope:** IMAP ingest (Phase 4.5), auto-approval, auto-reply handling,
multi-host / horizontal scale, Kubernetes, a metrics stack, HTML mail, provider rotation.

---

## 2. Success criteria (measurable)

1. `docker compose run --rm app send` on a fresh DB exits 0 and writes no `.eml`.
2. A scheduled dry day completes all four stages unattended with ≥1 draft, 0 sends, and one
   `run` ledger row per invocation.
3. An allowlisted live send through the scheduler produces `sends.status=sent`, exactly one
   `.eml` in `.\outbox`, and no `uncertain` row.
4. `docker kill --signal=SIGTERM` during the inter-send jitter exits within 10 s and leaves no
   new `pending`/`uncertain` row.
5. `docker kill --signal=SIGTERM` during `DATA` exits within `stop_grace_period`, finalizes
   normally when the relay answers, and otherwise leaves exactly one `pending` row that the
   next run reports as `uncertain` — never a resend.
6. Two send containers racing cannot both reserve the same `message_id` (DB-enforced).
7. A host reboot or Docker Desktop restart resumes the timetable without manual action, and a
   missed `send` tick does **not** fire a catch-up burst.
8. All 278 existing tests still pass; the suite remains offline.
9. `forgesmith doctor` exits non-zero on a broken DB, missing sender config, or unreachable
   Ollama, and `status` shows the last result of every stage.
10. A restore from the nightly backup into a scratch project reproduces the same lead/send
    counts.

---

## 3. Debate record and decision

### Round 1

- **A — resident daemon:** one long-running container, in-process asyncio scheduler, chunked
  sends, Chromium/engine amortized, `restart: unless-stopped`, heartbeat + `doctor`.
- **B — one-shot + host scheduler:** `docker compose run --rm -T app <stage>` driven by systemd
  timers; `flock` single-flight; two image targets; approval never scheduled.
- **C — red team:** verified 16 load-bearing claims, found four facts that changed the outcome:
  1. `mail_dry_run` is **dead code** (`config.py:78`; no reader in `src/`) and
     `SendRunner.run(live=True, confirm=None)` sends unconditionally (`runner.py:114-115`).
  2. `SmtpTransport.deliver` is `asyncio.to_thread` (`transport.py:150`); cancelling the await
     does **not** stop `smtplib.data`, and `CancelledError` bypasses `except Exception`
     (`runner.py:324`). A daemon that times out/cancels a send can overlap the next one — the
     only genuine double-send vector, created by the loop, not by Docker.
  3. The file lock is **load-bearing**: `reserve` checks uniqueness in Python
     (`repo.py:1100-1123`) before inserting, and no unique constraint exists
     (`models.py:158-160`; `phase4.md:455` only promises one).
  4. `data_dir`/`database_url` default to `PROJECT_ROOT = parents[2]` (`config.py:10,22-25`);
     correct for `/app/src`, wrong for a wheel/site-packages install.

### Round 2 (rebuttals)

- A conceded the double-send vector, dropped `limit=3` and stage-timeout mitigation, proved the
  Chromium-amortization argument is only 1–6% of CPU/day, and **switched to B's topology**.
- B conceded that single-flight belongs at the scheduler, that `RandomizedDelaySec` alone would
  regress the Gaussian distribution, and that the run ledger (not journald) must hold liveness
  evidence.
- Both independently proposed the same fix to the shutdown hole: **cooperative stop at safe
  points, never cancel inside `deliver`** — which helps both topologies and is therefore a
  Phase 5 prerequisite, not a daemon detail.

**Decision: adopt B's topology with A's operational rigour.** No resident Python scheduler in
Phase 5. Revisit only if Phase 4.5 needs IMAP IDLE (sub-minute reaction), which is explicitly a
Phase 4.5 decision.

| Criterion (1–5) | A daemon | B one-shot | Chosen hybrid |
|---|---|---|---|
| Safety (no double send, no approval bypass) | 3 | 4 | **5** |
| Operational simplicity | 3 | 5 | 4 |
| Failure recovery | 3 | 5 | 4 |
| Observability | 4 | 2 | 4 |
| Resources on a small host | 3 | 5 | 5 |
| Testability | 2 | 5 | 4 |
| Time to implement | 3 | 5 | 3 |
| **Total** | 29 | **39** | 37 |

---

## 4. Verified constraints

| Piece | Verified fact | Consequence |
|---|---|---|
| `mail_dry_run` | Declared, documented, never read; CLI ignores it | Must become a real gate or be deleted; a dead safety knob is worse than none |
| Send entry | `send_queued(live=?...); confirm=None` skips the TTY prompt (`runner.py:114`) | Every new caller must pass `live` explicitly |
| Shutdown | `deliver` runs in an unjoinable thread (`transport.py:150`) | Cooperative stop only; grace > `smtp_timeout` |
| Reserve | Python uniqueness check then insert (`repo.py:1100-1123`) | Needs a DB-level guard; the file lock is the only current mutex |
| `pending` recovery | Every startup flips leftovers to `uncertain` (`repo.py:1210-1230`) | A kill burns a lead; make kills rare and safe |
| SQLite writes | Async engine, no WAL, no busy timeout (`session.py:39`) | Multiple one-shot containers can hit `database is locked`; add WAL + `busy_timeout` |
| Paths | `PROJECT_ROOT = parents[2]` (`config.py:10`) | `FORGESMITH_DATA_DIR` must be explicit in containers |
| Seeds/overrides | Default under `PROJECT_ROOT/data` (`config.py:38,55`) | Must be remapped when the app lives elsewhere |
| Timezone | `tzdata` is a dependency; `zoneinfo` drives all policy (`policy.py:69-86`) | `TZ` is cosmetic; never rely on host time for policy |
| Tests | 278, temp SQLite, offline (`tests/conftest.py`) | New tests must inject clock/sleep; CI must not need SMTP |
| Browser | Chromium only for `source olx` and `enrich`; lazy import (`browser.py:70-86`) | Keep it out of the send image |
| Browser args | No `--no-sandbox` / `--disable-dev-shm-usage` today | Needed in containers; must be env-gated so local dev is unchanged |
| Compose | Only `db` + `adminer` today; no app image | Add services under profiles so `up` cannot start anything |
| Volume I/O | Docker Desktop bind mounts use virtiofs; `flock` is unreliable there | DB + `mailer.lock` must live on a **named volume** (ext4 in the WSL2 VM) |
| Docker Desktop | Runs in the interactive user session; needs login + no sleep | The unattended story depends on auto-login and power settings; document it |
| Postgres | Dialect branches exist (`schema.py:22-31`) but zero test coverage | Keep a CI smoke so the branch does not rot; not the production path |

---

## 5. Architecture

```
              Windows Task Scheduler (single-instance policy per task)
              ─ source 06:10 / olx 06:30 / enrich 2-hourly / compose 20 min
              ─ send Mon–Fri every 20 min, 08:45–17:05  ─ backup 03:15
                                  │  ops\run.ps1 <stage>  (mutex → 75 if busy)
                                  v
        docker compose --profile app run --rm -T <service> <stage>
                                  │
   ┌──────────────────────────────┴────────────────────────────────────────────┐
   │ app (slim, no Chromium)                 app-browser (Chromium)            │
   │ source shopify · compose · send ·       source olx · enrich               │
   │ approve (manual) · status · doctor                                        │
   └───────────────┬────────────────────────────────────┬──────────────────────┘
                   │ sqlite file                        │ http
         ┌─────────▼──────────┐                 ┌───────▼──────────────┐
         │ fs-data volume     │                 │ Ollama on the host   │
         │ forgesmith.db (WAL)│                 │ host.docker.internal │
         │ mailer.lock        │                 │ :11434               │
         └─────────┬──────────┘                 └──────────────────────┘
                   │
         ┌─────────▼─────────────────────────┐
         │ bind mounts for the operator:     │
         │ .\outbox  → /app/data/outbox      │
         │ .\backups → /app/data/backups     │
         └───────────────────────────────────┘
```

**Process model.** One short-lived container per stage invocation. No supervisor inside the
app, no in-process loop, no `restart:` policy on the job container. The OS owns the clock; a
traceback costs one tick, not the pipeline.

**Why one-shot wins here**

1. Every stage is already an idempotent CLI command that drains a DB-derived queue
   (`select_queued`, `select_composable`, `enrich_pending`). Missed ticks self-heal.
2. Process death is the cheapest correct answer to the unjoinable SMTP worker thread.
3. The timetable is inspectable (`schtasks /Query`) and survives app bugs.
4. SMTP credentials exist only for the ~75 s a send tick lives, not 24 h/day.

**What we borrow from the daemon proposal:** `status`/`doctor`, JSON logs, a run ledger, a
"decide not to start" pre-check, and a pause switch. Those are cheap and topology-neutral.

---

## 6. Phase 5.0 — pre-container correctness (no Docker yet)

These fixes are prerequisites for *any* topology. They ship first, with tests, because Docker
only makes the failure modes easier to trigger.

### 6.1 Make `mail_dry_run` real (or delete it)

- Treat it as a **kill switch**, not a duplicate of `--live`:
  `live_requested and settings.mail_dry_run` → print a banner, write a `run` event with
  `stop_reason="kill_switch"`, **exit 0**, no `sends` row, no `.eml`, no SMTP connection.
- Keep the Phase 4 rule that the CLI's plain `send` is dry regardless.
- Test: `live=True` + `mail_dry_run=True` sends nothing and exits 0; the test asserts the
  setting is actually read (the current regression is that it is not).
- Alternative (only if the switch is rejected): delete the field and its docs. Never leave it
  documented as protection it does not provide.

### 6.2 Cooperative shutdown in `SendRunner`

- Add `stop: Callable[[], bool] | None` to `SendRunner.run` / `send_queued`; wire
  `SIGTERM`/`SIGINT` to a plain flag via `signal.signal` (portable to Windows; **not**
  `loop.add_signal_handler`, unsupported on the Proactor loop).
- Check `stop()` at the top of each send iteration and **never** inside
  `await transport.deliver`.
- Replace the single jitter sleep with 5 s polling chunks so a stop is honoured within 5 s;
  keep the injected `sleep` seam for tests.
- Bound the jitter sleep by the remaining `--max-runtime` budget.
- Do **not** wrap `send` in `asyncio.timeout`. Timeouts stay on `source`/`enrich`/`compose`,
  which hold no unjoinable thread.
- Worst-case shutdown = 5 s poll + `smtp_timeout` (30 s) + `quit` ≈ 40 s < `stop_grace_period`
  (90 s). `TimeoutStopSec=120s` on Linux equivalents.
- On stop, set `stop_reason="signal"` and exit 0 (not an alert).
- Tests: stop during jitter → no send; stop between messages → prefix finalized, no pending;
  fake transport that blocks inside `deliver` → reservation is finalized, never cancelled;
  jitter chunks are bounded and the fake clock advances.

### 6.3 DB-enforced single live attempt per message

- Add to `ensure_schema` (idempotent, both dialects):
  ```sql
  CREATE UNIQUE INDEX IF NOT EXISTS uq_sends_message_live
    ON sends (message_id)
    WHERE status IN ('pending','sent','uncertain','bounced');
  ```
  `failed` and `aborted` stay retryable; `bounced` is terminal; `aborted` consumed no attempt.
- Before creating it, detect existing duplicates and **fail loudly** with the offending
  `message_id`s (dev DB currently has 0 sends; a dirty DB must be resolved by a human).
- Catch the race in `reserve`: map `sqlalchemy.exc.IntegrityError` on the insert
  (`repo.py:1122-1123`) to a `ReservationConflict` / `existing_attempt` result so the loser
  exits cleanly instead of crashing.
- **No advisory lock in Phase 5.** The production DB is SQLite on one host, and SQLite
  serializes writers; the partial unique index is the real guard. Keep `RunLock` as defence in
  depth and as the human/manual guard. If Postgres ever becomes production (multi-host), add
  `pg_advisory_xact_lock` then — document that branch in `phase4.md` instead of promising it.
- Tests: second live reservation for the same message is refused after a `pending`/`sent`
  row; a `failed` row allows retry; `aborted` allows retry; duplicate detection aborts
  `ensure_schema`; `ensure_schema` twice is safe.

### 6.4 SQLite concurrency (new, because the DB is now production)

- In `session.py::get_engine`, register a `connect` listener on SQLite engines only:
  `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA synchronous=NORMAL`,
  `PRAGMA foreign_keys=ON`.
- Keep transactions short (already the pattern); no session across SMTP/sleep.
- Stagger the timetable so two write-heavy stages do not start in the same minute
  (enrich `:50`, compose `:00/:20/:40`, send `:05/:25/:45`).
- Test: two sessions writing concurrently with a 5 s timeout do not raise
  `database is locked`; WAL is active on a temp file; a reader sees committed rows during a
  write.

### 6.5 Decide before opening SMTP; add `--max-runtime`

- Move the window/cap pre-check ahead of `transport.open()` (`runner.py:122` vs `:231`), using
  `SendingWindow.is_open` (`policy.py:69-73`) and `count_sent_between` (`repo.py:1233`).
  Closed window or exhausted cap → `stop_reason` set, exit 0, **no authenticated session**.
  Today a capped day would still open/login/quit on every tick — exactly the pattern Gmail's
  abuse heuristics dislike.
- Add `--max-runtime SECONDS` (default 0 = unlimited) to `send`: after each `finalize`, if the
  budget is exceeded, set `stop_reason="budget"` and break (exit 0). Cap the next jitter sleep
  to the remaining budget so a run cannot overshoot by 30 min.
- Tests: closed window → `transport.open` not called; cap reached → not called; budget 720 s
  with mean 540 s yields two sends and a clean exit; injected clock only.

### 6.6 Exit code 75 for "busy"

- `LockHeldError` now exits **75** (`EX_TEMPFAIL`) instead of 1. Update `phase4.md`, README and
  tests. A scheduled tick that loses the race is a no-op, not an alarm.
- Keep 0/1/2 unchanged. `ops\run.ps1` maps its own mutex-busy to 75 as well.
- `status`/`doctor` use 0/1/3 (3 = degraded: `uncertain > 0` or a stale stage).

### 6.7 Config/path fixes

- `FORGESMITH_DATA_DIR` is honoured everywhere; in the image it is `/app/data` and the DB URL
  is `sqlite+aiosqlite:////app/data/forgesmith.db` (four slashes = absolute).
- Default `shopify_seeds_file` and `enrich_domain_overrides` must resolve under `data_dir`,
  not `PROJECT_ROOT` (`config.py:38,55`), or be set explicitly in compose. Add a test that
  both resolve under the configured data dir with a non-default `PROJECT_ROOT`.
- Remove the `localhost:11434` footgun for containers by setting
  `FORGESMITH_OLLAMA_BASE_URL=http://host.docker.internal:11434` in compose; keep the dev
  default for native runs.
- Add `forgesmith backup --out DIR` using SQLite `VACUUM INTO` (consistent snapshot under WAL;
  parameter binding is not supported for the path, so quote/escape it and reject an existing
  target). Test: backup opens, row counts match, `VACUUM INTO` over an existing file fails
  cleanly.

---

## 7. Image design

Two targets from one Dockerfile. Debian, never Alpine (musl + Chromium + `tzdata` is a
`zoneinfo`/Playwright hazard).

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm AS slim
ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    TZ=Europe/Warsaw PATH=/app/.venv/bin:$PATH PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update && apt-get install -y --no-install-recommends tini ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
RUN useradd -u 10001 -m forgesmith && mkdir -p /app/data && chown -R 10001:10001 /app
USER 10001:10001
WORKDIR /app
ENTRYPOINT ["tini","--","forgesmith"]

FROM slim AS browser
USER root
RUN /app/.venv/bin/playwright install --with-deps chromium \
 && chmod -R a+rX /ms-playwright && rm -rf /var/lib/apt/lists/*
USER 10001:10001
```

- `README.md` must be in the build context: `pyproject.toml` declares `readme = "README.md"`.
- `slim` ≈ 250–350 MB (compose, send, approve, source shopify); `browser` ≈ 1.1–1.3 GB
  (source olx, enrich).
- `ENTRYPOINT ["tini","--","forgesmith"]` + `init: true` in compose; both are acceptable, use
  `init: true` and drop tini from the image if it proves redundant.
- Non-root uid **10001** everywhere; the named data volume inherits the image's `/app/data`
  ownership on first use.
- Browser args become configuration: `FORGESMITH_BROWSER_NO_SANDBOX=true` appends
  `--no-sandbox`, `--disable-dev-shm-usage` in `browser.py:79-86`. Default `false` keeps the
  native Windows dev behaviour identical. `shm_size: 1gb` on the browser service.
- `.dockerignore`: `.git`, `.venv`, `data`, `outbox`, `backups`, `state`, `tests`,
  `__pycache__`, `.pytest_cache`, `.ruff_cache`, `phase*.md`, `.env*` (keep `.env.example`
  out too; it is documentation).
- `FORGESMITH_LOG_JSON=true` swaps `ConsoleRenderer` for `JSONRenderer` (`logs.py`); default
  stays human-readable for dev.
- Do **not** install the project from a wheel into site-packages without setting
  `FORGESMITH_DATA_DIR` — the `parents[2]` default breaks there.

---

## 8. Compose topology and profiles

```yaml
x-app: &app
  image: forgesmith:${FORGESMITH_TAG:-latest}
  build: {context: ., target: slim}
  profiles: ["app"]
  env_file: [.env]
  init: true
  user: "10001:10001"
  read_only: true
  tmpfs: [/tmp]
  cap_drop: [ALL]
  security_opt: ["no-new-privileges:true"]
  volumes:
    - fs-data:/app/data
    - ./outbox:/app/data/outbox
    - ./backups:/app/data/backups
  extra_hosts: ["host.docker.internal:host-gateway"]
  restart: "no"

services:
  app: {<<: *app, profiles: ["app","dry"]}
  app-live:
    <<: *app
    profiles: ["live"]
    env_file: [.env, .env.live]     # .env.live sets MAIL_DRY_RUN=false
  app-browser:
    <<: *app
    profiles: ["browser","live-browser"]
    build: {context: ., target: browser}
    shm_size: 1gb
    read_only: false                 # Chromium crashpad/user-data needs a writable fs
    environment: {FORGESMITH_BROWSER_NO_SANDBOX: "true"}
  # Optional PG experiment / branch hygiene only; not production.
  db:      {image: postgres:16-alpine, profiles: ["tools"], ...}
  adminer: {image: adminer:latest, profiles: ["tools"], ports: ["8080:8080"]}
  ollama:  {image: ollama/ollama, profiles: ["llm"], volumes: [ollama:/root/.ollama]}

volumes: {fs-data: {}, ollama: {}}
```

- **`docker compose up -d` starts nothing** — every service has a profile. A reflexive `up`
  can never start the app, let alone send.
- **Four independent live gates:** `queued` + latest approval event; `--live --yes` only in a
  committed task definition; `FORGESMITH_MAIL_DRY_RUN=false` in `.env.live`; `--profile live`.
- **SQLite lives on the `fs-data` named volume**, not a bind mount: `mailer.lock` needs a real
  `flock`, and virtiofs bind mounts are unreliable for that. `outbox/` and `backups/` are bind
  mounts because the operator must inspect them from Windows and no locks are taken there.
- **`fs-data` layout:** `/app/data/forgesmith.db` (+ `-wal`, `-shm`), `mailer.lock`,
  `shopify_seeds.txt`, `domain_overrides.csv`.
- **Ollama stays on the host** via `host.docker.internal:11434`; the `llm` profile exists for a
  co-located CPU Ollama (7B Q4 needs ~5–6 GB RAM). The "scraped text stays local" claim holds
  because the host is the owner's machine.
- **Secrets:** `.env` and `.env.live` on the host (mode 600 via `icacls`), never baked
  (`.dockerignore`), never committed; `smtp_password` is already `repr=False`. `docker inspect`
  can read env vars — accepted for a single-user desktop, documented.
- **DB exposure:** none. The PG service, when used, is `tools`-only and binds `127.0.0.1`.
- **Postgres remains supported in code** (`aiosqlite` is the default; `asyncpg` stays a
  dependency) but is no longer a Phase 5 deliverable.

---

## 9. Scheduler and timetable

**Primary: Windows Task Scheduler** (the owner's chosen host). Each task runs
`powershell -NoProfile -File C:\Projects\forgesmith\ops\run.ps1 <stage> [args]` as the logged-on
user, with **"If the task is already running: do not start a new instance"**.

`ops/run.ps1` (essentials):

```powershell
$stage = $args[0]
$mutex = [Threading.Mutex]::new($false, "Global\forgesmith-$stage")
if (-not $mutex.WaitOne(0)) { exit 75 }          # another tick owns this stage
try {
  $log = "C:\Projects\forgesmith\state\logs\$stage-$(Get-Date -f yyyyMMdd).log"
  docker compose --profile live run --rm -T app-live @args *>> $log
  $code = $LASTEXITCODE
  "{`"stage`":`"$stage`",`"exit`":$code,`"at`":`"$(Get-Date -o)`"}" |
      Set-Content "C:\Projects\forgesmith\state\last-$stage.json"
  if ($code -in 1,2) { & .\ops\alert.ps1 $stage $code $log }
  exit $code
} finally { $mutex.ReleaseMutex() }
```

- Separate tasks for the browser stages use `--profile browser run --rm app-browser`.
- `send` uses the `live` profile; `compose`/`source shopify` use `app` (slim).
- Single-flight is enforced three ways: Task Scheduler's instance policy, the per-stage named
  mutex, and the in-container `RunLock` for live sends.
- **Never** schedule `approve`; it has no task definition and no `run.ps1` branch.

| Task | Cadence (host local time, Europe/Warsaw) | Service | Arguments | Catch-up |
|---|---|---|---|---|
| `forgesmith-source-shopify` | Mon 06:10 | `app` | `--limit 200` | yes |
| `forgesmith-source-olx` | Mon & Thu 06:30 | `app-browser` | `-q … --city … --pages 2` | yes |
| `forgesmith-enrich` | every 2 h at :50, 08:50–22:50 | `app-browser` | `--limit 30` | yes |
| `forgesmith-compose` | every 20 min, 08:00–17:40 | `app` | `--limit 5` | no |
| `forgesmith-send` | Mon–Fri every 20 min, 08:45–17:05 | `app-live` | `--limit 3 --max-runtime 720 --live --yes` | **no** |
| `forgesmith-approve` | **never** | — | human only | — |
| `forgesmith-inbox` | reserved for Phase 4.5 (every 30 min, 08:00–18:00) | — | not implemented | — |
| `forgesmith-backup` | daily 03:15 | `app` | `backup --out /app/data/backups` + outbox tar | yes |
| `forgesmith-digest` | daily 18:00 (optional) | `app` | `status --json` → `state\digest.txt` | no |

- **No catch-up for `send`:** a missed tick after sleep/reboot must not fire a burst. The
  runner re-checks the window anyway (`policy.py:69-73`), but don't rely on it.
- The timer is a *bound*, never the pacing source: `mail_delay_mean=540 s` remains
  authoritative inside a run. With `--max-runtime 720` a run sends ~2 messages spaced by the
  real Gaussian delay, exits ≤12 min, and the next tick is ≥8 min later (single-flight margin
  >1.5×). 20 sends/day therefore take ~3 h, matching Phase 4's intended shape
  (540 × 20 = 10 800 s < the 8 h window).
- **Never use `--no-delay` in a task.** It collapses intra-run gaps into a burst; it stays a
  manual smoke-test flag (`--limit 1` makes it a no-op there).
- Stagger `enrich` (`:50`), `compose` (`:00/:20/:40`) and `send` (`:05/:25/:45`) so SQLite
  writers rarely contend; WAL + `busy_timeout` is the safety net, not the plan.
- Document the invariant `mail_delay_mean × mail_daily_cap ≤ window length`. Raising the cap or
  the mean without widening the window truncates sends at the window edge.
- **Windows caveats (must be in the runbook):** Docker Desktop must be running in the
  interactive session; enable "Start Docker Desktop when you sign in", enable auto-login,
  disable sleep/hibernate on AC power, exclude `data/` and the Docker VHDX from Defender
  real-time scanning, and do not let `wsl --shutdown` run. Task Scheduler cannot wake a
  sleeping PC unless "Wake the computer to run this task" is set (leave it off for `send`;
  a missed send is safer than a 03:00 burst).
- **Clock-skew check:** `run.ps1` passes `FORGESMITH_HOST_NOW` (UTC, ISO 8601) into the
  container; `doctor`/`send` warn when container↔host skew exceeds 120 s (WSL2 time drift after
  resume is real).

**Alternate (optional, documented but not required):** enable systemd in the WSL2 distro
(`/etc/wsl.conf` → `[boot] systemd=true`) and use the same unit files as a Linux host. This
keeps a future migration cheap but is not part of the Phase 5 acceptance path.

---

## 10. Live-send safety contract (unchanged in spirit, explicit in code)

| Gate | Where enforced |
|---|---|
| Lead is `queued` and the latest message has an `approved` event | `repo.reserve` re-check (unchanged) |
| Suppression, contact identity, footer/prompt version, allowlist, attempts, backoff, uniqueness | `repo.reserve` (unchanged) |
| Window and daily cap | `runner` pre-check **before** `transport.open`, re-checked per send |
| Window/cap/kill-switch/budget no-op | exit 0 + `run` event; the wrapper treats it as success |
| `--live --yes` | only in the committed task definition; CLI remains dry by default |
| `mail_dry_run=false` | real kill switch (6.1) |
| `--profile live` + `.env.live` | compose |
| Approval | never automated; `approve` refuses when `FORGESMITH_APPROVAL_CHANNEL=unattended` (new, default empty) |
| One live writer | partial unique index + named mutex + Task Scheduler instance policy + `RunLock` |
| SIGTERM | cooperative stop at safe points; grace > `smtp_timeout` |

**Kill switch for an operator in a hurry:** set `FORGESMITH_MAIL_DRY_RUN=true` (the next tick
is a no-op and logs `kill_switch`), then disable the task:
`schtasks /Change /TN "Forgesmith\send" /DISABLE`. `mail_daily_cap=0` is the second independent
stop.

---

## 11. Observability and ops commands

- **Run ledger:** every mutating stage invocation writes `events(entity="run",
  action="finished", detail={stage, exit, stop_reason, duration_s, counts})` from a small
  decorator in `cli.py`. Written only after the stage finishes, so it cannot fake liveness.
  Read-only commands (`sends`, `drafts`, `leads`, `outbox`, `status`, `doctor`, `mark`,
  `suppress`, `unsuppress`) are not recorded.
- **`forgesmith status`:** counts by lead status, `composed` awaiting review, `queued`, sends
  by status, outstanding `uncertain`, last run per stage (exit/stop reason), next window
  opening, outbox file count/size, and `kill_switch` state.
- **`forgesmith doctor [--smtp-check] [--json]`:** DB connect + schema, data dir and outbox
  writable, sender identity, timezone load, Ollama reachability, disk free, lock holder PID,
  `uncertain` count, stale runs, host/container clock skew. Exit 0 ok / 1 broken / 3 degraded.
  `--smtp-check` performs a preflight connect only (never a send) and is opt-in.
- **Logs:** `structlog` JSON when `FORGESMITH_LOG_JSON=true`; `run.ps1` appends each stage to
  `state\logs\<stage>-YYYYMMDD.log` (date rotation) and writes `state\last-<stage>.json`.
  `docker logs` is useless with `--rm`.
- **Alerting:** `ops\alert.ps1` posts to ntfy/webhook/email when exit ∈ {1,2} **or** exit 0 with
  a bad `stop_reason` (`transport_error`, `archive_failed`, `relay_unavailable`,
  `policy_rejection_streak`, `aborted`). Exit 75 (`busy`) and
  `kill_switch`/`window`/`cap`/`budget` are silent. Task Scheduler's own failure events are a
  weak fallback, not the mechanism.
- **Daily digest** (optional): `status --json` to `state\digest.txt` at 18:00; a free dead-man
  ping (healthchecks.io) is the only way to notice a dead task, and is optional.
- No Prometheus/Grafana. At this volume a DB table and a healthcheck are enough.

---

## 12. Data, backup, retention

- `fs-data` holds `forgesmith.db` (+ WAL/SHM), `mailer.lock`, seeds, overrides. `outbox/` holds
  `.eml` evidence (names + business email = personal data) and is bind-mounted for inspection.
- **Backup (nightly):** `forgesmith backup --out /app/data/backups` (`VACUUM INTO`, consistent
  under WAL) + `tar` of `outbox/` to `.\backups\`, 14 daily + 8 weekly, `icacls` restricting
  the folder to the owner. Copy off-host occasionally (OneDrive/encrypted drive) — but never
  put the live DB itself in a synced folder.
- **Restore drill (M5.5):** stop the tasks, copy the backup over `fs-data`, run `db init`,
  compare `status` counts before/after.
- **Retention:** `.eml` kept 12 months (they prove the opt-out line was present); DB rows kept
  indefinitely except `events` (prune > 24 months, optional `purge` command later). Document
  the RODO storage-limitation rationale in the runbook.
- **Never** put `fs-data` on a network filesystem or a OneDrive-synced path: `flock` and SQLite
  both misbehave.
- `.dockerignore` and `.gitignore` already keep `data/` out of the image/repo; add `outbox/`,
  `backups/`, `state/`.

---

## 13. Configuration additions (`.env.example`)

```
# --- container / phase 5 ---------------------------------------------------
FORGESMITH_DATA_DIR=/app/data
FORGESMITH_DATABASE_URL=sqlite+aiosqlite:////app/data/forgesmith.db
FORGESMITH_OLLAMA_BASE_URL=http://host.docker.internal:11434
FORGESMITH_LOG_JSON=true
FORGESMITH_BROWSER_NO_SANDBOX=true
FORGESMITH_APPROVAL_CHANNEL=            # set to "unattended" to make approve refuse
# ops-side (read by the wrapper/compose, not pydantic)
FORGESMITH_TAG=latest
```

New pydantic fields: `browser_no_sandbox: bool = False`, `log_json: bool = False`,
`approval_channel: str = ""`, `send_max_runtime: float = 0`,
`db_busy_timeout_ms: int = 5000`, `stage_stale_after: float = 21600` (doctor). No new runtime
dependency.

---

## 14. Testing and CI

**Offline suite (extends the existing 278; no sockets, no real sleeps):**

| File | Cases |
|---|---|
| `test_mailer_dry_run_gate.py` | `live=True` + `mail_dry_run=True` sends nothing, exits 0, `stop_reason=kill_switch`, no `.eml`; setting is actually read; plain CLI `send` still dry |
| `test_mailer_stop.py` | stop during jitter (≤5 s chunks) sends nothing; stop between messages finalizes the prefix; fake blocking `deliver` is finalized, never cancelled; worst-case shutdown < grace; SIGINT path |
| `test_mailer_budget.py` | `--max-runtime` stops after the budget, exit 0, `stop_reason=budget`; jitter capped to remaining budget; injected clock |
| `test_mailer_schema.py` | partial unique index on SQLite; duplicate detection aborts with message ids; ran twice; `failed`/`aborted` retryable; second `pending`/`sent` refused; `IntegrityError` maps to `existing_attempt` |
| `test_mailer_sqlite.py` | WAL active, `busy_timeout` applied, concurrent writers do not raise `database is locked`, reader sees committed rows during a write |
| `test_mailer_recheck.py` | window/cap checked before `transport.open` (fake records `open` not called); same for kill switch |
| `test_mailer_cli.py` | lock held → exit 75; busy wrapper → 75; 0/1/2 unchanged |
| `test_ops_runner.py` | `run.ps1`/`run.sh` with a fake `docker` on `PATH`: busy → 75, args passed verbatim, log/last-state written, no `approve` stage exists |
| `test_runs_ledger.py` | every mutating stage writes one `run` row; read-only commands do not; failure records exit and stop reason |
| `test_status_doctor.py` | counts, uncertain surfaced, stale run → exit 3, broken DB → 1, `--json` shape |
| `test_backup.py` | `VACUUM INTO` snapshot restores and matches counts; existing target refused |
| `test_config_paths.py` | `DATA_DIR` honoured; seeds/overrides resolve under it; Ollama URL explicit |

**Image / compose smoke (CI, Ubuntu runner):**

1. `docker build --target slim .` and `docker run --rm forgesmith:slim forgesmith version`.
2. Build `browser` nightly (cache `~/.cache/uv` + Playwright); assert `forgesmith doctor` sees
   Chromium (`playwright install --dry-run`).
3. `docker compose --profile app run --rm -T app forgesmith db init` on a throwaway volume, then
   `doctor`, `status`, dry `send` → exit 0.
4. Optional PG job (`--profile tools`): `db init` against Postgres so `schema.py:22-31` keeps
   working. Branch hygiene, not production.
5. `shellcheck ops/run.sh`; `Invoke-ScriptAnalyzer ops/*.ps1` on a Windows runner;
   `systemd-analyze verify ops/systemd/*` only if the optional WSL2 path ships.

Keep the default gate `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`.
Live SMTP and live Ollama remain operator-run smoke tests, never CI.

---

## 15. Milestones

| # | Deliverable | Acceptance |
|---|---|---|
| **M5.0** | §6 hardening: real `mail_dry_run`, cooperative stop, partial unique index + `IntegrityError` mapping, WAL/busy timeout, pre-open window/cap, `--max-runtime`, exit 75, data-dir paths, `backup` | New tests green; 278 existing tests untouched; no topology change yet |
| **M5.1** | `Dockerfile` (`slim`, `browser`), `.dockerignore`, JSON logs, browser args, non-root | `docker run … forgesmith version`; browser target sees Chromium; image sizes recorded |
| **M5.2** | Compose: `app`, `app-live`, `app-browser`, named `fs-data`, `outbox`/`backups` binds, `ollama` profile, `tools` PG | `docker compose up -d` starts nothing; `run app send` dry works on SQLite in a volume |
| **M5.3** | `ops/run.ps1`, Task Scheduler definitions (XML import), mutex/75, alert/digest scripts, `status`, `doctor`, run ledger | Busy → 75; timetable inspectable via `schtasks`; one dry day of all stages; alert fires on a forced failure |
| **M5.4** | Docs: README/AGENTS/`.env.example`, `ops/README.md` runbook, Windows auto-login/power checklist, backup/restore, retention | An operator can deploy from the docs alone |
| **M5.5** | Deploy + smoke on the desktop: build, dry pipeline, allowlisted live send, reboot/Docker restart test, SIGTERM tests, backup/restore drill | §2 criteria 1–10 pass on the real host |
| **M5.6** | Optional: daily digest, dead-man ping, `purge --older-than`, image tag pinning, WSL2 systemd units | Not required for Phase 5 done |

Build order: hardening → image → compose → scheduler/ledger → docs → deploy/smoke.

---

## 16. Deployment runbook (this Windows desktop)

1. Docker Desktop: WSL2 backend, "Start Docker Desktop when you sign in"; configure auto-login;
   disable sleep/hibernate on AC; exclude `C:\Projects\forgesmith\data` and the Docker VHDX
   from Defender real-time scanning.
2. Clone/keep the repo at `C:\Projects\forgesmith`; `cp .env.example .env`; create `.env.live`;
   restrict both with `icacls .env /inheritance:r /grant:r "$env:USERNAME:(R,W)"`; fill SMTP,
   sender identity, allowlist = your own address.
3. `docker compose --profile app build` (and `--profile browser build` once).
4. `docker compose --profile app run --rm -T app forgesmith db init`;
   `… forgesmith doctor`.
5. Run the pipeline dry: `source shopify`, `enrich`, `compose`, `drafts`, inspect, **manually**
   `approve --lead-id N`, then `send` (dry) and inspect `.\outbox`.
6. Allowlisted live: `FORGESMITH_MAIL_ALLOWLIST=you@…` then
   `docker compose --profile live run --rm -T app-live send --live --yes --limit 1`.
7. Import the tasks with `schtasks /Create /XML ops\tasks\forgesmith-<stage>.xml /TN
   "Forgesmith\<stage>"`; keep `mail_dry_run=true` for the first full day.
8. Observe: `status`, `sends`, `drafts`, `state\logs\*`, `state\last-*.json`.
9. Flip `.env.live` to `FORGESMITH_MAIL_DRY_RUN=false`; enable the send task.
10. Kill tests: SIGTERM during jitter and during `DATA`; confirm §2 criteria 4–5.
11. Reboot; confirm Docker Desktop restarts, tasks fire, and a missed `send` tick did not
    back-fill.
12. Run the backup task manually, restore into a scratch project, compare counts.

---

## 17. Rejected alternatives

| Option | Why rejected |
|---|---|
| Resident daemon + in-process asyncio scheduler | Unjoinable SMTP thread + cancellation = double-send vector; `restart:` hides crash-loops; no structural single-flight; revisit only for IMAP IDLE |
| Postgres as the production DB | Single host, one writer, no scale requirement; SQLite on a named volume is simpler and its backup is one command. PG stays supported and CI-smoked for the future |
| Bind-mounting the SQLite DB / `mailer.lock` | Docker Desktop's virtiofs makes `flock` unreliable; the DB and lock go on a named volume |
| Alpine base | musl + Chromium + `zoneinfo`/`tzdata` fragility; no size win once Playwright is installed |
| `mcr.microsoft.com/playwright/python` as the base | Bundled browser version drifts from `playwright>=1.64` in `uv.lock`; one pinned source of truth |
| Chromium in the send/compose image | +1 GB and a writable fs for stages that never launch a browser |
| Scheduled `approve --all-composed --yes` | Turns the LLM into an unreviewed sender; RODO/product decision, not an ops one |
| `restart: unless-stopped` on the job container | Crash-loop + `pending → uncertain` silently consumes leads while looking healthy |
| Kubernetes / Nomad / Ofelia / runsc | Overkill for one desktop and ~60 ticks/day; extra control plane or dependency |
| A WSL2 systemd timetable as the primary | Works, but adds a second init/OS layer to debug on Windows; keep it as the documented migration path |
| `--no-delay` in a task | Same-second bursts; defeats the jitter the window exists for |

---

## 18. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Double send during shutdown | Cooperative stop at safe points; partial unique index; mutex + instance policy; grace > `smtp_timeout` |
| Kill in the middle of a send burns a lead | Rare by design (only during the ~4 s `deliver`); `uncertain` is loud in `status`/digest; `mark --send-id` resolves |
| Dead task / silent stop | Run ledger, `doctor`, daily digest, optional dead-man ping; `alert.ps1` on failure |
| Docker Desktop requires an interactive login | Auto-login + "start on sign-in" documented; if it breaks, the timetable stalls — the digest/dead-man exposes it |
| PC sleeps or WSL2 clock drifts | No sleep on AC; no wake-timer for `send`; host-time skew check in `doctor`; `Persistent` catch-up only for non-send stages |
| SQLite write contention | WAL + 5 s `busy_timeout`; staggered minute offsets; short transactions |
| Windows Defender / OneDrive stalls the volume | Exclusions for `data/` and the VHDX; never sync the live DB |
| Gmail suspension from a residential IP | Allowlist first, cap 20, jitter, dedicated domain/SPF/DKIM later; document as the main deliverability risk |
| Ollama unreachable / OOM | Explicit base URL, `doctor` check, `compose_failed` recovers next tick; 7B on ≥8 GB or a smaller model decision |
| Chromium wedged / OOM in container | Fresh container per run, `shm_size: 1gb`, `--disable-dev-shm-usage` |
| `ensure_schema` fails on a dirty DB | Duplicate detection aborts with ids before the index; documented resolution |
| Path defaults break in the image | `FORGESMITH_DATA_DIR` explicit + `test_config_paths` |
| Secrets leak via `docker inspect` | `.env.live` ACLs, not baked, documented |
| 3-hour send run misread as a bug | Document `mean × cap ≤ window`; `--max-runtime` bounds each run |
| RODO retention | 12-month `.eml`, documented purpose, ACLs, off-host encrypted backup |

---

## 19. Decisions

**Resolved (2026-10-23):**

1. Deploy target: this Windows desktop (Task Scheduler primary, WSL2 systemd optional).
2. Unattended live sending: allowed, approved leads only.
3. Production DB: SQLite on the `fs-data` named volume; no advisory lock; partial unique index
   is the DB guard.

**Still open (defaults proposed):**

4. Ollama model: `qwen2.5:7b` on this machine vs a smaller model (latency/RAM). Default: keep
   7B on the host.
5. Exit-code contract change: 75 = busy. Default: accept.
6. Retention: 12-month `.eml`, 14/8 backups. Default: accept.
7. Outbox location: `C:\Projects\forgesmith\outbox` (bind mount). Default: yes; revisit if the
   repo moves.

---

## 20. Definition of done

- [ ] `mail_dry_run` is a real gate (or deleted); a test proves it is read.
- [ ] SIGTERM during jitter or `deliver` never cancels the worker thread; grace > timeout.
- [ ] The partial unique index makes double reservation impossible and `ensure_schema` refuses
      to install it over duplicate rows.
- [ ] WAL + `busy_timeout` are active; concurrent stage writes do not raise `database is locked`.
- [ ] Window/cap are checked before any SMTP connection; `--max-runtime` bounds every run.
- [ ] `slim` and `browser` images build, run non-root, and the send image contains no Chromium.
- [ ] `docker compose up -d` starts nothing; live needs profile + `.env.live` + `--live`.
- [ ] Tasks exist for source/enrich/compose/send and never for approve; busy exits 75.
- [ ] Every mutating stage writes one `run` event; `status`/`doctor` are usable and exit
      correctly.
- [ ] `backup` produces a restorable snapshot and the restore drill matches counts.
- [ ] 278 existing tests still pass offline; new tests cover §14.
- [ ] One dry day, one allowlisted live send, one reboot, one SIGTERM drill, one restore drill.
- [ ] README/AGENTS/`.env.example`/runbook match the shipped behaviour; Phase 4.5 is still
      unimplemented and clearly marked.
