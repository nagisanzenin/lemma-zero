"""Fast stdlib-only index and keyword search over Verus `vstd` library lemmas.

Eliminates 5-20 turns of manual `grep`/`sed` exploration inside `vstd/` across all
models and Verus codebases by returning exact `vstd::...` paths, signatures, and
`requires`/`ensures` clauses in ~0.05s with zero LLM calls.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .anticheat import strip_comments_and_strings
from .verus_runner import find_verus_binary

_DECL_RE = re.compile(
    r"""
    ^\s*
    (?:pub(?:\([^)]*\))?\s+)?
    (?P<quals>(?:(?:open|closed|uninterp|broadcast|proof|spec|exec|unsafe|const)\s+)*)
    (?P<kind>fn|broadcast\s+group)\s+
    (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)
_TOK_RE = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class VstdEntry:
    name: str
    kind: str  # proof_fn | broadcast_proof_fn | broadcast_group | spec_fn
    module_path: str
    rel_file: str
    line: int
    header: str
    search_blob: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("search_blob", None)
        return d


def find_vstd_dir(explicit: str | Path | None = None) -> Path | None:
    """Locate the Verus `vstd` source directory alongside the Verus binary."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_dir = os.environ.get("LEMMA_ZERO_VSTD")
    if env_dir:
        candidates.append(Path(env_dir))
    vbin = find_verus_binary()
    if vbin is not None:
        candidates.append(vbin.parent / "vstd")
    repo_root = Path(__file__).resolve().parents[2]
    for arch_dir in ("verus-arm64-macos", "verus-x86-macos", "verus-x86-linux", "verus-arm64-linux"):
        candidates.append(repo_root / "vendor" / arch_dir / "vstd")
    for c in candidates:
        if c.is_dir() and (c / "prelude.rs").is_file():
            return c.resolve()
    return None


def _rel_to_module(rel_path: Path) -> str:
    parts = list(rel_path.with_suffix("").parts)
    if parts and parts[-1] in {"mod", "vstd"}:
        parts.pop()
    return "vstd::" + "::".join(parts) if parts else "vstd"


def _extract_header(raw_lines: list[str], start_idx: int, max_lines: int = 12) -> str:
    collected: list[str] = []
    n = len(raw_lines)
    for j in range(start_idx, min(n, start_idx + max_lines)):
        line = raw_lines[j].rstrip()
        if j > start_idx and line.strip().startswith("{"):
            break
        if "{" in line and j == start_idx:
            collected.append(line.split("{", 1)[0].rstrip())
            break
        collected.append(line)
        if line.rstrip().endswith((";", "{")):
            if collected[-1].rstrip().endswith("{"):
                collected[-1] = collected[-1].rstrip()[:-1].rstrip()
            break
    return "\n".join(collected).strip()


@lru_cache(maxsize=4)
def index_vstd(vstd_dir_str: str) -> tuple[VstdEntry, ...]:
    """Parse all `.rs` files in `vstd` into searchable proof/broadcast/spec entries."""
    vstd_root = Path(vstd_dir_str)
    entries: list[VstdEntry] = []

    for rs_path in sorted(vstd_root.rglob("*.rs")):
        if "target" in rs_path.parts or rs_path.name == "build.rs":
            continue
        rel = rs_path.relative_to(vstd_root)
        mod_path = _rel_to_module(rel)
        try:
            raw = rs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        raw_lines = raw.splitlines()
        clean_lines = strip_comments_and_strings(raw).splitlines()
        while len(clean_lines) < len(raw_lines):
            clean_lines.append("")

        for idx, c_line in enumerate(clean_lines):
            if "fn " not in c_line and "broadcast group" not in c_line:
                continue
            m = _DECL_RE.match(c_line)
            if not m:
                continue
            quals = m.group("quals") or ""
            raw_kind = m.group("kind")
            name = m.group("name")

            if raw_kind.startswith("broadcast"):
                kind = "broadcast_group"
            elif "proof" in quals:
                kind = "broadcast_proof_fn" if "broadcast" in quals else "proof_fn"
            elif "spec" in quals:
                kind = "spec_fn"
            else:
                # Include exec std_specs / tracked methods (e.g. tracked_insert, leading_zeros)
                if "std_specs" in rel.parts or rel.name in {"map.rs", "seq.rs", "set.rs", "bits.rs"}:
                    kind = "exec_spec_fn"
                else:
                    continue

            header = _extract_header(raw_lines, idx, max_lines=10)
            blob = f"{name} {mod_path} {header}".lower()
            entries.append(
                VstdEntry(
                    name=name,
                    kind=kind,
                    module_path=f"{mod_path}::{name}",
                    rel_file=str(rel),
                    line=idx + 1,
                    header=header,
                    search_blob=blob,
                )
            )

    return tuple(entries)


def search_vstd(
    query: str,
    *,
    vstd_dir: str | Path | None = None,
    kind: str = "all",
    limit: int = 12,
) -> dict[str, Any]:
    """Search indexed `vstd` lemmas/specs by space-separated keywords."""
    root = find_vstd_dir(vstd_dir)
    if root is None:
        return {"vstd_dir": None, "query": query, "total_indexed": 0, "matches": []}

    entries = index_vstd(str(root))
    raw_terms = [t for t in _TOK_RE.findall(query.lower()) if t]
    # Also split snake_case terms into sub-parts so "no_duplicates" matches "no_dup" / "duplicates"
    expanded: list[str] = []
    for t in raw_terms:
        expanded.append(t)
        if "_" in t:
            expanded.extend(part for part in t.split("_") if len(part) >= 2)
    terms = list(dict.fromkeys(expanded))

    scored: list[tuple[float, VstdEntry]] = []
    for ent in entries:
        if kind == "proof" and ent.kind not in {"proof_fn", "broadcast_proof_fn", "broadcast_group"}:
            continue
        if kind == "spec" and ent.kind not in {"spec_fn", "exec_spec_fn"}:
            continue

        name_l = ent.name.lower()
        mod_l = ent.module_path.lower()
        score = 0.0
        matched_raw = 0
        for t in raw_terms:
            if t in ent.search_blob:
                matched_raw += 1
        if raw_terms and matched_raw == 0:
            # Fallback to sub-token overlap only if all raw terms are compound snake_case
            if not any(t in ent.search_blob for t in terms):
                continue

        for t in terms:
            if t == name_l:
                score += 10.0
            elif t in name_l:
                score += 4.0
            if t in mod_l:
                score += 2.0
            if t in ent.search_blob:
                score += 1.0

        # Boost exact multi-term coverage and proof/broadcast lemmas over plain spec fns.
        score += matched_raw * 3.0
        if ent.kind in {"broadcast_proof_fn", "broadcast_group"}:
            score += 1.5
        elif ent.kind == "proof_fn":
            score += 1.0

        if score > 0:
            scored.append((score, ent))

    scored.sort(key=lambda pair: (-pair[0], len(pair[1].name), pair[1].module_path))
    top = [ent.to_dict() for _, ent in scored[:limit]]
    return {
        "vstd_dir": str(root),
        "query": query,
        "total_indexed": len(entries),
        "matches": top,
    }
