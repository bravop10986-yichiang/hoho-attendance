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
    names = ws_students.col_values(1)
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
    return float(val)

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
        except:
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

# ====== Postback data helpers ======
def parse_qs(data: str) -> dict:
    out = {}
    for p in (data or "").split("&"):
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
    return out

def enc(s: str) -> str:
    return quote(s, safe="")

def dec(s: str) -> str:
    return unquote(s)

# ====== Flex builders (全部 postback，不刷聊天室文字指令) ======
def flex_weekday_picker_card(today_wd: int):
    btns = [{
        "type": "button", "height": "sm", "style": "primary",
        "action": {"type": "postback", "label": f"今天（{weekday_label(today_wd)}）", "data": f"cmd=pick_day&wd={today_wd}"}
    }]
    for wd in range(1, 8):
        btns.append({
            "type": "button", "height": "sm", "style": "secondary",
            "action": {"type": "postback", "label": weekday_label(wd), "data": f"cmd=pick_day&wd={wd}"}
        })
    return FlexSendMessage(
        alt_text="點名-選上課日",
        contents={
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": "點名｜選擇上課日", "weight": "bold", "size": "lg"},
                    {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": btns}
                ]
            }
        }
    )

def flex_student_list_card(wd: int, students_all: list, keyword: str = None):
    # 前 12 + 搜尋
    show_search = len(students_all) > 12
    students = students_all[:12]

    buttons = []
    for name in students:
        buttons.append({
            "type": "button", "height": "sm", "style": "primary",
            "action": {"type": "postback", "label": name, "data": f"cmd=pick_student&wd={wd}&name={enc(name)}"}
        })
    if show_search:
        buttons.append({
            "type": "button", "height": "sm", "style": "secondary",
            "action": {"type": "postback", "label": "🔍 搜尋", "data": f"cmd=enter_search&wd={wd}"}
        })

    title = f"{weekday_label(wd)}｜選學生"
    if keyword:
        title = f"{weekday_label(wd)}｜搜尋：{keyword}"

    # 若完全沒學生：仍給「改星期」回去
    if not buttons:
        buttons = [{
            "type": "button", "height": "sm", "style": "secondary",
            "action": {"type": "postback", "label": "改星期", "data": "cmd=back_to_day"}
        }]

    return FlexSendMessage(
        alt_text="點名-選學生",
        contents={
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": title, "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "點選學生 → 選堂數", "size": "sm", "color": "#666666"},
                    {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": buttons}
                ]
            }
        }
    )

def flex_lesson_card(wd: int, name: str):
    options = ["0.5", "1", "1.5", "2", "請假"]
    btns = []
    for opt in options:
        btns.append({
            "type": "button", "height": "sm",
            "style": "primary" if opt != "請假" else "secondary",
            "action": {"type": "postback", "label": opt,
                       "data": f"cmd=pick_lesson&wd={wd}&name={enc(name)}&lesson={enc(opt)}"}
        })
    # 返回清單
    btns.append({
        "type": "button", "height": "sm", "style": "secondary",
        "action": {"type": "postback", "label": "返回學生清單", "data": f"cmd=pick_day&wd={wd}"}
    })
    return FlexSendMessage(
        alt_text="點名-選堂數",
        contents={
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": name, "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "選擇本次堂數", "size": "sm", "color": "#666666"},
                    {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": btns}
                ]
            }
        }
    )

def flex_done_card(wd: int, msg: str):
    # ✅ 只保留：繼續 / 改星期 / 搜尋（沒有「結束點名」）
    return FlexSendMessage(
        alt_text="點名-完成",
        contents={
            "type": "bubble",
            "body": {
                "type": "box", "layout": "vertical", "spacing": "md",
                "contents": [
                    {"type": "text", "text": msg, "weight": "bold", "size": "lg"},
                    {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": [
                        {"type": "button", "height": "sm", "style": "primary",
                         "action": {"type": "postback", "label": f"繼續（{weekday_label(wd)}）",
                                    "data": f"cmd=pick_day&wd={wd}"}},
                        {"type": "button", "height": "sm", "style": "secondary",
                         "action": {"type": "postback", "label": "改星期",
                                    "data": "cmd=back_to_day"}},
                        {"type": "button", "height": "sm", "style": "secondary",
                         "action": {"type": "postback", "label": "🔍 搜尋",
                                    "data": f"cmd=enter_search&wd={wd}"}},
                    ]}
                ]
            }
        }
    )

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

