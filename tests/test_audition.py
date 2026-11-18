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


def test_a_discography_root_that_says_live_does_not_make_its_studio_takes_live():
    names = [n.replace("Discography (1995-2022)", "Discography (Studio + Live)") for n in RAMMSTEIN]
    assert best.wanted_indexes(names, "Armee der Tristen (live in Mexico City)") == []


def test_a_live_single_whose_own_folder_says_live_is_taken_whole():
    names = ["twenty one pilots - RAWFEAR (Live From The Bellwether) [FLAC]/01. RAWFEAR.flac"]
    assert best.wanted_indexes(names, "RAWFEAR (Live From The Bellwether)") is None


def test_a_title_without_a_qualifier_still_finds_its_album_in_a_discography():
    assert best.wanted_indexes(RAMMSTEIN, "Zeit") == [1, 2]


# ── what an audition leaves behind ────────────────────────────────────────

import signal  # noqa: E402

import pytest  # noqa: E402

queue = load("audit_queue_audition", "tools/audit-queue.py")

HASH = "a" * 40


class FakeClient:
    """Just enough of qBittorrent for one audition to add a torrent and delete it."""

    def __init__(self, files=None):
        self.torrents: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.files = [{"index": i, "name": n} for i, n in enumerate(files or [])]

    def __call__(self, path, fields=None, timeout=60):
        if path.startswith("/torrents/files"):
            return self.files
        if path.startswith("/torrents/info"):
            return [{"hash": h, **t} for h, t in self.torrents.items()]
        if path == "/torrents/delete":
            for h in fields["hashes"].split("|"):
                self.torrents.pop(h, None)
                self.deleted.append(h)
        return "Ok."

    def add(self, raw, url, fields):
        self.torrents[HASH] = {"state": "stalledDL", "progress": 0.0, "tags": best.TAG,
                               "category": fields["category"]}


ZEIT = {"id": 1, "title": "Zeit", "artist": "Rammstein"}


def run_main(monkeypatch, wait_for, *extra, client=None, album=ZEIT, picked=None):
    client = client or FakeClient()
    picked = picked or [{"title": "Rammstein - Zeit [FLAC]", "downloadUrl": "http://x"}]
    monkeypatch.setattr(best, "qbt", client)
    monkeypatch.setattr(best, "qbt_add", client.add)
    monkeypatch.setattr(best, "find_album", lambda args: album)
    monkeypatch.setattr(best, "shortlist", lambda *a, **k: picked)
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


# ── what Lidarr is told ───────────────────────────────────────────────────

SINGLES = "Brujeria [Discography]/(2006) Machetazos! (The Singles)"
SINGLES_FILES = [f"{SINGLES}/{n}.mp3" for n in (
    "01-Marijuana", "02-Matando Gueros '97", "03-Grito De Los Brujos", "04-Padre Nuerstro",
    "05-Molestando Ninos Muertos", "06-Grito De Los Soldados Poseidos",
    "07-Machetazos (Sacrificio II)", "08-Castigo Del Brujo", "09-Cristo De La Roca",
    "10-El Patron", "11-Hermanos Menendez", "12-Seis Seis Seis")]
# The rest of the discography, which the audition skips.
TORRENT_FILES = SINGLES_FILES + [
    "Brujeria [Discography]/(1993) Matando Gueros/01-Pura De Venta.mp3",
    "Brujeria [Discography]/(1995) Raza Odiada/01-Raza Odiada.mp3"]
EP_TRACKS = [{"id": 900 + n, "title": t} for n, t in enumerate((
    "Padre nuestro", "Molestando niños muertos", "Grito de los soldados poseídos",
    "Machetazos (Sacrificio II)", "Castigo del brujo", "Cristo de la roca"), 1)]


