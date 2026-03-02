"""
Barber Shop Mini App — FastAPI backend
Читает из barber.db и schedule_config.json / services.json
Слот-логика идентична bot.py
"""

import json
import math
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Пути ─────────────────────────────────────────────────────────────────────
BASE       = Path(__file__).parent.parent          # корень проекта
DB_PATH    = BASE / "barber.db"
SVC_PATH   = BASE / "services.json"
CFG_PATH   = BASE / "schedule_config.json"

TZ         = ZoneInfo("Asia/Tashkent")
DAYS_AHEAD = 14

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="Barber Mini App API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],       # для разработки; на проде — конкретный домен
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def load_services() -> dict:
    with open(SVC_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_config() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)

def load_appointments() -> dict[str, dict]:
    """Загружает все подтверждённые записи из DB."""
    with get_db() as conn:
        rows = conn.execute("SELECT slot_key, data FROM bookings").fetchall()
    return {r["slot_key"]: json.loads(r["data"]) for r in rows}

def load_pending() -> dict[int, dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT bid, data FROM pending").fetchall()
    return {r["bid"]: json.loads(r["data"]) for r in rows}

# ── Slot logic (mirrors bot.py) ───────────────────────────────────────────────
def calc_duration(service_ids: list[str], services: dict) -> tuple[int, int]:
    """(total_mins, n_slots)"""
    total = sum(services[s]["mins"] for s in service_ids if s in services)
    return total, math.ceil(total / 30)

def all_taken_slots(appointments: dict, services: dict) -> set[str]:
    """Все занятые слоты с учётом длительности записей."""
    taken = set()
    for slot_key, bk in appointments.items():
        svc_ids = bk.get("services", [])
        _, n = calc_duration(svc_ids, services)
        n = max(n, bk.get("duration_slots", 1))
        dt = datetime.strptime(slot_key, "%Y-%m-%d %H:%M")
        for i in range(n):
            t = dt + timedelta(minutes=30 * i)
            taken.add(t.strftime("%Y-%m-%d %H:%M"))
    return taken

def available_slots_for_date(
    for_date: date,
    appointments: dict,
    pending: dict,
    services: dict,
    cfg: dict,
) -> list[str]:
    """Возвращает список доступных временных слотов ('HH:MM') для даты."""
    start_h = cfg["start_hour"]
    end_h   = cfg["end_hour"]
    taken   = all_taken_slots(appointments, services)

    # pending тоже занимают слоты
    for bk in pending.values():
        slot_key = bk.get("slot_key", "")
        if not slot_key:
            continue
        svc_ids = bk.get("services", [])
        _, n = calc_duration(svc_ids, services)
        n = max(n, bk.get("duration_slots", 1))
        try:
            dt = datetime.strptime(slot_key, "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        for i in range(n):
            t = dt + timedelta(minutes=30 * i)
            taken.add(t.strftime("%Y-%m-%d %H:%M"))

    slots = []
    current = datetime.combine(for_date, datetime.min.time()).replace(
        hour=start_h, minute=0, tzinfo=TZ
    )
    end = current.replace(hour=end_h, minute=0)
    while current < end:
        sk = current.strftime("%Y-%m-%d %H:%M")
        if sk not in taken:
            slots.append(current.strftime("%H:%M"))
        current += timedelta(minutes=30)
    return slots

def working_dates() -> list[date]:
    cfg = load_config()
    work_days = set(cfg["work_days"])
    today = datetime.now(tz=TZ).date()
    result = []
    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        if d.weekday() in work_days:
            result.append(d)
    return result

# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/services")
def get_services():
    """Список услуг из services.json."""
    raw = load_services()
    return [
        {
            "id":       sid,
            "name_ru":  s["ru_c"],
            "name_uz":  s["uz_c"],
            "mins":     s["mins"],
            "price":    s["price_uzs"],
        }
        for sid, s in raw.items()
    ]


@app.get("/api/dates")
def get_dates():
    """Рабочие даты на ближайшие 14 дней с количеством свободных слотов."""
    appointments = load_appointments()
    pending      = load_pending()
    services     = load_services()
    cfg          = load_config()

    day_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    month_names = ["янв","фев","мар","апр","май","июн",
                   "июл","авг","сен","окт","ноя","дек"]
    today = datetime.now(tz=TZ).date()

    result = []
    for d in working_dates():
        slots = available_slots_for_date(d, appointments, pending, services, cfg)
        result.append({
            "date":    d.isoformat(),
            "day":     day_names[d.weekday()],
            "num":     d.day,
            "month":   month_names[d.month - 1],
            "is_today": d == today,
            "free":    len(slots),
        })
    return result


@app.get("/api/slots")
def get_slots(date: str = Query(..., description="YYYY-MM-DD")):
    """Свободные временные слоты для конкретной даты."""
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Неверный формат даты. Ожидается YYYY-MM-DD")

    appointments = load_appointments()
    pending      = load_pending()
    services     = load_services()
    cfg          = load_config()

    slots = available_slots_for_date(d, appointments, pending, services, cfg)
    return {"date": date, "slots": slots}


@app.get("/api/bookings")
def get_user_bookings(user_id: int = Query(...)):
    """Записи конкретного клиента (confirmed + pending)."""
    appointments = load_appointments()
    pending      = load_pending()

    result = []

    for slot_key, bk in appointments.items():
        if bk.get("user_id") != user_id:
            continue
        result.append({
            "id":       slot_key,
            "date_str": bk.get("date_str", slot_key.split()[0]),
            "time_str": bk.get("time_range", slot_key.split()[1]),
            "services": bk.get("services", []),
            "price":    bk.get("total_price", 0),
            "status":   "confirmed",
        })

    for bid, bk in pending.items():
        if bk.get("user_id") != user_id:
            continue
        slot_key = bk.get("slot_key", "")
        result.append({
            "id":       slot_key,
            "date_str": bk.get("date_str", ""),
            "time_str": bk.get("time_range", ""),
            "services": bk.get("services", []),
            "price":    bk.get("total_price", 0),
            "status":   "pending",
        })

    result.sort(key=lambda x: x["id"])
    return result


class BookingRequest(BaseModel):
    user_id:    int
    date:       str   # YYYY-MM-DD
    time:       str   # HH:MM
    service_ids: list[str]
    name:       str
    phone:      str
    lang:       str = "ru"


@app.post("/api/booking")
def create_booking(body: BookingRequest):
    """
    Создаёт pending-запись и уведомляет мастера через бот.
    Данные пишутся напрямую в DB — бот подхватывает их при следующем approve.
    """
    services = load_services()

    # валидация услуг
    for sid in body.service_ids:
        if sid not in services:
            raise HTTPException(400, f"Неизвестная услуга: {sid}")

    slot_key = f"{body.date} {body.time}"

    # проверяем что слот ещё свободен
    appointments = load_appointments()
    pending      = load_pending()
    cfg          = load_config()
    d = datetime.strptime(body.date, "%Y-%m-%d").date()
    free = available_slots_for_date(d, appointments, pending, services, cfg)
    if body.time not in free:
        raise HTTPException(409, "Слот уже занят. Выберите другое время.")

    total_mins, n_slots = calc_duration(body.service_ids, services)
    total_price = sum(services[s]["price_uzs"] for s in body.service_ids)

    svc_names_ru = ", ".join(services[s]["ru"] for s in body.service_ids)
    day_names = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
    month_names = ["января","февраля","марта","апреля","мая","июня",
                   "июля","августа","сентября","октября","ноября","декабря"]
    dt = datetime.strptime(body.date, "%Y-%m-%d")
    date_str = f"{day_names[dt.weekday()]} {dt.day} {month_names[dt.month-1]}"

    start_dt = datetime.strptime(slot_key, "%Y-%m-%d %H:%M")
    end_dt   = start_dt + timedelta(minutes=total_mins)
    time_range = f"{body.time}–{end_dt.strftime('%H:%M')}"

    bk_data = {
        "user_id":       body.user_id,
        "chat_id":       body.user_id,
        "slot_key":      slot_key,
        "date_str":      date_str,
        "time_range":    time_range,
        "name":          body.name,
        "phone":         body.phone,
        "user_lang":     body.lang,
        "services":      body.service_ids,
        "duration_mins": total_mins,
        "duration_slots": n_slots,
        "total_price":   total_price,
        "source":        "miniapp",
        "booked_at":     datetime.now(tz=TZ).isoformat(),
    }

    # записываем в DB
    with get_db() as conn:
        # получаем следующий bid
        row = conn.execute("SELECT MAX(bid) FROM pending").fetchone()
        bid = (row[0] or 0) + 1
        conn.execute(
            "INSERT INTO pending (bid, data) VALUES (?, ?)",
            (bid, json.dumps(bk_data, ensure_ascii=False))
        )
        conn.commit()

    # уведомляем мастера через Bot API
    _notify_barber(bid, bk_data, services)

    return {"ok": True, "bid": bid, "slot_key": slot_key, "time_range": time_range}


def _notify_barber(bid: int, bk: dict, services: dict):
    """Шлёт уведомление мастеру через Telegram Bot API."""
    import urllib.request
    from urllib.error import URLError

    bot_token    = os.getenv("BOT_TOKEN", "")
    barber_id    = os.getenv("BARBER_CHAT_ID", "")
    if not bot_token or not barber_id:
        return

    svc_lines = []
    for sid in bk.get("services", []):
        s = services.get(sid, {})
        svc_lines.append(f"{s.get('ru','?')} — {s.get('price_uzs',0):,} сум".replace(",", " "))

    text = (
        f"📱 <b>Заявка из Mini App!</b>\n\n"
        f"📅 {bk['date_str']}\n"
        f"🕐 {bk['time_range']}\n"
        f"👤 {bk['name']}\n"
        f"📞 {bk['phone']}\n"
        f"✂️ {chr(10).join(svc_lines)}\n"
        f"⏱ ~{bk['duration_mins']} мин.\n"
        f"💰 {bk['total_price']:,} сум\n\n".replace(",", " ") +
        f"<i>Одобрение через /bookings или кнопку ниже</i>"
    )

    kb = json.dumps({
        "inline_keyboard": [[
            {"text": "✅ Одобрить",  "callback_data": f"approve_{bid}"},
            {"text": "❌ Отклонить", "callback_data": f"reject_{bid}"},
        ]]
    })

    payload = json.dumps({
        "chat_id":    barber_id,
        "text":       text,
        "parse_mode": "HTML",
        "reply_markup": kb,
    }).encode()

    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except URLError as e:
        print(f"[notify_barber] failed: {e}")
