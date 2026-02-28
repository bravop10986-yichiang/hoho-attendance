import os
from flask import Flask, request, abort

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    PostbackEvent,
)

app = Flask(__name__)

# ====== ENV VARS (Render Environment) ======
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# 防呆：如果忘了設定環境變數，至少不要整個直接報奇怪錯
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError(
        "Missing env vars. Please set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET in Render."
    )

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# ====== Webhook endpoint ======
@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


# ====== Text message handler ======
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = (event.message.text or "").strip()

    # 你的既有功能：聯絡教室
    if text == "聯絡教室":
        reply = "禾禾音樂教室\n電話：0978-136-812\nLINE：bravop109"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 暫時先給老師用的文字指令（之後我們會改成按鈕式流程）
    if text.startswith("點名"):
        # 例：點名 小明
        name = text.replace("點名", "", 1).strip()
        if not name:
            reply = "✅ 點名：請輸入「點名 + 學生姓名」\n例如：點名 小明"
        else:
            reply = f"✅ 已收到點名：{name}\n（下一步我會讓你選 0.5/1/1.5/2/請假）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text.startswith("紀錄"):
        # 例：紀錄 小明
        name = text.replace("紀錄", "", 1).strip()
        if not name:
            reply = "📒 紀錄：請輸入「紀錄 + 學生姓名」\n例如：紀錄 小明"
        else:
            reply = f"📒 查詢紀錄：{name}\n（下一步我會接上後台：日期/扣堂/剩餘堂數）"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 其他任何文字都先回覆收到
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"收到：{text}")
    )


# ====== Postback handler (Rich Menu: 點名/紀錄) ======
@handler.add(PostbackEvent)
def handle_postback(event):
    data = event.postback.data  # 例如 "action=attendance"

    if data == "action=attendance":
        reply = "✅ 點名：請輸入「點名 + 學生姓名」\n例如：點名 小明"
    elif data == "action=records":
        reply = "📒 紀錄：請輸入「紀錄 + 學生姓名」\n例如：紀錄 小明"
    else:
        reply = f"收到操作：{data}"

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


# ====== Local run (optional) ======
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
