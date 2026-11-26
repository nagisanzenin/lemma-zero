"""Real Verus + Z3 execution wrapper and structured diagnostic classifier."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]

_SUMMARY_RE = re.compile(r"verification results::\s*(\d+)\s+verified,\s*(\d+)\s+errors?")
_ERROR_HEADER_RE = re.compile(r"^error(?:\[(E\d+)\])?:\s*(.+)$")
_LOC_RE = re.compile(r"^\s*-->\s*([^:]+):(\d+):(\d+)\s*$")


@dataclass(frozen=True)
class VerusError:
    error_class: str
    code: str | None
    message: str
    line: int | None
    col: int | None
    snippet: str
    hint: str


@dataclass(frozen=True)
class VerusRunResult:
    verus_binary: str
    verified: bool
    verified_count: int
    error_count: int
    returncode: int
    timed_out: bool
    wall_seconds: float
    errors: tuple[VerusError, ...]
    raw_summary: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["errors"] = [asdict(e) for e in self.errors]
        return payload


def find_verus_binary(explicit: str | Path | None = None) -> Path | None:
    """Locate a working Verus binary (explicit -> env -> local vendor -> PATH)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_bin = os.environ.get("LEMMA_ZERO_VERUS")
    if env_bin:
        candidates.append(Path(env_bin))

    repo_root = Path(__file__).resolve().parents[2]
    candidates.extend(
        [
            repo_root / "vendor" / "verus-arm64-macos" / "verus",
            repo_root / "vendor" / "verus-x86-macos" / "verus",
            repo_root / "vendor" / "verus-x86-linux" / "verus",
            repo_root / "vendor" / "verus-arm64-linux" / "verus",
        ]
    )

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c.resolve()

    on_path = shutil.which("verus")
    return Path(on_path).resolve() if on_path else None


_GUTTER_RE = re.compile(r"^\s*(?:\d+|\.+)?\s*\|\s*(?:[/_\\|^-]+\s*)?")
_CARET_ONLY_RE = re.compile(r"^\s*[\^~_-]+(?:\s+.*)?$")
_NONLINEAR_OP_RE = re.compile(r"(?:\s[*/%]\s|\s<<\s|\s>>\s|\s&\s|\s\|\s)")
_TRIGGER_TOK_RE = re.compile(
    r"(?:\b(?:forall|exists|Seq|Map|Set|Multiset)\b|=~=|~~=|\.dom\(\)|\.contains(?:_key)?\()"
)


