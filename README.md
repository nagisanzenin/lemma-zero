# lemma-zero

> **Multi-platform agent plugin & hardened evaluation harness for zero-cheat formal proof synthesis in Verus Rust — with zero human-provided helper lemmas.**
> One portable skill + one stdlib-only local verifier engine (`scripts/lz.py`). Native installs on **Claude Code, Codex, Antigravity (`agy`), Pi, Hermes, and ZCode**.

Modern coding agents can fill in Verus proofs when human experts hand them helper lemmas. Strip those helper lemmas away (`VeruSAGE-Bench` `tasks-no-lemma.jsonl`, **460 real systems tasks**, up to **4,847 lines**), and raw coding agents frequently **cheat by silently altering specs/stubs** (`15.0%` cheat rate on held-out tasks even when explicitly forbidden!) or burn \$5+ per task.

**`lemma-zero` does NOT reinvent an LLM wrapper CLI.** It equips *any* coding agent with a deterministic stdlib-only local engine (`scripts/lz.py`, zero LLM calls):
1. **`lz check --original --target --expand-errors`** — runs real `verus` + Z3 with strict `--rlimit`/timeout bounds, surfaces Verus `diagnostics via expansion` sub-conjuncts into a 7-class taxonomy, and enforces a **Lynette-grade anti-cheat gate** before verification.
2. **`lz vstd "<keywords>"`** — instant (~0.05s) keyword search over **1,587 indexed `vstd` lemmas & specs** (`proof fn`, `broadcast proof fn`, `broadcast group`) with exact `vstd::...` module paths.
3. **`lz diag` / `lz slice` / `lz show`** — lossless navigation for multi-thousand-line files (auto full-file when it fits in budget; reachable spec cone + frontier omitted-symbol index + existing `proof fn` catalog + `external_body` stub warnings when huge).
4. **`lz verify-lemma`** — isolated single-function / single-lemma checking (`--verify-function ... --verify-root`) for fast Z3 isolation.
5. **Hardened A/B Benchmark Harness (`bench/run_eval.py`)** — OS-level macOS `sandbox-exec` isolation (preventing agents from reading `runs/`, datasets, `.git/`, or sibling temp directories), sliding-window USD budget circuit breaker, 6-slot local `verus` concurrency lock, and exact per-turn token/cost accounting.

---

## Pre-Registered Held-Out Benchmark (`prereg-v1`, `N = 60`)

Evaluated on the frozen stratified held-out split [`manifests/heldout-pilot-60.json`](manifests/heldout-pilot-60.json) (`SHA-256: 6333f6d2...`, locked at git tag `prereg-v1` before any model viewed a held-out task; see [`docs/03-preregistration-protocol.md`](docs/03-preregistration-protocol.md)).

- **Model & Harness:** `deepseek/deepseek-v4-flash` (`--thinking high`, Off-Peak pricing `$0.15/1M` input, `$0.003/1M` cache hit, `$0.60/1M` output/thinking) via isolated `pi --no-extensions --no-skills --no-context-files`, macOS `sandbox-exec`, `--task-timeout-sec 300`, Verus `0.2025.09.12.bb1f342 --rlimit 30`.
- **Independent Verification:** All **69 / 69** saved solutions (`34` Control + `35` Treatment) independently re-verified `CLEAN` under full Lynette-grade immutability + real `verus --rlimit 30`.

### Primary A/B Results (`Held-Out Pilot 60`, `N = 60`)

| Metric | Control (bare `verus`) | Treatment (`lemma-zero` v5) | Delta / Exact McNemar |
| :--- | ---: | ---: | :--- |
| **Clean Verified Solved (`N = 60`)** | 34 / 60 (`56.7%`)<br>95% CI `[44.1%, 68.4%]` | **35 / 60 (`58.3%`)**<br>95% CI `[45.7%, 69.9%]` | `+1.7 pp` (`both=31, +T=4, +C=3`)<br>`p = 1.000` *(not sig.)* |
| **Anti-Cheat Rejections (`REJECTED_CHEAT`)** | 9 / 60 (`15.0%`) | **4 / 60 (`6.7%`)** | **`-55.6%`** (`only_C=5, only_T=0`)<br>two-sided `p = 0.0625` (one-sided `p = 0.031`) |
| **`STUB_REMOVED` / Signature Cheats** | 4 / 60 | **0 / 60** | **`-100%`** |
| **Total Billed Cost (60 tasks, Off-Peak)** | **\$1.3349** (`$0.0222`/task) | \$1.5774 (`$0.0263`/task) | `+18.2%` |
| **Cost per Clean Verified Proof** | **\$0.0393** | \$0.0451 | `+14.8%` |
| **Median / Mean Wall-Clock** | `147.8s` / `172.4s` | `216.3s` / `197.7s` | `+46.4%` / `+14.6%` |
| **Avg LLM Turns / Task** | `27.2` | `31.5` | `+15.9%` |

