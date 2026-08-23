"""Navidrome -> Lidarr bridge.

Exposes the artists a user starred in Navidrome as a Lidarr "Custom List"
feed. Lidarr's CustomImport expects a JSON array of objects carrying a single
MusicBrainz artist id:

    [{"MusicBrainzId": "24e1b53c-3085-4581-8472-0b0088d2508c"}, ...]

Navidrome libraries frequently carry no MusicBrainz tags at all, so artist
names are resolved to ids through Lidarr's own /artist/lookup endpoint, which
already speaks to the metadata server Lidarr will use when adding the artist.

A name several artists share is settled by the library itself: unrelated bands
sharing a name do not share a back catalogue, so the candidate whose discography
contains the albums already owned is the right one. Only a name that nothing
distinguishes is reported on /status for a human to pin in overrides.json.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("bridge")

NAVIDROME_URL = os.environ.get("NAVIDROME_URL", "http://host.docker.internal:4533").rstrip("/")
NAVIDROME_USER = os.environ.get("NAVIDROME_USER", "")
NAVIDROME_PASS = os.environ.get("NAVIDROME_PASS", "")
LIDARR_URL = os.environ.get("LIDARR_URL", "http://lidarr:8686").rstrip("/")
LIDARR_API_KEY = os.environ.get("LIDARR_API_KEY", "")
# Which import list to refresh when the published set changes. Left empty the
# bridge discovers it, which only works while exactly one CustomImport exists.
LIDARR_IMPORTLIST_ID = os.environ.get("LIDARR_IMPORTLIST_ID", "").strip()
# How long to wait for Lidarr to list an album after its artist is imported.
IMPORT_WAIT_SECONDS = int(os.environ.get("IMPORT_WAIT_SECONDS", "45"))
# MusicBrainz asks for no more than one request a second, and identifies
# callers by User-Agent. Only used to break a tie between same-named artists.
MB_MIN_INTERVAL = float(os.environ.get("MB_MIN_INTERVAL", "1.1"))
MB_USER_AGENT = os.environ.get(
    "MB_USER_AGENT",
    "navidrome-lidarr-bridge/1.0 (+https://github.com/danielbanariba/navidrome-lidarr-bridge)")
REFRESH_SECONDS = int(os.environ.get("REFRESH_SECONDS", "900"))
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8687"))
STATE_DIR = os.environ.get("STATE_DIR", "/state")

# api.lidarr.audio is rate limited: pause between lookups, and back a failing
# name off exponentially instead of re-asking for it every single sync.
LOOKUP_DELAY_SECONDS = float(os.environ.get("LOOKUP_DELAY_SECONDS", "0.5"))
MAX_BACKOFF_SECONDS = int(os.environ.get("MAX_BACKOFF_SECONDS", str(6 * 3600)))

CACHE_PATH = os.path.join(STATE_DIR, "resolved.json")
OVERRIDES_PATH = os.path.join(STATE_DIR, "overrides.json")
# The set Lidarr was last told about. Persisted so a restart does not look like
# a change and refresh the list for nothing.
PUBLISHED_PATH = os.path.join(STATE_DIR, "published.json")
# The userscript that draws the panel inside Navidrome, served from /panel.user.js.
PANEL_PATH = os.environ.get("PANEL_PATH", "/app/panel.user.js")

# Only one sync may run at a time: the background loop and every GET /sync
# request (ThreadingHTTPServer serves each connection on its own thread) would
# otherwise interleave their writes to resolved.json.
SYNC_LOCK = threading.Lock()

# name -> {attempts, retry_at, reason, candidates}. In memory only, guarded by
# SYNC_LOCK; a restart simply retries everything once.
_BACKOFF: dict[str, dict] = {}


class BridgeError(RuntimeError):
    pass


# Errors that mean "this HTTP call did not produce usable JSON". urlopen only
# wraps *connection* failures in URLError; timeouts, resets and truncated or
# non-JSON bodies surface while the body is being read.
FETCH_ERRORS = (OSError, ValueError, http.client.HTTPException, BridgeError)


def _is_mbid(value: object) -> bool:
    """True for a canonical MusicBrainz id (a dashed UUID, any case)."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.strip().lower()
    except (ValueError, AttributeError, TypeError):
        return False


