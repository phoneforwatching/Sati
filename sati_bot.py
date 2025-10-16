# -*- coding: utf-8 -*-

"""
Sati App — Telegram Bot
Version: v5.1 (No Letting-Go + Short Gemini)

Changes:
- Remove Letting-Go from UI, storage, summaries
- Gemini via google-genai Client; short, concise response to avoid errors
"""

import os
from pathlib import Path
import csv
import json
import shutil
import tempfile
import statistics
import datetime as dt
import time
from typing import Dict, Any, List, Tuple
from zoneinfo import ZoneInfo
from collections import Counter

from dotenv import load_dotenv
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove, ReplyKeyboardMarkup, KeyboardButton
from apscheduler.schedulers.background import BackgroundScheduler

# ===== Gemini (per Quickstart) =====
GENAI_OK = True
try:
    from google import genai   # pip install google-genai
except Exception:
    GENAI_OK = False

# ---------- CONFIG ----------
# Load .env preferentially from the script directory so running the script
# from another CWD still picks up the project's .env file.
SCRIPT_DIR = Path(__file__).resolve().parent
dotenv_path = SCRIPT_DIR / ".env"
if dotenv_path.exists():
    load_dotenv(dotenv_path=str(dotenv_path))
else:
    # try to find a .env file (search upwards)
    try:
        from dotenv import find_dotenv
        found = find_dotenv()
    except Exception:
        found = ""
    if found:
        load_dotenv(found)
    else:
        # fallback to default behavior (may load from current working dir)
        load_dotenv()

def _mask_token(t: str) -> str:
    if not t:
        return ""
    # show only first 4 and last 4 chars
    if len(t) <= 10:
        return "*" * len(t)
    return t[:4] + "..." + t[-4:]


# Load BOT_TOKEN securely: order of precedence
# 
# 
# ) BOT_TOKEN env
# 2) BOT_TOKEN_FILE env -> read file contents
# 3) interactive prompt (getpass) if running in a tty
import sys, getpass
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Optionally allow running without Telegram for local/dev testing
SKIP_TELEGRAM = bool(os.getenv("SKIP_TELEGRAM"))  # set to any non-empty value to skip Telegram API calls

print(f"Bot token: {BOT_TOKEN}")

_env_csv = os.getenv("CSV_PATH")
if _env_csv:
    _p = Path(_env_csv)
    CSV_PATH = str(_p if _p.is_absolute() else SCRIPT_DIR.joinpath(_p))
else:
    CSV_PATH = str(SCRIPT_DIR / "sati_logs.csv")
SUBS_PATH = os.getenv("SUBS_PATH", "subscribers.json")
CHAT_ID_DEFAULT = os.getenv("CHAT_ID")  # optional start notice
TZ_NAME = os.getenv("TZ", "Asia/Bangkok")
TZ = ZoneInfo(TZ_NAME)

# Gemini
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")  # ใช้รุ่น flash ตาม docs
GENAI_CLIENT = None
if GENAI_OK and GEMINI_API_KEY:
    try:
        GENAI_CLIENT = genai.Client(api_key=GEMINI_API_KEY)
    except Exception:
        GENAI_CLIENT = None

CANCEL_TEXT = "ยกเลิก"
TAGS: List[str] = ["งาน", "คนรัก", "ครอบครัว", "เงิน", "สุขภาพ", "ตัวเอง", "อื่นๆ"]

# CSV schema (ไม่มี letting_go แล้ว)
CSV_FIELDS = [
    "timestamp_iso", "user_id", "username",
    "chat_id",
    "tag",
    "event_desc",
    "dissatisfaction_score", "dissatisfaction_reason",
    "reaction_desc", "reaction_score", "reaction_reason"
]

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
STATE: Dict[int, Dict[str, Any]] = {}

# ---------- Utils ----------
def now_local() -> dt.datetime:
    return dt.datetime.now(TZ)

def username_from(x) -> str:
    u = x.from_user
    if getattr(u, "username", None):
        return f"@{u.username}"
    name = (u.first_name or "") + (" " + u.last_name if getattr(u, "last_name", None) else "")
    return name.strip() or str(u.id)

def cancel_flow(m_or_c):
    uid = m_or_c.from_user.id
    STATE.pop(uid, None)
    if hasattr(m_or_c, "message"):  # callback
        bot.edit_message_text("ยกเลิกการบันทึกแล้วครับ ✅",
                              chat_id=m_or_c.message.chat.id,
                              message_id=m_or_c.message.message_id)
    else:
        bot.send_message(m_or_c.chat.id, "ยกเลิกการบันทึกแล้วครับ ✅", reply_markup=ReplyKeyboardRemove())

# ---------- CSV helpers ----------
def init_csv_if_needed():
    global CSV_PATH
    csvp = Path(CSV_PATH)
    try:
        if not csvp.exists():
            csvp.parent.mkdir(parents=True, exist_ok=True)
            with open(csvp, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(CSV_FIELDS)
        return
    except OSError:
        # Try fallbacks: user's home, then /tmp
        fallbacks = [Path.home() / "sati_logs.csv", Path("/tmp") / f"sati_logs_{os.getpid()}.csv"]
        for cand in fallbacks:
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                with open(cand, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(CSV_FIELDS)
                CSV_PATH = str(cand)
                print(f"CSV path fallback in use: {CSV_PATH}")
                return
            except OSError:
                continue
        # re-raise if no fallback worked
        raise

def migrate_csv_strip_letting_go_if_needed():
    """รองรับกรณีไฟล์เก่ามี letting_go_score -> ย้ายมา schema ใหม่โดยทิ้งคอลัมน์นั้น"""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_FIELDS)
        return
    header = rows[0]
    # ถ้า header เท่ากับของใหม่ ไม่ต้องทำอะไร
    if header == CSV_FIELDS:
        return
    # map เท่าที่มีในไฟล์เก่า (ignore letting_go_score)
    idx = {h: i for i, h in enumerate(header)}
    tmp = tempfile.NamedTemporaryFile("w", delete=False, newline="", encoding="utf-8")
    with tmp as tf:
        w = csv.writer(tf)
        w.writerow(CSV_FIELDS)
        for r in rows[1:]:
            def val(k, default=""):
                j = idx.get(k)
                return r[j] if j is not None and j < len(r) else default
            w.writerow([
                val("timestamp_iso"), val("user_id"), val("username"),
                val("chat_id"),
                val("tag"),
                val("event_desc"),
                val("dissatisfaction_score"), val("dissatisfaction_reason"),
                val("reaction_desc"), val("reaction_score"), val("reaction_reason")
            ])
    shutil.move(tmp.name, CSV_PATH)

def ensure_csv_ready():
    init_csv_if_needed()
    migrate_csv_strip_letting_go_if_needed()

def save_row(row: Dict[str, Any]):
    ensure_csv_ready()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            row.get("timestamp_iso",""), row.get("user_id",""), row.get("username",""),
            row.get("chat_id",""),
            row.get("tag",""),
            row.get("event_desc",""),
            row.get("dissatisfaction_score",0), row.get("dissatisfaction_reason",""),
            row.get("reaction_desc",""), row.get("reaction_score",0), row.get("reaction_reason","")
        ])

