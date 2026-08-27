#!/usr/bin/env python3
"""Configure Lidarr, Prowlarr and qBittorrent the way this system needs them.

Standing the containers up is the easy half. The half that took a week to get
right is the settings, and every one of them here was arrived at by watching
something go wrong:

  * The metadata profile has to allow every album type, or a demo or a live set
    held in the library is invisible to Lidarr — it carries a quality badge in
    the panel with no id behind it, so there is nothing to press.

  * Which means monitorNewItems has to be "new" FIRST. Widen the profile while
    artists are on "all" and Lidarr monitors an entire back catalogue at once.
    The order is not a preference.

  * minimumSeeders lives in Prowlarr's app profile, not in Lidarr. Set it in
    Lidarr and Prowlarr's next full sync quietly puts it back.

  * The quality profile allows Unknown deliberately, and ranks it last. Most
    releases declare no format at all; refusing them outright leaves only the
    ones that declare mp3.

  * The release profile rejects vinyl rips. They are someone's turntable, not
    the record, and they beat a real lossless copy on file size alone.

Everything is idempotent. --check changes nothing and reports what differs, so
it is safe to run against a system already in use.

Usage:

    bootstrap.py --check      # report drift, change nothing
    bootstrap.py              # make it so
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

LIDARR = os.environ.get("LIDARR_URL", "http://localhost:8686").rstrip("/")
PROWLARR = os.environ.get("PROWLARR_URL", "http://localhost:9696").rstrip("/")
QBIT = os.environ.get("QBIT_URL", "http://localhost:8090").rstrip("/")
BRIDGE_HOST = os.environ.get("BRIDGE_HOST", "navidrome-lidarr-bridge")
BRIDGE_PORT = os.environ.get("BRIDGE_PORT", "8687")
ROOT_FOLDER = os.environ.get("LIDARR_ROOT_FOLDER", "/data/Musica/Lidarr")
QBIT_USER = os.environ.get("QBITTORRENT_USER", "admin")
QBIT_PASS = os.environ.get("QBITTORRENT_PASS", "")
QBIT_HOST = os.environ.get("QBIT_HOST", "qbittorrent")
QBIT_CATEGORY = os.environ.get("QBIT_LIDARR_CATEGORY", "lidarr")
MIN_SEEDERS = int(os.environ.get("MIN_SEEDERS", "5"))

# Rejected outright. A needledrop is a recording of somebody's turntable — it is
# genuinely lossless and it is not the record, and it outweighs a real lossless
# copy on the one signal Lidarr can see without listening.
VINYL = r"(?i)\b(vinyl|vinilo|pbthal|needledrop|needle.?drop|\[LP\]|analog rip)\b"

CHANGES: list[str] = []
DRIFT: list[str] = []
CHECK = False


def note(made: bool, message: str) -> None:
    (CHANGES if made else DRIFT).append(message)
    print(f"    {'changed ' if made else 'WOULD CHANGE'}  {message}")


def ok(message: str) -> None:
    print(f"    already      {message}")


def call(base: str, key: str, path: str, method: str = "GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{base}/api/v1/{path}", data=data, method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def api_key(service: str) -> str:
    """Read a *arr's key from the config it writes on first start.

    Asked for rather than generated: these services mint their own on first run,
    and a key invented here would simply be wrong.
    """
    env = os.environ.get(f"{service.upper()}_API_KEY", "").strip()
    if env:
        return env
    path = os.path.join(HERE, "config", service, "config.xml")
    try:
        found = re.search(r"<ApiKey>([^<]+)</ApiKey>", open(path).read())
    except OSError:
        found = None
    if not found:
        sys.exit(f"no {service} API key: set {service.upper()}_API_KEY, "
                 f"or start the stack once so it writes {path}")
    return found.group(1)


def wait_for(name: str, base: str, key: str, seconds: int = 180) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            call(base, key, "system/status")
            print(f"  {name} is up")
            return
        except Exception:
            time.sleep(4)
    sys.exit(f"{name} did not answer within {seconds}s at {base}")


# ── Lidarr ────────────────────────────────────────────────────────────────

def lidarr_root(key: str) -> None:
    if any(r.get("path") == ROOT_FOLDER for r in call(LIDARR, key, "rootfolder")):
        ok(f"root folder {ROOT_FOLDER}")
        return
    if CHECK:
        note(False, f"add root folder {ROOT_FOLDER}")
        return
    call(LIDARR, key, "rootfolder", "POST", {"path": ROOT_FOLDER})
    note(True, f"added root folder {ROOT_FOLDER}")


def lidarr_metadata_profile(key: str) -> int:
    """Allow every album type, so everything owned can be paired and badged.

    The missing list filters instead. A single is catalogued so that a copy of
    it already on the shelf can carry a badge, and is never reported as a gap.
    """
    profile = next((p for p in call(LIDARR, key, "metadataprofile")
                    if p["name"] == "Standard"), None)
    if profile is None:
        sys.exit("Lidarr has no metadata profile called 'Standard'")
    blocked = [t for t in profile["primaryAlbumTypes"] + profile["secondaryAlbumTypes"]
               if not t.get("allowed")]
    if not blocked:
        ok("metadata profile allows every album type")
        return profile["id"]
    names = ", ".join(t["albumType"]["name"] for t in blocked[:6])
    if CHECK:
        note(False, f"metadata profile still excludes {len(blocked)} type(s): {names}")
        return profile["id"]
    # Order matters and this is the whole reason: widening while artists are on
    # "all" monitors a back catalogue at once.
    artists = call(LIDARR, key, "artist")
    stale = [a["id"] for a in artists if a.get("monitorNewItems") not in (None, "new", "none")]
    if stale:
        call(LIDARR, key, "artist/editor", "PUT",
             {"artistIds": stale, "monitorNewItems": "new"})
        note(True, f"{len(stale)} artist(s) set to monitorNewItems=new, before widening")
    for entry in profile["primaryAlbumTypes"] + profile["secondaryAlbumTypes"]:
        entry["allowed"] = True
    call(LIDARR, key, f"metadataprofile/{profile['id']}", "PUT", profile)
    note(True, f"metadata profile now allows every album type (was missing {names})")
    return profile["id"]


def lidarr_quality_profile(key: str) -> int:
    """Lossless preferred, upgrades on, cutoff at 24-bit.

    Unknown stays allowed on purpose and ranks last: most releases declare no
    format, and refusing them leaves only the ones that declare mp3.
    """
    profile = next((p for p in call(LIDARR, key, "qualityprofile")
                    if p["name"] == "Lossless"), None)
    if profile is None:
        note(False if CHECK else True,
             "no quality profile called 'Lossless' — create one with cutoff "
             "'Lossless 24bit' and upgrades enabled")
        return 0
    problems = []
    if not profile.get("upgradeAllowed"):
        problems.append("upgrades are disabled")
    cutoff_name = ""
    for item in profile.get("items", []):
        ident = item.get("id") if not item.get("quality") else item["quality"]["id"]
        if ident == profile.get("cutoff"):
            cutoff_name = item.get("name") or item["quality"]["name"]
    if "24" not in cutoff_name:
        problems.append(f"cutoff is {cutoff_name!r}, not a 24-bit lossless group")
    if not problems:
        ok(f"quality profile 'Lossless' (cutoff {cutoff_name}, upgrades on)")
        return profile["id"]
    if CHECK:
        note(False, "quality profile 'Lossless': " + "; ".join(problems))
        return profile["id"]
    profile["upgradeAllowed"] = True
    call(LIDARR, key, f"qualityprofile/{profile['id']}", "PUT", profile)
    note(True, "quality profile 'Lossless': " + "; ".join(problems) + " — upgrades enabled")
    return profile["id"]


# Matched on stems rather than on the exact pattern. A profile written by hand
# spells the same rule its own way — "needle-?\\s?drop" against "needledrop" —
# and a check that demands the exact string decides nothing is there and adds a
# second profile beside the first.
VINYL_STEMS = ("vinyl", "vinilo", "pbthal", "needle", "analog")


def lidarr_release_profile(key: str) -> None:
    existing = call(LIDARR, key, "releaseprofile")
    for profile in existing:
        rules = " ".join(profile.get("ignored") or []).lower()
        if any(stem in rules for stem in VINYL_STEMS):
            ok("release profile rejecting vinyl rips")
            return
    if CHECK:
        note(False, "add a release profile rejecting vinyl rips")
        return
    call(LIDARR, key, "releaseprofile", "POST", {
        "enabled": True, "required": [], "ignored": [VINYL],
        "indexerId": 0, "tags": [],
    })
    note(True, "added a release profile rejecting vinyl rips")


def lidarr_import_list(key: str, quality_id: int, metadata_id: int) -> None:
    """The list the bridge publishes, consumed as a Custom List.

    searchOnAdd stays off: an artist arriving from the list should be
    catalogued, not have their whole discography queued.
    """
    url = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}/artists.json"
    for lst in call(LIDARR, key, "importlist"):
        fields = {f["name"]: f.get("value") for f in lst.get("fields", [])}
        # Lidarr calls this field baseUrl on a Custom List and url elsewhere;
        # looking only for "url" reported an existing list as missing and would
        # have added a duplicate pointing at the same address.
        current = fields.get("baseUrl") or fields.get("url") or ""
        if current.rstrip("/") == url.rstrip("/"):
            ok(f"import list {lst['name']!r} -> {url}")
            return
        if lst.get("implementation") == "CustomImport" and current:
            note(False, f"import list {lst['name']!r} points at {current}, "
                        f"not {url} — left alone")
            return
    if CHECK:
        note(False, f"add a Custom List import list pointing at {url}")
        return
    schema = next((s for s in call(LIDARR, key, "importlist/schema")
                   if s.get("implementation") == "CustomImport"), None)
    if schema is None:
        note(False, "Lidarr offers no CustomImport list type")
        return
    for field in schema.get("fields", []):
        if field["name"] in ("baseUrl", "url"):
            field["value"] = url
    schema.update({
        "name": "Navidrome Starred", "enableAutomaticAdd": True,
        "shouldMonitor": "entireArtist", "shouldSearch": False,
        "qualityProfileId": quality_id, "metadataProfileId": metadata_id,
        "rootFolderPath": ROOT_FOLDER, "monitorNewItems": "new", "tags": [],
    })
    call(LIDARR, key, "importlist", "POST", schema)
    note(True, f"added import list 'Navidrome Starred' -> {url}")


# ── Prowlarr ──────────────────────────────────────────────────────────────

def prowlarr_seeders(key: str) -> None:
    """Where the seeder floor actually lives.

    Set it in Lidarr and Prowlarr's next full sync puts it back, silently.
    """
    for profile in call(PROWLARR, key, "appprofile"):
        current = profile.get("minimumSeeders")
        if current == MIN_SEEDERS:
            ok(f"app profile {profile['name']!r} minimumSeeders={MIN_SEEDERS}")
            continue
        if CHECK:
            note(False, f"app profile {profile['name']!r} minimumSeeders "
                        f"{current} -> {MIN_SEEDERS}")
            continue
        profile["minimumSeeders"] = MIN_SEEDERS
        call(PROWLARR, key, f"appprofile/{profile['id']}", "PUT", profile)
        note(True, f"app profile {profile['name']!r} minimumSeeders "
                   f"{current} -> {MIN_SEEDERS}")


def prowlarr_flaresolverr(key: str) -> None:
    if any(p.get("implementation") == "FlareSolverr"
           for p in call(PROWLARR, key, "indexerproxy")):
        ok("FlareSolverr proxy configured")
        return
    note(False, "add a FlareSolverr proxy tagged 'flaresolverr', and tag the "
                "indexers that sit behind Cloudflare with it")


def prowlarr_app(key: str, lidarr_key: str) -> None:
    for app in call(PROWLARR, key, "applications"):
        if app.get("implementation") == "Lidarr":
            ok(f"Lidarr connected to Prowlarr (syncLevel={app.get('syncLevel')})")
            return
    note(False, f"connect Lidarr to Prowlarr (Settings -> Apps), pointing at "
                f"{LIDARR} with its API key")


# ── qBittorrent ───────────────────────────────────────────────────────────

def qbit_category() -> None:
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(
            f"{QBIT}/api/v2/auth/login",
            data=urllib.parse.urlencode({"username": QBIT_USER,
                                         "password": QBIT_PASS}).encode(),
            headers={"Referer": QBIT}), timeout=30).read()
        cats = json.load(opener.open(f"{QBIT}/api/v2/torrents/categories", timeout=30))
    except Exception as exc:
        note(False, f"could not reach qBittorrent at {QBIT}: {type(exc).__name__}")
        return
    if QBIT_CATEGORY in cats:
        ok(f"qBittorrent category {QBIT_CATEGORY!r}")
        return
    if CHECK:
        note(False, f"add qBittorrent category {QBIT_CATEGORY!r}")
        return
    opener.open(urllib.request.Request(
        f"{QBIT}/api/v2/torrents/createCategory",
        data=urllib.parse.urlencode({"category": QBIT_CATEGORY,
                                     "savePath": "/data/torrents/music"}).encode(),
        headers={"Referer": QBIT}), timeout=30).read()
    note(True, f"added qBittorrent category {QBIT_CATEGORY!r}")


def main() -> None:
    global CHECK
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report what differs and change nothing")
    args = ap.parse_args()
    CHECK = args.check

    lkey, pkey = api_key("lidarr"), api_key("prowlarr")
    print("\n  waiting for the services")
    wait_for("Lidarr", LIDARR, lkey)
    wait_for("Prowlarr", PROWLARR, pkey)

    print("\n  Lidarr")
    lidarr_root(lkey)
    metadata_id = lidarr_metadata_profile(lkey)
    quality_id = lidarr_quality_profile(lkey)
    lidarr_release_profile(lkey)
    lidarr_import_list(lkey, quality_id, metadata_id)

    print("\n  Prowlarr")
    prowlarr_seeders(pkey)
    prowlarr_flaresolverr(pkey)
    prowlarr_app(pkey, lkey)

    print("\n  qBittorrent")
    qbit_category()

    print()
    if CHECK:
        if DRIFT:
            print(f"  {len(DRIFT)} setting(s) differ from what this system needs.")
            print("  Run without --check to apply what can be applied.")
            sys.exit(1)
        print("  everything matches")
        return
    print(f"  {len(CHANGES)} change(s) applied.")
    if DRIFT:
        print(f"  {len(DRIFT)} thing(s) need a person — they are listed above.")


if __name__ == "__main__":
    main()
