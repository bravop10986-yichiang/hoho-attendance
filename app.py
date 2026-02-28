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
    # Mon=1..Sun=7
    return datetime.now(TZ_TAIPEI).isoweekday()

def weekday_label(wd: int) -> str:
    labels = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
    return labels.get(wd, f"週{wd}")

# ====== In-memory state (Render 建議 workers=1) ======
# 用來記住：老師目前選的 weekday、是否在搜尋狀態
PENDING = {}  # uid -> {"stage": str, "weekday": int, "ts": int}
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

def pending_set(uid: str, stage: str, weekday: int):
    PENDING[uid] = {"stage": stage, "weekday": weekday, "ts": _now_ts()}

def pending_clear(uid: str):
    PENDING.pop(uid, None)

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

# ====== Flex builders (極簡 App 感) ======
def flex_weekday_picker_card(today_wd: int):
    # 星期選擇卡：今天 + 週一~週日
    btns = [
        {
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {"type": "message", "label": f"今天（{weekday_label(today_wd)}）", "text": f"上課日:{today_wd}"}
        }
    ]
    # 週一~週日（次要按鈕）
    for wd in range(1, 8):
        btns.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {"type": "message", "label": weekday_label(wd), "text": f"上課日:{wd}"}
        })

    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "選擇上課日", "weight": "bold", "size": "lg"},
                {"type": "text", "text": "點一下就進入學生名單", "size": "sm", "color": "#666666"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": btns
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選上課日", contents=contents)

def flex_student_list_card(title: str, students: list, show_search: bool, weekday: int):
    # students: list of names (already sliced)
    buttons = []
    for name in students:
        buttons.append({
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {"type": "message", "label": name, "text": f"選擇學生:{name}"}
        })

    if show_search:
        buttons.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {"type": "message", "label": "🔍 搜尋", "text": f"搜尋學生:{weekday}"}
        })

    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": title, "weight": "bold", "size": "lg"},
                {"type": "text", "text": "點選學生開始點名", "size": "sm", "color": "#666666"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": buttons if buttons else [
                        {"type": "text", "text": "（這天沒有綁定學生）", "size": "sm", "color": "#666666"}
                    ]
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選學生", contents=contents)

def flex_lesson_card(student_name: str):
    options = ["0.5", "1", "1.5", "2", "請假"]
    btns = []
    for opt in options:
        style = "primary" if opt != "請假" else "secondary"
        btns.append({
            "type": "button",
            "height": "sm",
            "style": style,
            "action": {"type": "message", "label": opt, "text": f"堂數:{student_name}:{opt}"}
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
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": btns
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-選堂數", contents=contents)

def flex_after_done_card(weekday: int):
    # 扣堂完成後給「繼續點名 / 改星期」兩個鍵
    contents = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "下一步", "weight": "bold", "size": "lg"},
                {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "sm",
                    "margin": "md",
                    "contents": [
                        {
                            "type": "button",
                            "height": "sm",
                            "style": "primary",
                            "action": {"type": "postback", "label": f"繼續點名（{weekday_label(weekday)}）", "data": f"action=attendance_day&wd={weekday}"}
                        },
                        {
                            "type": "button",
                            "height": "sm",
                            "style": "secondary",
                            "action": {"type": "postback", "label": "改星期", "data": "action=attendance"}
                        }
                    ]
                }
            ]
        }
    }
    return FlexSendMessage(alt_text="點名-下一步", contents=contents)

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

    def deny():
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此功能僅限老師使用。"))

    if data.startswith("action=attendance"):
        if not uid or (not is_teacher(uid)):
            deny()
            return

        # action=attendance -> 先出星期選擇（你要的回來了）
        if data == "action=attendance":
            today = weekday_today_1to7()
            line_bot_api.reply_message(
                event.reply_token,
                flex_weekday_picker_card(today_wd=today)
            )
            return

        # action=attendance_day&wd=3 -> 直接進該日學生清單（扣堂後「繼續點名」會用到）
        if data.startswith("action=attendance_day"):
            # parse wd
            wd = None
            try:
                parts = data.split("&")
                for p in parts:
                    if p.startswith("wd="):
                        wd = int(p.split("=", 1)[1])
            except:
                wd = None

            if not wd or wd < 1 or wd > 7:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ weekday 解析失敗，請重新按點名。"))
                return

            pending_set(uid, stage="in_day", weekday=wd)

            students_all = get_teacher_students_by_weekday(uid, wd)
            show_search = len(students_all) > 12
            students_show = students_all[:12]
            title = f"{weekday_label(wd)} 點名"

            line_bot_api.reply_message(
                event.reply_token,
                flex_student_list_card(title=title, students=students_show, show_search=show_search, weekday=wd)
            )
            return

    if data == "action=records":
        if not uid or (not is_teacher(uid)):
            deny()
            return

        # 簡易：最近5筆（該老師）
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
            msg = "📒 最近紀錄（5筆）\n" + "\n".join(lines)

        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=msg))
        return

    # default
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"收到操作：{data}"))

