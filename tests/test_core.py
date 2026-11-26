"""Unit tests for lemma-zero anti-cheat verifier, billing ledger, and CLI."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lemma_zero.anticheat import verify_candidate
from lemma_zero.cli import main as cli_main
from lemma_zero.ledger import BudgetExceededError, CallUsage, ExperimentLedger, TaskRecord, summarize_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

SAMPLE_ORIG = """
verus! {

pub open spec fn sorted(s: Seq<int>) -> bool {
    forall|i: int, j: int| 0 <= i < j < s.len() ==> s[i] <= s[j]
}

pub proof fn lemma_target(s: Seq<int>, k: int)
    requires
        sorted(s),
        0 <= k < s.len(),
    ensures
        s[0] <= s[k],
{
}

} // verus!
"""


class TestAntiCheat(unittest.TestCase):
    def test_accepts_legitimate_helper_lemma_and_proof_body(self) -> None:
        cand = """
verus! {

pub open spec fn sorted(s: Seq<int>) -> bool {
    forall|i: int, j: int| 0 <= i < j < s.len() ==> s[i] <= s[j]
}

// Helper lemma synthesized from scratch (no assume/admit!).
proof fn helper_step(s: Seq<int>, k: int)
    requires
        sorted(s),
        0 < k < s.len(),
    ensures
        s[0] <= s[k],
{
    assert(0 <= 0 < k < s.len());
}

pub proof fn lemma_target(s: Seq<int>, k: int)
    requires
        sorted(s),
        0 <= k < s.len(),
    ensures
        s[0] <= s[k],
{
    if k > 0 {
        helper_step(s, k);
    }
}

} // verus!
"""
        report = verify_candidate(SAMPLE_ORIG, cand)
        self.assertTrue(report.clean, report.violations)
        self.assertEqual(report.new_helper_functions, ("helper_step",))

    def test_ignores_cheat_words_inside_comments_and_strings(self) -> None:
        cand = SAMPLE_ORIG.replace(
            "{\n}",
            "{\n    // Do NOT use assume(false) or admit() or #[verifier::external_body]\n    /* nested /* assume(false) */ comment */\n    assert(k == 0 || s[0] <= s[k]);\n}",
        )
        report = verify_candidate(SAMPLE_ORIG, cand)
        self.assertTrue(report.clean, report.violations)

    def test_rejects_assume_admit_external_body_and_spec_weakening(self) -> None:
        cheats = [
            ("ASSUME_CALL", SAMPLE_ORIG.replace("{\n}", "{\n    assume(s[0] <= s[k]);\n}")),
            ("ADMIT_CALL", SAMPLE_ORIG.replace("{\n}", "{\n    admit();\n}")),
            (
                "EXTERNAL_BODY_ATTR",
                SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::external_body]\npub proof fn lemma_target"),
            ),
            (
                "EXTERNAL_ATTR",
                SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::external]\npub proof fn lemma_target"),
            ),
            (
                "SPEC_OR_SIGNATURE_MODIFIED",
                SAMPLE_ORIG.replace("s[0] <= s[k],", "true,"),
            ),
            (
                "AXIOM_DECL",
                SAMPLE_ORIG + "\naxiom fn fake_magic() ensures false;\n",
            ),
            (
                "PANIC_STUB",
                SAMPLE_ORIG.replace("{\n}", "{\n    unimplemented!();\n}"),
            ),
        ]
        for expected_code, bad_src in cheats:
            with self.subTest(expected_code=expected_code):
                report = verify_candidate(SAMPLE_ORIG, bad_src)
                self.assertFalse(report.clean)
                self.assertIn(expected_code, [v.code for v in report.violations])


class TestLedgerAndBudgetCap(unittest.TestCase):
    def setUp(self) -> None:
        cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
        self.pricing = cfg["pricing_usd_per_million_tokens"]

    def test_exact_cost_calculation_and_budget_cap(self) -> None:
        usage = CallUsage(
            tier="frontier_reasoning",
            model="test-frontier",
            input_tokens=100_000,      # $0.50
            cache_write_tokens=40_000, # $0.25
            cache_read_tokens=400_000, # $0.10
            output_tokens=10_000,      # $0.25
            thinking_tokens=20_000,    # $0.50
        )
        self.assertAlmostEqual(usage.compute_cost_usd(self.pricing), 1.60, places=6)

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "run.jsonl"
            ledger = ExperimentLedger(ledger_path, budget_cap_usd=2.0, pricing_table=self.pricing)
            ledger.check_budget(1.60)
            ledger.record_task(
                TaskRecord(
                    task_id="ac_001",
                    project="AC",
                    status="SOLVED",
                    anticheat_clean=True,
                    verus_verified=True,
                    calls=4,
                    total_cost_usd=1.60,
                    wall_seconds=42.5,
                )
            )
            with self.assertRaises(BudgetExceededError):
                ledger.check_budget(0.50)

            summary = summarize_ledger(ExperimentLedger.load_records(ledger_path))
            self.assertEqual(summary["solved"], 1)
            self.assertEqual(summary["solve_rate_pct"], 100.0)
            self.assertAlmostEqual(summary["cost_per_solved_usd"], 1.60, places=4)

    def test_refuses_recording_cheated_task_as_solved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = ExperimentLedger(Path(tmp) / "run.jsonl", budget_cap_usd=10.0, pricing_table=self.pricing)
            with self.assertRaises(ValueError):
                ledger.record_task(
                    TaskRecord(
                        task_id="nr_001",
                        project="NR",
                        status="SOLVED",
                        anticheat_clean=False,
                        verus_verified=True,
                        calls=1,
                        total_cost_usd=0.10,
                        wall_seconds=5.0,
                    )
                )


class TestCLI(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main(argv)
        return code, buf.getvalue()

    def test_env_check_and_anticheat_cli(self) -> None:
        code, out = self._run(["env-check", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertIn("free_disk_gb", data)
        self.assertIn("tools", data)

        with tempfile.TemporaryDirectory() as tmp:
            orig = Path(tmp) / "orig.rs"
            good = Path(tmp) / "good.rs"
            orig.write_text(SAMPLE_ORIG, encoding="utf-8")
            good.write_text(SAMPLE_ORIG, encoding="utf-8")
            code, out = self._run(["check-anticheat", "--original", str(orig), "--candidate", str(good), "--json"])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["clean"])


if __name__ == "__main__":
    unittest.main()