class FakeLidarr:
    """Lidarr's side of one handoff: the album, its tracks, and what it is told."""

    def __init__(self, tracked=True, unknown_artist=False):
        self.tracked = tracked
        # Lidarr leaves a download it cannot tie to an artist out of the
        # queue unless includeUnknownArtistItems=true is asked for.
        self.unknown_artist = unknown_artist
        self.commands: list[dict] = []

    def get(self, path):
        if path.startswith("/queue"):
            hidden = self.unknown_artist and "includeUnknownArtistItems=true" not in path
            return {"records": [{"downloadId": HASH.upper()}] if self.tracked and not hidden else []}
        if path == "/album/970":
            return {"id": 970, "artistId": 5, "title": "Machetazos",
                    "releases": [{"id": 77, "monitored": True, "trackCount": 6}]}
        if path.startswith("/track?albumId=970&albumReleaseId=77"):
            return EP_TRACKS
        if path.startswith(f"/manualimport?downloadId={HASH.upper()}"):
            return [{"path": f"/data/torrents/{best.TAG}/{rel}", "quality": {"quality": {"name": "MP3-320"}}}
                    for rel in TORRENT_FILES]
        if path.startswith("/command/"):
            return {"status": "completed", "message": "Manually imported 6 files"}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, url, headers, data=None, timeout=120, method=None):
        body = __import__("json").loads(data)
        self.commands.append(body)
        return {"id": len(self.commands), "status": "started"}


def hand_over_singles(monkeypatch, tmp_path, fake):
    """One audition whose winner is the singles folder inside a Brujería discography."""
    (tmp_path / "torrents" / best.TAG / SINGLES).mkdir(parents=True)
    torrent = {"hash": HASH, "name": "Brujeria - Discography (1990-2008)",
               "save_path": f"/data/torrents/{best.TAG}",
               "content_path": f"/data/torrents/{best.TAG}/Brujeria [Discography]"}
    monkeypatch.setattr(best, "PATH_TO", str(tmp_path))
    monkeypatch.setattr(best, "audit_album", lambda folder: {"tracks": 12})
    monkeypatch.setattr(best, "score", lambda audit: (1,))
    monkeypatch.setattr(best, "describe", lambda audit: "mp3 320")
    monkeypatch.setattr(best, "incumbent", lambda *a: None)
    monkeypatch.setattr(best, "lidarr", fake.get)
    monkeypatch.setattr(best, "_json", fake.post)
    client = FakeClient(TORRENT_FILES)
    run_main(monkeypatch, lambda hashes, minutes: [torrent], client=client,
             album={"id": 970, "title": "Machetazos", "artist": "Brujería"},
             picked=[{"title": "Brujeria - Discography (1990-2008) [mp3@128-320]",
                      "downloadUrl": "http://x", "_files": TORRENT_FILES,
                      "_want": list(range(len(SINGLES_FILES)))}])
    return client


def test_an_album_picked_out_of_a_larger_torrent_is_imported_into_that_album(monkeypatch, tmp_path):
    fake = FakeLidarr()
    hand_over_singles(monkeypatch, tmp_path, fake)

    imports = [c for c in fake.commands if c["name"] == "ManualImport"]
    assert len(imports) == 1
    files = imports[0]["files"]
    assert {f["albumId"] for f in files} == {970}
    assert {f["albumReleaseId"] for f in files} == {77}
    assert {f["downloadId"] for f in files} == {HASH.upper()}
    mapped = {f["trackIds"][0]: f["path"].rsplit("/", 1)[-1] for f in files}
    assert mapped == {901: "04-Padre Nuerstro.mp3", 902: "05-Molestando Ninos Muertos.mp3",
                      903: "06-Grito De Los Soldados Poseidos.mp3",
                      904: "07-Machetazos (Sacrificio II).mp3", 905: "08-Castigo Del Brujo.mp3",
                      906: "09-Cristo De La Roca.mp3"}


def test_an_album_missing_a_track_from_the_files_is_not_imported_half_way(monkeypatch, tmp_path):
    fake = FakeLidarr()
    tracks = EP_TRACKS + [{"id": 907, "title": "Demoniaco"}]
    real_get = fake.get
    monkeypatch.setattr(fake, "get", lambda path: tracks if path.startswith("/track?") else real_get(path))
    hand_over_singles(monkeypatch, tmp_path, fake)
    assert [c["name"] for c in fake.commands] == ["RefreshMonitoredDownloads"]


