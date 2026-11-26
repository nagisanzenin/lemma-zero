"""Command-line interface for lemma-zero Day-0 quality guards, ledger, and env checks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .anticheat import verify_candidate
from .envcheck import check_environment
from .ledger import ExperimentLedger, summarize_ledger


def _default_config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "experiment.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lemma-zero",
        description="Math-First Verus formal proof synthesis toolkit (guards, ledger, env-check).",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to experiment config JSON (defaults to config/experiment.json).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_env = sub.add_parser("env-check", help="Inspect local Rust/Verus toolchain and disk safety floor.")
    p_env.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_ac = sub.add_parser("check-anticheat", help="Verify candidate .rs against original .rs for cheats or spec edits.")
    p_ac.add_argument("--original", type=Path, required=True, help="Original VeruSAGE-Bench task .rs file.")
    p_ac.add_argument("--candidate", type=Path, required=True, help="Candidate synthesized proof .rs file.")
    p_ac.add_argument("--json", action="store_true", help="Emit JSON output.")

    p_led = sub.add_parser("ledger-summary", help="Summarize benchmark solve rate and exact USD cost from JSONL ledger.")
    p_led.add_argument("--file", type=Path, required=True, help="Path to JSONL ledger file.")
    p_led.add_argument("--json", action="store_true", help="Emit JSON output.")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg_path = args.config or _default_config_path()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    if args.command == "env-check":
        min_disk = float(cfg["safety"]["min_free_disk_gb"])
        report = check_environment(min_free_disk_gb=min_disk)
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        print(f"# lemma-zero Environment Check ({report['machine']})")
        print(f"- **Platform:** {report['platform']}")
        print(f"- **Free Disk:** {report['free_disk_gb']} GB (floor: {report['min_free_disk_gb']} GB) -> {'OK' if report['disk_ok'] else 'WARNING'}")
        print(f"- **Ready for Verus Runs:** {report['ready_for_verus_runs']}\n")
        for tool, ver in report["tools"].items():
            print(f"  - `{tool}`: {ver or 'MISSING'}")
        if report["warnings"]:
            print("\n## Warnings")
            for w in report["warnings"]:
                print(f"- {w}")
        return 0

    if args.command == "check-anticheat":
        orig = args.original.read_text(encoding="utf-8")
        cand = args.candidate.read_text(encoding="utf-8")
        report = verify_candidate(orig, cand)
        if args.json:
            print(json.dumps(report.to_dict(), indent=2))
        elif report.clean:
            helpers = ", ".join(report.new_helper_functions) or "none"
            print(
                f"CLEAN: checked {report.checked_original_functions} original functions; "
                f"new helper functions: {helpers}."
            )
        else:
            print("REJECTED_CHEAT:", file=sys.stderr)
            for v in report.violations:
                print(f"  - [{v.code}] {v.message} ({v.detail})", file=sys.stderr)
        return 0 if report.clean else 1

    if args.command == "ledger-summary":
        records = ExperimentLedger.load_records(args.file)
        summary = summarize_ledger(records)
        if args.json:
            print(json.dumps(summary, indent=2))
            return 0
        print("# lemma-zero Experiment Summary")
        print(f"- **Solved:** {summary['solved']} / {summary['total_tasks']} ({summary['solve_rate_pct']}%)")
        print(f"- **Cheat Rejections:** {summary['cheat_rejections']}")
        print(f"- **Total Billed Cost:** ${summary['total_cost_usd']:.4f}")
        print(f"- **Avg Cost / Task:** ${summary['avg_cost_per_task_usd']:.4f}")
        cps = (
            f"${summary['cost_per_solved_usd']:.4f}"
            if summary["cost_per_solved_usd"] is not None
            else "N/A"
        )
        print(f"- **Cost / Solved Proof:** {cps}")
        print(f"- **Median Wall Time:** {summary['median_wall_seconds']}s | **Avg Calls:** {summary['avg_calls_per_task']}")
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
