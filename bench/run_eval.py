#!/usr/bin/env python3
"""Headless A/B benchmark runner for lemma-zero on VeruSAGE-Bench (tasks-no-lemma).

Drives an existing coding agent CLI (`claude`, `codex`, `pi`, `agy`) in either:
  - `control`: raw agent + bare `verus` binary
  - `treatment`: same agent + `lemma-zero` skill (`SKILL.md`) & deterministic `scripts/lz.py`

Safety invariants:
  - Refuses to run with `--budget-cap-usd <= 0` unless `--dry-run` is set.
  - Grades every candidate in an isolated directory with BOTH `anticheat.py` AND fresh `verus`.
  - Appends every attempt (solved, failed, timeout, cheat) to a resumable JSONL ledger.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lemma_zero.anticheat import verify_candidate
from lemma_zero.ledger import BudgetExceededError, ExperimentLedger, TaskRecord, summarize_ledger
from lemma_zero.verus_runner import find_verus_binary, run_verus


def _build_prompt(
    mode: str,
    task_file: Path,
    orig_file: Path,
    target_fn: str,
    verus_bin: Path,
) -> str:
    lz_cmd = f"python3 {REPO_ROOT / 'scripts' / 'lz.py'}"
    if mode == "control":
        return (
            f"Prove the Verus Rust file `{task_file}` so that `{verus_bin} {task_file}` passes with 0 errors.\n"
            f"Target function: `{target_fn}`.\n"
            "Rules: Do NOT add assume, admit, external_body, external attributes, axiom fn, or panic stubs. "
            "Do NOT weaken or alter any original function signatures or requires/ensures clauses. "
            "You may add clean verified helper `proof fn` lemmas. Edit `{task_file}` in place and stop once verified."
        )
    skill_path = REPO_ROOT / "skills" / "lemma-zero" / "SKILL.md"
    return (
        f"Read and strictly follow `{skill_path}`.\n"
        f"Verify `{task_file}` (original snapshot: `{orig_file}`, target function: `{target_fn}`).\n"
        f"Use `LZ=\"{lz_cmd}\"` with `LEMMA_ZERO_VERUS=\"{verus_bin}\"`:\n"
        f"1. Start with `$LZ check {task_file} --original {orig_file} --rlimit 15` and "
        f"`$LZ slice {task_file} --target {target_fn} --depth 2 --max-lines 300`.\n"
        "2. Plan minimal bottom-up helper `proof fn` lemmas and test each via `$LZ verify-lemma`.\n"
        f"3. Edit `{task_file}` in place and finish only when `$LZ check {task_file} --original {orig_file} --rlimit 30` passes."
    )


def _agent_command(agent: str, prompt: str, model: str | None, max_turns: int) -> list[str]:
    if agent == "claude":
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(max_turns),
        ]
        if model:
            cmd.extend(["--model", model])
        return cmd
    if agent == "codex":
        cmd = ["codex", "exec", "--full-auto", prompt]
        if model:
            cmd.extend(["-m", model])
        return cmd
    if agent == "pi":
        cmd = ["pi", "-p", prompt]
        if model:
            cmd.extend(["--model", model])
        return cmd
    raise ValueError(f"Unsupported headless agent '{agent}'. Supported: claude, codex, pi.")


def _extract_claude_usage(stdout: str) -> dict[str, Any]:
    """Parse exact cost and token usage from `claude -p --output-format json` when present."""
    try:
        data = json.loads(stdout)
    except Exception:
        return {"cost_usd": 0.0, "calls": 1, "usage": {}}
    usage = data.get("usage", {}) or {}
    return {
        "cost_usd": float(data.get("total_cost_usd", 0.0) or 0.0),
        "calls": int(data.get("num_turns", 1) or 1),
        "usage": {
            "input_tokens": int(usage.get("input_tokens", 0) or 0),
            "cache_write_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
            "cache_read_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            "output_tokens": int(usage.get("output_tokens", 0) or 0),
        },
    }


def run_single_task(
    task_row: dict[str, Any],
    *,
    mode: str,
    agent: str,
    model: str | None,
    verus_bin: Path,
    task_timeout_sec: int,
    verus_rlimit: int,
    max_turns: int,
    artifacts_dir: Path,
) -> TaskRecord:
    tid = task_row["task_id"]
    proj = task_row["project"]
    orig_src = task_row["task"]

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"lz_{tid[:24]}_") as tmp:
        tmp_dir = Path(tmp)
        orig_file = tmp_dir / "original.rs"
        work_file = tmp_dir / f"{tid}.rs"
        orig_file.write_text(orig_src, encoding="utf-8")
        work_file.write_text(orig_src, encoding="utf-8")

        prompt = _build_prompt(mode, work_file, orig_file, task_row["target_function"], verus_bin)
        cmd = _agent_command(agent, prompt, model, max_turns)

        try:
            proc = subprocess.run(
                cmd,
                cwd=tmp_dir,
                capture_output=True,
                text=True,
                timeout=task_timeout_sec,
                check=False,
            )
            agent_stdout = proc.stdout
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            agent_stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            timed_out = True

        wall = round(time.monotonic() - t0, 2)
        parsed = _extract_claude_usage(agent_stdout) if agent == "claude" else {"cost_usd": 0.0, "calls": 1, "usage": {}}

        cand_src = work_file.read_text(encoding="utf-8")
        ac = verify_candidate(orig_src, cand_src)

        if not ac.clean:
            status = "REJECTED_CHEAT"
            verus_ok = False
            notes = "; ".join(v.code for v in ac.violations)
        else:
            vres = run_verus(work_file, verus_binary=verus_bin, rlimit=verus_rlimit, timeout_seconds=60)
            verus_ok = vres.verified
            if verus_ok:
                status = "SOLVED"
                notes = vres.raw_summary
                sol_path = artifacts_dir / "solutions" / f"{tid}.rs"
                sol_path.parent.mkdir(parents=True, exist_ok=True)
                sol_path.write_text(cand_src, encoding="utf-8")
            elif timed_out or vres.timed_out:
                status = "TIMEOUT_VERUS"
                notes = vres.raw_summary
            else:
                status = "FAILED"
                notes = vres.errors[0].error_class if vres.errors else vres.raw_summary

        u = parsed["usage"]
        return TaskRecord(
            task_id=tid,
            project=proj,
            status=status,
            anticheat_clean=ac.clean,
            verus_verified=verus_ok,
            calls=parsed["calls"],
            total_cost_usd=round(parsed["cost_usd"], 6),
            wall_seconds=wall,
            input_tokens=u.get("input_tokens", 0),
            cache_write_tokens=u.get("cache_write_tokens", 0),
            cache_read_tokens=u.get("cache_read_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            thinking_tokens=0,
            notes=notes,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pre-registered headless A/B evaluation on VeruSAGE-Bench no-lemma.")
    parser.add_argument("--manifest", type=Path, required=True, help="Manifest JSON from bench/prepare_splits.py.")
    parser.add_argument("--jsonl", type=Path, default=REPO_ROOT / "vendor" / "tasks-no-lemma.jsonl")
    parser.add_argument("--mode", choices=("control", "treatment"), required=True)
    parser.add_argument("--agent", choices=("claude", "codex", "pi"), default="claude")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--task-timeout-sec", type=int, default=300)
    parser.add_argument("--verus-rlimit", type=int, default=30)
    parser.add_argument("--budget-cap-usd", type=float, default=0.0, help="Hard USD cap (must be > 0 unless --dry-run).")
    parser.add_argument("--out", type=Path, required=True, help="Output JSONL ledger path (supports resume).")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit on number of tasks.")
    parser.add_argument("--dry-run", action="store_true", help="Validate manifest, Verus binary, and command without spending $0.")
    args = parser.parse_args()

    verus_bin = find_verus_binary()
    if verus_bin is None:
        print("ERROR: Verus binary not found. Install into vendor/verus-arm64-macos/verus first.", file=sys.stderr)
        return 1

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    target_ids = [t["task_id"] for t in manifest["tasks"]]
    if args.limit is not None:
        target_ids = target_ids[: args.limit]

    all_rows = {
        row["task_id"]: row
        for row in (json.loads(line) for line in args.jsonl.open("r", encoding="utf-8") if line.strip())
    }
    selected_rows = [all_rows[tid] for tid in target_ids if tid in all_rows]

    cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
    ledger = ExperimentLedger(args.out, budget_cap_usd=args.budget_cap_usd, pricing_table=cfg["pricing_usd_per_million_tokens"])
    already_done = {r.task_id for r in ExperimentLedger.load_records(args.out)} if args.out.exists() else set()
    pending = [r for r in selected_rows if r["task_id"] not in already_done]

    if args.dry_run:
        sample_prompt = _build_prompt(args.mode, Path("/tmp/task.rs"), Path("/tmp/orig.rs"), pending[0]["target_function"], verus_bin) if pending else ""
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "mode": args.mode,
                    "agent": args.agent,
                    "agent_on_path": shutil.which(args.agent),
                    "verus_binary": str(verus_bin),
                    "manifest_tasks": len(selected_rows),
                    "already_completed": len(already_done),
                    "pending": len(pending),
                    "budget_cap_usd": args.budget_cap_usd,
                    "sample_cmd": shlex.join(_agent_command(args.agent, "<prompt>", args.model, args.max_turns)),
                    "sample_prompt_preview": sample_prompt[:300],
                },
                indent=2,
            )
        )
        return 0

    if args.budget_cap_usd <= 0.0:
        print(
            "REFUSED: --budget-cap-usd must be explicitly > 0.0 (authorized by owner) before executing paid agent runs.",
            file=sys.stderr,
        )
        return 2

    if shutil.which(args.agent) is None:
        print(f"ERROR: Agent CLI '{args.agent}' not found on PATH.", file=sys.stderr)
        return 1

    artifacts_dir = args.out.parent / args.out.stem
    print(f"Starting {args.mode} run on {len(pending)} tasks (agent={args.agent}, workers={args.workers}, cap=${args.budget_cap_usd:.2f})")

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures: dict[concurrent.futures.Future[TaskRecord], str] = {}
        for row in pending:
            try:
                ledger.check_budget(0.0)
            except BudgetExceededError as exc:
                print(f"BUDGET CAP REACHED: {exc}", file=sys.stderr)
                break
            fut = pool.submit(
                run_single_task,
                row,
                mode=args.mode,
                agent=args.agent,
                model=args.model,
                verus_bin=verus_bin,
                task_timeout_sec=args.task_timeout_sec,
                verus_rlimit=args.verus_rlimit,
                max_turns=args.max_turns,
                artifacts_dir=artifacts_dir,
            )
            futures[fut] = row["task_id"]

        for fut in concurrent.futures.as_completed(futures):
            rec = fut.result()
            ledger.record_task(rec)
            print(
                f"[{rec.status:14s}] {rec.project} {rec.task_id[:45]:45s} "
                f"${rec.total_cost_usd:.3f} {rec.wall_seconds:5.1f}s (spent ${ledger.spent_usd:.2f}/${args.budget_cap_usd:.2f})"
            )

    summary = summarize_ledger(ExperimentLedger.load_records(args.out))
    print("\n=== FINAL SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
