"""Helpers shared by the stage-2 tests."""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fsu.heuristics import action_for
from fsu.models import Candidate, Category, Evidence
from fsu.protected import category_of


def make(path: Path, size: int = 10) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def candidate(path: Path, home: Path, score: float, matched: str | None = None) -> Candidate:
    category = category_of(path, home) or Category.CONFIG
    return Candidate(
        path=path,
        category=category,
        score=score,
        action=action_for(score),
        size_bytes=0,
        mtime=0.0,
        is_symlink=path.is_symlink(),
        matched_name=matched,
        evidence=(Evidence("test", "fixture"),),
    )


@contextmanager
def running(args: list[str], cwd: Path | None = None) -> Iterator[subprocess.Popen[bytes]]:
    """Run a helper process for the duration of a ``with`` block."""
    process = subprocess.Popen(args, cwd=cwd)
    try:
        # Wait until the process is visible in /proc with its final state.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if Path(f"/proc/{process.pid}/cwd").exists():
                break
            time.sleep(0.02)
        time.sleep(0.2)
        yield process
    finally:
        process.kill()
        process.wait()


def renamed_sleep(directory: Path, name: str) -> Path:
    """A copy of ``sleep`` with another name, so its process ``comm`` is ``name``."""
    sleep = shutil.which("sleep")
    assert sleep is not None
    target = directory / name
    shutil.copy2(sleep, target)
    target.chmod(0o755)
    return target
