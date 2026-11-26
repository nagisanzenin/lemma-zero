"""Exact token, cache, wall-clock, and USD billing ledger with hard budget caps."""

from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class BudgetExceededError(RuntimeError):
    """Raised when an API call or task would exceed the configured USD budget cap."""


@dataclass(frozen=True)
class CallUsage:
    tier: str
    model: str
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    wall_seconds: float = 0.0

    def compute_cost_usd(self, pricing_table: dict[str, dict[str, float]]) -> float:
        if self.tier not in pricing_table:
            raise ValueError(f"Unknown pricing tier '{self.tier}'.")
        rates = pricing_table[self.tier]
        usd = (
            self.input_tokens * float(rates["input"])
            + self.cache_write_tokens * float(rates["cache_write"])
            + self.cache_read_tokens * float(rates["cache_read"])
            + self.output_tokens * float(rates["output"])
            + self.thinking_tokens * float(rates["thinking"])
        ) / 1_000_000.0
        return round(usd, 6)


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    project: str
    status: str  # SOLVED | FAILED | TIMEOUT_VERUS | REJECTED_CHEAT | BUDGET_EXCEEDED
    anticheat_clean: bool
    verus_verified: bool
    calls: int
    total_cost_usd: float
    wall_seconds: float
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    notes: str = ""


class ExperimentLedger:
    """Append-only JSONL ledger enforcing a hard USD budget cap."""

    def __init__(self, path: Path | str, budget_cap_usd: float, pricing_table: dict[str, dict[str, float]]) -> None:
        self.path = Path(path)
        self.budget_cap_usd = float(budget_cap_usd)
        self.pricing_table = pricing_table
        self._spent_usd = 0.0

        if self.path.exists():
            for rec in self.load_records(self.path):
                self._spent_usd += rec.total_cost_usd

    @property
    def spent_usd(self) -> float:
        return round(self._spent_usd, 6)

    @property
    def remaining_usd(self) -> float:
        return round(max(0.0, self.budget_cap_usd - self._spent_usd), 6)

    def check_budget(self, projected_additional_usd: float = 0.0) -> None:
        if self._spent_usd + projected_additional_usd > self.budget_cap_usd + 1e-9:
            raise BudgetExceededError(
                f"Projected spend ${self._spent_usd + projected_additional_usd:.4f} "
                f"exceeds budget cap ${self.budget_cap_usd:.4f}."
            )

    def record_task(self, record: TaskRecord) -> None:
        if record.status == "SOLVED" and (not record.anticheat_clean or not record.verus_verified):
            raise ValueError("Task cannot be recorded as SOLVED without both anticheat_clean and verus_verified.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")
        self._spent_usd += record.total_cost_usd

    @staticmethod
    def load_records(path: Path | str) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        with Path(path).open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                records.append(TaskRecord(**json.loads(line)))
        return records


def summarize_ledger(records: list[TaskRecord]) -> dict[str, Any]:
    """Compute rigorous benchmark and Pareto cost metrics across all recorded tasks."""
    total = len(records)
    solved = sum(1 for r in records if r.status == "SOLVED" and r.anticheat_clean and r.verus_verified)
    cheat_rejections = sum(1 for r in records if r.status == "REJECTED_CHEAT" or not r.anticheat_clean)
    total_cost = round(sum(r.total_cost_usd for r in records), 6)
    avg_cost = round(total_cost / total, 4) if total else 0.0
    cost_per_solved = round(total_cost / solved, 4) if solved else None
    wall_times = [r.wall_seconds for r in records]
    median_wall = round(statistics.median(wall_times), 2) if wall_times else 0.0
    mean_wall = round(sum(wall_times) / total, 2) if total else 0.0
    avg_calls = round(sum(r.calls for r in records) / total, 2) if total else 0.0
    total_input = sum(r.input_tokens for r in records)
    total_cache_read = sum(r.cache_read_tokens for r in records)
    total_output = sum(r.output_tokens for r in records)
    total_thinking = sum(r.thinking_tokens for r in records)
    avg_tokens = round((total_input + total_output + total_thinking) / total, 1) if total else 0.0

    by_project: dict[str, dict[str, Any]] = {}
    for rec in records:
        bucket = by_project.setdefault(rec.project, {"solved": 0, "total": 0, "cost_usd": 0.0})
        bucket["total"] += 1
        bucket["cost_usd"] = round(bucket["cost_usd"] + rec.total_cost_usd, 6)
        if rec.status == "SOLVED" and rec.anticheat_clean and rec.verus_verified:
            bucket["solved"] += 1

    for bucket in by_project.values():
        bucket["solve_rate_pct"] = round((bucket["solved"] / bucket["total"]) * 100.0, 2) if bucket["total"] else 0.0

    return {
        "total_tasks": total,
        "solved": solved,
        "solve_rate_pct": round((solved / total) * 100.0, 2) if total else 0.0,
        "cheat_rejections": cheat_rejections,
        "total_cost_usd": total_cost,
        "avg_cost_per_task_usd": avg_cost,
        "cost_per_solved_usd": cost_per_solved,
        "median_wall_seconds": median_wall,
        "mean_wall_seconds": mean_wall,
        "avg_calls_per_task": avg_calls,
        "total_input_tokens": total_input,
        "total_cache_read_tokens": total_cache_read,
        "total_output_tokens": total_output,
        "total_thinking_tokens": total_thinking,
        "avg_tokens_per_task": avg_tokens,
        "by_project": dict(sorted(by_project.items())),
    }
