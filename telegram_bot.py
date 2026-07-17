import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BRAIN_API = "https://api.zerotobuilt.in"
ALLOWED_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
API_KEY = os.getenv("API_KEY", "631510f78e3cec7d45a27036be924ba432b33a2d64d822b1b4897ce03c7777ae")
BRAIN_HEADERS = {"X-API-Key": API_KEY}
TG_BASE = f"https://api.telegram.org/bot{TOKEN}"


async def get_updates(offset=None):
    params = {"timeout": 30, "allowed_updates": ["message"]}
    if offset:
        params["offset"] = offset
    async with httpx.AsyncClient(timeout=35) as client:
        r = await client.get(f"{TG_BASE}/getUpdates", params=params)
        return r.json().get("result", [])


async def send_message(chat_id: int, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TG_BASE}/sendMessage",
                          json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})


async def capture_to_brain(content: str, source: str = "telegram", capture_type: str = "text") -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{BRAIN_API}/capture",
                              headers=BRAIN_HEADERS,
                              json={"content": content, "source": source,
                                    "capture_type": capture_type})
        return r.json()


async def main():
    print(f"Brain Telegram bot started. Listening for chat_id: {ALLOWED_CHAT_ID}")
    offset = None
    while True:
        try:
            updates = await get_updates(offset)
            for update in updates:
                offset = update["update_id"] + 1
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
                result = await capture_to_brain(text)
                item_id = result.get("id", "?")
                await send_message(chat_id, f"✅ Captured → classifying\n`{item_id[:8]}...`")

        except Exception as e:
            print(f"[bot] error: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
