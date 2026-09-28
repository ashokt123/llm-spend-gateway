"""Reset state and issue a virtual key to each demo user.

Keys are written to .demo_keys.json (test values, git-ignored) for demo.py and cli.py.
Only a sha256 hash of each key goes into the database.
"""
import json
import secrets

from gateway import db
from gateway.app import hash_key
from gateway.config import ROOT

USERS = [
    # user,              team,          role
    ("alice@acme.com",   "marketing",   "pilot"),
    ("bob@acme.com",     "engineering", "standard"),
    ("ci-bot@acme.com",  "engineering", "service"),
    ("admin@acme.com",   "platform",    "admin"),
]

for path in (ROOT / "gateway.db", ROOT / "audit.jsonl"):
    path.unlink(missing_ok=True)

keys = {}
for user, team, role in USERS:
    key = "gw_" + secrets.token_urlsafe(24)
    db.create_key(hash_key(key), key[:10], user, team, role)
    keys[user] = key
    print(f"{user:<18} {team:<12} {role:<9} {key[:10]}…")

(ROOT / ".demo_keys.json").write_text(json.dumps(keys, indent=2))
print(f"\n{len(keys)} keys issued; plaintext saved to .demo_keys.json, only hashes stored in gateway.db")
