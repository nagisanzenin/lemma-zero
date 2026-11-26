# 03 — Pre-Registration Protocol (`prereg-v1` — LOCKED)

> **Status:** `LOCKED (prereg-v1)` — Committed and tagged (`prereg-v1`) on `2026-11-26` **before** any model or prompt ever viewed a held-out task from `manifests/heldout-pilot-60.json` or `manifests/heldout-full-411.json`.

---

## 1. Research Questions & Primary Hypotheses

Evaluated on the **exact same base model (`deepseek/deepseek-v4-flash`, `--thinking high`)** inside identical isolated macOS kernel sandboxes (`sandbox-exec`, blocking `runs/`, `manifests/`, `vendor/tasks-no-lemma.jsonl`, `.git/`, sibling task directories, and repo writes):

- **H1 (Verified Solve Rate & Zero-Cheat Integrity on `tasks-no-lemma`):** On the frozen stratified held-out split `manifests/heldout-pilot-60.json` (`N = 60` tasks across all 8 standalone projects `AC, AL, IR, MA, NO, NR, OS, VE`), **`lemma-zero` v5** (structured Verus/Z3 diagnostics with `--expand-errors`, Lynette-grade anti-cheat gate `lz check --original --target`, instant `lz vstd` lemma search, mode-aware `exec fn` vs `proof fn` guidance, and optional `lz slice`/`lz show`) achieves a higher **clean verified solve rate** and **lower cheat-rejection rate** than bare `verus` (`control`).
- **H2 (Latency & Cost Pareto Efficiency):** Under identical strict anti-cheat grading and identical `300s` per-task timeouts, `lemma-zero` v5 maintains competitive or lower median wall-clock time and competitive USD cost per clean verified proof (`total_billed_usd / clean_solved_count`) without requiring multi-model escalation or tiered routing.

---

## 2. Frozen Dataset Splits & Cryptographic Hashes

All splits are generated deterministically (`seed = 20261126`) from the **431 standalone tasks** in `vendor/tasks-no-lemma.jsonl` (excluding the 29 `ST` storage tasks that require separate crate compilation, archived separately in `manifests/annex-st-29.json`):

| Artifact | Role | Tasks | SHA-256 |
| :--- | :--- | ---: | :--- |
| `vendor/tasks-no-lemma.jsonl` | Upstream VeruSAGE-Bench no-lemma source | 460 | `dd3b18cb90a20e6b80bcf47cd88d439ed0309a952ff1fd4090148cf98cdae878` |
| `manifests/dev-20.json` | Dev slice (prompt/engine iteration only) | 20 | `3e76eed4343e3c7984196e75f110a3bf510764d8c990ad3c1aa822e315c5bb35` |
| `manifests/heldout-pilot-60.json` | **Frozen Held-Out Pilot (this run)** | **60** | `6333f6d20439c89be76a2df76864009af362e3e00e44875a7e616f45effa479f` |
| `manifests/heldout-full-411.json` | Remaining held-out pool (untouched) | 411 | `66e0af5ff8e8f6d40e99c291396b6be3d7ebacf8576aabb1bfc87f93b560ec1e` |

`heldout-pilot-60.json` project distribution: `AC: 8`, `AL: 11`, `IR: 5`, `MA: 7`, `NO: 1`, `NR: 17`, `OS: 10`, `VE: 1` (`Total: 60`, strictly disjoint from `dev-20.json`).

---

## 3. Grading, Anti-Cheat & Sandbox Contract

