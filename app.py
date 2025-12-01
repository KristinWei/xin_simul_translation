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

from dotenv import load_dotenv
load_dotenv()


# ==== CONFIG ====
YOUDAO_APP_KEY = os.getenv("YOUDAO_APP_KEY")
YOUDAO_APP_SECRET = os.getenv("YOUDAO_APP_SECRET")


YOUDAO_WS_BASE = "wss://openapi.youdao.com/stream_speech_trans"

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


# ==== AUTH helper ====
def sha256(s: str) -> str:
    h = hashlib.sha256()
    h.update(s.encode("utf-8"))
    return h.hexdigest()


def build_youdao_url(app_key, app_secret, from_lang="en-US", to_lang="zh-CHS"):
    from urllib.parse import urlencode

    salt = uuid.uuid4().hex
    curtime = str(int(time.time()))
    sign = sha256(app_key + salt + curtime + app_secret)

    params = {
        "appKey": app_key,
        "salt": salt,
        "curtime": curtime,
        "signType": "v4",
        "sign": sign,
        "from": from_lang,
        "to": to_lang,
        "format": "wav",
        "rate": "16000",
        "channel": "1",
        "version": "v1",
    }

    return f"{YOUDAO_WS_BASE}?{urlencode(params)}"


# ==== YOUDAO CLIENT ====
class YoudaoStreamClient:
    def __init__(self, key, secret, from_lang, to_lang):
        self.key = key
        self.secret = secret
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.ws: Optional[websockets.WebSocketClientProtocol] = None

    async def __aenter__(self):
        url = build_youdao_url(
            self.key,
            self.secret,
            self.from_lang,
            self.to_lang,
        )
        self.ws = await websockets.connect(url)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        try:
            if self.ws and not self.ws.closed:
                await self.ws.close()
        except:
            pass

    async def send_audio(self, chunk):
        if self.ws and not self.ws.closed:
            await self.ws.send(chunk)

    async def send_end(self):
        """Tell Youdao the audio stream is finished."""
        if self.ws and not self.ws.closed:
            try:
                await self.ws.send(b'{"end":"true"}')
            except:
                # connection already closed — ignore
                pass

    async def recv_messages(self):
        """Yield messages until Youdao closes the socket."""
        try:
            async for msg in self.ws:
                try:
                    yield json.loads(msg)
                except:
                    yield {"raw": msg}

        except websockets.ConnectionClosedOK:
            # Normal Youdao closure
            return

        except Exception as e:
            print("Youdao closed:", e)


# ==== FASTAPI APP ====
app = FastAPI()

# Serve /static/*
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ==== WEBSOCKET ====
@app.websocket("/ws/translate")
async def translate_ws(websocket: WebSocket):
    await websocket.accept()


    from_lang = "zh-CHS"
    to_lang = "en-US"

    browser_closed = False
    youdao_closed = False

    async with YoudaoStreamClient(
        YOUDAO_APP_KEY, YOUDAO_APP_SECRET, from_lang, to_lang
    ) as yd:

        async def browser_to_youdao():
            nonlocal browser_closed, youdao_closed
            try:
                while True:
                    msg = await websocket.receive()

                    if "bytes" in msg and msg["bytes"] is not None:
                        await yd.send_audio(msg["bytes"])

                    elif msg.get("text") == "END":
                        await yd.send_end()
                        youdao_closed = True
                        break

            except WebSocketDisconnect:
                browser_closed = True
                await yd.send_end()

            except Exception as e:
                browser_closed = True
                print("browser_to_youdao error:", e)
                await yd.send_end()

        async def youdao_to_browser():
            nonlocal youdao_closed, browser_closed
            try:
                async for data in yd.recv_messages():
                    if not browser_closed:
                        await websocket.send_text(json.dumps(data, ensure_ascii=False))
            except Exception as e:
                print("youdao_to_browser error:", e)
            finally:
                youdao_closed = True

        await asyncio.gather(browser_to_youdao(), youdao_to_browser())

        # Final cleanup — close browser WS safely
        try:
            await websocket.close()
        except:
            pass