# Forgesmith operations runbook (Phase 5)

Everything here targets **this Windows desktop**: Docker Desktop (WSL2 backend)
plus Windows Task Scheduler. The Python process never runs resident; every
stage is a one-shot container started by the scheduler.

`docker compose up -d` starts **nothing** — every service is behind a profile.

## Four independent live gates

A live message is possible only when all four hold:

1. The lead is `queued` and its latest message has an `approved` event
   (human approval; automation stops at `composed`).
2. The task definition passes `--live --yes` (only `ops/tasks/forgesmith-send.xml`).
3. `.env.live` sets `FORGESMITH_MAIL_DRY_RUN=false` (the real kill switch).
4. The run uses `--profile live` (the `app-live` service).

`approve` has no task definition and no `run.ps1` stage; it also refuses when
`FORGESMITH_APPROVAL_CHANNEL=unattended`.

## First-time setup

```powershell
# 1. Configuration
Copy-Item .env.example .env
Copy-Item .env.example .env.live
# Edit .env: DB, sender identity, SMTP, Ollama URL.
# Edit .env.live: FORGESMITH_MAIL_DRY_RUN=false plus SMTP/sender values.
icacls .env      /inheritance:r /grant:r "$env:USERNAME:(R,W)"
icacls .env.live /inheritance:r /grant:r "$env:USERNAME:(R,W)"

# 2. Host directories used as bind mounts
New-Item -ItemType Directory -Force outbox, backups, state | Out-Null

# 3. Images
docker compose --profile app build
docker compose --profile browser build

# 4. Database on the named volume
docker compose --profile app run --rm -T app forgesmith db init
docker compose --profile app run --rm -T app forgesmith doctor

# 5. Dry pipeline (no live mail)
docker compose --profile app run --rm -T app forgesmith source shopify --limit 10
docker compose --profile browser run --rm -T app-browser forgesmith enrich --limit 10
docker compose --profile app run --rm -T app forgesmith compose --limit 3
docker compose --profile app run --rm -T app forgesmith drafts
docker compose --profile app run --rm -T app forgesmith approve --lead-id 1
docker compose --profile app run --rm -T app forgesmith send
Get-ChildItem outbox

# 6. Allowlisted live smoke test (one message)
# .env.live: FORGESMITH_MAIL_ALLOWLIST=you@example.com
docker compose --profile live run --rm -T app-live forgesmith send --live --yes --limit 1

# 7. Schedule
powershell -NoProfile -ExecutionPolicy Bypass -File ops\install-tasks.ps1
schtasks /Query /TN "Forgesmith\send" /V /FO LIST
```

Keep `FORGESMITH_MAIL_DRY_RUN=true` for the first full scheduled day. Flip it in
`.env.live` only after the dry day looks clean.

## Timetable

| Task | Cadence (host local, Europe/Warsaw) | Service | Arguments |
|---|---|---|---|
| `Forgesmith\source-shopify` | Mon 06:10 | `app` | `--limit 200` |
| `Forgesmith\source-olx` | Mon & Thu 06:30 | `app-browser` | `-q meble --city warszawa --pages 2 --limit 200` |
| `Forgesmith\enrich` | every 2 h, 08:50–22:50 | `app-browser` | `--limit 30` |
| `Forgesmith\compose` | every 20 min, 08:00–17:40 | `app` | `--limit 5` |
| `Forgesmith\send` | Mon–Fri every 20 min, 08:45–17:05 | `app-live` | `--limit 3 --max-runtime 720` |
| `Forgesmith\backup` | daily 03:15 | `app` | `backup --out /app/data/backups` |
| `Forgesmith\digest` | daily 18:00 | `app` | `status --json` |

`send` has **no catch-up** (`StartWhenAvailable=false`): a missed tick after
sleep or reboot must not fire a burst. The runner also re-checks the window and
daily cap before opening SMTP.

Never add `--no-delay` to a task. Never schedule `approve`.

`inbox sync` is intentionally not in the task set: hard bounces and thread-matched
replies apply automatically, but opt-outs and unmatched mail wait in the review
ledger for `inbox apply --id N --yes`. Run it from an interactive shell before
review, or add an hourly task once you are comfortable with the ledger.

## Exit codes

