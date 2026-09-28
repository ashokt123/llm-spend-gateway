"""SQLite storage: virtual keys, metered usage, and which alerts have fired."""
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .config import ROOT

DB_PATH = os.environ.get("GATEWAY_DB", str(ROOT / "gateway.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT PRIMARY KEY,   -- sha256 of the key; the key itself is never stored
    key_prefix TEXT NOT NULL,      -- first chars, safe to show in logs and UIs
    user       TEXT NOT NULL,
    team       TEXT NOT NULL,
    role       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    period     TEXT NOT NULL,      -- UTC date the spend counts against
    request_id TEXT NOT NULL,
    user       TEXT NOT NULL,
    team       TEXT NOT NULL,
    model      TEXT NOT NULL,
    in_tok     INTEGER NOT NULL,
    out_tok    INTEGER NOT NULL,
    cost_usd   REAL NOT NULL,
    latency_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_team_period ON usage(team, period);
CREATE TABLE IF NOT EXISTS alerts_sent (
    team      TEXT NOT NULL,
    period    TEXT NOT NULL,
    threshold REAL NOT NULL,
    PRIMARY KEY (team, period, threshold)
);
"""

_conn: Optional[sqlite3.Connection] = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_key(key_hash: str, key_prefix: str, user: str, team: str, role: str) -> None:
    conn().execute(
        "INSERT INTO api_keys (key_hash, key_prefix, user, team, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (key_hash, key_prefix, user, team, role, now_iso()),
    )


def lookup_key(key_hash: str) -> Optional[sqlite3.Row]:
    return conn().execute("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)).fetchone()


def deactivate_user(user: str) -> int:
    return conn().execute("UPDATE api_keys SET active = 0 WHERE user = ? AND active = 1", (user,)).rowcount


def team_spend(team: str, period: str) -> float:
    row = conn().execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage WHERE team = ? AND period = ?", (team, period)
    ).fetchone()
    return row[0]


def record_usage(**row) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn().execute(f"INSERT INTO usage ({cols}) VALUES ({marks})", tuple(row.values()))


def mark_alert(team: str, period: str, threshold: float) -> bool:
    """Record that an alert fired. Returns False if it already fired this period."""
    cur = conn().execute(
        "INSERT OR IGNORE INTO alerts_sent (team, period, threshold) VALUES (?, ?, ?)", (team, period, threshold)
    )
    return cur.rowcount == 1


def usage_report(period: str) -> dict:
    c = conn()

    def rows(group_by: str):
        return [
            dict(r)
            for r in c.execute(
                f"""SELECT {group_by}, COUNT(*) AS requests, SUM(in_tok + out_tok) AS tokens,
                           ROUND(SUM(cost_usd), 4) AS cost_usd
                    FROM usage WHERE period = ? GROUP BY {group_by} ORDER BY cost_usd DESC""",
                (period,),
            )
        ]

    return {"teams": rows("team"), "users": rows("user, team"), "models": rows("model")}
