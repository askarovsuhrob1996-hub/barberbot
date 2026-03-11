"""
Barber Shop Appointment Scheduling Bot
=======================================
python-telegram-bot v20+  |  In-memory storage  |  GMT+5 (Asia/Tashkent)

Customer flow:
  /start → choose language (new users only)
         → pick date → pick time → enter name (new users)
         → share phone (new users) → select services
         → confirm → wait for barber approval

Barber commands  (only for BARBER_CHAT_ID):
  /bookings  — today's schedule + ❌ Cancel buttons per confirmed booking
  /week      — read-only 7-day overview

Customer commands:
  /start     — book an appointment
  /settings  — change language (works at any time, incl. mid-conversation)
  /cancel    — cancel current booking flow
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import math
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import (
    Contact,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ─────────────────────────── Logging ─────────────────────────────────────────
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_LOG_FILE   = Path(__file__).parent / "bot.log"

_root = logging.getLogger()
_root.setLevel(logging.INFO)
_root.addHandler(logging.StreamHandler())
_root.addHandler(
    logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
)
for _h in _root.handlers:
    _h.setFormatter(logging.Formatter(_LOG_FORMAT))

logger = logging.getLogger(__name__)

# ─────────────────────────── Config ──────────────────────────────────────────
load_dotenv()

BOT_TOKEN:       str  = os.environ["BOT_TOKEN"]
# Comma-separated list of barber chat IDs (first one is primary — receives notifications)
_barber_raw = os.environ["BARBER_CHAT_ID"]
BARBER_CHAT_IDS: set[int] = {int(x.strip()) for x in _barber_raw.split(",") if x.strip()}
BARBER_CHAT_ID:  int  = int(_barber_raw.split(",")[0].strip())  # primary barber
MINIAPP_URL:     str  = os.getenv("MINIAPP_URL", "")
MINIAPP_ENABLED: bool = os.getenv("MINIAPP_ENABLED", "false").lower() == "true"


def _is_barber(uid: int) -> bool:
    return uid in BARBER_CHAT_IDS


async def _send_to_all_barbers(bot, **kwargs):
    """Send a message to all barbers. Returns the message sent to the primary barber."""
    primary_msg = None
    for bid in BARBER_CHAT_IDS:
        try:
            msg = await bot.send_message(chat_id=bid, **kwargs)
            if bid == BARBER_CHAT_ID:
                primary_msg = msg
        except Exception as exc:
            logger.error("Send to barber %d failed: %s", bid, exc)
    return primary_msg

TZ         = ZoneInfo("Asia/Tashkent")   # UTC+5
DAYS_AHEAD = 14

# Mutable working-hours config — barber can change these via /config
_CONFIG_FILE = Path(__file__).parent / "schedule_config.json"
_DB_FILE     = Path(__file__).parent / "barber.db"

_CONFIG_DEFAULTS: dict[str, Any] = {
    "start_hour": 9,
    "end_hour":   18,
    "work_days":  [0, 1, 2, 3, 4, 5],   # stored as list in JSON, 0=Mon…6=Sun
}


def _load_config() -> dict[str, Any]:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text())
            return {
                "start_hour": int(data["start_hour"]),
                "end_hour":   int(data["end_hour"]),
                "work_days":  set(data["work_days"]),
            }
        except Exception as exc:
            logger.warning("Could not load schedule config: %s — using defaults", exc)
    return {**_CONFIG_DEFAULTS, "work_days": set(_CONFIG_DEFAULTS["work_days"])}


def _save_config() -> None:
    data = {
        "start_hour": schedule_config["start_hour"],
        "end_hour":   schedule_config["end_hour"],
        "work_days":  sorted(schedule_config["work_days"]),   # set → sorted list
    }
    _CONFIG_FILE.write_text(json.dumps(data, indent=2))
    logger.info("Schedule config saved: %s", data)


schedule_config: dict[str, Any] = _load_config()


# ─────────────────────────── SQLite persistence ──────────────────────────────

def _init_db() -> None:
    with sqlite3.connect(_DB_FILE) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                slot_key TEXT PRIMARY KEY,
                data     TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                bid  INTEGER PRIMARY KEY,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS customers (
                user_id INTEGER PRIMARY KEY,
                name    TEXT,
                phone   TEXT,
                lang    TEXT DEFAULT 'ru'
            );
            CREATE TABLE IF NOT EXISTS blocked_slots (
                slot_key TEXT PRIMARY KEY
            );
        """)
    logger.info("DB initialised: %s", _DB_FILE)


def _load_all() -> None:
    global _pending_counter
    with sqlite3.connect(_DB_FILE) as conn:
        for slot_key, data in conn.execute("SELECT slot_key, data FROM bookings"):
            appointments[slot_key] = json.loads(data)
        max_bid = 0
        for bid, data in conn.execute("SELECT bid, data FROM pending"):
            pending_bookings[bid] = json.loads(data)
            max_bid = max(max_bid, bid)
        _pending_counter = max_bid
        for user_id, name, phone, lang in conn.execute(
            "SELECT user_id, name, phone, lang FROM customers"
        ):
            entry: dict[str, str] = {"lang": lang or "ru"}
            if name:  entry["name"]  = name
            if phone: entry["phone"] = phone
            customer_cache[user_id] = entry
        for (slot_key,) in conn.execute("SELECT slot_key FROM blocked_slots"):
            blocked_slots.add(slot_key)
    logger.info(
        "DB loaded: %d bookings, %d pending, %d customers",
        len(appointments), len(pending_bookings), len(customer_cache),
    )


def _db_save_booking(slot_key: str, bk: dict) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO bookings (slot_key, data) VALUES (?, ?)",
                (slot_key, json.dumps(bk)),
            )
    except Exception as exc:
        logger.error("DB save_booking failed for %s: %s", slot_key, exc)


def _db_delete_booking(slot_key: str) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute("DELETE FROM bookings WHERE slot_key = ?", (slot_key,))
    except Exception as exc:
        logger.error("DB delete_booking failed for %s: %s", slot_key, exc)


def _db_save_pending(bid: int, bk: dict) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending (bid, data) VALUES (?, ?)",
                (bid, json.dumps(bk)),
            )
    except Exception as exc:
        logger.error("DB save_pending failed for bid=%d: %s", bid, exc)


def _db_delete_pending(bid: int) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute("DELETE FROM pending WHERE bid = ?", (bid,))
    except Exception as exc:
        logger.error("DB delete_pending failed for bid=%d: %s", bid, exc)


def _db_save_customer(uid: int) -> None:
    c = customer_cache.get(uid, {})
    with sqlite3.connect(_DB_FILE) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO customers (user_id, name, phone, lang) VALUES (?, ?, ?, ?)",
            (uid, c.get("name"), c.get("phone"), c.get("lang", "ru")),
        )


def _db_save_blocked(slot_key: str) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute("INSERT OR IGNORE INTO blocked_slots (slot_key) VALUES (?)", (slot_key,))
    except Exception as exc:
        logger.error("DB save_blocked failed for %s: %s", slot_key, exc)


def _db_delete_blocked(slot_key: str) -> None:
    try:
        with sqlite3.connect(_DB_FILE) as conn:
            conn.execute("DELETE FROM blocked_slots WHERE slot_key = ?", (slot_key,))
    except Exception as exc:
        logger.error("DB delete_blocked failed for %s: %s", slot_key, exc)


# Short day labels for the config UI (Russian, barber-facing)
_DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ─────────────────────────── Date/time formatting ────────────────────────────

_DAYS_LONG: dict[str, list[str]] = {
    "ru": ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"],
    "uz": ["Dushanba",    "Seshanba", "Chorshanba", "Payshanba", "Juma",   "Shanba",   "Yakshanba"],
}
_DAYS_SHORT: dict[str, list[str]] = {
    "ru": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
    "uz": ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"],
}


def _fmt_date(d: date, lang: str = "ru") -> str:
    """e.g. 'Вторник 24/02/2026'"""
    return f"{_DAYS_LONG[lang][d.weekday()]} {d.strftime('%d/%m/%Y')}"


def _fmt_date_short(d: date, lang: str = "ru") -> str:
    """e.g. 'Вт 24/02' — used in compact keyboard buttons"""
    return f"{_DAYS_SHORT[lang][d.weekday()]} {d.strftime('%d/%m')}"


def _fmt_time_range(start_time: str, n_slots: int) -> str:
    """e.g. '10:30–12:00' (n_slots × 30 min each)"""
    h, m    = int(start_time.split(":")[0]), int(start_time.split(":")[1])
    end_min = h * 60 + m + n_slots * 30
    return f"{start_time}–{end_min // 60:02d}:{end_min % 60:02d}"


