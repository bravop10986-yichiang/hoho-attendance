import os
import json
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote

from flask import Flask, request, abort

import gspread
from google.oauth2.service_account import Credentials

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent,
    FlexSendMessage
)

"""
全 Postback 極簡版（像 App 一樣）：
- Rich Menu「點名」(postback data=action=attendance) -> 回「星期選擇」Flex（全部 postback）
- 選星期（postback cmd=pick_day&wd=3）-> 回「學生清單」Flex（前 12 + 搜尋）
- 選學生（postback cmd=pick_student&wd=3&name=...）-> 回「堂數選擇」Flex
- 選堂數（postback cmd=pick_lesson&wd=3&name=...&lesson=1）-> 扣堂 + 寫 log + 回「成功卡」（含繼續/改星期/搜尋）
- 搜尋：仍需輸入文字（MessageEvent）：先按 Flex 的「🔍 搜尋」(postback cmd=enter_search&wd=3)
  -> bot 回提示「請輸入 搜尋:關鍵字」
  -> 文字搜尋後回學生清單（仍是 Flex，選學生用 postback）

重要：
- teacher_students 第三欄 weekday：1~7（週一~週日）
- Render 建議 Start Command：gunicorn app:app --workers 1 --threads 2
"""

app = Flask(__name__)

# ====== ENV ======
CHANNEL_ACCESS_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
CHANNEL_SECRET = (os.getenv("LINE_CHANNEL_SECRET") or "").strip()
SHEET_ID = (os.getenv("GOOGLE_SHEET_ID") or "").strip()
SA_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing LINE env vars.")
if not SHEET_ID or not SA_JSON:
    raise RuntimeError("Missing GOOGLE_SHEET_ID or GOOGLE_SERVICE_ACCOUNT_JSON.")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== Google Sheet ======
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds_info = json.loads(SA_JSON)
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SHEET_ID)

ws_students = sh.worksheet("students")
ws_log = sh.worksheet("attendance_log")
ws_teachers = sh.worksheet("teachers")
ws_teacher_students = sh.worksheet("teacher_students")

# ====== Time / TZ ======
TZ_TAIPEI = timezone(timedelta(hours=8))

def now_taipei_str():
    return datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")

def weekday_today_1to7():
    # Mon=1..Sun=7
    return datetime.now(TZ_TAIPEI).isoweekday()

def weekday_label(wd: int) -> str:
    labels = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
    return labels.get(wd, f"週{wd}")

# ====== Simple state (for search mode only) ======
STATE = {}  # uid -> {"mode": "search", "wd": int, "ts": int}
STATE_TIMEOUT_SEC = 10 * 60

def _now_ts():
    return int(time.time())

def state_set_search(uid: str, wd: int):
    STATE[uid] = {"mode": "search", "wd": wd, "ts": _now_ts()}

def state_get(uid: str):
    st = STATE.get(uid)
    if not st:
        return None
    if _now_ts() - st.get("ts", 0) > STATE_TIMEOUT_SEC:
        STATE.pop(uid, None)
        return None
    return st

def state_clear(uid: str):
    STATE.pop(uid, None)

# ====== Cache teachers ======
TEACHERS_CACHE = {"ts": 0, "ids": set()}
TEACHERS_CACHE_TTL_SEC = 30

def refresh_teachers_cache(force=False):
    now = _now_ts()
    if (not force) and (now - TEACHERS_CACHE["ts"] < TEACHERS_CACHE_TTL_SEC):
        return
    rows = ws_teachers.get_all_values()
    ids = set()
    for i, row in enumerate(rows):
        if i == 0:
            continue
        if len(row) >= 2:
            tid = (row[1] or "").strip()
            if tid:
                ids.add(tid)
    TEACHERS_CACHE["ts"] = now
    TEACHERS_CACHE["ids"] = ids

def is_teacher(uid: str) -> bool:
    refresh_teachers_cache()
    return uid in TEACHERS_CACHE["ids"]

# ====== students utils ======
def find_student_row(student_name: str):
    names = ws_students.col_values(1)  # A欄：student_name
    for idx, n in enumerate(names[1:], start=2):
        if (n or "").strip() == student_name:
            return idx
    return None