def load_rows() -> List[Dict[str, str]]:
    ensure_csv_ready()
    with open(CSV_PATH, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def load_last_row_for_user(uid_str: str):
    last = None
    for row in load_rows():
        if row.get("user_id") == uid_str:
            last = row
    return last

def delete_last_row_for_user(uid_str: str):
    rows = load_rows()
    target_idx = None
    for i in range(len(rows)-1, -1, -1):
        if rows[i].get("user_id") == uid_str:
            target_idx = i
            break
    if target_idx is None:
        return None
    removed = rows.pop(target_idx)
    tmp = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8", newline="")
    with tmp as tf:
        w = csv.DictWriter(tf, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in rows:
            for k in CSV_FIELDS:
                row.setdefault(k, "")
            w.writerow(row)
    shutil.move(tmp.name, CSV_PATH)
    return removed

# ---------- Subscribers ----------
def load_subs() -> List[int]:
    if not os.path.exists(SUBS_PATH):
        return []
    try:
        with open(SUBS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [int(x) for x in data]
    except Exception:
        pass
    return []

def save_subs(subs: List[int]):
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump(subs, f)

# ---------- Keyboards ----------
def kb_tags() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(*[InlineKeyboardButton(t, callback_data=f"tag:{t}") for t in TAGS])
    kb.add(InlineKeyboardButton("🔁 บันทึกเหตุการณ์เดิมอีกครั้ง", callback_data="use_last"))
    kb.add(InlineKeyboardButton("❌ ยกเลิก", callback_data="cancel"))
    return kb

def kb_score_inline(kind: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=5)
    buttons = [InlineKeyboardButton(str(i), callback_data=f"{kind}:{i}") for i in range(1, 11)]
    kb.add(*buttons[:5]); kb.add(*buttons[5:])
    kb.add(InlineKeyboardButton("❌ ยกเลิก", callback_data="cancel"))
    return kb

def kb_confirm_inline() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("⬅️ ย้อนกลับ", callback_data="back"),
        InlineKeyboardButton("✅ ยืนยันบันทึก", callback_data="confirm")
    )
    kb.add(InlineKeyboardButton("❌ ยกเลิก", callback_data="cancel"))
    return kb

def main_reply_kb():
    """Reply keyboard หัวข้อที่ใช้บ่อย เพื่อให้กดได้สะดวก"""
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton("/log"), KeyboardButton("/meditation"), KeyboardButton("/today"))
    kb.row(KeyboardButton("/meds_today"), KeyboardButton("/export"), KeyboardButton("/undo"))
    kb.row(KeyboardButton("/subscribe_daily"), KeyboardButton("/unsubscribe"))
    return kb

# ---------- Gemini reflection (SHORT) ----------
def gemini_reflection(event: str,
                      diss_score: int, diss_reason: str,
                      react_desc: str, react_score: int) -> str:
    """
    เรียก Gemini แบบตอบสั้นมาก (retry) ถ้าไม่สำเร็จจะใช้ fallback local response
    """
    def fallback():
        # สร้างคำแนะนำสำรองแบบสั้น (ภาษาไทย)
        return (
            "(AI) ไม่สามารถเรียก Gemini ได้ — ข้อความสะท้อนสำรอง:\n"
            "หลักธรรม: ยอมรับความไม่เที่ยง, ลดอวิชชา/อุปทาน\n"
            "แนวปฏิบัติ:\n"
            "• หายใจเข้ายาว ๆ 3 ครั้งเมื่อรู้สึกขึ้น\n"
            "• ยอมรับความรู้สึกโดยไม่ตัดสิน 1 นาที\n"
            "• จดสั้น ๆ สิ่งที่เรียนรู้ 1 ข้อ"
        )

    if GENAI_CLIENT is None:
        # ถ้าไม่มี Gemini client ให้ใช้ fallback local response แทน (ไม่ข้าม)
        # ช่วยให้ผู้ใช้ยังได้คำสะท้อนแม้ไม่ได้ตั้งค่า API
        return fallback()

    prompt = f"""
คุณคือพระพุทธเจ้าในพระไตรปิฎก (เชิงสำนวน/แนวคิด)
เหตุการณ์: {event}
ความไม่พอใจ: {diss_score}/10 — {diss_reason}
ปฏิกิริยา: {react_score}/10 — {react_desc}

จงสะท้อนคำสอนอย่างสั้นและกระชับมาก:
• อ้างหลักธรรมที่เกี่ยวข้อง (1–2 ข้อ)
• แนะแนวปฏิบัติแบบลงมือทำได้ทันที (ไม่เกิน 3 bullets)
• ความยาวรวม ≤ 80 คำ
ตอบเป็นภาษาไทยเท่านั้น
""".strip()

    # retry small number of times on transient errors (e.g., 503 overloaded)
    attempts = 3
    for attempt in range(attempts):
        try:
            resp = GENAI_CLIENT.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
            )
            text = (getattr(resp, "text", None) or "").strip()
            if text:
                return text
            # ถ้าได้ empty text ให้ใช้ fallback
            return fallback()
        except Exception as e:
            msg = str(e) or ""
            # หากเป็น overload ให้รอแล้ว retry
            if attempt < attempts - 1 and ("503" in msg or "UNAVAILABLE" in msg or "overloaded" in msg.lower()):
                time.sleep(1 + attempt * 2)
                continue
            # ถ้าเป็นข้อผิดพลาดอื่น ให้คืนข้อความแจ้งผู้ใช้ (สั้น) และ fallback
            try:
                return f"(AI) เกิดข้อผิดพลาดในการเรียก Gemini: {e}\n\n" + fallback()
            except Exception:
                return fallback()
    return fallback()

