# ==========================================================
# TONNAGEFLOW PULSE
# WhatsApp Webhook (Meta Cloud API) - Phase 1
# ==========================================================
#
# Phase 1 scope only:
#   - GET  /webhooks/whatsapp  Meta subscription verification
#   - POST /webhooks/whatsapp  Acknowledge valid JSON
#   - GET  /health             Liveness check
#
# Deliberately out of scope for Phase 1:
#   - Parsing inbound WhatsApp messages
#   - Sending outbound WhatsApp messages
#   - Writing anything to Supabase
#
# This module is separate from src/main.py and src/database.py.
# It may import from them later to reuse persistence functions,
# but neither of those files import from this module - the
# Pulse CLI engine keeps working unchanged.
#
# Privacy: never log request bodies, query parameters, tokens,
# or phone numbers. Only status-level messages are printed/logged.

import os

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

load_dotenv()

app = FastAPI(title="TonnageFlow Pulse WhatsApp Webhook")


# ==========================================================
# HEALTH
# ==========================================================


@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "TonnageFlow Pulse API",
        "version": "1.0.0",
    }


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "whatsapp-webhook",
    }


# ==========================================================
# WEBHOOK VERIFICATION (META SUBSCRIPTION HANDSHAKE)
# ==========================================================


@app.get("/webhooks/whatsapp")
def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    configured_token = os.getenv("WHATSAPP_VERIFY_TOKEN")

    if not configured_token:
        return PlainTextResponse(
            "Webhook verification is not configured.",
            status_code=403,
        )

    if (
        hub_mode == "subscribe"
        and hub_verify_token == configured_token
        and hub_challenge is not None
    ):
        return PlainTextResponse(hub_challenge, status_code=200)

    return PlainTextResponse("Verification failed.", status_code=403)


# ==========================================================
# WEBHOOK RECEIVER (ACKNOWLEDGEMENT ONLY - PHASE 1)
# ==========================================================


@app.post("/webhooks/whatsapp")
async def receive_webhook(request: Request):
    try:
        payload = await request.json()

    except Exception:
        return JSONResponse(
            {"status": "error", "reason": "invalid_json"},
            status_code=400,
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            {"status": "error", "reason": "invalid_json"},
            status_code=400,
        )

    # Phase 1: acknowledge receipt only. Do not parse message
    # content, do not send replies, do not write to Supabase.
    return JSONResponse({"status": "received"}, status_code=200)