# ─────────────────────────── Translations ────────────────────────────────────
STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        # ── language ──────────────────────────────────────────────────────────
        "choose_lang":          "🌐 Выберите язык:",
        "lang_changed_ru":      "✅ Язык изменён: 🇷🇺 Русский",
        "lang_changed_uz":      "✅ Язык изменён: 🇺🇿 O'zbek",
        # ── greeting ──────────────────────────────────────────────────────────
        "welcome":              (
            "👋 Привет, <b>{name}</b>! Добро пожаловать в наш барбершоп. ✂️\n\n"
            "Выберите удобную дату для визита:"
        ),
        "welcome_back":         (
            "👋 С возвращением, <b>{name}</b>! Рады снова вас видеть.\n\n"
            "Время <b>{time}</b> выбрано. Ваши данные уже у нас — "
            "осталось выбрать услуги:"
        ),
        # ── date ──────────────────────────────────────────────────────────────
        "choose_date":          "📅 На какой день вас записать?",
        "today":                "Сегодня",
        "tomorrow":             "Завтра",
        "no_slots":             (
            "😕 На <b>{date}</b> все слоты заняты.\n"
            "Выберите другой день:"
        ),
        "date_selected":        "📅 <b>{date}</b>\n\nВыберите удобное время:",
        # ── time ──────────────────────────────────────────────────────────────
        "morning":              "🌅 Утро",
        "afternoon":            "☀️ День",
        "evening":              "🌆 Вечер",
        "slot_taken":           (
            "😕 Этот слот только что заняли — быстро разбирают!\n"
            "Выберите другое время:"
        ),
        # ── name / phone ───────────────────────────────────────────────────────
        "enter_name":           (
            "Отлично! Время <b>{time}</b> зарезервировано.\n\n"
            "👤 Как вас зовут? Введите ваше имя:"
        ),
        "invalid_name":         "⚠️ Имя слишком короткое. Пожалуйста, введите полное имя:",
        "enter_phone":          (
            "Приятно познакомиться, <b>{name}</b>! 😊\n\n"
            "📞 Поделитесь номером телефона — мастер свяжется с вами при необходимости:"
        ),
        "share_phone":          "📱 Поделиться номером",
        "invalid_phone":        "⚠️ Некорректный номер. Пожалуйста, попробуйте ещё раз:",
        "phone_saved":          "✅ Номер сохранён!\n\nТеперь выберите услуги:",
        # ── services ───────────────────────────────────────────────────────────
        "select_svc":           "✂️ Выберите услуги (можно несколько):",
        "min_one_svc":          "Пожалуйста, выберите хотя бы одну услугу.",
        "svc_dur_min":          "мин",
        "no_consec":            (
            "😕 К сожалению, для выбранных услуг нужно <b>{n} ч.</b> подряд, "
            "а этого времени уже нет в этот день.\n\n"
            "Выберите другое время:"
        ),
        # ── confirm ────────────────────────────────────────────────────────────
        "confirm_text":         (
            "📋 <b>Проверьте детали записи:</b>\n\n"
            "  📅 Дата:          {date}\n"
            "  🕐 Время:         {time}\n"
            "  ⏱ Длительность:  ~{dur} мин.\n"
            "  👤 Имя:           {name}\n"
            "  📞 Телефон:       {phone}\n"
            "  ✂️ Услуги:        {svcs}\n\n"
            "Всё верно?"
        ),
        "btn_confirm":          "✅ Записаться",
        "btn_cancel":           "❌ Отмена",
        "btn_back":             "← Назад",
        "btn_done":             "Готово →",
        # ── status messages ────────────────────────────────────────────────────
        "waiting":              (
            "⏳ <b>Заявка отправлена мастеру!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Как только мастер подтвердит — вы получите сообщение. "
            "Обычно это занимает несколько минут. 😊"
        ),
        "approved":             (
            "🎉 <b>Запись подтверждена!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n"
            "✂️ {svcs}\n\n"
            "Ждём вас! Если планы изменятся — напишите нам заранее.\n"
            "Для новой записи: /start"
        ),
        "rejected":             (
            "😔 <b>Мастер не смог принять запись.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Попробуйте выбрать другое время: /start"
        ),
        "cancelled_barber":     (
            "😔 <b>Ваша запись была отменена мастером.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Приносим извинения! Для новой записи: /start"
        ),
        "slot_race":            "😕 Слот только что заняли. Напишите /start и выберите другое время.",
        "already_booked":       (
            "⚠️ У вас уже есть активная запись.\n\n"
            "Посмотрите её командой /mybooking — там можно перенести или отменить."
        ),
        "booking_cancel":       "Запись отменена. Будем рады видеть вас снова — /start",
        "flow_cancelled":       (
            "Оформление записи отменено. Данные не сохранены.\n\n"
            "Записаться снова — /start\n"
            "Ваши записи — /mybooking"
        ),
        "cancel_no_flow":       None,  # unused: /cancel redirects to cmd_mybooking
        "unexpected":           "Не совсем понял 🤔 Следуйте подсказкам.",
        # ── settings ───────────────────────────────────────────────────────────
        "settings":             "⚙️ <b>Настройки</b>\n\nВыберите язык:",
        "settings_mid_conv":    (
            "⚙️ <b>Смена языка</b>\n\nВыберите язык.\n\n"
            "<i>Текущая запись будет отменена — начните заново с /start.</i>"
        ),
        # ── my booking / user cancel ───────────────────────────────────────────
        "mybooking_none":       "У вас нет активных записей. Для записи нажмите /start",
        "mybooking_header":     "📋 <b>Ваша запись:</b>\n\n",
        "mybooking_pending":    "⏳ Ожидает подтверждения мастера",
        "mybooking_confirmed":  "✅ Подтверждена",
        "btn_cancel_booking":   "❌ Отменить запись",
        "cancelled_by_user":    "✅ Запись отменена. Будем рады видеть вас снова — /start",
        "cancelled_by_user_barber": (
            "❌ <b>Клиент отменил запись</b>\n\n"
            "👤 {name}\n"
            "📅 {date}\n"
            "🕐 {time}"
        ),
        # ── reminder ──────────────────────────────────────────────────────────
        "reminder":             (
            "⏰ <b>Напоминание!</b>\n\n"
            "Через 30 минут у вас запись в барбершоп.\n\n"
            "🕐 {time}\n\n"
            "Ждём вас! 💈"
        ),
        # ── pending timeout ────────────────────────────────────────────────────
        "pending_timeout":      (
            "⌛ <b>Запись не была подтверждена вовремя.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Мастер не успел ответить. Попробуйте снова: /start"
        ),
        # ── reschedule ─────────────────────────────────────────────────────────
        "reschedule_choose_date": "🔄 <b>Перенос записи</b>\n\nВыберите новую дату:",
        "reschedule_choose_time": "🕐 Выберите новое время для переноса:",
        "reschedule_confirm":     (
            "🔄 <b>Подтвердите перенос:</b>\n\n"
            "  ❌ Было:  {old_date}  {old_time}\n"
            "  ✅ Стало: {new_date}  {new_time}\n\n"
            "<i>Мастер должен будет одобрить новое время.</i>"
        ),
        "btn_reschedule":         "🔄 Перенести",
        "btn_confirm_reschedule": "✅ Перенести",
        "reschedule_waiting":     (
            "⏳ <b>Запрос на перенос отправлен!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Как только мастер подтвердит — вы получите уведомление. 😊"
        ),
        "same_slot":              "Это то же время, что и сейчас. Выберите другое.",
        # ── main menu buttons ──────────────────────────────────────────────────
        "menu_book": "✂️ Записаться",
        "menu_lang": "🌐 Выбрать язык",
        # ── help ───────────────────────────────────────────────────────────────
        "help": (
            "👋 Привет! Я бот барбершопа.\n\n"
            "Что умею:\n"
            "✂️ <b>Записаться</b> — выбрать дату, время и услуги\n"
            "📋 /mybooking — посмотреть, перенести или отменить запись\n"
            "🌐 /settings — сменить язык (O'zbek / Русский)\n"
            "⏰ Напомню за 30 минут до визита\n\n"
            "Нажмите <b>✂️ Записаться</b> или /start чтобы начать 👇"
        ),
        # ── info ──────────────────────────────────────────────────────────────
        "info": (
            "ℹ️ <b>О нашем барбершопе</b>\n\n"
            "🕐 <b>Режим работы:</b>\n"
            "{schedule}\n\n"
            "✂️ <b>Услуги и цены:</b>\n"
            "{services}\n\n"
            "📌 <b>Как записаться:</b>\n"
            "1. Нажмите <b>✂️ Записаться</b> или /start\n"
            "2. Выберите дату и время\n"
            "3. Выберите услуги\n"
            "4. Подтвердите запись — мастер получит уведомление\n\n"
            "📋 /mybooking — ваши записи (перенос, отмена)\n"
            "🌐 /settings — сменить язык\n"
            "⏰ Напомню за 30 минут до визита"
        ),
    },
    "uz": {
        # ── language ──────────────────────────────────────────────────────────
        "choose_lang":          "🌐 Tilni tanlang:",
        "lang_changed_ru":      "✅ Til o'zgartirildi: 🇷🇺 Русский",
        "lang_changed_uz":      "✅ Til o'zgartirildi: 🇺🇿 O'zbek",
        # ── greeting ──────────────────────────────────────────────────────────
        "welcome":              (
            "👋 Salom, <b>{name}</b>! Sartaroshxonamizga xush kelibsiz! ✂️\n\n"
            "Tashrif uchun qulay sanani tanlang:"
        ),
        "welcome_back":         (
            "👋 Qaytib kelganingizdan xursandmiz, <b>{name}</b>!\n\n"
            "<b>{time}</b> vaqti tanlandi. Ma'lumotlaringiz saqlanган — "
            "faqat xizmatlarni tanlang:"
        ),
        # ── date ──────────────────────────────────────────────────────────────
        "choose_date":          "📅 Qaysi kunga yozib qo'yaylik?",
        "today":                "Bugun",
        "tomorrow":             "Ertaga",
        "no_slots":             (
            "😕 <b>{date}</b> kuni barcha vaqtlar band.\n"
            "Boshqa kun tanlang:"
        ),
        "date_selected":        "📅 <b>{date}</b>\n\nQulay vaqtni tanlang:",
        # ── time ──────────────────────────────────────────────────────────────
        "morning":              "🌅 Ertalab",
        "afternoon":            "☀️ Kunduz",
        "evening":              "🌆 Kechqurun",
        "slot_taken":           (
            "😕 Bu vaqt band bo'lib qoldi — tez ketmoqda!\n"
            "Boshqa vaqt tanlang:"
        ),
        # ── name / phone ───────────────────────────────────────────────────────
        "enter_name":           (
            "Ajoyib! <b>{time}</b> vaqti zahiralandi.\n\n"
            "👤 Ismingiz nima? Iltimos, to'liq ismingizni kiriting:"
        ),
        "invalid_name":         "⚠️ Ism juda qisqa. Iltimos, to'liq ismingizni kiriting:",
        "enter_phone":          (
            "Tanishganimizdan xursandmiz, <b>{name}</b>! 😊\n\n"
            "📞 Telefon raqamingizni ulashing — kerak bo'lganda usta siz bilan bog'lanadi:"
        ),
        "share_phone":          "📱 Raqamni ulashish",
        "invalid_phone":        "⚠️ Noto'g'ri raqam. Iltimos, qaytadan kiriting:",
        "phone_saved":          "✅ Raqam saqlandi!\n\nEndi xizmatlarni tanlang:",
        # ── services ───────────────────────────────────────────────────────────
        "select_svc":           "✂️ Xizmatlarni tanlang (bir nechtasini ham tanlash mumkin):",
        "min_one_svc":          "Iltimos, kamida bitta xizmat tanlang.",
        "svc_dur_min":          "daq",
        "no_consec":            (
            "😕 Afsuski, tanlangan xizmatlar uchun <b>{n} soat</b> ketma-ket bo'sh vaqt kerak, "
            "lekin bu kunda bunday vaqt qolmadi.\n\n"
            "Boshqa vaqt tanlang:"
        ),
        # ── confirm ────────────────────────────────────────────────────────────
        "confirm_text":         (
            "📋 <b>Yozilish ma'lumotlarini tekshiring:</b>\n\n"
            "  📅 Sana:         {date}\n"
            "  🕐 Vaqt:         {time}\n"
            "  ⏱ Davomiyligi:  ~{dur} daqiqa\n"
            "  👤 Ism:          {name}\n"
            "  📞 Telefon:      {phone}\n"
            "  ✂️ Xizmat:       {svcs}\n\n"
            "Hammasi to'g'rimi?"
        ),
        "btn_confirm":          "✅ Yozilish",
        "btn_cancel":           "❌ Bekor",
        "btn_back":             "← Orqaga",
        "btn_done":             "Tayyor →",
        # ── status messages ────────────────────────────────────────────────────
        "waiting":              (
            "⏳ <b>Ariza ustaga yuborildi!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Usta tasdiqlashi bilanoq sizga xabar beramiz. "
            "Odatda bu bir necha daqiqa oladi. 😊"
        ),
        "approved":             (
            "🎉 <b>Yozilishingiz tasdiqlandi!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n"
            "✂️ {svcs}\n\n"
            "Sizni kutamiz! Reja o'zgarsa — oldindan xabar bering.\n"
            "Yangi yozilish uchun: /start"
        ),
        "rejected":             (
            "😔 <b>Usta bu vaqtni qabul qila olmadi.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Boshqa vaqt tanlang: /start"
        ),
        "cancelled_barber":     (
            "😔 <b>Yozilishingiz usta tomonidan bekor qilindi.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Uzr so'raymiz! Yangi yozilish uchun: /start"
        ),
        "slot_race":            "😕 Vaqt band bo'lib qoldi. /start yozing va boshqa vaqt tanlang.",
        "already_booked":       (
            "⚠️ Sizda allaqachon faol yozilish bor.\n\n"
            "/mybooking buyrug'i bilan ko'ring — u yerda ko'chirish yoki bekor qilish mumkin."
        ),
        "booking_cancel":       "Yozilish bekor qilindi. Yana ko'rishguncha — /start",
        "flow_cancelled":       (
            "Yozilish jarayoni bekor qilindi. Ma'lumotlar saqlanmadi.\n\n"
            "Qaytadan yozilish — /start\n"
            "Yozilishlarim — /mybooking"
        ),
        "cancel_no_flow":       None,  # unused: /cancel redirects to cmd_mybooking
        "unexpected":           "Tushunmadim 🤔 Ko'rsatmalarga amal qiling.",
        # ── settings ───────────────────────────────────────────────────────────
        "settings":             "⚙️ <b>Sozlamalar</b>\n\nTilni tanlang:",
        "settings_mid_conv":    (
            "⚙️ <b>Tilni o'zgartirish</b>\n\nTilni tanlang.\n\n"
            "<i>Joriy yozilish bekor qilinadi — /start bilan qaytadan boshlang.</i>"
        ),
        # ── my booking / user cancel ───────────────────────────────────────────
        "mybooking_none":       "Faol yozilishingiz yo'q. Yozilish uchun /start bosing",
        "mybooking_header":     "📋 <b>Yozilishingiz:</b>\n\n",
        "mybooking_pending":    "⏳ Usta tasdig'ini kutmoqda",
        "mybooking_confirmed":  "✅ Tasdiqlangan",
        "btn_cancel_booking":   "❌ Yozilishni bekor qilish",
        "cancelled_by_user":    "✅ Yozilish bekor qilindi. Yana ko'rishguncha — /start",
        "cancelled_by_user_barber": (
            "❌ <b>Mijoz yozilishni bekor qildi</b>\n\n"
            "👤 {name}\n"
            "📅 {date}\n"
            "🕐 {time}"
        ),
        # ── reminder ──────────────────────────────────────────────────────────
        "reminder":             (
            "⏰ <b>Eslatma!</b>\n\n"
            "30 daqiqadan keyin sartaroshxonaga yozilishingiz bor.\n\n"
            "🕐 {time}\n\n"
            "Sizni kutamiz! 💈"
        ),
        # ── pending timeout ────────────────────────────────────────────────────
        "pending_timeout":      (
            "⌛ <b>Yozilish vaqtida tasdiqlanmadi.</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Usta javob bermadi. Qaytadan urinib ko'ring: /start"
        ),
        # ── reschedule ─────────────────────────────────────────────────────────
        "reschedule_choose_date": "🔄 <b>Yozilishni ko'chirish</b>\n\nYangi sana tanlang:",
        "reschedule_choose_time": "🕐 Ko'chirish uchun yangi vaqt tanlang:",
        "reschedule_confirm":     (
            "🔄 <b>Ko'chirishni tasdiqlang:</b>\n\n"
            "  ❌ Edi:    {old_date}  {old_time}\n"
            "  ✅ Bo'ladi: {new_date}  {new_time}\n\n"
            "<i>Usta yangi vaqtni tasdiqlashi kerak.</i>"
        ),
        "btn_reschedule":         "🔄 Ko'chirish",
        "btn_confirm_reschedule": "✅ Ko'chirish",
        "reschedule_waiting":     (
            "⏳ <b>Ko'chirish so'rovi yuborildi!</b>\n\n"
            "📅 {date}\n"
            "🕐 {time}\n\n"
            "Usta tasdiqlashi bilanoq sizga xabar beramiz. 😊"
        ),
        "same_slot":              "Bu hozirgi vaqt bilan bir xil. Boshqa tanlang.",
        # ── main menu buttons ──────────────────────────────────────────────────
        "menu_book": "✂️ Yozilish",
        "menu_lang": "🌐 Tilni tanlash",
        # ── help ───────────────────────────────────────────────────────────────
        "help": (
            "👋 Salom! Men sartaroshxona botiman.\n\n"
            "Nima qila olaman:\n"
            "✂️ <b>Yozilish</b> — sana, vaqt va xizmat tanlash\n"
            "📋 /mybooking — yozilishni ko'rish, ko'chirish yoki bekor qilish\n"
            "🌐 /settings — tilni o'zgartirish (O'zbek / Русский)\n"
            "⏰ Tashrif oldidan 30 daqiqa oldin eslataman\n\n"
            "✂️ <b>Yozilish</b> tugmasini bosing yoki /start yuboring 👇"
        ),
        # ── info ──────────────────────────────────────────────────────────────
        "info": (
            "ℹ️ <b>Sartaroshxonamiz haqida</b>\n\n"
            "🕐 <b>Ish vaqti:</b>\n"
            "{schedule}\n\n"
            "✂️ <b>Xizmatlar va narxlar:</b>\n"
            "{services}\n\n"
            "📌 <b>Qanday yozilish mumkin:</b>\n"
            "1. <b>✂️ Yozilish</b> yoki /start bosing\n"
            "2. Sana va vaqtni tanlang\n"
            "3. Xizmatlarni tanlang\n"
            "4. Tasdiqlang — usta xabar oladi\n\n"
            "📋 /mybooking — yozilishlaringiz (ko'chirish, bekor qilish)\n"
            "🌐 /settings — tilni o'zgartirish\n"
            "⏰ Tashrif oldidan 30 daqiqa oldin eslataman"
        ),
    },
}


