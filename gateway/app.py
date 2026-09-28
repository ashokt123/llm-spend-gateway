"""LLM API gateway.

Every request to POST /v1/messages passes, in order:
  1. authenticate  virtual key -> user, team, role          (401)
  2. validate      model, max_tokens, messages; no streaming (400)
  3. authorize     model allowed for the role?               (403)
  4. rate limit    requests/minute per key                   (429)
  5. budget        team spend + in-flight + worst case       (429, or 503 if metering is down)
  6. forward       to the (mock) upstream
  7. meter         price the real usage, record it, audit log
  8. alert         webhook once per threshold per day
"""
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, mock_upstream
from .config import CONFIG, ROOT

AUDIT_PATH = os.environ.get("GATEWAY_AUDIT_LOG", str(ROOT / "audit.jsonl"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or None
# Set to 1 to pretend the spend database is unreachable and exercise fail_mode.
SIMULATE_METERING_OUTAGE = os.environ.get("GATEWAY_SIMULATE_METERING_OUTAGE") == "1"

app = FastAPI(title="LLM Gateway")

_rate_windows = defaultdict(deque)  # key_hash -> timestamps of recent requests
_reserved = defaultdict(float)      # team -> worst-case cost of requests still in flight
_background = set()                 # strong refs so webhook tasks aren't garbage-collected


# --- helpers -----------------------------------------------------------------

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def current_period() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def extract_key(request: Request) -> Optional[str]:
    key = request.headers.get("x-api-key")
    auth = request.headers.get("authorization", "")
    if not key and auth.lower().startswith("bearer "):
        key = auth[7:].strip()
    return key or None


def price(model: str, in_tok: int, out_tok: int) -> float:
    p = CONFIG["pricing_usd_per_mtok"][model]
    return (in_tok * p["input"] + out_tok * p["output"]) / 1_000_000


def estimate_input_tokens(body: dict) -> int:
    # Rough (~4 chars/token) but only used for the pre-call reservation;
    # the real count from the response is what gets billed.
    return max(1, len(json.dumps(body.get("messages", [])) + str(body.get("system", ""))) // 4)


def audit(**fields) -> None:
    """One JSON line per event. Metadata only: prompts and completions are never logged."""
    line = {"ts": db.now_iso(), **fields}
    with open(AUDIT_PATH, "a") as f:
        f.write(json.dumps(line) + "\n")


def error(status: int, etype: str, reason: str, message: str, request_id: str) -> JSONResponse:
    # Same envelope as the Claude API so existing clients handle it, plus a machine-readable reason.
    return JSONResponse(
        {"type": "error", "error": {"type": etype, "message": message}, "gateway_reason": reason},
        status_code=status,
        headers={"x-gateway-request-id": request_id},
    )


async def post_webhook(text: str) -> None:
    if not WEBHOOK_URL:
        print(f"ALERT (no WEBHOOK_URL set): {text}", file=sys.stderr)
        return
    payload = {"content": text} if "discord.com" in WEBHOOK_URL else {"text": text}  # Discord vs Slack shape
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            await client.post(WEBHOOK_URL, json=payload)
    except httpx.HTTPError as e:
        print(f"webhook failed: {e!r}", file=sys.stderr)


def alert_once(team: str, period: str, threshold: float, text: str) -> None:
    if db.mark_alert(team, period, threshold):
        audit(event="alert.sent", team=team, period=period, threshold=threshold, text=text)
        task = asyncio.create_task(post_webhook(text))
        _background.add(task)
        task.add_done_callback(_background.discard)


# --- the proxied endpoint ----------------------------------------------------

@app.post("/v1/messages")
async def messages(request: Request):
    started = time.monotonic()
    ctx = {"event": "llm.request", "request_id": "req_" + uuid.uuid4().hex[:12]}

    def reject(status: int, etype: str, reason: str, message: str) -> JSONResponse:
        audit(**ctx, status=status, reason=reason, latency_ms=int((time.monotonic() - started) * 1000))
        return error(status, etype, reason, message, ctx["request_id"])

    # 1. authenticate
    key = extract_key(request)
    key_hash = hash_key(key) if key else None
    rec = db.lookup_key(key_hash) if key_hash else None
    if rec is None:
        return reject(401, "authentication_error", "invalid_key", "Unknown or missing gateway key.")
    user, team, role = rec["user"], rec["team"], rec["role"]
    ctx.update(user=user, team=team, role=role, key_prefix=rec["key_prefix"])
    if not rec["active"]:
        return reject(401, "authentication_error", "key_revoked", "This key has been deactivated.")

    # 2. validate
    try:
        body = await request.json()
    except ValueError:
        return reject(400, "invalid_request_error", "bad_json", "Request body must be JSON.")
    model, max_tokens, msgs = body.get("model"), body.get("max_tokens"), body.get("messages")
    ctx["model"] = model
    if not isinstance(model, str) or not isinstance(max_tokens, int) or max_tokens < 1 or not msgs:
        return reject(400, "invalid_request_error", "bad_request", "model, max_tokens and messages are required.")
    if body.get("stream"):
        return reject(400, "invalid_request_error", "streaming_not_supported", "Streaming is not supported yet.")

    # 3. authorize: model allowlist per role
    allowed = CONFIG["roles"][role]["models"]
    if model not in allowed:
        return reject(403, "permission_error", "model_not_allowed",
                      f"Role '{role}' may use: {', '.join(allowed) or 'no models'}.")

    # 4. rate limit: sliding one-minute window per key
    rpm = CONFIG["roles"][role]["rpm"]
    window, now = _rate_windows[key_hash], time.monotonic()
    while window and window[0] <= now - 60:
        window.popleft()
    if len(window) >= rpm:
        return reject(429, "rate_limit_error", "rate_limited", f"Limit is {rpm} requests/minute for role '{role}'.")
    window.append(now)

    # 5. budget. No await between the check and the reservation, so concurrent
    # requests on this event loop can't both squeeze through the last dollars.
    period = current_period()
    limit = CONFIG["team_budgets_usd_per_day"].get(team)
    worst_case = price(model, estimate_input_tokens(body), max_tokens)
    metered = True
    try:
        if SIMULATE_METERING_OUTAGE:
            raise RuntimeError("metering database unreachable")
        spent = db.team_spend(team, period)
    except Exception:
        if CONFIG["fail_mode"] != "open":
            return reject(503, "api_error", "metering_unavailable",
                          "Spend tracking is unavailable; the gateway fails closed.")
        metered, spent = False, 0.0
        ctx["unmetered"] = True

    reserved = metered and limit is not None
    if reserved:
        if spent + _reserved[team] + worst_case > limit:
            alert_once(team, period, 1.0,
                       f"{team} has used its daily AI budget (${limit:.2f}). Requests are blocked until 00:00 UTC.")
            return reject(429, "rate_limit_error", "budget_exceeded",
                          f"Team '{team}' reached its ${limit:.2f} daily budget; resets 00:00 UTC.")
        _reserved[team] += worst_case

    # 6. forward
    try:
        resp = await mock_upstream.create_message(body)
    finally:
        if reserved:
            _reserved[team] -= worst_case

    # 7. meter: bill what was actually used, not the estimate
    in_tok, out_tok = resp["usage"]["input_tokens"], resp["usage"]["output_tokens"]
    cost = price(model, in_tok, out_tok)
    latency_ms = int((time.monotonic() - started) * 1000)
    if metered:
        db.record_usage(ts=db.now_iso(), period=period, request_id=ctx["request_id"], user=user, team=team,
                        model=model, in_tok=in_tok, out_tok=out_tok, cost_usd=cost, latency_ms=latency_ms)
    audit(**ctx, status=200, reason="ok", in_tok=in_tok, out_tok=out_tok,
          cost_usd=round(cost, 6), latency_ms=latency_ms)

    # 8. alert on crossed thresholds
    headers = {"x-gateway-request-id": ctx["request_id"], "x-gateway-cost-usd": f"{cost:.6f}"}
    if metered and limit is not None:
        spent_after = spent + cost
        for t in CONFIG["alert_thresholds"]:
            if spent_after >= t * limit:
                alert_once(team, period, t,
                           f"{team} has used {spent_after / limit:.0%} of its daily AI budget "
                           f"(${spent_after:.3f} of ${limit:.2f}).")
        headers["x-gateway-team-spend-usd"] = f"{spent_after:.6f}"
        headers["x-gateway-team-budget-usd"] = f"{limit:.2f}"

    return JSONResponse(resp, headers=headers)


# --- admin -------------------------------------------------------------------

def require_admin(request: Request):
    key = extract_key(request)
    rec = db.lookup_key(hash_key(key)) if key else None
    if rec is None or not rec["active"] or rec["role"] != "admin":
        return None
    return rec


@app.get("/admin/usage")
async def usage(request: Request, period: str = "today"):
    if require_admin(request) is None:
        return JSONResponse({"error": "admin key required"}, status_code=403)
    period = current_period() if period == "today" else period
    report = db.usage_report(period)
    budgets = CONFIG["team_budgets_usd_per_day"]
    for row in report["teams"]:
        limit = budgets.get(row["team"])
        row["budget_usd"] = limit
        row["budget_used"] = round(row["cost_usd"] / limit, 3) if limit else None
    return {"period": period, **report}


@app.post("/admin/users/{user}/deactivate")
async def deactivate(user: str, request: Request):
    """What a SCIM 'active: false' from the identity provider would call."""
    admin = require_admin(request)
    if admin is None:
        return JSONResponse({"error": "admin key required"}, status_code=403)
    n = db.deactivate_user(user)
    audit(event="admin.deactivate_user", actor=admin["user"], target=user, keys_deactivated=n)
    return {"user": user, "keys_deactivated": n}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