# ---------- Help ----------
@bot.message_handler(commands=["start", "help"])
def handle_help(m):
    text = (
        "🧘‍♂️ <b>Sati App (v5.1)</b>\n\n"
        "คำสั่ง:\n"
        "• <b>/log</b> เริ่มบันทึก (แท็ก → เหตุการณ์ → คะแนน → ยืนยัน)\n"
        "• <b>/meditation</b> บันทึกสมาธิ (เลือกระยะเวลา ประเภท หมายเหตุ)\n"
        "• <b>/meds_today</b> หรือ <b>/med_summarize</b> สรุปสมาธิวันนี้\n"
        "• <b>/today</b> สรุปวันนี้\n"
        "• <b>/weekly</b> สรุป 7 วันย้อนหลัง\n"
        "• <b>/monthly</b> สรุป 30 วันย้อนหลัง\n"
        "• <b>/subscribe_daily</b> สมัครรับสรุปอัตโนมัติ 21:00\n"
        "• <b>/unsubscribe</b> ยกเลิกสรุปอัตโนมัติ\n"
        "• <b>/undo</b> ลบรายการล่าสุดของฉัน\n"
        "• <b>/export</b> ส่งไฟล์ CSV ทั้งหมด\n"
        "• <b>/export_meds</b> ส่งเฉพาะ meditations.csv\n\n"
        "<b>สเกล</b>\n"
        "• ความไม่พอใจ: 1 = แทบไม่รบกวน | 10 = ไม่พอใจมาก\n"
        "• ความรุนแรงของ React: 10 = สงบมาก | 1 = รุนแรง/ขาดสติ\n\n"
        "หมายเหตุ: มีเมนูปุ่มลัดด้านล่างสำหรับเรียกคำสั่งที่พบบ่อย"
    )
    bot.send_message(m.chat.id, text, reply_markup=main_reply_kb())

# ---------- Log Flow ----------
# steps: tag -> event_desc -> diss_score -> diss_reason -> react_desc -> react_score -> react_reason -> confirm

@bot.message_handler(commands=["log"])
def start_log(m):
    ensure_csv_ready()
    uid = m.from_user.id
    STATE[uid] = {"step": "tag"}
    bot.send_message(
        m.chat.id,
        "🔖 <b>เลือกหมวดเหตุการณ์</b>\n(ช่วยวิเคราะห์ pattern ภายหลัง)",
        reply_markup=kb_tags()
    )

@bot.callback_query_handler(func=lambda c: c.data in ("use_last", "cancel") or c.data.startswith("tag:"))
def on_tag_or_use_last(c):
    uid = c.from_user.id
    if c.data == "cancel":
        return cancel_flow(c)

    if c.data == "use_last":
        last = load_last_row_for_user(str(uid))
        if not last:
            return bot.edit_message_text("ยังไม่มีเหตุการณ์เก่า — โปรดเลือกแท็ก",
                                         chat_id=c.message.chat.id,
                                         message_id=c.message.message_id,
                                         reply_markup=kb_tags())
        STATE[uid] = {
            "step": "diss_score",
            "tag": last.get("tag",""),
            "event_desc": last.get("event_desc","")
        }
        return bot.edit_message_text(
            text=(
                f"ใช้เหตุการณ์เดิม ✅\n• แท็ก: {STATE[uid]['tag'] or '-'}\n"
                f"• เหตุการณ์: {STATE[uid]['event_desc'] or '-'}\n\n"
                "ให้คะแนน <b>ความไม่พอใจ</b> (1–10)\n1 = แทบไม่รบกวน | 10 = ไม่พอใจมาก"
            ),
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            reply_markup=kb_score_inline("diss")
        )

    tag = c.data.split(":", 1)[1]
    STATE[uid] = {"step": "event_desc", "tag": tag}
    bot.edit_message_text(
        f"แท็ก: {tag} ✅\n\nพิมพ์เล่าเหตุการณ์ไม่พอใจแบบสั้น ๆ ได้เลยครับ",
        chat_id=c.message.chat.id,
        message_id=c.message.message_id
    )

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "event_desc", content_types=["text"])
def step_event_desc(m):
    if m.text.strip() == CANCEL_TEXT:
        return cancel_flow(m)
    uid = m.from_user.id
    STATE[uid]["event_desc"] = m.text.strip()
    STATE[uid]["step"] = "diss_score"
    bot.send_message(
        m.chat.id,
        "ให้คะแนน <b>ความไม่พอใจ</b> (1–10)\n1 = แทบไม่รบกวน | 10 = ไม่พอใจมาก",
        reply_markup=kb_score_inline("diss")
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith(("diss:", "react:")) or c.data == "cancel")
def on_score_callback(c):
    uid = c.from_user.id
    if c.data == "cancel":
        return cancel_flow(c)

    kind, val = c.data.split(":")
    val = int(val)
    if kind == "diss":
        STATE.setdefault(uid, {})
        STATE[uid]["dissatisfaction_score"] = val
        STATE[uid]["step"] = "diss_reason"
        bot.edit_message_text(f"ไม่พอใจ: {val}/10 ✅",
                              chat_id=c.message.chat.id,
                              message_id=c.message.message_id)
        bot.send_message(c.message.chat.id, "เหตุผลของความไม่พอใจ (พิมพ์สั้น ๆ)")

    elif kind == "react":
        STATE.setdefault(uid, {})
        STATE[uid]["reaction_score"] = val
        STATE[uid]["step"] = "react_reason"
        bot.edit_message_text(f"React (10=สงบ, 1=รุนแรง): {val}/10 ✅",
                              chat_id=c.message.chat.id,
                              message_id=c.message.message_id)
        bot.send_message(c.message.chat.id, "เหตุผลของการ React (ถ้าไม่มี พิมพ์ - ได้)")

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "diss_reason", content_types=["text"])
def step_diss_reason(m):
    if m.text.strip() == CANCEL_TEXT:
        return cancel_flow(m)
    uid = m.from_user.id
    STATE[uid]["dissatisfaction_reason"] = m.text.strip()
    STATE[uid]["step"] = "react_desc"
    bot.send_message(m.chat.id, "ตอนนั้นคุณเลือกที่จะให้ความหมายกับเหตุการณ์อย่างไร? (เช่น เถียงกลับ, เงียบ, หายใจลึก ฯลฯ)")

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "react_desc", content_types=["text"])
def step_react_desc(m):
    if m.text.strip() == CANCEL_TEXT:
        return cancel_flow(m)
    uid = m.from_user.id
    STATE[uid]["reaction_desc"] = m.text.strip()
    STATE[uid]["step"] = "react_score"
    bot.send_message(
        m.chat.id,
        "ให้คะแนน <b>ความรุนแรงของการ React</b> (1–10)\n10 = สงบมาก | 1 = รุนแรง/ขาดสติ",
        reply_markup=kb_score_inline("react")
    )

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "react_reason", content_types=["text"])
def step_react_reason(m):
    if m.text.strip() == CANCEL_TEXT:
        return cancel_flow(m)
    uid = m.from_user.id
    STATE[uid]["reaction_reason"] = m.text.strip()

    data = STATE.get(uid, {})

    preview = (
        "🔎 <b>ตรวจสอบก่อนบันทึก</b>\n"
        f"• แท็ก: {data.get('tag','-')}\n"
        f"• เหตุการณ์: {data.get('event_desc','-')}\n"
        f"• ไม่พอใจ: {data.get('dissatisfaction_score','-')}/10 — {data.get('dissatisfaction_reason','-')}\n"
        f"• React: {data.get('reaction_score','-')}/10 — {data.get('reaction_desc','-')} ({data.get('reaction_reason','-')})\n\n"
        "กด <b>ยืนยันบันทึก</b> หรือ <b>ย้อนกลับ</b> เพื่อแก้คะแนน React"
    )
    STATE[uid]["step"] = "confirm"
    bot.send_message(m.chat.id, preview, reply_markup=kb_confirm_inline())

