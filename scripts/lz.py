#!/usr/bin/env python3
"""lemma-zero deterministic local verifier engine (`lz`).

Stdlib-only Python entry point invoked by ANY coding agent (Claude Code, Codex,
Antigravity, Pi, Hermes, ZCode). Makes zero LLM calls: provides spec slicing,
structured Verus/Z3 checking, bottom-up single-lemma verification, strict
anti-cheat gating, and exact cost ledger summaries.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from lemma_zero.anticheat import verify_candidate
from lemma_zero.envcheck import check_environment
from lemma_zero.ledger import ExperimentLedger, summarize_ledger
from lemma_zero.slicer import show_symbol, show_symbols, slice_for_target
from lemma_zero.verus_runner import find_verus_binary, run_verus


def _load_config(path: Path | None) -> dict:
    cfg_path = path or (REPO_ROOT / "config" / "experiment.json")
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def _cmd_selftest(cfg: dict) -> int:
    sample_orig = "verus! {\npub proof fn t(x: int) requires x > 0 ensures x >= 1 {}\n}"
    sample_good = "verus! {\nproof fn h(x: int) requires x > 0 ensures x >= 1 {}\npub proof fn t(x: int) requires x > 0 ensures x >= 1 { h(x); }\n}"
    sample_bad = "verus! {\npub proof fn t(x: int) requires x > 0 ensures x >= 1 { assume(x >= 1); }\n}"

    ok_good = verify_candidate(sample_orig, sample_good).clean
    ok_bad = not verify_candidate(sample_orig, sample_bad).clean
    sl = slice_for_target(sample_good, target_function="t")
    verus_bin = find_verus_binary()
    payload = {
        "ok": bool(ok_good and ok_bad and sl["target"] is not None),
        "anticheat_selftest": ok_good and ok_bad,
        "slicer_selftest": sl["target"] is not None,
        "verus_binary": str(verus_bin) if verus_bin else None,
        "verus_ready": verus_bin is not None,
        "budget_cap_usd": cfg["safety"]["budget_cap_usd"],
    }
    print(json.dumps(payload, indent=2))
    return 0 if payload["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lz",
        description="lemma-zero deterministic local Verus engine for coding agents.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Path to config/experiment.json.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selftest", help="Verify plugin installation and local engine invariants.")

    p_env = sub.add_parser("env-check", help="Inspect Rust, Verus, coding-agent CLIs, and disk space.")
    p_env.add_argument("--json", action="store_true")

    p_slice = sub.add_parser("slice", help="Extract compact reachable spec cone around target function.")
    p_slice.add_argument("file", type=Path, help="Path to Verus .rs file.")
    p_slice.add_argument("--target", type=str, default=None, help="Target function name.")
    p_slice.add_argument("--line", type=int, default=None, help="Optional failing line number.")
    p_slice.add_argument("--depth", type=int, default=2, help="Transitive dependency depth.")
    p_slice.add_argument("--max-lines", type=int, default=450, help="Maximum sliced lines.")
    p_slice.add_argument("--json", action="store_true")

    p_diag = sub.add_parser("diag", help="Single-call Turn-1 check + reachable spec slice (saves turns on large files).")
    p_diag.add_argument("file", type=Path, help="Path to Verus .rs file.")
    p_diag.add_argument("--original", type=Path, default=None, help="Optional original task .rs file.")
    p_diag.add_argument("--target", type=str, default=None, help="Target function name.")
    p_diag.add_argument("--depth", type=int, default=2)
    p_diag.add_argument("--max-lines", type=int, default=450)
    p_diag.add_argument("--rlimit", type=int, default=15)
    p_diag.add_argument("--timeout", type=int, default=25)
    p_diag.add_argument("--verus", type=Path, default=None)
    p_diag.add_argument("--json", action="store_true")

    p_show = sub.add_parser("show", help="Show exact line-numbered source for one or more symbols on demand.")
    p_show.add_argument("file", type=Path)
    p_show.add_argument(
        "--symbol",
        "--symbols",
        dest="symbols",
        type=str,
        action="append",
        required=True,
        help="Symbol name(s); accepts comma-separated lists (e.g. --symbol foo,bar) or repeated flags.",
    )
    p_show.add_argument("--json", action="store_true")

    p_check = sub.add_parser("check", help="Run anti-cheat (optional) + real Verus check with structured errors.")
    p_check.add_argument("file", type=Path, help="Candidate .rs file to verify.")
    p_check.add_argument("--original", type=Path, default=None, help="Original task .rs file for anti-cheat gate.")
    p_check.add_argument("--fn", dest="fn_name", type=str, default=None, help="Optional single function for fast intermediate checks.")
    p_check.add_argument("--rlimit", type=int, default=None)
    p_check.add_argument("--timeout", type=int, default=None)
    p_check.add_argument("--expand-errors", action="store_true")
    p_check.add_argument("--verus", type=Path, default=None, help="Explicit Verus binary path.")
    p_check.add_argument("--json", action="store_true")

    p_vlem = sub.add_parser("verify-lemma", help="Verify a single helper proof fn bottom-up in seconds.")
    p_vlem.add_argument("file", type=Path)
    p_vlem.add_argument("--fn", dest="fn_name", type=str, required=True, help="Function name to verify in isolation.")
    p_vlem.add_argument("--module", type=str, default=None, help="Optional Verus module path.")
    p_vlem.add_argument("--rlimit", type=int, default=15)
    p_vlem.add_argument("--timeout", type=int, default=30)
    p_vlem.add_argument("--verus", type=Path, default=None)
    p_vlem.add_argument("--json", action="store_true")

    p_ac = sub.add_parser("anticheat", help="Run deterministic anti-cheat & spec-preservation check.")
    p_ac.add_argument("--original", type=Path, required=True)
    p_ac.add_argument("--candidate", type=Path, required=True)
    p_ac.add_argument("--json", action="store_true")

    p_led = sub.add_parser("ledger-summary", help="Summarize experiment JSONL ledger.")
    p_led.add_argument("--file", type=Path, required=True)
    p_led.add_argument("--json", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = _load_config(args.config)

    if args.command == "selftest":
        return _cmd_selftest(cfg)

    if args.command == "env-check":
        report = check_environment(min_free_disk_gb=float(cfg["safety"]["min_free_disk_gb"]))
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        print(f"# lemma-zero Environment ({report['machine']})")
        print(f"- Free Disk: {report['free_disk_gb']} GB (floor {report['min_free_disk_gb']} GB)")
        print(f"- Verus Ready: {report['ready_for_verus_runs']}")
        for k, v in report["tools"].items():
            print(f"  - {k}: {v or 'MISSING'}")
        for w in report["warnings"]:
            print(f"- WARNING: {w}")
        return 0

    if args.command == "slice":
        src = args.file.read_text(encoding="utf-8")
        res = slice_for_target(
            src,
            target_function=args.target,
            failing_line=args.line,
            max_depth=args.depth,
            max_lines=args.max_lines,
        )
        if args.json:
            print(json.dumps(res, indent=2))
            return 0
        tgt = res["target"]
        tgt_label = f"{tgt['name']} (L{tgt['start_line']}..L{tgt['end_line']})" if tgt else "unknown"
        print(
            f"# Slice: {args.file.name} | target={tgt_label} | "
            f"{res['sliced_lines']}/{res['total_lines']} lines (-{res['reduction_pct']}%) | "
            f"omitted={res['omitted_symbol_count']} symbols"
        )
        print(res["context_text"])
        return 0

    if args.command == "diag":
        src = args.file.read_text(encoding="utf-8")
        ac_report = None
        if args.original is not None:
            ac_report = verify_candidate(args.original.read_text(encoding="utf-8"), src)
        verus_res = run_verus(
            args.file,
            verus_binary=args.verus,
            rlimit=args.rlimit,
            timeout_seconds=args.timeout,
            verify_function=args.target,
        )
        failing_line = verus_res.errors[0].line if verus_res.errors else None
        sl = slice_for_target(
            src,
            target_function=args.target,
            failing_line=failing_line,
            max_depth=args.depth,
            max_lines=args.max_lines,
        )
        if args.json:
            print(
                json.dumps(
                    {
                        "anticheat": ac_report.to_dict() if ac_report else None,
                        "verus": verus_res.to_dict(),
                        "slice": sl,
                    },
                    indent=2,
                )
            )
            return 0 if verus_res.verified else 1
        status = "VERIFIED" if verus_res.verified else "FAILED"
        ac_label = "CLEAN" if (ac_report and ac_report.clean) else ("UNCHECKED" if ac_report is None else "FAIL")
        print(f"{status} (scope={args.target or 'full'}) | {verus_res.raw_summary} | time={verus_res.wall_seconds}s | anticheat={ac_label}")
        for idx, err in enumerate(verus_res.errors, start=1):
            loc = f"L{err.line}:{err.col}" if err.line else "unknown"
            print(f"[{idx}] {err.error_class} @ {loc}: {err.message}")
            if err.snippet:
                print(err.snippet)
            print(f"  -> {err.hint}")
        tgt = sl["target"]
        tgt_label = f"{tgt['name']} (L{tgt['start_line']}..L{tgt['end_line']})" if tgt else "unknown"
        is_exec_fn = bool(tgt and "proof fn" not in tgt["signature"] and "spec fn" not in tgt["signature"])
        mode_hint = (
            "target_mode=exec_fn (prefer inline `proof { ... }` + existing file lemmas; avoid unnecessary helper `proof fn`s)"
            if is_exec_fn
            else "target_mode=proof_fn (batch-first helper `proof fn` DAG when induction/liveness is needed)"
        )
        print(
            f"\n# Slice: {args.file.name} | target={tgt_label} | {mode_hint} | "
            f"{sl['sliced_lines']}/{sl['total_lines']} lines (-{sl['reduction_pct']}%) | "
            f"omitted={sl['omitted_symbol_count']} symbols"
        )
        print(sl["context_text"])
        return 0 if verus_res.verified else 1

    if args.command == "show":
        src = args.file.read_text(encoding="utf-8")
        sym_list: list[str] = []
        for raw_arg in args.symbols:
            for part in raw_arg.split(","):
                s = part.strip()
                if s and s not in sym_list:
                    sym_list.append(s)
        found_map = show_symbols(src, sym_list)
        if args.json:
            if len(sym_list) == 1:
                print(json.dumps(found_map[sym_list[0]], indent=2))
            else:
                print(json.dumps(found_map, indent=2))
            return 0
        any_found = False
        missing: list[str] = []
        for sym in sym_list:
            matches = found_map[sym]
            if not matches:
                missing.append(sym)
                continue
            any_found = True
            for m in matches:
                print(f"// === [{m['kind']} {m['name']}] lines {m['start_line']}..{m['end_line']} ===")
                print(m["numbered_source"])
        if missing:
            print(f"Symbols not found in {args.file}: {', '.join(missing)}", file=sys.stderr)
        return 0 if any_found else 1

    if args.command == "anticheat":
        rep = verify_candidate(
            args.original.read_text(encoding="utf-8"),
            args.candidate.read_text(encoding="utf-8"),
        )
        if args.json:
            print(json.dumps(rep.to_dict(), indent=2))
        elif rep.clean:
            print(
                f"CLEAN (checked={rep.checked_original_functions}, "
                f"new_helpers={list(rep.new_helper_functions)})"
            )
        else:
            for v in rep.violations:
                print(f"REJECTED [{v.code}]: {v.message} ({v.detail})", file=sys.stderr)
        return 0 if rep.clean else 1

    if args.command == "check":
        ac_report = None
        if args.original is not None:
            ac_report = verify_candidate(
                args.original.read_text(encoding="utf-8"),
                args.file.read_text(encoding="utf-8"),
            )
            if not ac_report.clean:
                out = {
                    "verified": False,
                    "anticheat_clean": False,
                    "anticheat": ac_report.to_dict(),
                    "verus": None,
                }
                if args.json:
                    print(json.dumps(out, indent=2))
                else:
                    print("REJECTED_CHEAT (Verus skipped until cheat/spec-edit is removed):", file=sys.stderr)
                    for v in ac_report.violations:
                        print(f"  - [{v.code}] {v.message} ({v.detail})", file=sys.stderr)
                return 2

        verus_res = run_verus(
            args.file,
            verus_binary=args.verus,
            rlimit=args.rlimit or int(cfg["verus"]["rlimit"]),
            timeout_seconds=args.timeout or int(cfg["verus"]["timeout_seconds"]),
            verify_function=args.fn_name,
            expand_errors=args.expand_errors,
        )
        payload = {
            "verified": verus_res.verified,
            "scope": args.fn_name or "full",
            "anticheat_clean": ac_report.clean if ac_report is not None else None,
            "anticheat": ac_report.to_dict() if ac_report is not None else None,
            "verus": verus_res.to_dict(),
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            status = "VERIFIED" if verus_res.verified else "FAILED"
            scope_note = f" [scope={args.fn_name}; run full-file check before stopping]" if args.fn_name else ""
            print(
                f"{status}{scope_note} | {verus_res.raw_summary} | time={verus_res.wall_seconds}s | "
                f"anticheat={'CLEAN' if (ac_report and ac_report.clean) else ('UNCHECKED' if ac_report is None else 'FAIL')}"
            )
            for idx, err in enumerate(verus_res.errors, start=1):
                loc = f"L{err.line}:{err.col}" if err.line else "unknown"
                print(f"\n[{idx}] {err.error_class} @ {loc}: {err.message}")
                if err.snippet:
                    print(err.snippet)
                print(f"  -> Next move: {err.hint}")
        return 0 if verus_res.verified else 1

    if args.command == "verify-lemma":
        verus_res = run_verus(
            args.file,
            verus_binary=args.verus,
            rlimit=args.rlimit,
            timeout_seconds=args.timeout,
            verify_function=args.fn_name,
            verify_module=args.module,
        )
        if args.json:
            print(json.dumps(verus_res.to_dict(), indent=2))
        else:
            status = "LEMMA_VERIFIED" if verus_res.verified else "LEMMA_FAILED"
            print(f"{status} `{args.fn_name}` | {verus_res.raw_summary} | time={verus_res.wall_seconds}s")
            for err in verus_res.errors:
                loc = f"L{err.line}:{err.col}" if err.line else "unknown"
                print(f"  - [{err.error_class}] @ {loc}: {err.message}\n    -> {err.hint}")
        return 0 if verus_res.verified else 1

    if args.command == "ledger-summary":
        summary = summarize_ledger(ExperimentLedger.load_records(args.file))
        if args.json:
            print(json.dumps(summary, indent=2))
        else:
            cps = f"${summary['cost_per_solved_usd']:.4f}" if summary["cost_per_solved_usd"] is not None else "N/A"
            print(
                f"Solved: {summary['solved']}/{summary['total_tasks']} ({summary['solve_rate_pct']}%) | "
                f"Cheats: {summary['cheat_rejections']} | Total: ${summary['total_cost_usd']:.4f} | "
                f"Avg/task: ${summary['avg_cost_per_task_usd']:.4f} | Cost/solved: {cps}"
            )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
