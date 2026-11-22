"""Helpers shared by the stage-2 tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


Answer = Callable[[dict[str, Any]], tuple[int, Any]]


class FakeOllama:
    """In-process HTTP server that mimics the parts of the Ollama API FSU uses."""

    def __init__(self) -> None:
        self.models = ["qwen2.5:7b", "nomic-embed-text:latest"]
        # name -> extra fields, e.g. {"remote_host": "https://ollama.com:443"}
        self.remote: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self.answer: Answer = lambda body: (
            200,
            {"verdict": "uncertain", "confidence": 0.5, "rationale": "not enough data"},
        )
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, status: int, payload: Any) -> None:
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                fake.requests.append((self.path, None))
                if self.path == "/api/version":
                    self._send(200, {"version": "0.12.0"})
                elif self.path == "/api/tags":
                    entries = [{"name": m} for m in fake.models]
                    entries += [{"name": n, **extra} for n, extra in fake.remote.items()]
                    self._send(200, {"models": entries})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                fake.requests.append((self.path, body))
                status, answer = fake.answer(body)
                if isinstance(answer, bytes):
                    self._send(status, answer)
                else:
                    self._send(status, {"model": body["model"], "response": json.dumps(answer)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def generate_bodies(self) -> list[dict[str, Any]]:
        return [b for p, b in self.requests if p == "/api/generate" and b is not None]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
