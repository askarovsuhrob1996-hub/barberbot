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
```

`.env` requires two keys: `BOT_TOKEN` (from @BotFather) and `BARBER_CHAT_ID` (the barber's Telegram user/chat ID).

## Architecture

Everything lives in a single file: **`bot.py`** (~2200 lines). SQLite (`barber.db`) provides persistence — in-memory dicts are the working copy, DB is the write-through store.

### Data stores (module-level globals)

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

### Reminder jobs

Two reminder jobs per confirmed booking:
- **Customer reminder** (`reminder_{slot_key}`): `_schedule_reminder()` / `_cancel_reminder()` — sends customer the `reminder` string 30 min before.
- **Barber reminder** (`barber_reminder_{slot_key}`): `_schedule_barber_reminder()` / `_cancel_barber_reminder()` — sends barber a concise appointment summary 30 min before.

Both are scheduled on barber approve, cancelled on any cancellation, and rescheduled from DB on restart via `_post_init`.

### Pending timeout jobs

When a customer confirms a booking (`cb_confirm`), `_schedule_pending_timeout()` registers a job named `pending_timeout_{bid}` to fire in 30 minutes. If the barber hasn't acted, `_pending_timeout_job` auto-rejects, notifies customer (`pending_timeout` string), and edits the barber's approval message (using stored `barber_msg_id`) to remove stale buttons. On restart, `_post_init` re-schedules timeout jobs for pending bookings loaded from DB (firing in 5 s if already past deadline).

### Barber cancel confirmation

When barber taps ❌ on a confirmed booking in `/bookings`, the flow is two-step:
1. `bconfirm_{enc}` → `cb_barber_confirm_cancel` shows "Are you sure?" with ✅ Yes / ← Back
2. `bcancel_{enc}` → `cb_barber_cancel_booking` actually cancels, notifies customer, returns to booking list

### Customer reschedule flow

`/mybooking` shows confirmed bookings with **🔄 Перенести** (`uresch_{enc}`) and **❌ Отменить** (`ucancel_{enc}`). The reschedule is a chain of global callbacks outside `ConversationHandler`:

`uresch_` → stores `reschedule_old_slot` in `user_data`, shows date picker (`urdate_` prefix)
`urdate_` → stores new date, shows time picker (`urtime_` prefix, `exclude_slot_key=old_slot` so old slot appears free)
`urtime_` → stores new time, shows confirmation
`urconfirm` → pops old booking, cancels its reminders, checks `_can_fit`, creates new pending + notifies barber
`urback` / `urback_date` → restore previous views

`_date_keyboard` and `_time_keyboard` accept `date_prefix`, `time_prefix`, `back_data`, `cancel_data`, `exclude_slot_key` so both normal booking and reschedule flow reuse the same builders.

### Working hours config

`schedule_config.json` stores `start_hour`, `end_hour`, and `work_days` (list of weekday ints, 0=Mon). Loaded at startup, written on every barber `/config` change. `DAYS_AHEAD = 14` controls how far ahead dates are shown.