def _strip_diagnostic_gutter(snippet: str) -> str:
    """Remove rustc line-number gutters (`48 | /`) and underline markers before heuristic scanning."""
    cleaned: list[str] = []
    for raw_line in snippet.splitlines():
        line = _GUTTER_RE.sub("", raw_line).strip()
        if not line or _CARET_ONLY_RE.match(line):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def classify_error(code: str | None, message: str, snippet: str) -> tuple[str, str]:
    """Classify a single Verus/Rust error block into a deterministic recovery bucket."""
    low_msg = message.lower()
    code_only = _strip_diagnostic_gutter(snippet)
    low_code = code_only.lower()
    combined = f"{low_msg}\n{low_code}"

    if "rlimit" in combined or "resource limit" in combined or "timed out" in combined:
        return (
            "RLIMIT_TIMEOUT",
            "Isolate heavy sub-claims into `assert(...) by { ... }` blocks or `#[verifier::spinoff_prover]` helpers; pinpoint failing conjuncts via `lz check --fn <target> --expand-errors --rlimit 15`.",
        )

    if code is not None or any(
        k in low_msg
        for k in (
            "cannot find",
            "mismatched types",
            "expected ",
            "unresolved import",
            "cannot call",
            "mode",
            "borrow",
            "no method named",
            "not found in this scope",
            "syntax",
        )
    ):
        return (
            "SYNTAX_OR_TYPE",
            "Fix mechanical Rust/Verus syntax, import, or spec/proof/exec mode mismatch locally without altering original specs.",
        )

    if "decreases" in combined or "termination" in combined:
        return (
            "TERMINATION_FAIL",
            "Add an explicit `decreases <measure>` clause and prove strict decrease on recursive calls.",
        )

    if "arithmetic underflow/overflow" in low_msg:
        return (
            "NONLINEAR_ARITH",
            "Isolate pure arithmetic bounds into `assert(...) by (nonlinear_arith)` or `by (bit_vector)` with minimal `requires`.",
        )

    if "precondition not satisfied" in low_msg:
        return (
            "PRECONDITION_FAIL",
            "Prove the callee's failing `requires` clause immediately before the call (or strengthen an upstream helper lemma).",
        )

    if _NONLINEAR_OP_RE.search(code_only):
        if "postcondition" not in low_msg or re.search(r"(?:\s[*/%]\s|\s<<\s|\s>>\s)", code_only):
            return (
                "NONLINEAR_ARITH",
                "Isolate pure arithmetic bounds into `assert(...) by (nonlinear_arith)` or `by (bit_vector)` with minimal `requires`.",
            )

    if _TRIGGER_TOK_RE.search(code_only):
        return (
            "TRIGGER_INSTANTIATION",
            "Provide explicit ground trigger term, witness for `exists`, `assert(a =~= b)`, `reveal(...)`, or a scoped `assert forall ... by { ... }`.",
        )

    if "postcondition not satisfied" in low_msg or "assertion failed" in low_msg:
        return (
            "MISSING_LEMMA",
            "Stop local trial-and-error: identify the mathematical invariant and extract 1-2 standalone helper `proof fn` lemmas verified via `lz verify-lemma`.",
        )

    return (
        "MISSING_LEMMA",
        "Inspect the failing span and decompose into a smaller verifiable sub-obligation.",
    )


def parse_verus_errors(stderr: str, max_errors: int = 8) -> list[VerusError]:
    """Parse rustc/Verus diagnostic blocks from stderr into compact structured errors."""
    lines = stderr.splitlines()
    errors: list[VerusError] = []
    i = 0
    n = len(lines)

    while i < n and len(errors) < max_errors:
        m = _ERROR_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue

        code = m.group(1)
        message = m.group(2).strip()
        if message.startswith("aborting due to"):
            i += 1
            continue

        i += 1
        loc_line: int | None = None
        loc_col: int | None = None
        snippet_lines: list[str] = []

        while i < n:
            if _ERROR_HEADER_RE.match(lines[i]):
                break
            if lines[i].startswith("note: "):
                # Keep Verus expansion & related-location notes (pinpoint failing sub-conjuncts),
                # but stop at top-level verification summary notes.
                low_note = lines[i].lower()
                if any(
                    keep in low_note
                    for keep in ("diagnostics via expansion", "related location", "failed this", "recommendation")
                ):
                    if len(snippet_lines) < 18:
                        snippet_lines.append(lines[i].rstrip())
                    i += 1
                    continue
                if snippet_lines:
                    break
            loc_m = _LOC_RE.match(lines[i])
            if loc_m and loc_line is None:
                loc_line = int(loc_m.group(2))
                loc_col = int(loc_m.group(3))
            elif lines[i].strip() and len(snippet_lines) < 18:
                snippet_lines.append(lines[i].rstrip())
            i += 1

        snippet = "\n".join(snippet_lines)
        err_class, hint = classify_error(code, message, snippet)
        errors.append(
            VerusError(
                error_class=err_class,
                code=code,
                message=message,
                line=loc_line,
                col=loc_col,
                snippet=snippet,
                hint=hint,
            )
        )

    return errors