# ─────────────────────────── Main menu keyboard ───────────────────────────────

# All possible texts that the persistent menu buttons can send.
MENU_BOOK_TEXTS = {STRINGS["ru"]["menu_book"], STRINGS["uz"]["menu_book"]}
MENU_LANG_TEXTS = {STRINGS["ru"]["menu_lang"], STRINGS["uz"]["menu_lang"]}


def _main_menu_kb(lang: str) -> ReplyKeyboardMarkup:
    """Persistent two-button keyboard shown below the message input."""
    if MINIAPP_ENABLED and MINIAPP_URL:
        book_btn = KeyboardButton(
            STRINGS[lang]["menu_book"],
            web_app=WebAppInfo(url=MINIAPP_URL),
        )
    else:
        book_btn = KeyboardButton(STRINGS[lang]["menu_book"])
    return ReplyKeyboardMarkup(
        [[book_btn], [KeyboardButton(STRINGS[lang]["menu_lang"])]],
        resize_keyboard=True,
    )


# ─────────────────────────── Services catalogue ───────────────────────────────
# Loaded from services.json at startup.
# "ru" / "uz"     — label shown to barber
# "ru_c" / "uz_c" — label shown to clients (no price)
# "mins"          — service duration in minutes

_SERVICES_FILE = Path(__file__).parent / "services.json"


def _load_services() -> dict[str, dict[str, Any]]:
    if _SERVICES_FILE.exists():
        try:
            return json.loads(_SERVICES_FILE.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not load services.json: %s — using empty catalogue", exc)
    return {}


SERVICES: dict[str, dict[str, Any]] = _load_services()


def _svc_label(svc_id: str, lang: str) -> str:
    """Full label with price — for barber."""
    return SERVICES[svc_id][lang]


def _svc_client_label(svc_id: str, lang: str) -> str:
    """Label without price — for clients."""
    return SERVICES[svc_id].get(f"{lang}_c", SERVICES[svc_id][lang])


def _calc_duration(service_ids: list[str]) -> tuple[int, int]:
    """Return (total_minutes, 30-min_slots_needed)."""
    mins  = sum(SERVICES[s]["mins"] for s in service_ids if s in SERVICES)
    slots = max(1, math.ceil(mins / 30))
    return mins, slots


def _calc_total_price(service_ids: list[str]) -> int:
    return sum(SERVICES[s].get("price_uzs", 0) for s in service_ids if s in SERVICES)


def _price_line(price: int, lang: str) -> str:
    """Format a price value for display, or empty string if no price configured."""
    if price <= 0:
        return ""
    formatted = f"{price:,}".replace(",", "\u00a0")   # non-breaking space as thousands sep
    if lang == "uz":
        return f"💰 Narx: {formatted} so'm"
    return f"💰 Стоимость: {formatted} сум"


# ─────────────────────────── In-memory storage ───────────────────────────────
appointments:    dict[str, dict[str, Any]] = {}   # "YYYY-MM-DD HH:MM" → booking
pending_bookings: dict[int, dict[str, Any]] = {}   # booking_id → booking
_pending_counter = 0

customer_cache: dict[int, dict[str, str]] = {}     # user_id → {name, phone, lang}
blocked_slots:  set[str] = set()                   # "YYYY-MM-DD HH:MM" — barber-blocked slots


def _next_id() -> int:
    global _pending_counter
    _pending_counter += 1
    return _pending_counter


def _all_taken_slots(exclude_slot_key: str | None = None) -> set[str]:
    taken: set[str] = set(blocked_slots)   # barber-blocked slots always taken
    for slot_key, bk in appointments.items():
        if slot_key == exclude_slot_key:
            continue
        d_str, t_str = slot_key.split(" ")
        h, m = int(t_str[:2]), int(t_str[3:5])
        start_min = h * 60 + m
        for i in range(bk.get("duration_slots", 1)):
            sm = start_min + i * 30
            taken.add(f"{d_str} {sm // 60:02d}:{sm % 60:02d}")
    for bk in pending_bookings.values():
        if bk["slot_key"] == exclude_slot_key:
            continue
        d_str, t_str = bk["slot_key"].split(" ")
        h, m = int(t_str[:2]), int(t_str[3:5])
        start_min = h * 60 + m
        for i in range(bk.get("duration_slots", 1)):
            sm = start_min + i * 30
            taken.add(f"{d_str} {sm // 60:02d}:{sm % 60:02d}")
    return taken


# ─────────────────────────── Translation helper ──────────────────────────────

def _lang(uid: int) -> str:
    return customer_cache.get(uid, {}).get("lang", "ru")


def tx(uid: int, key: str, **kwargs: Any) -> str:
    lang = _lang(uid)
    text = STRINGS[lang].get(key, STRINGS["ru"].get(key, key))
    return text.format(**kwargs) if kwargs else text


# ─────────────────────────── Slot / date helpers ─────────────────────────────

def _working_dates() -> list[date]:
    today  = datetime.now(tz=TZ).date()
    result: list[date] = []
    cursor = today
    while len(result) < DAYS_AHEAD:
        if cursor.weekday() in schedule_config["work_days"]:
            result.append(cursor)
        cursor += timedelta(days=1)
    return result


def _available_slots(for_date: date, exclude_slot_key: str | None = None) -> list[str]:
    now       = datetime.now(tz=TZ)
    taken     = _all_taken_slots(exclude_slot_key=exclude_slot_key)
    slots     = []
    start_min = schedule_config["start_hour"] * 60
    end_min   = schedule_config["end_hour"] * 60
    for sm in range(start_min, end_min, 30):
        h, m     = sm // 60, sm % 60
        slot_dt  = datetime(for_date.year, for_date.month, for_date.day, h, m, tzinfo=TZ)
        slot_key = f"{for_date.isoformat()} {h:02d}:{m:02d}"
        if slot_dt > now and slot_key not in taken:
            slots.append(f"{h:02d}:{m:02d}")
    return slots


def _can_fit(for_date: date, start_time: str, n_slots: int) -> bool:
    h, m      = int(start_time.split(":")[0]), int(start_time.split(":")[1])
    start_min = h * 60 + m
    end_min   = schedule_config["end_hour"] * 60
    if start_min + n_slots * 30 > end_min:
        return False
    taken = _all_taken_slots()
    iso   = for_date.isoformat()
    for i in range(n_slots):
        sm = start_min + i * 30
        if f"{iso} {sm // 60:02d}:{sm % 60:02d}" in taken:
            return False
    return True


# ─────────────────────────── Keyboard builders ───────────────────────────────

def _lang_keyboard(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🇷🇺 Русский", callback_data=f"{prefix}ru"),
        InlineKeyboardButton("🇺🇿 O'zbek",  callback_data=f"{prefix}uz"),
    ]])


def _date_keyboard(lang: str, date_prefix: str = "date",
                   cancel_data: str = "cancel") -> InlineKeyboardMarkup:
    today    = datetime.now(tz=TZ).date()
    tomorrow = today + timedelta(days=1)
    rows = []
    for d in _working_dates():
        short = _fmt_date_short(d, lang)
        if d == today:
            label = f"📌 {STRINGS[lang]['today']} — {short}"
        elif d == tomorrow:
            label = f"➡️ {STRINGS[lang]['tomorrow']} — {short}"
        else:
            label = f"📅 {short}"
        rows.append([InlineKeyboardButton(label, callback_data=f"{date_prefix}_{d.isoformat()}")])
    rows.append([InlineKeyboardButton(STRINGS[lang]["btn_cancel"], callback_data=cancel_data)])
    return InlineKeyboardMarkup(rows)


def _time_keyboard(for_date: date, lang: str,
                   time_prefix: str = "time",
                   back_data: str = "back_to_date",
                   cancel_data: str = "cancel",
                   exclude_slot_key: str | None = None) -> InlineKeyboardMarkup | None:
    slots = _available_slots(for_date, exclude_slot_key=exclude_slot_key)
    if not slots:
        return None

    # Group slots by time of day
    groups = [
        (STRINGS[lang]["morning"],   [s for s in slots if int(s[:2]) < 12]),
        (STRINGS[lang]["afternoon"], [s for s in slots if 12 <= int(s[:2]) < 17]),
        (STRINGS[lang]["evening"],   [s for s in slots if int(s[:2]) >= 17]),
    ]

    rows: list[list[InlineKeyboardButton]] = []
    for label, group_slots in groups:
        if not group_slots:
            continue
        # Non-clickable section header
        rows.append([InlineKeyboardButton(label, callback_data="noop")])
        # Time buttons in rows of 3
        row: list[InlineKeyboardButton] = []
        for s in group_slots:
            row.append(InlineKeyboardButton(s, callback_data=f"{time_prefix}_{s}"))
            if len(row) == 3:
                rows.append(row)
                row = []
        if row:
            rows.append(row)

    rows.append([
        InlineKeyboardButton(STRINGS[lang]["btn_back"],   callback_data=back_data),
        InlineKeyboardButton(STRINGS[lang]["btn_cancel"], callback_data=cancel_data),
    ])
    return InlineKeyboardMarkup(rows)


