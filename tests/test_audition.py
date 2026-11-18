"""The audition: which files it takes, what it hands over, and what it leaves.

Each of these encodes something that went wrong on the real library. A live
single was "upgraded" with the studio take of the same song, twice, because the
title was folded down to the song's name before anything was compared. A
singles compilation picked out of a discography was handed to Lidarr, which
read the torrent's name, filed it under three other albums and never imported
it. And fifty-six torrents, 13.9 GB, sat in the audition's folder because the
run that added them was stopped before it could clean up.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


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


best = load("best_release_audition", "tools/best-release.py")

RAMMSTEIN = [
    "Rammstein - Discography (1995-2022)/2019 - Rammstein/01.- Deutschland.flac",
    "Rammstein - Discography (1995-2022)/2022 - Zeit/01.- Armee Der Tristen.flac",
    "Rammstein - Discography (1995-2022)/2022 - Zeit/02.- Zeit.flac",
]


# ── which files an audition takes ─────────────────────────────────────────

def test_a_live_single_does_not_take_the_studio_take_of_the_same_song():
    assert best.wanted_indexes(RAMMSTEIN, "Armee der Tristen (live in Mexico City)") == []


def test_a_live_single_does_not_take_one_track_off_the_studio_album():
    names = ["Twenty One Pilots - Breach [2025]/01. City Walls.flac",
             "Twenty One Pilots - Breach [2025]/02. RAWFEAR.flac",
             "Twenty One Pilots - Breach [2025]/03. Drum Show.flac"]
    assert best.wanted_indexes(names, "RAWFEAR (Live From The Bellwether)") == []


def test_a_live_single_still_finds_its_live_recording():
    names = RAMMSTEIN + [
        "Rammstein - Discography (1995-2022)/Singles/Armee der Tristen (Live in Mexico City).flac"]
    assert best.wanted_indexes(names, "Armee der Tristen (live in Mexico City)") == [3]


def test_a_title_without_a_qualifier_still_finds_its_album_in_a_discography():
    assert best.wanted_indexes(RAMMSTEIN, "Zeit") == [1, 2]


# ── what an audition leaves behind ────────────────────────────────────────

import signal  # noqa: E402

import pytest  # noqa: E402

queue = load("audit_queue_audition", "tools/audit-queue.py")

HASH = "a" * 40


class FakeClient:
    """Just enough of qBittorrent for one audition to add a torrent and delete it."""

    def __init__(self):
        self.torrents: dict[str, dict] = {}
        self.deleted: list[str] = []

    def __call__(self, path, fields=None, timeout=60):
        if path.startswith("/torrents/info"):
            return [{"hash": h, **t} for h, t in self.torrents.items()]
        if path == "/torrents/delete":
            for h in fields["hashes"].split("|"):
                self.torrents.pop(h, None)
                self.deleted.append(h)
        return "Ok."

    def add(self, raw, url, fields):
        self.torrents[HASH] = {"state": "stalledDL", "progress": 0.0, "tags": best.TAG}


def run_main(monkeypatch, wait_for, *extra, client=None):
    client = client or FakeClient()
    monkeypatch.setattr(best, "qbt", client)
    monkeypatch.setattr(best, "qbt_add", client.add)
    monkeypatch.setattr(best, "find_album", lambda args: {"id": 1, "title": "Zeit", "artist": "Rammstein"})
    monkeypatch.setattr(best, "shortlist", lambda *a, **k: [{"title": "Rammstein - Zeit [FLAC]", "downloadUrl": "http://x"}])
    monkeypatch.setattr(best, "wait_for", wait_for)
    monkeypatch.setattr(best.shutil, "which", lambda tool: "/usr/bin/" + tool)
    monkeypatch.setattr(best.time, "sleep", lambda s: None)
    monkeypatch.setattr(sys, "argv", ["best-release.py", "--album-id", "1", "--download", *extra])
    saved = signal.getsignal(signal.SIGTERM)
    try:
        best.main()
    finally:
        signal.signal(signal.SIGTERM, saved)
    return client


def test_an_audition_where_nothing_finished_removes_what_it_added(monkeypatch):
    client = run_main(monkeypatch, lambda hashes, minutes: [])
    assert client.deleted == [HASH]


def test_an_audition_stopped_by_sigterm_removes_what_it_added(monkeypatch):
    def stopped_while_waiting(hashes, minutes):
        os.kill(os.getpid(), signal.SIGTERM)
        return []
    client = FakeClient()
    with pytest.raises(SystemExit):
        run_main(monkeypatch, stopped_while_waiting, client=client)
    assert client.deleted == [HASH]


def test_keep_losers_still_leaves_them_in_place(monkeypatch):
    client = run_main(monkeypatch, lambda hashes, minutes: [], "--keep-losers")
    assert client.deleted == [] and HASH in client.torrents


def test_a_timed_out_audition_is_asked_to_stop_before_it_is_killed(tmp_path):
    marker = tmp_path / "cleaned"
    child = ("import pathlib, signal, sys, time\n"
             f"signal.signal(signal.SIGTERM, lambda *a: (pathlib.Path({str(marker)!r}).write_text('ok'), sys.exit(0)))\n"
             "print('started', flush=True)\n"
             "time.sleep(30)\n")
    output = queue.run_audition([sys.executable, "-c", child], timeout=1, grace=10)
    assert marker.read_text() == "ok"
    assert "started" in output
    assert output.endswith("the audition ran past its timeout and was stopped")
