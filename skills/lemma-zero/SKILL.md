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

# lemma-zero — Math-First Proof Synthesis Engine for Verus Rust

Prove Verus Rust obligations **without human-provided helper lemmas** (`0 errors`, `0 cheats`) in the **fewest turns, tokens, and wall-clock seconds**.

Invoke the local engine directly via `python3 <plugin-root>/scripts/lz.py` (never rely on shell variables persisting across subshells).

## Hard Red Lines
1. **Zero Cheats:** Never add `assume(...)`, `admit()`, `#[verifier::external_body]`, `#[verifier(external_body)]`, `#[verifier::external*]`, `#[verifier(external*)]`, `#[verifier::assume_specification]`, `#[verifier::exec_allows_no_decreases_clause]`, `axiom fn`, `unimplemented!()`, `todo!()`, or `panic!()`.
2. **Immutable Contract & Specs:** Never alter original signatures, `requires`/`ensures`/`recommends`, `struct`/`enum`/`const`/`type`, or pre-existing `spec fn` bodies (including never removing `#[verifier::external_body]` / `unimplemented!()` from an uninterpreted spec).
3. **Clean Helpers Allowed:** Any newly added `proof fn` must verify cleanly with zero cheats.
4. **Final Gate:** Never stop until full-file `python3 <plugin-root>/scripts/lz.py check <file.rs> --original <orig.rs> --target <target_fn> --rlimit 30` prints `VERIFIED` and `anticheat=CLEAN`.

## Universal Math-First Protocol (Built for 1M-Context & Any Model)

### 1. Inspect & Diagnose Fast
- Read the file directly (or on multi-thousand-line files, optionally run `diag --target <target_fn>` / `slice` / `show --symbol s1,s2` as a fast jump-table to exact line numbers, existing `proof fn` lemmas, and `#[verifier::external_body]` stub warnings).
- Run `python3 <plugin-root>/scripts/lz.py check <file.rs> --original <orig.rs> --target <target_fn> --rlimit 15 --expand-errors` to get the exact failing conjunct, Verus/Z3 error class, and anti-cheat status in 1–3s without hanging Z3.
- **Need a `vstd` standard library lemma?** Never grep `vstd/` manually across turns — run ONE instant search:
  ```bash
  python3 <plugin-root>/scripts/lz.py vstd "filter no_duplicates"
  ```

### 2. Mode-Aware, Batch-First Proof Synthesis
Identify the mathematical object before editing (`=~=`, structural induction, trigger/witness, state-machine invariant, `by (nonlinear_arith)` / `by (bit_vector)`, or `reveal`). Consult `references/` **only if stuck**.
- **If target is an `exec fn`** (e.g. `pub fn ...(&mut self, ...)`): keep proofs concise and inline using `proof { ... }` blocks and existing file lemmas (avoid inventing unnecessary standalone helper `proof fn`s unless a pure math identity needs one).
- **If target is a `proof fn`** (induction, temporal leads-to, refinement): insert ALL needed helper `proof fn`s AND the target proof body in **one batched edit**, using scoped `assert(...) by { ... }` blocks.

### 3. Bounded Verification & Lazy Isolation
- Verify with `python3 <plugin-root>/scripts/lz.py check <file.rs> --original <orig.rs> --target <target_fn> --rlimit 30`.
- **Only if** verification is slow, hits `RLIMIT_TIMEOUT`, or a single obligation fails repeatedly:
  - Isolate the target function with `check --fn <target_fn> --expand-errors --rlimit 20 --timeout 25`, or isolate one failing helper with `verify-lemma --fn <helper> --rlimit 15`.
  - Use `#[verifier::spinoff_prover]` or scoped `assert(...) by { ... }` to isolate heavy Z3 queries.
- Stop immediately once full-file `check --rlimit 30` prints `VERIFIED` and `anticheat=CLEAN`.