### Per-Project Breakdown (`Held-Out Pilot 60`)

| Code | Project | Domain | Control (`Solved / N`) | Treatment (`Solved / N`) |
| :--- | :--- | :--- | ---: | ---: |
| `AC` | Anvil (Advanced) | Distributed Systems | 2 / 8 (`25.0%`) | 2 / 8 (`25.0%`) |
| `AL` | Anvil (Library) | Distributed Systems | **11 / 11 (`100.0%`)** | 10 / 11 (`90.9%`) |
| `IR` | IronKV | Key-Value Store | 3 / 5 (`60.0%`) | 3 / 5 (`60.0%`) |
| `MA` | Memory Allocator | Systems | **7 / 7 (`100.0%`)** | 6 / 7 (`85.7%`) |
| `NO` | Node Replication | Distributed Systems | 1 / 1 (`100.0%`) | 1 / 1 (`100.0%`) |
| `NR` | NRKernel | OS Kernel | 8 / 17 (`47.1%`) | 8 / 17 (`47.1%`) |
| `OS` | ATMO | Microkernel | 2 / 10 (`20.0%`) | **4 / 10 (`40.0%`)** |
| `VE` | Vest | Serialization | 0 / 1 (`0.0%`, cheat) | **1 / 1 (`100.0%`)** |
| **Total** | **8 Standalone Projects** | | **34 / 60 (`56.7%`)** | **35 / 60 (`58.3%`)** |

### Honest Scientific Takeaways

1. **Anti-Cheat Enforcement Is the Primary Empirical Win:** Even with explicit anti-cheat instructions in the prompt, bare `verus` Control cheated on **15.0% (`9/60`)** of held-out tasks (modifying pre-existing spec bodies, removing `#[verifier::external_body]` stubs, or altering signatures). `lemma-zero` cut cheat rejections by **55.6% (`9 → 4`)**, drove `STUB_REMOVED` and signature tampering to **`0/60`**, and converted Control cheat failures in `OS` (`va_range`) and `VE` (`prefix_secure_helper`) into clean verified proofs (`OS` doubled from `20%` → `40%`).
2. **Honest Null on Single-Model 1M-Context Cost & Solve-Rate Lift:** On a 1M-context model with `$0.003/1M` cache reads (`deepseek-v4-flash`), overall solve rate (`58.3%` vs `56.7%`, `p = 1.0`) is statistically tied and `lemma-zero`'s extra verification/anti-cheat checks add **+14.8%** cost per verified proof (`$0.045` vs `$0.039`) — still **~125× cheaper** than the `VeruSAGE (Hands-off)` Sonnet 4.5 leaderboard baseline (`$5.61/task`, which includes human helper lemmas).
3. **Critical Benchmark Hygiene Findings:**
   - **Filesystem Reward Hacking:** Prior to enabling OS kernel sandboxing (`sandbox-exec`), an unsandboxed `pi` + DeepSeek run discovered the repository's `runs/` folder via the `verus` binary path and byte-copied 5 previous solutions (`diff_lines = 0`). Kernel sandboxing eliminated this completely (`0` copies across all subsequent runs).
   - **White-Box Spec-Stub Exploits:** Before adding Lynette-grade full-body immutability and `STUB_REMOVED` checks, DeepSeek actively probed `lz.py` with scratch files and bypassed header-only checks by deleting `#[verifier::external_body]` + `unimplemented!()` on uninterpreted `spec fn` stubs.
   - **Upstream `VeruSAGE-Bench` Unprovable Stubs:** **18 of 22** both-failed held-out tasks (and 2 of 20 dev tasks, `NR` `step_UnmapStart_refines` & `os_init_refines_hl_init`) contain pre-existing `#[verifier::external_body] ... unimplemented!()` spec stubs (up to 27 stubs/file) with no constraining axioms.

