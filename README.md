# lemma-zero

> **Multi-platform agent plugin for math-first formal proof synthesis in Verus Rust — with zero human-provided helper lemmas.**
> One skill + one stdlib-only local verifier engine (`scripts/lz.py`). Native installs on **Claude Code, Codex, Antigravity (`agy`), Pi, Hermes, and ZCode**.

Modern coding agents can fill in Verus proof lines when human experts hand them the helper lemmas. Strip those helper lemmas away (`VeruSAGE-Bench` `tasks-no-lemma.jsonl`, **460 real systems tasks**, up to **4,237 lines / ~40k tokens per file**), and frontier agents collapse on hard systems while burning \$5+ per task.

**`lemma-zero` does NOT reinvent an LLM wrapper CLI.** It turns *any* coding agent you already pay for into a disciplined Verus prover via two focused components:
1. **Deterministic Local Verifier Engine (`scripts/lz.py`, zero LLM calls):**
   - `lz slice` — cuts 4,000-line systems files down to the exact reachable spec cone with line numbers (`-70%` to `-95%` context reduction).
   - `lz show` — pulls one omitted symbol on demand.
   - `lz check` — runs the real `verus` + Z3 binary and parses errors into a compact 7-class actionable taxonomy.
   - `lz verify-lemma` — verifies a single proposed helper `proof fn` bottom-up in 1–3 seconds before touching the main proof.
   - `lz anticheat` — deterministic zero-cheat gate blocking `assume`, `admit`, `external_body`, `axiom fn`, panic stubs, and spec/signature tampering.
2. **Portable Math-First Skill (`skills/lemma-zero/SKILL.md`):** teaches any coding agent to name the mathematical object, synthesize a minimal bottom-up lemma DAG, verify each lemma in isolation, and close the target obligation with minimal billed tokens.

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
```

---

## Target Baselines (`VeruSAGE-Bench`, Verified 2026-11-26)

Official leaderboard: https://microsoft.github.io/verus-proof-synthesis/ (`last_updated: 2026-01-21`).

| System | Model | Split | Solved | Rate | Avg Time | Avg Cost | Hardest (`AC` Anvil Adv.) |
| :--- | :--- | :--- | ---: | ---: | ---: | ---: | ---: |
| `VeruSAGE (Hands-off)` | Sonnet 4.5 | Full 849 (with lemmas) | 687 / 849 | 80.9% | 7.2 min | \$5.61 | 23 / 63 (36.5%) |
| `VeruSAGE (Hands-on)` | Sonnet 4.5 | Full 849 (with lemmas) | 573 / 849 | 67.5% | 6.9 min | \$1.47 | 23 / 63 (36.5%) |
| `VeruSAGE (Hands-on)` | GPT-5 | Full 849 (with lemmas) | 479 / 849 | 56.4% | 12.7 min | \$0.52 | 17 / 63 (27.0%) |
| **`tasks-no-lemma.jsonl`** | **Open Frontier** | **460 no-lemma tasks** | **0 leaderboard entries** | — | — | — | **58 tasks (`AC`)** |

---

## Running Pre-Registered A/B Benchmarks

The benchmark harness (`bench/run_eval.py`) compares the **exact same coding agent** in `control` (bare `verus`) vs. `treatment` (`lemma-zero` plugin) on frozen stratified splits (`manifests/`):

```bash
# 1. Generate frozen stratified manifests (dev-20, heldout-pilot-60, heldout-full-411)
python3 bench/prepare_splits.py

# 2. Dry-run benchmark harness ($0, verifies Verus + agent CLI + manifest)
python3 bench/run_eval.py --manifest manifests/dev-20.json --mode treatment --agent claude --out runs/dev-dry.jsonl --dry-run

# 3. Live run (requires explicit owner-authorized --budget-cap-usd > 0)
python3 bench/run_eval.py --manifest manifests/dev-20.json --mode control   --agent claude --workers 4 --budget-cap-usd 10.0 --out runs/dev-control.jsonl
python3 bench/run_eval.py --manifest manifests/dev-20.json --mode treatment --agent claude --workers 4 --budget-cap-usd 10.0 --out runs/dev-treatment.jsonl
```

---

## Repository Layout

| Path | Role |
| :--- | :--- |
| `plugin.json`, `.claude-plugin/`, `.codex-plugin/`, `.agents/` | Native plugin manifests for Claude Code, Codex, Antigravity, Pi, Hermes, ZCode. |
| [`skills/lemma-zero/SKILL.md`](skills/lemma-zero/SKILL.md) | Portable Math-First No-Lemma verification skill + error taxonomy & lemma pattern references. |
| [`scripts/lz.py`](scripts/lz.py) | Stdlib-only local verifier engine (`selftest`, `slice`, `show`, `check`, `verify-lemma`, `anticheat`, `ledger-summary`). |
| [`src/lemma_zero/`](src/lemma_zero/) | Core Python modules (`slicer.py`, `verus_runner.py`, `anticheat.py`, `ledger.py`, `envcheck.py`). |
| [`bench/`](bench/) | Frozen split generator (`prepare_splits.py`) and headless A/B runner (`run_eval.py`). |
| [`manifests/`](manifests/) | Deterministic stratified manifests (`dev-20.json`, `heldout-pilot-60.json`, `heldout-full-411.json`, `annex-st-29.json`). |
| [`docs/`](docs/) | Verified Step 0 baseline, plugin architecture, and pre-registration protocol. |
| [`tests/test_core.py`](tests/test_core.py) | Unit and real-Verus integration tests. |