def get_remaining(student_name: str) -> float:
    row = find_student_row(student_name)
    if not row:
        raise ValueError(f"student not found: {student_name}")
    val = (ws_students.cell(row, 2).value or "").strip()
    if val == "":
        return 0.0
    try:
        return float(val)
    except ValueError:
        raise ValueError(f"remaining_classes not a number for {student_name}: {val}")

def set_remaining(student_name: str, remaining: float):
    row = find_student_row(student_name)
    if not row:
        raise ValueError(f"student not found: {student_name}")
    ws_students.update_cell(row, 2, remaining)

def append_log(teacher_line_id: str, student_name: str, classes: str, status: str, remaining_after: float):
    ws_log.append_row([
        now_taipei_str(),
        teacher_line_id,
        student_name,
        classes,
        status,
        remaining_after
    ], value_input_option="USER_ENTERED")

# ====== teacher_students utils ======
def get_teacher_students_by_weekday(teacher_line_id: str, weekday: int) -> list:
    """
    teacher_students:
    A teacher_line_id
    B student_name
    C weekday (1~7)
    """
    rows = ws_teacher_students.get_all_values()
    out = []
    for i, row in enumerate(rows):
        if i == 0:
            continue
        if len(row) < 3:
            continue
        tid = (row[0] or "").strip()
        name = (row[1] or "").strip()
        wd_raw = (row[2] or "").strip()
        if not tid or not name or not wd_raw:
            continue
        try:
            wd = int(wd_raw)
        except ValueError:
            continue
        if tid == teacher_line_id and wd == weekday:
            out.append(name)

    # uniq keep order
    seen = set()
    uniq = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

def filter_students_by_keyword(students: list, keyword: str) -> list:
    kw = (keyword or "").strip()
    if not kw:
        return students
    return [s for s in students if kw in s]

# ====== Postback data parsing ======
def parse_qs(data: str) -> dict:
    """
    data like: cmd=pick_day&wd=3&name=%E7%8E%8B%E5%B0%8F%E6%98%8E
    """
    out = {}
    if not data:
        return out
    parts = data.split("&")
    for p in parts:
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
        else:
            out[p] = ""
    return out

def enc(s: str) -> str:
    return quote(s, safe="")

def dec(s: str) -> str:
    try:
        return unquote(s)
    except:
        return s

# ====== Flex builders (全部 postback，不產生文字訊息) ======
def flex_weekday_picker_card(today_wd: int):
    btns = [
        {
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {"type": "postback", "label": f"今天（{weekday_label(today_wd)}）", "data": f"cmd=pick_day&wd={today_wd}"}
        }
    ]
    for wd in range(1, 8):
        btns.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {"type": "postback", "label": weekday_label(wd), "data": f"cmd=pick_day&wd={wd}"}
        })

    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "點名｜選擇上課日", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "（按鈕不會刷聊天室）", "size": "sm", "color": "#666666"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": btns}
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選上課日", contents=contents)

def flex_student_list_card(teacher_uid: str, wd: int, students_all: list, title_prefix: str = None, keyword: str = None):
    # 前 12 + 搜尋
    show_search = len(students_all) > 12
    students = students_all[:12]

    buttons = []
    for name in students:
        buttons.append({
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {"type": "postback", "label": name, "data": f"cmd=pick_student&wd={wd}&name={enc(name)}"}
        })

    if show_search:
        buttons.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {"type": "postback", "label": "🔍 搜尋", "data": f"cmd=enter_search&wd={wd}"}
        })

    title = f"{weekday_label(wd)}｜選學生"
    if title_prefix:
        title = f"{title_prefix}"
    if keyword:
        title = f"{weekday_label(wd)}｜搜尋：{keyword}"

    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "lg"},
                {"type": "text", "text": "點選學生 → 選堂數", "size": "sm", "color": "#666666"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": buttons if buttons else [
                        {"type": "text", "text": "（這天沒有綁定學生）", "size": "sm", "color": "#666666"},
                        {"type": "button", "height": "sm", "style": "secondary",
                         "action": {"type": "postback", "label": "改星期", "data": "cmd=back_to_day"}}
                    ]
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選學生", contents=contents)

