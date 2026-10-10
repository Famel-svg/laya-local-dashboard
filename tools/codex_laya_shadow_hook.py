#!/usr/bin/env python3
"""Observe Laya recommendation vs later MCP tool call; never block or rewrite."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Codex" / "Laya" / "dispatcher_shadow.sqlite3"
SELECTOR = "mcp__laya__laya_select_dependency"


def init() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB, timeout=5) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS decisions ("
            "id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, turn_id TEXT NOT NULL, "
            "tool_use_id TEXT, created_at TEXT NOT NULL, recommendation TEXT, "
            "candidate_ids TEXT NOT NULL, confidence REAL, observed_tool TEXT, "
            "matched INTEGER, observed_at TEXT)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS decisions_pending ON decisions(session_id, turn_id, observed_at)")


def find_decision(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("recommendation"), str) and isinstance(value.get("candidate_ids"), list):
            return value
        for child in value.values():
            found = find_decision(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_decision(child)
            if found:
                return found
    elif isinstance(value, str) and len(value) <= 12000:
        try:
            return find_decision(json.loads(value))
        except (ValueError, TypeError):
            return None
    return None


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    event_name, tool_name = event.get("hook_event_name"), event.get("tool_name")
    session_id, turn_id = event.get("session_id"), event.get("turn_id")
    if not all(isinstance(item, str) and item for item in (event_name, tool_name, session_id, turn_id)):
        return 0
    try:
        init()
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(DB, timeout=5) as conn:
            if event_name == "PostToolUse" and tool_name == SELECTOR:
                decision = find_decision(event.get("tool_response"))
                args = event.get("tool_input") or {}
                raw_candidates = args.get("candidates", []) if isinstance(args, dict) else []
                ids = decision.get("candidate_ids") if decision else None
                if not isinstance(ids, list):
                    ids = [item.get("id") for item in raw_candidates if isinstance(item, dict) and isinstance(item.get("id"), str)]
                confidence = decision.get("confidence") if decision else None
                conn.execute(
                    "INSERT INTO decisions(session_id,turn_id,tool_use_id,created_at,recommendation,candidate_ids,confidence) VALUES(?,?,?,?,?,?,?)",
                    (session_id, turn_id, event.get("tool_use_id"), now,
                     decision.get("recommendation") if decision else None,
                     json.dumps(ids[:5], ensure_ascii=False),
                     confidence if isinstance(confidence, (int, float)) else None),
                )
            elif event_name == "PreToolUse" and tool_name.startswith("mcp__") and tool_name != SELECTOR:
                conn.execute(
                    "UPDATE decisions SET observed_tool=?,matched=(recommendation=?),observed_at=? "
                    "WHERE id=(SELECT id FROM decisions WHERE session_id=? AND turn_id=? AND observed_at IS NULL ORDER BY id DESC LIMIT 1)",
                    (tool_name, tool_name, now, session_id, turn_id),
                )
    except (sqlite3.Error, OSError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
