# navidrome-lidarr-bridge

Star an artist in Navidrome and Lidarr starts monitoring them. Open an artist
page and the albums you are missing appear greyed out in the grid, each with a
button that asks Lidarr to go find it.

![Architecture](docs/architecture.png)

The loop closes on its own: what Lidarr imports lands inside Navidrome's own
music folder, so the album is indexed and stops being missing without anyone
touching anything.

## Quick start

1. Copy `.env.example` to `.env` and fill in the Navidrome credentials and the
   Lidarr API key.
2. Add the service to the compose file that already runs Lidarr — see
   `docker-compose.example.yml` — and bring it up.
3. In Lidarr, add an import list of type **Custom List** pointing at
   `http://navidrome-lidarr-bridge:8687/artists.json`, with the quality profile
   and root folder you want new artists to use.
4. Install a panel in Navidrome — see
   [The panel inside Navidrome](#the-panel-inside-navidrome).

Star an artist and it appears in Lidarr within `REFRESH_SECONDS`.

## Why a name→id lookup step

Lidarr's `CustomImport` accepts only MusicBrainz ids:

```json
[{"MusicBrainzId": "24e1b53c-3085-4581-8472-0b0088d2508c"}]
```

Libraries tagged without MusicBrainz fields therefore need every starred artist
resolved by name first. The bridge asks Lidarr's own `/api/v1/artist/lookup`,
so the id it emits is exactly the one Lidarr uses when adding the artist.

## Ambiguity is settled by the library, not by a coin toss

Ten different artists are called "Delirium". Picking the first would silently
monitor the wrong discography, and asking the user to pick is just handing the
problem back.

But unrelated bands sharing a name do not share a back catalogue. Holding
`Abismo` and `Los signos del Fauno` identifies exactly one of those ten — the
Honduran metal band — and nothing else comes close. So when several artists
match a name, each candidate's discography is fetched from MusicBrainz and
compared against what the library already holds; the one that overlaps wins.

A tie is not an answer: if two catalogues match equally well, the library cannot
tell them apart either, and the name is left unresolved. So is a name where
nothing matches at all. Those appear on `/status` with every candidate and what
each had in common, so the pin is an informed one:

```json
{
  "unresolved": {
    "Delirium": {
      "reason": "ambiguous: 6 artists share this name",
      "candidates": [
        {"mbid": "03a6ea55-...", "disambiguation": "Italian progressive rock band"},
        {"mbid": "a76754ec-...", "disambiguation": "Dutch punk band"}
      ]
    }
  }
}
```

Pin the right one in `overrides.json` inside the state volume:

```json
{"Delirium": "03a6ea55-25e8-4d03-a98b-ba0f2ccb4ca5"}
```

Overrides are merged when the feed is published and never written into
`resolved.json`, so editing or deleting an entry takes effect on the next sync.
A value that is not a MusicBrainz id is refused and reported on `/status`
instead of being handed to Lidarr.

A name that cannot be resolved is retried with exponential backoff (starting at
`REFRESH_SECONDS`, capped by `MAX_BACKOFF_SECONDS`) rather than on every pass,
because `/artist/lookup` reaches the rate-limited `api.lidarr.audio`. When
Navidrome already carries a MusicBrainz tag for a starred artist, the bridge
uses it directly and skips the lookup entirely.

## Pushing the change to Lidarr

`CustomImport` declares `MinRefreshInterval = 6h`, so the scheduled
`ImportListSync` reads the list at most every six hours no matter how often it
runs — star an artist and it sits there, with Lidarr logging `No list items to
process` while the feed already serves it.

So whenever the published set changes, the bridge posts an `ImportListSync`
command carrying `definitionId`. That takes Lidarr's single-list path, which
fetches immediately instead of consulting the interval.

The set it last pushed is persisted, so a restart is not mistaken for a change.
A push that fails is logged and reported on `/status` but never fails the sync:
Lidarr still picks the change up on its own six-hour refresh.

## Endpoints

| Path              | Purpose                                             | Status                                |
|-------------------|-----------------------------------------------------|---------------------------------------|
| `/artists.json`   | the list Lidarr consumes (also served at `/`)       | `503` until the first successful sync |
| `/status`         | last sync, counts, unresolved names + candidates    | `503` when the last sync failed       |
| `/sync`           | force a refresh now (`GET` or `POST`)               | `500` if that refresh fails           |
| `/missing?id=`    | albums an artist is missing, by Navidrome artist id | `502` if either service is unreachable |
| `/request`        | `POST {"albumId"\|"mbid"}` — monitor it and search   | `404` if Lidarr has no such album     |
| `/panel.user.js`  | the userscript that draws the panel in Navidrome    | `404` if the file is missing          |

`/request` takes either a Lidarr album id or a MusicBrainz release-group id.
Lidarr stores that release-group id as `foreignAlbumId`, so a caller that
already speaks MusicBrainz — as any MusicBrainz-driven panel does — needs no
translation step.

An album whose artist Lidarr has never imported is still requestable: Lidarr
resolves a release-group id to its artist even for artists it does not hold, so
the bridge imports that artist and carries on rather than sending the caller
away to add it first. The artist is added with `monitor: none`, and only the one
album that was actually asked for is then monitored — added the usual way, a
single click would queue the entire discography.

That last step has an order to it. Lidarr accepts a write monitoring an album
whose artist is unmonitored, and then silently drops the flag, so the artist has
to be monitored first. Profiles and root folder for the new artist are read from
the import list, so a requested artist lands exactly where a starred one would.

## The panel inside Navidrome

Navidrome exposes no UI extension point — the eight plugin capabilities are
metadata, lifecycle, lyrics, scheduler, scrobbler, sonicsimilarity, taskworker
and websocket, and none of them renders anything. `ui/src/plugin/` is the admin
screen *for* plugins, not an extension point. So the panel is injected, and
there are two ways to do it.

**A userscript.** [navidrome-missing-albums-userscript][mau] reads the full
discography from MusicBrainz, greys out the albums you do not have inside
Navidrome's own grid, and puts a request button over each cover — it probes this
bridge and only draws the button when it answers. Install it in Tampermonkey or
Violentmonkey; it self-updates. The `/panel.user.js` served by this bridge is a
simpler alternative that draws a floating card instead.

[mau]: https://github.com/danielbanariba/navidrome-missing-albums-userscript

**An nginx front.** `nginx/navidrome-panel.conf` proxies Navidrome and injects
the panel into the HTML with `sub_filter`, so every browser and phone gets it
with nothing installed. Navidrome keeps serving its own port untouched.

Both draw over the page rather than into it: Navidrome is a React app and would
drop an injected child on its next render. The userscript hangs its button on
the tile wrapper rather than the cover container, because that container carries
`grayscale` and reduced `opacity` and children inherit both — the cover is meant
to look faded, the button is not.

### Why "missing" is not Lidarr's own count

Lidarr only sees its own root folder. A library organised anywhere else reads
as entirely missing — for one artist here Lidarr reported 13 missing albums
while 11 of them sat in the library in FLAC. So `/missing` diffs Lidarr's
discography against what Navidrome actually holds, and Navidrome wins.

Titles are compared with case, punctuation and parenthesised edition notes
stripped, since one side reports file tags and the other reports MusicBrainz.
That also collapses a `(Live)` variant onto the studio album of the same name,
which can hide a live release — the safe direction, since the cost is a missing
suggestion rather than re-downloading something already owned.

`/artists.json` answers `503` rather than an empty `200` while Navidrome has
never been reached: an empty list means "nothing is starred", which is a very
different statement from "the source is down". `/status` drives the container
`HEALTHCHECK`, so a service whose syncs keep failing reports `unhealthy`
instead of looking fine forever.

## Configuration

Copy `.env.example` to `.env` and fill in the Navidrome credentials and the
Lidarr API key. Unstarring an artist removes them from the feed on the next
sync; Lidarr keeps artists it already added unless the list is set to remove.

## Behind a reverse proxy

Serving Navidrome under a hostname breaks two things at once: the userscript's
`@match` no longer fires, and — once the proxy terminates TLS — the page can no
longer call the bridge on plain `http://host:8687`, because a browser blocks
that as mixed content.

Mount the bridge on the same origin instead. Caddy:

```caddyfile
navidrome.example {
    handle_path /ndlb/* {
        reverse_proxy localhost:8687
    }

    reverse_proxy localhost:4533
}
```

`handle_path` strips the prefix, so `/ndlb/missing` arrives as `/missing`. The
equivalent nginx block is in `nginx/navidrome-panel.conf`.

The userscript picks the right target on its own: it uses the bridge's port only
when the page is Navidrome's own `:4533`, and the same-origin `/ndlb` prefix
everywhere else. Add the proxy host to its `@match` list.
