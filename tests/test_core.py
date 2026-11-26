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
from lemma_zero.slicer import show_symbol, show_symbols, slice_for_target
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
            ("EXTERNAL_BODY_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier(external_body)]\npub proof fn lemma_target")),
            ("EXTERNAL_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::external]\npub proof fn lemma_target")),
            ("EXTERNAL_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier(external)]\npub proof fn lemma_target")),
            ("ASSUME_SPEC_ATTR", SAMPLE_ORIG.replace("pub proof fn lemma_target", "#[verifier::assume_specification]\npub proof fn lemma_target")),
            ("SPEC_OR_SIGNATURE_MODIFIED", SAMPLE_ORIG.replace("s[0] <= s[k],", "true,")),
            ("SPEC_OR_ITEM_BODY_MODIFIED", SAMPLE_ORIG.replace("x + 42", "x + 0")),
            ("AXIOM_DECL", SAMPLE_ORIG + "\naxiom fn fake_magic() ensures false;\n"),
            ("PANIC_STUB", SAMPLE_ORIG.replace("{\n}", "{\n    unimplemented!();\n}")),
        ]
        for expected_code, bad_src in cheats:
            with self.subTest(expected_code=expected_code):
                report = verify_candidate(SAMPLE_ORIG, bad_src, target_function="lemma_target")
                self.assertFalse(report.clean)
                self.assertIn(expected_code, [v.code for v in report.violations])

    def test_rejects_removing_external_body_stub_and_inventing_spec_body(self) -> None:
        orig_stub = """
verus! {
#[verifier::external_body]
pub closed spec fn init(x: int) -> bool {
    unimplemented!()
}
pub proof fn target(x: int) requires init(x) ensures x == 0 {}
}
"""
        fake_cand = orig_stub.replace(
            "#[verifier::external_body]\npub closed spec fn init(x: int) -> bool {\n    unimplemented!()\n}",
            "pub closed spec fn init(x: int) -> bool {\n    x == 0\n}",
        )
        rep = verify_candidate(orig_stub, fake_cand, target_function="target")
        self.assertFalse(rep.clean)
        codes = {v.code for v in rep.violations}
        self.assertIn("STUB_REMOVED", codes)
        self.assertIn("SPEC_OR_ITEM_BODY_MODIFIED", codes)


