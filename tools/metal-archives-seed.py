#!/usr/bin/env python3
"""Carry a band from Encyclopaedia Metallum into MusicBrainz.

Lidarr is keyed entirely on MusicBrainz ids. A band MusicBrainz has never heard
of cannot be imported, cannot be monitored, and cannot be requested — the panel
can only say, correctly, that it knows nothing. For underground metal that is
most of the shelf: a Honduran black metal band with three records and a split is
documented in full on Metal Archives and absent from both MusicBrainz and
Discogs.

The way out is not another catalogue to read. It is putting the band in the one
catalogue the pipeline already speaks, once, so everything downstream starts
working on its own — and so the next person looking for that record finds it.

This does the transcription, not the judgement. It gathers what Metal Archives
holds, times the tracks against the files actually on disk, and produces a page
of MusicBrainz forms with every field already filled in. A person reviews them
and presses the button; nothing is submitted from here.

Which band is settled the way it is settled everywhere else in this project:
sixty-seven bands are called "Delirium" on Metal Archives, so the one whose
discography matches the library wins, and a name nothing confirms is left alone.

Usage:

    metal-archives-seed.py --artist <navidrome-artist-id>
    metal-archives-seed.py --unresolved          # every name the bridge gave up on
    metal-archives-seed.py --artist <id> --band-id 3540448871   # settle it by hand
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

NAVIDROME = os.environ.get("NAVIDROME_URL", "http://localhost:4533").rstrip("/")
USER = os.environ.get("NAVIDROME_USER", "")
PASSWORD = os.environ.get("NAVIDROME_PASS", "")
BRIDGE = os.environ.get("BRIDGE_URL", "http://localhost:8687").rstrip("/")

MA = "https://www.metal-archives.com"
# Metal Archives refuses urllib's default headers outright, so every request
# goes out looking like a browser, slowly. It is somebody's server.
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120 Safari/537.36")
MA_DELAY = float(os.environ.get("MA_DELAY", "1.6"))

# Metal Archives release types mapped onto how MusicBrainz files the same thing.
RELEASE_TYPES = {
    "Full-length": ("Album", None),
    "Demo": ("Album", "Demo"),
    "EP": ("EP", None),
    "Single": ("Single", None),
    "Live album": ("Album", "Live"),
    "Compilation": ("Album", "Compilation"),
    "Video": None,      # not a release MusicBrainz's release editor wants here
    "Split": None,      # needs a credit per track; worth doing by hand
    "Split video": None,
    "Boxed set": ("Album", None),
}

_LAST = [0.0]


def fetch(url: str) -> str:
    wait = MA_DELAY - (time.monotonic() - _LAST[0])
    if wait > 0:
        time.sleep(wait)
    _LAST[0] = time.monotonic()
    out = subprocess.run(
        ["curl", "-sSL", "-m", "45", "-A", BROWSER_UA,
         "-H", "Accept-Language: en-US,en;q=0.9", url],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"curl failed for {url}: {out.stderr[:200]}")
    return out.stdout


def strip(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup)).replace("\xa0", " ").strip()


def norm(title: str) -> str:
    """Same folding the bridge uses, so a title matches across catalogues."""
    folded = unicodedata.normalize("NFKD", title.lower())
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    stripped = re.sub(r"\(.*?\)|\[.*?\]", " ", folded)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", stripped).split())


# ── Navidrome ─────────────────────────────────────────────────────────────

_TOKEN: list[str] = []


def navidrome(path: str):
    """Navidrome's own API. Subsonic's `path` is synthesised and points nowhere."""
    if not (USER and PASSWORD):
        sys.exit("set NAVIDROME_USER and NAVIDROME_PASS")
    if not _TOKEN:
        body = json.dumps({"username": USER, "password": PASSWORD}).encode()
        req = urllib.request.Request(f"{NAVIDROME}/auth/login", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _TOKEN.append(json.load(resp)["token"])
    req = urllib.request.Request(f"{NAVIDROME}{path}",
                                 headers={"X-ND-Authorization": f"Bearer {_TOKEN[0]}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def owned_albums(nd_artist_id: str) -> tuple[str, list[dict]]:
    """The artist's name, and every album of theirs the library holds, timed.

    Asked as a plain query parameter: Navidrome ignores a JSON `filter` without
    complaining and hands back the entire library instead.
    """
    artist = navidrome(f"/api/artist/{urllib.parse.quote(nd_artist_id)}")
    albums = navidrome(f"/api/album?artist_id={urllib.parse.quote(nd_artist_id)}"
                       f"&_sort=max_year&_order=ASC&_start=0&_end=500")
    out = []
    for album in albums:
        songs = navidrome(f"/api/song?album_id={urllib.parse.quote(album['id'])}"
                          f"&_sort=track_number&_order=ASC&_start=0&_end=200")
        out.append({
            "title": (album.get("name") or "").strip(),
            "year": album.get("maxYear"),
            "format": (songs[0].get("suffix") if songs else "") or "",
            "tracks": [{"n": s.get("trackNumber"), "title": (s.get("title") or "").strip(),
                        "ms": int(round((s.get("duration") or 0) * 1000))} for s in songs],
        })
    return (artist.get("name") or "").strip(), out


# ── Metal Archives ────────────────────────────────────────────────────────

def search_bands(name: str) -> list[dict]:
    query = urllib.parse.urlencode({
        "field": "name", "query": name, "sEcho": 1,
        "iColumns": 3, "iDisplayStart": 0, "iDisplayLength": 200,
    })
    data = json.loads(fetch(f"{MA}/search/ajax-band-search/?{query}"))
    bands = []
    for row in data.get("aaData", []):
        link = re.search(r'href="([^"]+)"', row[0])
        if not link:
            continue
        bands.append({
            "name": strip(row[0]), "genre": strip(row[1]), "country": strip(row[2]),
            "url": link.group(1), "id": link.group(1).rstrip("/").split("/")[-1],
        })
    return bands


def discography(band_id: str) -> list[dict]:
    page = fetch(f"{MA}/band/discography/id/{band_id}/tab/all")
    out = []
    for url, title in re.findall(
            r'href="(https://www\.metal-archives\.com/albums/[^"]+)"[^>]*>([^<]+)</a>', page):
        out.append({"title": html.unescape(title).strip(), "url": url})
    return out


def release_detail(url: str) -> dict:
    page = fetch(url)
    def field(label: str) -> str:
        found = re.search(rf"<dt>{label}:</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S)
        return strip(found.group(1)) if found else ""
    tracks = []
    for row in re.findall(r'<tr class="(?:even|odd)">(.*?)</tr>', page, re.S):
        cells = [strip(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        if len(cells) >= 3 and re.match(r"^\d+\.?$", cells[0]):
            # A split names the artist inside the title cell; collapse the
            # whitespace Metal Archives leaves around the line break.
            tracks.append({"n": int(cells[0].rstrip(".")),
                           "title": " ".join(cells[1].split()),
                           "length": cells[2]})
    return {"type": field("Type"), "date": field("Release date"),
            "label": field("Label"), "format": field("Format"), "tracks": tracks}


def identify(name: str, owned: list[dict], forced: str | None,
             limit: int = 20) -> dict | None:
    """Which of the bands by this name is the one on the shelf.

    Sixty-seven are called "Delirium". Asking by name alone answers nothing, and
    asking a person to pick is handing the problem back — but unrelated bands do
    not share a back catalogue, so the library settles it. A name nothing
    confirms is left alone rather than guessed at, which is the whole point.
    """
    candidates = search_bands(name)
    if forced:
        picked = next((b for b in candidates if b["id"] == forced), None)
        return picked or {"id": forced, "name": name, "genre": "?", "country": "?",
                          "url": f"{MA}/bands/{urllib.parse.quote(name)}/{forced}"}
    if not candidates:
        print(f"  no band called {name!r} on Metal Archives")
        return None

    mine = {norm(a["title"]) for a in owned if a["title"]}
    print(f"  {len(candidates)} bands called {name!r}")
    scored = []
    for band in candidates[:limit]:
        titles = {norm(r["title"]) for r in discography(band["id"])}
        overlap = {t for t in titles
                   if any(m == t or m.startswith(t + " ") for m in mine)}
        scored.append({"band": band, "overlap": overlap, "total": len(titles)})
        print(f"    {band['country'][:18]:<20} {band['genre'][:26]:<28} "
              f"{len(overlap)} of yours")
    if len(candidates) > limit:
        # Said out loud rather than dropped quietly: a right answer found among
        # the first twenty is still an answer chosen from a truncated list, and
        # a reader deserves to know the search had an edge. --candidates moves it.
        print(f"    ({len(candidates) - limit} more not checked — "
              f"raise --candidates if the right one is missing)")

    scored.sort(key=lambda s: len(s["overlap"]), reverse=True)
    best = scored[0]
    if not best["overlap"]:
        print("  none of them shares an album with your library — not guessing")
        return None
    runner_up = len(scored[1]["overlap"]) if len(scored) > 1 else 0
    if len(best["overlap"]) == runner_up:
        print("  two match equally well — the library cannot tell them apart either")
        return None
    band = best["band"]
    print(f"  -> {band['country']} / {band['genre']} "
          f"({len(best['overlap'])} albums in common)")
    return band


# ── the seeding page ──────────────────────────────────────────────────────

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}

COUNTRY = {"Honduras": "HN", "Guatemala": "GT", "El Salvador": "SV",
           "Nicaragua": "NI", "Costa Rica": "CR", "Panama": "PA", "Mexico": "MX",
           "Argentina": "AR", "Chile": "CL", "Colombia": "CO", "Peru": "PE",
           "Brazil": "BR", "Spain": "ES", "United States": "US"}


def parse_date(text: str) -> tuple[int | None, int | None, int | None]:
    """Metal Archives writes "April 29th, 2025", or just a year, or a month."""
    year = re.search(r"\b(\d{4})\b", text)
    month = re.search(r"\b(" + "|".join(MONTHS) + r")\b", text)
    day = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)\b", text)
    return (int(year.group(1)) if year else None,
            MONTHS[month.group(1)] if month else None,
            int(day.group(1)) if day else None)


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def build_page(artist: str, band: dict, releases: list[dict],
               owned: list[dict], skipped: list[tuple[str, str]]) -> str:
    by_title = {norm(a["title"]): a for a in owned}
    year, _, _ = parse_date(band.get("formed", "") or "")

    seed = {
        "edit-artist.name": artist,
        "edit-artist.sort_name": artist,
        "edit-artist.type_id": "2",   # Group
        "edit-artist.area.name": band.get("country", ""),
        # "black metal band from Honduras" rather than "Honduras black metal
        # band": the country is a noun, and guessing demonyms for every country
        # on Metal Archives is a worse bet than a phrasing that always reads.
        "edit-artist.comment": (
            f"{band.get('genre','').split('/')[0].strip().lower()} band"
            + (f" from {band['country']}" if band.get("country") else "")).strip(),
        "edit-artist.url.0.text": band["url"],
        "edit-artist.url.0.link_type_id": "181",   # other databases
        "edit-artist.edit_note": (
            f"{band.get('genre','')} band from "
            f"{band.get('location') or band.get('country','')}. "
            f"Source: Encyclopaedia Metallum, {band['url']}"),
    }
    if year:
        seed["edit-artist.period.begin_date.year"] = str(year)
    if band.get("location"):
        seed["edit-artist.begin_area.name"] = band["location"].split(",")[0].strip()
    artist_url = "https://musicbrainz.org/artist/create?" + urllib.parse.urlencode(seed)

    cards = []
    for rel in releases:
        primary, secondary = RELEASE_TYPES.get(rel["type"], ("Album", None))
        y, m, d = parse_date(rel["date"])
        mine = by_title.get(norm(rel["title"]))
        fields: list[tuple[str, str]] = [
            ("name", rel["title"]),
            ("artist_credit.names.0.name", artist),
            ("type", primary), ("status", "Official"),
            ("script", "Latn"),
            ("mediums.0.format", "Digital Media" if "Digital" in rel["format"] else rel["format"]),
            ("edit_note", f"Track list and release details from Encyclopaedia Metallum: "
                          f"{rel['url']}" + ("\nDurations taken from the release itself."
                                             if mine else "")),
            ("urls.0.url", rel["url"]),
        ]
        if secondary:
            fields.append(("type", secondary))
        for key, value in (("events.0.date.year", y), ("events.0.date.month", m),
                           ("events.0.date.day", d)):
            if value:
                fields.append((key, value))
        if band.get("country") in COUNTRY:
            fields.append(("events.0.country", COUNTRY[band["country"]]))
        if rel["label"] and rel["label"].lower() != "independent":
            fields.append(("labels.0.name", rel["label"]))
        for i, track in enumerate(rel["tracks"]):
            fields.append((f"mediums.0.track.{i}.number", track["n"]))
            fields.append((f"mediums.0.track.{i}.name", track["title"]))
            got = next((t for t in (mine or {}).get("tracks", [])
                        if t["n"] == track["n"]), None)
            if got and got["ms"]:
                fields.append((f"mediums.0.track.{i}.length", got["ms"]))

        rows = []
        for track in rel["tracks"]:
            got = next((t for t in (mine or {}).get("tracks", [])
                        if t["n"] == track["n"]), None)
            shown = (f"{got['ms'] // 60000}:{got['ms'] // 1000 % 60:02d}"
                     if got and got["ms"] else track["length"] or "—")
            rows.append(f"<tr><td>{track['n']}</td><td>{esc(track['title'])}</td>"
                        f"<td class=len>{esc(shown)}</td></tr>")
        held = (f'<span class="own">you own it in {mine["format"].upper()}</span>'
                if mine and mine["format"] else '<span class="miss">not in your library</span>')
        cards.append(f"""
  <section class="card">
    <h3>{esc(rel['title'])}</h3>
    <p class="meta">{esc(rel['type'])} &middot; {esc(rel['date'])} &middot;
       {esc(rel['format'])} &middot; {esc(rel['label'])} &middot;
       {len(rel['tracks'])} tracks &middot; {held}</p>
    <table><thead><tr><th>#</th><th>Track</th><th class="len">Length</th></tr></thead>
      <tbody>{''.join(rows)}</tbody></table>
    <form method="post" action="https://musicbrainz.org/release/add" target="_blank">
      {chr(10).join(f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">'
                    for k, v in fields)}
      <input type="hidden" name="artist_credit.names.0.mbid" value="" class="mbid">
      <button type="submit">Open this release in MusicBrainz &rarr;</button>
    </form>
  </section>""")

    left_out = "".join(
        f"<li><strong>{esc(t)}</strong> &mdash; {esc(why)}</li>" for t, why in skipped)
    left_out_block = (f'<h2>Left out</h2><div class="step"><ul>{left_out}</ul>'
                      f'<p class="note">Nothing here is refused on principle; each one '
                      f'needs a decision this tool should not make for you.</p></div>'
                      if skipped else "")

    return PAGE.replace("__ARTIST__", esc(artist)) \
               .replace("__SUB__", esc(f"{band.get('genre','')} · {band.get('country','')}"
                                       + (f" · formed {year}" if year else ""))) \
               .replace("__ARTIST_URL__", esc(artist_url)) \
               .replace("__CARDS__", "".join(cards)) \
               .replace("__LEFTOUT__", left_out_block) \
               .replace("__SOURCE__", esc(band["url"]))


PAGE = """<title>__ARTIST__ &rarr; MusicBrainz</title>
<style>
  :root { --bg:#fbfaf8; --fg:#1c1a17; --muted:#6b655d; --line:#e2ddd5;
          --card:#fff; --accent:#7a2e2e; --warn-bg:#fdf3e7; --warn-line:#e0b070;
          --ok:#2f6b45; --miss:#8a7a55; }
  @media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
    --bg:#16151a; --fg:#eceaf0; --muted:#9b95a3; --line:#302d38; --card:#1e1c24;
    --accent:#e08585; --warn-bg:#2b2417; --warn-line:#8a6a35; --ok:#7fbf9a;
    --miss:#c0aa72; }}
  :root[data-theme="dark"] {
    --bg:#16151a; --fg:#eceaf0; --muted:#9b95a3; --line:#302d38; --card:#1e1c24;
    --accent:#e08585; --warn-bg:#2b2417; --warn-line:#8a6a35; --ok:#7fbf9a;
    --miss:#c0aa72; }
  body { background:var(--bg); color:var(--fg); margin:0;
         font:16px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
  .wrap { max-width:820px; margin:0 auto; padding:40px 22px 90px; }
  h1 { font-size:1.7rem; margin:0 0 6px; letter-spacing:-.02em; }
  .sub { color:var(--muted); margin:0 0 30px; }
  h2 { font-size:1.05rem; text-transform:uppercase; letter-spacing:.08em;
       color:var(--muted); margin:40px 0 14px; font-weight:600; }
  .step { border-left:3px solid var(--accent); padding:2px 0 2px 16px; margin:0 0 24px; }
  .warn { background:var(--warn-bg); border:1px solid var(--warn-line);
          border-radius:8px; padding:14px 16px; margin:0 0 24px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:18px 20px; margin:0 0 18px; }
  .card h3 { margin:0 0 4px; font-size:1.1rem; }
  .meta { color:var(--muted); font-size:.86rem; margin:0 0 14px; }
  .own { color:var(--ok); font-weight:600; }
  .miss { color:var(--miss); font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:.88rem; margin:0 0 16px;
          display:block; overflow-x:auto; }
  th { text-align:left; font-weight:600; color:var(--muted); font-size:.76rem;
       text-transform:uppercase; letter-spacing:.06em;
       border-bottom:1px solid var(--line); padding:5px 8px 5px 0; }
  td { padding:4px 8px 4px 0; border-bottom:1px solid var(--line); }
  td:first-child, th:first-child { width:2.4em; color:var(--muted); }
  .len { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  button, .btn { background:var(--accent); color:#fff; border:0; border-radius:7px;
          padding:10px 18px; font:inherit; font-weight:600; cursor:pointer;
          text-decoration:none; display:inline-block; }
  button:hover, .btn:hover { filter:brightness(1.12); }
  input[type=text] { font:inherit; padding:9px 12px; border:1px solid var(--line);
          border-radius:7px; background:var(--bg); color:var(--fg);
          width:100%; max-width:420px; box-sizing:border-box; }
  code { background:var(--line); padding:1px 6px; border-radius:4px; font-size:.86em; }
  .note { color:var(--muted); font-size:.88rem; }
  ul { padding-left:1.2em; }
</style>
<div class="wrap">
<h1>__ARTIST__ &rarr; MusicBrainz</h1>
<p class="sub">__SUB__ &middot; <a href="__SOURCE__" target="_blank">Metal Archives</a><br>
Every field below is already filled in &mdash; titles and release details from
Encyclopaedia Metallum, track lengths measured from the files in your own library.</p>

<div class="warn">
  <strong>Check the duplicate warning.</strong> MusicBrainz will list any existing
  artist with a similar name and hold the <em>Enter edit</em> button until you tick
  <em>&ldquo;Yes, I still want to enter this&rdquo;</em>. Read that list first: if one of
  them <em>is</em> your band, use it instead of making a second entry.
</div>

<h2>Step 1 &mdash; create the artist</h2>
<div class="step">
  <p>Opens the MusicBrainz artist editor with name, type, area and the Metal Archives
  link already filled in. Review, tick the duplicate box if the warning appears, save.</p>
  <p><a class="btn" href="__ARTIST_URL__" target="_blank">Create the artist &rarr;</a></p>
</div>

<h2>Step 2 &mdash; paste the new artist id</h2>
<div class="step">
  <p>After saving, the address bar reads <code>musicbrainz.org/artist/&lt;id&gt;</code>.
  Paste that id, or the whole URL:</p>
  <p><input type="text" id="mbid" placeholder="e.g. 4f2c1b8e-... or the full URL"
     autocomplete="off"></p>
  <p class="note" id="status">Nothing pasted yet &mdash; the forms below would ask
  MusicBrainz to match by name, which is exactly what you want to avoid.</p>
</div>

<h2>Step 3 &mdash; add the releases</h2>
__CARDS__
__LEFTOUT__

<h2>What this unlocks</h2>
<div class="step">
  <p>Lidarr is keyed entirely on MusicBrainz ids. Once this artist exists there, the
  panel identifies the band by catalogue overlap on its own, Lidarr can import it, and
  the Request button starts working for whatever is missing. Until then the panel
  correctly shows nothing rather than another band&rsquo;s records.</p>
</div>
</div>
<script>
  (function () {
    var input = document.getElementById("mbid");
    var status = document.getElementById("status");
    var UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
    var KEY = "ma-seed-mbid:" + document.title;
    function apply() {
      var found = UUID.exec(input.value || "");
      var id = found ? found[0].toLowerCase() : "";
      document.querySelectorAll("input.mbid").forEach(function (el) { el.value = id; });
      if (id) {
        status.textContent = "Every form below will credit " + id + ".";
        status.style.color = "var(--ok)";
        try { localStorage.setItem(KEY, id); } catch (e) {}
      } else {
        status.textContent = input.value
          ? "That does not contain a MusicBrainz id yet."
          : "Nothing pasted yet \\u2014 the forms below would ask MusicBrainz to match by name, which is exactly what you want to avoid.";
        status.style.color = "";
      }
    }
    input.addEventListener("input", apply);
    try {
      var saved = localStorage.getItem(KEY);
      if (saved) { input.value = saved; apply(); }
    } catch (e) {}
  })();
</script>"""


# ── driving it ────────────────────────────────────────────────────────────

def band_details(band: dict) -> dict:
    page = fetch(band["url"])
    def field(label: str) -> str:
        found = re.search(rf"<dt>{label}:</dt>\s*<dd[^>]*>(.*?)</dd>", page, re.S)
        return strip(found.group(1)) if found else ""
    band = dict(band)
    band["country"] = field("Country of origin") or band.get("country", "")
    band["genre"] = field("Genre") or band.get("genre", "")
    band["formed"] = field("Formed in")
    band["location"] = field("Location")
    return band


def seed_one(nd_artist_id: str, forced: str | None, out_dir: str,
             limit: int = 20) -> str | None:
    name, owned = owned_albums(nd_artist_id)
    print(f"\n  {name} — {len(owned)} albums in the library")
    band = identify(name, owned, forced, limit)
    if not band:
        return None
    band = band_details(band)

    releases, skipped = [], []
    for entry in discography(band["id"]):
        detail = release_detail(entry["url"])
        mapped = RELEASE_TYPES.get(detail["type"], ("Album", None))
        if mapped is None:
            skipped.append((entry["title"],
                            f"{detail['type'].lower()} — needs a credit per track, "
                            f"better entered by hand"))
            continue
        if not detail["tracks"]:
            skipped.append((entry["title"], "no track list on Metal Archives"))
            continue
        releases.append({**entry, **detail})
        print(f"    {entry['title'][:44]:<46} {detail['type']:<12} "
              f"{len(detail['tracks'])} tracks")
    for title, why in skipped:
        print(f"    {title[:44]:<46} left out: {why}")

    if not releases:
        print("  nothing to seed")
        return None

    page = build_page(name, band, releases, owned, skipped)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "band"
    path = os.path.join(out_dir, f"{slug}-musicbrainz.html")
    with open(path, "w") as fh:
        fh.write(page)
    print(f"  -> {path}")
    return path


def unresolved_names() -> list[str]:
    """Every name the bridge could not answer — which is exactly this list."""
    with urllib.request.urlopen(f"{BRIDGE}/status", timeout=30) as resp:
        return sorted((json.load(resp).get("unresolved") or {}).keys())


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--artist", help="Navidrome artist id")
    ap.add_argument("--unresolved", action="store_true",
                    help="every name the bridge gave up on")
    ap.add_argument("--band-id", help="Metal Archives band id, to settle it by hand")
    ap.add_argument("--out", default=os.path.expanduser("~"),
                    help="where to write the pages (default: your home directory)")
    ap.add_argument("--candidates", type=int, default=20,
                    help="how many same-named bands to weigh (default 20). Each "
                         "costs one request to somebody else's server.")
    args = ap.parse_args()
    if not args.artist and not args.unresolved:
        ap.error("give --artist, or --unresolved")

    targets: list[tuple[str, str | None]] = []
    if args.artist:
        targets.append((args.artist, args.band_id))
    if args.unresolved:
        names = unresolved_names()
        print(f"  the bridge is stuck on {len(names)} names")
        index = {a["name"].strip().lower(): a["id"]
                 for a in navidrome("/api/artist?_start=0&_end=5000")}
        for name in names:
            found = index.get(name.lower())
            if found:
                targets.append((found, None))
            else:
                print(f"    {name}: no such artist in Navidrome, skipping")

    written = [p for p in (seed_one(nd, forced, args.out, args.candidates)
                           for nd, forced in targets) if p]
    print(f"\n  {len(written)} page(s) written; open them and press the buttons")


if __name__ == "__main__":
    main()
