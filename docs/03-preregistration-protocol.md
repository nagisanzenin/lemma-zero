# 03 — Pre-Registration Protocol (Draft — Lock Before Held-Out Runs)

> **Status:** `DRAFT (UNFROZEN)` — Fill in exact split manifest hashes, model IDs, and owner-authorized USD cap, then commit and tag (`prereg-v1`) **before** any model ever sees a held-out task.

---

## 1. Research Questions & Primary Hypotheses

- **H1 (Solve Rate on `tasks-no-lemma`):** On a stratified held-out split of `VeruSAGE-Bench` `tasks-no-lemma.jsonl`, **Math-First bottom-up lemma decomposition + Z3 trace routing (`lemma-zero`)** achieves a statistically significant higher verified solve rate than a direct repair-loop baseline using the same base model family.
- **H2 (Cost Pareto Frontier):** Under identical anti-cheat grading, `lemma-zero`'s spec-slicing + KV-cache prefix stability + tiered syntax/lemma routing reduces **total billed USD per verified proof** (`total_run_cost_usd / solved_count`) by at least **2×** relative to frontier-only flat repair loops.

---

## 2. Dataset Split Discipline

From the **431 standalone tasks** in `tasks-no-lemma.jsonl` (excluding the 29 `ST` storage tasks that require separate crate compilation, which will be evaluated in a separate clearly labeled annex if environment-verified):

- **Dev Slice (`N = 20` tasks):** Stratified across all 8 standalone projects (`AL`, `AC`, `IR`, `MA`, `NO`, `NR`, `OS`, `VE`). Used exclusively for prompt design, parser debugging, and ablation engineering.
- **Held-Out Pilot Split (`N = 60` tasks):** Stratified by project code with a fixed deterministic PRNG seed (`seed = 20261126`), strictly disjoint from Dev. Locked in `manifests/heldout-60.json` prior to execution.
- **Full Held-Out Evaluation (`N = 411` remaining standalone tasks):** Triggered *only* after Pilot beats baseline on both H1 and H2 under explicit owner spend approval.

---

## 3. Grading & Anti-Cheat Contract

Every candidate solution is graded automatically by two gates:
1. `lemma_zero.anticheat` passes with `clean == True` (zero new `assume`, `admit`, `external_body`, `external`, `unimplemented!`/`todo!`/`panic!`, and byte-equivalent normalized original specs/signatures).
2. Official Verus binary exits `0` with `0 errors` and `verified >= 1` on the standalone candidate file (with optional cross-check via `microsoft/verus-proof-synthesis/utils/lynette` AST checker).

Any violation marks the attempt `REJECTED_CHEAT` (counts as **unsolved**, while its full token cost STILL counts toward total run cost).

---

## 4. Metrics Reported (No Cherry-Picking)

For every pre-registered run, publish the full JSONL ledger and report:
1. **Primary:** `solve_rate = solved / total` (overall + per-project breakdown `AL..VE`, with exact McNemar / Wilson 95% CI).
2. **Primary Cost:** `cost_per_solved_usd = total_billed_usd / solved` and `avg_cost_per_task_usd = total_billed_usd / total` (including failed runs!).
3. **Efficiency:** Median & mean wall-clock seconds per task, average model calls per task, cache hit ratio, and tier breakdown (cheap vs. frontier calls).
4. **Failure Taxonomy:** Counts of `TIMEOUT_VERUS`, `RLIMIT_EXCEEDED`, `SYNTAX_UNRESOLVED`, `LEMMA_NOT_FOUND`, `BUDGET_EXCEEDED`, and `REJECTED_CHEAT`.

---

## 5. Pre-Execution Authorization Gate

- **Authorized Pilot Budget Cap (`USD`):** `TBD (must be explicitly approved by owner before flipping config/experiment.json from 0.0)`
- **Pinned Verus Binary & Version:** `TBD (record exact verus --version output & SHA-256)`
- **Pre-Registration Commit SHA:** `TBD (locked before first held-out call)`