@bot.callback_query_handler(func=lambda c: c.data in ("back", "confirm"))
def on_confirm_flow(c):
    uid = c.from_user.id
    data = STATE.get(uid, {})

    if c.data == "back":
        STATE[uid]["step"] = "react_score"
        return bot.edit_message_text(
            "แก้คะแนน <b>ความรุนแรงของการ React</b> (1–10)\n10 = สงบมาก | 1 = รุนแรง/ขาดสติ",
            chat_id=c.message.chat.id,
            message_id=c.message.message_id,
            reply_markup=kb_score_inline("react")
        )

    if c.data == "confirm":
        row = {
            "timestamp_iso": now_local().isoformat(timespec="seconds"),
            "user_id": uid,
            "username": username_from(c),
            "chat_id": c.message.chat.id,
            "tag": data.get("tag",""),
            "event_desc": data.get("event_desc",""),
            "dissatisfaction_score": data.get("dissatisfaction_score", 0),
            "dissatisfaction_reason": data.get("dissatisfaction_reason",""),
            "reaction_desc": data.get("reaction_desc",""),
            "reaction_score": data.get("reaction_score", 0),
            "reaction_reason": data.get("reaction_reason","")
        }
        save_row(row)

        bot.edit_message_text("บันทึกเรียบร้อย ✅",
                              chat_id=c.message.chat.id,
                              message_id=c.message.message_id)
        bot.send_message(
            c.message.chat.id,
            "✅ <b>บันทึกสำเร็จ</b>\n"
            f"• แท็ก: {row['tag'] or '-'}\n"
            f"• เหตุการณ์: {row['event_desc']}\n"
            f"• ไม่พอใจ: {row['dissatisfaction_score']}/10 — {row['dissatisfaction_reason']}\n"
            f"• React: {row['reaction_score']}/10 — {row['reaction_desc']} ({row['reaction_reason']})"
        )

        # Gemini: สะท้อนคำสอนแบบสั้นมาก (UI-friendly + เก็บ state)
        input_for_ai = {
            "event": row["event_desc"],
            "diss_score": int(row["dissatisfaction_score"]),
            "diss_reason": row["dissatisfaction_reason"],
            "react_desc": row["reaction_desc"],
            "react_score": int(row["reaction_score"])
        }
        advice = gemini_reflection(
            event=input_for_ai["event"],
            diss_score=input_for_ai["diss_score"],
            diss_reason=input_for_ai["diss_reason"],
            react_desc=input_for_ai["react_desc"],
            react_score=input_for_ai["react_score"]
        )

        # เก็บข้อมูลเพื่อให้ปุ่ม "ขอคำสอนใหม่ / บันทึก" ทำงานได้
        # เก็บเฉพาะสองคีย์นี้ไว้ใน STATE (ล้างข้อมูล flow ที่เหลือ)
        STATE[uid] = {
            "last_reflection_input": input_for_ai,
            "last_reflection_text": advice
        }

        ui = format_reflection_ui(advice)
        try:
            bot.send_message(c.message.chat.id, ui, reply_markup=kb_reflection_actions())
        except Exception:
            # fallback: ถ้าส่ง UI ไม่ได้ ให้ส่งข้อความดิบแทน
            bot.send_message(c.message.chat.id, f"🧘‍♂️ <b>คำสอนสะท้อนเหตุการณ์</b>\n{advice}")

        # ไม่ลบ STATE ทั้งหมด เพื่อให้ callback handlers ใช้งานได้
        # ...existing code...

# ---------- /undo ----------
@bot.message_handler(commands=["undo"])
def handle_undo(m):
    ensure_csv_ready()
    uid = str(m.from_user.id)
    removed = delete_last_row_for_user(uid)
    if not removed:
        return bot.reply_to(m, "ยังไม่มีรายการของคุณให้ลบครับ")
    bot.reply_to(m, (
        "↩️ ลบรายการล่าสุดของคุณแล้วครับ\n"
        f"• แท็ก: {removed.get('tag','-')}\n"
        f"• เหตุการณ์: {removed.get('event_desc','-')}\n"
        f"• ไม่พอใจ: {removed.get('dissatisfaction_score','-')}/10\n"
        f"• React: {removed.get('reaction_score','-')}/10"
    ))

# ---------- Meditation CSV & helpers ----------
_env_med_csv = os.getenv("MEDITATION_CSV_PATH")
if _env_med_csv:
    _mp = Path(_env_med_csv)
    MEDITATION_CSV_PATH = str(_mp if _mp.is_absolute() else SCRIPT_DIR.joinpath(_mp))
else:
    MEDITATION_CSV_PATH = str(SCRIPT_DIR / "meditations.csv")
MED_CSV_FIELDS = ["timestamp_iso", "user_id", "username", "chat_id", "duration_min", "type", "note"]

