# 02 — System Architecture (`lemma-zero`)

## Core Hypothesis

Standard Verus repair agents fail on `tasks-no-lemma` and burn \$5+ per task for two coupled reasons:
1. **Syntax-first flailing instead of Math-First decomposition:** Without human helper lemmas, an agent dumped into a ~950-LoC Rust systems file immediately tries patching Z3 assertion failures locally instead of discovering the missing mathematical invariant or intermediate lemma DAG.
2. **Unbounded context & model waste:** Reloading full ~950-LoC files + raw multi-hundred-line Verus error dumps into a frontier model across 15–20 turns explodes input/thinking token cost (the exact billing law documented in `nonhuman`: $C = P(1.25u + 0.05N) + \sum_i \Delta_i(1.25 + 0.05(N-i)) + 5\sum_i(o_i + h_i)$).

`lemma-zero` solves both via a **3-Stage Oracle-Bounded Pipeline**.

---

## Stage 1 — Deterministic Spec & Dependency Slicer (0 LLM Tokens)

Before calling any LLM:
1. Parse the target `.rs` file (Rust/Verus AST or structured block parser) to separate:
   - The **target function** (`fn` / `proof fn` with missing proof)
   - Directly referenced `spec fn` definitions, data types, invariants, and `broadcast use` groups
   - Unrelated functions/helpers in the 950-LoC file that are outside the reachable call/type cone
2. Run a **baseline `verus` check** (0 LLM tokens!) to capture the exact initial failing `ensures` / `requires` / assertion span and Z3 diagnostics (`--expand-errors`, `--rlimit`).
3. Assemble a **cache-stable prefix $P$**: immutable task spec + minimal dependency cone, kept byte-identical across turns so prompt/KV cache reads hit at ~0.05×–0.1× cost.

---

## Stage 2 — Math-First Lemma & Invariant Planner (Frontier Model, 1–2 Calls)

Instead of emitting Rust syntax immediately, the frontier reasoning model produces a **compact structured Proof Plan**:
1. **Mathematical Object Identification:** Classify the proof obligation (e.g., *structural induction on sequence/map*, *extensional equality `=~=`*, *quantifier trigger instantiation*, *non-linear integer/bitvector arithmetic*, *state-machine transition invariant*).
2. **Helper Lemma DAG:** Propose 1–4 minimal, self-contained helper `proof fn` signatures (`requires` → `ensures`) ordered bottom-up so each lemma is independently verifiable by Verus/Z3 in < 5 seconds.
3. **Bottom-Up Lemma Verification:** Verify each proposed helper lemma *individually* (`--verify-only-module` / `--verify-function`) before assembling the top-level proof. If a proposed helper lemma itself is false (Z3 counterexample or rapid failure), prune or strengthen its precondition immediately instead of thrashing the whole file!

---

## Stage 3 — Verus/Z3 Trace-Guided Tiered Repair & FinOps Router

Every Verus failure is classified deterministically into one of four error buckets before selecting a model tier:

| Error Class | Examples | Routed Model Tier | Strategy |
| :--- | :--- | :--- | :--- |
| **A. Mechanical / Syntax / Type** | Borrow/syntax error, missing `use`, `spec` vs `exec` mode mismatch, wrong trigger annotation | **Cheap / Fast tier** | Tight diff patch on local span only (< 200 output tokens) |
| **B. Missing Assert / Trigger Instantiation** | Z3 needs `assert(P(x))`, `assert(a =~= b)`, `reveal(...)`, `broadcast use` | **Cheap / Mid tier** | Structured error span + relevant `spec fn` body |
| **C. Missing Mathematical Lemma / Induction Step** | `postcondition not satisfied` after local hints fail, or repeated Z3 timeout (`rlimit exceeded`) | **Frontier reasoning tier** | Invoke Stage 2 Math-First Planner to decompose into a new verifiable helper lemma |
| **D. Anti-Cheat Violation** | Any `assume`, `admit`, `external_body`, or spec edit detected by `anticheat.py` | **Deterministic Reject (0 tokens)** | Immediate hard rejection + rollback to last clean checkpoint |

---

## Mandatory Verification & Billing Root

Every candidate diff passes through two immutable local gates before being recorded as `SOLVED`:
1. **`lemma_zero.anticheat.verify_candidate(original, candidate)`** — guarantees strict spec preservation and zero cheat constructs.
2. **Fresh `verus --crate-type=lib` subprocess** with deterministic `--rlimit` and wall-clock timeout.
3. **`lemma_zero.ledger.ExperimentLedger`** — records exact token counts, USD cost, wall-clock seconds, and enforces `budget_cap_usd` before dispatching any new model call.
