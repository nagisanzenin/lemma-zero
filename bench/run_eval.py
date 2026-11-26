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
import os
import platform
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
from lemma_zero.ledger import BudgetExceededError, CallUsage, ExperimentLedger, TaskRecord, summarize_ledger
from lemma_zero.slicer import find_target_item, index_verus_items
from lemma_zero.verus_runner import find_verus_binary, run_verus


def resolve_agent_binary(agent: str) -> str | None:
    found = shutil.which(agent)
    if found:
        return found
    if agent == "agy":
        fallback = Path.home() / ".gemini" / "bin" / "agy"
        if fallback.is_file():
            return str(fallback)
    return None


def _sbpl_quote(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def build_macos_sandbox_profile(
    mode: str,
    own_tmp_dir: Path,
    extra_read_paths: tuple[Path, ...] = (),
) -> str:
    """Build an Apple Seatbelt profile preventing agents from reading runs/, datasets, or sibling tasks."""
    repo = REPO_ROOT.resolve()
    home = Path.home().resolve()
    own_tmp = own_tmp_dir.resolve()
    tmp_parent = own_tmp.parent.resolve()

    deny_reads: list[str] = [
        f'(subpath "{_sbpl_quote(repo / "runs")}")',
        f'(subpath "{_sbpl_quote(repo / "manifests")}")',
        f'(subpath "{_sbpl_quote(repo / ".git")}")',
        f'(subpath "{_sbpl_quote(repo / "bench")}")',
        f'(subpath "{_sbpl_quote(repo / "docs")}")',
        f'(subpath "{_sbpl_quote(repo / "tests")}")',
        f'(literal "{_sbpl_quote(repo / "vendor" / "tasks-no-lemma.jsonl")}")',
        f'(subpath "{_sbpl_quote(home / ".gemini" / "antigravity" / "brain")}")',
        f'(subpath "{_sbpl_quote(home / ".Trash")}")',
        f'(literal "{_sbpl_quote(home / ".env")}")',
        f'(literal "{_sbpl_quote(repo / ".env")}")',
    ]
    if mode == "control":
        deny_reads.extend(
            [
                f'(subpath "{_sbpl_quote(repo / "skills")}")',
                f'(subpath "{_sbpl_quote(repo / "scripts")}")',
                f'(subpath "{_sbpl_quote(repo / "src")}")',
                f'(subpath "{_sbpl_quote(repo / "config")}")',
            ]
        )

    # Block reading any sibling benchmark temp dir (`lz_*`) while explicitly allowing own_tmp and extra_read_paths.
    sibling_regex = f"^{_sbpl_quote(tmp_parent)}/lz_"
    deny_block = "\n    ".join(deny_reads)
    extra_allows = "".join(f'(allow file-read* (literal "{_sbpl_quote(p.resolve())}"))\n' for p in extra_read_paths)
    return (
        "(version 1)\n"
        "(allow default)\n"
        f'(deny file-write* (subpath "{_sbpl_quote(repo)}"))\n'
        "(deny file-read*\n"
        f"    {deny_block}\n"
        f'    (regex #"{sibling_regex}")\n'
        ")\n"
        f'(allow file-read* (subpath "{_sbpl_quote(own_tmp)}"))\n'
        f"{extra_allows}"
    )


def wrap_with_sandbox(
    cmd: list[str],
    *,
    mode: str,
    own_tmp_dir: Path,
    extra_read_paths: tuple[Path, ...] = (),
    enabled: bool = True,
) -> list[str]:
    """Wrap agent command in OS-level sandbox-exec on macOS to prevent benchmark filesystem leakage."""
    if not enabled or platform.system() != "Darwin":
        return cmd
    sb_bin = shutil.which("sandbox-exec") or ("/usr/bin/sandbox-exec" if Path("/usr/bin/sandbox-exec").is_file() else None)
    if sb_bin is None:
        return cmd
    return [sb_bin, "-p", build_macos_sandbox_profile(mode, own_tmp_dir, extra_read_paths=extra_read_paths), *cmd]


def _load_dotenv_env() -> dict[str, str]:
    """Load key=value pairs from ~/.env and repo .env into subprocess env (never logged)."""
    env = dict(os.environ)
    for candidate in (Path.home() / ".env", REPO_ROOT / ".env"):
        if not candidate.is_file():
            continue
        for raw_line in candidate.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in env:
                env[k] = v
    return env


def _build_prompt(
    mode: str,
    task_file: Path,
    orig_file: Path,
    target_fn: str,
    verus_bin: Path,
    line_count: int = 0,
    is_exec_fn: bool = False,
) -> str:
    lz_cmd = f"python3 {REPO_ROOT / 'scripts' / 'lz.py'}"
    if mode == "control":
        return (
            f"Prove the Verus Rust file `{task_file}` so that `{verus_bin} {task_file}` passes with 0 errors.\n"
            f"Target function: `{target_fn}`.\n"
            "Rules: Do NOT add assume, admit, external_body, external attributes, axiom fn, or panic stubs. "
            "Do NOT weaken or alter any original function signatures or requires/ensures clauses. "
            "Work strictly inside this temporary directory; do NOT search external directories or prior runs. "
            "You may add clean verified helper `proof fn` lemmas. Edit `{task_file}` in place and stop once verified. "
            "Reply concisely in English with final status."
        )
    lz_py = REPO_ROOT / "scripts" / "lz.py"
    lz_prefix = f"LEMMA_ZERO_VERUS={shlex.quote(str(verus_bin))} python3 {shlex.quote(str(lz_py))}"
    synth_step = (
        "2. Target is an `exec fn`: keep proofs concise and inline using `proof { ... }` blocks and existing file lemmas "
        "(avoid unnecessary standalone helper `proof fn`s unless a pure math identity needs one).\n"
        if is_exec_fn
        else "2. Target is a `proof fn`: identify the mathematical object (`=~=`, induction, triggers, `by (nonlinear_arith)`/`by (bit_vector)`, `reveal`), "
        "then write all needed clean helper `proof fn`s + the target body in one batched edit using scoped `assert(...) by { ... }` blocks.\n"
    )
    return (
        f"Verify `{task_file}` (target function: `{target_fn}`).\n"
        "Rules: zero cheats (no assume/admit/external_body/external/assume_specification/axiom/panic); never alter original signatures, "
        "requires/ensures, structs/consts, or pre-existing spec fn bodies (including uninterpreted external_body specs). "
        "Work strictly on `{task_file}`; do not search external directories.\n"
        f"1. Read `{task_file}` directly (for huge files you may also jump to the target cone via `{lz_prefix} slice {task_file} --target {target_fn}`) "
        f"and run `{lz_prefix} check {task_file} --original {orig_file} --target {target_fn} --rlimit 15 --expand-errors`.\n"
        f"   Need a vstd lemma? Use `{lz_prefix} vstd \"<keywords>\"` (1 instant call instead of grepping `vstd/`).\n"
        f"{synth_step}"
        f"3. Verify with `{lz_prefix} check {task_file} --original {orig_file} --target {target_fn} --rlimit 30` "
        f"(use `--fn {target_fn} --expand-errors --rlimit 20 --timeout 25`, `verify-lemma --fn <helper>`, or `#[verifier::spinoff_prover]` ONLY if slow or `RLIMIT_TIMEOUT`).\n"
        "Edit in place and stop immediately once full-file `check --rlimit 30` prints `VERIFIED` and `anticheat=CLEAN`. "
        "Reply concisely in English with final status."
    )


def _agent_command(
    agent: str,
    agent_bin: str,
    prompt: str,
    model: str | None,
    max_turns: int,
    *,
    mode: str,
    task_timeout_sec: int,
    thinking: str | None = None,
) -> list[str]:
    if agent == "agy":
        cmd = [
            agent_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--dangerously-skip-permissions",
            "--print-timeout",
            f"{task_timeout_sec}s",
        ]
        if mode == "control":
            cmd.append("--disable-slash-commands")
        else:
            cmd.extend(["--add-dir", str(REPO_ROOT)])
        if model:
            cmd.extend(["--model", model])
        return cmd
    if agent == "claude":
        cmd = [
            agent_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--permission-mode",
            "bypassPermissions",
            "--max-turns",
            str(max_turns),
            "--setting-sources",
            "",
        ]
        if mode == "control":
            cmd.append("--disable-slash-commands")
        else:
            cmd.extend(["--add-dir", str(REPO_ROOT)])
        if model:
            cmd.extend(["--model", model])
        return cmd
    if agent == "codex":
        cmd = [agent_bin, "exec", "--full-auto", prompt]
        if model:
            cmd.extend(["-m", model])
        return cmd
    if agent == "pi":
        cmd = [
            agent_bin,
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
            "--no-prompt-templates",
            "--no-themes",
            "-p",
        ]
        if model:
            cmd.extend(["--model", model])
        if thinking:
            cmd.extend(["--thinking", thinking])
        cmd.append(prompt)
        return cmd
    raise ValueError(f"Unsupported headless agent '{agent}'. Supported: agy, claude, codex, pi.")


def _parse_last_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return None


def _extract_turn_count(data: dict[str, Any]) -> int:
    for key in ("num_turns", "turns", "turn_count", "steps", "iterations"):
        val = data.get(key)
        if isinstance(val, int) and val > 0:
            return val
    stats = data.get("stats") or data.get("metadata") or {}
    if isinstance(stats, dict):
        for key in ("num_turns", "turns", "turn_count", "steps", "tool_calls"):
            val = stats.get(key)
            if isinstance(val, int) and val > 0:
                return val
    msgs = data.get("messages")
    if isinstance(msgs, list) and msgs:
        return sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "assistant") or 1
    return 1


