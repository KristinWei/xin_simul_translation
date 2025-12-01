import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

import websockets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# -----------------------------------
# PUT YOUR KEYS HERE
# -----------------------------------
YOUDAO_APP_KEY = "32737ad9d0645a01"
YOUDAO_APP_SECRET = "QxoWAkmAO3DtBWdWhuImWoubHM2wbBqj"

YOUDAO_WS_BASE = "wss://openapi.youdao.com/stream_speech_trans"

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Create static directory if not exists
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI()

# Mount static folder
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ========= AUTH HELPERS =========
def _sha256(s: str) -> str:
    h = hashlib.sha256()
    h.update(s.encode("utf-8"))
    return h.hexdigest()


def build_youdao_url(
    app_key: str,
    app_secret: str,
    from_lang="zh-CHS",
    to_lang="en-US",
    format_="wav",
    rate="16000",
    channel="1",
    version="v1",
) -> str:
    from urllib.parse import urlencode

    salt = uuid.uuid4().hex
    curtime = str(int(time.time()))
    sign = _sha256(app_key + salt + curtime + app_secret)

    params = {
        "appKey": app_key,
        "salt": salt,
        "curtime": curtime,
        "signType": "v4",
        "sign": sign,
        "from": from_lang,
        "to": to_lang,
        "format": format_,
        "rate": rate,
        "channel": channel,
        "version": version,
    }

    return f"{YOUDAO_WS_BASE}?{urlencode(params)}"


# ========= CLIENT =========
class YoudaoStreamClient:
    def __init__(self, app_key, app_secret, from_lang, to_lang):
        self.app_key = app_key
        self.app_secret = app_secret
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    async def __aenter__(self):
        url = build_youdao_url(
            self.app_key,
            self.app_secret,
            self.from_lang,
            self.to_lang,
        )
        self.ws = await websockets.connect(url)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.ws and not self.ws.closed:
            await self.ws.close()

    async def send_audio(self, chunk: bytes):
        if self.ws:
            await self.ws.send(chunk)

    async def send_end(self):
        if self.ws:
            await self.ws.send(b'{"end":"true"}')

    async def recv_messages(self):
        async for msg in self.ws:
            try:
                yield json.loads(msg)
            except:
                yield {"raw": msg}


# ========= ROUTES =========

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws/translate")
async def translate_ws(websocket: WebSocket):
    await websocket.accept()

    from_lang="zh-CHS",
    to_lang="en-US",

    async with YoudaoStreamClient(
        YOUDAO_APP_KEY, YOUDAO_APP_SECRET, from_lang, to_lang
    ) as client:

        browser_closed = False

        async def browser_to_youdao():
            nonlocal browser_closed
            try:
                while True:
                    msg = await websocket.receive()

                    if "bytes" in msg and msg["bytes"] is not None:
                        await client.send_audio(msg["bytes"])

                    elif msg.get("text") == "END":
                        await client.send_end()
                        break

            except WebSocketDisconnect:
                browser_closed = True
                await client.send_end()
            except Exception as e:
                print("Browser→Youdao error:", e)
                await client.send_end()

        async def youdao_to_browser():
            try:
                async for data in client.recv_messages():
                    if not browser_closed:
                        await websocket.send_text(
                            json.dumps(data, ensure_ascii=False)
                        )
            except Exception as e:
                print("Youdao→Browser closed:", e)

        await asyncio.gather(browser_to_youdao(), youdao_to_browser())

        try:
            await websocket.close()
        except:
            pass