def _services_keyboard(selected: set[str], lang: str) -> InlineKeyboardMarkup:
    dur_unit = STRINGS[lang]["svc_dur_min"]
    rows = []
    for svc_id in SERVICES:
        tick = "✓ " if svc_id in selected else ""
        mins = SERVICES[svc_id]["mins"]
        label = f"{tick}{_svc_client_label(svc_id, lang)}  ({mins} {dur_unit})"
        rows.append([InlineKeyboardButton(label, callback_data=svc_id)])
    rows.append([
        InlineKeyboardButton(STRINGS[lang]["btn_done"],   callback_data="services_done"),
        InlineKeyboardButton(STRINGS[lang]["btn_cancel"], callback_data="cancel"),
    ])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(STRINGS[lang]["btn_confirm"], callback_data="confirm_yes"),
        InlineKeyboardButton(STRINGS[lang]["btn_cancel"],  callback_data="cancel"),
    ]])


def _phone_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(STRINGS[lang]["share_phone"], request_contact=True)],
            [KeyboardButton(STRINGS[lang]["btn_cancel"])],
        ],
        resize_keyboard=True, one_time_keyboard=True,
    )


def _approval_keyboard(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить",  callback_data=f"approve_{booking_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{booking_id}"),
    ]])


# ─────────────────────────── FSM states ──────────────────────────────────────
(
    STATE_LANG,
    STATE_DATE,
    STATE_TIME,
    STATE_NAME,
    STATE_PHONE,
    STATE_SERVICES,
    STATE_CONFIRM,
) = range(7)


# ─────────────────────────── /start  /cancel ─────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    name = update.effective_user.first_name
    logger.info("User %s (%d) /start", name, uid)

    if "lang" not in customer_cache.get(uid, {}):
        cfg = schedule_config
        day_names_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        day_names_uz = ["Du", "Se", "Cho", "Pa", "Ju", "Sha", "Ya"]
        days_ru = "–".join([day_names_ru[min(cfg["work_days"])],
                            day_names_ru[max(cfg["work_days"])]]) if cfg["work_days"] else "—"
        days_uz = "–".join([day_names_uz[min(cfg["work_days"])],
                            day_names_uz[max(cfg["work_days"])]]) if cfg["work_days"] else "—"
        hours = f"{cfg['start_hour']:02d}:00–{cfg['end_hour']:02d}:00"

        svc_ru = ", ".join(s["ru_c"].lower() for s in SERVICES.values())
        svc_uz = ", ".join(s["uz_c"].lower() for s in SERVICES.values())

        greeting = (
            f"👋 Добро пожаловать! | Xush kelibsiz!\n\n"
            f"✂️ {svc_ru.capitalize()}\n"
            f"🕐 {days_ru} {hours}\n"
            f"📱 Запишитесь прямо здесь!\n\n"
            f"✂️ {svc_uz.capitalize()}\n"
            f"🕐 {days_uz} {hours}\n"
            f"📱 Shu yerda yoziling!"
        )
        await update.message.reply_text(greeting, parse_mode="HTML")
        await update.message.reply_text(
            "🌐 Выберите язык / Tilni tanlang:",
            reply_markup=_lang_keyboard("lang_"),
        )
        return STATE_LANG

    await update.message.reply_text(
        tx(uid, "welcome", name=name),
        parse_mode="HTML",
        reply_markup=_date_keyboard(_lang(uid)),
    )
    return STATE_DATE


async def cmd_menu_lang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle 'Choose language' persistent-menu button (outside conversation)."""
    uid = update.effective_user.id
    await update.message.reply_text(
        STRINGS[_lang(uid)]["settings"],
        parse_mode="HTML",
        reply_markup=_lang_keyboard("setlang_"),
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        tx(uid, "help"),
        parse_mode="HTML",
        reply_markup=_main_menu_kb(_lang(uid)),
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    lang = _lang(uid)
    cfg = schedule_config

    # Schedule text
    day_names_ru = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    day_names_uz = ["Du", "Se", "Cho", "Pa", "Ju", "Sha", "Ya"]
    day_names = day_names_ru if lang == "ru" else day_names_uz
    days = ", ".join(day_names[d] for d in sorted(cfg["work_days"])) or "—"
    schedule = f"{days}  |  {cfg['start_hour']:02d}:00 – {cfg['end_hour']:02d}:00"

    # Services text
    svc_lines = []
    for svc_id, svc in SERVICES.items():
        name = svc.get(f"{lang}_c", svc.get(lang, svc_id))
        price = f"{svc['price_uzs']:,}".replace(",", " ")
        svc_lines.append(f"• {name} — {svc['mins']} мин. — {price} сум"
                         if lang == "ru" else
                         f"• {name} — {svc['mins']} daq. — {price} so'm")
    services_text = "\n".join(svc_lines) or "—"

    await update.message.reply_text(
        tx(uid, "info", schedule=schedule, services=services_text),
        parse_mode="HTML",
        reply_markup=_main_menu_kb(lang),
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id
    if context.user_data:
        # Inside an active booking flow — exit it
        context.user_data.clear()
        await update.message.reply_text(
            tx(uid, "flow_cancelled"),
            reply_markup=_main_menu_kb(_lang(uid)),
            parse_mode="HTML",
        )
    else:
        # Not in a flow — show mybooking directly
        await cmd_mybooking(update, context)
    return ConversationHandler.END


async def _cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.callback_query.edit_message_text(
        tx(update.effective_user.id, "booking_cancel")
    )
    return ConversationHandler.END


# ─────────────────────────── /settings (works any time) ──────────────────────

async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Outside a conversation — just show the language picker."""
    uid = update.effective_user.id
    await update.message.reply_text(
        STRINGS[_lang(uid)]["settings"],
        parse_mode="HTML",
        reply_markup=_lang_keyboard("setlang_"),
    )


async def _settings_in_conv(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Inside a conversation — show picker, cancel the current booking flow."""
    uid  = update.effective_user.id
    lang = _lang(uid)
    context.user_data.clear()
    await update.message.reply_text(
        STRINGS[lang]["settings_mid_conv"],
        parse_mode="HTML",
        reply_markup=_lang_keyboard("setlang_"),
    )
    return ConversationHandler.END   # cleanly exits the conversation


async def cb_setlang(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle language-change button from /settings (outside conversation)."""
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = query.data.split("_")[-1]              # "setlang_ru" → "ru"
    customer_cache.setdefault(uid, {})["lang"] = lang
    _db_save_customer(uid)
    key  = f"lang_changed_{lang}"
    await query.edit_message_text(STRINGS[lang].get(key, "✅"))
    await query.message.reply_text("👇", reply_markup=_main_menu_kb(lang))


# ─────────────────────────── STATE_LANG ──────────────────────────────────────

async def cb_lang_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = query.data.split("_")[-1]              # "lang_ru" → "ru"
    customer_cache.setdefault(uid, {})["lang"] = lang
    _db_save_customer(uid)
    await query.edit_message_text(
        tx(uid, "welcome", name=update.effective_user.first_name),
        parse_mode="HTML",
        reply_markup=_date_keyboard(lang),
    )
    return STATE_DATE


# ─────────────────────────── STATE_DATE ──────────────────────────────────────

async def cb_date_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = _lang(uid)

    if query.data == "cancel":
        return await _cancel_cb(update, context)

    chosen = date.fromisoformat(query.data.split("_", 1)[1])
    context.user_data["date"] = chosen

    kb = _time_keyboard(chosen, lang)
    if kb is None:
        await query.edit_message_text(
            tx(uid, "no_slots", date=_fmt_date(chosen, lang)),
            parse_mode="HTML",
            reply_markup=_date_keyboard(lang),
        )
        return STATE_DATE

    await query.edit_message_text(
        tx(uid, "date_selected", date=_fmt_date(chosen, lang)),
        parse_mode="HTML",
        reply_markup=kb,
    )
    return STATE_TIME


# ─────────────────────────── STATE_TIME ──────────────────────────────────────

async def cb_time_selected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = _lang(uid)

    if query.data == "cancel":
        return await _cancel_cb(update, context)

    if query.data == "back_to_date":
        await query.edit_message_text(
            tx(uid, "choose_date"), reply_markup=_date_keyboard(lang)
        )
        return STATE_DATE

    t        = query.data.split("_", 1)[1]      # "09:00"
    slot_key = f"{context.user_data['date'].isoformat()} {t}"
    context.user_data["time"] = t

    if slot_key in _all_taken_slots():
        await query.edit_message_text(
            tx(uid, "slot_taken"),
            reply_markup=_time_keyboard(context.user_data["date"], lang),
        )
        return STATE_TIME

    context.user_data["services"] = set()

    cached = customer_cache.get(uid, {})
    if "name" in cached and "phone" in cached:
        context.user_data.update(name=cached["name"], phone=cached["phone"])
        await query.edit_message_text(
            tx(uid, "welcome_back", time=t, name=cached["name"]),
            parse_mode="HTML",
            reply_markup=_services_keyboard(set(), lang),
        )
        return STATE_SERVICES

    await query.edit_message_text(
        tx(uid, "enter_name", time=t),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(STRINGS[lang]["btn_cancel"], callback_data="cancel"),
        ]]),
    )
    return STATE_NAME


# ─────────────────────────── STATE_NAME ──────────────────────────────────────

async def handle_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid  = update.effective_user.id
    lang = _lang(uid)
    name = update.message.text.strip()

    if len(name) < 2:
        await update.message.reply_text(tx(uid, "invalid_name"))
        return STATE_NAME

    context.user_data["name"] = name
    await update.message.reply_text(
        tx(uid, "enter_phone", name=name),
        parse_mode="HTML",
        reply_markup=_phone_keyboard(lang),
    )
    return STATE_PHONE


# ─────────────────────────── STATE_PHONE ─────────────────────────────────────

async def handle_phone_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    phone = update.message.contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"
    return await _after_phone(update, context, phone)


