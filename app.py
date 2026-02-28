import os
import json
import time
from datetime import datetime, timezone, timedelta
from flask import Flask, request, abort

import gspread
from google.oauth2.service_account import Credentials

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    PostbackEvent,
    QuickReply, QuickReplyButton, MessageAction
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
    # Mon=1 .. Sun=7
    return datetime.now(TZ_TAIPEI).isoweekday()

def weekday_label(wd: int) -> str:
    labels = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
    return labels.get(wd, f"週{wd}")

# ====== Simple in-memory session (Render 建議 workers=1) ======
PENDING = {}  # user_id -> {"stage": "...", "weekday": int|None, "ts": int, "keyword": str|None}
PENDING_TIMEOUT_SEC = 10 * 60

def _now_ts():
    return int(time.time())

def pending_get(uid: str):
    st = PENDING.get(uid)
    if not st:
        return None
    if _now_ts() - st.get("ts", 0) > PENDING_TIMEOUT_SEC:
        PENDING.pop(uid, None)
        return None
    return st

def pending_set(uid: str, stage: str, weekday: int = None, keyword: str = None):
    PENDING[uid] = {"stage": stage, "weekday": weekday, "keyword": keyword, "ts": _now_ts()}

def pending_clear(uid: str):
    PENDING.pop(uid, None)

# ====== Cache teachers (avoid reading sheet every message) ======
TEACHERS_CACHE = {"ts": 0, "ids": set()}
TEACHERS_CACHE_TTL_SEC = 30

def refresh_teachers_cache(force=False):
    now = _now_ts()
    if (not force) and (now - TEACHERS_CACHE["ts"] < TEACHERS_CACHE_TTL_SEC):
        return
    rows = ws_teachers.get_all_values()  # header + rows
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

# ====== Utility (students) ======
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

# ====== teacher_students (filter by weekday) ======
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

# ====== Quick Reply builders ======
def weekday_quick_reply():
    items = [
        QuickReplyButton(action=MessageAction(label="今天", text="上課日:今天")),
        QuickReplyButton(action=MessageAction(label="週一", text="上課日:1")),
        QuickReplyButton(action=MessageAction(label="週二", text="上課日:2")),
        QuickReplyButton(action=MessageAction(label="週三", text="上課日:3")),
        QuickReplyButton(action=MessageAction(label="週四", text="上課日:4")),
        QuickReplyButton(action=MessageAction(label="週五", text="上課日:5")),
        QuickReplyButton(action=MessageAction(label="週六", text="上課日:6")),
        QuickReplyButton(action=MessageAction(label="週日", text="上課日:7")),
    ]
    return QuickReply(items=items[:13])

def student_quick_reply(students: list):
    buttons = [
        QuickReplyButton(action=MessageAction(label=n, text=f"選擇學生:{n}"))
        for n in students[:13]
    ]
    return QuickReply(items=buttons)

def lesson_quick_reply(name: str):
    lessons = ["0.5", "1", "1.5", "2", "請假"]
    buttons = [
        QuickReplyButton(action=MessageAction(label=l, text=f"堂數:{name}:{l}"))
        for l in lessons
    ]
    return QuickReply(items=buttons)

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

# ====== Rich Menu Postback ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    uid = getattr(event.source, "user_id", None)

    if data == "action=attendance":
        # 權限擋家長（就算誤綁 all 也安全）
        if not uid or (not is_teacher(uid)):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="此功能僅限老師使用。若需協助請點「聯絡教室」。")
            )
            return

        pending_set(uid, stage="choose_day", weekday=None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請選擇上課日", quick_reply=weekday_quick_reply())
        )
        return

    elif data == "action=records":
        if not uid or (not is_teacher(uid)):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="此功能僅限老師使用。")
            )
            return

        # 簡易版：抓最近 10 筆，過濾該老師，顯示最近 5 筆
        rows = ws_log.get_all_values()
        # header: timestamp, teacher_line_id, student_name, classes, status, remaining_after
        hits = []
        for row in reversed(rows[1:]):
            if len(row) < 6:
                continue
            if (row[1] or "").strip() == uid:
                hits.append(row)
            if len(hits) >= 5:
                break

        if not hits:
            msg = "📒 目前沒有紀錄。"
        else:
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
            msg = "📒 最近紀錄（最多5筆）\n" + "\n".join(lines)

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=msg)
        )
        return

    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"收到操作：{data}")
        )