Every candidate solution is graded automatically by two mandatory gates (and independently re-verified post-run):
1. **Lynette-Grade Anti-Cheat Gate (`src/lemma_zero/anticheat.py`):**
   - Zero newly added `assume(...)`, `admit()`, `#[verifier::external_body]`, `#[verifier(external_body)]`, `#[verifier::external*]`, `#[verifier::assume_specification]`, `#[verifier::exec_allows_no_decreases_clause]`, `axiom fn`, or panic stubs (`unimplemented!`/`todo!`/`unreachable!`/`panic!`).
   - Zero removals of pre-existing `external_body`/`external*`/panic stubs (`STUB_REMOVED`).
   - Byte-equivalent normalized headers (`requires`/`ensures`/`recommends`) for all original functions.
   - Normalized full-body immutability for every pre-existing `spec fn`, `struct`, `enum`, `type`, `const`, `trait`, `broadcast group`, and every non-target function (`SPEC_OR_ITEM_BODY_MODIFIED`).
2. **Real Verus Kernel Verification:**
   - Official pinned Verus binary exits `0` with `0 errors` and `verified >= 1` at `--rlimit 30`.
3. **OS-Level Sandbox Isolation (`sandbox-exec`):**
   - Blocks read access to `runs/`, `manifests/`, `.git/`, `vendor/tasks-no-lemma.jsonl`, `bench/`, `docs/`, `tests/`, `~/.env`, `~/.Trash`, conversation logs, sibling `lz_*` task temp dirs (plus `skills/`, `scripts/`, `src/`, `config/` in `control` mode), and blocks all writes into the repository.

Any violation marks the attempt `REJECTED_CHEAT` (counts as **unsolved**, while its full token cost STILL counts toward total run cost).

---

## 4. Metrics Reported (No Cherry-Picking)

For every pre-registered run, publish the full JSONL ledger and report:
1. **Primary:** `solve_rate = solved / 60` (overall + per-project breakdown `AC..VE`, exact two-sided McNemar test on discordant pairs, and Wilson 95% confidence intervals).
2. **Primary Cost:** `total_billed_usd`, `avg_cost_per_task_usd = total_billed_usd / 60`, and `cost_per_solved_usd = total_billed_usd / solved` (including failed, timed-out, and cheat-rejected attempts).
3. **Efficiency:** Median & mean wall-clock seconds per task, average LLM turns (`calls`) per task, token breakdown (`input`, `cache_read`, `output`, `thinking`), and shared-solved pairwise deltas.
4. **Failure & Integrity Taxonomy:** Exact counts of `SOLVED`, `FAILED`, `TIMEOUT_AGENT`, `TIMEOUT_VERUS`, `REJECTED_CHEAT`, and any upstream unprovable `external_body` spec stubs discovered post-hoc (reported transparently alongside raw `N = 60` denominator — never silently dropped).

---

## 5. Pre-Execution Authorization & Pinned Environment

- **Authorized Pilot Budget Cap (`USD`):** **`$3.50` total** (`$1.75` Control + `$1.75` Treatment, explicitly approved by owner on `2026-11-26`; `config/experiment.json` default remains `0.0`).
- **Headless Agent & Model:** `pi` (`--mode json --no-session --no-extensions --no-skills --no-context-files`), model `deepseek/deepseek-v4-flash`, `--thinking high`, `--pricing-tier deepseek_flash_offpeak` (`$0.15/1M` input, `$0.003/1M` cache read, `$0.60/1M` output/thinking).
- **Execution Bounds:** `--workers 10` per arm (`10 + 10` parallel with local 6-slot `LEMMA_ZERO_MAX_VERUS=6` semaphore), `--task-timeout-sec 300`, `--verus-rlimit 30`, sliding-window budget circuit breaker.
- **Pinned Verus Binary & Version:** `Verus 0.2025.09.12.bb1f342` (`macos_aarch64`, Rust `1.88.0-aarch64-apple-darwin`, commit `bb1f342683fd26de011825725a55325b65e7d359`), SHA-256 `7a7b319b170692d33ef3b9f526112f3a29003a7f60313f6e11f97ef34c9ffb36`.
- **Pre-Registration Git Tag:** `prereg-v1` (tagged on this exact commit before launching `runs/heldout60-ds41-control.jsonl` and `runs/heldout60-ds41-treatment.jsonl`).
