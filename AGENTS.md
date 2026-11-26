# AGENTS.md — Scientific & Engineering Protocol for `lemma-zero`

This repository is a formal-verification research codebase. Scientific integrity and exact cost accounting take priority over speed or headline numbers.

---

## 1. Non-Negotiable Quality & Anti-Cheat Invariants

A candidate Verus solution counts as **SOLVED** if and only if **both** conditions hold:
1. **Zero-Cheat Static Check (`src/lemma_zero/anticheat.py`):**
   - Zero newly added `assume(...)` or `admit()` calls.
   - Zero newly added cheat attributes (`#[verifier::external_body]`, `#[verifier::external]`, `#[verifier::external_fn_specification]`, `#[verifier::exec_allows_no_decreases_clause]`, `#[verifier::external_type_specification]`, `#[verifier::axiom]` / axiom stubs).
   - Zero newly added `unimplemented!()`, `todo!()`, `unreachable!()`, `panic!()` in verified bodies.
   - Zero modifications, weakenings, or deletions of original function signatures, `requires`, `ensures`, or `recommends` clauses.
   - Newly invented `proof fn` / `spec fn` helper lemmas are permitted **only** when they are fully verified (no `external_body`, no `admit`/`assume`).
2. **Real Verus Kernel Verification:**
   - Verified cleanly by the pinned real `verus` binary (`0 errors`, `verified > 0`, matching `VeruSAGE-Bench` invocation). Never mock or stand-in for Verus verification in benchmark reporting.

---

## 2. Pre-Registration Discipline

- Never inspect, tune prompts on, or iterate against held-out test tasks before committing the frozen split manifest and hypothesis in `docs/03-preregistration-protocol.md`.
- All prompt/architecture iteration happens exclusively on the designated **dev slice**.
- Report **every** held-out run honestly — including negative results, timeouts, failed proof attempts, and full billed cost across both solved and failed tasks.

---

## 3. Exact Cost & Spend Controls

- `config/experiment.json` defaults to `"budget_cap_usd": 0.0`. No paid API or cloud GPU run may execute without explicit owner authorization and an explicit USD cap.
- Every model call MUST be logged in `ledger.py` with exact `input_tokens`, `cache_write_tokens`, `cache_read_tokens`, `output_tokens`, `thinking_tokens`, `wall_seconds`, and `cost_usd`.
- Long batch runs must execute detached with hard per-task timeouts (`verus_timeout_sec`, `task_timeout_sec`) and run-level `budget_cap_usd` circuit breakers.

---

## 4. Hardware & Disk Budget (256 GB Apple M2)

- Keep at least **20 GB free** on local disk at all times.
- Prefer the lightweight native `verus` binary (~30 MB zip) or cloud/ephemeral storage over multi-GB local Docker images or full upstream history clones.
- Use shallow clones (`git clone --depth 1`) or sparse checkout when pulling benchmark tasks; store large run archives in cloud storage if they exceed a few hundred MB.