class TestSlicerAndErrorClassifier(unittest.TestCase):
    def test_slicer_keeps_reachable_spec_and_drops_unrelated_symbols(self) -> None:
        # When file fits within max_lines, slicer preserves the complete numbered file (full_file=True).
        sl_full = slice_for_target(SAMPLE_ORIG, target_function="lemma_target", max_depth=2, max_lines=200)
        self.assertTrue(sl_full["full_file"])
        self.assertEqual(sl_full["omitted_symbol_count"], 0)

        # When file exceeds max_lines, slicer keeps reachable cone and drops unrelated symbols.
        padded = SAMPLE_ORIG + "\n" * 60
        sl = slice_for_target(padded, target_function="lemma_target", max_depth=2, max_lines=30)
        self.assertFalse(sl["full_file"])
        names = [it["name"] for it in sl["selected_items"]]
        self.assertIn("lemma_target", names)
        self.assertIn("sorted", names)
        self.assertNotIn("unrelated_helper", names)

        shown = show_symbol(SAMPLE_ORIG, "unrelated_helper")
        self.assertEqual(len(shown), 1)
        self.assertIn("x + 42", shown[0]["numbered_source"])

        batch = show_symbols(SAMPLE_ORIG, ["sorted", "unrelated_helper", "missing_sym"])
        self.assertEqual(len(batch["sorted"]), 1)
        self.assertEqual(len(batch["unrelated_helper"]), 1)
        self.assertEqual(batch["missing_sym"], [])

    def test_treatment_prompt_uses_literal_lz_path_without_shell_var(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        import run_eval

        prompt = run_eval._build_prompt(
            "treatment",
            Path("/tmp/task.rs"),
            Path("/tmp/orig.rs"),
            "lemma_target",
            Path("/tmp/verus"),
            line_count=50,
            is_exec_fn=False,
        )
        self.assertNotIn("$LZ", prompt)
        self.assertIn("scripts/lz.py", prompt)
        self.assertIn("check ", prompt)
        self.assertIn(" vstd ", prompt)
        self.assertNotIn("SKILL.md", prompt)

    def test_vstd_search_and_expand_errors_note_parsing(self) -> None:
        from lemma_zero.vstd_index import find_vstd_dir, search_vstd

        if find_vstd_dir() is not None:
            res_seq = search_vstd("filter no_duplicates", limit=5)
            self.assertGreater(res_seq["total_indexed"], 50)
            self.assertGreater(len(res_seq["matches"]), 0)
            res_bits = search_vstd("leading_zeros", limit=5)
            self.assertGreater(len(res_bits["matches"]), 0)

        expanded_stderr = (
            "error: precondition not satisfied\n"
            "  --> os.rs:1076:9\n"
            "   |\n"
            "1010 | value.perm.present ==> MEM_valid(value.addr),\n"
            "   | -------------------------------------------- failed precondition\n"
            "note: diagnostics via expansion\n"
            "  --> os.rs:1010:32\n"
            "   |\n"
            "1010 | value.perm.present ==> MEM_valid(value.addr),\n"
        )
        errs = parse_verus_errors(expanded_stderr)
        self.assertEqual(len(errs), 1)
        self.assertIn("diagnostics via expansion", errs[0].snippet)

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

        # Regression: rustc multi-line span `| /` must NOT trigger NONLINEAR_ARITH,
        # and substring `Set` in `VReplicaSetView` must NOT trigger TRIGGER_INSTANTIATION.
        multiline_precond = (
            "     |\n"
            "1010 |           value.perm.present ==> MEM_valid(value.addr),\n"
            "     |           -------------------------------------------- failed precondition\n"
            "...\n"
            "1076 | /         page_map_set(\n"
            "1077 | |             target_l2_p,\n"
        )
        cls_pre, _ = classify_error(None, "precondition not satisfied", multiline_precond)
        self.assertEqual(cls_pre, "PRECONDITION_FAIL")

        multiline_post = (
            "     |\n"
            "4826 | / pub proof fn stable_check(vrs: VReplicaSetView, cluster: Cluster)\n"
            "4832 | |     valid(stable(stable_spec(cluster)))\n"
        )
        cls_post, _ = classify_error(None, "postcondition not satisfied", multiline_post)
        self.assertEqual(cls_post, "MISSING_LEMMA")

    def test_slicer_exposes_available_proof_fn_catalog(self) -> None:
        src = SAMPLE_ORIG.replace(
            "pub proof fn lemma_target",
            "pub proof fn provided_library_lemma(x: int) ensures x == x {}\n\npub proof fn lemma_target",
        )
        sl = slice_for_target(src, target_function="lemma_target", max_depth=2, max_lines=200)
        avail_names = [it["name"] for it in sl["available_proof_fns"]]
        self.assertIn("provided_library_lemma", avail_names)
        self.assertIn("provided_library_lemma", sl["context_text"])

    def test_agy_usage_does_not_zero_out_input_tokens_when_cache_exceeds_input(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        import run_eval

        cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
        pricing = cfg["pricing_usd_per_million_tokens"]
        fake_stdout = json.dumps(
            {
                "usage": {
                    "input_tokens": 24224,
                    "cache_read_tokens": 45533,
                    "output_tokens": 1744,
                    "thinking_tokens": 798,
                },
                "stats": {"turns": 5},
            }
        )
        parsed = run_eval.extract_agent_usage("agy", fake_stdout, pricing, pricing_tier="mid_balanced")
        self.assertEqual(parsed["calls"], 5)
        # 24224*3.0 + 45533*0.3 + 1744*15.0 + 798*15.0 = 124461.9 -> $0.124462
        self.assertAlmostEqual(parsed["cost_usd"], 0.124462, places=6)

    def test_pi_ndjson_usage_counts_exact_turns_and_avoids_double_charging_reasoning(self) -> None:
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        import run_eval

        cfg = json.loads((REPO_ROOT / "config" / "experiment.json").read_text(encoding="utf-8"))
        pricing = cfg["pricing_usd_per_million_tokens"]
        ndjson = "\n".join(
            [
                json.dumps({"type": "session", "version": 3}),
                json.dumps({"type": "tool_execution_start", "toolName": "bash"}),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "model": "deepseek-v4-flash",
                            "usage": {"input": 10000, "cacheRead": 50000, "cacheWrite": 0, "output": 3000, "reasoning": 2000},
                            "content": [{"type": "text", "text": "step 1"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "model": "deepseek-v4-flash",
                            "usage": {"input": 2000, "cacheRead": 60000, "cacheWrite": 0, "output": 1000, "reasoning": 500},
                            "content": [{"type": "text", "text": "VERIFIED"}],
                        },
                    }
                ),
            ]
        )
        parsed = run_eval.extract_agent_usage("pi", ndjson, pricing, pricing_tier="deepseek_flash_offpeak")
        self.assertEqual(parsed["calls"], 2)
        self.assertEqual(parsed["usage"]["input_tokens"], 12000)
        self.assertEqual(parsed["usage"]["cache_read_tokens"], 110000)
        self.assertEqual(parsed["usage"]["output_tokens"], 1500)
        self.assertEqual(parsed["usage"]["thinking_tokens"], 2500)
        # (12000*0.15 + 110000*0.003 + 4000*0.60) / 1e6 = (1800 + 330 + 2400) / 1e6 = 0.004530
        self.assertAlmostEqual(parsed["cost_usd"], 0.00453, places=6)

    def test_macos_sandbox_blocks_runs_dataset_and_sibling_tmpdirs(self) -> None:
        if sys.platform != "darwin":
            self.skipTest("macOS sandbox-exec test")
        sys.path.insert(0, str(REPO_ROOT / "bench"))
        import run_eval

        with tempfile.TemporaryDirectory(prefix="lz_own_") as own_tmp, tempfile.TemporaryDirectory(prefix="lz_sib_") as sib_tmp:
            own_p = Path(own_tmp)
            sib_p = Path(sib_tmp)
            (own_p / "mine.txt").write_text("ok", encoding="utf-8")
            (sib_p / "secret.txt").write_text("secret", encoding="utf-8")

            # Own dir is readable, sibling lz_* dir is blocked, runs/ and skills/ (in control) are blocked.
            r_own = subprocess.run(
                run_eval.wrap_with_sandbox(["cat", str(own_p / "mine.txt")], mode="control", own_tmp_dir=own_p),
                capture_output=True,
                text=True,
            )
            self.assertEqual(r_own.returncode, 0, r_own.stderr)
            self.assertEqual(r_own.stdout.strip(), "ok")

            r_sib = subprocess.run(
                run_eval.wrap_with_sandbox(["cat", str(sib_p / "secret.txt")], mode="control", own_tmp_dir=own_p),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r_sib.returncode, 0)

            r_runs = subprocess.run(
                run_eval.wrap_with_sandbox(["ls", str(REPO_ROOT / "runs")], mode="treatment", own_tmp_dir=own_p),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r_runs.returncode, 0)

            r_skill_ctrl = subprocess.run(
                run_eval.wrap_with_sandbox(["cat", str(REPO_ROOT / "skills" / "lemma-zero" / "SKILL.md")], mode="control", own_tmp_dir=own_p),
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(r_skill_ctrl.returncode, 0)

            r_skill_trt = subprocess.run(
                run_eval.wrap_with_sandbox(["cat", str(REPO_ROOT / "skills" / "lemma-zero" / "SKILL.md")], mode="treatment", own_tmp_dir=own_p),
                capture_output=True,
                text=True,
            )
            self.assertEqual(r_skill_trt.returncode, 0, r_skill_trt.stderr)


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

    def test_agy_bench_dry_run_succeeds(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "bench" / "run_eval.py"),
                "--manifest",
                str(REPO_ROOT / "manifests" / "dev-20.json"),
                "--mode",
                "treatment",
                "--agent",
                "agy",
                "--model",
                "gemini-4-neon-medium",
                "--limit",
                "6",
                "--stratified",
                "--out",
                "/tmp/lz_test_agy_dry.jsonl",
                "--dry-run",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["manifest_tasks"], 6)
        self.assertEqual(len(data["projects"]), 6)
        self.assertIn("--add-dir", data["sample_cmd"])

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