def _parse_pi_ndjson(
    stdout: str,
    pricing_table: dict[str, dict[str, float]],
    pricing_tier: str,
) -> dict[str, Any]:
    """Aggregate exact assistant turns, token usage, tool calls, and cost from `pi --mode json` NDJSON."""
    calls = 0
    in_tok = 0
    cache_w = 0
    cache_r = 0
    out_non_think = 0
    think_tok = 0
    pi_reported_cost = 0.0
    tool_calls: list[str] = []
    final_text = ""
    error_msg = ""
    model_seen = ""

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if not isinstance(ev, dict):
            continue
        ev_type = ev.get("type")
        if ev_type == "tool_execution_start":
            tname = ev.get("toolName")
            if tname:
                args = ev.get("args") or {}
                preview = ""
                if isinstance(args, dict):
                    cmd_str = args.get("command") or args.get("path") or ""
                    if cmd_str:
                        preview = ":" + str(cmd_str).replace("\n", " ")[:110]
                tool_calls.append(f"{tname}{preview}")
        elif ev_type == "message_end":
            msg = ev.get("message") or {}
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            calls += 1
            if msg.get("model"):
                model_seen = str(msg.get("model"))
            if msg.get("errorMessage"):
                error_msg = str(msg.get("errorMessage"))
            usage = msg.get("usage") or {}
            if isinstance(usage, dict):
                in_tok += int(usage.get("input", 0) or 0)
                cache_w += int(usage.get("cacheWrite", 0) or 0)
                cache_r += int(usage.get("cacheRead", 0) or 0)
                total_out = int(usage.get("output", 0) or 0)
                # In pi Usage, reasoning is a subset of output; split cleanly to prevent double charging.
                reasoning = int(usage.get("reasoning", usage.get("thinking", 0)) or 0)
                think_tok += reasoning
                out_non_think += max(0, total_out - reasoning)
                cost_obj = usage.get("cost")
                if isinstance(cost_obj, dict):
                    pi_reported_cost += float(cost_obj.get("total", 0.0) or 0.0)
            content = msg.get("content")
            if isinstance(content, list):
                texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                joined = "\n".join(t for t in texts if t).strip()
                if joined:
                    final_text = joined

    est_cost = CallUsage(
        tier=pricing_tier,
        model=model_seen or "pi",
        input_tokens=in_tok,
        cache_write_tokens=cache_w,
        cache_read_tokens=cache_r,
        output_tokens=out_non_think,
        thinking_tokens=think_tok,
    ).compute_cost_usd(pricing_table)

    return {
        "cost_usd": est_cost,
        "calls": calls or 1,
        "usage": {
            "input_tokens": in_tok,
            "cache_write_tokens": cache_w,
            "cache_read_tokens": cache_r,
            "output_tokens": out_non_think,
            "thinking_tokens": think_tok,
        },
        "raw": {
            "agent": "pi",
            "model": model_seen,
            "num_turns": calls,
            "tool_calls": tool_calls,
            "pi_catalog_cost_usd": round(pi_reported_cost, 6),
            "pricing_tier": pricing_tier,
            "pricing_tier_cost_usd": est_cost,
            "error": error_msg,
            "response": final_text,
            "usage": {
                "input_tokens": in_tok,
                "cache_write_tokens": cache_w,
                "cache_read_tokens": cache_r,
                "output_tokens": out_non_think,
                "thinking_tokens": think_tok,
            },
        },
    }


