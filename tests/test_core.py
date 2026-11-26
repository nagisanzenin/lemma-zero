"""Unit and real-Verus integration tests for the lemma-zero plugin."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from lemma_zero.anticheat import verify_candidate
from lemma_zero.ledger import BudgetExceededError, CallUsage, ExperimentLedger, TaskRecord, summarize_ledger
from lemma_zero.slicer import show_symbol, slice_for_target
from lemma_zero.verus_runner import classify_error, find_verus_binary, parse_verus_errors, run_verus

REPO_ROOT = Path(__file__).resolve().parents[1]
LZ_SCRIPT = REPO_ROOT / "scripts" / "lz.py"

SAMPLE_ORIG = """
use vstd::prelude::*;
fn main() {}
verus! {

pub open spec fn sorted(s: Seq<int>) -> bool {
    forall|i: int, j: int| 0 <= i < j < s.len() ==> s[i] <= s[j]
}

pub open spec fn unrelated_helper(x: int) -> int {
    x + 42
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
        cand = SAMPLE_ORIG.replace(
            "pub proof fn lemma_target",
            """proof fn helper_step(s: Seq<int>, k: int)
    requires sorted(s), 0 < k < s.len(),
    ensures s[0] <= s[k],
{
    assert(0 <= 0 < k < s.len());
}

pub proof fn lemma_target""",
        ).replace("{\n}", "{\n    if k > 0 { helper_step(s, k); }\n}")
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

    def test_rejects_all_cheat_vectors(self) -> None:
        cheats = [
            ("ASSUME_CALL", SAMPLE_ORIG.replace("{\n}", "{\n    assume(s[0] <= s[k]);\n}")),
            ("ADMIT_CALL", SAMPLE_ORIG.replace("{\n}", "{\n    admit();\n}")),
            ("EXTERNAL_BODY_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::external_body]\npub proof fn lemma_target")),
            ("EXTERNAL_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::external]\npub proof fn lemma_target")),
            ("SPEC_OR_SIGNATURE_MODIFIED", SAMPLE_ORIG.replace("s[0] <= s[k],", "true,")),
            ("AXIOM_DECL", SAMPLE_ORIG + "\naxiom fn fake_magic() ensures false;\n"),
            ("PANIC_STUB", SAMPLE_ORIG.replace("{\n}", "{\n    unimplemented!();\n}")),
        ]
        for expected_code, bad_src in cheats:
            with self.subTest(expected_code=expected_code):
                report = verify_candidate(SAMPLE_ORIG, bad_src)
                self.assertFalse(report.clean)
                self.assertIn(expected_code, [v.code for v in report.violations])


class TestSlicerAndErrorClassifier(unittest.TestCase):
    def test_slicer_keeps_reachable_spec_and_drops_unrelated_symbols(self) -> None:
        sl = slice_for_target(SAMPLE_ORIG, target_function="lemma_target", max_depth=2, max_lines=200)
        names = [it["name"] for it in sl["selected_items"]]
        self.assertIn("lemma_target", names)
        self.assertIn("sorted", names)
        self.assertNotIn("unrelated_helper", names)

        shown = show_symbol(SAMPLE_ORIG, "unrelated_helper")
        self.assertEqual(len(shown), 1)
        self.assertIn("x + 42", shown[0]["numbered_source"])

    def test_error_parser_and_classifier(self) -> None:
        sample_stderr = (
            "error: postcondition not satisfied\n"
            "  --> task.rs:48:13\n"
            "   |\n"
            "48 |     ensures forall|i: int| 0 <= i < s.len() ==> s[i] >= 0,\n"
            "   |             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ failed\n"
            "\n"
            "error: aborting due to 1 previous error\n"
        )
        errs = parse_verus_errors(sample_stderr)
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0].line, 48)
        self.assertEqual(errs[0].error_class, "TRIGGER_INSTANTIATION")

        cls, _ = classify_error(None, "Resource limit (rlimit) exceeded", "")
        self.assertEqual(cls, "RLIMIT_TIMEOUT")


class TestLedgerAndBudgetCap(unittest.TestCase):
    def setUp(self) -> None:
        cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
        self.pricing = cfg["pricing_usd_per_million_tokens"]

    def test_exact_cost_and_budget_cap(self) -> None:
        usage = CallUsage(
            tier="frontier_reasoning",
            model="test",
            input_tokens=100_000,
            cache_write_tokens=40_000,
            cache_read_tokens=400_000,
            output_tokens=10_000,
            thinking_tokens=20_000,
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
            self.assertAlmostEqual(summary["cost_per_solved_usd"], 1.60, places=4)


class TestPluginAndRealVerus(unittest.TestCase):
    def test_lz_selftest_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(LZ_SCRIPT), "selftest"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["ok"])

    @unittest.skipUnless(find_verus_binary() is not None, "Verus binary not installed locally")
    def test_real_verus_rejects_unproved_and_accepts_proved_with_helper_lemma(self) -> None:
        inductive_orig = """
use vstd::prelude::*;
fn main() {}
verus! {

pub open spec fn tri(n: nat) -> nat
    decreases n,
{
    if n == 0 { 0 } else { n + tri((n - 1) as nat) }
}

pub proof fn lemma_gauss_target(n: nat)
    ensures
        2 * tri(n) == n * (n + 1),
{
}

} // verus!
"""
        proved = inductive_orig.replace(
            "pub proof fn lemma_gauss_target",
            """proof fn helper_gauss_ind(n: nat)
    ensures
        2 * tri(n) == n * (n + 1),
    decreases n,
{
    if n == 0 {
        assert(2 * tri(0) == 0 * (0 + 1)) by (nonlinear_arith)
            requires tri(0) == 0;
    } else {
        helper_gauss_ind((n - 1) as nat);
        assert(tri(n) == n + tri((n - 1) as nat));
        assert(((n - 1) as nat) * (((n - 1) as nat) + 1) + 2 * n == n * (n + 1)) by (nonlinear_arith)
            requires n > 0;
    }
}

pub proof fn lemma_gauss_target""",
        ).replace("{\n}", "{\n    helper_gauss_ind(n);\n}")

        with tempfile.TemporaryDirectory() as tmp:
            orig_p = Path(tmp) / "orig.rs"
            proved_p = Path(tmp) / "proved.rs"
            orig_p.write_text(inductive_orig, encoding="utf-8")
            proved_p.write_text(proved, encoding="utf-8")

            r_fail = run_verus(orig_p, rlimit=10, timeout_seconds=30)
            self.assertFalse(r_fail.verified)
            self.assertGreaterEqual(r_fail.error_count, 1)

            r_lem = run_verus(proved_p, rlimit=10, timeout_seconds=30, verify_function="helper_gauss_ind")
            self.assertTrue(r_lem.verified, r_lem.to_dict())

            r_ok = run_verus(proved_p, rlimit=10, timeout_seconds=30)
            self.assertTrue(r_ok.verified, r_ok.to_dict())

            ac = verify_candidate(inductive_orig, proved)
            self.assertTrue(ac.clean)


if __name__ == "__main__":
    unittest.main()
