import os
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    PostbackEvent,
    QuickReply,
    QuickReplyButton,
    MessageAction
)

app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


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


# ====== 點名按鈕名單 ======
students = ["小明", "小華", "小美"]


def student_quick_reply():
    buttons = [
        QuickReplyButton(action=MessageAction(label=name, text=f"選擇學生:{name}"))
        for name in students
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


# ====== 文字處理 ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    # 選擇學生後
    if text.startswith("選擇學生:"):
        name = text.split(":")[1]
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"請選擇 {name} 上課堂數",
                quick_reply=lesson_quick_reply(name)
            )
        )
        return

    # 選擇堂數後
    if text.startswith("堂數:"):
        _, name, lesson = text.split(":")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"✅ 已為 {name} 記錄 {lesson} 堂（日期自動記今天）"
            )
        )
        return

    # 其他文字
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text="請使用下方選單操作")
    )


# ====== Rich Menu Postback ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data

    if data == "action=attendance":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="請選擇學生",
                quick_reply=student_quick_reply()
            )
        )
    elif data == "action=records":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="紀錄查詢功能建置中")
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
