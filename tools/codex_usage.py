#!/usr/bin/env python3
"""Read token metadata from Codex rollouts without parsing prompt content."""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

FIELDS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


def read_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as stream:
            first = json.loads(stream.readline())
        if first.get("type") != "session_meta":
            return None
        return first.get("payload") or {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def discover(root: Path) -> tuple[dict[str, Path], dict[str, str | None]]:
    files: dict[str, Path] = {}
    parents: dict[str, str | None] = {}
    for path in root.rglob("rollout-*.jsonl"):
        meta = read_meta(path)
        if not meta:
            continue
        session = meta.get("id") or meta.get("session_id")
        if session:
            files[str(session)] = path
            parents[str(session)] = meta.get("parent_thread_id")
    return files, parents


def family(root_id: str, parents: dict[str, str | None]) -> set[str]:
    members = {root_id}
    changed = True
    while changed:
        changed = False
        for session, parent in parents.items():
            if parent in members and session not in members:
                members.add(session)
                changed = True
    return members


def read_usage(path: Path) -> dict[tuple[str, str], dict[str, int]]:
    # Exact per-response records are additive; token_count totals are cumulative snapshots.
    records: dict[tuple[str, str], dict[str, int]] = {}
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # active rollout may end with a partial JSONL line
                if row.get("type") != "token_usage_record":
                    continue
                payload = row.get("payload") or {}
                usage = payload.get("usage") or {}
                thread = payload.get("thread_id")
                response = payload.get("response_id")
                if not thread or not response:
                    continue  # avoid unsafe fallback/double-counting
                key = (str(thread), str(response))
                current = {name: int(usage.get(name) or 0) for name in FIELDS}
                if not current["total_tokens"]:
                    current["total_tokens"] = current["input_tokens"] + current["output_tokens"]
                # Duplicate copies should have identical usage; keep largest snapshot defensively.
                previous = records.get(key)
                if previous is None or current["total_tokens"] > previous["total_tokens"]:
                    records[key] = current
                elif current == previous:
                    continue
                else:
                    # Conflicting duplicate is ambiguous: retain larger record, do not sum.
                    records[key] = max((previous, current), key=lambda item: item["total_tokens"])
    except (OSError, UnicodeError):
        return records
    return records


def read_tool_names(path: Path) -> dict[str, int]:
    """Count rollout tool names only; do not retain or print inputs/results."""
    counts: dict[str, int] = defaultdict(int)
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = row.get("payload") or {}
                if row.get("type") == "response_item" and payload.get("type") == "custom_tool_call":
                    name = payload.get("name")
                    if isinstance(name, str):
                        counts[name] += 1
    except (OSError, UnicodeError):
        pass
    return dict(counts)


def sum_records(session_ids: set[str], files: dict[str, Path]) -> dict[str, int]:
    unique: dict[tuple[str, str], dict[str, int]] = {}
    for session in sorted(session_ids):
        path = files.get(session)
        if path:
            unique.update(read_usage(path))
    totals = {name: sum(item[name] for item in unique.values()) for name in FIELDS}
    totals["uncached_input_tokens"] = max(0, totals["input_tokens"] - totals["cached_input_tokens"])
    totals["responses"] = len(unique)
    totals["sessions"] = len(session_ids)
    calls: dict[str, int] = defaultdict(int)
    for session in session_ids:
        path = files.get(session)
        if path:
            for name, count in read_tool_names(path).items():
                calls[name] += count
    totals["tool_calls_by_name"] = dict(sorted(calls.items()))
    return totals


def read_manifest(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"pair_id", "group", "thread_id"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("manifest requires pair_id,group,thread_id columns")
        rows = list(reader)
    for row in rows:
        if row.get("group") not in {"control", "treatment"}:
            raise ValueError("group must be control or treatment")
        if not row.get("pair_id") or not row.get("thread_id"):
            raise ValueError("pair_id and thread_id cannot be empty")
    return rows


def compare(rows: list[dict[str, str]], files, parents) -> dict[str, Any]:
    pairs: dict[str, dict[str, list[dict[str, int]]]] = defaultdict(lambda: {"control": [], "treatment": []})
    for row in rows:
        totals = sum_records(family(row["thread_id"], parents), files)
        pairs[row["pair_id"]][row["group"]].append(totals)
    deltas = []
    for pair_id, groups in pairs.items():
        if not groups["control"] or not groups["treatment"]:
            continue
        control = sum(item["total_tokens"] for item in groups["control"])
        treatment = sum(item["total_tokens"] for item in groups["treatment"])
        deltas.append({
            "pair_id": pair_id, "control_total_tokens": control,
            "treatment_total_tokens": treatment, "saved_tokens": control - treatment,
            "saved_percent": (100 * (control - treatment) / control) if control else None,
            "control_uncached_input_tokens": sum(item["uncached_input_tokens"] for item in groups["control"]),
            "treatment_uncached_input_tokens": sum(item["uncached_input_tokens"] for item in groups["treatment"]),
            "control_output_tokens": sum(item["output_tokens"] for item in groups["control"]),
            "treatment_output_tokens": sum(item["output_tokens"] for item in groups["treatment"]),
        })
    return {
        "method": "paired task totals from per-response Codex token_usage_record usage",
        "matched_pairs": len(deltas), "pairs": deltas,
        "mean_saved_tokens": statistics.mean(x["saved_tokens"] for x in deltas) if deltas else None,
        "median_saved_percent": statistics.median(x["saved_percent"] for x in deltas if x["saved_percent"] is not None) if deltas else None,
        "warning": "Observed differences measure real recorded tokens, but do not prove causality unless paired tasks, model, context, and outcome are comparable. Cached input is a subset of input.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions-root", type=Path)
    parser.add_argument("--thread-id", help="Codex thread id; includes child agents")
    parser.add_argument("--manifest", type=Path, help="CSV: pair_id,group,thread_id")
    args = parser.parse_args()
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    root = args.sessions_root or codex_home / "sessions"
    if not root.is_dir():
        parser.error(f"sessions directory not found: {root}")
    files, parents = discover(root)
    if args.manifest:
        result = compare(read_manifest(args.manifest), files, parents)
    elif args.thread_id:
        result = {
            "thread_id": args.thread_id, "usage_source": "Codex per-response token_usage_record",
            "totals": sum_records(family(args.thread_id, parents), files),
            "cached_input_is_subset_of_input": True,
        }
    else:
        parser.error("provide --thread-id or --manifest")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