def init_med_csv_if_needed():
    global MEDITATION_CSV_PATH
    medp = Path(MEDITATION_CSV_PATH)
    try:
        if not medp.exists():
            medp.parent.mkdir(parents=True, exist_ok=True)
            with open(medp, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(MED_CSV_FIELDS)
        return
    except OSError:
        fallbacks = [Path.home() / "meditations.csv", Path("/tmp") / f"meditations_{os.getpid()}.csv"]
        for cand in fallbacks:
            try:
                cand.parent.mkdir(parents=True, exist_ok=True)
                with open(cand, "w", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow(MED_CSV_FIELDS)
                MEDITATION_CSV_PATH = str(cand)
                print(f"Meditation CSV path fallback in use: {MEDITATION_CSV_PATH}")
                return
            except OSError:
                continue
        raise

def ensure_med_csv_ready():
    init_med_csv_if_needed()

def save_med_row(row: Dict[str, Any]):
    ensure_med_csv_ready()
    with open(MEDITATION_CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            row.get("timestamp_iso",""), row.get("user_id",""), row.get("username",""),
            row.get("chat_id",""),
            row.get("duration_min", 0), row.get("type",""), row.get("note","")
        ])

def load_med_rows() -> List[Dict[str, str]]:
    ensure_med_csv_ready()
    with open(MEDITATION_CSV_PATH, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def med_rows_for_chat_between(chat_id: int, start: dt.date, end_inclusive: dt.date) -> List[Dict[str,str]]:
    res = []
    for row in load_med_rows():
        if str(chat_id) != row.get("chat_id", ""):
            continue
        try:
            ts = dt.datetime.fromisoformat(row.get("timestamp_iso",""))
        except Exception:
            continue
        d = ts.date()
        if start <= d <= end_inclusive:
            res.append(row)
    return res

def summarize_meds_today(chat_id: int) -> str:
    today = now_local().date()
    rows = med_rows_for_chat_between(chat_id, today, today)
    if not rows:
        return f"🧘‍♂️ <b>สรุปสมาธิวันนี้</b> ({today.isoformat()})\nยังไม่มีบันทึกสมาธิวันนี้ครับ"
    total = 0
    durations = []
    for r in rows:
        try:
            dur = int(r.get("duration_min", 0))
        except Exception:
            dur = 0
        durations.append(dur)
        total += dur
    avg = (total / len(durations)) if durations else 0
    return (
        f"🧘‍♂️ <b>สรุปสมาธิวันนี้</b> ({today.isoformat()})\n"
        f"จำนวนครั้ง: {len(durations)}\n"
        f"รวมเวลา: {total} นาที\n"
        f"เฉลี่ยต่อครั้ง: {avg:.1f} นาที"
    )

# ---------- Keyboards for meditation ----------
def kb_med_durations():
    kb = InlineKeyboardMarkup(row_width=4)
    buttons = [InlineKeyboardButton(f"{m} นาที", callback_data=f"med_dur:{m}") for m in (5,10,15,20,30,45,60)]
    kb.add(*buttons[:4]); kb.add(*buttons[4:])
    kb.add(InlineKeyboardButton("กำหนดเอง", callback_data="med_custom"))
    kb.add(InlineKeyboardButton("❌ ยกเลิก", callback_data="med_cancel"))
    return kb

def kb_med_type():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Guided", callback_data="med_type:guided"),
        InlineKeyboardButton("Unguided", callback_data="med_type:unguided")
    )
    kb.add(InlineKeyboardButton("อื่นๆ", callback_data="med_type:other"))
    kb.add(InlineKeyboardButton("❌ ยกเลิก", callback_data="med_cancel"))
    return kb

# ---------- Meditation flow ----------
# steps: med_duration -> med_type -> med_note -> save

@bot.message_handler(commands=["meditate", "meditation"])
def start_meditate(m):
    print(f"[DEBUG] start_meditate called from user={m.from_user.id} chat={m.chat.id}")
    ensure_med_csv_ready()
    uid = m.from_user.id
    STATE[uid] = {"step": "med_duration"}
    try:
        bot.send_message(
            m.chat.id,
            "🧘‍♂️ เริ่มบันทึกสมาธิ — เลือกระยะเวลา",
            reply_markup=kb_med_durations()
        )
    except Exception as e:
        print(f"[ERROR] sending med durations keyboard: {e}")
        bot.send_message(m.chat.id, "เกิดข้อผิดพลาดขณะส่งปุ่ม — แจ้งผู้ดูแล")

@bot.callback_query_handler(func=lambda c: bool(getattr(c, "data", "") and getattr(c, "data", "").startswith(("med_dur:", "med_type:", "med_custom", "med_cancel"))))
def on_med_callback(c):
    print(f"[DEBUG] on_med_callback called user={c.from_user.id} data={getattr(c,'data',None)}")
    uid = c.from_user.id
    data = getattr(c, "data", "") or ""
    if data == "med_cancel":
        return cancel_flow(c)

    if data == "med_custom":
        STATE.setdefault(uid, {})["step"] = "med_custom_duration"
        try:
            return bot.edit_message_text("พิมพ์จำนวนเวลา (นาที) เช่น 25", chat_id=c.message.chat.id, message_id=c.message.message_id)
        except Exception as e:
            print(f"[ERROR] edit_message_text med_custom: {e}")
            return

    if data.startswith("med_dur:"):
        try:
            val = int(data.split(":",1)[1])
        except Exception:
            print(f"[WARN] invalid med_dur value: {data}")
            return bot.answer_callback_query(c.id, "ค่าที่ส่งมาผิด")
        STATE.setdefault(uid, {})["duration_min"] = val
        STATE[uid]["step"] = "med_type"
        try:
            return bot.edit_message_text(f"ระยะเวลา: {val} นาที ✅\n\nเลือกประเภทการนั่งสมาธิ", chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb_med_type())
        except Exception as e:
            print(f"[ERROR] edit_message_text med_dur: {e}")
            return

    if data.startswith("med_type:"):
        typ = data.split(":",1)[1]
        STATE.setdefault(uid, {})["type"] = typ
        STATE[uid]["step"] = "med_note"
        try:
            return bot.edit_message_text("พิมพ์หมายเหตุสั้น ๆ (หรือพิมพ์ - ถ้าไม่มี)", chat_id=c.message.chat.id, message_id=c.message.message_id)
        except Exception as e:
            print(f"[ERROR] edit_message_text med_type: {e}")
            return

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "med_custom_duration", content_types=["text"])
def med_custom_duration(m):
    text = m.text.strip()
    if text == CANCEL_TEXT:
        return cancel_flow(m)
    try:
        minutes = int(float(text))
        if minutes <= 0:
            raise ValueError()
    except Exception:
        return bot.reply_to(m, "โปรดพิมพ์จำนวนเต็มของนาที เช่น 20 หรือพิมพ์ ยกเลิก")
    uid = m.from_user.id
    STATE.setdefault(uid, {})["duration_min"] = minutes
    STATE[uid]["step"] = "med_type"
    bot.send_message(m.chat.id, f"ระยะเวลา: {minutes} นาที ✅\n\nเลือกประเภทการนั่งสมาธิ", reply_markup=kb_med_type())

