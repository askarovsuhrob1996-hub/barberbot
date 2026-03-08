# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup & Running

```bash
# Create and activate venv
python -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure credentials (copy template, then fill in real values)
cp .env.example .env

# Run the bot in the foreground
python bot.py

# Run in background (screen session named "barberbot")
screen -dmS barberbot bash -c "source .venv/bin/activate && python bot.py >> bot.log 2>&1"

# Restart (kill ALL old processes and screen sessions to avoid Telegram getUpdates conflict)
pkill -9 -f "python bot.py"; pkill -9 SCREEN; sleep 3; screen -dmS barberbot bash -c "source .venv/bin/activate && python bot.py >> bot.log 2>&1"

# View logs (RotatingFileHandler: max 10 MB per file, 5 backups)
tail -f bot.log

# Run miniapp dev server
cd miniapp && npm install && npm run dev

# Run FastAPI backend (reads barber.db, services.json, schedule_config.json)
uvicorn backend.api:app --host 127.0.0.1 --port 8000
```

`.env` requires: `BOT_TOKEN` (from @BotFather), `BARBER_CHAT_ID` (barber's Telegram user/chat ID). Optional: `MINIAPP_ENABLED=true` and `MINIAPP_URL` to activate the WebApp button.

## Architecture

**Three components** share a single SQLite database (`barber.db`) and JSON configs:

| Component | Path | Tech | Purpose |
|---|---|---|---|
| Telegram Bot | `bot.py` (~2200 lines) | python-telegram-bot 21.5 | Core booking flow, barber approval, reminders |
| FastAPI Backend | `backend/api.py` | FastAPI + uvicorn | REST API for the mini app (read-only slot queries + booking creation) |
| Mini App | `miniapp/` | React 19 + Vite + Tailwind + @twa-dev/sdk | Telegram WebApp UI for customers |

The bot and backend share identical slot-calculation logic but are **not** importing from each other — changes to slot logic must be replicated in both `bot.py` and `backend/api.py`.

### Data stores (module-level globals in bot.py)

| Variable | Type | Purpose |
|---|---|---|
| `appointments` | `dict[str, dict]` | Confirmed bookings. Key = `"YYYY-MM-DD HH:MM"` |
| `pending_bookings` | `dict[int, dict]` | Awaiting barber approval. Key = auto-increment int |
| `customer_cache` | `dict[int, dict]` | Remembers `name`, `phone`, `lang` per user ID |
| `schedule_config` | `dict` | Working hours/days. Loaded from `schedule_config.json` on startup, saved on every barber change |

### Booking flow (FSM)

`ConversationHandler` with 7 states: `STATE_LANG → STATE_DATE → STATE_TIME → STATE_NAME → STATE_PHONE → STATE_SERVICES → STATE_CONFIRM`

Returning customers (cached name+phone) skip NAME and PHONE, going straight `STATE_TIME → STATE_SERVICES`.

On `STATE_CONFIRM` → `confirm_yes`: booking enters `pending_bookings`. The barber gets an approval message. On barber approve → moves to `appointments` + both customer and barber reminder jobs scheduled. On barber reject or user cancel → removed, slot freed.

### Handler registration order (critical)

In `build_application()`, handlers are registered in this order, which determines priority:

1. `ConversationHandler` (entry: `/start`, fallbacks include `/cancel` and `/settings`)
2. Barber-only global callbacks: `approve/reject_N`, `bconfirm_DATE_TIME`, `bcancel_DATE_TIME`
3. Language change: `setlang_(ru|uz)`
4. Customer commands: `/bookings`, `/week`, `/settings`, `/config`, `/mybooking`, `/cancel`
5. User booking cancel: `ucancel_DATE_TIME`
6. Reschedule flow: `uresch_/urdate_/urtime_/urconfirm/urback/urback_date`
7. Config UI: `cfg_*`
8. No-op (non-clickable header buttons): `^noop$`
9. Flow cancel fallback: `^cancel$`

**Critical pattern rule**: All state handlers inside the ConversationHandler use narrow `pattern=` regexes so unrelated callbacks (e.g. `setlang_`, `cfg_`, `approve_`) fall through to global handlers.

### Translations & services

`STRINGS` dict has `"ru"` and `"uz"` sub-dicts with identical keys. `tx(uid, key, **kwargs)` resolves the user's stored language and formats the string.

`SERVICES` dict is loaded at startup from **`services.json`** via `_load_services()`. Each entry has `ru`/`uz` (barber-facing), `ru_c`/`uz_c` (client-facing), `mins`, and `price_uzs`. Use `_svc_label()` for barber display, `_svc_client_label()` for customer display. `_calc_total_price()` sums `price_uzs` for selected services; `_price_line()` formats it for display. Edit `services.json` to add/rename services or change prices without touching `bot.py`.

### 30-minute slot logic

Slots are 30 min each. `_calc_duration(service_ids)` → `(total_mins, n_slots)` where `n_slots = math.ceil(mins / 30)`. `_can_fit(for_date, start_time, n_slots)` checks if `n_slots` consecutive 30-min slots starting at `start_time` (`"HH:MM"`) are all free and within `end_hour`. `_all_taken_slots()` expands every booking by its `duration_slots × 30 min`. `_available_slots()` iterates from `start_hour:00` to `end_hour - 30 min` in 30-min increments.

Slot keys use format `"YYYY-MM-DD HH:MM"` where MM is `00` or `30`.

### Reminder & timeout jobs

- **Customer reminder** (`reminder_{slot_key}`): 30 min before appointment.
- **Barber reminder** (`barber_reminder_{slot_key}`): 30 min before appointment.
- **Pending timeout** (`pending_timeout_{bid}`): 30 min after customer confirms; auto-rejects if barber hasn't acted.

All jobs are scheduled on booking events and restored from DB on restart via `_post_init`.

### Barber cancel confirmation

Two-step: `bconfirm_{enc}` shows "Are you sure?" → `bcancel_{enc}` actually cancels and notifies customer.

### Customer reschedule flow

`/mybooking` shows confirmed bookings with reschedule/cancel buttons. Reschedule is a chain of global callbacks outside `ConversationHandler`: `uresch_` → `urdate_` → `urtime_` → `urconfirm`. `_date_keyboard` and `_time_keyboard` accept prefix/back/cancel/exclude params so both normal booking and reschedule reuse the same builders.

### Backend API endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/api/services` | List services from services.json |
| GET | `/api/dates` | Working dates with free slot counts |
| GET | `/api/slots?date=YYYY-MM-DD` | Free time slots for a date |
| GET | `/api/bookings?user_id=N` | Customer's bookings (confirmed + pending) |
| POST | `/api/booking` | Create pending booking, notify barber via Bot API |

The backend writes directly to `barber.db`; the bot picks up mini app bookings on next approve/reject cycle.

## Deployment

**Target**: Ubuntu VPS at `/home/ubuntu/barberbot`. Two systemd services: `barberbot` (the Telegram bot) and `barberapi` (FastAPI on port 8000). Nginx reverse-proxies the API and serves `miniapp/dist/` as static files.

**CI/CD**: `.github/workflows/deploy.yml` — on push to `main`: builds miniapp (Node 20), SSH into VPS, `git pull`, `pip install`, rsync miniapp dist, restart both systemd services. Secrets: `DEPLOY_SSH_KEY`, `DEPLOY_HOST`, `DEPLOY_USER`.

**First-time VPS setup**: `bash deploy/setup-vps.sh` (installs systemd service, prints nginx/SSL instructions).

## Working hours config

`schedule_config.json` stores `start_hour`, `end_hour`, and `work_days` (list of weekday ints, 0=Mon). Loaded at startup, written on every barber `/config` change. `DAYS_AHEAD = 14` controls how far ahead dates are shown.
