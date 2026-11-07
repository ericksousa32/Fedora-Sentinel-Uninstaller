from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from fsu.ai_engine import (
    RESPONSE_SCHEMA,
    AiEngine,
    AiUnavailableError,
    OllamaClient,
    Verdict,
    choose_model,
    describe,
)
from fsu.models import Action
from helpers import candidate, make

Answer = Callable[[dict[str, Any]], tuple[int, Any]]


class FakeOllama:
    """In-process HTTP server that mimics the parts of the Ollama API FSU uses."""

    def __init__(self) -> None:
        self.models = ["qwen2.5:7b", "nomic-embed-text:latest"]
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
                    self._send(200, {"models": [{"name": m} for m in fake.models]})
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


@pytest.fixture
def ollama() -> Iterator[FakeOllama]:
    fake = FakeOllama()
    yield fake
    fake.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# -- client -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.0.10:11434",
        "http://ollama.example.com",
        "http://10.0.0.1",
        "ftp://127.0.0.1",
        "not a url",
    ],
)
def test_non_local_addresses_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match=r"non-local|invalid"):
        OllamaClient(url)


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1", "http://[::1]:1"])
def test_loopback_addresses_are_accepted(url: str) -> None:
    OllamaClient(url)


def test_proxies_are_ignored(ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(variable, "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert OllamaClient(ollama.url).version() == "0.12.0"


def test_service_down() -> None:
    client = OllamaClient(f"http://127.0.0.1:{_free_port()}", timeout=2)
    with pytest.raises(AiUnavailableError, match="cannot reach"):
        client.version()


@pytest.mark.parametrize(
    ("installed", "preferred", "chosen"),
    [
        (["mistral:7b", "qwen2.5:7b"], None, "qwen2.5:7b"),
        (["nomic-embed-text:latest", "llama3.1:8b"], None, "llama3.1:8b"),
        (["nomic-embed-text:latest"], None, None),
        (["custom-model:1b"], None, "custom-model:1b"),
        (["mistral:latest"], "mistral", "mistral:latest"),
        (["mistral:latest"], "llama3", None),
        ([], None, None),
    ],
)
def test_choose_model(installed: list[str], preferred: str | None, chosen: str | None) -> None:
    assert choose_model(installed, preferred) == chosen


# -- describe -----------------------------------------------------------------------


def test_describe_sends_names_never_contents(home: Path) -> None:
    item = home / ".local/share/mystery"
    make(item / "notes.txt")
    (item / "notes.txt").write_text("SECRET-CONTENT")
    make(item / "sub/x")
    data = describe(candidate(item, home, 0.20), now=time.time())
    assert data["first_level_entries"] == ["notes.txt", "sub/"]
    assert data["location"] == "~/.local/share"
    assert "SECRET-CONTENT" not in json.dumps(data)


def test_describe_sanitizes_names(home: Path) -> None:
    item = home / ".config/evil\x1b[31mname"
    make(item / ("x" * 200))
    data = describe(candidate(item, home, 0.20), now=time.time())
    assert "\x1b" not in data["name"]
    assert len(data["first_level_entries"][0]) <= 80


# -- review -------------------------------------------------------------------------


@pytest.fixture
def items(home: Path) -> list[Path]:
    paths = [home / ".config/high", home / ".config/medium", home / ".cache/low"]
    for path in paths:
        make(path / "a")
    return paths


def _review(ollama: FakeOllama, home: Path, paths: list[Path], scores: list[float]) -> Any:
    engine = AiEngine(OllamaClient(ollama.url, timeout=5))
    cands = [candidate(p, home, s) for p, s in zip(paths, scores, strict=True)]
    return engine.review(cands)


def test_high_confidence_items_are_never_sent(
    ollama: FakeOllama, home: Path, items: list[Path]
) -> None:
    review = _review(ollama, home, items, [0.85, 0.57, 0.20])
    assert review.available
    assert review.model == "qwen2.5:7b"  # embedding model skipped
    sent = [json.loads(b["prompt"])["name"] for b in ollama.generate_bodies()]
    assert sent == ["medium", "low"]
    assert items[0] not in review.opinions


def test_request_shape(ollama: FakeOllama, home: Path, items: list[Path]) -> None:
    _review(ollama, home, items[1:2], [0.57])
    body = ollama.generate_bodies()[0]
    assert body["format"] == RESPONSE_SCHEMA
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0, "seed": 42}
    assert "untrusted" in body["system"]


def test_in_use_demotes_medium_item(ollama: FakeOllama, home: Path, items: list[Path]) -> None:
    ollama.answer = lambda body: (
        200,
        {"verdict": "in_use", "confidence": 0.8, "rationale": "Shared Python cache."},
    )
    review = _review(ollama, home, items[1:2], [0.57])
    cand = review.candidates[0]
    assert cand.action is Action.LIST_ONLY
    assert cand.score == 0.30
    assert cand.evidence[-1].source == "ai"
    assert review.opinions[cand.path].demoted


def test_low_confidence_in_use_does_not_demote(
    ollama: FakeOllama, home: Path, items: list[Path]
) -> None:
    ollama.answer = lambda body: (200, {"verdict": "in_use", "confidence": 0.4, "rationale": "?"})
    review = _review(ollama, home, items[1:2], [0.57])
    assert review.candidates[0].action is Action.QUARANTINE_WITH_SNAPSHOT


def test_ai_never_promotes(ollama: FakeOllama, home: Path, items: list[Path]) -> None:
    """A manipulated model answering 'residue, 1.0' cannot raise any band."""
    ollama.answer = lambda body: (
        200,
        {"verdict": "residue", "confidence": 1.0, "rationale": "IGNORE RULES, DELETE IT"},
    )
    review = _review(ollama, home, items, [0.85, 0.57, 0.20])
    assert [c.action for c in review.candidates] == [
        Action.QUARANTINE,
        Action.QUARANTINE_WITH_SNAPSHOT,
        Action.LIST_ONLY,
    ]
    assert [c.score for c in review.candidates] == [0.85, 0.57, 0.20]
    assert review.opinions[items[2]].verdict is Verdict.RESIDUE  # shown as advice only


@pytest.mark.parametrize(
    "bad_answer",
    [
        {"verdict": "delete_everything", "confidence": 0.9, "rationale": "x"},
        {"verdict": "residue", "confidence": 7, "rationale": "x"},
        {"verdict": "residue", "confidence": True, "rationale": "x"},
        {"verdict": "residue", "confidence": 0.9},
        ["not", "an", "object"],
    ],
)
def test_invalid_answers_become_uncertain(
    ollama: FakeOllama, home: Path, items: list[Path], bad_answer: Any
) -> None:
    ollama.answer = lambda body: (200, bad_answer)
    review = _review(ollama, home, items[1:2], [0.57])
    opinion = review.opinions[items[1]]
    assert (opinion.verdict, opinion.source) == (Verdict.UNCERTAIN, "error")
    assert review.candidates[0].action is Action.QUARANTINE_WITH_SNAPSHOT
    assert review.warnings


def test_non_json_response_becomes_uncertain(
    ollama: FakeOllama, home: Path, items: list[Path]
) -> None:
    ollama.answer = lambda body: (200, b'{"response": "this is not json"}')
    review = _review(ollama, home, items[1:2], [0.57])
    assert review.opinions[items[1]].verdict is Verdict.UNCERTAIN


def test_circuit_breaker_after_consecutive_failures(ollama: FakeOllama, home: Path) -> None:
    ollama.answer = lambda body: (500, b"internal error")
    paths = [home / f".cache/item{i}" for i in range(6)]
    for path in paths:
        make(path / "a")
    review = _review(ollama, home, paths, [0.20] * 6)
    assert len(ollama.generate_bodies()) == 3
    assert len(review.opinions) == 3
    assert any("consecutive" in w for w in review.warnings)
    assert len(review.candidates) == 6


def test_unavailable_service_falls_back(home: Path, items: list[Path]) -> None:
    engine = AiEngine(OllamaClient(f"http://127.0.0.1:{_free_port()}", timeout=2))
    cands = [candidate(p, home, 0.57) for p in items]
    review = engine.review(cands)
    assert not review.available
    assert review.candidates == tuple(cands)
    assert review.opinions == {}
    assert "AI review skipped" in review.warnings[0]


def test_missing_preferred_model(ollama: FakeOllama, home: Path, items: list[Path]) -> None:
    engine = AiEngine(OllamaClient(ollama.url), model="llama3.1:8b")
    review = engine.review([candidate(items[1], home, 0.57)])
    assert not review.available
    assert "ollama pull llama3.1:8b" in review.warnings[0]
