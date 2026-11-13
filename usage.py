"""
Per-user daily caps + shared daily Neuron budget.

Why caps exist: the Cloudflare free quota is 10,000 Neurons/day,
SHARED across all bot users, resetting 00:00 UTC. Without caps,
one user could drain the whole day in ~173 pictures.

Defaults from the research recommendation:
- 5 text-to-image + 3 edits per user per UTC day.
- Global estimate stops new generations near the 10,000 cap.

Stored in a small SQLite file (usage.db) next to the bot.
No Telegram user data besides user id, date, and counts is kept.
"""

import os
import sqlite3
from datetime import datetime, timezone

DAILY_NEURON_BUDGET = 10_000.0


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class UsageStore:
    def __init__(self, path: str = "usage.db"):
        self.path = path
        self._init()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS daily_usage ("
                " user_id INTEGER, day TEXT, txt INT DEFAULT 0, edits INT DEFAULT 0,"
                " PRIMARY KEY (user_id, day))"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS neuron_usage ("
                " day TEXT PRIMARY KEY, neurons REAL DEFAULT 0)"
            )

    def check(self, user_id: int, kind: str,
              txt_limit: int, edit_limit: int) -> tuple[bool, str, int, int]:
        """
        Check per-user caps WITHOUT counting anything.
        kind is 'txt' or 'edit'.
        Returns (allowed, reason, used_txt_today, used_edits_today).
        Call count() only AFTER a picture/edit is actually delivered -
        a failed attempt must not eat the user's daily allowance.

        NOTE: the shared Neuron budget is deliberately NOT a blocker
        here. When Cloudflare's 10,000 Neurons are spent, the
        provider chain falls back to Pollinations / AI Horde, which
        do not use Neurons. The budget is still tracked (add_neurons)
        and shown in /status.
        """
        day = today_utc()
        with self._connect() as con:
            row = con.execute(
                "SELECT txt, edits FROM daily_usage WHERE user_id=? AND day=?",
                (user_id, day),
            ).fetchone()
            txt_used, edits_used = row if row else (0, 0)

            if kind == "txt" and txt_used >= txt_limit:
                return False, f"You used your {txt_limit} free pictures for today. Try again after 00:00 UTC.", txt_used, edits_used
            if kind == "edit" and edits_used >= edit_limit:
                return False, f"You used your {edit_limit} free edits for today. Try again after 00:00 UTC.", txt_used, edits_used
            return True, "", txt_used, edits_used

    def count(self, user_id: int, kind: str) -> tuple[int, int]:
        """Record one DELIVERED picture/edit. Returns (txt_used, edits_used)."""
        day = today_utc()
        with self._connect() as con:
            row = con.execute(
                "SELECT txt, edits FROM daily_usage WHERE user_id=? AND day=?",
                (user_id, day),
            ).fetchone()
            txt_used, edits_used = row if row else (0, 0)
            if kind == "txt":
                txt_used += 1
            else:
                edits_used += 1
            con.execute(
                "INSERT INTO daily_usage (user_id, day, txt, edits) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, day) DO UPDATE SET txt=excluded.txt, edits=excluded.edits",
                (user_id, day, txt_used, edits_used),
            )
            return txt_used, edits_used

    def add_neurons(self, neurons_cost: float) -> None:
        day = today_utc()
        with self._connect() as con:
            con.execute(
                "INSERT INTO neuron_usage (day, neurons) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET neurons = neurons + excluded.neurons",
                (day, neurons_cost),
            )

    def status(self, user_id: int, txt_limit: int, edit_limit: int) -> dict:
        day = today_utc()
        with self._connect() as con:
            row = con.execute(
                "SELECT txt, edits FROM daily_usage WHERE user_id=? AND day=?",
                (user_id, day),
            ).fetchone()
            txt_used, edits_used = row if row else (0, 0)
            nrow = con.execute(
                "SELECT neurons FROM neuron_usage WHERE day=?", (day,)
            ).fetchone()
            neurons_used = nrow[0] if nrow else 0.0
        return {
            "day": day,
            "txt_used": txt_used,
            "txt_limit": txt_limit,
            "edits_used": edits_used,
            "edit_limit": edit_limit,
            "neurons_used": round(neurons_used, 1),
            "neurons_budget": DAILY_NEURON_BUDGET,
            "neurons_left": round(max(0.0, DAILY_NEURON_BUDGET - neurons_used), 1),
        }