# ====== Text Handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    uid = getattr(event.source, "user_id", None)

    # 取得 user_id
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

    # ====== 流程相關：非老師全部擋 ======
    if text.startswith(("上課日:", "選擇學生:", "堂數:", "搜尋", "搜尋:")):
        if not uid or (not is_teacher(uid)):
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="此功能僅限老師使用。"))
            return

    # ====== 選上課日：上課日:3 ======
    if text.startswith("上課日:"):
        try:
            wd = int(text.split(":", 1)[1].strip())
        except:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 上課日格式錯誤。"))
            return

        if wd < 1 or wd > 7:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ weekday 必須是 1~7。"))
            return

        pending_set(uid, stage="in_day", weekday=wd)

        students_all = get_teacher_students_by_weekday(uid, wd)
        if not students_all:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"{weekday_label(wd)} 沒有綁定學生（請確認 teacher_students 的 weekday）。")
            )
            return

        show_search = len(students_all) > 12
        students_show = students_all[:12]
        title = f"{weekday_label(wd)} 點名"
        line_bot_api.reply_message(
            event.reply_token,
            flex_student_list_card(title=title, students=students_show, show_search=show_search, weekday=wd)
        )
        return

    # ====== 搜尋入口：搜尋學生:3（由 Flex 按鈕帶 weekday） ======
    if text.startswith("搜尋學生:"):
        st = pending_get(uid)
        # 以訊息中的 weekday 為準
        try:
            wd = int(text.split(":", 1)[1].strip())
        except:
            wd = st.get("weekday") if st else weekday_today_1to7()

        pending_set(uid, stage="searching", weekday=wd)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"{weekday_label(wd)}：請輸入「搜尋:關鍵字」（例：搜尋:王）")
        )
        return

    # ====== 搜尋指令：搜尋:王（在 pending 的 weekday 下） ======
    if text.startswith("搜尋:"):
        st = pending_get(uid)
        wd = st.get("weekday") if st else weekday_today_1to7()

        keyword = text.split(":", 1)[1].strip()
        students_all = get_teacher_students_by_weekday(uid, wd)
        matches = filter_students_by_keyword(students_all, keyword)

        if not matches:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"找不到符合「{keyword}」的學生（{weekday_label(wd)}）。")
            )
            return

        show_search = len(matches) > 12
        matches_show = matches[:12]
        title = f"{weekday_label(wd)}｜搜尋：{keyword}"

        line_bot_api.reply_message(
            event.reply_token,
            flex_student_list_card(title=title, students=matches_show, show_search=show_search, weekday=wd)
        )
        return

    # ====== 選學生 → 回堂數 Flex ======
    if text.startswith("選擇學生:"):
        name = text.split(":", 1)[1].strip()
        line_bot_api.reply_message(
            event.reply_token,
            flex_lesson_card(student_name=name)
        )
        return

    # ====== 選堂數 / 扣堂 ======
    if text.startswith("堂數:"):
        try:
            _, name, lesson = text.split(":", 2)
            teacher_id = uid

            if not teacher_id:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前拿不到你的 user_id。"))
                return

            # 請假（不扣堂，只記錄）
            if lesson == "請假":
                remaining = get_remaining(name)
                append_log(teacher_id, name, "", "請假", remaining)
                # 完成後保留 weekday，方便「繼續點名」
                st = pending_get(teacher_id)
                wd = st.get("weekday") if st else weekday_today_1to7()
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"✅ {name} 請假｜剩 {remaining}")
                )
                # 再補一張「下一步」卡（可選；你要極簡就保留這張）
                line_bot_api.push_message(
                    teacher_id,
                    flex_after_done_card(weekday=wd)
                )
                return

            used = float(lesson)
            before = get_remaining(name)
            after = round(before - used, 2)

            if after < 0:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"⚠️ {name} 剩餘不足（現有 {before}，本次扣 {used}）")
                )
                return

            set_remaining(name, after)
            append_log(teacher_id, name, lesson, "上課", after)

            # 極簡結果
            st = pending_get(teacher_id)
            wd = st.get("weekday") if st else weekday_today_1to7()

            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"✅ {name} -{lesson}｜剩 {after}")
            )

            # 再丟一張「繼續點名/改星期」卡（讓老師像 App 一樣連點）
            line_bot_api.push_message(
                teacher_id,
                flex_after_done_card(weekday=wd)
            )
            return

        except Exception as e:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 扣堂失敗：{e}"))
            return

    # 其他
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請使用下方選單（點名 / 紀錄 / 聯絡教室）")
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
