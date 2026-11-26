"""Deterministic static anti-cheat and spec-preservation verifier for Verus Rust proofs.

Guarantees that synthesized proofs do NOT cheat by introducing `assume`, `admit`,
`external_body` / external verifier pragmas, panic stubs, or modifying original
function signatures and `requires`/`ensures`/`recommends` specifications.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

# Cheat patterns scanned on comment- and string-stripped Verus source.
FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "ASSUME_CALL",
        re.compile(r"\bassume\s*\("),
        "Introduces `assume(...)`, bypassing mathematical proof.",
    ),
    (
        "ADMIT_CALL",
        re.compile(r"\badmit\s*\("),
        "Introduces `admit()`, leaving an unproven proof obligation.",
    ),
    (
        "EXTERNAL_BODY_ATTR",
        re.compile(r"#\s*!?\s*\[\s*verifier\s*::\s*external_body\b"),
        "Introduces `#[verifier::external_body]`, skipping body verification.",
    ),
    (
        "EXTERNAL_ATTR",
        re.compile(r"#\s*!?\s*\[\s*verifier\s*::\s*external(?:_fn_specification|_type_specification|_trait_specification)?\b"),
        "Introduces `#[verifier::external*]` attribute.",
    ),
    (
        "NO_DECREASES_ATTR",
        re.compile(r"#\s*!?\s*\[\s*verifier\s*::\s*exec_allows_no_decreases_clause\b"),
        "Introduces `#[verifier::exec_allows_no_decreases_clause]`, bypassing termination check.",
    ),
    (
        "AXIOM_DECL",
        re.compile(r"\baxiom\s+fn\b"),
        "Introduces an unproved `axiom fn` declaration.",
    ),
    (
        "PANIC_STUB",
        re.compile(r"\b(?:unimplemented|todo|unreachable|panic)\s*!\s*\("),
        "Introduces a runtime abort/stub macro (`unimplemented!`, `todo!`, `unreachable!`, `panic!`).",
    ),
)

# Captures function headers up to the opening `{` or `;`, including spec clauses.
_FN_HEADER_RE = re.compile(
    r"""
    (?P<qualifiers>(?:pub(?:\([^)]*\))?\s+)?(?:open\s+|closed\s+)?(?:uninterp\s+)?(?:broadcast\s+)?(?:proof\s+|spec\s+|exec\s+)?(?:unsafe\s+)?(?:const\s+)?)
    fn\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Violation:
    code: str
    message: str
    detail: str = ""


@dataclass(frozen=True)
class AntiCheatReport:
    clean: bool
    violations: tuple[Violation, ...]
    checked_original_functions: int
    new_helper_functions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["violations"] = [asdict(v) for v in self.violations]
        return payload


def strip_comments_and_strings(source: str) -> str:
    """Remove Rust/Verus comments (including nested `/* */`) and string literals while keeping structure."""
    out: list[str] = []
    i = 0
    n = len(source)
    block_depth = 0
    in_line_comment = False
    in_string = False

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
                out.append("\n")
            i += 1
            continue

        if block_depth > 0:
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 2
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 2
            else:
                if ch == "\n":
                    out.append("\n")
                i += 1
            continue

        if in_string:
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append('""')
            i += 1
            continue

        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue

        if ch == "/" and nxt == "*":
            block_depth = 1
            i += 2
            continue

        if ch == '"':
            in_string = True
            i += 1
            continue

        # Character literal like '\'' or 'a' (avoid Rust lifetimes like `'a`).
        if ch == "'" and i + 2 < n:
            if source[i + 1] == "\\" and i + 3 < n and source[i + 3] == "'":
                out.append("''")
                i += 4
                continue
            if source[i + 1] != "'" and source[i + 2] == "'":
                out.append("''")
                i += 3
                continue

        out.append(ch)
        i += 1

    return "".join(out)


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_function_headers(stripped_source: str) -> dict[str, list[str]]:
    """Extract normalized function signatures + spec clauses (up to body `{` or `;`) keyed by function name."""
    headers: dict[str, list[str]] = {}
    n = len(stripped_source)

    for match in _FN_HEADER_RE.finditer(stripped_source):
        name = match.group("name")
        start = match.start()
        idx = match.end()

        # Skip optional generic parameter list `<...>` immediately following `fn name`.
        while idx < n and stripped_source[idx].isspace():
            idx += 1
        if idx < n and stripped_source[idx] == "<":
            angle_depth = 1
            idx += 1
            while idx < n and angle_depth > 0:
                if stripped_source[idx] == "<":
                    angle_depth += 1
                elif stripped_source[idx] == ">":
                    angle_depth -= 1
                idx += 1

        # Scan parameter list, return type, and Verus spec clauses (`requires`/`ensures`/`recommends`/`decreases`)
        # without treating `<` / `>` comparison operators inside specs as generic brackets.
        paren_depth = 0
        bracket_depth = 0
        header_end = n

        while idx < n:
            ch = stripped_source[idx]
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif ch == "[":
                bracket_depth += 1
            elif ch == "]":
                bracket_depth = max(0, bracket_depth - 1)
            elif paren_depth == 0 and bracket_depth == 0 and ch in {"{", ";"}:
                header_end = idx
                break
            idx += 1

        normalized = _normalize_ws(stripped_source[start:header_end])
        headers.setdefault(name, []).append(normalized)

    return headers


def verify_candidate(original_source: str, candidate_source: str) -> AntiCheatReport:
    """Compare candidate Verus source against original task source for cheating or spec tampering."""
    orig_clean = strip_comments_and_strings(original_source)
    cand_clean = strip_comments_and_strings(candidate_source)

    violations: list[Violation] = []

    # 1. Count-based check on forbidden cheat constructs (allows pre-existing ones in original harness,
    #    strictly forbids introducing ANY new occurrence).
    for code, pattern, message in FORBIDDEN_PATTERNS:
        orig_count = len(pattern.findall(orig_clean))
        cand_count = len(pattern.findall(cand_clean))
        if cand_count > orig_count:
            violations.append(
                Violation(
                    code=code,
                    message=message,
                    detail=f"count increased from {orig_count} to {cand_count}",
                )
            )

    # 2. Check that every original function signature & spec clause (`requires`/`ensures`/`recommends`)
    #    remains intact in the candidate file.
    orig_headers = extract_function_headers(orig_clean)
    cand_headers = extract_function_headers(cand_clean)

    for fn_name, orig_list in orig_headers.items():
        cand_list = cand_headers.get(fn_name, [])
        orig_bag = Counter(orig_list)
        cand_bag = Counter(cand_list)
        missing = orig_bag - cand_bag
        if missing:
            sample = next(iter(missing.keys()))
            violations.append(
                Violation(
                    code="SPEC_OR_SIGNATURE_MODIFIED",
                    message=f"Original function `{fn_name}` signature or specification clause was altered or removed.",
                    detail=sample[:180],
                )
            )

    new_helpers = sorted(name for name in cand_headers if name not in orig_headers)

    return AntiCheatReport(
        clean=len(violations) == 0,
        violations=tuple(violations),
        checked_original_functions=sum(len(v) for v in orig_headers.values()),
        new_helper_functions=tuple(new_helpers),
    )