@contextlib.contextmanager
def _verus_slot_lock() -> Iterator[None]:
    """Bound concurrent local Verus/Z3 processes (default 6 via LEMMA_ZERO_MAX_VERUS) so high-worker LLM fan-outs never thrash CPU."""
    max_slots = int(os.environ.get("LEMMA_ZERO_MAX_VERUS", "6") or "6")
    if fcntl is None or max_slots <= 0:
        yield
        return
    slot_dir = Path(tempfile.gettempdir()) / "lemma_zero_verus_slots"
    try:
        slot_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield
        return
    # Round-robin start index by pid hash to spread contention cleanly across slots.
    start = int(hashlib.md5(str(os.getpid()).encode()).hexdigest(), 16) % max_slots
    acquired_fh = None
    try:
        # Fast non-blocking scan across all slots first.
        for offset in range(max_slots):
            slot_path = slot_dir / f"slot_{(start + offset) % max_slots}.lock"
            try:
                fh = slot_path.open("a+")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired_fh = fh
                break
            except BlockingIOError:
                fh.close()
            except OSError:
                break
        # If all slots are busy, block cleanly on our hashed slot.
        if acquired_fh is None:
            slot_path = slot_dir / f"slot_{start}.lock"
            try:
                fh = slot_path.open("a+")
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                acquired_fh = fh
            except OSError:
                acquired_fh = None
        yield
    finally:
        if acquired_fh is not None:
            try:
                fcntl.flock(acquired_fh.fileno(), fcntl.LOCK_UN)
            finally:
                acquired_fh.close()


def run_verus(
    file_path: Path | str,
    *,
    verus_binary: str | Path | None = None,
    rlimit: int = 30,
    timeout_seconds: int = 60,
    verify_function: str | None = None,
    verify_module: str | None = None,
    expand_errors: bool = False,
) -> VerusRunResult:
    """Execute the real Verus binary on a standalone .rs file and return structured diagnostics."""
    verus = find_verus_binary(verus_binary)
    if verus is None:
        raise FileNotFoundError(
            "Verus binary not found. Set LEMMA_ZERO_VERUS, place binary in vendor/verus-arm64-macos/verus, or add `verus` to PATH."
        )

    target = Path(file_path).resolve()
    source = target.read_text(encoding="utf-8")

    cmd: list[str] = [str(verus), "--rlimit", str(rlimit)]
    if "fn main(" not in source and "fn main (" not in source:
        cmd.append("--crate-type=lib")
    if expand_errors:
        cmd.append("--expand-errors")
    if verify_module:
        cmd.extend(["--verify-module", verify_module])
    if verify_function:
        cmd.extend(["--verify-function", verify_function])
        if not verify_module:
            cmd.append("--verify-root")
    cmd.append(str(target))

    with _verus_slot_lock():
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            wall = round(time.monotonic() - t0, 3)
            timed_out = False
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as exc:
            wall = round(time.monotonic() - t0, 3)
            timed_out = True
            stdout = (exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
            stderr = (exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
            rc = 124

    verified_count = 0
    error_count = 0
    raw_summary = ""
    combined = f"{stdout}\n{stderr}"
    m = _SUMMARY_RE.search(combined)
    if m:
        verified_count = int(m.group(1))
        error_count = int(m.group(2))
        raw_summary = m.group(0)

    parsed_errors = parse_verus_errors(stderr)
    if timed_out:
        parsed_errors.insert(
            0,
            VerusError(
                error_class="RLIMIT_TIMEOUT",
                code=None,
                message=f"Verus process exceeded timeout ({timeout_seconds}s).",
                line=None,
                col=None,
                snippet="",
                hint="Decompose proof into smaller isolated helper lemmas and verify bottom-up via `lz verify-lemma`.",
            ),
        )
        error_count = max(error_count, 1)

    verified = (rc == 0) and (not timed_out) and (error_count == 0) and (verified_count > 0)

    return VerusRunResult(
        verus_binary=str(verus),
        verified=verified,
        verified_count=verified_count,
        error_count=error_count,
        returncode=rc,
        timed_out=timed_out,
        wall_seconds=wall,
        errors=tuple(parsed_errors),
        raw_summary=raw_summary or f"exit={rc}",
    )
