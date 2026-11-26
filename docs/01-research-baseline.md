# 01 — Research Baseline (Step 0 Verified Findings)

*Audited directly against `microsoft/verus-proof-synthesis`, `verus-lang/verus`, and `Beneficial-AI-Foundation/dalek-verus` on 2026-11-26.*

---

## 1. VeruSAGE-Bench Overview

- **Repository:** `https://github.com/microsoft/verus-proof-synthesis`
- **Papers:** AutoVerus (`arXiv:2409.13082`, OOPSLA 2025) & VeruSAGE (`arXiv:2512.18436`)
- **Official Leaderboard:** `https://microsoft.github.io/verus-proof-synthesis/` (PR-based JSON submissions via `leaderboard/data/verusage-bench-results.json`, `last_updated: 2026-01-21`).
- **Pinned Verus Commit for `VeruSAGE-Bench`:** `ddc66116aa7a844a9e19cc50922fe85c84b8b4a5` (separate from `Verus-Bench` commit `33269ac6a0ea33a08109eefe5016c1fdd0ce9fbd`).
- **Standalone Execution:** 8 of 9 projects verify standalone with `verus --crate-type=lib <task>.rs`. Only `ST` (Storage, 63 full / 29 no-lemma) requires external crate dependencies from `source-projects/verified-storage`.

### Task Anatomy (Why Systems Verification Is Fundamentally Harder Than Toy Benchmarks)

| Per-Task Average | `Verus-Bench` (150 toy tasks) | `VeruSAGE-Bench` (849 systems tasks) |
| :--- | ---: | ---: |
| Total Lines of Code | ~30 | **~950** |
| Specification LoC | ~8 | **~500** |
| Proof LoC | ~10 | **~50** |
| Loop-invariant proof LoC | 8 | **1** |
| Non-loop-invariant proof LoC | 2 | **~50** |
| Number of loops | 1.6 | **< 0.1** |
| Number of helper lemmas | < 0.1 | **2.4** |

**Key Structural Insight:** Toy Verus benchmarks test loop invariants. Real-world `VeruSAGE-Bench` tasks almost never fail on loops (`< 0.1` loops/task) — they fail because the prover must reason over **~500 lines of algebraic/state-machine specifications** and chain **~2.4 non-trivial helper lemmas**.

---

## 2. The `tasks-no-lemma.jsonl` Frontier (460 Tasks)

`tasks-no-lemma.jsonl` contains the **460 of 849 tasks** whose original human proof relied on at least one helper lemma, with all human helper lemmas stripped out. There is **no single canonical `ground_truth`**; any non-cheating synthesized proof that Verus accepts counts as solved.

| Code | Project | Domain | Full (849) | No-Lemma (460) | Standalone Verus? |
| :--- | :--- | :--- | ---: | ---: | :--- |
| `AL` | Anvil (Library) | Distributed Systems | 104 | 83 | Yes |
| `AC` | Anvil (Advanced) | Distributed Systems | 63 | 58 | Yes |
| `IR` | IronKV | Key-Value Store | 118 | 30 | Yes |
| `MA` | Memory Allocator | Systems | 89 | 47 | Yes |
| `NO` | Node Replication | Distributed Systems | 29 | 5 | Yes |
| `NR` | NRKernel | OS Kernel | 204 | 133 | Yes |
| `OS` | ATMO | Microkernel | 157 | 71 | Yes |
| `ST` | Storage | Storage Systems | 63 | 29 | Needs crate deps |
| `VE` | Vest | Serialization | 22 | 4 | Yes |
| **Total** | **9 subsets / 8 projects** | | **849** | **460** | **431 standalone** |

---

## 3. Official Leaderboard Baselines (Full 849 With-Lemma Split)

Pulled verbatim from `leaderboard/data/verusage-bench-results.json`:

| Submission | Model | Solved / 849 | % | Avg Time | Avg Cost | Est. Full Cost | `AC` (63) | `NR` (204) | `OS` (157) |
| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `VeruSAGE (Hands-off)` | Sonnet 4.5 | 687 | 80.9% | 7.2 min | \$5.61 | \$4,763 | 23 (36.5%) | 151 (74.0%) | 130 (82.8%) |
| `VeruSAGE (Hands-on)` | Sonnet 4.5 | 573 | 67.5% | 6.9 min | \$1.47 | \$1,248 | 23 (36.5%) | 122 (59.8%) | 98 (62.4%) |
| `VeruSAGE (Hands-off)` | Sonnet 4 | 544 | 64.1% | 9.2 min | \$6.65 | \$5,646 | 15 (23.8%) | 113 (55.4%) | 96 (61.1%) |
| `VeruSAGE (Hands-on)` | Sonnet 4 | 491 | 57.8% | 7.1 min | \$1.72 | \$1,460 | 13 (23.8%) | 115 (56.4%) | 85 (54.1%) |
| `VeruSAGE (Hands-on)` | GPT-5 | 479 | 56.4% | 12.7 min | \$0.52 | \$441 | 17 (27.0%) | 98 (48.0%) | 81 (51.6%) |
| `VeruSAGE (Hands-off)` | GPT-5 | 425 | 50.1% | 3.7 min | \$1.30 | \$1,104 | 10 (15.9%) | 79 (38.7%) | 68 (43.3%) |

**Why this is an exceptional indie research wedge:**
1. Even *with* human helper lemmas, `AC` (Anvil Advanced) is only **36.5% solved** and `NR` (NRKernel) is **74.0%**, while Hands-off mode costs **\$5.61/task**!
2. On `tasks-no-lemma` (460 tasks), performance collapses drastically and **no official leaderboard submissions exist yet**.
3. The repository's own `utils/lynette` Rust parser + static anti-cheat checks provide a crisp, objective grading standard with zero subjectivity.

---

## 4. Phase 2 Upstream Real-World Target: `dalek-verus`

- **Repository:** `https://github.com/Beneficial-AI-Foundation/dalek-verus`
- **Base:** `signal-curve25519-4.1.3` (`signalapp/curve25519-dalek`), `serial/u64` backend, pure-Rust Curve25519 & Ristretto elliptic-curve cryptography used in Signal Messenger.
- **Why it fits Phase 2:** Once `lemma-zero` proves strong no-lemma synthesis and low cost per verified proof on `VeruSAGE-Bench`, applying it to unverified arithmetic/scalar/point functions in `dalek-verus` produces permanent, machine-checked proofs in live production cryptography — opening direct upstream recognition and proactive verification grant eligibility.

---

## 5. Local Machine Readiness (Apple M2, Audited 2026-11-26)

| Component | Status | Notes |
| :--- | :--- | :--- |
| CPU / RAM | Apple M2 / 16 GB | Sufficient for parallel local Verus/Z3 runs (2–4 workers). |
| Local Disk Free | **18 GiB free** | Below 20 GB safety floor — avoid Docker builds; use native Verus binary (~30 MB) + shallow/sparse data pulls. |
| `rustc` / `cargo` | Installed (`1.99.1`) | Ready. |
| `python3` / `uv` | Installed (`3.14.4` / `0.10.5`) | Ready. |
| `verus` / `z3` | Not yet installed | Official `verus-0.2026.11.22.1ce6a59-arm64-macos.zip` is **30 MB** (includes bundled Z3). Note: verify compatibility vs. pinned commit `ddc66116...` during Step 1 setup. |