| Code | Meaning | Alert? |
|---|---|---|
| 0 | finished; window/cap/kill switch/budget are also 0 | only for a bad stop reason |
| 1 | real failure | yes |
| 2 | refused (confirmation/approval) | yes |
| 75 | another tick of the same stage is running | no |

`ops/run.ps1` uses a `Global\forgesmith-<stage>` mutex, writes
`state\logs\<stage>-YYYYMMDD.log` and `state\last-<stage>.json`, and calls
`ops\alert.ps1` for codes 1/2 or a bad stop reason (`transport_error`,
`archive_failed`, `relay_unavailable`, `policy_rejection_streak`, `aborted`).
Set `FORGESMITH_NTFY_URL` or `FORGESMITH_ALERT_WEBHOOK`; otherwise alerts append
to `state\alerts.log`.

## Kill switch (in a hurry)

```powershell
# 1. Next tick becomes a no-op (exit 0, stop_reason=kill_switch)
(Get-Content .env.live) -replace 'FORGESMITH_MAIL_DRY_RUN=false','FORGESMITH_MAIL_DRY_RUN=true' |
    Set-Content .env.live

# 2. Stop the schedule
schtasks /Change /TN "Forgesmith\send" /DISABLE

# 3. Independent second stop
# FORGESMITH_MAIL_DAILY_CAP=0 in .env.live
```

Check state with:

```powershell
docker compose --profile app run --rm -T app forgesmith status
docker compose --profile app run --rm -T app forgesmith status --json
docker compose --profile app run --rm -T app forgesmith sends --status uncertain
docker compose --profile app run --rm -T app forgesmith doctor
```

## Backup and restore

Nightly `backup` runs `VACUUM INTO` inside SQLite, which is consistent while
other stages write (WAL). The snapshot lands in the `backups/` bind mount,
alongside an `outbox-*.tar.gz`.

Manual backup:

```powershell
docker compose --profile app run --rm -T app forgesmith backup --out /app/data/backups
```

Restore drill (stop every stage first):

```powershell
# A restore while compose/enrich write would corrupt the DB.
Get-ScheduledTask -TaskPath '\Forgesmith\' | Disable-ScheduledTask

# Pick a snapshot from .\backups, replace the DB, and drop stale WAL files.
docker compose --profile app run --rm -T --entrypoint sh app -c `
  "rm -f /app/data/forgesmith.db-wal /app/data/forgesmith.db-shm; cp /app/data/backups/forgesmith-YYYYMMDDTHHMMSSZ.db /app/data/forgesmith.db"

docker compose --profile app run --rm -T app forgesmith doctor

# Re-enable once the restored state looks right.
Get-ScheduledTask -TaskPath '\Forgesmith\' | Enable-ScheduledTask
```

Keep 14 daily + 8 weekly copies: `ops\run.ps1` adds `--keep 22` to every
scheduled `backup`, so manual runs need `--keep 22` explicitly. Copy off-host
occasionally, but never put the live DB inside a synced folder.

## Windows host checklist

- Docker Desktop → WSL2 backend, “Start Docker Desktop when you sign in”.
- Windows auto-login enabled so the interactive session exists after reboot.
- Sleep/hibernate disabled on AC power; leave “Wake the computer” **off** for
  `send` (a missed send is safer than a 03:00 burst).
- Do not run `wsl --shutdown` while tasks are scheduled.
- Exclude `C:\Projects\forgesmith\backups` and the Docker VHDX from Defender
  real-time scanning.
- Task Scheduler → each task → “If the task is already running: do not start a
  new instance” (already in the XML as `IgnoreNew`).
- `ops\install-tasks.ps1` imports definitions; `schtasks /Query /XML` shows the
  exact command line.

## Optional Linux/WSL2

`ops/run.sh` mirrors `run.ps1` with `flock` and exit 75. It is not required for
Phase 5; it keeps a future migration cheap.

## Logs and the run ledger

- Per-stage log: `state\logs\<stage>-YYYYMMDD.log`.
- Last result: `state\last-<stage>.json`.
- Durable ledger: `events(entity="run", action="finished")` with
  `{stage, exit_code, duration_s, stop_reason, error}`.
- `status` shows the latest run per stage, window state, kill switch, counts,
  uncertain sends, and outbox usage.
- `doctor` exits 0 (ok), 1 (broken), or 3 (degraded); `--smtp-check` performs a
  connect/STARTTLS/login preflight only, never a send.
