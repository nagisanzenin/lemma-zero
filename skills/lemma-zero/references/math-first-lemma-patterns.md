# Math-First Lemma Discovery Patterns for Verus Systems

Real-world `VeruSAGE-Bench` files average ~950 LoC (~500 LoC specs, `< 0.1` loops, `2.4` helper lemmas). Almost all hard obligations reduce to one of six mathematical objects. Identify the object *before* writing proof lines:

## 1. Extensional Equality (`Seq`, `Map`, `Set`, `Multiset`)
- **Symptom:** Two collections are mathematically equal (`s1 == s2`, `m1 == m2`), yet Z3 rejects `s1 == s2`.
- **Math Object:** Axiom of Extensionality.
- **Minimal Move:** Use `assert(s1 =~= s2)` (or `assert(s1 =~= s2) by { ... }` proving `.len()` and pointwise equality for arbitrary `i` / key `k`). For nested structures, use `=~~=`.

## 2. Structural Induction on Sequences, Trees, Maps, or Executions
- **Symptom:** A property is defined over a recursive `spec fn` (`fold`, `filter`, `subrange`, `execution` trace, page-table walk, log prefix).
- **Math Object:** Well-founded induction on structure size (`s.len()`, `n`, `dom().len()`).
- **Minimal Move:** Write a standalone helper:
  ```rust
  proof fn lemma_<property>(...)
      requires ...
      ensures ...
      decreases <measure>,
  {
      if <base_case> {
      } else {
          lemma_<property>(<smaller>);
          // bridge one step cleanly
      }
  }
  ```
  Verify this helper in isolation using `lz verify-lemma` before calling it from the target function.

## 3. Quantifier Trigger Instantiation (`forall` / `exists`)
- **Symptom:** A `forall|x| P(x) ==> Q(x)` is in scope (often inside an invariant or `spec fn`), yet Z3 does not apply it to a specific term `t`, or an `exists|x| R(x)` fails to prove.
- **Math Object:** Universal instantiation / Existential witness introduction.
- **Minimal Move:**
  - For `forall`: explicitly mention the trigger term (e.g., `assert(P(t));` or call the `spec fn` appearing in `#[trigger]`).
  - For `exists`: explicitly construct the witness `w` and write `assert(R(w));` so Z3 closes `exists|x| R(x)`.
  - For proving a `forall|x| ...`: use `assert forall|x| P(x) implies Q(x) by { ... };`.

## 4. State-Machine & Distributed-System Invariants (Anvil / IronKV / ATMO / NRKernel)
- **Symptom:** Preserving a system invariant `inv(s')` across a step `next(s, s')` or proving a liveness/leads-to property (`p ~> q`).
- **Math Object:** Inductive transition system / Temporal logic leads-to chain.
- **Minimal Move:**
  1. Case-split explicitly on the transition action (`match step { ... }`) or `reveal` the top-level `next` / `inv` predicate.
  2. Prove sub-invariants inside isolated `assert(...) by { ... }` blocks so Z3 does not choke on all 15 conjuncts at once (`rlimit`).
  3. For Anvil temporal proofs (`always`, `eventually`, `leads_to`), chain intermediate state predicates `P ~> Q` and `Q ~> R` via small helper lemmas rather than one giant proof body.

## 5. Non-Linear & Bitvector Arithmetic (Memory Allocator, Page Tables, Curve25519)
- **Symptom:** Bounds involving alignment (`addr % page_size == 0`), limb arithmetic (`u64` carries in `dalek-verus`), or division/multiplication inequalities fail or time out.
- **Math Object:** Modular arithmetic / Bitvector algebra.
- **Minimal Move:** Isolate the pure arithmetic identity into a tiny `proof fn` or `assert(...) by (nonlinear_arith) requires ...;` with **zero** heap/struct/map context in scope. Verify in < 1 second, then instantiate in the main proof.

## 6. Opaque / Recursive Fuel Control
- **Symptom:** Z3 ignores the body of an `#[verifier::opaque]` spec function or only unrolls a recursive `spec fn` once.
- **Math Object:** Controlled definitional unfolding.
- **Minimal Move:** Call `reveal(fn_name);` or `reveal_with_fuel(rec_fn, 2);` strictly inside a scoped `assert(...) by { ... }` block so you do not pollute the outer Z3 context.