@bot.message_handler(func=lambda msg: STATE.get(msg.from_user.id,{}).get("step") == "med_note", content_types=["text"])
def med_note_step(m):
    if m.text.strip() == CANCEL_TEXT:
        return cancel_flow(m)
    uid = m.from_user.id
    STATE[uid]["note"] = m.text.strip() if m.text.strip() != "-" else ""
    data = STATE.get(uid, {})
    row = {
        "timestamp_iso": now_local().isoformat(timespec="seconds"),
        "user_id": uid,
        "username": username_from(m),
        "chat_id": m.chat.id,
        "duration_min": int(data.get("duration_min", 0)),
        "type": data.get("type",""),
        "note": data.get("note","")
    }
    save_med_row(row)
    bot.send_message(
        m.chat.id,
        "✅ บันทึกสมาธิเรียบร้อย\n"
        f"• เวลา: {row['duration_min']} นาที\n"
        f"• ประเภท: {row['type'] or '-'}\n"
        f"• หมายเหตุ: {row['note'] or '-'}"
    )
    STATE.pop(uid, None)

# ---------- Meditation summaries ----------
@bot.message_handler(commands=["meds_today", "med_summarize"])
def handle_meds_today(m):
    bot.send_message(m.chat.id, summarize_meds_today(m.chat.id))

@bot.message_handler(commands=["export"])
def handle_export(m):
    """ส่งไฟล์ CSV หลัก และ meditations.csv (ถ้ามี)"""
    ensure_csv_ready()
    ensure_med_csv_ready()

    files_to_send = [CSV_PATH, MEDITATION_CSV_PATH]
    sent_any = False

    for path in files_to_send:
        print(f"[DEBUG] export requested by user={m.from_user.id} file={path}")
        if not os.path.exists(path):
            print(f"[DEBUG] file not found: {path}")
            continue
        try:
            with open(path, "rb") as f:
                bot.send_document(m.chat.id, f, caption=f"Export: {os.path.basename(path)}")
                sent_any = True
        except Exception as e:
            print(f"[ERROR] export failed for {path}: {e}")
            bot.reply_to(m, f"เกิดข้อผิดพลาดขณะส่งไฟล์ {os.path.basename(path)}: {e}")

    if not sent_any:
        bot.reply_to(m, "ยังไม่มีไฟล์ CSV ให้ส่ง")

@bot.message_handler(commands=["export_meds", "export_meditations"])
def handle_export_meds(m):
    """ส่งไฟล์ meditations.csv"""
    ensure_med_csv_ready()
    path = MEDITATION_CSV_PATH
    print(f"[DEBUG] export_meds requested by user={m.from_user.id} file={path}")
    if not os.path.exists(path):
        return bot.reply_to(m, "ยังไม่มีไฟล์บันทึกสมาธิให้ส่ง")
    try:
        with open(path, "rb") as f:
            bot.send_document(m.chat.id, f, caption=f"Export: {os.path.basename(path)}")
    except Exception as e:
        print(f"[ERROR] export_meds failed: {e}")
        bot.reply_to(m, f"เกิดข้อผิดพลาดขณะส่งไฟล์: {e}")

# ---------- Subscribers ----------
# เพิ่ม handlers สำหรับ subscribe / unsubscribe
@bot.message_handler(commands=["subscribe_daily"])
def handle_subscribe_daily(m):
    chat_id = m.chat.id
    subs = load_subs()
    if chat_id in subs:
        bot.reply_to(m, "✅ สถานะ: สมัครรับสรุปอัตโนมัติอยู่แล้ว", reply_markup=main_reply_kb())
    else:
        subs.append(chat_id)
        save_subs(subs)
        bot.reply_to(m, "✅ สมัครรับสรุปอัตโนมัติเรียบร้อยแล้ว", reply_markup=main_reply_kb())
    bot.send_message(chat_id, f"จำนวนผู้รับสรุปปัจจุบัน: {len(subs)}", reply_markup=main_reply_kb())

@bot.message_handler(commands=["unsubscribe"])
def handle_unsubscribe(m):
    chat_id = m.chat.id
    subs = load_subs()
    if chat_id not in subs:
        bot.reply_to(m, "ℹ️ คุณไม่ได้สมัครรับสรุปอัตโนมัติอยู่", reply_markup=main_reply_kb())
    else:
        subs = [s for s in subs if s != chat_id]
        save_subs(subs)
        bot.reply_to(m, "✅ ยกเลิกการรับสรุปอัตโนมัติเรียบร้อยแล้ว", reply_markup=main_reply_kb())
    bot.send_message(chat_id, f"จำนวนผู้รับสรุปปัจจุบัน: {len(subs)}", reply_markup=main_reply_kb())

@bot.message_handler(func=lambda m: isinstance(m.text, str) and not m.text.startswith("/") and STATE.get(m.from_user.id, {}).get("step") is None, content_types=["text"])
def handle_non_command_text(m):
    """
    เมื่อผู้ใช้พิมพ์ข้อความที่ไม่ใช่คำสั่งและไม่ได้อยู่ใน flow ใด ๆ
    ให้ตอบกลับโดยแสดงข้อความที่พิมพ์และแนะนำคำสั่งที่ใช้บ่อย
    """
    try:
        cmds = (
            "/log  /meditation  /today  /weekly  /monthly\n"
            "/subscribe_daily  /unsubscribe  /undo  /export  /export_meds\n"
            "พิมพ์ /help เพื่อดูรายละเอียดเพิ่มเติม"
        )
        reply = (
            f"คุณพิมพ์: {m.text}\n\n"
            "คำสั่งที่ใช้บ่อย:\n"
            f"{cmds}"
        )
        bot.reply_to(m, reply, reply_markup=main_reply_kb())
    except Exception as e:
        print(f"[ERROR] handle_non_command_text: {e}")

