"""Azure Functions entry point — the NauroLabs interactive ops bot webhook.

One always-on HTTP endpoint that Telegram POSTs updates to. Today it drives the idea
feedback loop (approve/decline autoRefine proposals); new scenarios — approve deploys,
status queries, trigger a plan run — slot in as new branches in ``handlers.handle_update``.
"""
import json
import logging

import azure.functions as func
import httpx

import config
from github_ops import GitHub
from handlers import handle_update
from telegram import Telegram

app = func.FunctionApp()
log = logging.getLogger("naurobot")


@app.route(route="telegram", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
async def telegram_webhook(req: func.HttpRequest) -> func.HttpResponse:
    """Receive a Telegram update and drive the idea feedback loop.

    Telegram can't send a Function key, so we authenticate via the secret token registered
    at setWebhook time (echoed in the ``X-Telegram-Bot-Api-Secret-Token`` header).
    """
    if config.WEBHOOK_SECRET:
        if req.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != config.WEBHOOK_SECRET:
            return func.HttpResponse("forbidden", status_code=403)

    try:
        update = req.get_json()
    except ValueError:
        return func.HttpResponse('{"ok":false}', mimetype="application/json", status_code=400)

    if not config.BOT_TOKEN or not config.GITHUB_TOKEN:
        log.error("nauroBot not configured (missing NAURO_BOT_TOKEN or GH_ASSIGN_PAT)")
        # 200 so Telegram doesn't retry a config problem it can't fix.
        return func.HttpResponse('{"ok":false}', mimetype="application/json", status_code=200)

    async with httpx.AsyncClient(timeout=20) as client:
        tg = Telegram(config.BOT_TOKEN, client)
        gh = GitHub(config.GITHUB_TOKEN, config.GITHUB_OWNER, client)
        try:
            result = await handle_update(update, tg, gh, config.ALLOWED_CHAT_ID)
        except Exception:
            log.exception("update handling failed")
            result = {"ok": False, "error": "exception"}

    # Always 200 — a non-200 makes Telegram retry-storm on a handler bug.
    return func.HttpResponse(json.dumps(result), mimetype="application/json", status_code=200)
