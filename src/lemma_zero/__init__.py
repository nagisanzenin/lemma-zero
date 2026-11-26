"""lemma-zero: Math-First formal proof synthesis for Verus Rust systems."""

from .anticheat import AntiCheatReport, Violation, verify_candidate
from .envcheck import check_environment
from .ledger import BudgetExceededError, CallUsage, ExperimentLedger, TaskRecord, summarize_ledger

__all__ = [
    "AntiCheatReport",
    "BudgetExceededError",
    "CallUsage",
    "ExperimentLedger",
    "TaskRecord",
    "Violation",
    "check_environment",
    "summarize_ledger",
    "verify_candidate",
]