def _get_json(url: str, headers: dict | None = None) -> object:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def subsonic(endpoint: str, **params) -> dict:
    """Call a Subsonic endpoint using salted-token auth."""
    if not (NAVIDROME_USER and NAVIDROME_PASS):
        raise BridgeError("NAVIDROME_USER / NAVIDROME_PASS are not set")
    salt = secrets.token_hex(8)
    digest = hashlib.md5((NAVIDROME_PASS + salt).encode(), usedforsecurity=False)
    query = urllib.parse.urlencode({
        "u": NAVIDROME_USER, "t": digest.hexdigest(), "s": salt,
        "v": "1.16.1", "c": "navidrome-lidarr-bridge", "f": "json",
        **params,
    })
    payload = _get_json(f"{NAVIDROME_URL}/rest/{endpoint}?{query}")
    if not isinstance(payload, dict):
        raise BridgeError(f"{endpoint}: unexpected response body")
    # Navidrome answers auth failures with HTTP 200 and status "failed", so the
    # envelope is the only place a bad password shows up.
    body = payload.get("subsonic-response", {})
    if body.get("status") != "ok":
        raise BridgeError(f"{endpoint}: {body.get('error', {}).get('message', 'unknown error')}")
    return body


def starred_artists() -> dict[str, dict]:
    """Starred artist names -> {"id": Navidrome id, "mbid": tagged id or None}.

    The Navidrome id is carried along because disambiguating a shared name needs
    to know which albums the library actually holds for it.
    """
    body = subsonic("getStarred2")
    out: dict[str, dict] = {}
    for artist in body.get("starred2", {}).get("artist", []) or []:
        name = (artist.get("name") or "").strip()
        if not name:
            continue
        tagged = artist.get("musicBrainzId")
        entry = out.setdefault(name, {"id": artist.get("id"), "mbid": None})
        # A duplicate name must not erase an id seen on an earlier entry.
        if _is_mbid(tagged):
            entry["mbid"] = tagged.strip().lower()
    return out


def owned_titles(nd_artist_id: str) -> set[str]:
    """Normalised album titles the library holds for a Navidrome artist."""
    artist = subsonic("getArtist", id=nd_artist_id).get("artist", {})
    return {norm_title(a["name"]) for a in artist.get("album", []) if a.get("name")}


_MB_LAST = [0.0]


def musicbrainz_albums(mbid: str) -> set[str]:
    """Normalised studio album titles MusicBrainz lists for an artist.

    Called only to break a tie between artists sharing a name, so the one
    request per second MusicBrainz asks for costs nothing in the common case.
    """
    wait = MB_MIN_INTERVAL - (time.monotonic() - _MB_LAST[0])
    if wait > 0:
        time.sleep(wait)
    _MB_LAST[0] = time.monotonic()
    url = ("https://musicbrainz.org/ws/2/release-group?"
           + urllib.parse.urlencode({"artist": mbid, "type": "album",
                                     "limit": "100", "fmt": "json"}))
    req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    return {norm_title(g["title"]) for g in data.get("release-groups", [])
            if g.get("primary-type") == "Album" and g.get("title")}


def disambiguate(candidates: list[dict], owned: set[str]) -> tuple[dict | None, list[dict]]:
    """Pick the artist whose discography contains the albums already owned.

    Several unrelated bands share a name, but they do not share a back
    catalogue. Holding "Abismo" and "Los signos del Fauno" identifies exactly
    one of the ten artists called Delirium, and no human has to be asked.

    Returns the winner and, either way, every candidate with its overlap so an
    unresolved name can show its working.
    """
    scored = []
    for cand in candidates:
        try:
            albums = musicbrainz_albums(cand["foreignArtistId"])
        except FETCH_ERRORS as exc:
            log.warning("discography lookup failed for %s: %s", cand["foreignArtistId"], exc)
            albums = set()
        scored.append({"candidate": cand, "overlap": owned & albums, "total": len(albums)})
    scored.sort(key=lambda s: len(s["overlap"]), reverse=True)

    report = [
        {"mbid": s["candidate"]["foreignArtistId"],
         "disambiguation": s["candidate"].get("disambiguation") or "",
         "albums_in_common": sorted(s["overlap"])}
        for s in scored
    ]
    best = scored[0] if scored else None
    if not best or not best["overlap"]:
        return None, report
    # A tie is not an answer: two catalogues matching equally well means the
    # library cannot tell them apart either.
    runner_up = scored[1]["overlap"] if len(scored) > 1 else set()
    if len(best["overlap"]) == len(runner_up):
        return None, report
    return best["candidate"], report


