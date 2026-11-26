#!/usr/bin/env python3
"""Create deterministic stratified Dev and Held-Out manifests from tasks-no-lemma.jsonl.

Strictly locks split boundaries and SHA-256 hashes before any held-out benchmark run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def stratified_sample(
    by_project: dict[str, list[dict[str, Any]]],
    target_n: int,
    rng: random.Random,
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Proportional stratified draw ensuring >=1 item per non-empty project when possible."""
    remaining = {p: list(rows) for p, rows in by_project.items() if rows}
    for rows in remaining.values():
        rng.shuffle(rows)

    total_avail = sum(len(v) for v in remaining.values())
    if target_n > total_avail:
        raise ValueError(f"Requested {target_n} tasks, only {total_avail} available.")

    # Allocate proportional counts with floor >= 1 if target_n >= number of projects
    projects = sorted(remaining.keys())
    alloc: dict[str, int] = {}
    base = 1 if target_n >= len(projects) else 0
    for p in projects:
        alloc[p] = min(len(remaining[p]), base)

    rem_slots = target_n - sum(alloc.values())
    weights = {p: len(remaining[p]) - alloc[p] for p in projects}
    weight_sum = sum(weights.values())

    if rem_slots > 0 and weight_sum > 0:
        exact = {p: (weights[p] / weight_sum) * rem_slots for p in projects}
        for p in projects:
            add = min(weights[p], int(exact[p]))
            alloc[p] += add
        leftover = target_n - sum(alloc.values())
        order = sorted(projects, key=lambda p: (exact[p] - int(exact[p]), p), reverse=True)
        for p in order:
            if leftover <= 0:
                break
            if alloc[p] < len(remaining[p]):
                alloc[p] += 1
                leftover -= 1

    picked: list[dict[str, Any]] = []
    leftover_pool: dict[str, list[dict[str, Any]]] = {}
    for p in projects:
        k = alloc[p]
        picked.extend(remaining[p][:k])
        leftover_pool[p] = remaining[p][k:]

    picked.sort(key=lambda r: (r["project"], r["task_id"]))
    return picked, leftover_pool


def build_manifests(
    jsonl_path: Path,
    out_dir: Path,
    *,
    seed: int = 20261126,
    dev_n: int = 20,
    pilot_n: int = 60,
    exclude_projects: tuple[str, ...] = ("ST",),
) -> dict[str, Any]:
    rows = [json.loads(line) for line in jsonl_path.open("r", encoding="utf-8") if line.strip()]
    source_sha = sha256_file(jsonl_path)

    standalone: dict[str, list[dict[str, Any]]] = defaultdict(list)
    annex: list[dict[str, Any]] = []
    for r in rows:
        meta = {
            "task_id": r["task_id"],
            "project": r["project"],
            "project_name": r["project_name"],
            "target_function": r["target_function"],
            "lines": r["task"].count("\n") + 1,
            "chars": len(r["task"]),
        }
        if r["project"] in exclude_projects:
            annex.append(meta)
        else:
            standalone[r["project"]].append(meta)

    rng = random.Random(seed)
    dev_rows, after_dev = stratified_sample(standalone, dev_n, rng)
    pilot_rows, after_pilot = stratified_sample(after_dev, pilot_n, rng)
    rest_rows = sorted(
        [item for lst in after_pilot.values() for item in lst],
        key=lambda r: (r["project"], r["task_id"]),
    )
    all_heldout = sorted(pilot_rows + rest_rows, key=lambda r: (r["project"], r["task_id"]))

    # Disjointness invariant check
    dev_ids = {r["task_id"] for r in dev_rows}
    pilot_ids = {r["task_id"] for r in pilot_rows}
    rest_ids = {r["task_id"] for r in rest_rows}
    assert dev_ids.isdisjoint(pilot_ids) and dev_ids.isdisjoint(rest_ids) and pilot_ids.isdisjoint(rest_ids)

    out_dir.mkdir(parents=True, exist_ok=True)
    splits = {
        f"dev-{dev_n}.json": ("dev", dev_rows),
        f"heldout-pilot-{pilot_n}.json": ("heldout_pilot", pilot_rows),
        f"heldout-full-{len(all_heldout)}.json": ("heldout_full", all_heldout),
        f"annex-st-{len(annex)}.json": ("annex_requires_deps", sorted(annex, key=lambda r: r["task_id"])),
    }

    summary: dict[str, Any] = {
        "source_jsonl": str(jsonl_path.name),
        "source_sha256": source_sha,
        "seed": seed,
        "excluded_from_standalone": list(exclude_projects),
        "manifests": {},
    }

    for fname, (split_role, items) in splits.items():
        by_proj: dict[str, int] = defaultdict(int)
        for it in items:
            by_proj[it["project"]] += 1
        payload = {
            "split": split_role,
            "seed": seed,
            "source_sha256": source_sha,
            "count": len(items),
            "by_project": dict(sorted(by_proj.items())),
            "tasks": items,
        }
        target_path = out_dir / fname
        target_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        summary["manifests"][fname] = {
            "count": len(items),
            "by_project": payload["by_project"],
            "sha256": sha256_file(target_path),
        }

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate frozen stratified manifests for VeruSAGE-Bench no-lemma.")
    parser.add_argument("--jsonl", type=Path, default=REPO_ROOT / "vendor" / "tasks-no-lemma.jsonl")
    parser.add_argument("--out-dir", type=Path, default=REPO_ROOT / "manifests")
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--dev-n", type=int, default=20)
    parser.add_argument("--pilot-n", type=int, default=60)
    args = parser.parse_args()

    summary = build_manifests(
        args.jsonl,
        args.out_dir,
        seed=args.seed,
        dev_n=args.dev_n,
        pilot_n=args.pilot_n,
    )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