def extract_agent_usage(
    agent: str,
    stdout: str,
    pricing_table: dict[str, dict[str, float]],
    pricing_tier: str = "mid_balanced",
) -> dict[str, Any]:
    """Parse exact turns, token usage, and USD cost across supported headless agents."""
    if agent == "pi":
        return _parse_pi_ndjson(stdout, pricing_table, pricing_tier)

    data = _parse_last_json_object(stdout)
    if data is None:
        return {"cost_usd": 0.0, "calls": 1, "usage": {}, "raw": None}

    usage = data.get("usage", {}) or {}
    calls = _extract_turn_count(data)

    if agent == "agy":
        # agy reports uncached input_tokens separately from cache_read_tokens
        # (observed cache_read_tokens > input_tokens across runs), so do NOT subtract cache_r.
        in_tok = int(usage.get("input_tokens", 0) or 0)
        cache_w = int(usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens", 0)) or 0)
        cache_r = int(usage.get("cache_read_tokens", usage.get("cache_read_input_tokens", 0)) or 0)
        out_tok = int(usage.get("output_tokens", 0) or 0)
        think_tok = int(usage.get("thinking_tokens", 0) or 0)
        est_cost = CallUsage(
            tier=pricing_tier,
            model="agy",
            input_tokens=in_tok,
            cache_write_tokens=cache_w,
            cache_read_tokens=cache_r,
            output_tokens=out_tok,
            thinking_tokens=think_tok,
        ).compute_cost_usd(pricing_table)
        return {
            "cost_usd": float(data.get("total_cost_usd") or est_cost),
            "calls": calls,
            "usage": {
                "input_tokens": in_tok,
                "cache_write_tokens": cache_w,
                "cache_read_tokens": cache_r,
                "output_tokens": out_tok,
                "thinking_tokens": think_tok,
            },
            "raw": data,
        }

    in_tok = int(usage.get("input_tokens", 0) or 0)
    cache_w = int(usage.get("cache_creation_input_tokens", 0) or 0)
    cache_r = int(usage.get("cache_read_input_tokens", 0) or 0)
    out_tok = int(usage.get("output_tokens", 0) or 0)
    think_tok = int(usage.get("thinking_tokens", 0) or 0)
    reported_cost = float(data.get("total_cost_usd", 0.0) or 0.0)
    est_cost = CallUsage(
        tier=pricing_tier,
        model=agent,
        input_tokens=in_tok,
        cache_write_tokens=cache_w,
        cache_read_tokens=cache_r,
        output_tokens=out_tok,
        thinking_tokens=think_tok,
    ).compute_cost_usd(pricing_table)
    return {
        "cost_usd": reported_cost if reported_cost > 0 else est_cost,
        "calls": calls,
        "usage": {
            "input_tokens": in_tok,
            "cache_write_tokens": cache_w,
            "cache_read_tokens": cache_r,
            "output_tokens": out_tok,
            "thinking_tokens": think_tok,
        },
        "raw": data,
    }


def _is_target_exec_fn(source: str, target_fn: str) -> bool:
    items = index_verus_items(source)
    selectable = [it for it in items if it.kind != "impl"]
    tgt = find_target_item(selectable, target_function=target_fn)
    if tgt is None:
        return False
    return ("proof fn" not in tgt.signature) and ("spec fn" not in tgt.signature)


def run_single_task(
    task_row: dict[str, Any],
    *,
    mode: str,
    agent: str,
    agent_bin: str,
    model: str | None,
    verus_bin: Path,
    task_timeout_sec: int,
    verus_rlimit: int,
    max_turns: int,
    artifacts_dir: Path,
    pricing_table: dict[str, dict[str, float]],
    pricing_tier: str = "mid_balanced",
    thinking: str | None = None,
) -> TaskRecord:
    tid = task_row["task_id"]
    proj = task_row["project"]
    orig_src = task_row["task"]
    line_count = orig_src.count("\n") + 1
    is_exec_fn = _is_target_exec_fn(orig_src, task_row["target_function"])

    t0 = time.monotonic()
    with (
        tempfile.TemporaryDirectory(prefix=f"lz_{tid[:24]}_") as tmp,
        tempfile.TemporaryDirectory(prefix=f"lz_orig_{tid[:18]}_") as orig_tmp,
    ):
        tmp_dir = Path(tmp)
        orig_file = Path(orig_tmp) / "snapshot.rs"
        work_file = tmp_dir / f"{tid}.rs"
        orig_file.write_text(orig_src, encoding="utf-8")
        work_file.write_text(orig_src, encoding="utf-8")

        prompt = _build_prompt(
            mode,
            work_file,
            orig_file,
            task_row["target_function"],
            verus_bin,
            line_count=line_count,
            is_exec_fn=is_exec_fn,
        )
        cmd = _agent_command(
            agent,
            agent_bin,
            prompt,
            model,
            max_turns,
            mode=mode,
            task_timeout_sec=task_timeout_sec,
            thinking=thinking,
        )
        extra_reads = (orig_file,) if mode == "treatment" else ()
        cmd = wrap_with_sandbox(cmd, mode=mode, own_tmp_dir=tmp_dir, extra_read_paths=extra_reads, enabled=True)
        sub_env = _load_dotenv_env()

        try:
            proc = subprocess.run(
                cmd,
                cwd=tmp_dir,
                env=sub_env,
                capture_output=True,
                text=True,
                timeout=task_timeout_sec + 20,
                check=False,
            )
            agent_stdout = proc.stdout
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            agent_stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            timed_out = True

        wall = round(time.monotonic() - t0, 2)
        parsed = extract_agent_usage(agent, agent_stdout, pricing_table, pricing_tier=pricing_tier)

        # Persist raw structured agent metadata for full post-hoc auditability.
        raw_meta = parsed.get("raw")
        if raw_meta is not None:
            log_path = artifacts_dir / "logs" / f"{tid}.json"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(raw_meta, indent=2), encoding="utf-8")

        raw_err = str((raw_meta or {}).get("error", "")).lower()
        agent_timed_out = timed_out or ("timeout" in raw_err) or ("timed out" in raw_err)

        cand_src = work_file.read_text(encoding="utf-8")
        ac = verify_candidate(orig_src, cand_src, target_function=task_row["target_function"])

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
            elif vres.timed_out:
                status = "TIMEOUT_VERUS"
                notes = f"agent_timeout={agent_timed_out}; {vres.raw_summary}"
            elif agent_timed_out:
                status = "TIMEOUT_AGENT"
                err_cls = vres.errors[0].error_class if vres.errors else vres.raw_summary
                notes = f"agent_timeout=True; last_verus={err_cls}"
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
            thinking_tokens=u.get("thinking_tokens", 0),
            notes=notes,
        )