def flex_lesson_card(wd: int, student_name: str):
    options = ["0.5", "1", "1.5", "2", "請假"]
    btns = []
    for opt in options:
        style = "primary" if opt != "請假" else "secondary"
        btns.append({
            "type": "button",
            "height": "sm",
            "style": style,
            "action": {"type": "postback", "label": opt, "data": f"cmd=pick_lesson&wd={wd}&name={enc(student_name)}&lesson={enc(opt)}"}
        })

    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": student_name, "weight": "bold", "size": "lg"},
                {"type": "text", "text": "選擇本次堂數", "size": "sm", "color": "#666666"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": btns},
                {"type": "separator", "margin": "md"},
                {"type": "button", "height": "sm", "style": "secondary",
                 "action": {"type": "postback", "label": "返回學生清單", "data": f"cmd=pick_day&wd={wd}"}}
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選堂數", contents=contents)

def flex_done_card(wd: int, msg: str):
    # 成功卡：同一張卡內提供「繼續同日」「改星期」「搜尋」
    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": msg, "weight": "bold", "size": "lg"},
                {"type": "text", "text": "下一步", "size": "sm", "color": "#666666"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": [
                    {"type": "button", "height": "sm", "style": "primary",
                     "action": {"type": "postback", "label": f"繼續（{weekday_label(wd)}）", "data": f"cmd=pick_day&wd={wd}"}},
                    {"type": "button", "height": "sm", "style": "secondary",
                     "action": {"type": "postback", "label": "改星期", "data": "cmd=back_to_day"}},
                    {"type": "button", "height": "sm", "style": "secondary",
                     "action": {"type": "postback", "label": "🔍 搜尋", "data": f"cmd=enter_search&wd={wd}"}},
                ]}
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-完成", contents=contents)

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# ====== Postback handler (主控台) ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    uid = getattr(event.source, "user_id", None)

    def reply(msg_obj):
        line_bot_api.reply_message(event.reply_token, msg_obj)

    def deny():
        reply(TextSendMessage(text="此功能僅限老師使用。"))

    # Rich Menu action
    if data == "action=attendance":
        if not uid or (not is_teacher(uid)):
            deny()
            return
        today = weekday_today_1to7()
        reply(flex_weekday_picker_card(today_wd=today))
        return

    if data == "action=records":
        if not uid or (not is_teacher(uid)):
            deny()
            return
        # 最近5筆（老師自己的）
        rows = ws_log.get_all_values()
        hits = []
        for row in reversed(rows[1:]):
            if len(row) < 6:
                continue
            if (row[1] or "").strip() == uid:
                hits.append(row)
            if len(hits) >= 5:
                break
        if not hits:
            reply(TextSendMessage(text="📒 目前沒有紀錄。"))
            return
        lines = []
        for r in hits:
            ts = (r[0] or "").strip()
            name = (r[2] or "").strip()
            classes = (r[3] or "").strip()
            status = (r[4] or "").strip()
            remain = (r[5] or "").strip()
            if status == "請假":
                lines.append(f"{ts}  {name}  請假  剩{remain}")
            else:
                lines.append(f"{ts}  {name}  -{classes}  剩{remain}")
        reply(TextSendMessage(text="📒 最近紀錄（5筆）\n" + "\n".join(lines)))
        return

    # All other postbacks: only teachers
    if not uid or (not is_teacher(uid)):
        deny()
        return

    qs = parse_qs(data)
    cmd = qs.get("cmd", "")

    # Back to weekday picker
    if cmd == "back_to_day":
        today = weekday_today_1to7()
        reply(flex_weekday_picker_card(today_wd=today))
        return

    # Pick day -> student list
    if cmd == "pick_day":
        try:
            wd = int(qs.get("wd", "").strip())
        except:
            reply(TextSendMessage(text="⚠️ weekday 解析失敗，請重新按點名。"))
            return
        if wd < 1 or wd > 7:
            reply(TextSendMessage(text="⚠️ weekday 必須是 1~7。"))
            return

        students_all = get_teacher_students_by_weekday(uid, wd)
        if not students_all:
            reply(flex_student_list_card(uid, wd, students_all, title_prefix=f"{weekday_label(wd)}｜沒有學生"))
            return

        reply(flex_student_list_card(uid, wd, students_all))
        return

    # Enter search mode (needs next MessageEvent)
    if cmd == "enter_search":
        try:
            wd = int(qs.get("wd", "").strip())
        except:
            wd = weekday_today_1to7()
        if wd < 1 or wd > 7:
            wd = weekday_today_1to7()
        state_set_search(uid, wd)
        reply(TextSendMessage(text=f"{weekday_label(wd)}：請輸入「搜尋:關鍵字」（例：搜尋:王）"))
        return

    # Pick student -> lesson card
    if cmd == "pick_student":
        try:
            wd = int(qs.get("wd", "").strip())
        except:
            reply(TextSendMessage(text="⚠️ weekday 解析失敗，請回上一頁重試。"))
            return
        name = dec(qs.get("name", ""))
        if not name:
            reply(TextSendMessage(text="⚠️ student 解析失敗，請回上一頁重試。"))
            return
        reply(flex_lesson_card(wd=wd, student_name=name))
        return

    # Pick lesson -> do attendance -> done card
    if cmd == "pick_lesson":
        try:
            wd = int(qs.get("wd", "").strip())
        except:
            wd = weekday_today_1to7()
        name = dec(qs.get("name", ""))
        lesson = dec(qs.get("lesson", ""))

        if not name or not lesson:
            reply(TextSendMessage(text="⚠️ 資訊不足，請回上一頁重試。"))
            return

        try:
            if lesson == "請假":
                remaining = get_remaining(name)
                append_log(uid, name, "", "請假", remaining)
                state_clear(uid)
                reply(flex_done_card(wd=wd, msg=f"✅ {name} 請假｜剩 {remaining}"))
                return

            used = float(lesson)
            before = get_remaining(name)
            after = round(before - used, 2)

            if after < 0:
                reply(flex_done_card(wd=wd, msg=f"⚠️ {name} 剩餘不足（現有 {before}，本次扣 {used}）"))
                return

            set_remaining(name, after)
            append_log(uid, name, lesson, "上課", after)
            state_clear(uid)
            reply(flex_done_card(wd=wd, msg=f"✅ {name} -{lesson}｜剩 {after}"))
            return

        except Exception as e:
            reply(TextSendMessage(text=f"⚠️ 扣堂失敗：{e}"))
            return

    # Unknown cmd
    reply(TextSendMessage(text=f"收到操作：{data}"))

