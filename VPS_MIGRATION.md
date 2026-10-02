# VPS Migration Spec — Trading Bots

**Status:** proposed · **Date:** 2026-09-13

Move IB Gateway and the always-on bots off the home iMac onto an always-on
Linux server, and fix the failure modes the logs show — so the forward test
produces usable data.

---

## 1. Why

Since 2026-07-01, **21 of 55 TSMOM runs failed**, POC logged 3,557 and sweep 713
`reconnect_failed` events, and both produced far fewer setups than their
backtest rates. At that uptime the paper forward test cannot validate or reject
any strategy.

### What the logs say caused the outages

| Evidence | Count | Meaning |
|---|---|---|
| sweep `gateway_disconnected` by UTC hour | **540 of 722** between 01:00–04:59 UTC | = 04:00–07:59 Helsinki. Starts at IBC `AutoRestartTime=04:00`, ends when the first health check runs at 08:00 |
| health check outcomes since Jul 1 | 37 down → 29 recovered, **8 failed** | 8 days needed a manual login; these hit TSMOM's 16:00 run |
| IBC "Unrecognized Username or Password" | 36 | transient IBKR login rejections (credentials are correct) |
| `Error 1100` (IBKR↔Gateway connectivity lost) in bot logs | 760 | session/network drops |
| POC + sweep `reconnect_failed` | 4,270 | bots never recover in-process after Gateway comes back |

Two conclusions shape this spec:

1. **The biggest single cause is not the Mac.** It is a ~4-hour blind spot
   between the nightly Gateway restart and the first health check, made worse
   by bots that can't reconnect on their own. Moving to a VPS doesn't fix that
   by itself — the watchdog and restart changes in §5 do, and they ship before
   or with the move.
