"""Local toolchain and disk-budget readiness checker."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .verus_runner import find_verus_binary

TOOLS = ("rustc", "cargo", "verus", "python3", "uv", "agy", "claude", "codex", "pi", "hermes", "gh")


def _tool_version(cmd: str) -> str | None:
    if cmd == "verus":
        found = find_verus_binary()
        path = str(found) if found else None
    elif cmd == "agy":
        on_path = shutil.which("agy")
        fallback = Path.home() / ".gemini" / "bin" / "agy"
        path = on_path or (str(fallback) if fallback.is_file() else None)
    else:
        path = shutil.which(cmd)
    if path is None:
        return None
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        out = [line.strip() for line in (proc.stdout or proc.stderr).strip().splitlines() if line.strip()]
        if cmd == "verus":
            for line in out:
                if line.lower().startswith("version:"):
                    return f"Verus {line.split(':', 1)[1].strip()} ({path})"
        return out[0] if out else path
    except Exception:
        return path


def check_environment(min_free_disk_gb: float = 20.0, disk_path: str | Path = "/") -> dict[str, Any]:
    """Inspect local machine readiness without downloading or modifying anything."""
    usage = shutil.disk_usage(disk_path)
    free_gb = round(usage.free / (1024**3), 2)
    tools = {cmd: _tool_version(cmd) for cmd in TOOLS}

    warnings: list[str] = []
    if free_gb < min_free_disk_gb:
        warnings.append(
            f"Free disk space is {free_gb} GB (below configured floor {min_free_disk_gb} GB). "
            "Avoid Docker image builds; install lightweight native Verus binary (~30 MB) after freeing space."
        )
    if tools["verus"] is None:
        warnings.append(
            "Verus binary not on PATH. Download native release from https://github.com/verus-lang/verus/releases "
            "or build pinned VeruSAGE-Bench commit ddc66116aa7a844a9e19cc50922fe85c84b8b4a5."
        )

    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "free_disk_gb": free_gb,
        "min_free_disk_gb": min_free_disk_gb,
        "disk_ok": free_gb >= min_free_disk_gb,
        "tools": tools,
        "ready_for_verus_runs": tools["verus"] is not None and tools["rustc"] is not None,
        "warnings": warnings,
    }
