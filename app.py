import os
import json
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
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

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


# ====== Utility ======
def now_taipei_str():
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def get_students_list():
    records = ws_students.get_all_records()
    return [r["student_name"] for r in records if r.get("student_name")]


def find_student_row(student_name: str):
    names = ws_students.col_values(1)  # A欄：student_name
    for idx, n in enumerate(names[1:], start=2):
        if n == student_name:
            return idx
    return None


def get_remaining(student_name: str) -> float:
    row = find_student_row(student_name)
    if not row:
        raise ValueError("student not found")
    val = ws_students.cell(row, 2).value
    return float(val) if val else 0.0


def set_remaining(student_name: str, remaining: float):
    row = find_student_row(student_name)
    if not row:
        raise ValueError("student not found")
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


# ====== Quick Reply ======
def student_quick_reply():
    students = get_students_list()
    buttons = [
        QuickReplyButton(action=MessageAction(label=n, text=f"選擇學生:{n}"))
        for n in students[:13]
    ]
    return QuickReply(items=buttons)


def lesson_quick_reply(name):
    lessons = ["0.5", "1", "1.5", "2", "請假"]
    buttons = [
        QuickReplyButton(
            action=MessageAction(label=l, text=f"堂數:{name}:{l}")
        )
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
    data = event.postback.data

    if data == "action=attendance":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請選擇學生", quick_reply=student_quick_reply())
        )
    elif data == "action=records":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="📒 紀錄查詢功能建置中")
        )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"收到操作：{data}")
        )


# ====== Text Handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()

    # 聯絡教室
    if text == "聯絡教室":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="禾禾音樂教室\n電話：0978-136-812\nLINE：bravop109")
        )
        return

    # 選學生
    if text.startswith("選擇學生:"):
        name = text.split(":", 1)[1].strip()
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"請選擇 {name} 上課堂數",
                quick_reply=lesson_quick_reply(name)
            )
        )
        return

    # 選堂數
    if text.startswith("堂數:"):
        try:
            _, name, lesson = text.split(":", 2)
            teacher_id = event.source.user_id

            # 請假
            if lesson == "請假":
                remaining = get_remaining(name)
                append_log(teacher_id, name, "", "請假", remaining)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"✅ 已記錄 {name}：請假\n剩餘：{remaining} 堂"
                    )
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

    # 其他
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請使用下方選單（點名 / 紀錄 / 聯絡教室）")
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
