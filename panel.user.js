// ==UserScript==
// @name         Navidrome — missing albums
// @namespace    navidrome-lidarr-bridge
// @version      1.0.0
// @description  Show which albums an artist is missing, and request them without leaving Navidrome.
// @match        *://*/app/*
// @grant        none
// ==/UserScript==

/*
 * Navidrome has no plugin hook for the web UI, so the panel is injected.
 *
 * It renders as a fixed-position card rather than a node inside the artist
 * page: Navidrome is a React app and would drop an injected child on its next
 * render. Nothing here touches Navidrome's own DOM.
 *
 * "Missing" is computed by the bridge, not by Lidarr. Lidarr only sees its own
 * root folder, so it reports albums the wider library already holds.
 */
(function () {
  'use strict';

  // Injected by a proxy, the bridge is reachable under a same-origin prefix it
  // sets here. Installed as a userscript instead, fall back to the port the
  // bridge publishes beside Navidrome.
  const BRIDGE = window.__NDLB_BASE || `${location.protocol}//${location.hostname}:8687`;
  const ARTIST_ROUTE = /#\/artist\/([^/]+)\/show/;

  let currentArtist = null;
  let collapsed = false;

  const css = `
    #ndlb {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483000;
      width: 340px; max-height: 60vh; display: flex; flex-direction: column;
      background: #23272f; color: #e8eaed; border: 1px solid #3a3f4b;
      border-radius: 10px; box-shadow: 0 8px 28px rgba(0,0,0,.5);
      font: 13px/1.45 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    }
    #ndlb header {
      display: flex; align-items: center; gap: 8px; padding: 10px 12px;
      border-bottom: 1px solid #3a3f4b; cursor: pointer; user-select: none;
    }
    #ndlb header b { flex: 1; font-weight: 600; font-size: 13px; }
    #ndlb .count {
      background: #c0392b; color: #fff; border-radius: 10px;
      padding: 1px 8px; font-size: 11px; font-weight: 700;
    }
    #ndlb .count.zero { background: #2e7d4f; }
    #ndlb ul { list-style: none; margin: 0; padding: 4px 0; overflow-y: auto; }
    #ndlb li {
      display: flex; align-items: center; gap: 8px; padding: 7px 12px;
    }
    #ndlb li + li { border-top: 1px solid #2c313a; }
    #ndlb .t { flex: 1; min-width: 0; }
    #ndlb .t span { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    #ndlb .y { color: #9aa0aa; font-size: 11px; }
    #ndlb button {
      border: 1px solid #4a5160; background: #2f3540; color: #e8eaed;
      border-radius: 6px; padding: 4px 10px; font-size: 12px; cursor: pointer;
    }
    #ndlb button:hover:not(:disabled) { background: #3a424f; }
    #ndlb button:disabled { opacity: .55; cursor: default; }
    #ndlb .msg { padding: 12px; color: #9aa0aa; }
    #ndlb.collapsed ul, #ndlb.collapsed .msg { display: none; }
  `;

  function root() {
    let el = document.getElementById('ndlb');
    if (el) return el;
    const style = document.createElement('style');
    style.textContent = css;
    document.head.appendChild(style);
    el = document.createElement('div');
    el.id = 'ndlb';
    el.innerHTML = '<header><b></b><span class="count"></span></header><div class="body"></div>';
    el.querySelector('header').addEventListener('click', () => {
      collapsed = !collapsed;
      el.classList.toggle('collapsed', collapsed);
    });
    document.body.appendChild(el);
    return el;
  }

  function render(data) {
    const el = root();
    el.style.display = '';
    el.classList.toggle('collapsed', collapsed);
    el.querySelector('b').textContent = data.artist || 'Artist';

    const body = el.querySelector('.body');
    const badge = el.querySelector('.count');

    if (data.error || data.hint) {
      badge.textContent = '–';
      badge.className = 'count zero';
      body.innerHTML = `<div class="msg"></div>`;
      body.querySelector('.msg').textContent = data.error || data.hint;
      return;
    }

    const missing = data.missing || [];
    badge.textContent = missing.length;
    badge.className = missing.length ? 'count' : 'count zero';

    if (!missing.length) {
      body.innerHTML = `<div class="msg">Tenés la discografía completa (${data.owned} álbumes).</div>`;
      return;
    }

    const ul = document.createElement('ul');
    for (const album of missing) {
      const li = document.createElement('li');
      const t = document.createElement('div');
      t.className = 't';
      const title = document.createElement('span');
      title.textContent = album.title;
      title.title = album.title;
      const year = document.createElement('span');
      year.className = 'y';
      year.textContent = [album.year, album.type].filter(Boolean).join(' · ');
      t.append(title, year);

      const btn = document.createElement('button');
      btn.textContent = 'Buscar';
      btn.addEventListener('click', async () => {
        btn.disabled = true;
        btn.textContent = '…';
        try {
          const res = await fetch(`${BRIDGE}/request`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ albumId: album.id }),
          });
          btn.textContent = res.ok ? 'Pedido' : 'Error';
        } catch {
          btn.textContent = 'Error';
        }
      });

      li.append(t, btn);
      ul.appendChild(li);
    }
    body.replaceChildren(ul);
  }

  function hide() {
    const el = document.getElementById('ndlb');
    if (el) el.style.display = 'none';
  }

  async function refresh() {
    const match = location.hash.match(ARTIST_ROUTE);
    if (!match) {
      currentArtist = null;
      hide();
      return;
    }
    const id = match[1];
    if (id === currentArtist) return;
    currentArtist = id;

    render({ artist: '…', hint: 'consultando' });
    try {
      const res = await fetch(`${BRIDGE}/missing?id=${encodeURIComponent(id)}`);
      const data = await res.json();
      // A slower request for a previous artist must not overwrite the current one.
      if (currentArtist === id) render(data);
    } catch (err) {
      if (currentArtist === id) render({ error: `no pude hablar con el puente: ${err.message}` });
    }
  }

  window.addEventListener('hashchange', refresh);
  refresh();
})();
