"""Green API webhook router.

A single public URL that demultiplexes incoming WhatsApp webhooks by chatId
and forwards each event to the bot responsible for that chat. This lets many
independent bots share one Green API instance (one phone number) without
fighting over the global webhookUrl setting.

Routing config — env var ``ROUTES_JSON`` is a JSON object:
    {"<chatId>@g.us|@c.us": "https://<bot>.onrender.com/webhook", ...}

Forwarding is fire-and-forget on a background task: we 200-OK Green API
immediately so it never retries due to slow downstream cold starts. The
downstream bots already have their own dedup, so even occasional duplicates
(from a hypothetical Green API retry) are harmless.

We never *set* the Green API webhook from inside the router — that's a
one-time human action pointed at this service. The bots downstream don't
touch it either, so once it's set here it stays.
"""
from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx
from fastapi import BackgroundTasks, FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("router")

ROUTES: dict[str, str] = json.loads(os.environ.get("ROUTES_JSON", "{}"))
DEFAULT_ROUTE: str = os.environ.get("DEFAULT_ROUTE", "").strip()  # optional fallback


_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _client
    _client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
    )
    log.info("router up | routes=%d default=%s", len(ROUTES), bool(DEFAULT_ROUTE))
    try:
        yield
    finally:
        await _client.aclose()


app = FastAPI(title="Green API webhook router", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "routes": {chat: url for chat, url in ROUTES.items()},
        "default": DEFAULT_ROUTE or None,
    }


@app.post("/webhook")
async def webhook(request: Request, bg: BackgroundTasks) -> dict:
    try:
        payload = await request.json()
    except Exception:
        log.warning("invalid JSON body, dropping")
        return {"ok": True, "note": "invalid_json"}

    chat_id = ""
    if isinstance(payload, dict):
        chat_id = (payload.get("senderData") or {}).get("chatId", "") or ""

    target = ROUTES.get(chat_id) or DEFAULT_ROUTE
    if not target:
        log.info("no route for chat=%s typeWebhook=%s — dropping",
                 chat_id, payload.get("typeWebhook") if isinstance(payload, dict) else "?")
        return {"ok": True, "routed": None}

    bg.add_task(_forward, target, payload, chat_id)
    return {"ok": True, "routed_to": target}


async def _forward(url: str, payload: dict, chat_id: str) -> None:
    assert _client is not None
    try:
        resp = await _client.post(url, json=payload)
        log.info("forward chat=%s -> %s status=%s", chat_id, url, resp.status_code)
    except Exception:
        log.exception("forward failed chat=%s url=%s", chat_id, url)
