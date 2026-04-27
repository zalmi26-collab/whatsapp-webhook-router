"""Green API webhook router.

A single public URL that demultiplexes incoming WhatsApp webhooks by chatId
and forwards each event to the bot responsible for that chat. This lets many
independent bots share one Green API instance (one phone number) without
fighting over the global webhookUrl setting.

Routing config — env var ``ROUTES_JSON`` is a JSON object:
    {"<chatId>@g.us|@c.us": "https://<bot>.onrender.com/webhook", ...}

Forwarding is fire-and-forget on a background task with **retry on 5xx** —
we 200-OK Green API immediately so it never retries the entire flow due to
slow downstream cold starts, but the router itself retries the downstream a
few times because Render Free sleeps after 15 min idle and the first
webhook to a sleeping bot returns 502 from the edge before the container
finishes booting. Subsequent retries (with backoff) usually succeed.

Downstream bots already have their own dedup keyed on idMessage, so even
the rare double-deliver caused by router retries is harmless.

We never *set* the Green API webhook from inside the router — that's a
one-time human action pointed at this service. The bots downstream don't
touch it either, so once it's set here it stays.
"""
from __future__ import annotations

import asyncio
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
    """POST the original webhook body to ``url``. Retry on 5xx because the
    first request to a sleeping Render Free service returns 502 from the
    edge while the container is still cold-starting; the request itself
    triggers the wake-up so a retry ~20s later usually lands on a live
    container."""
    assert _client is not None
    delays = [0, 20, 40]  # immediate, then back off to ride out cold starts
    last_status: Optional[int] = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            resp = await _client.post(url, json=payload)
        except Exception as e:
            log.warning("forward chat=%s attempt=%d transport error: %s", chat_id, attempt, e)
            continue
        last_status = resp.status_code
        if resp.status_code < 500:
            log.info("forward chat=%s -> %s status=%s attempt=%d",
                     chat_id, url, resp.status_code, attempt)
            return
        log.warning("forward chat=%s -> %s status=%s attempt=%d, will retry",
                    chat_id, url, resp.status_code, attempt)
    log.error("forward chat=%s -> %s gave up after %d attempts (last status=%s)",
              chat_id, url, len(delays), last_status)