async def handle_phone_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid   = update.effective_user.id
    phone = update.message.text.strip()
    lang  = _lang(uid)
    # Cancel button pressed on ReplyKeyboard
    if phone in (STRINGS["ru"]["btn_cancel"], STRINGS["uz"]["btn_cancel"]):
        context.user_data.clear()
        await update.message.reply_text(
            tx(uid, "flow_cancelled"),
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    if len("".join(c for c in phone if c.isdigit())) < 7:
        await update.message.reply_text(
            tx(uid, "invalid_phone"), reply_markup=_phone_keyboard(lang)
        )
        return STATE_PHONE
    return await _after_phone(update, context, phone)


async def _after_phone(
    update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str
) -> int:
    uid  = update.effective_user.id
    lang = _lang(uid)
    context.user_data["phone"] = phone
    await update.message.reply_text(
        tx(uid, "phone_saved", phone=phone),
        reply_markup=ReplyKeyboardRemove(),
    )
    await update.message.reply_text(
        tx(uid, "select_svc"),
        reply_markup=_services_keyboard(set(), lang),
    )
    return STATE_SERVICES


# ─────────────────────────── STATE_SERVICES ──────────────────────────────────

async def cb_service_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    selected: set[str] = context.user_data.setdefault("services", set())

    if query.data == "cancel":
        return await _cancel_cb(update, context)

    if query.data in SERVICES:
        selected.discard(query.data) if query.data in selected else selected.add(query.data)
        await query.edit_message_text(
            tx(uid, "select_svc"),
            reply_markup=_services_keyboard(selected, lang),
        )
        return STATE_SERVICES

    if query.data == "services_done":
        if not selected:
            await query.answer(tx(uid, "min_one_svc"), show_alert=True)
            return STATE_SERVICES

        d          = context.user_data["date"]
        t          = context.user_data["time"]
        name       = context.user_data["name"]
        phone      = context.user_data["phone"]
        total_mins, n_slots = _calc_duration(list(selected))

        if not _can_fit(d, t, n_slots):
            await query.edit_message_text(
                tx(uid, "no_consec", n=n_slots),
                parse_mode="HTML",
                reply_markup=_time_keyboard(d, lang),
            )
            context.user_data.pop("services", None)
            return STATE_TIME

        time_range = _fmt_time_range(t, n_slots)
        context.user_data["duration_slots"] = n_slots
        context.user_data["duration_mins"]  = total_mins
        context.user_data["time_range"]     = time_range

        svc_text = ", ".join(_svc_client_label(s, lang) for s in selected)

        await query.edit_message_text(
            tx(uid, "confirm_text",
               date=_fmt_date(d, lang), time=time_range,
               dur=total_mins, name=name, phone=phone, svcs=svc_text),
            parse_mode="HTML",
            reply_markup=_confirm_keyboard(lang),
        )
        return STATE_CONFIRM

    return STATE_SERVICES


# ─────────────────────────── STATE_CONFIRM ───────────────────────────────────

async def cb_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = _lang(uid)

    if query.data == "cancel":
        return await _cancel_cb(update, context)
    if query.data != "confirm_yes":
        return STATE_CONFIRM

    d          = context.user_data["date"]
    t          = context.user_data["time"]
    slot_key   = f"{d.isoformat()} {t}"
    name       = context.user_data["name"]
    phone      = context.user_data["phone"]
    services   = list(context.user_data["services"])
    n_slots    = context.user_data.get("duration_slots", 1)
    total_mins = context.user_data.get("duration_mins",  30)
    time_range = context.user_data.get("time_range", t)
    date_str   = _fmt_date(d, lang)

    if not _can_fit(d, t, n_slots):
        await query.edit_message_text(tx(uid, "slot_race"))
        context.user_data.clear()
        return ConversationHandler.END

    # Prevent double-booking: one active booking per user
    has_confirmed = any(bk.get("user_id") == uid for bk in appointments.values())
    has_pending   = any(bk.get("user_id") == uid for bk in pending_bookings.values())
    if has_confirmed or has_pending:
        await query.edit_message_text(tx(uid, "already_booked"))
        context.user_data.clear()
        return ConversationHandler.END

    bid = _next_id()
    pending_bookings[bid] = {
        "slot_key":       slot_key,
        "user_id":        uid,
        "user_lang":      lang,
        "chat_id":        update.effective_chat.id,
        "name":           name,
        "phone":          phone,
        "services":       services,
        "duration_slots": n_slots,
        "duration_mins":  total_mins,
        "time_range":     time_range,
        "date_str":       date_str,
        "time":           t,
        "booked_at":      datetime.now(tz=TZ).isoformat(),
    }
    logger.info("Pending #%d: %s → %s (%d slots)", bid, slot_key, name, n_slots)
    customer_cache.setdefault(uid, {}).update(name=name, phone=phone)
    _db_save_pending(bid, pending_bookings[bid])
    _db_save_customer(uid)
    _schedule_pending_timeout(context.application, bid, timedelta(minutes=30))

    # Barber notification — always in Russian, with prices
    svc_ru      = ", ".join(_svc_label(s, "ru") for s in services)
    total_price = _calc_total_price(services)
    price_ru    = _price_line(total_price, "ru")
    price_part  = f"\n{price_ru}" if price_ru else ""
    barber_msg = await _send_to_all_barbers(
        query.get_bot(),
        text=(
            f"🔔 <b>Новая заявка!</b>\n\n"
            f"📅 {date_str}\n"
            f"🕐 {time_range}\n"
            f"👤 {name}\n"
            f"📞 {phone}\n"
            f"✂️ {svc_ru}\n"
            f"⏱ ~{total_mins} мин.{price_part}"
        ),
        parse_mode="HTML",
        reply_markup=_approval_keyboard(bid),
    )
    if barber_msg:
        pending_bookings[bid]["barber_msg_id"] = barber_msg.message_id
        _db_save_pending(bid, pending_bookings[bid])

    await query.edit_message_text(
        tx(uid, "waiting", date=date_str, time=time_range),
        parse_mode="HTML",
    )
    context.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────── Barber: approve / reject ────────────────────────

async def cb_barber_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query

    if not _is_barber(update.effective_user.id):
        await query.answer("Только мастер может одобрять записи.", show_alert=True)
        return

    action, bid_str = query.data.split("_", 1)
    bid = int(bid_str)

    if bid not in pending_bookings:
        # Already processed (e.g. double-click) — just dismiss the spinner
        await query.answer("Уже обработано.", show_alert=False)
        return

    await query.answer()

    booking   = pending_bookings.pop(bid)
    _db_delete_pending(bid)
    _cancel_pending_timeout(context.application, bid)
    cust_lang = booking.get("user_lang", "ru")

    if action == "approve":
        appointments[booking["slot_key"]] = booking
        _db_save_booking(booking["slot_key"], booking)
        _schedule_reminder(context.application, booking)
        _schedule_barber_reminder(context.application, booking)
        logger.info("Approved #%d %s", bid, booking["slot_key"])
        await query.edit_message_text(
            query.message.text + "\n\n✅ <b>Одобрено</b>", parse_mode="HTML"
        )
        cust_text = STRINGS[cust_lang]["approved"].format(
            date=booking["date_str"],
            time=booking["time_range"],
            svcs=", ".join(_svc_client_label(s, cust_lang) for s in booking["services"]),
        )
    else:
        logger.info("Rejected #%d %s", bid, booking["slot_key"])
        await query.edit_message_text(
            query.message.text + "\n\n❌ <b>Отклонено</b>", parse_mode="HTML"
        )
        cust_text = STRINGS[cust_lang]["rejected"].format(
            date=booking["date_str"], time=booking["time_range"]
        )

    try:
        await query.get_bot().send_message(
            chat_id=booking["chat_id"], text=cust_text,
            parse_mode="HTML", reply_markup=_main_menu_kb(cust_lang),
        )
    except Exception as exc:
        logger.error("Customer notify failed: %s", exc)


# ─────────────────────────── Barber: cancel confirmed booking ─────────────────
# Step 1 — confirmation prompt  callback_data: "bconfirm_2026-02-24_10:00"
async def cb_barber_confirm_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_barber(update.effective_user.id):
        return

    encoded  = query.data[len("bconfirm_"):]
    slot_key = encoded.replace("_", " ", 1)

    bk = appointments.get(slot_key)
    if not bk:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    text = (
        f"⚠️ <b>Подтвердите отмену</b>\n\n"
        f"👤 {bk['name']}  <i>({bk.get('phone', '')})</i>\n"
        f"📅 {bk.get('date_str', slot_key.split()[0])}\n"
        f"🕐 {bk.get('time_range', slot_key.split()[1])}\n\n"
        f"Отменить эту запись?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, отменить", callback_data=f"bcancel_{encoded}")],
        [InlineKeyboardButton("← Назад",        callback_data=f"bselect_{encoded}")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


# Step 2 — actual cancel  callback_data: "bcancel_2026-02-24_10:00"
async def cb_barber_cancel_booking(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_barber(update.effective_user.id):
        await query.answer("Только мастер может отменять записи.", show_alert=True)
        return

    encoded  = query.data[len("bcancel_"):]       # "2026-02-24_10:00"
    slot_key = encoded.replace("_", " ", 1)        # "2026-02-24 10:00"

    if slot_key not in appointments:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    booking   = appointments.pop(slot_key)
    _db_delete_booking(slot_key)
    _cancel_reminder(context.application, slot_key)
    _cancel_barber_reminder(context.application, slot_key)
    cust_lang = booking.get("user_lang", "ru")
    logger.info("Barber cancelled: %s (%s)", slot_key, booking["name"])

    try:
        await query.get_bot().send_message(
            chat_id=booking["chat_id"],
            text=STRINGS[cust_lang]["cancelled_barber"].format(
                date=booking.get("date_str", slot_key.split(" ")[0]),
                time=booking.get("time_range", slot_key.split(" ")[1]),
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Customer cancel-notify failed: %s", exc)

    manage_text, manage_kb = _build_manage_list()
    try:
        await query.edit_message_text(
            f"✅ Запись <b>{booking['name']}</b> отменена.\n\n" + manage_text,
            parse_mode="HTML",
            reply_markup=manage_kb,
        )
    except Exception:
        await query.message.reply_text(f"✅ Запись {booking['name']} отменена.")


# ─────────────────────────── Booking management (barber) ─────────────────────

def _all_upcoming_bookings() -> list[tuple[str, dict]]:
    """All confirmed bookings from now on, sorted chronologically."""
    now = datetime.now(tz=TZ)
    result = []
    for slot_key, bk in appointments.items():
        d_str, t_str = slot_key.split(" ")
        appt_dt = datetime(*map(int, d_str.split("-")), int(t_str[:2]), int(t_str[3:5]), 0, tzinfo=TZ)
        if appt_dt >= now - timedelta(hours=1):
            result.append((slot_key, bk))
    return sorted(result, key=lambda x: x[0])


def _build_manage_list() -> tuple[str, InlineKeyboardMarkup]:
    bookings = _all_upcoming_bookings()
    now = datetime.now(tz=TZ)
    upcoming_blocked = sorted(
        s for s in blocked_slots
        if datetime(*map(int, s.replace(" ", "-").replace(":", "-").split("-")), tzinfo=TZ) >= now
    )

    if not bookings and not upcoming_blocked:
        return (
            "📋 <b>Нет предстоящих записей.</b>",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🚫 Заблокировать слот", callback_data="bblock")],
                [InlineKeyboardButton("← Закрыть",            callback_data="bclose")],
            ]),
        )

    lines = ["📋 <b>Предстоящие записи — выберите:</b>\n"]
    buttons = []
    for slot_key, bk in bookings:
        d = date.fromisoformat(slot_key.split(" ")[0])
        tr = bk.get("time_range", slot_key.split(" ")[1])
        label = f"{_fmt_date_short(d)} {tr} — {bk['name']}"
        enc = slot_key.replace(" ", "_", 1)
        buttons.append([InlineKeyboardButton(label, callback_data=f"bselect_{enc}")])

    if upcoming_blocked:
        lines.append("\n🚫 <b>Заблокированные слоты:</b>")
        for slot_key in upcoming_blocked:
            d = date.fromisoformat(slot_key.split(" ")[0])
            t = slot_key.split(" ")[1]
            enc = slot_key.replace(" ", "_", 1)
            label = f"🔓 {_fmt_date_short(d)} {t}"
            buttons.append([InlineKeyboardButton(label, callback_data=f"bblkunblock_{enc}")])

    buttons.append([InlineKeyboardButton("🚫 Заблокировать слот", callback_data="bblock")])
    buttons.append([InlineKeyboardButton("← Закрыть",            callback_data="bclose")])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def cb_bmanage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_barber(update.effective_user.id):
        await query.answer()
        return
    await query.answer()
    text, kb = _build_manage_list()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def cb_bselect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_barber(update.effective_user.id):
        await query.answer()
        return
    await query.answer()

    encoded  = query.data[len("bselect_"):]
    slot_key = encoded.replace("_", " ", 1)

    if slot_key not in appointments:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    bk       = appointments[slot_key]
    svc_text = ", ".join(_svc_label(s, "ru") for s in bk["services"])
    text = (
        f"📋 <b>Детали записи:</b>\n\n"
        f"👤 <b>{bk['name']}</b>  <i>({bk['phone']})</i>\n"
        f"📅 {bk.get('date_str', slot_key.split()[0])}\n"
        f"🕐 {bk.get('time_range', slot_key.split()[1])}\n"
        f"✂️ {svc_text}\n"
        f"⏱ ~{bk.get('duration_mins', 30)} мин."
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отменить запись", callback_data=f"bconfirm_{encoded}")],
        [InlineKeyboardButton("← Назад",           callback_data="bmanage")],
    ])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def cb_bclose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)


# ─────────────────────────── Schedule builder ────────────────────────────────