def test_a_download_lidarr_never_tracks_is_not_imported(monkeypatch, tmp_path):
    # Imported while Lidarr is not tracking it, "auto" means move: the files
    # would leave the torrent's folder and the client would lose them.
    fake = FakeLidarr(tracked=False)
    hand_over_singles(monkeypatch, tmp_path, fake)
    assert [c["name"] for c in fake.commands] == ["RefreshMonitoredDownloads"]


def test_a_whole_album_torrent_is_still_only_handed_over(monkeypatch):
    fake = FakeLidarr()
    calls = []
    monkeypatch.setattr(best, "qbt", lambda path, fields=None, timeout=60: calls.append((path, fields)))
    monkeypatch.setattr(best, "_json", fake.post)
    best.hand_over(ZEIT, {"hash": HASH}, None)
    assert calls == [("/torrents/setCategory", {"hashes": HASH, "category": best.LIDARR_CATEGORY})]
    assert fake.commands == []


def test_a_download_lidarr_cannot_tie_to_an_artist_is_still_imported(monkeypatch, tmp_path):
    fake = FakeLidarr(unknown_artist=True)
    hand_over_singles(monkeypatch, tmp_path, fake)
    assert [c["name"] for c in fake.commands] == ["RefreshMonitoredDownloads", "ManualImport"]


def test_a_song_and_its_demo_are_not_swapped():
    paths = ["/x/Album/01 Song.flac", "/x/Album/02 Song (Demo).flac"]
    tracks = [{"id": 1, "title": "Song (Demo)"}, {"id": 2, "title": "Song"}]
    assert best.match_tracks(paths, tracks) == {1: "/x/Album/02 Song (Demo).flac",
                                                2: "/x/Album/01 Song.flac"}


def test_cleanup_never_deletes_a_torrent_already_handed_to_lidarr(monkeypatch):
    client = FakeClient()
    client.torrents = {"b" * 40: {"tags": best.TAG, "category": best.TAG},
                       "c" * 40: {"tags": best.TAG, "category": best.LIDARR_CATEGORY}}
    monkeypatch.setattr(best, "qbt", client)
    best.cleanup(["b" * 40, "c" * 40], None)
    assert client.deleted == ["b" * 40]


def test_a_live_album_whose_folder_says_live_still_pairs_its_tracks():
    paths = ["/x/1994 - MTV Unplugged in New York/01 About a Girl.flac",
             "/x/1994 - MTV Unplugged in New York/02 Come as You Are.flac"]
    tracks = [{"id": 1, "title": "About a Girl"}, {"id": 2, "title": "Come as You Are"}]
    assert best.match_tracks(paths, tracks) == {1: paths[0], 2: paths[1]}


def test_a_file_that_says_live_never_pairs_with_the_studio_track():
    assert best.match_tracks(["/x/Singles/16-Raza Odiada (live).mp3"],
                             [{"id": 1, "title": "Raza odiada"}]) is None


def test_a_lidarr_error_after_the_handover_says_so_instead_of_crashing(monkeypatch, tmp_path, capsys):
    fake = FakeLidarr()
    real_get = fake.get

    def failing(path):
        if path == "/album/970":
            raise best.urllib.error.URLError("connection refused")
        return real_get(path)
    monkeypatch.setattr(fake, "get", failing)
    hand_over_singles(monkeypatch, tmp_path, fake)
    assert "import it by hand" in capsys.readouterr().out


def test_a_stop_during_the_import_says_it_needs_importing_by_hand(monkeypatch, tmp_path, capsys):
    fake = FakeLidarr()
    real_get = fake.get

    def stopped(path):
        if path.startswith("/command/"):
            os.kill(os.getpid(), signal.SIGTERM)
        return real_get(path)
    monkeypatch.setattr(fake, "get", stopped)
    with pytest.raises(SystemExit):
        hand_over_singles(monkeypatch, tmp_path, fake)
    assert "import it by hand" in capsys.readouterr().out


def test_a_grandchild_holding_the_pipes_does_not_hold_the_queue(tmp_path):
    import time
    child = ("import signal, subprocess, sys, time\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
             "time.sleep(60)\n")
    started = time.time()
    output = queue.run_audition([sys.executable, "-c", child], timeout=1, grace=1, drain=2)
    assert time.time() - started < 15
    assert output.endswith("the audition ran past its timeout and was stopped")