2. **What the VPS fixes:** home-network drops, macOS GUI login quirks (the web
   login window IBC can't click), and depending on a machine left unattended
   for weeks. It also makes the remaining manual re-auth cases fixable from a
   phone (§7).

### Success criteria (2-week burn-in)

- ≥ 95% of scheduled TSMOM runs complete (`done`, `verified: true`)
- No bot disconnected > 15 min outside the Gateway restart window
- Zero manual interventions except IBKR-forced re-auth, each fixed remotely in < 15 min

---

## 2. Scope

| Component | Decision |
|---|---|
| IB Gateway + IBC | Move |
| `tsmom`, `poc_futures` (dry), `sweep_futures` (dry) | Move |
| `commander` + deploy / digest / weekly / monthly / execution audit | Move |
| `crypto_scalper` (Binance paper) | Move — EU region (Binance doesn't serve US IPs) |
| Alpaca stock bot (`main.py`, `run_bot.sh`, launchd `trading-bot`) | **Decision needed:** migrate or retire |
| `stock_screener` | No change (GitHub Actions) |
| Research dirs, `*/data/` caches | Not run on the server |
| Home Mac | Dev/research only after cutover. **Must never run Gateway or commander again** |

---

## 3. Target architecture

- **Provider:** Hetzner Cloud or UpCloud, Helsinki. 2 vCPU / 4 GB RAM / 40 GB,
  roughly €5–10/month (verify current pricing). 4 GB because the Gateway JVM
  runs with `-Xmx768m` plus five long-running Python processes. Add 2 GB swap.
- **OS:** Ubuntu 24.04 LTS, timezone `Europe/Helsinki` — every existing
  schedule transfers unchanged. Strategy code already uses `America/New_York`
  explicitly wherever session times matter.
- **User:** `trader`, with `loginctl enable-linger trader`. All jobs are
  **systemd user units**, so commander controls them with `systemctl --user`
  and no sudo — the same model as today's launchd `gui/<uid>` domain.
- **Python:** 3.13 via `uv`; venv built from a lockfile generated from the Mac
  venv (`pip freeze > requirements-lock.txt`). Key pins: pandas 3.0.2,
  numpy 2.2.6, ib-insync 0.9.86, ccxt 4.5.51, alpaca-py 0.43.2.
- **Gateway:** IB Gateway 10.46 Linux (stable offline installer) + IBC Linux,
  headless on Xvfb display `:1`, `x11vnc` attached to `:1` for manual re-auth.
- **Remote access:** Tailscale on the server, phone and Mac. Public SSH closed;
  SSH and VNC reachable only over the tailnet.
- **Firewall:** ufw default-deny incoming. Port 4002 never exposed (Gateway
  trusted IP `127.0.0.1` only).
- **Updates:** unattended security upgrades; reboots only via a Saturday timer
  when `/var/run/reboot-required` exists.
- **Secrets:** `.env` → `~/trading-bots/.env` (mode 600). IBKR credentials
  replace the Keychain → `~/.config/trading/ibkr.env` (600), loaded by
  `EnvironmentFile=`.
- **Code delivery:** unchanged — `deploy_pull.py` every 5 min; repo cloned with
  a read-only GitHub deploy key.
- **Versioned infra:** unit files and Linux scripts live in the repo under
  `deploy/linux/`.

---

## 4. launchd → systemd mapping

| launchd label | Runs | systemd user unit | Schedule / policy |
|---|---|---|---|
| `ibcgateway` | IBC → Gateway | `xvfb.service` + `ibgateway.service` | always on, `Restart=always` |
| `ibchealthcheck` | `healthcheck.sh` at 8 fixed times | `gateway-watchdog.timer` | **every 5 min** |
| `tsmomdaily` | `tsmom/live_daily.py` | `tsmom.timer` | daily 16:00 (as today), `Persistent=true` |
| `pocfutures` | `poc_futures/live.py --dry` | `poc.service` | always on, `Restart=on-failure` |
| `sweepfutures` | `sweep_futures/live.py --dry` | `sweep.service` | always on, `Restart=on-failure` |
| `cryptoscalper` | `crypto_scalper/live.py` | `crypto.service` | always on, `Restart=on-failure` |
| `botcommander` | `commander/commander.py` | `commander.service` | always on, `Restart=on-failure` |
| `botdeploy` | `commander/deploy_pull.py` | `deploy.timer` | every 5 min |
| `botdigest` | `commander/digest.py` | `digest.timer` | Mon–Sat 09:00 |
| `botweekly` | `commander/weekly_report.py` | `weekly.timer` | Sun 18:00 |
| `botmonthly` | `commander/monthly_review.py` | `monthly.timer` | 1st of month 18:15 |
| `executionaudit` | `commander/execution_audit.py --collect` | `execution-audit.timer` | every 4 h at :20 (00:20, 04:20, …) |
| `trading-bot` | `run_bot.sh` (Alpaca) | `alpaca.timer` — or retire | Mon–Fri 08:00 |

Keep today's log filenames with `StandardOutput=append:<dir>/launchd.out.log`
so `commander logs <bot>` and the digest keep working unchanged.

### Example units

`~/.config/systemd/user/ibgateway.service`
```ini
[Unit]
Description=IB Gateway (paper) via IBC
Requires=xvfb.service
After=xvfb.service

[Service]
EnvironmentFile=%h/.config/trading/ibkr.env
Environment=DISPLAY=:1
ExecStart=%h/ibc/gatewaystart.sh -inline
Restart=always
RestartSec=30
StandardOutput=append:%h/ibc/launchd.out.log
StandardError=append:%h/ibc/launchd.err.log

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/poc.service`
```ini
[Unit]
Description=POC futures bot
Wants=ibgateway.service
After=ibgateway.service
StartLimitIntervalSec=0

[Service]
WorkingDirectory=%h/trading-bots/poc_futures
EnvironmentFile=%h/trading-bots/state/poc.env   # BOT_ARGS=--dry
ExecStart=%h/trading-bots/.venv/bin/python live.py $BOT_ARGS
Restart=on-failure
RestartSec=20
StandardOutput=append:%h/trading-bots/poc_futures/launchd.out.log
StandardError=append:%h/trading-bots/poc_futures/launchd.err.log

[Install]
WantedBy=default.target
```

`~/.config/systemd/user/tsmom.timer`
```ini
[Timer]
OnCalendar=*-*-* 16:00:00
Persistent=true      # a run missed during downtime fires on boot; the rebalancer is idempotent

[Install]
WantedBy=timers.target
```

Bots use `Wants=` not `Requires=` on the Gateway, so a Gateway restart never
stops them.

---

## 5. Code changes — one PR, backwards-compatible, deployed to the Mac first

### 5a. Portability (everything macOS-specific in the repo, from a full grep)

| File | Today | Change |
|---|---|---|
| `commander/commander.py` 50, 100–101 | `LAUNCH_AGENTS`, `GATEWAY_PLIST`, `IBC_DIR` under `~/Library` | come from a new `commander/supervisor.py` / env |
| `commander/commander.py` 133–175, 264–268, 353–359 | `launchctl print / bootout / bootstrap / kickstart` | `supervisor.status / stop / start / restart(bot)`, launchd and systemd backends picked by platform |
| `commander/commander.py` `bot_mode`, `set_dry` | dry/live flag lives in plist `ProgramArguments` | backend-specific: launchd keeps editing the plist; systemd writes `state/<bot>.env` (`BOT_ARGS=--dry`) and restarts |
| `commander/digest.py` 54 | `launchctl print` | `supervisor.status` |
| `commander/deploy_pull.py` 90 | `launchctl kickstart -k` | `supervisor.restart` |
| `commander/digest.py` 29, `tsmom/live_daily.py` 101 | hardcoded `~/IBC/...` | `IBC_DIR` env var, default `~/IBC` |
| `run_bot.sh` | `cd "/Users/villerajala/Trading bot"` | `cd "$(dirname "$0")"` — or retire with the Alpaca bot |
| `~/IBC/start_gateway_paper.sh`, `~/IBC/healthcheck.sh` | Keychain, `gatewaystartmacos.sh`, `launchctl`, `/usr/sbin/lsof` | Linux versions in `deploy/linux/`: `EnvironmentFile`, `gatewaystart.sh`, `systemctl --user`, `ss -ltn` |

Nothing else in the bots touches macOS.

### 5b. Reliability (driven by §1 — worth shipping on the Mac even before the move)

1. **One owner of Gateway restarts: the watchdog.** Today the health check,
   TSMOM's `connect_with_recovery()` (which calls `healthcheck.sh`) and
   commander's `gateway restart` can all recycle Gateway independently.
   Overlapping re-logins are a plausible trigger for IBKR's transient
   "Unrecognized" rejections (36 seen) — a hypothesis, to be checked by
   watching whether rejections drop. After the change, bots only retry the
   connection; commander's `gateway restart` calls the watchdog script.
2. **Bots exit instead of looping.** After ~10 minutes of consecutive failed
   reconnects, exit non-zero; systemd (or launchd `KeepAlive`) restarts the
   process with a fresh API client. Replaces the in-process loops behind the
   4,270 `reconnect_failed` events.
3. **Watchdog every 5 minutes**, not at 8 fixed times — closes the 04:00→08:00
   blind spot.
4. **Move IBC `AutoRestartTime` from 04:00 to 00:10 Helsinki.** That's inside
   CME's daily halt (17:00–18:00 ET) for most of the year, so the restart
   happens while futures aren't trading. In the ~4 weeks a year when US and EU
   daylight-saving dates differ, it lands 10 minutes after the halt ends —
   harmless once bots recover in minutes.

---

## 6. Gateway on Linux — lessons carried over from the Mac

- IBC's `gatewaystart.sh` has hardcoded `TWSUSERID=` / `TWSPASSWORD=` lines
  that **overwrite exported env vars** (the trap that left the Mac login fields
  empty). Change them to `TWSUSERID=${TWSUSERID}` / `TWSPASSWORD=${TWSPASSWORD}`.
- Keep these `config.ini` settings: `TradingMode=paper`,
  `OverrideTwsApiPort=4002`, `ReadOnlyApi=no`,
  `AcceptIncomingConnectionAction=accept`,
  `ExistingSessionDetectedAction=primary`,
  `AcceptNonBrokerageAccountWarning=yes`.
- IBC passes the password on the java command line (visible in `ps`), same as
  on the Mac. Acceptable on a single-user box for a paper account.
- The first login from a datacenter IP may trigger an IBKR new-device or
  unusual-location check. Watch it over VNC.
- Positions from `ib.positions()` have an empty `exchange` field — any
  close/flatten built from them must re-qualify the contract first (already
  fixed in `tsmom/live_daily.py`; keep it that way).

---

## 7. Manual re-auth from anywhere

`x11vnc` runs on display `:1` listening only on the server's Tailscale IP, with
a VNC password. From a phone: Tailscale app + any VNC viewer → the Gateway
window. The remaining cases IBKR forces (weekly re-auth, login rejections)
become a few minutes on the phone instead of waiting until you're home.

---

## 8. Cutover plan

**Prerequisite — clean git tree.** About 20 modified files plus new modules
(`governor`, `execution_audit`, `stress_test`, …) from other sessions are
uncommitted, so `deploy_pull.py` has paused auto-deploy. Nothing from this
spec reaches either machine until that's committed.

| Phase | What | Where | Time | You needed? |
|---|---|---|---|---|
| 0 | Portability + reliability PR (§5), deployed to the Mac, run a week | repo → Mac | ~½ day | review |
| 1 | Provision + harden: user, linger, Tailscale, ufw, updates, swap, TZ | server | 1 h | account + payment |
| 2 | Install Xvfb, x11vnc, Gateway 10.46, IBC; build venv; install units (not started) | server | 1–2 h | no |
| 3 | Cutover — a Saturday after the Sep 21–22 ZN/CL rolls | both | 1–2 h | yes (laptop, remote OK) |
| 4 | Burn-in 2 weeks, with weekend kill-tests | server | — | read digests |

### Phase 3 steps, in order

1. Commander `pause` every bot on the Mac, then `launchctl bootout` every
   `com.villerajala.*` job and move the plists to
   `~/Library/LaunchAgents/archive/` (don't delete — rollback).
2. Confirm the Mac's Gateway is down (port 4002 closed). **Never run two
   Gateways on the same IBKR username** — with
   `ExistingSessionDetectedAction=primary` they log each other out in a loop.
3. rsync state + secrets over the tailnet (≈3 MB):
   `.env`, `status.json`, `commander/.offset`,
   `commander/execution_audit.jsonl`, `commander/execution_audit_state.json`,
   `tsmom/.rebalancer_state.json`, `tsmom/daily_log.jsonl`,
   `poc_futures/state.json`, `poc_futures/live_log.jsonl`,
   `sweep_futures/state.json`, `sweep_futures/live_log.jsonl`,
   `crypto_scalper/paper_state.json`, `crypto_scalper/runner_state.json`,
   `crypto_scalper/journal/`, `journal/`.
   Don't copy `tsmom/.rebalancer.lock`.
4. Start `xvfb` and `ibgateway`; watch the first login over VNC; confirm 4002 is listening.
5. Start `commander`. **Only one commander may poll the bot token** — two cause
   Telegram `409 Conflict` errors and double-executed commands.
6. Run `tsmom/live_daily.py --dry-run` once and compare targets with the Mac's
   last run; then enable all timers and bot services.
7. From Telegram: `status`, `positions`, `gateway` — all green.

**Positions:** TSMOM's futures live at IBKR, not on the machine — nothing to
move. Just don't cut over on a day a contract roll is due.

**Rollback** (keep it possible for 2 weeks, ~15 min): stop the server units →
rsync state back → move plists out of `archive/` → `launchctl bootstrap` each.

### Burn-in kill tests (weekend)

Each must self-recover with at most one Telegram message:
- `kill -9` the Gateway java process
- `systemctl --user stop ibgateway` for 20 minutes, then start
- reboot the server
- drop the network for 5 minutes

---

## 9. Cost and effort

- Server ~€5–10/month; Tailscale personal plan is free.
- Claude: the §5 PR, unit files and Linux scripts — about half a day.
- You: ~3–4 hours across phases 1–3, mostly waiting on installs and the first login.

---

## 10. Open decisions

1. **Provider / region** — recommend Hetzner or UpCloud in Helsinki.
2. **Alpaca stock bot** — migrate or retire?
3. **Access** — Tailscale-only (recommended) or public SSH with keys.
4. **Rollback window** — keep the Mac rollback-ready for 2 weeks (recommended).
5. **Cutover date** — a Saturday after Sep 22.

---

## 11. What this won't fix

- It won't tell us whether any strategy has an edge — it makes the forward
  test valid.
- IBKR can still force a re-auth or reject a login. This makes those fixable
  from a phone in minutes.
- `ib_insync` 0.9.86 is unmaintained (successor: `ib_async`). Not needed for
  the move; worth doing later.