def _compute_summary_from_rows(rows: List[Dict[str,str]], start: dt.date, end: dt.date) -> Tuple[int, float, float, List[Tuple[str,int]], List[str]]:
    if not rows:
        return 0, 0.0, 0.0, [], []
    count = len(rows)
    diss_vals = [int(float(r.get("dissatisfaction_score",0))) for r in rows if r.get("dissatisfaction_score","")!="-"]
    react_vals = [int(float(r.get("reaction_score",0))) for r in rows if r.get("reaction_score","")!="-"]
    avg_diss = (sum(diss_vals)/len(diss_vals)) if diss_vals else 0.0
    avg_react = (sum(react_vals)/len(react_vals)) if react_vals else 0.0
    tags = [r.get("tag","-") for r in rows]
    top_tags = Counter(tags).most_common(3)
    samples = [r.get("event_desc","-") for r in rows][:3]
    return count, avg_diss, avg_react, top_tags, samples

def summarize_with_comparison(chat_id: int, start: dt.date, end_inclusive: dt.date) -> str:
    rows = event_rows_for_chat_between(chat_id, start, end_inclusive)
    count, avg_diss, avg_react, top_tags, samples = _compute_summary_from_rows(rows, start, end_inclusive)
    # previous period
    span_days = (end_inclusive - start).days + 1
    prev_end = start - dt.timedelta(days=1)
    prev_start = prev_end - dt.timedelta(days=span_days-1)
    prev_rows = event_rows_for_chat_between(chat_id, prev_start, prev_end)
    p_count, p_avg_diss, p_avg_react, _, _ = _compute_summary_from_rows(prev_rows, prev_start, prev_end)

    def pct_change(current, prev):
        try:
            if prev == 0:
                return "—"
            return f"{(current - prev)/prev*100:.0f}%"
        except Exception:
            return "—"

    if not rows:
        return f"📋 <b>สรุปเหตุการณ์</b> ({start.isoformat()}" + (f" - {end_inclusive.isoformat()}" if start != end_inclusive else "") + ")\nยังไม่มีบันทึกในช่วงนี้ครับ"

    top_tags_str = ", ".join(f"{t} ({c})" for t,c in top_tags) or "-"
    samples_str = "\n".join(f"• {s}" for s in samples) if samples else ""

    return (
        f"📋 <b>สรุปเหตุการณ์</b> ({start.isoformat()}"
        + (f" - {end_inclusive.isoformat()}" if start != end_inclusive else "")
        + ")\n"
        f"จำนวนบันทึก: {count} (ก่อนหน้า: {p_count} | เปลี่ยน: {pct_change(count,p_count)})\n"
        f"ความไม่พอใจ (avg): {avg_diss:.1f}/10 (ก่อนหน้า: {p_avg_diss:.1f} | เปลี่ยน: {pct_change(avg_diss,p_avg_diss)})\n"
        f"React (avg): {avg_react:.1f}/10 (ก่อนหน้า: {p_avg_react:.1f} | เปลี่ยน: {pct_change(avg_react,p_avg_react)})\n"
        f"แท็กยอดนิยม: {top_tags_str}\n\n"
        f"ตัวอย่างเหตุการณ์:\n{samples_str}\n\n"
        "ปุ่ม: ดูรายละเอียด | ส่ง CSV | ขอคำสอนสั้น"
    )

@bot.message_handler(commands=["today"])
def handle_today(m):
    today = now_local().date()
    bot.send_message(m.chat.id, summarize_with_comparison(m.chat.id, today, today))

@bot.message_handler(commands=["weekly"])
def handle_weekly(m):
    today = now_local().date()
    start = today - dt.timedelta(days=6)
    bot.send_message(m.chat.id, summarize_with_comparison(m.chat.id, start, today))

@bot.message_handler(commands=["monthly"])
def handle_monthly(m):
    today = now_local().date()
    start = today - dt.timedelta(days=29)
    bot.send_message(m.chat.id, summarize_with_comparison(m.chat.id, start, today))

def event_rows_for_chat_between(chat_id: int, start: dt.date, end_inclusive: dt.date) -> List[Dict[str,str]]:
    """คืนรายการเหตุการณ์ (จาก CSV) ของ chat_id ระหว่างวันที่ start..end_inclusive"""
    res = []
    for row in load_rows():
        if str(chat_id) != str(row.get("chat_id", "")):
            continue
        ts_str = row.get("timestamp_iso", "")
        try:
            ts = dt.datetime.fromisoformat(ts_str)
        except Exception:
            # รองรับรูปแบบอื่นถ้ามี
            try:
                ts = dt.datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S")
            except Exception:
                continue
        d = ts.date()
        if start <= d <= end_inclusive:
            res.append(row)
    return res

# ---------- Reflection AI ----------
def kb_reflection_actions() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton("🔁 ขอคำสอนใหม่", callback_data="ai_reflect_again"),
        InlineKeyboardButton("💾 บันทึก", callback_data="ai_reflect_save")
    )
    kb.add(InlineKeyboardButton("❌ ปิด", callback_data="ai_reflect_close"))
    return kb

