---
name: lemma-zero
description: >-
  Math-first formal proof synthesis for Verus Rust systems without human-provided
  helper lemmas. Use when verifying or repairing Verus Rust code, synthesizing
  inductive or algebraic helper lemmas, solving VeruSAGE-Bench no-lemma tasks,
  or formally verifying production Rust crates (e.g. dalek-verus, OS/distributed
  systems). Keeps context small via dependency slicing, verifies helper lemmas
  bottom-up, and enforces strict zero-cheat spec preservation.
---

# lemma-zero — Math-First Formal Proof Synthesis for Verus Rust

You are proving a Verus Rust verification obligation **without human-provided helper lemmas**.
Your goal is **machine-checked mathematical truth** (`0 errors`, `0 cheats`) in the **fewest tokens and wall-clock seconds**.

Locate the plugin root (`$LZ_ROOT`, the directory containing `scripts/lz.py`) and invoke:
```bash
LZ="python3 $LZ_ROOT/scripts/lz.py"
```

---

## Hard Red Lines (Never Violate)

1. **Zero Cheats:** Never add `assume(...)`, `admit()`, `#[verifier::external_body]`, `#[verifier::external*]`, `#[verifier::exec_allows_no_decreases_clause]`, `axiom fn`, `unimplemented!()`, `todo!()`, or `panic!()`.
2. **Immutable Contract:** Never weaken, delete, or rewrite any original function signature or `requires` / `ensures` / `recommends` clause.
3. **You MAY invent new clean helper lemmas** (`proof fn lemma_...`) — every helper lemma MUST itself verify cleanly with zero cheats.
4. **Never claim solved** until `$LZ check <file.rs> --original <orig.rs>` returns exit code `0` (`VERIFIED` + `anticheat=CLEAN`).

---

## The 4-Step Math-First Verification Protocol

### Step 1 — Slice & Diagnose First (Never `cat` a 4,000-Line File Blindly)

Run both commands in parallel on your first turn:
```bash
$LZ check <file.rs> --original <orig.rs> --rlimit 15
$LZ slice <file.rs> --target <target_fn> --depth 2 --max-lines 300
```
- `lz check` returns the exact failing line, Verus/Z3 error class, and next move in ~0.2–2s.
- `lz slice` returns ONLY the target function + reachable spec definitions with exact line numbers (`L4220..L4237`).
- If you need one extra helper or `spec fn` omitted from the slice, fetch it surgically with:
  ```bash
  $LZ show <file.rs> --symbol <symbol_name>
  ```

### Step 2 — Identify the Mathematical Object Before Editing

Consult [`references/verus-error-taxonomy.md`](references/verus-error-taxonomy.md) and [`references/math-first-lemma-patterns.md`](references/math-first-lemma-patterns.md).
Classify the failing obligation in 1 line before touching code:
- **Mechanical (`SYNTAX_OR_TYPE`, `TERMINATION_FAIL`):** Fix locally and re-run `lz check`.
- **Instantiation (`TRIGGER_INSTANTIATION`, `PRECONDITION_FAIL`):** Add the exact ground trigger, extensional equality (`=~=`), `reveal(...)`, or witness (`exists`).
- **Arithmetic (`NONLINEAR_ARITH`):** Isolate the pure inequality inside `assert(...) by (nonlinear_arith)` or `by (bit_vector)` with minimal hypotheses.
- **Structural Gap (`MISSING_LEMMA`, `RLIMIT_TIMEOUT`):** **STOP trial-and-error assertion spamming.** Go straight to Step 3.

### Step 3 — Bottom-Up Helper Lemma Synthesis

When a proof requires induction, multi-step state-machine reasoning, or algebraic rewriting:
1. Design a small DAG of **1 to 3 self-contained helper `proof fn` lemmas**, ordered bottom-up.
2. Add the lowest-level lemma `proof fn lemma_step_1(...)` directly above the target function.
3. **Verify that helper lemma in isolation first** (1–3 seconds, zero distraction from the rest of the file):
   ```bash
   $LZ verify-lemma <file.rs> --fn lemma_step_1 --rlimit 15
   ```
   - If `lemma_step_1` fails immediately, either its mathematical statement is **false** (strengthen `requires` / fix the invariant) or it needs a tiny sub-assertion. Never propagate a broken lemma into the target function!
4. Once every helper lemma passes `lz verify-lemma`, call them cleanly inside the target function body (using scoped `assert(...) by { ... }` blocks to prevent Z3 `rlimit` blowups). If a helper passes `lz verify-lemma` in isolation but fails only during full-file `lz check`, annotate the helper with `#[verifier::spinoff_prover]` or add the explicit missing arithmetic/base-case step.

### Step 4 — Final Gate (Anti-Cheat + Full File Verification)

Always verify the **entire** candidate file against the **original** file at full `--rlimit`:
```bash
$LZ check <file.rs> --original <orig.rs> --rlimit 30
```
Stop immediately when it prints `VERIFIED` and `anticheat=CLEAN`. Do not polish or re-run after passing.
