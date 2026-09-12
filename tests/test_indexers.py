"""One place to ask whether Prowlarr still has working indexers.

search-backlog.py learned this the expensive way: a burst of searches
tripped Prowlarr's own rate limits, Prowlarr disabled indexer after indexer,
and every search after that returned nothing at all while reporting it as
though the releases did not exist. Sixty-seven searches went out before
anyone noticed, because a failure to even ask Prowlarr was being read the
same as "nothing is down." indexers_down() is the fix for that mistake, and
these tests pin its contract down so stuck-imports.py can depend on it too.
"""

import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import indexers  # noqa: E402


class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def fake_prowlarr(monkeypatch, indexer_list, status_list=None, key="prowlarr-key"):
    """Wire indexers.py to a scripted Prowlarr, keyed by the path each GET asks for."""
    monkeypatch.setattr(indexers, "PROWLARR_KEY", key)
    bodies = {"indexer": indexer_list, "indexerstatus": status_list or []}

    def fake_urlopen(req, timeout=60):
        path = req.full_url.rsplit("/", 1)[-1]
        return FakeResponse(json.dumps(bodies[path]).encode())

    monkeypatch.setattr(indexers.urllib.request, "urlopen", fake_urlopen)


# ── indexers_down(): None is not the same as [] ───────────────────────────

def test_indexers_down_is_an_empty_list_without_a_prowlarr_key(monkeypatch):
    monkeypatch.setattr(indexers, "PROWLARR_KEY", "")
    assert indexers.indexers_down() == []


def test_indexers_down_names_the_indexers_prowlarr_marked_out_of_service(monkeypatch):
    fake_prowlarr(
        monkeypatch,
        [{"id": 1, "name": "The Pirate Bay"}, {"id": 2, "name": "LimeTorrents"},
         {"id": 3, "name": "Knaben"}],
        [{"indexerId": 1}, {"indexerId": 2}],
    )
    assert set(indexers.indexers_down()) == {"The Pirate Bay", "LimeTorrents"}


def test_indexers_down_is_none_when_prowlarr_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(indexers, "PROWLARR_KEY", "key")

    def boom(req, timeout=60):
        raise OSError("Resource temporarily unavailable")

    monkeypatch.setattr(indexers.urllib.request, "urlopen", boom)
    assert indexers.indexers_down() is None


# ── indexer_count(): the other half of "is this a total outage" ──────────

def test_indexer_count_is_none_without_a_prowlarr_key(monkeypatch):
    monkeypatch.setattr(indexers, "PROWLARR_KEY", "")
    assert indexers.indexer_count() is None


def test_indexer_count_reports_how_many_indexers_are_configured(monkeypatch):
    fake_prowlarr(monkeypatch, [{"id": 1}, {"id": 2}, {"id": 3}])
    assert indexers.indexer_count() == 3


def test_indexer_count_is_none_when_prowlarr_cannot_be_asked(monkeypatch):
    monkeypatch.setattr(indexers, "PROWLARR_KEY", "key")

    def boom(req, timeout=60):
        raise OSError("nope")

    monkeypatch.setattr(indexers.urllib.request, "urlopen", boom)
    assert indexers.indexer_count() is None
