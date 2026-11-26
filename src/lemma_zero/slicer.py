"""Deterministic Rust/Verus item indexer and dependency-cone spec slicer.

Cuts multi-thousand-line VeruSAGE-Bench files (up to 4,200+ lines / ~40k tokens)
down to the exact target function and its reachable specification cone, while
keeping exact 1-based line coordinates for surgical edits.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any

from .anticheat import strip_comments_and_strings

_ITEM_RE = re.compile(
    r"""
    ^\s*
    (?:(?:\#\[[^\]]*\]\s*)*)
    (?:pub(?:\([^)]*\))?\s+)?
    (?:open\s+|closed\s+)?
    (?:uninterp\s+)?
    (?:broadcast\s+)?
    (?:proof\s+|spec\s+|exec\s+)?
    (?:unsafe\s+)?
    (?:const\s+)?
    (?P<kind>fn|struct|enum|type|trait|impl|mod|const|use|broadcast\s+group)\b
    """,
    re.VERBOSE,
)

_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_RUST_KEYWORDS = frozenset(
    {
        "fn", "pub", "open", "closed", "spec", "proof", "exec", "requires", "ensures",
        "recommends", "decreases", "let", "mut", "if", "else", "match", "for", "while",
        "loop", "return", "true", "false", "self", "Self", "struct", "enum", "impl",
        "trait", "type", "where", "use", "mod", "const", "assert", "by", "forall",
        "exists", "implies", "int", "nat", "bool", "usize", "u8", "u16", "u32", "u64",
        "i64", "isize", "Seq", "Set", "Map", "Multiset", "Option", "Result", "Some",
        "None", "Ok", "Err", "in", "as", "ref", "move", "verus", "broadcast", "reveal",
        "tracked", "ghost", "Ghost", "Tracked", "old", "old", "trigger", "choose",
    }
)


@dataclass(frozen=True)
class VerusItem:
    index: int
    kind: str
    name: str
    start_line: int
    end_line: int
    signature: str
    body: str
    identifiers: frozenset[str]

    def to_summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "lines": self.end_line - self.start_line + 1,
            "signature": self.signature,
        }


def _extract_item_name(kind: str, header_line: str) -> str:
    if kind == "impl":
        # Match `impl<...> Trait for Type` or `impl<...> Type`
        m_for = re.search(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)", header_line)
        if m_for:
            return m_for.group(1)
        m_impl = re.search(r"\bimpl(?:<[^>]*>)?\s+([A-Za-z_][A-Za-z0-9_]*)", header_line)
        return m_impl.group(1) if m_impl else "impl"
    m = re.search(rf"\b{re.escape(kind)}\s+([A-Za-z_][A-Za-z0-9_]*)", header_line)
    return m.group(1) if m else kind


def _brace_delta(clean_line: str) -> int:
    return clean_line.count("{") - clean_line.count("}")


def index_verus_items(source: str) -> list[VerusItem]:
    """Index top-level items (and methods inside impl blocks) with exact 1-based line spans."""
    raw_lines = source.splitlines()
    clean_lines = strip_comments_and_strings(source).splitlines()
    # Pad clean_lines in case trailing newline differed
    while len(clean_lines) < len(raw_lines):
        clean_lines.append("")

    items: list[VerusItem] = []
    n = len(raw_lines)
    brace_depth = 0
    verus_depth: int | None = None
    i = 0

    while i < n:
        c_line = clean_lines[i]
        stripped = c_line.strip()

        if verus_depth is None and re.search(r"\bverus!\s*\{", c_line):
            brace_depth += _brace_delta(c_line)
            verus_depth = brace_depth
            i += 1
            continue

        top_level_depth = verus_depth if verus_depth is not None else 0
        # Capture items at file top level, verus! top level, or direct methods inside impl blocks (+1).
        if brace_depth in {top_level_depth, top_level_depth + 1}:
            m = _ITEM_RE.match(c_line)
            if m:
                kind = m.group("kind")
                if kind in {"use", "mod"}:
                    brace_depth += _brace_delta(c_line)
                    i += 1
                    continue

                # Include leading attribute lines directly above item (e.g. `#[verifier::opaque]`)
                start = i
                while start > 0 and raw_lines[start - 1].lstrip().startswith("#["):
                    start -= 1

                # For `impl` blocks at top_level_depth, also descend inside so individual methods are indexed!
                if kind == "impl" and brace_depth == top_level_depth:
                    # Record impl block span by scanning ahead without consuming inner lines
                    impl_end = i
                    d = _brace_delta(c_line)
                    saw_open = "{" in c_line
                    j = i + 1
                    while j < n and (not saw_open or d > 0):
                        if "{" in clean_lines[j]:
                            saw_open = True
                        d += _brace_delta(clean_lines[j])
                        impl_end = j
                        j += 1
                    impl_body = "\n".join(raw_lines[start : impl_end + 1])
                    impl_clean = "\n".join(clean_lines[start : impl_end + 1])
                    name = _extract_item_name("impl", c_line)
                    items.append(
                        VerusItem(
                            index=len(items),
                            kind="impl",
                            name=name,
                            start_line=start + 1,
                            end_line=impl_end + 1,
                            signature=raw_lines[i].strip()[:140],
                            body=impl_body,
                            identifiers=frozenset(
                                tok for tok in _IDENT_RE.findall(impl_clean) if tok not in _RUST_KEYWORDS
                            ),
                        )
                    )
                    brace_depth += _brace_delta(c_line)
                    i += 1
                    continue

                # Non-impl item (or method inside impl): scan to end of block/semicolon
                end = i
                local_depth = _brace_delta(c_line)
                saw_open = "{" in c_line
                if (saw_open and local_depth <= 0) or (not saw_open and ";" in c_line):
                    end = i
                else:
                    j = i + 1
                    while j < n:
                        if "{" in clean_lines[j]:
                            saw_open = True
                        local_depth += _brace_delta(clean_lines[j])
                        end = j
                        if (saw_open and local_depth <= 0) or (not saw_open and ";" in clean_lines[j]):
                            break
                        j += 1

                body = "\n".join(raw_lines[start : end + 1])
                clean_body = "\n".join(clean_lines[start : end + 1])
                name = _extract_item_name(kind, c_line)
                items.append(
                    VerusItem(
                        index=len(items),
                        kind=kind,
                        name=name,
                        start_line=start + 1,
                        end_line=end + 1,
                        signature=raw_lines[i].strip()[:140],
                        body=body,
                        identifiers=frozenset(
                            tok for tok in _IDENT_RE.findall(clean_body) if tok not in _RUST_KEYWORDS
                        ),
                    )
                )
                for k in range(i, end + 1):
                    brace_depth += _brace_delta(clean_lines[k])
                i = end + 1
                continue

        brace_depth += _brace_delta(c_line)
        i += 1

    return items


def find_target_item(
    items: list[VerusItem],
    *,
    target_function: str | None = None,
    failing_line: int | None = None,
) -> VerusItem | None:
    """Locate the target function item by name or failing Verus line."""
    fn_items = [it for it in items if it.kind == "fn"]
    if target_function:
        exact = [it for it in fn_items if it.name == target_function]
        if exact:
            return exact[-1]
    if failing_line is not None:
        containing = [it for it in fn_items if it.start_line <= failing_line <= it.end_line]
        if containing:
            # Prefer innermost/smallest span
            return min(containing, key=lambda it: it.end_line - it.start_line)
    # Fallback: last non-main function in the file (typical in VeruSAGE-Bench single-file tasks)
    non_main = [it for it in fn_items if it.name != "main"]
    return non_main[-1] if non_main else None


def slice_for_target(
    source: str,
    *,
    target_function: str | None = None,
    failing_line: int | None = None,
    max_depth: int = 2,
    max_lines: int = 450,
) -> dict[str, Any]:
    """Extract a compact, dependency-ordered slice around target_function with exact line numbers."""
    total_lines = source.count("\n") + 1
    items = index_verus_items(source)
    # Exclude huge container `impl` items from direct slicing (individual methods are indexed cleanly)
    selectable = [it for it in items if it.kind != "impl"]
    target = find_target_item(selectable, target_function=target_function, failing_line=failing_line)

    if target is None:
        return {
            "total_lines": total_lines,
            "sliced_lines": min(total_lines, max_lines),
            "reduction_pct": 0.0,
            "target": None,
            "selected_items": [],
            "omitted_symbol_count": len(selectable),
            "context_text": "\n".join(
                f"{idx:4d} | {line}" for idx, line in enumerate(source.splitlines()[:max_lines], start=1)
            ),
        }

    if total_lines <= max_lines:
        numbered_full = "\n".join(
            f"{ln:4d} | {txt}" for ln, txt in enumerate(source.splitlines(), start=1)
        )
        available_proof_fns = [
            it
            for it in selectable
            if it.index != target.index
            and it.kind == "fn"
            and ("proof fn" in it.signature or "axiom fn" in it.signature or "broadcast" in it.signature)
        ]
        external_spec_stubs = [
            it
            for it in selectable
            if it.kind == "fn" and "spec fn" in it.signature and "external_body" in it.body
        ]
        blocks = [f"// === [full file: {total_lines} lines <= max_lines {max_lines}] ===\n{numbered_full}"]
        if external_spec_stubs:
            stub_lines = [
                f"// WARNING: L{it.start_line}: `{it.name}` is #[verifier::external_body] spec fn"
                for it in external_spec_stubs
            ]
            blocks.append("// === [Reachable external_body spec stubs] ===\n" + "\n".join(stub_lines))
        return {
            "total_lines": total_lines,
            "sliced_lines": total_lines,
            "reduction_pct": 0.0,
            "full_file": True,
            "target": target.to_summary(),
            "selected_items": [it.to_summary() for it in selectable],
            "available_proof_fns": [it.to_summary() for it in available_proof_fns[:40]],
            "frontier_omitted": [],
            "external_spec_stubs": [it.to_summary() for it in external_spec_stubs],
            "omitted_symbol_count": 0,
            "context_text": "\n\n".join(blocks),
        }

    by_name: dict[str, list[VerusItem]] = {}
    for it in selectable:
        by_name.setdefault(it.name, []).append(it)

    selected_indices: list[int] = [target.index]
    seen_indices: set[int] = {target.index}
    used_lines = target.end_line - target.start_line + 1

    queue: deque[tuple[VerusItem, int]] = deque([(target, 0)])
    while queue:
        curr, depth = queue.popleft()
        if depth >= max_depth:
            continue
        # Prioritize structs/enums/types and spec/proof defs referenced by curr
        referenced_items: list[VerusItem] = []
        for ident in sorted(curr.identifiers):
            for cand in by_name.get(ident, ()):
                if cand.index not in seen_indices and cand.name != "main":
                    referenced_items.append(cand)
        # Prefer compact definitions first so one giant unrelated function never eats the whole budget
        referenced_items.sort(key=lambda it: (it.end_line - it.start_line))
        for cand in referenced_items:
            cand_len = cand.end_line - cand.start_line + 1
            if used_lines + cand_len > max_lines:
                continue
            seen_indices.add(cand.index)
            selected_indices.append(cand.index)
            used_lines += cand_len
            queue.append((cand, depth + 1))

    selected_items = sorted((items[idx] for idx in selected_indices), key=lambda it: it.start_line)

    # Lossless navigation: list exact symbols directly referenced by selected_items that were omitted.
    frontier_indices: set[int] = set()
    for it in selected_items:
        for ident in it.identifiers:
            for cand in by_name.get(ident, ()):
                if cand.index not in seen_indices and cand.name != "main":
                    frontier_indices.add(cand.index)
    frontier_omitted = sorted((items[idx] for idx in frontier_indices), key=lambda it: it.start_line)

    # Surface existing non-target proof/axiom lemmas in the file so agents never miss provided library lemmas.
    available_proof_fns = [
        it
        for it in selectable
        if it.index != target.index
        and it.index not in seen_indices
        and it.kind == "fn"
        and ("proof fn" in it.signature or "axiom fn" in it.signature or "broadcast" in it.signature)
    ]

    rendered_blocks: list[str] = []
    for it in selected_items:
        header = f"// === [{it.kind} {it.name}] lines {it.start_line}..{it.end_line} ==="
        numbered = "\n".join(
            f"{ln:4d} | {txt}"
            for ln, txt in enumerate(it.body.splitlines(), start=it.start_line)
        )
        rendered_blocks.append(f"{header}\n{numbered}")

    if frontier_omitted:
        f_lines = [
            f"// L{it.start_line}..{it.end_line} [{it.kind} {it.name}]: {it.signature}"
            for it in frontier_omitted[:50]
        ]
        rendered_blocks.append(
            "// === [Referenced frontier symbols omitted (batch-fetch via `lz show --symbol s1,s2`)] ===\n"
            + "\n".join(f_lines)
        )

    if available_proof_fns:
        catalog_lines = [
            f"// L{it.start_line}..{it.end_line}: {it.signature}"
            for it in available_proof_fns[:40]
        ]
        rendered_blocks.append(
            "// === [Existing proof/broadcast lemmas in file (use `lz show --symbol s1,s2`)] ===\n"
            + "\n".join(catalog_lines)
        )

    external_spec_stubs = [
        it
        for it in selected_items
        if it.kind == "fn" and "spec fn" in it.signature and "external_body" in it.body
    ]
    if external_spec_stubs:
        stub_lines = [f"// WARNING: L{it.start_line}: `{it.name}` is #[verifier::external_body] spec fn" for it in external_spec_stubs]
        rendered_blocks.append("// === [Reachable external_body spec stubs] ===\n" + "\n".join(stub_lines))

    reduction_pct = round((1.0 - (used_lines / total_lines)) * 100.0, 1) if total_lines else 0.0

    return {
        "total_lines": total_lines,
        "sliced_lines": used_lines,
        "reduction_pct": max(0.0, reduction_pct),
        "full_file": False,
        "target": target.to_summary(),
        "selected_items": [it.to_summary() for it in selected_items],
        "available_proof_fns": [it.to_summary() for it in available_proof_fns[:40]],
        "frontier_omitted": [it.to_summary() for it in frontier_omitted[:50]],
        "external_spec_stubs": [it.to_summary() for it in external_spec_stubs],
        "omitted_symbol_count": max(0, len(selectable) - len(selected_items)),
        "context_text": "\n\n".join(rendered_blocks),
    }


def show_symbol(source: str, symbol: str) -> list[dict[str, Any]]:
    """Return exact line-numbered source for all definitions matching `symbol`."""
    items = index_verus_items(source)
    matches = [it for it in items if it.name == symbol and it.kind != "impl"]
    if not matches:
        matches = [it for it in items if it.name == symbol]
    results: list[dict[str, Any]] = []
    for it in matches:
        numbered = "\n".join(
            f"{ln:4d} | {txt}"
            for ln, txt in enumerate(it.body.splitlines(), start=it.start_line)
        )
        results.append({**it.to_summary(), "numbered_source": numbered})
    return results


def show_symbols(source: str, symbols: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Batch-fetch exact line-numbered sources for multiple symbols in one pass."""
    items = index_verus_items(source)
    by_name_non_impl: dict[str, list[VerusItem]] = {}
    by_name_all: dict[str, list[VerusItem]] = {}
    for it in items:
        by_name_all.setdefault(it.name, []).append(it)
        if it.kind != "impl":
            by_name_non_impl.setdefault(it.name, []).append(it)

    out: dict[str, list[dict[str, Any]]] = {}
    for sym in symbols:
        matches = by_name_non_impl.get(sym) or by_name_all.get(sym) or []
        out[sym] = [
            {
                **it.to_summary(),
                "numbered_source": "\n".join(
                    f"{ln:4d} | {txt}"
                    for ln, txt in enumerate(it.body.splitlines(), start=it.start_line)
                ),
            }
            for it in matches
        ]
    return out
