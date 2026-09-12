"""search-backlog.py's own guard against searching while Prowlarr is blind.

indexers_down() moved to tools/indexers.py (tests/test_indexers.py covers its
behaviour); what matters here is narrower and specific to this tool: that the
move left search-backlog.py's two early exits — Prowlarr unreachable, and
every indexer out of service — firing exactly as they did before, off the
one shared function instead of a private copy.
"""

import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import indexers  # noqa: E402


def load(name: str, path: str):
    """Import a tool whose file name is not a Python identifier."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    module = importlib.util.module_from_spec(spec)
    saved, sys.argv = sys.argv, [name]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = saved
    return module


search_backlog = load("search_backlog", "tools/search-backlog.py")


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def test_search_backlog_shares_the_one_indexers_module_not_a_copy():
    assert search_backlog.indexers is indexers
    assert search_backlog.indexers.indexers_down is indexers.indexers_down


def test_prowlarr_being_unreachable_stops_the_search_before_it_starts(monkeypatch):
    monkeypatch.setattr(search_backlog.indexers, "indexers_down", lambda: None)
    monkeypatch.setattr(sys, "argv", ["search-backlog.py"])
    with pytest.raises(SystemExit) as exc:
        search_backlog.main()
    assert "not searching blind" in str(exc.value).lower()


def test_every_indexer_out_of_service_stops_the_search_before_it_starts(monkeypatch):
    monkeypatch.setattr(search_backlog.indexers, "indexers_down", lambda: ["A", "B"])
    body = json.dumps([{"id": 1}, {"id": 2}]).encode()
    monkeypatch.setattr(search_backlog.urllib.request, "urlopen",
                        lambda *a, **k: FakeResponse(body))
    monkeypatch.setattr(sys, "argv", ["search-backlog.py"])
    with pytest.raises(SystemExit) as exc:
        search_backlog.main()
    assert "out of service" in str(exc.value)