def _build_day_schedule(d: date) -> tuple[str, InlineKeyboardMarkup]:
    today    = datetime.now(tz=TZ).date()
    max_date = today + timedelta(days=DAYS_AHEAD)
    iso      = d.isoformat()

    confirmed = {k: v for k, v in appointments.items()     if k.startswith(iso)}
    pending   = {i: b for i, b in pending_bookings.items() if b["slot_key"].startswith(iso)}

    if d == today:
        header = f"📅 <b>Сегодня — {_fmt_date(d)}</b>"
    else:
        header = f"📅 <b>{_fmt_date(d)}</b>"

    if not confirmed and not pending:
        lines = [header, "\nЗаписей нет."]
    else:
        lines = [header + "\n"]
        entries = []

        for sk, bk in confirmed.items():
            t  = sk.split(" ")[1]
            tr = bk.get("time_range", t)
            entries.append((t, "confirmed", sk, bk["name"], bk["phone"],
                            [_svc_label(s, "ru") for s in bk["services"]], tr))

        for _, bk in pending.items():
            t  = bk["slot_key"].split(" ")[1]
            tr = bk.get("time_range", t)
            entries.append((t, "pending", None, bk["name"], bk["phone"],
                            [_svc_label(s, "ru") for s in bk["services"]], tr))

        entries.sort(key=lambda x: x[0])

        for t, status, sk, name, phone, svc, tr in entries:
            icon = "✅" if status == "confirmed" else "⏳"
            lines.append(
                f"{icon} <code>{tr}</code>  <b>{name}</b>  <i>({phone})</i>\n"
                f"    {', '.join(svc)}"
            )
        lines.append("\n<i>✅ подтверждено  |  ⏳ ожидает одобрения</i>")

    # Navigation row
    prev_d = d - timedelta(days=1)
    next_d = d + timedelta(days=1)
    nav: list[InlineKeyboardButton] = []
    if prev_d >= today:
        nav.append(InlineKeyboardButton("← Назад", callback_data=f"bday_{prev_d.isoformat()}"))
    else:
        nav.append(InlineKeyboardButton(" ", callback_data="noop"))
    if d != today:
        nav.append(InlineKeyboardButton("📌 Сегодня", callback_data=f"bday_{today.isoformat()}"))
    if next_d <= max_date:
        nav.append(InlineKeyboardButton("Вперёд →", callback_data=f"bday_{next_d.isoformat()}"))

    rows: list[list[InlineKeyboardButton]] = []
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("📋 Управление записями", callback_data="bmanage")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


# ─────────────────────────── /bookings ───────────────────────────────────────

async def cb_bday_nav(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    d = date.fromisoformat(query.data[len("bday_"):])
    text, kb = _build_day_schedule(d)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_barber(update.effective_user.id):
        await update.message.reply_text("Эта команда только для мастера.")
        return
    text, kb = _build_day_schedule(datetime.now(tz=TZ).date())
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ─────────────────────────── /week ───────────────────────────────────────────

async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_barber(update.effective_user.id):
        await update.message.reply_text("Эта команда только для мастера.")
        return

    lines = ["📅 <b>Расписание на неделю</b>\n"]

    for d in _working_dates():
        iso       = d.isoformat()
        day_label = _fmt_date(d)
        confirmed = {k: v for k, v in appointments.items()     if k.startswith(iso)}
        pending   = {i: b for i, b in pending_bookings.items() if b["slot_key"].startswith(iso)}

        lines.append(f"<b>{day_label}</b>")

        if not confirmed and not pending:
            lines.append("  — свободно\n")
            continue

        entries = []
        for sk, bk in confirmed.items():
            t  = sk.split(" ")[1]
            tr = bk.get("time_range", t)
            entries.append((t, "confirmed", bk["name"], bk["phone"],
                            [_svc_label(s, "ru") for s in bk["services"]], tr))
        for _, bk in pending.items():
            t  = bk["slot_key"].split(" ")[1]
            tr = bk.get("time_range", t)
            entries.append((t, "pending", bk["name"], bk["phone"],
                            [_svc_label(s, "ru") for s in bk["services"]], tr))

        entries.sort(key=lambda x: x[0])

        for t, status, name, phone, svc, tr in entries:
            icon = "✅" if status == "confirmed" else "⏳"
            lines.append(
                f"  {icon} <code>{tr}</code>  {name}  <i>({phone})</i>\n"
                f"       {', '.join(svc)}"
            )
        lines.append("")

    lines.append("<i>✅ подтверждено  |  ⏳ ожидает одобрения</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# ─────────────────────────── Barber: /config working hours ───────────────────

def _config_main_text() -> str:
    cfg  = schedule_config
    days = " ".join(_DAY_SHORT[d] for d in sorted(cfg["work_days"])) or "—"
    miniapp_line = (
        f"\n📱 Mini App:     <b>✅ активен</b>  <code>{MINIAPP_URL}</code>"
        if MINIAPP_ENABLED and MINIAPP_URL else
        "\n📱 Mini App:     <b>❌ выкл</b>  (MINIAPP_ENABLED=false в .env)"
    )
    return (
        "⚙️ <b>Настройка рабочего времени</b>\n\n"
        f"📅 Рабочие дни:  <b>{days}</b>\n"
        f"🕐 Начало:       <b>{cfg['start_hour']:02d}:00</b>\n"
        f"🕐 Конец:        <b>{cfg['end_hour']:02d}:00</b>"
        f"{miniapp_line}"
    )


def _config_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📅 Дни",  callback_data="cfg_days"),
            InlineKeyboardButton("🕐 Часы", callback_data="cfg_hours"),
        ],
        [InlineKeyboardButton("✅ Готово", callback_data="cfg_done")],
    ])


def _config_days_keyboard() -> InlineKeyboardMarkup:
    work_days = schedule_config["work_days"]
    row1, row2 = [], []
    for d in range(4):   # Mon–Thu
        tick = "✓" if d in work_days else "✗"
        row1.append(InlineKeyboardButton(f"{tick} {_DAY_SHORT[d]}", callback_data=f"cfg_day_{d}"))
    for d in range(4, 7):  # Fri–Sun
        tick = "✓" if d in work_days else "✗"
        row2.append(InlineKeyboardButton(f"{tick} {_DAY_SHORT[d]}", callback_data=f"cfg_day_{d}"))
    return InlineKeyboardMarkup([
        row1, row2,
        [InlineKeyboardButton("← Назад", callback_data="cfg_main")],
    ])


def _config_hours_keyboard() -> InlineKeyboardMarkup:
    s = schedule_config["start_hour"]
    e = schedule_config["end_hour"]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▼", callback_data="cfg_start_dec"),
            InlineKeyboardButton(f"Начало  {s:02d}:00", callback_data="cfg_noop"),
            InlineKeyboardButton("▲", callback_data="cfg_start_inc"),
        ],
        [
            InlineKeyboardButton("▼", callback_data="cfg_end_dec"),
            InlineKeyboardButton(f"Конец   {e:02d}:00", callback_data="cfg_noop"),
            InlineKeyboardButton("▲", callback_data="cfg_end_inc"),
        ],
        [InlineKeyboardButton("← Назад", callback_data="cfg_main")],
    ])


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_barber(update.effective_user.id):
        await update.message.reply_text("Эта команда только для мастера.")
        return
    await update.message.reply_text(
        _config_main_text(), parse_mode="HTML", reply_markup=_config_main_keyboard()
    )


async def cb_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not _is_barber(update.effective_user.id):
        return

    data = query.data   # cfg_main | cfg_days | cfg_hours | cfg_day_N |
                        # cfg_start_inc/dec | cfg_end_inc/dec | cfg_done | cfg_noop

    if data == "cfg_noop":
        return

    if data == "cfg_done":
        await query.edit_message_text(
            _config_main_text() + "\n\n✅ <b>Сохранено.</b>",
            parse_mode="HTML",
        )
        return

    if data == "cfg_main":
        await query.edit_message_text(
            _config_main_text(), parse_mode="HTML", reply_markup=_config_main_keyboard()
        )
        return

    if data == "cfg_days":
        await query.edit_message_text(
            "📅 <b>Рабочие дни</b>\n\nНажмите для переключения:",
            parse_mode="HTML",
            reply_markup=_config_days_keyboard(),
        )
        return

    if data == "cfg_hours":
        await query.edit_message_text(
            "🕐 <b>Рабочие часы</b>\n\nНастройте начало и конец рабочего дня:",
            parse_mode="HTML",
            reply_markup=_config_hours_keyboard(),
        )
        return

    if data.startswith("cfg_day_"):
        d = int(data.split("_")[-1])
        work_days = schedule_config["work_days"]
        if d in work_days:
            if len(work_days) > 1:          # keep at least one working day
                work_days.discard(d)
        else:
            work_days.add(d)
        _save_config()
        await query.edit_message_text(
            "📅 <b>Рабочие дни</b>\n\nНажмите для переключения:",
            parse_mode="HTML",
            reply_markup=_config_days_keyboard(),
        )
        return

    if data in ("cfg_start_inc", "cfg_start_dec", "cfg_end_inc", "cfg_end_dec"):
        s = schedule_config["start_hour"]
        e = schedule_config["end_hour"]
        if data == "cfg_start_inc" and s + 1 < e:
            schedule_config["start_hour"] = s + 1
        elif data == "cfg_start_dec" and s - 1 >= 5:
            schedule_config["start_hour"] = s - 1
        elif data == "cfg_end_inc" and e + 1 <= 23:
            schedule_config["end_hour"] = e + 1
        elif data == "cfg_end_dec" and e - 1 > s:
            schedule_config["end_hour"] = e - 1
        _save_config()
        await query.edit_message_text(
            "🕐 <b>Рабочие часы</b>\n\nНастройте начало и конец рабочего дня:",
            parse_mode="HTML",
            reply_markup=_config_hours_keyboard(),
        )
        return


# ─────────────────────────── 30-min reminder job ─────────────────────────────

async def _send_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=STRINGS[data["lang"]]["reminder"].format(time=data["time_range"]),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Reminder send failed: %s", exc)


def _schedule_reminder(app, booking: dict) -> None:
    """Schedule a 30-min-before reminder for a confirmed booking."""
    d_str, t_str = booking["slot_key"].split(" ")
    appt_dt = datetime(
        *map(int, d_str.split("-")), int(t_str[:2]), int(t_str[3:5]), 0, tzinfo=TZ
    )
    remind_at = appt_dt - timedelta(minutes=30)
    if remind_at > datetime.now(tz=TZ):
        app.job_queue.run_once(
            _send_reminder,
            when=remind_at,
            data={
                "chat_id":    booking["chat_id"],
                "lang":       booking.get("user_lang", "ru"),
                "time_range": booking["time_range"],
            },
            name=f"reminder_{booking['slot_key']}",
        )
        logger.info("Reminder scheduled for %s at %s", booking["slot_key"], remind_at)


def _cancel_reminder(app, slot_key: str) -> None:
    """Remove any pending reminder job for a slot."""
    for job in app.job_queue.get_jobs_by_name(f"reminder_{slot_key}"):
        job.schedule_removal()


# ─────────────────────────── Barber reminder job ─────────────────────────────

async def _send_barber_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    svc_text = ", ".join(_svc_label(s, "ru") for s in data["services"])
    await _send_to_all_barbers(
        context.bot,
        text=(
            f"🔔 <b>Через 30 мин:</b> {data['name']}\n"
            f"🕐 {data['time_range']}\n"
            f"✂️ {svc_text}"
        ),
        parse_mode="HTML",
    )


def _schedule_barber_reminder(app, booking: dict) -> None:
    """Schedule a 30-min-before reminder to BARBER_CHAT_ID."""
    d_str, t_str = booking["slot_key"].split(" ")
    appt_dt   = datetime(*map(int, d_str.split("-")), int(t_str[:2]), int(t_str[3:5]), 0, tzinfo=TZ)
    remind_at = appt_dt - timedelta(minutes=30)
    if remind_at > datetime.now(tz=TZ):
        app.job_queue.run_once(
            _send_barber_reminder,
            when=remind_at,
            data={
                "name":       booking["name"],
                "time_range": booking.get("time_range", booking["slot_key"].split()[1]),
                "services":   booking.get("services", []),
            },
            name=f"barber_reminder_{booking['slot_key']}",
        )
        logger.info("Barber reminder scheduled for %s at %s", booking["slot_key"], remind_at)


def _cancel_barber_reminder(app, slot_key: str) -> None:
    for job in app.job_queue.get_jobs_by_name(f"barber_reminder_{slot_key}"):
        job.schedule_removal()


# ─────────────────────────── Pending timeout job ─────────────────────────────

