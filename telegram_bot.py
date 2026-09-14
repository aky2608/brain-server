import asyncio
import os
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BRAIN_API = "https://api.zerotobuilt.in"
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise RuntimeError("API_KEY not set")
BRAIN_HEADERS = {"X-API-Key": API_KEY}
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"


async def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
    if offset:
        params["offset"] = offset
    async with httpx.AsyncClient(timeout=35) as client:
        r = await client.get(f"{TG_BASE}/getUpdates", params=params)
        return r.json().get("result", [])


async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_BASE}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


async def answer_callback(callback_query_id: str, text: str = "") -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TG_BASE}/answerCallbackQuery",
                          json={"callback_query_id": callback_query_id, "text": text})


async def edit_message_reply_markup(chat_id: int, message_id: int) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(f"{TG_BASE}/editMessageReplyMarkup",
                          json={"chat_id": chat_id, "message_id": message_id,
                                "reply_markup": {"inline_keyboard": [[]]}})


async def capture_to_brain(
    content: str,
    source: str = "telegram",
    capture_type: str = "text",
    chat_id: Optional[int] = None,
) -> dict:
    payload: dict = {"content": content, "source": source, "capture_type": capture_type}
    if chat_id is not None:
        payload["metadata"] = {"chat_id": chat_id}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{BRAIN_API}/capture", headers=BRAIN_HEADERS, json=payload)
        return r.json()


async def handle_callback(cb: dict) -> None:
    cb_id = cb["id"]
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")

    parts = data.split(":", 1)
    if len(parts) != 2 or parts[0] not in ("promote", "kill"):
        await answer_callback(cb_id, "Unknown action")
        return

    action, approval_id = parts[0], parts[1]
    endpoint = f"{BRAIN_API}/build/{approval_id}/{action}"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(endpoint, headers=BRAIN_HEADERS)

        if r.status_code == 409:
            await answer_callback(cb_id, f"Already {r.text.strip()}")
            return

        r.raise_for_status()
        result = r.json()

        if action == "promote":
            toast = "PR created ✅"
            follow_up = f"PR opened: {result['pr_url']}"
        else:
            toast = "Branch killed ❌"
            follow_up = None

        await answer_callback(cb_id, toast)
        if chat_id and message_id:
            await edit_message_reply_markup(chat_id, message_id)
        if follow_up and chat_id:
            await send_message(chat_id, follow_up)

    except Exception as e:
        print(f"[bot] callback error ({action} {approval_id[:8]}): {e}")
        await answer_callback(cb_id, f"Error: {str(e)[:100]}")


async def main():
    print(f"Brain Telegram bot started. Listening for chat_id: {ALLOWED_CHAT_ID}")
    offset = None
    while True:
        try:
            updates = await get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1

                cb = update.get("callback_query")
                if cb:
                    cb_chat_id = cb.get("message", {}).get("chat", {}).get("id")
                    if cb_chat_id != ALLOWED_CHAT_ID:
                        await answer_callback(cb["id"], "")
                        continue
                    await handle_callback(cb)
                    continue

                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")

                if chat_id != ALLOWED_CHAT_ID:
                    continue

                text = msg.get("text", "").strip()

                if not text:
                    continue

                if text == "/start":
                    await send_message(chat_id, "🧠 *Brain bot ready.*\nSend me anything — thoughts, tasks, URLs, ideas.")
                    continue

                if text == "/status":
                    async with httpx.AsyncClient() as client:
                        r = await client.get(f"{BRAIN_API}/items", headers=BRAIN_HEADERS)
                        count = r.json().get("count", 0)
                    await send_message(chat_id, f"🧠 Brain has *{count}* captures so far.")
                    continue

                # Capture it
                result = await capture_to_brain(text, chat_id=chat_id)
                item_id = result.get("id", "?")
                await send_message(chat_id, f"✅ Captured → classifying\n`{item_id[:8]}...`")

        except Exception as e:
            print(f"[bot] error: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
