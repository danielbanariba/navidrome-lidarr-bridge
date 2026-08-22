// ==UserScript==
// @name         Navidrome - Álbumes Faltantes
// @namespace    navidrome-missing-albums
// @version      6.4
// @description  Albums faltantes en gris + boton para solicitarlos a Lidarr + sidebar "Not Favourites"
// @match        *://localhost:4533/*
// @match        *://localhost:4534/*
// @match        *://navidrome.localhost/*
// @match        *://*.localhost/app/*
// @include      http://localhost:4533/*
// @include      http://localhost:4533/app*
// @run-at       document-idle
// @grant        none
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  const MB_BASE = "https://musicbrainz.org/ws/2";
  const CAA_BASE = "https://coverartarchive.org";
  // Puente hacia Lidarr. Recibe el mismo release-group MBID que ya usa este
  // script, porque Lidarr guarda ese id como foreignAlbumId.
  //
  // Contra Navidrome directo en :4533 hay que ir a su puerto. Detras de un proxy
  // el puente se monta en el mismo origen bajo /ndlb, que ademas es lo unico que
  // funciona si la pagina va por HTTPS: un fetch a http://host:8687 desde una
  // pagina https es mixed content y el navegador lo bloquea.
  const BRIDGE =
    window.__NDLB_BASE ||
    (location.port === "4533"
      ? `${location.protocol}//${location.hostname}:8687`
      : "/ndlb");
  const CACHE_TTL = 7 * 24 * 60 * 60 * 1000;
  const MARKER = "data-missing-album";

  const EXCLUDED_SECONDARY = new Set([
    "Compilation", "Live", "Remix", "Soundtrack", "DJ-mix",
    "Mixtape/Street", "Demo", "Interview", "Audiobook",
    "Audio drama", "Spokenword",
  ]);

  // ── Cache ────────────────────────────────────────────────
  let cache = { artists: {}, albums: {}, ts: {} };

  function loadCache() {
    try {
      const raw = localStorage.getItem("nd-missing-albums-cache");
      if (raw) cache = JSON.parse(raw);
    } catch (_) {}
  }

  function saveCache() {
    try {
      localStorage.setItem("nd-missing-albums-cache", JSON.stringify(cache));
    } catch (_) {}
  }

  function isFresh(key) {
    return cache.ts[key] && Date.now() - cache.ts[key] < CACHE_TTL;
  }

  // ── Navidrome API ────────────────────────────────────────
  function ndFetch(path) {
    const token = localStorage.getItem("token");
    return fetch(path, {
      headers: {
        Accept: "application/json",
        "X-ND-Authorization": `Bearer ${token}`,
        "X-ND-Client-Unique-Id": "missing-albums-userscript",
      },
    }).then((r) => r.json());
  }

  function getArtistInfo(id) {
    return ndFetch(`/api/artist/${id}`);
  }

  function getArtistAlbums(id) {
    const filter = encodeURIComponent(JSON.stringify({ artist_id: id }));
    return ndFetch(
      `/api/album?filter=${filter}&sort=["max_year","ASC"]&range=[0,499]`
    );
  }

  // ── MusicBrainz API ─────────────────────────────────────
  let lastMB = 0;

  async function mbFetch(url) {
    const wait = Math.max(0, 1100 - (Date.now() - lastMB));
    if (wait) await new Promise((r) => setTimeout(r, wait));
    lastMB = Date.now();
    const r = await fetch(url, {
      headers: { Accept: "application/json" },
    });
    if (!r.ok) throw new Error(`MB ${r.status}`);
    return r.json();
  }

  async function findArtistMBID(name, mbzId) {
    if (mbzId) return mbzId;

    const key = name.toLowerCase();
    if (cache.artists[key] !== undefined && isFresh("a:" + key))
      return cache.artists[key];

    const data = await mbFetch(
      `${MB_BASE}/artist/?query=artist:"${encodeURIComponent(name)}"&limit=5&fmt=json`
    );

    let mbid = null;
    for (const a of data.artists || []) {
      if (a.name.toLowerCase() === key) {
        mbid = a.id;
        break;
      }
    }
    if (!mbid && data.artists?.[0]?.score >= 90) {
      mbid = data.artists[0].id;
    }

    cache.artists[key] = mbid;
    cache.ts["a:" + key] = Date.now();
    saveCache();
    return mbid;
  }

  async function getStudioAlbums(mbid) {
    if (cache.albums[mbid] && isFresh("rg:" + mbid)) return cache.albums[mbid];

    const albums = [];
    let offset = 0;

    while (true) {
      const data = await mbFetch(
        `${MB_BASE}/release-group?artist=${mbid}&type=album&limit=100&offset=${offset}&fmt=json`
      );
      const groups = data["release-groups"] || [];
      if (!groups.length) break;

      for (const rg of groups) {
        if (rg["primary-type"] !== "Album") continue;
        const secondary = rg["secondary-types"] || [];
        if (secondary.some((s) => EXCLUDED_SECONDARY.has(s))) continue;

        albums.push({
          title: rg.title,
          year: (rg["first-release-date"] || "????").slice(0, 4),
          mbid: rg.id,
        });
      }

      offset += 100;
      if (offset >= (data["release-group-count"] || 0)) break;
    }

    albums.sort((a, b) => a.year.localeCompare(b.year));
    cache.albums[mbid] = albums;
    cache.ts["rg:" + mbid] = Date.now();
    saveCache();
    return albums;
  }

  // ── Comparacion ──────────────────────────────────────────
  function normalize(name) {
    return name
      .toLowerCase()
      .replace(/\s*\(.*?\)\s*/g, " ")
      .replace(/\s*\[.*?\]\s*/g, " ")
      // Por espacio y no por vacio: borrando el guion, "Revenge-10th" quedaba
      // "revenge10th" y dejaba de empezar por "revenge", que es justo lo que
      // mira el match por prefijo de abajo.
      .replace(/[^\w\s]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function findMissing(localAlbums, mbAlbums) {
    const local = localAlbums.map((a) => normalize(a.name));

    // Un disco propio suele traer un sufijo de edicion que el catalogo no tiene
    // — "...Revenge-10th Anniversary Edition" contra "...Revenge" — y no siempre
    // viene entre parentesis, asi que hace falta el prefijo. Solo en esa
    // direccion: un titulo propio corto no debe satisfacer una entrada distinta
    // que lo extiende.
    const owned = (title) => {
      const key = normalize(title);
      return local.some((have) => have === key || have.startsWith(key + " "));
    };

    return mbAlbums.filter((a) => !owned(a.title));
  }

  // ── DOM ──────────────────────────────────────────────────
  // MUI v4 en produccion genera clases como MuiGridList-root-123
  // Usamos [class*="..."] para matchear sin importar el sufijo

  const PLACEHOLDER_SVG =
    'data:image/svg+xml,' +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 300">' +
        '<rect width="300" height="300" fill="#282828"/>' +
        '<text x="150" y="140" text-anchor="middle" font-family="sans-serif" font-size="60" fill="#555">&#9834;</text>' +
        '<text x="150" y="190" text-anchor="middle" font-family="sans-serif" font-size="16" fill="#444">Sin portada</text>' +
        "</svg>"
    );

  function findGrid() {
    return document.querySelector('[class*="MuiGridList-root"]');
  }

  function findTiles(grid) {
    return grid.querySelectorAll(`:scope > [class*="MuiGridListTile-root"]:not([${MARKER}])`);
  }

  function getTileYear(tile) {
    const tileDiv = tile.querySelector('[class*="MuiGridListTile-tile"]');
    if (!tileDiv) return "0000";
    const container = tileDiv.querySelector(":scope > div");
    if (!container) return "0000";
    const spans = container.querySelectorAll(":scope > span");
    const lastSpan = spans[spans.length - 1];
    return lastSpan?.textContent?.match(/\d{4}/)?.[0] || "0000";
  }

  function createMissingTile(album, templateTile) {
    const tile = templateTile.cloneNode(true);
    tile.setAttribute(MARKER, "true");
    tile.dataset.mbid = album.mbid;

    const tileDiv = tile.querySelector('[class*="MuiGridListTile-tile"]');
    const container = tileDiv?.querySelector(":scope > div");

    if (container) {
      // 0.4 dejaba el titulo casi ilegible sobre el fondo oscuro. Sube apenas
      // lo justo para poder leerlo, y bastante mas al pasar el mouse.
      container.style.filter = "grayscale(100%)";
      container.style.opacity = "0.5";
      container.style.transition = "opacity .25s ease, filter .25s ease";

      tile.addEventListener("mouseenter", () => {
        container.style.opacity = "0.9";
        container.style.filter = "grayscale(35%)";
      });
      tile.addEventListener("mouseleave", () => {
        container.style.opacity = "0.5";
        container.style.filter = "grayscale(100%)";
      });
    }

    // Reemplazar portada
    const img = tile.querySelector("img");
    if (img) {
      img.src = `${CAA_BASE}/release-group/${album.mbid}/front-250`;
      img.alt = album.title;
      img.onerror = function () {
        this.onerror = null;
        this.src = PLACEHOLDER_SVG;
      };
    }

    // Reemplazar nombre (primer <p> del segundo <a> con href de album)
    const links = tile.querySelectorAll('a[href*="/album/"]');
    const nameLink = links[1];
    if (nameLink) {
      const nameP = nameLink.querySelector("p");
      if (nameP) nameP.textContent = album.title;
      const allP = nameLink.querySelectorAll("p");
      if (allP.length > 1) allP[1].remove();
    }

    // Reemplazar subtitulo (año)
    if (container) {
      const spans = container.querySelectorAll(":scope > span");
      const lastSpan = spans[spans.length - 1];
      if (lastSpan) lastSpan.textContent = album.year !== "????" ? album.year : "";
    }

    // Desactivar links
    tile.querySelectorAll("a").forEach((a) => {
      a.removeAttribute("href");
      a.style.cursor = "default";
      a.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
      });
    });

    // Quitar overlay de play
    const tileBar = tile.querySelector('[class*="MuiGridListTileBar-root"]');
    if (tileBar) tileBar.remove();

    // Badge "No descargado", arriba a la izquierda: abajo chocaba con el boton.
    const imgWrapper = img?.closest("div");
    if (imgWrapper) {
      imgWrapper.style.position = "relative";
      const badge = document.createElement("div");
      badge.style.cssText =
        "position:absolute;top:6px;left:6px;background:rgba(0,0,0,0.72);" +
        "color:#b9bec7;font-size:9px;padding:2px 7px;border-radius:10px;" +
        "pointer-events:none;letter-spacing:0.6px;text-transform:uppercase;";
      badge.textContent = "No descargado";
      imgWrapper.appendChild(badge);
    }

    // La capa de accion va colgada de tileDiv y no del container, porque ese
    // lleva grayscale + opacity y los heredaria: la portada se ve apagada a
    // proposito, el boton no. Se dimensiona con aspect-ratio 1 para calzar
    // sobre la tapa cuadrada y no invadir la fila del titulo.
    if (tileDiv) {
      tileDiv.style.position = "relative";
      const overlay = createRequestOverlay(album);
      tileDiv.appendChild(overlay);
      tile.addEventListener("mouseenter", () => (overlay.style.opacity = "1"));
      tile.addEventListener("mouseleave", () => {
        if (!overlay.dataset.pinned) overlay.style.opacity = "0";
      });
    }

    return tile;
  }

  function createRequestOverlay(album) {
    const overlay = document.createElement("div");
    overlay.style.cssText =
      "position:absolute;top:0;left:0;width:100%;aspect-ratio:1;z-index:2;" +
      "display:flex;align-items:center;justify-content:center;" +
      "background:linear-gradient(180deg,rgba(0,0,0,0) 35%,rgba(0,0,0,0.55) 100%);" +
      "opacity:0;transition:opacity .18s ease;pointer-events:none;";

    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = "Solicitar";
    btn.style.cssText =
      "pointer-events:auto;cursor:pointer;font:600 11px/1 system-ui,sans-serif;" +
      "letter-spacing:.4px;padding:7px 16px;border-radius:999px;" +
      "border:1px solid rgba(255,255,255,.25);color:#fff;" +
      "background:rgba(28,32,38,.92);backdrop-filter:blur(2px);" +
      "box-shadow:0 2px 10px rgba(0,0,0,.45);transition:background .15s;";
    btn.addEventListener("mouseenter", () => {
      if (!btn.disabled) btn.style.background = "rgba(48,54,64,.95)";
    });
    btn.addEventListener("mouseleave", () => {
      if (!btn.disabled) btn.style.background = "rgba(28,32,38,.92)";
    });

    // Un resultado tiene que quedar a la vista aunque el mouse se vaya.
    const settle = (text, ok) => {
      btn.textContent = text;
      btn.disabled = true;
      btn.style.cursor = "default";
      btn.style.borderColor = "transparent";
      btn.style.background = ok ? "rgba(46,125,79,.95)" : "rgba(142,59,52,.95)";
      overlay.dataset.pinned = "1";
      overlay.style.opacity = "1";
    };

    btn.addEventListener("click", async (e) => {
      // El tile entero tiene los clicks anulados; este boton es la excepcion.
      e.preventDefault();
      e.stopPropagation();
      btn.disabled = true;
      btn.textContent = "Pidiendo…";
      overlay.dataset.pinned = "1";
      try {
        const res = await fetch(`${BRIDGE}/request`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mbid: album.mbid }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.ok) {
          settle("Pedido ✓", true);
        } else {
          // 404 = Lidarr no tiene al artista todavia; hay que marcarlo con ★.
          settle(res.status === 404 ? "Marcá ★ primero" : "Error", false);
          btn.title = data.error || `HTTP ${res.status}`;
        }
      } catch (err) {
        settle("Error", false);
        btn.title = String(err && err.message ? err.message : err);
      }
    });

    overlay.appendChild(btn);
    return overlay;
  }

  function insertByYear(grid, tile, album) {
    const existing = findTiles(grid);
    for (const el of existing) {
      const year = getTileYear(el);
      if (album.year < year) {
        grid.insertBefore(tile, el);
        return;
      }
    }
    grid.appendChild(tile);
  }

  // ── Indicador de carga ───────────────────────────────────
  function showLoading(grid) {
    removeMessages();
    const el = document.createElement("div");
    el.id = "missing-albums-loading";
    el.style.cssText =
      "padding:8px 16px;color:#888;font-size:13px;font-style:italic;";
    el.textContent = "Buscando \u00e1lbumes faltantes en MusicBrainz...";
    grid.parentNode.insertBefore(el, grid);
  }

  function removeMessages() {
    document.getElementById("missing-albums-loading")?.remove();
    document.getElementById("missing-albums-counter")?.remove();
    document.getElementById("missing-albums-complete")?.remove();
  }

  // ── Logica principal ─────────────────────────────────────
  let currentArtistId = null;
  let working = false;

  async function processArtistPage() {
    const match = window.location.hash.match(/^#\/artist\/([^/]+)\/show/);
    if (!match) return;

    const artistId = match[1];
    if (working) return;

    const grid = findGrid();
    if (!grid) {
      console.log("[Missing Albums] Grid no encontrado todavia");
      return;
    }

    const existingTiles = findTiles(grid);
    if (!existingTiles.length) {
      console.log("[Missing Albums] No hay tiles en el grid todavia");
      return;
    }

    if (
      currentArtistId === artistId &&
      grid.querySelector(`[${MARKER}]`)
    )
      return;

    working = true;
    currentArtistId = artistId;

    grid
      .querySelectorAll(`[${MARKER}]`)
      .forEach((el) => el.remove());
    removeMessages();

    try {
      showLoading(grid);

      const [artistInfo, localAlbums] = await Promise.all([
        getArtistInfo(artistId),
        getArtistAlbums(artistId),
      ]);

      console.log("[Missing Albums] Artista:", artistInfo?.name, "| Albums locales:", localAlbums?.length);

      if (!artistInfo?.name) {
        removeMessages();
        working = false;
        return;
      }

      const mbid = await findArtistMBID(
        artistInfo.name,
        artistInfo.mbzArtistId
      );
      console.log("[Missing Albums] MBID:", mbid);

      if (!mbid) {
        removeMessages();
        working = false;
        return;
      }

      const mbAlbums = await getStudioAlbums(mbid);
      const missing = findMissing(localAlbums, mbAlbums);

      console.log("[Missing Albums] MusicBrainz:", mbAlbums.length, "| Faltantes:", missing.length);

      removeMessages();

      if (!missing.length) {
        const msg = document.createElement("div");
        msg.style.cssText = "padding:8px 16px;color:#555;font-size:12px;";
        msg.textContent = "Tienes todos los \u00e1lbumes de estudio.";
        msg.id = "missing-albums-complete";
        grid.parentNode.insertBefore(msg, grid);
        setTimeout(() => msg.remove(), 4000);
        working = false;
        return;
      }

      const template = existingTiles[0];

      for (const album of missing) {
        const tile = createMissingTile(album, template);
        insertByYear(grid, tile, album);
      }

      const counter = document.createElement("div");
      counter.id = "missing-albums-counter";
      counter.style.cssText = "padding:4px 16px 12px;color:#666;font-size:12px;";
      counter.textContent = `${missing.length} \u00e1lbum(es) de estudio no descargado(s)`;
      grid.parentNode.insertBefore(counter, grid);
    } catch (e) {
      console.error("[Missing Albums] Error:", e);
      removeMessages();
    }

    working = false;
  }

  // ── Sidebar "Not Favourites" ─────────────────────────────
  const NOT_FAV_ID = "nd-not-favourites-sidebar";

  // Heart with diagonal line (HeartBroken / Not Favourite)
  const HEART_BROKEN_SVG = '<svg class="MuiSvgIcon-root" focusable="false" viewBox="0 0 24 24" aria-hidden="true" data-testid="icon"><path d="M16.5 3c-1.74 0-3.41.81-4.5 2.09C10.91 3.81 9.24 3 7.5 3 4.42 3 2 5.42 2 8.5c0 3.78 3.4 6.86 8.55 11.54L12 21.35l1.45-1.32C18.6 15.36 22 12.28 22 8.5 22 5.42 19.58 3 16.5 3zm-4.4 15.55l-.1.1-.1-.1C7.14 14.24 4 11.39 4 8.5 4 6.5 5.5 5 7.5 5c1.54 0 3.04.99 3.57 2.36h1.87C13.46 5.99 14.96 5 16.5 5c2 0 3.5 1.5 3.5 3.5 0 2.89-3.14 5.74-7.9 10.05z"></path><line x1="3" y1="21" x2="21" y2="3" stroke="currentColor" stroke-width="2"></line></svg>';

  function injectNotFavSidebar() {
    // Find the "Favourites" sidebar link
    const favLink = document.querySelector('a[href="#/album/starred"]');
    if (!favLink) return;
    if (document.getElementById(NOT_FAV_ID)) return;

    // Clone the Favourites item as template
    const newItem = favLink.cloneNode(true);
    newItem.id = NOT_FAV_ID;
    newItem.setAttribute("href", "#/album/notFavourites");
    newItem.setAttribute("title", "Not Favourites");

    // Replace icon with broken heart
    const iconDiv = newItem.querySelector('[class*="MuiListItemIcon-root"]');
    if (iconDiv) iconDiv.innerHTML = HEART_BROKEN_SVG;

    // Replace text: remove all text nodes and set new one
    const ripple = newItem.querySelector('[class*="MuiTouchRipple-root"]');
    Array.from(newItem.childNodes).forEach((n) => {
      if (n.nodeType === Node.TEXT_NODE) n.remove();
    });
    if (ripple) {
      newItem.insertBefore(document.createTextNode("Not Favourites"), ripple);
    } else {
      newItem.appendChild(document.createTextNode("Not Favourites"));
    }

    // Handle click — navigate to starred=false filter view
    newItem.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      window.location.hash = '/album?displayedFilters=%5B%22starred%22%5D&filter=%7B%22starred%22%3Afalse%7D&order=ASC&page=1&perPage=15&sort=name';
    });

    // Insert right after "Favourites"
    favLink.parentNode.insertBefore(newItem, favLink.nextSibling);
  }

  function updateNotFavActive() {
    const el = document.getElementById(NOT_FAV_ID);
    if (!el) return;
    const hash = window.location.hash;
    if (hash.includes("starred") && hash.includes("false")) {
      el.classList.add("nd-active");
    } else {
      el.classList.remove("nd-active");
    }
  }

  // ── Observadores ─────────────────────────────────────────
  function init() {
    loadCache();
    console.log("[Navidrome Mods] v6.0 iniciado");

    const style = document.createElement("style");
    style.textContent = `
      [${MARKER}] { transition: transform 0.2s; }
      [${MARKER}]:hover { transform: scale(1.02); }
      #${NOT_FAV_ID}.nd-active { font-weight: bold; }
      #${NOT_FAV_ID}.nd-active .MuiListItemIcon-root { color: inherit; }
    `;
    document.head.appendChild(style);

    window.addEventListener("hashchange", () => {
      currentArtistId = null;
      removeMessages();
      scheduleCheck();
      updateNotFavActive();
    });

    const observer = new MutationObserver(() => {
      if (window.location.hash.match(/^#\/artist\/[^/]+\/show/)) {
        scheduleCheck();
      }
      // Inyectar "Not Favourites" en el sidebar
      injectNotFavSidebar();
    });
    observer.observe(document.body, { childList: true, subtree: true });

    scheduleCheck();
    injectNotFavSidebar();
    updateNotFavActive();
  }

  let checkTimer = null;
  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(processArtistPage, 800);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