def lidarr_lookup(name: str) -> list[dict]:
    if not LIDARR_API_KEY:
        raise BridgeError("LIDARR_API_KEY is not set")
    url = f"{LIDARR_URL}/api/v1/artist/lookup?" + urllib.parse.urlencode({"term": name})
    results = _get_json(url, {"X-Api-Key": LIDARR_API_KEY})
    if not isinstance(results, list):
        raise BridgeError("artist/lookup did not return a list")
    return results


def _send_json(url: str, body: object, method: str, headers: dict | None = None) -> object:
    payload = json.dumps(body).encode()
    req = urllib.request.Request(url, data=payload, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def _post_json(url: str, body: object, headers: dict | None = None) -> object:
    return _send_json(url, body, "POST", headers)


def _put_json(url: str, body: object, headers: dict | None = None) -> object:
    return _send_json(url, body, "PUT", headers)


# Resolved once per process: the id only changes if the list is recreated.
_IMPORTLIST_ID: int | None = None


def importlist_id() -> int | None:
    """The Lidarr import list to refresh, from config or by discovery."""
    global _IMPORTLIST_ID
    if LIDARR_IMPORTLIST_ID.isdigit():
        return int(LIDARR_IMPORTLIST_ID)
    if _IMPORTLIST_ID is not None:
        return _IMPORTLIST_ID

    lists = _get_json(f"{LIDARR_URL}/api/v1/importlist", {"X-Api-Key": LIDARR_API_KEY})
    if not isinstance(lists, list):
        raise BridgeError("importlist did not return a list")
    custom = [item for item in lists
              if isinstance(item, dict) and item.get("implementation") == "CustomImport"]
    if len(custom) != 1:
        # Guessing between several would refresh somebody else's list.
        raise BridgeError(
            f"found {len(custom)} CustomImport lists; set LIDARR_IMPORTLIST_ID to pick one")
    _IMPORTLIST_ID = int(custom[0]["id"])
    log.info("lidarr import list discovered: id=%d", _IMPORTLIST_ID)
    return _IMPORTLIST_ID


def notify_lidarr() -> str:
    """Ask Lidarr to re-read the list now.

    A plain ImportListSync respects the list's MinRefreshInterval (6h for
    CustomImport) and would silently do nothing; passing definitionId takes the
    single-list path, which fetches immediately.
    """
    list_id = importlist_id()
    if list_id is None:
        return "no import list to refresh"
    _post_json(f"{LIDARR_URL}/api/v1/command",
               {"name": "ImportListSync", "definitionId": list_id},
               {"X-Api-Key": LIDARR_API_KEY})
    return f"refreshed import list {list_id}"


def lidarr_get(path: str) -> object:
    if not LIDARR_API_KEY:
        raise BridgeError("LIDARR_API_KEY is not set")
    return _get_json(f"{LIDARR_URL}{path}", {"X-Api-Key": LIDARR_API_KEY})


# Edition markers that distinguish pressings of one album, not different albums.
_EDITION = re.compile(
    r"\b(remaster(ed)?|deluxe|expanded|anniversary|edition|reissue|bonus|disc|cd\d*)\b")


def norm_title(title: str) -> str:
    """Compare album titles across two catalogues that disagree on punctuation.

    Navidrome reports whatever the files are tagged with; Lidarr reports
    MusicBrainz. They differ in case, in apostrophes (' vs U+2019), and in
    parenthesised edition notes, so strip all three down to bare words.
    """
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", title.lower())
    text = _EDITION.sub(" ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def missing_albums(nd_artist_id: str) -> dict:
    """Albums Lidarr knows for this artist that Navidrome does not have.

    Lidarr's own "missing" count is useless here: it only sees its own root
    folder, so every album already in the wider library reads as missing.
    Navidrome is the authority on what is actually owned.
    """
    artist = subsonic("getArtist", id=nd_artist_id).get("artist", {})
    name = (artist.get("name") or "").strip()
    if not name:
        raise BridgeError(f"no Navidrome artist with id {nd_artist_id!r}")
    owned = {norm_title(a["name"]) for a in artist.get("album", []) if a.get("name")}

    def is_owned(title: str) -> bool:
        """Whether the library already holds this album.

        An owned copy often carries an edition suffix the catalogue title does
        not — "…Revenge-10th Anniversary Edition" against plain "…Revenge" —
        and those suffixes are not always parenthesised, so a prefix match
        catches them. Only in that direction: an owned plain title must not
        satisfy a distinct catalogue entry that extends it, such as a live
        recording named after the studio album.
        """
        key = norm_title(title)
        return any(have == key or have.startswith(key + " ") for have in owned)

    overrides, _ = _read_overrides()
    mbid = overrides.get(name) or _load(CACHE_PATH, {}).get(name)
    if not mbid:
        return {"artist": name, "monitored": False, "owned": len(owned), "missing": [],
                "hint": "not monitored yet — star this artist in Navidrome"}

    match = next((a for a in lidarr_get("/api/v1/artist")
                  if a.get("foreignArtistId") == mbid), None)
    if match is None:
        return {"artist": name, "monitored": False, "owned": len(owned), "missing": [],
                "hint": "starred, but Lidarr has not imported it yet"}

    missing = [
        {"id": album["id"], "title": album["title"],
         "year": (album.get("releaseDate") or "")[:4],
         "type": album.get("albumType", "")}
        for album in lidarr_get(f"/api/v1/album?artistId={match['id']}")
        if not is_owned(album["title"])
    ]
    missing.sort(key=lambda a: a["year"] or "9999")
    return {"artist": name, "monitored": True, "owned": len(owned),
            "lidarrArtistId": match["id"], "missing": missing}


def importlist_defaults() -> dict:
    """Quality profile, metadata profile and root folder for a newly added artist.

    Taken from the import list rather than from configuration of our own: an
    artist arriving through a request should land exactly where a starred one
    would, and there is no reason to have two places that can disagree.
    """
    list_id = importlist_id()
    for item in lidarr_get("/api/v1/importlist"):
        if item.get("id") == list_id:
            return {
                "qualityProfileId": item["qualityProfileId"],
                "metadataProfileId": item["metadataProfileId"],
                "rootFolderPath": item["rootFolderPath"],
            }
    raise BridgeError(f"import list {list_id} disappeared")


def import_artist_for(mbid: str) -> int:
    """Add the artist owning this album to Lidarr, and return the album's id.

    Lidarr resolves an album's MusicBrainz id to its artist even for artists it
    has never imported, so a request for an unmonitored artist does not have to
    bounce back to the user asking them to add it first.

    The artist is added with monitor "none": the caller monitors the one album
    that was actually requested. Adding with the usual "all" would queue the
    entire discography off a single click.
    """
    found = lidarr_get("/api/v1/album/lookup?" + urllib.parse.urlencode({"term": f"lidarr:{mbid}"}))
    if not isinstance(found, list) or not found:
        raise BridgeError(f"no album in Lidarr's catalogue for {mbid}")
    artist = (found[0] or {}).get("artist") or {}
    if not artist.get("foreignArtistId"):
        raise BridgeError(f"Lidarr's catalogue has no artist for album {mbid}")

    payload = dict(artist)
    payload.update(monitored=True, **importlist_defaults())
    payload["addOptions"] = {"monitor": "none", "searchForMissingAlbums": False}
    _post_json(f"{LIDARR_URL}/api/v1/artist", payload, {"X-Api-Key": LIDARR_API_KEY})
    log.info("imported artist %r for album %s", artist.get("artistName"), mbid)

    # The discography is fetched asynchronously, so the album is not there the
    # instant the artist is.
    deadline = time.monotonic() + IMPORT_WAIT_SECONDS
    while time.monotonic() < deadline:
        album_id = album_id_for_mbid(mbid)
        if album_id is not None:
            return album_id
        time.sleep(1)
    raise BridgeError(
        f"added {artist.get('artistName')!r}, but Lidarr has not listed the album yet")


def album_id_for_mbid(mbid: str) -> int | None:
    """The Lidarr album for a MusicBrainz release-group id, if it knows one.

    Lidarr's foreignAlbumId is the release-group id, which is also what the
    Navidrome panel carries, so no title matching is needed here. Lidarr only
    holds albums of artists it has imported, so an unknown id means the artist
    is not monitored rather than that the album does not exist.
    """
    if not _is_mbid(mbid):
        raise BridgeError(f"{mbid!r} is not a MusicBrainz id")
    found = lidarr_get("/api/v1/album?" + urllib.parse.urlencode({"foreignAlbumId": mbid}))
    return int(found[0]["id"]) if found else None


def ensure_monitored(kind: str, item_id: int, tries: int = 6) -> bool:
    """Set monitored on an artist or album, and confirm it actually stuck.

    Lidarr answers 202 to a write it has merely accepted. Right after an artist
    is imported it is also refreshing that artist, and a refresh landing after
    the write clears the flag again — so the write has to be read back rather
    than trusted. Backing off between attempts lets the refresh finish.
    """
    path = f"/api/v1/{kind}/{item_id}"
    for attempt in range(tries):
        item = lidarr_get(path)
        if item.get("monitored"):
            return True
        item["monitored"] = True
        _put_json(f"{LIDARR_URL}{path}", item, {"X-Api-Key": LIDARR_API_KEY})
        time.sleep(1 + attempt)
    return bool(lidarr_get(path).get("monitored"))


def request_album(album_id: int) -> dict:
    """Ask Lidarr to monitor an album and go look for it now."""
    album = lidarr_get(f"/api/v1/album/{album_id}")

    # An album will not stay monitored while its artist is not: Lidarr accepts
    # the write and drops the flag. An artist imported on demand arrives
    # unmonitored — that is what keeps its whole discography out of the queue —
    # so the artist has to be lifted before the one requested album can be.
    artist_id = album.get("artistId") or (album.get("artist") or {}).get("id")
    if artist_id and not ensure_monitored("artist", artist_id):
        raise BridgeError(f"could not keep artist {artist_id} monitored")

    # Searching an unmonitored album finds releases and grabs none of them, so
    # a request that could not monitor it has failed, however healthy it looks.
    if not ensure_monitored("album", album_id):
        raise BridgeError(f"could not keep album {album_id} monitored")
    _post_json(f"{LIDARR_URL}/api/v1/command",
               {"name": "AlbumSearch", "albumIds": [album_id]},
               {"X-Api-Key": LIDARR_API_KEY})
    return {"requested": album.get("title"), "albumId": album_id}


def resolve(name: str, nd_artist_id: str | None = None) -> tuple[str | None, str, list[dict]]:
    """Map an artist name to a MusicBrainz id.

    Returns (mbid, reason, candidates). One exact match resolves outright. When
    several artists share the name, the library itself decides: the one whose
    catalogue contains the albums already owned is the right one. Only a name
    that nothing distinguishes is left for a human.

    A failed lookup must never abort the caller's whole sync, so every network
    and decoding error is turned into an unresolved result.
    """
    try:
        results = lidarr_lookup(name)
    except FETCH_ERRORS as exc:
        return None, f"lookup failed: {type(exc).__name__}: {exc}", []

    exact = [
        a for a in results
        if isinstance(a, dict)
        and a.get("artistName", "").strip().lower() == name.lower()
        and _is_mbid(a.get("foreignArtistId"))
    ]
    if not exact:
        return None, "not found in MusicBrainz", []
    if len(exact) == 1:
        return exact[0]["foreignArtistId"], "ok", []

    if not nd_artist_id:
        return None, f"ambiguous: {len(exact)} artists share this name", [
            {"mbid": a["foreignArtistId"], "disambiguation": a.get("disambiguation") or ""}
            for a in exact
        ]

    try:
        owned = owned_titles(nd_artist_id)
    except FETCH_ERRORS as exc:
        return None, f"ambiguous, and the library could not be read: {exc}", []

    winner, report = disambiguate(exact, owned)
    if winner is None:
        return None, (f"ambiguous: {len(exact)} artists share this name, and none "
                      f"of their catalogues is a better match for what is owned"), report
    log.info("%r disambiguated by discography -> %s (%s)",
             name, winner["foreignArtistId"], winner.get("disambiguation") or "no note")
    return winner["foreignArtistId"], "ok", []


def _load(path: str, default):
    try:
        with open(path) as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return default
    return loaded if isinstance(loaded, type(default)) else default


def _save(path: str, data) -> None:
    """Atomically replace `path`, safely even with concurrent writers."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    # A per-writer temp name: a shared "<path>.tmp" lets a second writer
    # truncate the file the first one is still streaming into.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f"{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class State:
    """Shared, lock-guarded view of the last successful sync."""

    def __init__(self):
        self.lock = threading.Lock()
        self.mbids: list[str] = []
        self.unresolved: dict[str, dict] = {}
        self.last_sync: str | None = None
        self.last_error: str | None = None
        self.last_push: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "artists": len(self.mbids),
                "unresolved": self.unresolved,
                "last_sync": self.last_sync,
                "last_error": self.last_error,
                "last_push": self.last_push,
                # A failed push is not unhealthy: Lidarr still refreshes on its own.
                "healthy": self.last_sync is not None and self.last_error is None,
            }


STATE = State()


def _read_overrides() -> tuple[dict[str, str], dict[str, object]]:
    """Hand-pinned name -> mbid, split into usable and malformed entries."""
    raw = _load(OVERRIDES_PATH, {})
    good: dict[str, str] = {}
    bad: dict[str, object] = {}
    for name, mbid in raw.items():
        key = str(name).strip()
        if not key:
            continue
        if _is_mbid(mbid):
            good[key] = mbid.strip().lower()
        else:
            bad[key] = mbid
    return good, bad


def sync() -> None:
    with SYNC_LOCK:
        _sync_locked()


def _sync_locked() -> None:
    """Refresh the resolved-id cache from Navidrome's starred artists."""
    cache = _load(CACHE_PATH, {})          # name -> mbid, resolved once and kept
    overrides, bad_overrides = _read_overrides()
    unresolved: dict[str, dict] = {}

    starred = starred_artists()
    names = sorted(starred)
    log.info("navidrome: %d starred artists", len(names))

    now = time.monotonic()
    for name in names:
        # Overrides are merged at publish time, never written into the cache:
        # persisting them would keep a pin alive after it is deleted by hand.
        if name in overrides:
            continue
        if name in bad_overrides:
            unresolved[name] = {
                "reason": f"overrides.json: {bad_overrides[name]!r} is not a MusicBrainz id",
                "candidates": [],
            }
            continue
        tagged = starred[name].get("mbid")
        if tagged:
            cache[name] = tagged  # Navidrome is tagged: a free, exact answer
            continue
        if name in cache:
            continue

        pending = _BACKOFF.get(name)
        if pending and now < pending["retry_at"]:
            unresolved[name] = {
                "reason": pending["reason"],
                "candidates": pending["candidates"],
                "retry_in_seconds": int(pending["retry_at"] - now),
            }
            continue

        mbid, reason, candidates = resolve(name, starred[name].get("id"))
        if mbid:
            cache[name] = mbid
            _BACKOFF.pop(name, None)
            log.info("resolved %r -> %s", name, mbid)
        else:
            attempts = (pending["attempts"] + 1) if pending else 1
            delay = min(REFRESH_SECONDS * (2 ** (attempts - 1)), MAX_BACKOFF_SECONDS)
            _BACKOFF[name] = {
                "attempts": attempts,
                "retry_at": time.monotonic() + delay,
                "reason": reason,
                "candidates": candidates,
            }
            unresolved[name] = {
                "reason": reason,
                "candidates": candidates,
                "retry_in_seconds": int(delay),
            }
            log.warning("unresolved %r: %s (retrying in %ds)", name, reason, delay)
        if LOOKUP_DELAY_SECONDS > 0:
            time.sleep(LOOKUP_DELAY_SECONDS)

    # Drop artists that are no longer starred so unstarring actually removes them.
    cache = {k: v for k, v in cache.items() if k in starred}
    for name in list(_BACKOFF):
        if name not in starred:
            _BACKOFF.pop(name, None)

    try:
        _save(CACHE_PATH, cache)
    except OSError as exc:
        # A cache we could not persist only costs re-resolution after a restart;
        # it must not throw away the artists this pass just resolved.
        log.error("could not persist %s: %s", CACHE_PATH, exc)

    published = dict(cache)
    published.update({n: overrides[n] for n in names if n in overrides})
    mbids = sorted(set(published.values()))
    changed = mbids != _load(PUBLISHED_PATH, [])

    # Publish before telling Lidarr, so the refresh it runs reads the new list.
    with STATE.lock:
        STATE.mbids = mbids
        STATE.unresolved = unresolved
        STATE.last_sync = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        STATE.last_error = None

    if not changed:
        return
    try:
        _save(PUBLISHED_PATH, mbids)
    except OSError as exc:
        # Only costs one redundant refresh after a restart.
        log.error("could not persist %s: %s", PUBLISHED_PATH, exc)
    try:
        outcome = notify_lidarr()
        log.info("list changed (%d artists): %s", len(mbids), outcome)
    except FETCH_ERRORS as exc:
        # Lidarr still picks the change up on its own 6h refresh.
        outcome = f"{type(exc).__name__}: {exc}"
        log.warning("could not refresh lidarr: %s", outcome)
    with STATE.lock:
        STATE.last_push = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {outcome}"


def sync_loop() -> None:
    while True:
        try:
            sync()
        except Exception as exc:  # keep serving the last good list
            log.error("sync failed: %s", exc)
            with STATE.lock:
                STATE.last_error = str(exc)
        time.sleep(REFRESH_SECONDS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Without this an idle keep-alive connection pins a thread forever.
    timeout = 30

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The panel runs on Navidrome's origin and calls this service on another
        # port, so every answer it reads is a cross-origin one.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _respond(self, payload: object, status: int = 200) -> None:
        self._send(json.dumps(payload, indent=2).encode(), "application/json", status)

    def _missing(self, query: dict) -> None:
        artist_id = (query.get("id") or [""])[0]
        if not artist_id:
            self._respond({"error": "missing 'id' (Navidrome artist id)"}, 400)
            return
        try:
            self._respond(missing_albums(artist_id))
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)

    def _request(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._respond({"error": "body is not JSON"}, 400)
            return
        try:
            if body.get("albumId") is not None:
                album_id = int(body["albumId"])
            elif body.get("mbid"):
                mbid = str(body["mbid"])
                album_id = album_id_for_mbid(mbid)
                if album_id is None:
                    # Lidarr only holds albums of artists it imported, and being
                    # sent away to add the artist first is a poor answer to
                    # "fetch me this album". Import it and carry on.
                    album_id = import_artist_for(mbid)
            else:
                raise KeyError("albumId")
        except (ValueError, KeyError, TypeError, AttributeError):
            self._respond({"error": "expected a JSON body with albumId or mbid"}, 400)
            return
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)
            return
        try:
            self._respond(request_album(album_id))
        except FETCH_ERRORS as exc:
            self._respond({"error": f"{type(exc).__name__}: {exc}"}, 502)

    def _panel(self) -> None:
        try:
            with open(PANEL_PATH, "rb") as fh:
                body = fh.read()
        except OSError as exc:
            self._respond({"error": f"panel unavailable: {exc}"}, 404)
            return
        self._send(body, "application/javascript; charset=utf-8")

    def _artists(self) -> None:
        with STATE.lock:
            synced, error, mbids = STATE.last_sync, STATE.last_error, list(STATE.mbids)
        if synced is None:
            # An empty 200 would tell Lidarr "nothing is starred" when the truth
            # is "Navidrome was never reached". Fail loudly instead.
            self._respond({"error": "no successful sync yet", "detail": error}, 503)
            return
        self._respond([{"MusicBrainzId": m} for m in mbids])

    def _status(self) -> None:
        snapshot = STATE.snapshot()
        # Drives the container HEALTHCHECK: a service whose syncs all fail is
        # not healthy, however long it has been answering requests.
        self._respond(snapshot, 200 if snapshot["healthy"] else 503)

    def _sync(self) -> None:
        try:
            sync()
        except Exception as exc:
            with STATE.lock:
                STATE.last_error = str(exc)
            self._respond({"error": str(exc)}, 500)
            return
        self._respond(STATE.snapshot())

    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/", "/artists.json"):
            self._artists()
        elif path == "/status":
            self._status()
        elif path == "/sync":
            self._sync()
        elif path == "/missing":
            self._missing(urllib.parse.parse_qs(parsed.query))
        elif path == "/panel.user.js":
            self._panel()
        else:
            self._respond({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urllib.parse.urlparse(self.path).path
        if path == "/sync":
            self._sync()
        elif path == "/request":
            self._request()
        else:
            self._respond({"error": "not found"}, 404)

    def do_OPTIONS(self):  # noqa: N802 - CORS preflight for POST /request
        self._send(b"", "text/plain", 204)

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    threading.Thread(target=sync_loop, daemon=True).start()
    log.info("serving on :%d", LISTEN_PORT)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
