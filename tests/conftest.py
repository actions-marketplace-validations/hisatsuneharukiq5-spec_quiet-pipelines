"""Test doubles: no test touches the network."""
from __future__ import annotations

import json as _json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, status: int = 200, body: Any = None, headers: dict[str, str] | None = None):
        self.status_code = status
        self.headers = headers or {}
        if isinstance(body, (dict, list)):
            self.text = _json.dumps(body)
        else:
            self.text = body or ""
        self.content = self.text.encode()

    def json(self):
        return _json.loads(self.text)


class FakeSession:
    """Route (method, url-substring[, predicate]) -> FakeResponse | Exception. Records calls."""

    def __init__(self):
        self.routes: list[tuple[str, str, Callable[[dict], bool] | None, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def add(self, method: str, url_part: str, response: Any, when: Callable[[dict], bool] | None = None):
        self.routes.append((method.upper(), url_part, when, response))
        return self

    def _dispatch(self, method: str, url: str, **kw):
        call = {"method": method, "url": url, **kw}
        self.calls.append(call)
        for m, part, when, resp in self.routes:
            if m == method and part in url and (when is None or when(call)):
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unexpected request: {method} {url} {kw.get('params')}")

    def get(self, url, **kw):
        return self._dispatch("GET", url, **kw)

    def post(self, url, **kw):
        return self._dispatch("POST", url, **kw)

    def head(self, url, **kw):
        return self._dispatch("HEAD", url, **kw)

    def request(self, method, url, **kw):
        return self._dispatch(method.upper(), url, **kw)


@pytest.fixture
def session():
    return FakeSession()


@pytest.fixture
def now():
    return lambda: NOW


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("real network access attempted in a test")
    monkeypatch.setattr(requests.Session, "request", boom)
    monkeypatch.setattr(requests.Session, "send", boom)