async def _pending_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-reject a pending booking if the barber hasn't responded in 30 min."""
    bid = context.job.data["bid"]
    if bid not in pending_bookings:
        return   # Already processed (approved / rejected / cancelled by user)

    bk = pending_bookings.pop(bid)
    _db_delete_pending(bid)
    cust_lang = bk.get("user_lang", "ru")
    logger.info("Pending #%d auto-timed-out: %s", bid, bk["slot_key"])

    try:
        await context.bot.send_message(
            chat_id=bk["chat_id"],
            text=STRINGS[cust_lang]["pending_timeout"].format(
                date=bk.get("date_str", bk["slot_key"].split()[0]),
                time=bk.get("time_range", bk["slot_key"].split()[1]),
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Timeout customer notify failed: %s", exc)

    barber_msg_id = bk.get("barber_msg_id")
    timeout_text  = (
        f"⌛ <b>Заявка истекла</b> (30 мин без ответа)\n\n"
        f"👤 {bk['name']}\n"
        f"📅 {bk.get('date_str', bk['slot_key'].split()[0])}\n"
        f"🕐 {bk.get('time_range', bk['slot_key'].split()[1])}"
    )
    for bid in BARBER_CHAT_IDS:
        try:
            if barber_msg_id and bid == BARBER_CHAT_ID:
                await context.bot.edit_message_text(
                    chat_id=bid,
                    message_id=barber_msg_id,
                    text=timeout_text,
                    parse_mode="HTML",
                )
            else:
                await context.bot.send_message(
                    chat_id=bid,
                    text=timeout_text,
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.error("Timeout barber notify %d failed: %s", bid, exc)


def _cancel_pending_timeout(app, bid: int) -> None:
    """Remove the timeout job for a pending booking (when barber or user acts first)."""
    for job in app.job_queue.get_jobs_by_name(f"pending_timeout_{bid}"):
        job.schedule_removal()


def _schedule_pending_timeout(app, bid: int, when) -> None:
    """Schedule auto-rejection of pending booking at `when` (datetime or timedelta)."""
    app.job_queue.run_once(
        _pending_timeout_job,
        when=when,
        data={"bid": bid},
        name=f"pending_timeout_{bid}",
    )


# ─────────────────────────── Customer: /mybooking ────────────────────────────

async def cmd_mybooking(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid  = update.effective_user.id
    lang = _lang(uid)

    # Search confirmed appointments
    found_slot, found_bk, found_status = None, None, None
    for slot_key, bk in appointments.items():
        if bk.get("user_id") == uid:
            found_slot, found_bk, found_status = slot_key, bk, "confirmed"
            break

    # Then pending
    if found_slot is None:
        for bk in pending_bookings.values():
            if bk.get("user_id") == uid:
                found_slot  = bk["slot_key"]
                found_bk    = bk
                found_status = "pending"
                break

    if found_slot is None:
        await update.message.reply_text(tx(uid, "mybooking_none"))
        return

    status_label = tx(uid, "mybooking_confirmed" if found_status == "confirmed" else "mybooking_pending")
    svc_text     = ", ".join(_svc_client_label(s, lang) for s in found_bk["services"])
    dur_unit     = STRINGS[lang]["svc_dur_min"]

    text = (
        tx(uid, "mybooking_header") +
        f"📅 {found_bk.get('date_str', found_slot.split()[0])}\n"
        f"🕐 {found_bk.get('time_range', found_slot.split()[1])}\n"
        f"✂️ {svc_text}\n"
        f"⏱ ~{found_bk.get('duration_mins', 30)} {dur_unit}\n\n"
        f"{status_label}"
    )
    enc = found_slot.replace(" ", "_", 1)
    if found_status == "confirmed":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(tx(uid, "btn_reschedule"),     callback_data=f"uresch_{enc}")],
            [InlineKeyboardButton(tx(uid, "btn_cancel_booking"), callback_data=f"ucancel_{enc}")],
        ])
    else:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton(tx(uid, "btn_cancel_booking"), callback_data=f"ucancel_{enc}")
        ]])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cb_user_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    encoded  = query.data[len("ucancel_"):]
    slot_key = encoded.replace("_", " ", 1)

    # Try confirmed
    if slot_key in appointments and appointments[slot_key].get("user_id") == uid:
        booking = appointments.pop(slot_key)
        _db_delete_booking(slot_key)
        _cancel_reminder(context.application, slot_key)
        _cancel_barber_reminder(context.application, slot_key)
        await _send_to_all_barbers(
            query.get_bot(),
            text=STRINGS["ru"]["cancelled_by_user_barber"].format(
                name=booking["name"],
                date=booking.get("date_str", slot_key.split()[0]),
                time=booking.get("time_range", slot_key.split()[1]),
            ),
            parse_mode="HTML",
        )
        await query.edit_message_text(tx(uid, "cancelled_by_user"), parse_mode="HTML")
        return

    # Try pending
    for bid, bk in list(pending_bookings.items()):
        if bk["slot_key"] == slot_key and bk.get("user_id") == uid:
            pending_bookings.pop(bid)
            _db_delete_pending(bid)
            _cancel_pending_timeout(context.application, bid)
            await _send_to_all_barbers(
                query.get_bot(),
                text=STRINGS["ru"]["cancelled_by_user_barber"].format(
                    name=bk["name"],
                    date=bk.get("date_str", slot_key.split()[0]),
                    time=bk.get("time_range", slot_key.split()[1]),
                ),
                parse_mode="HTML",
            )
            await query.edit_message_text(tx(uid, "cancelled_by_user"), parse_mode="HTML")
            return

    await query.answer("Запись не найдена.", show_alert=True)


# ─────────────────────────── Customer: reschedule ────────────────────────────
# Flow: /mybooking → uresch_ → urdate_ → urtime_ → urconfirm / urback

def _mybooking_keyboard(uid: int, slot_key: str) -> InlineKeyboardMarkup:
    enc = slot_key.replace(" ", "_", 1)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(tx(uid, "btn_reschedule"),     callback_data=f"uresch_{enc}")],
        [InlineKeyboardButton(tx(uid, "btn_cancel_booking"), callback_data=f"ucancel_{enc}")],
    ])


async def cb_user_reschedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start reschedule flow: show new-date picker."""
    query = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    encoded  = query.data[len("uresch_"):]
    slot_key = encoded.replace("_", " ", 1)

    if slot_key not in appointments or appointments[slot_key].get("user_id") != uid:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    context.user_data["reschedule_old_slot"] = slot_key
    context.user_data.pop("reschedule_new_date", None)
    context.user_data.pop("reschedule_new_time", None)

    await query.edit_message_text(
        tx(uid, "reschedule_choose_date"),
        parse_mode="HTML",
        reply_markup=_date_keyboard(lang, date_prefix="urdate", cancel_data="urback"),
    )


async def cb_ur_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked new date — show time picker for that date."""
    query = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    iso      = query.data[len("urdate_"):]
    new_date = date.fromisoformat(iso)
    old_slot = context.user_data.get("reschedule_old_slot")

    context.user_data["reschedule_new_date"] = new_date

    kb = _time_keyboard(
        new_date, lang,
        time_prefix="urtime",
        back_data="urback_date",
        cancel_data="urback",
        exclude_slot_key=old_slot,
    )
    if kb is None:
        await query.edit_message_text(
            tx(uid, "no_slots", date=_fmt_date(new_date, lang)),
            parse_mode="HTML",
            reply_markup=_date_keyboard(lang, date_prefix="urdate", cancel_data="urback"),
        )
        return

    await query.edit_message_text(
        tx(uid, "reschedule_choose_time"),
        reply_markup=kb,
    )


async def cb_ur_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """User picked new time — show reschedule confirmation."""
    query    = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    new_time = query.data[len("urtime_"):]
    new_date = context.user_data.get("reschedule_new_date")
    old_slot = context.user_data.get("reschedule_old_slot")

    if not new_date or not old_slot:
        await query.answer("Сессия истекла. Попробуйте /mybooking.", show_alert=True)
        return

    old_bk = appointments.get(old_slot)
    if not old_bk:
        await query.answer("Запись не найдена.", show_alert=True)
        return

    new_slot = f"{new_date.isoformat()} {new_time}"
    if new_slot == old_slot:
        await query.answer(tx(uid, "same_slot"), show_alert=True)
        return

    n_slots       = old_bk.get("duration_slots", 1)
    new_time_range = _fmt_time_range(new_time, n_slots)
    context.user_data["reschedule_new_time"] = new_time

    old_date_str = old_bk.get("date_str", old_slot.split()[0])
    old_time_str = old_bk.get("time_range", old_slot.split()[1])
    new_date_str = _fmt_date(new_date, lang)

    await query.edit_message_text(
        tx(uid, "reschedule_confirm",
           old_date=old_date_str, old_time=old_time_str,
           new_date=new_date_str, new_time=new_time_range),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(tx(uid, "btn_confirm_reschedule"), callback_data="urconfirm")],
            [InlineKeyboardButton(tx(uid, "btn_cancel"),             callback_data="urback")],
        ]),
    )


async def cb_ur_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute the reschedule: cancel old booking, create new pending."""
    query    = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    old_slot = context.user_data.pop("reschedule_old_slot", None)
    new_date = context.user_data.pop("reschedule_new_date", None)
    new_time = context.user_data.pop("reschedule_new_time", None)

    if not old_slot or not new_date or not new_time:
        await query.edit_message_text("Сессия истекла. Попробуйте /mybooking.")
        return

    if old_slot not in appointments or appointments[old_slot].get("user_id") != uid:
        await query.edit_message_text("Запись не найдена.")
        return

    # Remove old confirmed booking
    old_bk = appointments.pop(old_slot)
    _db_delete_booking(old_slot)
    _cancel_reminder(context.application, old_slot)
    _cancel_barber_reminder(context.application, old_slot)

    # Verify the new slot is free (now that old is freed)
    new_slot   = f"{new_date.isoformat()} {new_time}"
    n_slots    = old_bk.get("duration_slots", 1)

    if not _can_fit(new_date, new_time, n_slots):
        # New slot taken — restore old booking and tell user to pick again
        appointments[old_slot] = old_bk
        _db_save_booking(old_slot, old_bk)
        _schedule_reminder(context.application, old_bk)
        _schedule_barber_reminder(context.application, old_bk)
        context.user_data["reschedule_old_slot"] = old_slot
        context.user_data["reschedule_new_date"] = new_date
        kb = _time_keyboard(
            new_date, lang,
            time_prefix="urtime", back_data="urback_date", cancel_data="urback",
            exclude_slot_key=old_slot,
        )
        await query.edit_message_text(
            tx(uid, "slot_taken"),
            reply_markup=kb or _date_keyboard(lang, date_prefix="urdate", cancel_data="urback"),
        )
        return

    # Create new pending booking with updated slot
    bid          = _next_id()
    new_date_str = _fmt_date(new_date, lang)
    new_tr       = _fmt_time_range(new_time, n_slots)
    new_bk       = {
        **old_bk,
        "slot_key":         new_slot,
        "date_str":         new_date_str,
        "time":             new_time,
        "time_range":       new_tr,
        "booked_at":        datetime.now(tz=TZ).isoformat(),
        "rescheduled_from": old_slot,
        "user_lang":        lang,
    }
    pending_bookings[bid] = new_bk
    _db_save_pending(bid, new_bk)
    _schedule_pending_timeout(context.application, bid, timedelta(minutes=30))

    logger.info("Reschedule #%d: %s → %s (%s)", bid, old_slot, new_slot, old_bk["name"])

    old_date_str = old_bk.get("date_str", old_slot.split()[0])
    old_time_str = old_bk.get("time_range", old_slot.split()[1])
    svc_ru       = ", ".join(_svc_label(s, "ru") for s in old_bk["services"])
    await _send_to_all_barbers(
        query.get_bot(),
        text=(
            f"🔄 <b>Запрос на перенос!</b>\n\n"
            f"👤 {old_bk['name']}\n"
            f"📞 {old_bk['phone']}\n"
            f"❌ Было:  {old_date_str}  {old_time_str}\n"
            f"✅ Стало: {new_date_str}  {new_tr}\n"
            f"✂️ {svc_ru}\n"
            f"⏱ ~{old_bk.get('duration_mins', 30)} мин."
        ),
        parse_mode="HTML",
        reply_markup=_approval_keyboard(bid),
    )

    await query.edit_message_text(
        tx(uid, "reschedule_waiting", date=new_date_str, time=new_tr),
        parse_mode="HTML",
    )


async def cb_ur_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel reschedule — restore mybooking view."""
    query    = update.callback_query
    await query.answer()
    uid      = update.effective_user.id
    lang     = _lang(uid)
    old_slot = context.user_data.pop("reschedule_old_slot", None)
    context.user_data.pop("reschedule_new_date", None)
    context.user_data.pop("reschedule_new_time", None)

    if old_slot and old_slot in appointments and appointments[old_slot].get("user_id") == uid:
        bk       = appointments[old_slot]
        svc_text = ", ".join(_svc_client_label(s, lang) for s in bk["services"])
        dur_unit = STRINGS[lang]["svc_dur_min"]
        text = (
            tx(uid, "mybooking_header") +
            f"📅 {bk.get('date_str', old_slot.split()[0])}\n"
            f"🕐 {bk.get('time_range', old_slot.split()[1])}\n"
            f"✂️ {svc_text}\n"
            f"⏱ ~{bk.get('duration_mins', 30)} {dur_unit}\n\n"
            f"{tx(uid, 'mybooking_confirmed')}"
        )
        await query.edit_message_text(
            text, parse_mode="HTML",
            reply_markup=_mybooking_keyboard(uid, old_slot),
        )
    else:
        await query.edit_message_text(tx(uid, "mybooking_none"))