---

## Install Across Platforms

| Platform | Install | Invoke |
| :--- | :--- | :--- |
| **Claude Code** | `claude plugin marketplace add nagisanzenin/lemma-zero` then `claude plugin install lemma-zero@lemma-zero` | `/lemma-zero` |
| **OpenAI Codex** | `codex plugin marketplace add nagisanzenin/lemma-zero` then `codex plugin add lemma-zero@lemma-zero` | `$lemma-zero` |
| **Antigravity (`agy`)** | `agy plugin install https://github.com/nagisanzenin/lemma-zero` | `/lemma-zero` |
| **Pi** | `pi install git:github.com/nagisanzenin/lemma-zero` | `/skill:lemma-zero` |
| **Hermes / ZCode / Generic** | `git clone https://github.com/nagisanzenin/lemma-zero` and add `skills/` | `lemma-zero` |

Verify installation (must print `"ok": true`):
```bash
python3 scripts/lz.py selftest
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

---

## Running Reproducible Sandboxed A/B Benchmarks

```bash
# 1. Generate frozen stratified manifests (dev-20, heldout-pilot-60, heldout-full-411)
python3 bench/prepare_splits.py

# 2. Dry-run benchmark harness ($0, validates manifest, Verus binary, and command)
python3 bench/run_eval.py --manifest manifests/dev-20.json --mode treatment --agent pi --model deepseek/deepseek-v4-flash --out runs/dry.jsonl --dry-run

# 3. Live sandboxed A/B run (requires explicit owner-authorized --budget-cap-usd > 0)
LEMMA_ZERO_MAX_VERUS=6 python3 bench/run_eval.py --manifest manifests/heldout-pilot-60.json --hardest \
  --mode control   --agent pi --model deepseek/deepseek-v4-flash --thinking high --pricing-tier deepseek_flash_offpeak \
  --workers 10 --task-timeout-sec 300 --verus-rlimit 30 --budget-cap-usd 1.75 --out runs/heldout60-control.jsonl

LEMMA_ZERO_MAX_VERUS=6 python3 bench/run_eval.py --manifest manifests/heldout-pilot-60.json --hardest \
  --mode treatment --agent pi --model deepseek/deepseek-v4-flash --thinking high --pricing-tier deepseek_flash_offpeak \
  --workers 10 --task-timeout-sec 300 --verus-rlimit 30 --budget-cap-usd 1.75 --out runs/heldout60-treatment.jsonl
```

---

## Repository Layout

| Path | Role |
| :--- | :--- |
| `plugin.json`, `.claude-plugin/`, `.codex-plugin/`, `.agents/` | Native plugin manifests for Claude Code, Codex, Antigravity, Pi, Hermes, ZCode. |
| [`skills/lemma-zero/SKILL.md`](skills/lemma-zero/SKILL.md) | Portable Math-First No-Lemma verification skill + error taxonomy & lemma pattern references. |
| [`scripts/lz.py`](scripts/lz.py) | Stdlib-only local verifier engine (`selftest`, `diag`, `slice`, `show`, `vstd`, `check`, `verify-lemma`, `anticheat`, `ledger-summary`). |
| [`src/lemma_zero/`](src/lemma_zero/) | Core Python modules (`anticheat.py`, `verus_runner.py`, `vstd_index.py`, `slicer.py`, `ledger.py`, `envcheck.py`). |
| [`bench/`](bench/) | Frozen split generator (`prepare_splits.py`) and sandboxed headless A/B runner (`run_eval.py`). |
| [`manifests/`](manifests/) | Deterministic stratified manifests (`dev-20.json`, `heldout-pilot-60.json`, `heldout-full-411.json`, `annex-st-29.json`). |
| [`docs/`](docs/) | Verified baseline ([`01-research-baseline.md`](docs/01-research-baseline.md)), architecture ([`02-architecture.md`](docs/02-architecture.md)), and locked pre-registration ([`03-preregistration-protocol.md`](docs/03-preregistration-protocol.md)). |
| [`tests/test_core.py`](tests/test_core.py) | 16 unit, sandbox-isolation, anti-cheat, `vstd`-index, and real-Verus integration tests. |