def _select_stratified(tasks: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    by_proj: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        by_proj.setdefault(t["project"], []).append(t)
    selected: list[dict[str, Any]] = []
    projects = sorted(by_proj.keys())
    idx = 0
    while len(selected) < limit and any(by_proj.values()):
        p = projects[idx % len(projects)]
        if by_proj[p]:
            selected.append(by_proj[p].pop(0))
        idx += 1
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pre-registered headless A/B evaluation on VeruSAGE-Bench no-lemma.")
    parser.add_argument("--manifest", type=Path, required=True, help="Manifest JSON from bench/prepare_splits.py.")
    parser.add_argument("--jsonl", type=Path, default=REPO_ROOT / "vendor" / "tasks-no-lemma.jsonl")
    parser.add_argument("--mode", choices=("control", "treatment"), required=True)
    parser.add_argument("--agent", choices=("agy", "claude", "codex", "pi"), default="agy")
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--thinking", type=str, default=None, help="Optional thinking level for pi (off|low|medium|high|max).")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-turns", type=int, default=16)
    parser.add_argument("--task-timeout-sec", type=int, default=180)
    parser.add_argument("--verus-rlimit", type=int, default=30)
    parser.add_argument("--budget-cap-usd", type=float, default=0.0, help="Hard USD cap (must be > 0 unless --dry-run).")
    parser.add_argument("--pricing-tier", type=str, default="mid_balanced", help="Pricing tier in config/experiment.json for list-price estimates.")
    parser.add_argument("--out", type=Path, required=True, help="Output JSONL ledger path (supports resume).")
    parser.add_argument("--limit", type=int, default=None, help="Optional limit on number of tasks.")
    parser.add_argument("--stratified", action="store_true", help="When --limit is set, draw round-robin across projects.")
    parser.add_argument("--hardest", action="store_true", help="Sort manifest tasks by descending source line count before applying --limit.")
    parser.add_argument(
        "--exclude-ledger",
        type=Path,
        action="append",
        default=[],
        help="Optional existing JSONL ledger path(s) whose task_ids should be excluded.",
    )
    parser.add_argument(
        "--task-id",
        dest="task_ids",
        type=str,
        action="append",
        default=[],
        help="Optional explicit task_id filter (repeatable; must belong to --manifest).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate manifest, Verus binary, and command without spending $0.")
    args = parser.parse_args()

    verus_bin = find_verus_binary()
    if verus_bin is None:
        print("ERROR: Verus binary not found. Install into vendor/verus-arm64-macos/verus first.", file=sys.stderr)
        return 1

    agent_bin = resolve_agent_binary(args.agent)
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    task_entries = list(manifest["tasks"])
    if args.task_ids:
        wanted = set(args.task_ids)
        task_entries = [t for t in task_entries if t["task_id"] in wanted]
    excluded_ids: set[str] = set()
    for ex_path in args.exclude_ledger:
        if ex_path.exists():
            excluded_ids.update(r.task_id for r in ExperimentLedger.load_records(ex_path))
    if excluded_ids:
        task_entries = [t for t in task_entries if t["task_id"] not in excluded_ids]
    if args.hardest:
        task_entries.sort(key=lambda t: (-int(t.get("lines", 0)), t["task_id"]))
    if args.limit is not None:
        task_entries = _select_stratified(task_entries, args.limit) if args.stratified else task_entries[: args.limit]
    target_ids = [t["task_id"] for t in task_entries]

    all_rows = {
        row["task_id"]: row
        for row in (json.loads(line) for line in args.jsonl.open("r", encoding="utf-8") if line.strip())
    }
    selected_rows = [all_rows[tid] for tid in target_ids if tid in all_rows]

    cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
    pricing = cfg["pricing_usd_per_million_tokens"]
    if args.pricing_tier not in pricing:
        print(f"ERROR: Unknown --pricing-tier '{args.pricing_tier}'. Choices: {sorted(pricing)}", file=sys.stderr)
        return 1
    ledger = ExperimentLedger(args.out, budget_cap_usd=args.budget_cap_usd, pricing_table=pricing)
    already_done = {r.task_id for r in ExperimentLedger.load_records(args.out)} if args.out.exists() else set()
    pending = [r for r in selected_rows if r["task_id"] not in already_done]

    if args.dry_run:
        sample_prompt = (
            _build_prompt(
                args.mode,
                Path("/tmp/task.rs"),
                Path("/tmp/orig.rs"),
                pending[0]["target_function"],
                verus_bin,
                line_count=pending[0]["task"].count("\n") + 1,
                is_exec_fn=_is_target_exec_fn(pending[0]["task"], pending[0]["target_function"]),
            )
            if pending
            else ""
        )
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "mode": args.mode,
                    "agent": args.agent,
                    "agent_binary": agent_bin,
                    "model": args.model,
                    "thinking": args.thinking,
                    "pricing_tier": args.pricing_tier,
                    "verus_binary": str(verus_bin),
                    "manifest_tasks": len(selected_rows),
                    "projects": sorted({r["project"] for r in selected_rows}),
                    "already_completed": len(already_done),
                    "pending": len(pending),
                    "budget_cap_usd": args.budget_cap_usd,
                    "sample_cmd": shlex.join(
                        _agent_command(
                            args.agent,
                            agent_bin or args.agent,
                            "<prompt>",
                            args.model,
                            args.max_turns,
                            mode=args.mode,
                            task_timeout_sec=args.task_timeout_sec,
                            thinking=args.thinking,
                        )
                    ),
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

    if agent_bin is None:
        print(f"ERROR: Agent CLI '{args.agent}' not found.", file=sys.stderr)
        return 1

    artifacts_dir = args.out.parent / args.out.stem
    print(
        f"Starting {args.mode} run on {len(pending)} tasks "
        f"(agent={args.agent}, model={args.model}, workers={args.workers}, cap=${args.budget_cap_usd:.2f})",
        flush=True,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        in_flight: dict[concurrent.futures.Future[TaskRecord], str] = {}
        queue_iter = iter(pending)
        cap_reached = False

        def _submit_up_to_workers() -> None:
            nonlocal cap_reached
            while len(in_flight) < args.workers and not cap_reached:
                try:
                    ledger.check_budget(0.0)
                except BudgetExceededError as exc:
                    print(f"BUDGET CAP REACHED: {exc}", file=sys.stderr)
                    cap_reached = True
                    break
                try:
                    row = next(queue_iter)
                except StopIteration:
                    break
                fut = pool.submit(
                    run_single_task,
                    row,
                    mode=args.mode,
                    agent=args.agent,
                    agent_bin=agent_bin,
                    model=args.model,
                    verus_bin=verus_bin,
                    task_timeout_sec=args.task_timeout_sec,
                    verus_rlimit=args.verus_rlimit,
                    max_turns=args.max_turns,
                    artifacts_dir=artifacts_dir,
                    pricing_table=pricing,
                    pricing_tier=args.pricing_tier,
                    thinking=args.thinking,
                )
                in_flight[fut] = row["task_id"]

        _submit_up_to_workers()
        while in_flight:
            done, _ = concurrent.futures.wait(
                in_flight.keys(),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                in_flight.pop(fut)
                rec = fut.result()
                ledger.record_task(rec)
                print(
                    f"[{rec.status:14s}] {rec.project} {rec.task_id[:45]:45s} "
                    f"turns={rec.calls:2d} in={rec.input_tokens:7d} out={rec.output_tokens:5d} "
                    f"${rec.total_cost_usd:.3f} {rec.wall_seconds:5.1f}s (spent ${ledger.spent_usd:.2f}/${args.budget_cap_usd:.2f})",
                    flush=True,
                )
            _submit_up_to_workers()

    summary = summarize_ledger(ExperimentLedger.load_records(args.out))
    print("\n=== FINAL SUMMARY ===", flush=True)
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