async def cb_ur_back_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Go back to date picker within reschedule flow."""
    query = update.callback_query
    await query.answer()
    uid  = update.effective_user.id
    lang = _lang(uid)
    context.user_data.pop("reschedule_new_date", None)
    context.user_data.pop("reschedule_new_time", None)
    await query.edit_message_text(
        tx(uid, "reschedule_choose_date"),
        parse_mode="HTML",
        reply_markup=_date_keyboard(lang, date_prefix="urdate", cancel_data="urback"),
    )


# ─────────────────────────── Barber: block/unblock slots ─────────────────────

async def cb_bblock_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show date picker to choose a slot to block."""
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    await query.edit_message_text(
        "🚫 <b>Заблокировать слот</b>\n\nВыберите дату:",
        parse_mode="HTML",
        reply_markup=_date_keyboard("ru", date_prefix="bblkdate", cancel_data="bmanage"),
    )


async def cb_bblock_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Date chosen — show time picker."""
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    d = date.fromisoformat(query.data[len("bblkdate_"):])
    context.user_data["bblock_date"] = d
    slots = _time_keyboard(d, "ru", time_prefix="bblktime", back_data="bblock", cancel_data="bmanage")
    if not slots:
        await query.edit_message_text("Все слоты на эту дату уже заняты или заблокированы.")
        return
    await query.edit_message_text(
        f"🚫 Выберите время для блокировки\n📅 {_fmt_date(d, 'ru')}:",
        parse_mode="HTML",
        reply_markup=slots,
    )


async def cb_bblock_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Time chosen — ask confirmation."""
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    t = query.data[len("bblktime_"):]
    d = context.user_data.get("bblock_date")
    if not d:
        await query.edit_message_text("Ошибка: дата не выбрана.")
        return
    context.user_data["bblock_time"] = t
    slot_key = f"{d.isoformat()} {t}"
    await query.edit_message_text(
        f"🚫 Заблокировать <b>{_fmt_date(d, 'ru')} {t}</b>?\n\n"
        f"Клиенты не смогут записаться на это время.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Заблокировать", callback_data="bblkconfirm")],
            [InlineKeyboardButton("← Назад",          callback_data=f"bblkdate_{d.isoformat()}")],
        ]),
    )


async def cb_bblock_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm block — save to memory and DB."""
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    d = context.user_data.pop("bblock_date", None)
    t = context.user_data.pop("bblock_time", None)
    if not d or not t:
        await query.edit_message_text("Ошибка: данные не найдены.")
        return
    slot_key = f"{d.isoformat()} {t}"
    blocked_slots.add(slot_key)
    _db_save_blocked(slot_key)
    logger.info("Barber blocked slot: %s", slot_key)
    text, kb = _build_manage_list()
    await query.edit_message_text(
        f"✅ Слот <b>{_fmt_date(d, 'ru')} {t}</b> заблокирован.\n\n" + text,
        parse_mode="HTML",
        reply_markup=kb,
    )


async def cb_bblock_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unblock a previously blocked slot."""
    query = update.callback_query
    await query.answer()
    if not _is_barber(update.effective_user.id):
        return
    encoded  = query.data[len("bblkunblock_"):]
    slot_key = encoded.replace("_", " ", 1)
    blocked_slots.discard(slot_key)
    _db_delete_blocked(slot_key)
    logger.info("Barber unblocked slot: %s", slot_key)
    text, kb = _build_manage_list()
    await query.edit_message_text(
        f"🔓 Слот <b>{slot_key}</b> разблокирован.\n\n" + text,
        parse_mode="HTML",
        reply_markup=kb,
    )


# ─────────────────────────── Fallback ────────────────────────────────────────

async def handle_unexpected(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(tx(update.effective_user.id, "unexpected"))


# ─────────────────────────── Application assembly ────────────────────────────

async def _post_init(app: Application) -> None:
    """Register bot command menus and reschedule jobs for loaded data."""
    from telegram import BotCommand, BotCommandScopeChat, BotCommandScopeDefault

    # Reschedule customer + barber reminders for confirmed appointments loaded from DB
    for slot_key, bk in appointments.items():
        _schedule_reminder(app, bk)
        _schedule_barber_reminder(app, bk)

    # Reschedule pending-timeout jobs for bookings loaded from DB on restart
    now = datetime.now(tz=TZ)
    for bid, bk in list(pending_bookings.items()):
        booked_at_raw = bk.get("booked_at")
        if booked_at_raw:
            booked_at = datetime.fromisoformat(booked_at_raw)
            if booked_at.tzinfo is None:
                booked_at = booked_at.replace(tzinfo=TZ)
            expires_at = booked_at + timedelta(minutes=30)
        else:
            expires_at = now  # unknown age — expire immediately
        # If already past deadline, fire in 5 s; otherwise at the original deadline
        when = max(expires_at, now + timedelta(seconds=5))
        _schedule_pending_timeout(app, bid, when)
        logger.info("Re-scheduled timeout for pending #%d at %s", bid, when)

    customer_commands = [
        BotCommand("start",      "📅 Book / Записаться / Yozilish"),
        BotCommand("mybooking",  "📋 My booking / Моя запись / Mening yozilishim"),
        BotCommand("settings",   "⚙️ Language / Язык / Til"),
        BotCommand("help",       "ℹ️ Help / Помощь / Yordam"),
    ]
    barber_commands = customer_commands + [
        BotCommand("bookings", "📋 Today's schedule / Сегодня"),
        BotCommand("week",     "🗓 Weekly schedule / Неделя"),
        BotCommand("config",   "⚙️ Working hours / Рабочее время"),
    ]

    # Default menu for all customers
    await app.bot.set_my_commands(customer_commands, scope=BotCommandScopeDefault())
    # Extended menu for all barbers
    for bid in BARBER_CHAT_IDS:
        await app.bot.set_my_commands(
            barber_commands, scope=BotCommandScopeChat(chat_id=bid)
        )
    logger.info("Bot command menus registered.")


def build_application() -> Application:
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    _book_filter = filters.TEXT & filters.Regex(
        "^(" + "|".join(re.escape(t) for t in MENU_BOOK_TEXTS) + ")$"
    )
    _lang_filter = filters.TEXT & filters.Regex(
        "^(" + "|".join(re.escape(t) for t in MENU_LANG_TEXTS) + ")$"
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(_book_filter, cmd_start),
        ],
        states={
            # Patterns are intentionally narrow so that unrelated callbacks
            # (e.g. setlang_) fall through and are handled by global handlers.
            STATE_LANG:     [CallbackQueryHandler(cb_lang_selected,
                                                   pattern=r"^lang_(ru|uz)$")],
            STATE_DATE:     [CallbackQueryHandler(cb_date_selected,
                                                   pattern=r"^(date_\d{4}-\d{2}-\d{2}|cancel)$")],
            STATE_TIME:     [CallbackQueryHandler(cb_time_selected,
                                                   pattern=r"^(time_\d{2}:\d{2}|back_to_date|cancel)$")],
            STATE_NAME:     [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name)],
            STATE_PHONE:    [
                MessageHandler(filters.CONTACT, handle_phone_contact),
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_phone_text),
            ],
            STATE_SERVICES: [CallbackQueryHandler(cb_service_toggle,
                                                   pattern=r"^(svc_\w+|services_done|cancel)$")],
            STATE_CONFIRM:  [CallbackQueryHandler(cb_confirm,
                                                   pattern=r"^(confirm_yes|cancel)$")],
        },
        fallbacks=[
            CommandHandler("cancel",   cmd_cancel),
            # /settings mid-conversation: change language, then end the booking flow
            CommandHandler("settings", _settings_in_conv),
            CommandHandler("help",     cmd_help),
            CommandHandler("info",     cmd_info),
            # "Choose language" persistent menu button mid-conversation
            MessageHandler(_lang_filter, _settings_in_conv),
            # barber config callbacks must work even if barber is in conversation state
            CallbackQueryHandler(cb_config, pattern=r"^cfg_"),
            MessageHandler(filters.ALL, handle_unexpected),
        ],
        allow_reentry=True,
        conversation_timeout=600,
    )

    app.add_handler(conv)

    # Global handlers — process callbacks that the ConversationHandler lets through
    app.add_handler(CallbackQueryHandler(cb_barber_decision,
                                          pattern=r"^(approve|reject)_\d+$"))
    app.add_handler(CallbackQueryHandler(cb_bmanage,  pattern=r"^bmanage$"))
    app.add_handler(CallbackQueryHandler(cb_bselect,
                                          pattern=r"^bselect_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_bclose,   pattern=r"^bclose$"))
    app.add_handler(CallbackQueryHandler(cb_barber_confirm_cancel,
                                          pattern=r"^bconfirm_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_barber_cancel_booking,
                                          pattern=r"^bcancel_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_setlang,
                                          pattern=r"^setlang_(ru|uz)$"))

    app.add_handler(CallbackQueryHandler(cb_bday_nav, pattern=r"^bday_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CommandHandler("bookings",  cmd_bookings))
    app.add_handler(CommandHandler("week",      cmd_week))
    app.add_handler(CommandHandler("settings",  cmd_settings))
    app.add_handler(CommandHandler("config",    cmd_config))
    app.add_handler(CommandHandler("mybooking", cmd_mybooking))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("info",      cmd_info))
    # /cancel also works outside an active booking conversation
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    # Persistent menu "language" button (outside conversation)
    app.add_handler(MessageHandler(_lang_filter, cmd_menu_lang))
    app.add_handler(CallbackQueryHandler(cb_user_cancel,
                                          pattern=r"^ucancel_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    # Reschedule flow
    app.add_handler(CallbackQueryHandler(cb_user_reschedule,
                                          pattern=r"^uresch_\d{4}-\d{2}-\d{2}_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_ur_date,
                                          pattern=r"^urdate_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_ur_time,
                                          pattern=r"^urtime_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_ur_confirm,    pattern=r"^urconfirm$"))
    app.add_handler(CallbackQueryHandler(cb_ur_back,       pattern=r"^urback$"))
    app.add_handler(CallbackQueryHandler(cb_ur_back_date,  pattern=r"^urback_date$"))
    # Block/unblock time slots (barber)
    app.add_handler(CallbackQueryHandler(cb_bblock_start,   pattern=r"^bblock$"))
    app.add_handler(CallbackQueryHandler(cb_bblock_date,    pattern=r"^bblkdate_\d{4}-\d{2}-\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_bblock_time,    pattern=r"^bblktime_\d{2}:\d{2}$"))
    app.add_handler(CallbackQueryHandler(cb_bblock_confirm, pattern=r"^bblkconfirm$"))
    app.add_handler(CallbackQueryHandler(cb_bblock_unblock, pattern=r"^bblkunblock_"))
    # "cancel" inline button from an expired conversation keyboard
    app.add_handler(CallbackQueryHandler(_cancel_cb, pattern=r"^cancel$"))
    app.add_handler(CallbackQueryHandler(cb_config, pattern=r"^cfg_"))
    app.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(),
                                          pattern=r"^noop$"))

    return app


# ─────────────────────────── Entry point ─────────────────────────────────────

def main() -> None:
    logger.info("Starting Barber Shop Bot…")
    _init_db()
    _load_all()
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
