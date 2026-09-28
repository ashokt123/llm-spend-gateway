"""Scripted walkthrough: plays several users sending traffic through the gateway.

Also runs a tiny webhook receiver so budget alerts appear inline, the way they
would land in a Slack channel. Run via `make demo`, which starts the gateway first.
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from gateway.config import ROOT

GATEWAY = "http://127.0.0.1:8000"
SINK_PORT = 9009

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"

KEYS = json.loads((ROOT / ".demo_keys.json").read_text())
client = httpx.Client(base_url=GATEWAY, timeout=10)


class AlertSink(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
        text = payload.get("text") or payload.get("content")
        print(f"    {YELLOW}{BOLD}⚠ webhook → #ai-platform-alerts:{RESET}{YELLOW} {text}{RESET}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def scene(title: str) -> None:
    print(f"\n{BOLD}{CYAN}── {title} {'─' * (60 - len(title))}{RESET}")
    time.sleep(0.4)


def ask(user: str, model: str, prompt: str, max_tokens: int = 800, quiet_ok: bool = False) -> int:
    r = client.post(
        "/v1/messages",
        headers={"x-api-key": KEYS[user]},
        json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
    )
    who = f"[{user:<16}] {model:<16}"
    if r.status_code == 200:
        u = r.json()["usage"]
        cost = float(r.headers["x-gateway-cost-usd"])
        team = ""
        if "x-gateway-team-spend-usd" in r.headers:
            team = f"team ${float(r.headers['x-gateway-team-spend-usd']):.3f} / ${r.headers['x-gateway-team-budget-usd']}"
        if not quiet_ok:
            print(f"  {who} {GREEN}→ 200{RESET} {u['input_tokens'] + u['output_tokens']:>5} tok  ${cost:.4f}  {DIM}{team}{RESET}")
    else:
        body = r.json()
        print(f"  {who} {RED}→ {r.status_code} {body.get('gateway_reason')}{RESET}  {DIM}{body['error']['message']}{RESET}")
    return r.status_code


def admin_get(path: str):
    return client.get(path, headers={"x-api-key": KEYS["admin@acme.com"]}).json()


def wait_for_gateway() -> None:
    for _ in range(50):
        try:
            if client.get("/healthz").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    sys.exit("gateway did not start on " + GATEWAY)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", SINK_PORT), AlertSink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    wait_for_gateway()

    scene("1. Normal traffic")
    ask("alice@acme.com", "claude-haiku-4-5", "Draft a launch email for our new AI assistant.")
    ask("bob@acme.com", "claude-sonnet-5", "Review this Python function for bugs.", max_tokens=1200)

    scene("2. Role-based model access")
    ask("alice@acme.com", "claude-opus-5", "Summarize the Q3 board deck.")

    scene("3. Runaway CI job hits the team budget")
    print(f"  {DIM}ci-bot@acme.com loops Sonnet calls; engineering's budget is $0.20/day{RESET}")
    for i in range(1, 60):
        status = ask("ci-bot@acme.com", "claude-sonnet-5", f"Generate unit tests for module {i}.", max_tokens=1500)
        if status == 429:
            break
        time.sleep(0.05)
    time.sleep(0.3)
    print(f"  {DIM}same team, different person:{RESET}")
    ask("bob@acme.com", "claude-sonnet-5", "Explain this stack trace.", max_tokens=1500)
    print(f"  {DIM}different team is unaffected:{RESET}")
    ask("alice@acme.com", "claude-haiku-4-5", "Write three taglines for the launch.")
    time.sleep(0.3)

    scene("4. Burst from one key hits the rate limit")
    print(f"  {DIM}alice's role (pilot) allows 10 requests/minute; she sends 10 more quickly{RESET}")
    for _ in range(10):
        if ask("alice@acme.com", "claude-haiku-4-5", "Rewrite this sentence.", max_tokens=100, quiet_ok=True) == 429:
            break

    scene("5. Offboarding (what a SCIM deactivation would trigger)")
    r = client.post("/admin/users/bob@acme.com/deactivate", headers={"x-api-key": KEYS["admin@acme.com"]})
    print(f"  admin deactivates bob@acme.com → {r.json()}")
    ask("bob@acme.com", "claude-haiku-4-5", "Am I still in?")

    scene("6. Finance and security views")
    report = admin_get("/admin/usage?period=today")
    print(f"  {BOLD}{'team':<13}{'requests':>9}{'tokens':>9}{'cost':>10}{'budget':>9}{'used':>7}{RESET}")
    for t in report["teams"]:
        budget = f"${t['budget_usd']:.2f}" if t["budget_usd"] else "—"
        used = f"{t['budget_used']:.0%}" if t["budget_used"] is not None else "—"
        print(f"  {t['team']:<13}{t['requests']:>9}{t['tokens']:>9,}{'$' + format(t['cost_usd'], '.3f'):>10}{budget:>9}{used:>7}")
    print(f"\n  {BOLD}{'user':<18}{'requests':>9}{'cost':>10}{RESET}")
    for u in report["users"]:
        print(f"  {u['user']:<18}{u['requests']:>9}{'$' + format(u['cost_usd'], '.3f'):>10}")

    lines = (ROOT / "audit.jsonl").read_text().splitlines()
    print(f"\n  {BOLD}audit.jsonl{RESET} {DIM}({len(lines)} events, metadata only, no prompt text). Last entry:{RESET}")
    print("  " + json.dumps(json.loads(lines[-1]), indent=2).replace("\n", "\n  "))
    server.shutdown()


if __name__ == "__main__":
    main()
