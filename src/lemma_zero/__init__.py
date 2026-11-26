"""lemma-zero: Math-First formal proof synthesis plugin for Verus Rust systems."""

from .anticheat import AntiCheatReport, Violation, verify_candidate
from .envcheck import check_environment
from .ledger import BudgetExceededError, CallUsage, ExperimentLedger, TaskRecord, summarize_ledger
from .slicer import show_symbol, slice_for_target
from .verus_runner import VerusError, VerusRunResult, find_verus_binary, run_verus

__all__ = [
    "AntiCheatReport",
    "BudgetExceededError",
    "CallUsage",
    "ExperimentLedger",
    "TaskRecord",
    "VerusError",
    "VerusRunResult",
    "Violation",
    "check_environment",
    "find_verus_binary",
    "run_verus",
    "show_symbol",
    "slice_for_target",
    "summarize_ledger",
    "verify_candidate",
]