def format_reflection_ui(text: str) -> str:
    """
    แปลงผลข้อความ (จาก AI / fallback) เป็นบล็อกข้อความปกติ (plain text)
    - แยกบรรทัดที่เป็นหัวข้อ / bullets
    - ใส่ header และ emoji เพื่อให้อ่านสบายตา (ไม่มี HTML/Markdown)
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    header = "🧘‍♂️ คำสอนสะท้อนเหตุการณ์\n\n"
    body_lines = []
    for ln in lines:
        if ln.startswith(("•", "-", "• ")) or ln.startswith("•"):
            clean = ln.lstrip("•- ").strip()
            body_lines.append(f"• {clean}")
        elif ln.lower().startswith("หลักธรรม") or ln.startswith("หลักธรรม"):
            # หลักธรรม: ...  -> แสดงเป็นหัวข้อ
            if ":" in ln:
                body_lines.append("📜 หลักธรรม:")
                body_lines.append(ln.split(":",1)[1].strip())
            else:
                body_lines.append(f"📜 {ln}")
        elif ln.lower().startswith("แนวปฏิบัติ") or ln.startswith("แนวปฏิบัติ"):
            if ":" in ln:
                body_lines.append("🛠️ แนวปฏิบัติ:")
                body_lines.extend([f"• {s.strip()}" for s in ln.split(":",1)[1].splitlines() if s.strip()])
            else:
                body_lines.append(f"🛠️ {ln}")
        else:
            body_lines.append(ln)
    body = "\n".join(body_lines)
    footer = '\n\nกด "ขอคำสอนใหม่" เพื่อให้ AI สร้างอีกชุด หรือ "บันทึก" เพื่อเก็บไว้'
    return header + body + footer

# เก็บ input สำหรับการขอคำสอนใหม่ (ใช้เมื่อกดปุ่ม)
# ปรับการส่งคำสอนใน on_confirm_flow: เก็บ last_reflection_input ใน STATE ก่อนเรียก gemini_reflection

@bot.callback_query_handler(func=lambda c: c.data in ("ai_reflect_again","ai_reflect_save","ai_reflect_close"))
def on_ai_reflect_actions(c):
    uid = c.from_user.id
    data = c.data
    if data == "ai_reflect_close":
        try:
            return bot.edit_message_reply_markup(chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=None)
        except Exception:
            return bot.answer_callback_query(c.id, "ปิดแล้ว")
    if data == "ai_reflect_save":
        # อย่างสั้น: บันทึกข้อความสะท้อนลงไฟล์ reflections.txt
        last = STATE.get(uid, {}).get("last_reflection_text")
        if not last:
            bot.answer_callback_query(c.id, "ไม่มีคำสอนให้บันทึก")
            return
        try:
            with open("reflections.txt", "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now(TZ).isoformat()} | user:{uid}\n{last}\n---\n")
            bot.answer_callback_query(c.id, "บันทึกคำสอนเรียบร้อย ✅")
            # ปิดปุ่มหลังบันทึก
            bot.edit_message_reply_markup(chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=None)
        except Exception as e:
            bot.answer_callback_query(c.id, f"ไม่สามารถบันทึก: {e}")
        return
    if data == "ai_reflect_again":
        # ดึง input เดิมจาก STATE แล้วเรียก gemini_reflection ใหม่
        inp = STATE.get(uid, {}).get("last_reflection_input")
        if not inp:
            bot.answer_callback_query(c.id, "ไม่พบข้อมูลเดิมสำหรับการสร้างใหม่")
            return
        bot.answer_callback_query(c.id, "กำลังสร้างคำสอนใหม่…")
        try:
            advice = gemini_reflection(
                event=inp.get("event",""),
                diss_score=int(inp.get("diss_score",0)),
                diss_reason=inp.get("diss_reason",""),
                react_desc=inp.get("react_desc",""),
                react_score=int(inp.get("react_score",0))
            )
        except Exception as e:
            advice = "(AI) เกิดข้อผิดพลาดขณะขอคำสอนใหม่ — ใช้ข้อความสำรอง"
        STATE.setdefault(uid, {})["last_reflection_text"] = advice
        ui = format_reflection_ui(advice)
        try:
            bot.edit_message_text(ui, chat_id=c.message.chat.id, message_id=c.message.message_id, reply_markup=kb_reflection_actions())
        except Exception:
            # ถ้าแก้ข้อความไม่ได้ ให้ส่งใหม่
            bot.send_message(c.message.chat.id, ui, reply_markup=kb_reflection_actions())

if __name__ == "__main__":
    # เบื้องต้นเตรียมไฟล์ CSV/med CSV
    init_csv_if_needed()
    ensure_med_csv_ready()

    # If SKIP_TELEGRAM is enabled, run in offline/dev mode (no Telegram API calls)
    if SKIP_TELEGRAM:
        print("[INFO] SKIP_TELEGRAM enabled — not contacting Telegram API. Running in offline/dev mode.")
        print("[INFO] You can still exercise non-Telegram code paths or run tests. Exiting main loop.")
        # Keep process alive for manual testing if desired, otherwise exit
        # Here we exit with 0 to indicate successful startup in dev mode
        raise SystemExit(0)

    # ป้องกันกรณีลืมตั้ง BOT_TOKEN (runtime check)
    if BOT_TOKEN.startswith("PUT_YOUR_TOKEN") or not BOT_TOKEN.strip():
        print("[ERROR] BOT_TOKEN ยังไม่ได้ตั้งค่า. ตั้งค่าใน .env แล้ว restart")
        raise SystemExit(1)
    # ตรวจสอบความถูกต้องของ token กับ Telegram ก่อนเริ่มงานระยะยาว
    try:
        # เรียก get_me() หนึ่งครั้งเพื่อยืนยันว่า token ใช้งานได้
        me = bot.get_me()
    except Exception as e:
        # พยายามตรวจสอบว่าคือข้อผิดพลาดการอนุญาต (401)
        err_text = str(e)
        if '401' in err_text or 'Unauthorized' in err_text or 'unauthorized' in err_text:
            print("[ERROR] Telegram API authorization failed (401 Unauthorized).\nPlease verify your BOT_TOKEN in the environment (.env) is the bot token from @BotFather and not empty.")
            print(f"[ERROR] Detailed error: {e}")
            raise SystemExit(1)
        # กรณีอื่น ๆ ให้แสดงข้อความและออกด้วย exit
        print(f"[ERROR] Failed to contact Telegram API with get_me(): {e}")
        raise SystemExit(1)

    # เริ่ม scheduler (ถ้ามี job) และเริ่ม polling ของ Telegram bot
    scheduler = BackgroundScheduler(timezone=TZ)
    scheduler.start()
    print(f"[INFO] Starting Sati bot... GENAI_OK={GENAI_OK} GENAI_CLIENT={'present' if GENAI_CLIENT else 'none'} BOT_USER={getattr(me,'username', '<unknown>')}")

    try:
        # ใช้ infinity_polling เพื่อให้รับข้อความตลอด
        bot.infinity_polling(timeout=60, long_polling_timeout=60)
    except KeyboardInterrupt:
        print("[INFO] Shutdown requested (KeyboardInterrupt)")
    except Exception as e:
        # หากเป็นข้อผิดพลาด API แบบ Unauthorized ให้แสดงคำแนะนำและออก
        err_text = str(e)
        if '401' in err_text or 'Unauthorized' in err_text or 'unauthorized' in err_text:
            print("[ERROR] Telegram API returned 401 Unauthorized while polling. This usually means the BOT_TOKEN is invalid or revoked.\nCheck the token in your .env (BOT_TOKEN) and regenerate it from @BotFather if needed.")
            print(f"[ERROR] Detailed error: {e}")
            raise SystemExit(1)
        print(f"[ERROR] polling failed: {e}")
    finally:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass