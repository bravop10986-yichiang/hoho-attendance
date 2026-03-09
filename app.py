import os
import json
import time
import threading
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

def now_dt():
    return datetime.now(TZ_TAIPEI)

def now_taipei_str():
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")

def fmt_num(x):
    try:
        f = float(x)
        if f.is_integer():
            return str(int(f))
        return str(round(f, 2))
    except:
        return str(x)

def _now_ts():
    return int(time.time())

def enc(s: str) -> str:
    return quote(s, safe="")

def dec(s: str) -> str:
    return unquote(s)

def parse_qs(data: str) -> dict:
    out = {}
    for p in (data or "").split("&"):
        if "=" in p:
            k, v = p.split("=", 1)
            out[k] = v
    return out

# ====== Cache ======
CACHE_TTL_TEACHERS = 30
CACHE_TTL_STUDENTS = 20
CACHE_TTL_TEACHER_STUDENTS = 20
CACHE_TTL_LOGS = 5

TEACHERS_CACHE = {"ts": 0, "ids": set()}
STUDENTS_CACHE = {"ts": 0, "rows": [], "name_to_row": {}, "name_to_remaining": {}}
TEACHER_STUDENTS_CACHE = {"ts": 0, "map": {}}
LOG_TAIL_CACHE = {"ts": 0, "rows": []}

# ====== Anti-duplicate / Undo ======
ACTION_LOCK = threading.Lock()
IN_FLIGHT = {}        # uid -> ts
RECENT_ACTIONS = {}   # f"{uid}|{student}|{classes}" -> ts
LAST_SUCCESS = {}     # uid -> {ts, student_name, used, before, after, type}

IN_FLIGHT_TIMEOUT_SEC = 20
DUPLICATE_WINDOW_SEC = 15
UNDO_WINDOW_SEC = 10 * 60

# ====== Teacher cache ======
def refresh_teachers_cache(force=False):
    now = _now_ts()
    if (not force) and (now - TEACHERS_CACHE["ts"] < CACHE_TTL_TEACHERS):
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

# ====== Students cache ======
def refresh_students_cache(force=False):
    now = _now_ts()
    if (not force) and (now - STUDENTS_CACHE["ts"] < CACHE_TTL_STUDENTS):
        return

    rows = ws_students.get_all_values()
    name_to_row = {}
    name_to_remaining = {}

    for idx, row in enumerate(rows[1:], start=2):
        name = (row[0] or "").strip() if len(row) >= 1 else ""
        remain_raw = (row[1] or "").strip() if len(row) >= 2 else ""
        if not name:
            continue
        try:
            remain = float(remain_raw) if remain_raw != "" else 0.0
        except:
            remain = 0.0
        name_to_row[name] = idx
        name_to_remaining[name] = remain

    STUDENTS_CACHE["ts"] = now
    STUDENTS_CACHE["rows"] = rows
    STUDENTS_CACHE["name_to_row"] = name_to_row
    STUDENTS_CACHE["name_to_remaining"] = name_to_remaining

def find_student_row(student_name: str):
    refresh_students_cache()
    return STUDENTS_CACHE["name_to_row"].get(student_name)

def get_remaining(student_name: str) -> float:
    refresh_students_cache()
    if student_name not in STUDENTS_CACHE["name_to_remaining"]:
        raise ValueError(f"student not found: {student_name}")
    return STUDENTS_CACHE["name_to_remaining"][student_name]

def set_remaining(student_name: str, remaining: float):
    row = find_student_row(student_name)
    if not row:
        raise ValueError(f"student not found: {student_name}")

    ws_students.update_cell(row, 2, remaining)

    # 同步更新 cache，避免下一次又查遠端
    refresh_students_cache(force=False)
    STUDENTS_CACHE["name_to_remaining"][student_name] = float(remaining)

# ====== Log ======
def append_log(teacher_line_id: str, student_name: str, classes: str, status: str, remaining_after: float):
    ws_log.append_row([
        now_taipei_str(),
        teacher_line_id,
        student_name,
        classes,
        status,
        remaining_after
    ], value_input_option="USER_ENTERED")

    # 新增後讓 log tail cache 下次重抓
    LOG_TAIL_CACHE["ts"] = 0

def refresh_log_tail_cache(force=False, tail_size=80):
    now = _now_ts()
    if (not force) and (now - LOG_TAIL_CACHE["ts"] < CACHE_TTL_LOGS):
        return

    rows = ws_log.get_all_values()
    if len(rows) <= 1:
        LOG_TAIL_CACHE["rows"] = []
    else:
        LOG_TAIL_CACHE["rows"] = rows[-tail_size:]
    LOG_TAIL_CACHE["ts"] = now

def has_recent_duplicate_log(uid: str, student_name: str, classes: str, status: str, window_sec=20) -> bool:
    """
    防止 server 重啟後記憶體遺失，仍可從最近 log 再擋一次重複。
    """
    refresh_log_tail_cache()
    now = now_dt()

    for row in reversed(LOG_TAIL_CACHE["rows"]):
        if len(row) < 6:
            continue

        ts_str = (row[0] or "").strip()
        teacher_id = (row[1] or "").strip()
        name = (row[2] or "").strip()
        cls = (row[3] or "").strip()
        st = (row[4] or "").strip()

        if teacher_id != uid or name != student_name or cls != classes or st != status:
            continue

        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TAIPEI)
        except:
            continue

        if (now - dt).total_seconds() <= window_sec:
            return True

        break

    return False

# ====== teacher_students cache ======
def refresh_teacher_students_cache(force=False):
    now = _now_ts()
    if (not force) and (now - TEACHER_STUDENTS_CACHE["ts"] < CACHE_TTL_TEACHER_STUDENTS):
        return

    rows = ws_teacher_students.get_all_values()
    out = {}

    for i, row in enumerate(rows):
        if i == 0:
            continue
        if len(row) < 2:
            continue

        tid = (row[0] or "").strip()
        name = (row[1] or "").strip()

        if not tid or not name:
            continue

        out.setdefault(tid, [])
        if name not in out[tid]:
            out[tid].append(name)

    TEACHER_STUDENTS_CACHE["ts"] = now
    TEACHER_STUDENTS_CACHE["map"] = out

def get_teacher_students(teacher_line_id: str) -> list:
    refresh_teacher_students_cache()
    return TEACHER_STUDENTS_CACHE["map"].get(teacher_line_id, [])

# ====== Utils ======
def cleanup_runtime_maps():
    now = _now_ts()

    # 清過期 in-flight
    for uid, ts in list(IN_FLIGHT.items()):
        if now - ts > IN_FLIGHT_TIMEOUT_SEC:
            IN_FLIGHT.pop(uid, None)

    # 清過期 recent actions
    for k, ts in list(RECENT_ACTIONS.items()):
        if now - ts > DUPLICATE_WINDOW_SEC:
            RECENT_ACTIONS.pop(k, None)

    # 清過期 undo
    for uid, data in list(LAST_SUCCESS.items()):
        if now - data.get("ts", 0) > UNDO_WINDOW_SEC:
            LAST_SUCCESS.pop(uid, None)

def try_enter_inflight(uid: str) -> bool:
    cleanup_runtime_maps()
    with ACTION_LOCK:
        if uid in IN_FLIGHT:
            return False
        IN_FLIGHT[uid] = _now_ts()
        return True

def leave_inflight(uid: str):
    with ACTION_LOCK:
        IN_FLIGHT.pop(uid, None)

# ====== Flex ======
def flex_student_picker(uid: str, page: int = 0, page_size: int = 8):
    students = get_teacher_students(uid)
    total = len(students)

    if total == 0:
        return FlexSendMessage(
            alt_text="點名-學生清單",
            contents={
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "點名", "weight": "bold", "size": "xl"},
                        {"type": "text", "text": "你目前沒有被分配學生", "color": "#888888", "wrap": True}
                    ]
                }
            }
        )

    max_page = (total - 1) // page_size
    page = max(0, min(page, max_page))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total)
    page_students = students[start_idx:end_idx]

    buttons = []
    for name in page_students:
        buttons.append({
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {
                "type": "postback",
                "label": name,
                "data": f"cmd=attendance_mark&name={enc(name)}"
            }
        })

    footer_btns = []
    if page > 0:
        footer_btns.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {
                "type": "postback",
                "label": "⬅ 上一頁",
                "data": f"cmd=attendance_page&page={page-1}"
            }
        })
    if page < max_page:
        footer_btns.append({
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {
                "type": "postback",
                "label": "下一頁 ➡",
                "data": f"cmd=attendance_page&page={page+1}"
            }
        })

    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "contents": [
                {"type": "text", "text": "點名", "weight": "bold", "size": "xl"},
                {"type": "text", "text": f"直接點學生＝扣 1 堂", "size": "sm", "color": "#666666"},
                {"type": "text", "text": f"第 {page+1}/{max_page+1} 頁｜共 {total} 位", "size": "xs", "color": "#888888"},
                {"type": "box", "layout": "vertical", "spacing": "sm", "margin": "md", "contents": buttons}
            ]
        }
    }

    if footer_btns:
        bubble["footer"] = {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": footer_btns
        }

    return FlexSendMessage(
        alt_text="點名-學生清單",
        contents=bubble
    )

def flex_done_card(student_name: str, used: float, before: float, after: float):
    ts = now_taipei_str()
    return FlexSendMessage(
        alt_text="點名成功",
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": "✅ 點名成功", "weight": "bold", "size": "xl"},
                    {"type": "text", "text": student_name, "weight": "bold", "size": "lg"},
                    {"type": "text", "text": f"本次扣堂：{fmt_num(used)} 堂", "size": "md"},
                    {"type": "text", "text": f"扣前：{fmt_num(before)}　→　扣後：{fmt_num(after)}", "size": "sm", "color": "#555555"},
                    {"type": "text", "text": ts, "size": "xs", "color": "#999999"}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "height": "sm",
                        "style": "primary",
                        "action": {"type": "postback", "label": "繼續點名", "data": "cmd=attendance_page&page=0"}
                    },
                    {
                        "type": "button",
                        "height": "sm",
                        "style": "secondary",
                        "action": {"type": "postback", "label": "更正上一筆", "data": "cmd=undo_last"}
                    }
                ]
            }
        }
    )

def flex_warning_card(title: str, msg: str):
    return FlexSendMessage(
        alt_text=title,
        contents={
            "type": "bubble",
            "body": {
                "type": "box",
                "layout": "vertical",
                "spacing": "md",
                "contents": [
                    {"type": "text", "text": title, "weight": "bold", "size": "xl"},
                    {"type": "text", "text": msg, "wrap": True, "size": "sm", "color": "#555555"}
                ]
            },
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "height": "sm",
                        "style": "primary",
                        "action": {"type": "postback", "label": "回到點名", "data": "cmd=attendance_page&page=0"}
                    }
                ]
            }
        }
    )

# ====== 紀錄（保留簡單版） ======
def get_records_last_14_days(uid: str) -> list:
    rows = ws_log.get_all_values()
    if len(rows) <= 1:
        return []

    now = now_dt()
    start = now - timedelta(days=14)

    hits = []
    for r in reversed(rows[1:]):
        if len(r) < 6:
            continue
        if (r[1] or "").strip() != uid:
            continue

        ts_str = (r[0] or "").strip()
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_TAIPEI)
        except:
            continue

        if dt < start:
            break

        hits.append(r)

    return hits

def _record_item_box(r):
    ts = (r[0] or "").strip()
    name = (r[2] or "").strip()
    classes = (r[3] or "").strip()
    status = (r[4] or "").strip()
    remain = (r[5] or "").strip()

    if status == "請假":
        line_text = f"{name}｜請假｜剩 {remain}"
        color = "#999999"
    elif status in ["更正", "更正取消請假"]:
        line_text = f"{name}｜{status}｜剩 {remain}"
        color = "#D97706"
    else:
        line_text = f"{name}｜-{classes}｜剩 {remain}"
        color = "#1A73E8"

    return {
        "type": "box",
        "layout": "vertical",
        "spacing": "xs",
        "contents": [
            {"type": "text", "text": ts, "size": "xs", "color": "#888888"},
            {"type": "text", "text": line_text, "size": "sm", "color": color, "wrap": True},
            {"type": "separator", "margin": "sm"}
        ]
    }

def flex_records_last_14_days_paged(uid: str, page: int = 0, page_size: int = 20):
    all_hits = get_records_last_14_days(uid)
    total = len(all_hits)

    if total == 0:
        return FlexSendMessage(
            alt_text="近兩週紀錄",
            contents={
                "type": "bubble",
                "body": {"type": "box", "layout": "vertical", "contents": [
                    {"type": "text", "text": "📒 近兩週紀錄", "weight": "bold", "size": "lg"},
                    {"type": "text", "text": "（沒有資料）", "margin": "md", "color": "#888888"}
                ]}
            }
        )

    max_page = (total - 1) // page_size
    page = max(0, min(page, max_page))

    start_idx = page * page_size
    end_idx = min(start_idx + page_size, total)
    page_hits = all_hits[start_idx:end_idx]

    chunk_size = 5
    bubbles = []
    for bi in range(0, len(page_hits), chunk_size):
        chunk = page_hits[bi:bi + chunk_size]
        body_contents = []

        if bi == 0:
            body_contents.extend([
                {"type": "text", "text": "📒 近兩週紀錄", "weight": "bold", "size": "lg"},
                {"type": "text", "text": f"第 {page+1}/{max_page+1} 頁｜顯示 {start_idx+1}-{end_idx} / {total}",
                 "size": "xs", "color": "#888888", "margin": "sm"},
                {"type": "separator", "margin": "md"},
            ])

        for r in chunk:
            body_contents.append(_record_item_box(r))

        bubble = {
            "type": "bubble",
            "size": "mega",
            "body": {"type": "box", "layout": "vertical", "spacing": "sm", "contents": body_contents}
        }
        bubbles.append(bubble)

    footer_btns = []
    if page > 0:
        footer_btns.append({
            "type": "button",
            "height": "sm",
            "style": "secondary",
            "action": {"type": "postback", "label": "⬅ 上一頁", "data": f"cmd=records_page&page={page-1}"}
        })
    if page < max_page:
        footer_btns.append({
            "type": "button",
            "height": "sm",
            "style": "primary",
            "action": {"type": "postback", "label": "下一頁 ➡", "data": f"cmd=records_page&page={page+1}"}
        })
    if footer_btns:
        bubbles[-1]["footer"] = {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": footer_btns}

    return FlexSendMessage(
        alt_text="近兩週紀錄",
        contents={"type": "carousel", "contents": bubbles}
    )

# ====== Health ======
@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

# ====== Webhook ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        app.logger.exception(f"Webhook error: {e}")
        abort(500)
    return "OK"

# ====== Postback handler ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = (event.postback.data or "").strip()
    uid = getattr(event.source, "user_id", None)

    def reply(msg):
        line_bot_api.reply_message(event.reply_token, msg)

    try:
        # Rich Menu：點名
        if data == "action=attendance":
            if not uid or not is_teacher(uid):
                reply(TextSendMessage(text="此功能僅限老師使用。"))
                return
            reply(flex_student_picker(uid, page=0))
            return

        # Rich Menu：紀錄
        if data == "action=records":
            if not uid or not is_teacher(uid):
                reply(TextSendMessage(text="此功能僅限老師使用。"))
                return
            reply(flex_records_last_14_days_paged(uid, page=0, page_size=20))
            return

        # 其他 postback：全部限定老師
        if not uid or not is_teacher(uid):
            reply(TextSendMessage(text="此功能僅限老師使用。"))
            return

        qs = parse_qs(data)
        cmd = qs.get("cmd", "")

        # 紀錄翻頁
        if cmd == "records_page":
            try:
                page = int(qs.get("page", "0"))
            except:
                page = 0
            reply(flex_records_last_14_days_paged(uid, page=page, page_size=20))
            return

        # 點名頁翻頁
        if cmd == "attendance_page":
            try:
                page = int(qs.get("page", "0"))
            except:
                page = 0
            reply(flex_student_picker(uid, page=page))
            return

        # 直接點學生 = 扣 1 堂
        if cmd == "attendance_mark":
            name = dec(qs.get("name", "")).strip()
            if not name:
                reply(flex_warning_card("⚠️ 失敗", "找不到學生名稱，請重試。"))
                return

            # 防亂點：該學生必須屬於這位老師
            students = get_teacher_students(uid)
            if name not in students:
                reply(flex_warning_card("⚠️ 無法操作", f"{name} 不在你的學生名單內。"))
                return

            # 防併發重複操作
            if not try_enter_inflight(uid):
                reply(flex_warning_card("⏳ 處理中", "你剛剛有一筆操作尚未完成，請等一下再點。"))
                return

            try:
                cleanup_runtime_maps()

                used = 1.0
                dedup_key = f"{uid}|{name}|{fmt_num(used)}"

                # 記憶體防連點
                if dedup_key in RECENT_ACTIONS:
                    reply(flex_warning_card("⚠️ 疑似重複點名", f"{name} 剛剛已經記錄過了，這次不再重複扣堂。"))
                    return

                # Sheet log 再擋一次
                if has_recent_duplicate_log(uid, name, fmt_num(used), "上課", window_sec=20):
                    RECENT_ACTIONS[dedup_key] = _now_ts()
                    reply(flex_warning_card("⚠️ 疑似重複點名", f"{name} 剛剛已經記錄過了，這次不再重複扣堂。"))
                    return

                before = get_remaining(name)
                after = round(before - used, 2)

                if after < 0:
                    reply(flex_warning_card("⚠️ 堂數不足", f"{name} 目前剩 {fmt_num(before)} 堂，不能再扣 1 堂。"))
                    return

                set_remaining(name, after)
                append_log(uid, name, fmt_num(used), "上課", after)

                RECENT_ACTIONS[dedup_key] = _now_ts()
                LAST_SUCCESS[uid] = {
                    "ts": _now_ts(),
                    "student_name": name,
                    "used": used,
                    "before": before,
                    "after": after,
                    "type": "上課"
                }

                reply(flex_done_card(name, used, before, after))
                return

            finally:
                leave_inflight(uid)

        # 更正上一筆
        if cmd == "undo_last":
            cleanup_runtime_maps()
            last = LAST_SUCCESS.get(uid)

            if not last:
                reply(flex_warning_card("⚠️ 無法更正", "找不到最近一筆可更正紀錄，或已超過可更正時間。"))
                return

            if _now_ts() - last.get("ts", 0) > UNDO_WINDOW_SEC:
                LAST_SUCCESS.pop(uid, None)
                reply(flex_warning_card("⚠️ 無法更正", "已超過更正時間。"))
                return

            name = last["student_name"]
            used = float(last["used"])
            action_type = last.get("type", "上課")

            if not try_enter_inflight(uid):
                reply(flex_warning_card("⏳ 處理中", "系統正在處理上一筆操作，請稍後再試。"))
                return

            try:
                if action_type == "上課":
                    current = get_remaining(name)
                    restored = round(current + used, 2)
                    set_remaining(name, restored)
                    append_log(uid, name, fmt_num(used), "更正", restored)
                    LAST_SUCCESS.pop(uid, None)

                    reply(FlexSendMessage(
                        alt_text="已更正",
                        contents={
                            "type": "bubble",
                            "body": {
                                "type": "box",
                                "layout": "vertical",
                                "spacing": "md",
                                "contents": [
                                    {"type": "text", "text": "↩️ 已更正成功", "weight": "bold", "size": "xl"},
                                    {"type": "text", "text": name, "weight": "bold", "size": "lg"},
                                    {"type": "text", "text": f"已回補 {fmt_num(used)} 堂", "size": "md"},
                                    {"type": "text", "text": f"目前剩餘：{fmt_num(restored)}", "size": "sm", "color": "#555555"},
                                    {"type": "text", "text": now_taipei_str(), "size": "xs", "color": "#999999"}
                                ]
                            },
                            "footer": {
                                "type": "box",
                                "layout": "vertical",
                                "spacing": "sm",
                                "contents": [
                                    {
                                        "type": "button",
                                        "height": "sm",
                                        "style": "primary",
                                        "action": {"type": "postback", "label": "繼續點名", "data": "cmd=attendance_page&page=0"}
                                    }
                                ]
                            }
                        }
                    ))
                    return

                reply(flex_warning_card("⚠️ 無法更正", "目前這筆紀錄不支援更正。"))
                return

            finally:
                leave_inflight(uid)

        reply(TextSendMessage(text=f"收到操作：{data}"))
        return

    except Exception as e:
        app.logger.exception(f"Postback handler error: {e}")
        try:
            reply(TextSendMessage(text=f"⚠️ 系統錯誤：{e}"))
        except:
            pass
        return

# ====== Message handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()
    uid = getattr(event.source, "user_id", None)

    try:
        # ID 查詢
        if text in ["老師報到", "ID", "id", "我的ID", "我的 id", "我的Id"]:
            if not uid:
                line_bot_api.reply_message(event.reply_token, TextSendMessage(text="⚠️ 目前拿不到你的 user_id。"))
                return
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"✅ 你的 user_id：{uid}"))
            return

        # 快速測試：輸入 點名 也可進去
        if text == "點名":
            if not uid or not is_teacher(uid):
                return
            line_bot_api.reply_message(event.reply_token, flex_student_picker(uid, page=0))
            return

        if text == "紀錄":
            if not uid or not is_teacher(uid):
                return
            line_bot_api.reply_message(event.reply_token, flex_records_last_14_days_paged(uid, page=0, page_size=20))
            return

        # 其他文字靜默
        return

    except Exception as e:
        app.logger.exception(f"Message handler error: {e}")
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"⚠️ 系統錯誤：{e}"))
        except:
            pass
        return

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
