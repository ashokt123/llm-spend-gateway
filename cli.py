"""Admin CLI for a running gateway.

  python cli.py usage [YYYY-MM-DD]     spend report (default: today)
  python cli.py revoke USER            deactivate every key for USER
"""
import json
import sys

import httpx

from gateway.config import ROOT

GATEWAY = "http://127.0.0.1:8000"
ADMIN_KEY = json.loads((ROOT / ".demo_keys.json").read_text())["admin@acme.com"]
headers = {"x-api-key": ADMIN_KEY}

if len(sys.argv) >= 2 and sys.argv[1] == "usage":
    period = sys.argv[2] if len(sys.argv) > 2 else "today"
    r = httpx.get(f"{GATEWAY}/admin/usage", params={"period": period}, headers=headers)
elif len(sys.argv) == 3 and sys.argv[1] == "revoke":
    r = httpx.post(f"{GATEWAY}/admin/users/{sys.argv[2]}/deactivate", headers=headers)
else:
    sys.exit(__doc__)

print(json.dumps(r.json(), indent=2))