# ====== Text Handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    uid = getattr(event.source, "user_id", None)

    # ====== 最高優先：回傳 user_id（用來登記老師/家長） ======
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

    # 聯絡教室
    if text == "聯絡教室":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="禾禾音樂教室\n電話：0978-136-812\nLINE：bravop109")
        )
        return

    # ====== 權限擋：非老師不要進入點名流程 ======
    #（但保留他們可以用「聯絡教室」等訊息）
    # 只有在「點名流程」相關文字時才擋，避免影響一般聊天
    if text.startswith(("上課日:", "選擇學生:", "堂數:", "搜尋:")):
        if not uid or (not is_teacher(uid)):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="此功能僅限老師使用。若需協助請點「聯絡教室」。")
            )
            return

    # ====== 點名流程：選上課日 ======
    if text.startswith("上課日:"):
        if not uid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前拿不到你的 user_id。"))
            return

        st = pending_get(uid)
        # 沒有 pending 也允許直接選上課日（容錯）
        raw = text.split(":", 1)[1].strip()

        if raw == "今天":
            wd = weekday_today_1to7()
        else:
            try:
                wd = int(raw)
            except ValueError:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="⚠️ 上課日格式錯誤，請重新按「點名」。")
                )
                return

        if wd < 1 or wd > 7:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ weekday 必須是 1~7。")
            )
            return

        pending_set(uid, stage="choose_student", weekday=wd)
        students = get_teacher_students_by_weekday(uid, wd)

        if not students:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"{weekday_label(wd)} 沒有綁定學生。\n請到 teacher_students 填 weekday（1~7）。")
            )
            return

        # QuickReply 上限 13：若超過，先提示用搜尋縮小（你下一版再擴充 UI）
        if len(students) > 13:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"{weekday_label(wd)} 學生共有 {len(students)} 位，為避免選單太長：\n"
                         f"請輸入「搜尋:關鍵字」(例如：搜尋:王)，我會列出符合的名單。"
                )
            )
            pending_set(uid, stage="searching", weekday=wd)
            return

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"請選擇學生（{weekday_label(wd)}）", quick_reply=student_quick_reply(students))
        )
        return

    # ====== 點名流程：搜尋學生（在某個 weekday 裡） ======
    if text.startswith("搜尋:"):
        if not uid:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前拿不到你的 user_id。"))
            return

        st = pending_get(uid)
        if not st or st.get("stage") not in ["searching", "choose_student"]:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="請先按「點名」並選上課日。")
            )
            return

        wd = st.get("weekday")
        if not wd:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="⚠️ 目前沒有上課日資訊，請重新按「點名」。")
            )
            return

        keyword = text.split(":", 1)[1].strip()
        students_all = get_teacher_students_by_weekday(uid, wd)
        students = filter_students_by_keyword(students_all, keyword)

        if not students:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"找不到符合「{keyword}」的學生（{weekday_label(wd)}）。\n請換關鍵字再試一次。")
            )
            return

        if len(students) > 13:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"符合「{keyword}」的學生仍有 {len(students)} 位，請再輸入更精準的關鍵字（例如兩個字）。")
            )
            return

        pending_set(uid, stage="choose_student", weekday=wd)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"請選擇學生（{weekday_label(wd)}，關鍵字：{keyword}）", quick_reply=student_quick_reply(students))
        )
        return

    # ====== 選學生 ======
    if text.startswith("選擇學生:"):
        name = text.split(":", 1)[1].strip()

        # 若你希望「必須先選上課日」才可選學生，就開啟這段檢查
        # st = pending_get(uid) if uid else None
        # if not st or st.get("stage") not in ["choose_student", "searching"]:
        #     line_bot_api.reply_message(event.reply_token, TextSendMessage(text="請先按「點名」並選上課日。"))
        #     return

        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"請選擇 {name} 上課堂數",
                quick_reply=lesson_quick_reply(name)
            )
        )
        return

    # ====== 選堂數 / 扣堂 ======
    if text.startswith("堂數:"):
        try:
            _, name, lesson = text.split(":", 2)
            teacher_id = uid

            if not teacher_id:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="⚠️ 目前拿不到你的 user_id。請用手機 LINE 與官方帳號一對一聊天再試。")
                )
                return

            # 請假（不扣堂，只記錄）
            if lesson == "請假":
                remaining = get_remaining(name)
                append_log(teacher_id, name, "", "請假", remaining)
                pending_clear(teacher_id)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"✅ 已記錄 {name}：請假\n剩餘：{remaining} 堂")
                )
                return

            used = float(lesson)
            before = get_remaining(name)
            after = round(before - used, 2)

            if after < 0:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"⚠️ {name} 剩餘堂數不足\n目前：{before} 堂\n本次要扣：{used} 堂"
                    )
                )
                return

            set_remaining(name, after)
            append_log(teacher_id, name, lesson, "上課", after)
            pending_clear(teacher_id)

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"✅ 已為 {name} 記錄 {lesson} 堂\n"
                         f"時間：{now_taipei_str()}\n"
                         f"剩餘：{after} 堂"
                )
            )
            return

        except Exception as e:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"⚠️ 扣堂失敗：{e}")
            )
            return

    # ====== 其他 ======
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請使用下方選單（點名 / 紀錄 / 聯絡教室）")
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