# ====== Message handler (only for ID / contact / search input) ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    uid = getattr(event.source, "user_id", None)

    # Get user_id (teacher onboarding)
    if text in ["老師報到", "ID", "id", "我的ID", "我的 id", "我的Id"]:
        if not uid:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 目前拿不到你的 user_id。請用手機 LINE 與官方帳號一對一聊天，再輸入「老師報到」。")
            )
            return
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"✅ 你的 LINE user_id（teacher_line_id）如下：\n{uid}")
        )
        return

    # Contact
    if text == "聯絡教室":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="禾禾音樂教室\n電話：0978-136-812\nLINE：bravop109")
        )
        return

    # Search input (needs teacher)
    if text.startswith("搜尋:"):
        if not uid or (not is_teacher(uid)):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此功能僅限老師使用。"))
            return

        st = state_get(uid)
        wd = st.get("wd") if st and st.get("mode") == "search" else weekday_today_1to7()

        keyword = text.split(":", 1)[1].strip()
        students_all = get_teacher_students_by_weekday(uid, wd)
        matches = filter_students_by_keyword(students_all, keyword)

        if not matches:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"找不到符合「{keyword}」的學生（{weekday_label(wd)}）。")
            )
            return

        # 回學生清單（依舊是 postback）
        line_bot_api.reply_message(
            event.reply_token,
            flex_student_list_card(uid, wd, matches, keyword=keyword)
        )
        return

    # Default
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請使用下方選單（點名 / 紀錄 / 聯絡教室）")
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