# ====== Postback handler ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    uid = getattr(event.source, "user_id", None)

    def reply(msg):
        line_bot_api.reply_message(event.reply_token, msg)

    # Rich Menu：點名
    if data == "action=attendance":
        if not uid or not is_teacher(uid):
            reply(TextSendMessage(text="此功能僅限老師使用。"))
            return
        reply(flex_weekday_picker_card(weekday_today_1to7()))
        return

    # Rich Menu：紀錄
    if data == "action=records":
        if not uid or not is_teacher(uid):
            reply(TextSendMessage(text="此功能僅限老師使用。"))
            return

        # 最近 5 筆（該老師）
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

    # 其他 postback：全部限定老師
    if not uid or not is_teacher(uid):
        reply(TextSendMessage(text="此功能僅限老師使用。"))
        return

    qs = parse_qs(data)
    cmd = qs.get("cmd", "")

    if cmd == "back_to_day":
        reply(flex_weekday_picker_card(weekday_today_1to7()))
        return

    if cmd == "pick_day":
        try:
            wd = int(qs.get("wd", weekday_today_1to7()))
        except:
            wd = weekday_today_1to7()
        students = get_teacher_students_by_weekday(uid, wd)
        reply(flex_student_list_card(wd, students))
        return

    if cmd == "enter_search":
        try:
            wd = int(qs.get("wd", weekday_today_1to7()))
        except:
            wd = weekday_today_1to7()
        state_set_search(uid, wd)
        reply(TextSendMessage(text=f"{weekday_label(wd)}：請輸入「搜尋:關鍵字」（例：搜尋:王）"))
        return

    if cmd == "pick_student":
        try:
            wd = int(qs.get("wd", weekday_today_1to7()))
        except:
            wd = weekday_today_1to7()
        name = dec(qs.get("name", ""))
        if not name:
            reply(TextSendMessage(text="⚠️ 找不到學生名稱，請回上一頁重試。"))
            return
        reply(flex_lesson_card(wd, name))
        return

    if cmd == "pick_lesson":
        try:
            wd = int(qs.get("wd", weekday_today_1to7()))
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
                reply(flex_done_card(wd, f"✅ {name} 請假｜剩 {remaining}"))
                return

            used = float(lesson)
            before = get_remaining(name)
            after = round(before - used, 2)

            if after < 0:
                reply(flex_done_card(wd, f"⚠️ {name} 剩餘不足（現有 {before}，本次扣 {used}）"))
                return

            set_remaining(name, after)
            append_log(uid, name, lesson, "上課", after)
            state_clear(uid)
            reply(flex_done_card(wd, f"✅ {name} -{lesson}｜剩 {after}"))
            return

        except Exception as e:
            reply(TextSendMessage(text=f"⚠️ 扣堂失敗：{e}"))
            return

    reply(TextSendMessage(text=f"收到操作：{data}"))

# ====== Message handler（只保留 ID / 搜尋 / 其他提示） ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    uid = getattr(event.source, "user_id", None)

    if text in ["老師報到", "ID", "id", "我的ID", "我的 id", "我的Id"]:
        if not uid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前拿不到你的 user_id。"))
            return
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 你的 user_id：{uid}"))
        return

    if text.startswith("搜尋:"):
        if not uid or not is_teacher(uid):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此功能僅限老師使用。"))
            return

        st = state_get(uid)
        wd = st["wd"] if st else weekday_today_1to7()
        keyword = text.split(":", 1)[1].strip()

        students = get_teacher_students_by_weekday(uid, wd)
        matches = filter_students_by_keyword(students, keyword)

        if not matches:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"找不到符合「{keyword}」的學生（{weekday_label(wd)}）。"))
            return

        line_bot_api.reply_message(event.reply_token, flex_student_list_card(wd, matches, keyword=keyword))
        return

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請使用選單（點名 / 紀錄）"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
