// The microMAPS page: a block on the map, its state, the request, the route. Text that shows up
// after load is in T below, everything else is in index.html. Grid: blocks.js. GPX: route/route.js.
"use strict";

(function () {
  const T = {
    word: {
      ready: "Ready", notbuilt: "Not built", building: "Building", empty: "No map data", dense: "Too dense",
      offgrid: "Outside the grid", checking: "Checking", unknown: "Not known",
    },
    tag: {
      ready: "READY", notbuilt: "NOT BUILT", building: "BUILDING", empty: "NO MAP DATA", dense: "TOO DENSE",
      offgrid: "OFF GRID", checking: "CHECKING", unknown: "NOT KNOWN",
    },
    note: {
      ready: "Snapshot {date}. Copy the link, then follow the steps below.",
      notbuilt: "A request publishes this block in the open data for good, with no name attached.",
      lowzoom: "Zoom in to place the block before you request it.",
      building: "Requested at {time}. It usually takes up to about an hour. You can close this page.",
      slow: "Requested at {time}. This is taking longer than usual: each run builds a limited number of blocks.",
      expired: "The request from {time} brought no block. You can send it again.",
      empty: "There is no map data here, so there is nothing to build.",
      dense20: "Too much map for the watch at 20 km. Try 10 km.",
      dense10: "Too much map for the watch, even at 10 km. Try a block next to it.",
      offgrid: "Blocks stop at 80 degrees north and south, and at the 180 line.",
      checking: "Checking which blocks exist.",
      unknown: "Could not load the block list, so the state of this block is not known.",
    },
    act: {
      copy: "Copy map link", copied: "Link copied. Steps below", request: "Request this block", sending: "Sending the request",
      check: "Check now", checking: "Checking", zoom: "Zoom in to this block", to10: "Switch to 10 km", to20: "Try 20 km",
      other: "Search another place", retry: "Try again", list: "Checking the list",
    },
    requestFailed: "The request did not go through. Send the block id by mail instead.",
    requestMail: "Mail the block id",
    requestReady: "Your block {id} is ready.",
    requestRefused: "Your block {id} could not be built: {why}.",
    show: "Show it",
    why: { empty: "no map data", dense: "too dense" },
    requestWaiting: "Building", requestOld: "No result", requestDone: "Ready",
    lastCheck: "Last checked at {time}.",
    badLink: "This link does not point to a block on the grid.",
    copyFail: "Hold the text to copy",
    copied: "Copied",
    search: "Search", searching: "Searching",
    noPlace: "Nothing found for \"{q}\". Try another name, or type lat, lon.",
    noSearch: "Search is not available right now. Type coordinates instead, like 67.62, 33.72.",
    badCoords: "Those coordinates are outside the map. Latitude goes up to 85, longitude up to 180.",
    noLocate: "This browser cannot give a position. Search for a place instead.",
    locateDenied: "Location is off for this page. Allow it in the browser settings, or search for a place.",
    locateFailed: "The position could not be found. Search for a place instead.",
    tiles: "Map pictures are not loading. Search, coordinates and block ids still work.",
    badFile: "Could not read this file.",
    catalogFailed: "Could not load the list. The watch shows it under Maps.",
    routeFits: "Fits the {size} km block now on the map.",
    routeOutside: "Too wide for a 20 km block: {out} km of {all} km ({pct}%) is outside the block on the map. The route still works on the watch, that part just has no map under it.",
    showDetails: "Show details", hideDetails: "Hide details",
  };

  const INDEX_URL = "blocks/index.json";
  const CATALOG_URL = "catalog.json";
  const NTFY = "https://ntfy.sh/micromaps-blocks-v1";
  const NOMINATIM = "https://nominatim.openstreetmap.org/search";
  const TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
  const MAIL = "trdimads@gmail.com";
  const HOME = [67.62, 33.72];

  const RECHECK_MS = 120000;
  const RETURN_GAP_MS = 20000;
  const SLOW_MS = 90 * 60000;
  const REQUEST_LIFE_MS = 13 * 3600000;   // ntfy keeps a message 12 h
  const SEARCH_GAP_MS = 1100;             // Nominatim: at most one request a second
  const FETCH_TIMEOUT_MS = 15000;
  const PICK_ZOOM = 9;                    // below this a block is too small to aim
  const OUTLINE_ZOOM = 9;
  const VEIL_ZOOM = 7;
  const MAX_OUTLINES = 300;
  const TAG_MIN_PX = 150;                 // the state alone needs this much width of frame
  const TAG_FULL_PX = 250;                // the state and the size
  const DASH = { notbuilt: [14, 8], building: [0, 11], empty: [2, 9], offgrid: [2, 9], dense: [3, 3], checking: [10, 10], unknown: [10, 10] };
  const TRACK_DRAW_POINTS = 4000;
  const TRACK_FIT_POINTS = 2000;
  const PIN_SLACK_PX = 4;

  // docs/DESIGN.md: the micro font and the route glyphs of the watch, width:rows in hex
  const MICRO = {"0": "6:1e33373f3b331e", "1": "6:0c1c0c0c0c0c1e", "2": "6:1e3303060c183f", "3": "6:3e03031e03033e", "4": "6:060e1e363f0606", "5": "6:3f303e0303331e", "6": "6:0e18303e33331e", "7": "6:3f03060c181818", "8": "6:1e33331e33331e", "9": "6:1e33331f03061c", "A": "6:1e33333f333333", "B": "6:3e33333e33333e", "C": "6:1e33303030331e", "D": "6:3e33333333333e", "E": "6:3f30303e30303f", "F": "6:3f30303e303030", "G": "6:1e33303f33331f", "H": "6:3333333f333333", "I": "4:0f06060606060f", "J": "6:0f06060606361c", "K": "6:33363c383c3633", "L": "6:3030303030303f", "M": "8:c3e7ffdbc3c3c3", "N": "6:33333b3f373333", "O": "6:1e33333333331e", "P": "6:3e33333e303030", "Q": "6:1e3333333f361f", "R": "6:3e33333e3c3633", "S": "6:1f30301e03033e", "T": "6:3f0c0c0c0c0c0c", "U": "6:3333333333331e", "V": "6:33333333331e0c", "W": "8:c3c3c3dbdbff66", "X": "6:33331e0c1e3333", "Y": "6:33331e0c0c0c0c", "Z": "6:3f03060c18303f"};
  const GLYPH = {
    start: { anchor: [4, 4], rows: [".ooooooo.", "oo#####oo", "o##ooo##o", "o#ooooo#o", "o#ooooo#o", "o#ooooo#o", "o##ooo##o", "oo#####oo", ".ooooooo."] },
    way: { anchor: [4, 4], rows: ["....o....", "...o#o...", "..o###o..", ".o##o##o.", "o##ooo##o", ".o##o##o.", "..o###o..", "...o#o...", "....o...."] },
    finish: { anchor: [1, 12], rows: ["ooooooooooo", "o#########o", "o##oo##oo#o", "o##oo##oo#o", "o#oo##oo##o", "o#oo##oo##o", "o#########o", "o#ooooooooo", "o#o........", "o#o........", "o#o........", "o#o........", "o#o........", "ooo........"] },
  };
  // the texture a failed tile leaves behind: the sparse dots of a contour, so a drag still shows
  const NO_TILE = "data:image/svg+xml," + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256"><defs><pattern id="d" width="16" height="16" patternUnits="userSpaceOnUse"><rect width="1" height="1"/></pattern></defs><rect width="256" height="256" fill="#fff"/><rect width="256" height="256" fill="url(#d)"/></svg>');

  const $ = id => document.getElementById(id);
  const fill = (text, values) => text.replace(/\{(\w+)\}/g, (m, k) => values[k]);
  const clock = ms => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  const stamp = ms => new Date(ms).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const later = ms => new Promise(done => setTimeout(done, ms));
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const wideQuery = matchMedia("(min-width: 720px), (min-width: 540px) and (max-height: 500px)");

  const store = {
    get(key, fallback) {
      try {
        const v = localStorage.getItem(key);
        return v === null ? fallback : JSON.parse(v);
      } catch (e) {
        return fallback;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
      } catch (e) {
        // private mode: the page works, the request is just not remembered
      }
    },
  };

  function timedFetch(url, options) {
    const stop = new AbortController();
    const timer = setTimeout(() => stop.abort(), FETCH_TIMEOUT_MS);
    return fetch(url, Object.assign({ signal: stop.signal }, options)).finally(() => clearTimeout(timer));
  }

  function microSvg(text, scale) {
    let d = "";
    let x = 0;
    for (const ch of text) {
      const g = MICRO[ch];
      if (!g) {
        x += 4;
        continue;
      }
      const [w, hex] = g.split(":");
      for (let y = 0; y < 7; y++) {
        const bits = parseInt(hex.substr(y * 2, 2), 16);
        for (let i = 0; i < +w; i++) {
          if (bits & (1 << (+w - 1 - i))) {
            d += "M" + (x + i) + " " + y + "h1v1h-1z";
          }
        }
      }
      x += +w + 1;
    }
    x -= 1;
    return '<svg viewBox="0 0 ' + x + ' 7" width="' + x * scale + '" height="' + 7 * scale + '" aria-hidden="true"><path d="' + d + '"/></svg>';
  }

  function glyphIcon(g, scale) {
    const w = g.rows[0].length;
    const h = g.rows.length;
    const runs = ch => g.rows.map((row, y) => row.split("").map((c, x) => (c === ch ? "M" + x + " " + y + "h1v1h-1z" : "")).join("")).join("");
    return L.divIcon({
      className: "glyph",
      iconSize: [w * scale, h * scale],
      iconAnchor: [g.anchor[0] * scale, g.anchor[1] * scale],
      html: '<svg viewBox="0 0 ' + w + " " + h + '" width="' + w * scale + '" height="' + h * scale + '"><path fill="#fff" d="' + runs("o") + '"/><path d="' + runs("#") + '"/></svg>',
    });
  }

  /* ---------- the map ---------- */

  const map = L.map("map", {
    zoomControl: false,
    attributionControl: false,
    minZoom: 2,
    maxZoom: 18,
    maxBounds: [[-85.05, -180], [85.05, 180]],
    maxBoundsViscosity: 1,
    zoomAnimation: !reduced,
    markerZoomAnimation: !reduced,
    fadeAnimation: false,
    inertia: !reduced,
  });

  const tiles = L.tileLayer(TILES, { maxZoom: 19, noWrap: true, errorTileUrl: NO_TILE, keepBuffer: 1 }).addTo(map);
  L.control.scale({ position: "bottomleft", imperial: false, maxWidth: 120 }).addTo(map);
  const Caption = L.Control.extend({
    onAdd() {
      const el = L.DomUtil.create("div", "cap");
      el.id = "cap";
      return el;
    },
  });
  new Caption({ position: "bottomleft" }).addTo(map);
  L.control.attribution({ position: "bottomleft", prefix: false })
    .addAttribution('&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors')
    .addTo(map);

  for (const [name, z] of [["veil", 410], ["outlines", 420], ["track", 430], ["frame", 440]]) {
    map.createPane(name).style.zIndex = z;
  }
  const pane = name => ({ pane: name, renderer: L.svg({ pane: name, padding: 0.6 }), interactive: false });

  const WORLD = [[-85.05, -180], [85.05, -180], [85.05, 180], [-85.05, 180]];
  const veil = L.polygon([WORLD], Object.assign(pane("veil"), { stroke: false, fillColor: "#fff", fillOpacity: 0.5 })).addTo(map);
  const outlines = L.layerGroup().addTo(map);
  const outlineStyle = Object.assign(pane("outlines"), { weight: 1.5, color: "#000", opacity: 1, dashArray: "1 4", fill: false });
  const trackLayer = L.layerGroup().addTo(map);
  const trackStyle = pane("track");
  const frameStyle = Object.assign(pane("frame"), { lineJoin: "miter", lineCap: "butt", fill: false, opacity: 1 });
  const BLANK = [[0, 0], [0, 0]];
  const halo = L.rectangle(BLANK, Object.assign({}, frameStyle, { className: "f-halo", weight: 9, color: "#fff" })).addTo(map);
  const ink = L.rectangle(BLANK, Object.assign({}, frameStyle, { className: "f-ink", weight: 5, color: "#000" })).addTo(map);
  const tag = L.marker([0, 0], { interactive: false, keyboard: false, icon: L.divIcon({ className: "frame-tag", iconSize: null, iconAnchor: [0, 0], html: '<span class="tag" id="tag"></span>' }) }).addTo(map);
  let youMarker = null;

  /* ---------- state of the page ---------- */

  let size = 10;
  let cur = null;           // id of the block on the map
  let pin = null;           // {id, ll}: a block chosen by id stays chosen until the map really moves
  let index = null;         // {b, x} of blocks/index.json
  let indexFailed = false;
  let lastCheck = 0;
  let checking = false;
  let sending = false;
  let open = false;
  let wide = wideQuery.matches;
  let requests = store.get("mm.requests", {});
  let copiedUntil = 0;
  let booted = false;
  let framePx = 0;
  let frameHigh = 0;
  let frameState = "checking";

  const hasWaiting = () => Object.keys(requests).some(id => Date.now() - requests[id] < REQUEST_LIFE_MS);

  function stateOf(id) {
    if (!Blocks.parse(id)) {
      return "offgrid";
    }
    if (index) {
      if (index.b[id]) {
        return "ready";
      }
      if (index.x[id] === "empty") {
        return "empty";
      }
      if (index.x[id] === "dense") {
        return "dense";
      }
    }
    const asked = requests[id];
    if (asked && Date.now() - asked < REQUEST_LIFE_MS) {
      return "building";
    }
    if (index) {
      return "notbuilt";
    }
    return indexFailed ? "unknown" : "checking";
  }

  /* ---------- drawing the block ---------- */

  function placeFrame() {
    const [s, w, n, e] = Blocks.bounds(cur);
    const box = [[s, w], [n, e]];
    halo.setBounds(box);
    ink.setBounds(box);
    tag.setLatLng([n, w]);
    veil.setLatLngs([WORLD, [[s, w], [n, w], [n, e], [s, e]]]);
  }

  function fitPlates() {
    const z = map.getZoom();
    const [s, w, n, e] = Blocks.bounds(cur);
    const px = map.project([s, e], z).x - map.project([s, w], z).x;
    framePx = px;
    frameHigh = map.project([s, w], z).y - map.project([n, w], z).y;
    $("stage").classList.toggle("tiny", px < TAG_MIN_PX);
    dashFrame();
    veil.setStyle({ fillOpacity: z >= VEIL_ZOOM ? 0.5 : 0 });
  }

  // a whole number of dashes on every side, each corner in the middle of a dash
  function dashFrame() {
    const el = ink.getElement();
    const pattern = DASH[frameState];
    if (!pattern) {
      el.style.strokeDasharray = "none";
      el.style.strokeDashoffset = "0";
      return;
    }
    const [on, off] = pattern;
    const per = Math.max(1, Math.round(framePx / (on + off)));
    const k = (framePx + frameHigh) / 2 / per / (on + off);
    el.style.strokeDasharray = on * k + " " + off * k;
    el.style.strokeDashoffset = String(on * k / 2);
    el.style.setProperty("--march", -(on + off) * k + "px");
  }

  function setAct(label, handler, disabled) {
    const act = $("act");
    act.textContent = label;
    act.disabled = !!disabled;
    act.onclick = handler;
  }

  function otherPlace() {
    setOpen(false);
    $("q").focus();
    $("q").select();
  }

  function render() {
    const id = cur;
    const st = stateOf(id);
    const lowZoom = map.getZoom() < PICK_ZOOM;
    const asked = requests[id];
    const old = st === "notbuilt" && asked && Date.now() - asked >= REQUEST_LIFE_MS;
    let meta = id;
    let note = T.note[st] || "";

    ink.getElement().setAttribute("class", "f-ink s-" + st);
    frameState = st;
    dashFrame();
    $("swatch").setAttribute("class", "f-ink s-" + st);
    const tagEl = $("tag");
    if (tagEl) {
      tagEl.className = "tag" + (st === "empty" || st === "dense" || st === "offgrid" ? " alert" : "");
      tagEl.innerHTML = microSvg(framePx < TAG_FULL_PX ? T.tag[st] : T.tag[st] + " " + size + " KM", 2);
    }
    $("stWord").textContent = T.word[st];

    if (st === "ready") {
      const [title, kb, date, commit] = index.b[id];
      meta = title + ", " + kb + " KB";
      note = fill(T.note.ready, { date });
      $("rdTitle").textContent = title;
      $("rdKb").textContent = kb + " KB";
      $("rdDate").textContent = date;
      $("rdId").textContent = id;
      $("mainLink").value = Blocks.mainLink(id);
      const good = /^[0-9a-f]{7,40}$/.test(String(commit));
      $("second").hidden = !good;
      $("cdnLink").value = good ? Blocks.secondLink(id, commit) : "";
      if (Date.now() < copiedUntil) {
        setAct(T.act.copied, null, true);
      } else {
        setAct(T.act.copy, async () => {
          // the steps come into view, so the Copy button beside the link is where the result shows
          setOpen(true);
          $("toWatch").scrollIntoView({ block: "start" });
          if (await copyText(Blocks.mainLink(id), document.querySelector('[data-copy="mainLink"]'), $("mainLink"))) {
            copiedUntil = Date.now() + 2200;
            render();
            setTimeout(render, 2300);
          }
        });
      }
    } else if (st === "notbuilt") {
      note = old ? fill(T.note.expired, { time: stamp(asked) }) : lowZoom ? T.note.lowzoom : T.note.notbuilt;
      if (sending) {
        setAct(T.act.sending, null, true);
      } else if (lowZoom) {
        setAct(T.act.zoom, () => select(id));
      } else {
        setAct(T.act.request, () => sendRequest(id));
      }
    } else if (st === "building") {
      note = fill(Date.now() - asked > SLOW_MS ? T.note.slow : T.note.building, { time: clock(asked) });
      setAct(checking ? T.act.checking : T.act.check, () => loadIndex(), checking);
    } else if (st === "dense") {
      note = size === 20 ? T.note.dense20 : T.note.dense10;
      if (size === 20) {
        setAct(T.act.to10, () => changeSize(10));
      } else {
        setAct(T.act.other, otherPlace);
      }
    } else if (st === "empty") {
      if (size === 10) {
        setAct(T.act.to20, () => changeSize(20));
      } else {
        setAct(T.act.other, otherPlace);
      }
    } else if (st === "offgrid") {
      setAct(T.act.other, otherPlace);
    } else if (st === "unknown") {
      setAct(checking ? T.act.checking : T.act.retry, () => loadIndex(), checking);
    } else {
      setAct(T.act.list, null, true);
    }

    $("stMeta").textContent = meta;
    $("stNote").textContent = note;

    for (const el of document.querySelectorAll("[data-for]")) {
      el.hidden = !el.dataset.for.split(" ").includes(st);
    }
    for (const el of document.querySelectorAll('[data-show="building"]')) {
      el.hidden = st !== "building";
    }
    $("reqTitle").textContent = st === "building" ? "While you wait" : "What a request does";
    $("lastCheck").textContent = lastCheck ? fill(T.lastCheck, { time: clock(lastCheck) }) : "";
    $("shareLink").value = location.href.split("#")[0] + "#" + id;
  }

  function caption() {
    const c = map.getCenter();
    $("cap").textContent = c.lat.toFixed(4) + ", " + c.lng.toFixed(4) + "  " + cur;
  }

  function update() {
    const c = map.getCenter();
    if (pin) {
      const z = map.getZoom();
      if (map.project(c, z).distanceTo(map.project(pin.ll, z)) > PIN_SLACK_PX) {
        pin = null;
      }
    }
    const id = pin ? pin.id : Blocks.nearest(c.lat, c.lng, size);
    if (id !== cur) {
      cur = id;
      placeFrame();
      fitPlates();
      render();
    }
    caption();
  }

  let frameDue = false;
  map.on("move", () => {
    if (booted && !frameDue) {
      frameDue = true;
      requestAnimationFrame(() => {
        frameDue = false;
        update();
      });
    }
  });

  function drawOutlines() {
    outlines.clearLayers();
    if (!index || map.getZoom() < OUTLINE_ZOOM) {
      return;
    }
    const view = map.getBounds().pad(0.3);
    let n = 0;
    for (const id of Object.keys(index.b)) {
      if (id === cur || !Blocks.parse(id)) {
        continue;
      }
      const [s, w, north, e] = Blocks.bounds(id);
      if (s < view.getNorth() && north > view.getSouth() && w < view.getEast() && e > view.getWest()) {
        outlines.addLayer(L.rectangle([[s, w], [north, e]], outlineStyle));
        if (++n >= MAX_OUTLINES) {
          break;
        }
      }
    }
  }

  map.on("moveend", () => {
    if (!booted) {
      return;     // the first moveend comes before the layers are on the map
    }
    update();
    fitPlates();
    drawOutlines();
    render();
    const c = map.getCenter();
    store.set("mm.view", { lat: c.lat, lon: c.lng, z: map.getZoom(), size });
    if (Blocks.parse(cur)) {
      try {
        history.replaceState(null, "", "#" + cur);
      } catch (e) {
        // some file:// setups refuse it, the hash is only a convenience
      }
      $("shareLink").value = location.href;
    }
    $("live").textContent = $("stWord").textContent + ". " + $("stMeta").textContent;
  });

  /* ---------- moving to a place ---------- */

  function fitZoom(id, extra) {
    const [s, w, n, e] = Blocks.bounds(id);
    const box = L.latLngBounds([[s, w], [n, e]]);
    if (extra) {
      box.extend(extra);
    }
    // centred on the map, so the plate at the top costs its height twice
    const plate = document.querySelector(".brand").getBoundingClientRect().height;
    // and the zoom is for the map behind the sheet, not the strip the open sheet leaves
    const hidden = open ? $("sheet").offsetHeight - parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--peek")) : 0;
    return Math.min(map.getBoundsZoom(box, false, L.point(24, 2 * plate + 32 - hidden)), 14);
  }

  function select(id, extra) {
    const b = Blocks.split(id);
    setSizeControl(b.size);
    size = b.size;
    const ll = L.latLng(Blocks.middle(id));
    pin = { id, ll };
    map.setView(ll, fitZoom(id, extra), { animate: false });
    if (booted) {
      update();
      render();
    }
  }

  function go(lat, lon) {
    pin = null;
    const id = Blocks.nearest(lat, lon, size);
    map.setView([lat, lon], Blocks.parse(id) ? fitZoom(id) : 8, { animate: false });
  }

  function setSizeControl(s) {
    for (const r of document.querySelectorAll('input[name="size"]')) {
      r.checked = +r.value === s;
    }
  }

  function changeSize(s) {
    size = s;
    setSizeControl(s);
    pin = null;
    cur = null;
    update();
    const fit = fitZoom(cur);
    if (fit < map.getZoom()) {
      map.setZoom(fit, { animate: false });
    }
    render();
  }

  for (const r of document.querySelectorAll('input[name="size"]')) {
    r.addEventListener("change", () => changeSize(+r.value));
  }

  map.on("click", e => {
    if (open && !wide) {
      setOpen(false);
      return;
    }
    if (index && map.getZoom() >= OUTLINE_ZOOM) {
      let best = null;
      let bestD = Infinity;
      for (const id of Object.keys(index.b)) {
        const [s, w, n, east] = Blocks.bounds(id);
        if (e.latlng.lat > s && e.latlng.lat < n && e.latlng.lng > w && e.latlng.lng < east) {
          const d = Math.hypot((e.latlng.lat - (s + n) / 2) / (n - s), (e.latlng.lng - (w + east) / 2) / (east - w));
          if (d < bestD && Blocks.parse(id)) {
            bestD = d;
            best = id;
          }
        }
      }
      if (best) {
        select(best);
        return;
      }
    }
    pin = null;
    map.setView(e.latlng, map.getZoom(), { animate: !reduced });
  });

  $("zoomIn").addEventListener("click", () => map.zoomIn());
  $("zoomOut").addEventListener("click", () => map.zoomOut());

  /* ---------- tiles ---------- */

  let tileBad = 0;
  let alertEl = null;
  tiles.on("tileerror", () => {
    tileBad += 1;
    if (tileBad >= 4 && !alertEl) {
      alertEl = document.createElement("p");
      alertEl.className = "plate";
      alertEl.setAttribute("role", "alert");
      alertEl.textContent = T.tiles;
      $("alerts").append(alertEl);
    }
  });
  tiles.on("tileload", e => {
    if (e.tile.src.startsWith("http")) {
      tileBad = 0;
      if (alertEl) {
        alertEl.remove();
        alertEl = null;
      }
    }
  });

  /* ---------- the block list ---------- */

  async function loadIndex() {
    if (checking) {
      return;
    }
    checking = true;
    render();
    try {
      const r = await timedFetch(INDEX_URL + "?t=" + Date.now(), { cache: "no-store" });
      if (r.status === 404) {
        index = { b: {}, x: {} };   // nothing published yet
      } else if (r.ok) {
        const j = await r.json();
        index = { b: j.b || {}, x: j.x || {} };
      } else {
        throw new Error("status " + r.status);
      }
      indexFailed = false;
      lastCheck = Date.now();
      settleRequests();
    } catch (e) {
      indexFailed = true;       // a list from before stays good enough
    }
    checking = false;
    if (cur) {
      render();
      drawOutlines();
    }
  }

  // a request that got its answer leaves the list, and the answer is said once
  function settleRequests() {
    let told = false;
    for (const id of Object.keys(requests)) {
      const done = index.b[id] ? "ready" : index.x[id];
      if (done) {
        delete requests[id];
        if (!told && id !== cur) {
          told = true;
          flash(done === "ready" ? fill(T.requestReady, { id }) : fill(T.requestRefused, { id, why: T.why[done] || done }),
            { label: T.show, run: () => select(id) });
        }
      } else if (Date.now() - requests[id] > 7 * 24 * 3600000) {
        delete requests[id];
      }
    }
    store.set("mm.requests", requests);
    renderRequests();
  }

  function renderRequests() {
    const list = $("reqList");
    list.textContent = "";
    const ids = Object.keys(requests);
    $("requests").hidden = !ids.length;
    for (const id of ids) {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = id;
      b.addEventListener("click", () => select(id));
      const s = document.createElement("span");
      s.className = "val";
      s.textContent = Date.now() - requests[id] < REQUEST_LIFE_MS ? T.requestWaiting : T.requestOld;
      li.append(b, s);
      list.append(li);
    }
  }

  setInterval(() => {
    if (hasWaiting() && !document.hidden) {
      loadIndex();
    }
  }, RECHECK_MS);

  function onReturn() {
    if (!document.hidden && hasWaiting() && Date.now() - lastCheck > RETURN_GAP_MS) {
      loadIndex();
    }
  }
  document.addEventListener("visibilitychange", onReturn);
  window.addEventListener("pageshow", onReturn);
  window.addEventListener("focus", onReturn);

  /* ---------- requests ---------- */

  async function sendRequest(id) {
    if (sending) {
      return;
    }
    sending = true;
    render();
    try {
      const r = await timedFetch(NTFY, { method: "POST", body: id });
      if (!r.ok) {
        throw new Error("status " + r.status);
      }
      requests[id] = Date.now();
      store.set("mm.requests", requests);
      renderRequests();
    } catch (e) {
      const body = encodeURIComponent(id);
      flash(T.requestFailed, { label: T.requestMail, href: "mailto:" + MAIL + "?subject=microMAPS%20block%20request&body=" + body });
    }
    sending = false;
    render();
  }

  /* ---------- plates ---------- */

  function flash(text, action) {
    $("flashText").textContent = text;
    const link = $("flashLink");
    const btn = $("flashAct");
    link.hidden = true;
    btn.hidden = true;
    if (action && action.href) {
      link.href = action.href;
      link.textContent = action.label;
      link.hidden = false;
    } else if (action && action.run) {
      btn.textContent = action.label;
      btn.onclick = () => {
        $("flash").hidden = true;
        action.run();
      };
      btn.hidden = false;
    }
    $("flash").hidden = false;
  }
  $("flashClose").addEventListener("click", () => {
    $("flash").hidden = true;
  });

  /* ---------- copy ---------- */

  async function copyText(text, button, field) {
    let ok = true;
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      if (field) {
        field.focus();
        field.select();
      }
      try {
        ok = document.execCommand("copy");
      } catch (e2) {
        ok = false;
      }
    }
    if (button) {
      const label = button.dataset.label || button.textContent;
      button.dataset.label = label;
      button.textContent = ok ? T.copied : T.copyFail;
      setTimeout(() => {
        button.textContent = label;
      }, 2000);
    }
    return ok;
  }
  for (const b of document.querySelectorAll("[data-copy]")) {
    b.addEventListener("click", () => {
      const field = $(b.dataset.copy);
      copyText(field.value, b, field);
    });
  }

  /* ---------- search ---------- */

  let lastSearch = 0;
  let searching = false;

  // "lat, lon": a typed comma may also be the separator of "67.6 33.7"
  function parseCoords(text) {
    const m = /^\s*(-?\d+(?:\.\d+)?)\s*°?\s*[,;\s]\s*(-?\d+(?:\.\d+)?)\s*°?\s*$/.exec(text);
    return m ? [parseFloat(m[1]), parseFloat(m[2])] : null;
  }

  function hideHits() {
    $("hits").hidden = true;
  }

  function showHits(list) {
    const ul = $("hitList");
    ul.textContent = "";
    list.forEach((place, i) => {
      const li = document.createElement("li");
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = place.display_name;
      if (i === 0) {
        b.setAttribute("aria-current", "true");
      }
      b.addEventListener("click", () => {
        go(parseFloat(place.lat), parseFloat(place.lon));
        hideHits();
      });
      li.append(b);
      ul.append(li);
    });
    $("hits").hidden = false;
  }

  $("hitsClose").addEventListener("click", hideHits);

  $("search").addEventListener("submit", async e => {
    e.preventDefault();
    const q = $("q").value.trim();
    $("flash").hidden = true;
    if (!q) {
      return;
    }
    $("q").blur();
    const coords = parseCoords(q);
    if (coords) {
      hideHits();
      if (Math.abs(coords[0]) > 85 || Math.abs(coords[1]) > 180) {
        flash(T.badCoords);
      } else {
        go(coords[0], coords[1]);
      }
      return;
    }
    if (Blocks.parse(q)) {
      hideHits();
      select(q);
      return;
    }
    if (searching) {
      return;
    }
    searching = true;
    $("go").disabled = true;
    $("go").textContent = T.searching;
    try {
      const wait = lastSearch + SEARCH_GAP_MS - Date.now();
      if (wait > 0) {
        await later(wait);
      }
      lastSearch = Date.now();
      const url = NOMINATIM + "?format=jsonv2&limit=4&accept-language=en&q=" + encodeURIComponent(q);
      const r = await timedFetch(url);
      if (!r.ok) {
        throw new Error("status " + r.status);
      }
      const list = (await r.json()).filter(p => Math.abs(parseFloat(p.lat)) <= 85);
      if (list.length) {
        showHits(list);
        go(parseFloat(list[0].lat), parseFloat(list[0].lon));
      } else {
        hideHits();
        flash(fill(T.noPlace, { q }));
      }
    } catch (err) {
      hideHits();
      flash(T.noSearch);
    }
    searching = false;
    $("go").disabled = false;
    $("go").textContent = T.search;
  });

  /* ---------- locate ---------- */

  $("locate").addEventListener("click", () => {
    const btn = $("locate");
    if (!navigator.geolocation) {
      flash(T.noLocate);
      return;
    }
    btn.setAttribute("aria-busy", "true");
    navigator.geolocation.getCurrentPosition(p => {
      btn.removeAttribute("aria-busy");
      $("flash").hidden = true;
      const ll = [p.coords.latitude, p.coords.longitude];
      if (youMarker) {
        youMarker.setLatLng(ll);
      } else {
        youMarker = L.marker(ll, {
          interactive: false, keyboard: false, zIndexOffset: 100,
          icon: L.divIcon({
            className: "glyph", iconSize: [24, 30], iconAnchor: [12, 16],
            html: '<svg viewBox="-12 -14 24 30" width="24" height="30" aria-hidden="true" style="shape-rendering:auto"><path d="M0-10 7 8 0 4-7 8z" fill="#fff" stroke="#fff" stroke-width="5" stroke-linejoin="round"/><path d="M0-10 7 8 0 4-7 8z"/></svg>',
          }),
        }).addTo(map);
      }
      go(ll[0], ll[1]);
    }, err => {
      btn.removeAttribute("aria-busy");
      flash(err.code === 1 ? T.locateDenied : T.locateFailed);
    }, { enableHighAccuracy: false, timeout: 12000, maximumAge: 60000 });
  });

  /* ---------- the sheet ---------- */

  function setOpen(v) {
    open = v && !wide;
    document.body.classList.toggle("open", open);
    $("grip").setAttribute("aria-expanded", String(open));
    $("gripText").textContent = open ? T.hideDetails : T.showDetails;
    $("more").inert = !open && !wide;
    if (!open) {
      $("scroll").scrollTop = 0;
    }
  }

  function measure() {
    const peek = Math.ceil($("grip").getBoundingClientRect().height + $("peek").getBoundingClientRect().height);
    if (!wide && !open) {
      document.documentElement.style.setProperty("--peek", peek + "px");
    }
  }

  // the map keeps its centre through every change of size, and with it the block
  new ResizeObserver(() => {
    if (cur) {
      pin = pin || { id: cur, ll: map.getCenter() };
    }
    map.invalidateSize({ animate: false });
    if (cur) {
      fitPlates();
    }
  }).observe($("stage"));
  new ResizeObserver(measure).observe($("peek"));

  wideQuery.addEventListener("change", e => {
    wide = e.matches;
    setOpen(open);
    measure();
  });

  (function dragSheet() {
    const grip = $("grip");
    const sheet = $("sheet");
    let from = null;
    let start = 0;
    let moved = 0;
    grip.addEventListener("pointerdown", e => {
      from = e.clientY;
      moved = 0;
      start = sheet.getBoundingClientRect().top;
      grip.setPointerCapture(e.pointerId);
    });
    grip.addEventListener("pointermove", e => {
      if (from === null) {
        return;
      }
      moved = e.clientY - from;
      if (Math.abs(moved) > 5) {
        const top = innerHeight - sheet.offsetHeight;
        const low = innerHeight - parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--peek"));
        const y = Math.max(top, Math.min(low, start + moved));
        sheet.classList.add("dragging");
        sheet.style.transform = "translateY(" + (y - top) + "px)";
      }
    });
    const end = () => {
      if (from === null) {
        return;
      }
      from = null;
      const dragged = sheet.classList.contains("dragging");
      const top = sheet.getBoundingClientRect().top;
      sheet.classList.remove("dragging");
      sheet.style.transform = "";
      if (dragged) {
        const low = innerHeight - parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--peek"));
        const high = innerHeight - sheet.offsetHeight;
        setOpen(top < (low + high) / 2);
      }
    };
    grip.addEventListener("pointerup", end);
    grip.addEventListener("pointercancel", end);
    // pointerup above already did the work of a drag; a tap or a key press comes here
    grip.addEventListener("click", () => {
      if (Math.abs(moved) <= 5) {
        setOpen(!open);
      }
      moved = 0;
    });
  })();

  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && open) {
      setOpen(false);
      $("grip").focus();
    }
  });

  /* ---------- route ---------- */

  function clearTrack() {
    trackLayer.clearLayers();
  }

  function drawTrack(gpx) {
    clearTrack();
    const step = Math.ceil(gpx.track.length / TRACK_DRAW_POINTS);
    const line = gpx.track.filter((p, i) => i % step === 0 || i === gpx.track.length - 1);
    const stroke = { lineCap: "round", lineJoin: "round", opacity: 1, smoothFactor: 1.2 };
    trackLayer.addLayer(L.polyline(line, Object.assign({}, trackStyle, stroke, { color: "#fff", weight: 9 })));
    trackLayer.addLayer(L.polyline(line, Object.assign({}, trackStyle, stroke, { color: "#000", weight: 5 })));
    const mark = (ll, g) => trackLayer.addLayer(L.marker(ll, { interactive: false, keyboard: false, icon: glyphIcon(g, 2) }));
    for (const w of gpx.wpts.slice(0, 10)) {
      mark([w.lat, w.lon], GLYPH.way);
    }
    mark(gpx.track[0], GLYPH.start);
    mark(gpx.track[gpx.track.length - 1], GLYPH.finish);
  }

  function holds(id, box) {
    const [s, w, n, e] = Blocks.bounds(id);
    return box.s >= s && box.n <= n && box.w >= w && box.e <= e;
  }

  // share of the track's length inside a block
  function share(id, pts) {
    const [s, w, n, e] = Blocks.bounds(id);
    let all = 0;
    let inside = 0;
    for (let i = 1; i < pts.length; i++) {
      const lat = (pts[i][0] + pts[i - 1][0]) / 2;
      const lon = (pts[i][1] + pts[i - 1][1]) / 2;
      const d = Math.hypot(pts[i][0] - pts[i - 1][0], (pts[i][1] - pts[i - 1][1]) * Math.cos(lat * Math.PI / 180));
      all += d;
      if (lat >= s && lat <= n && lon >= w && lon <= e) {
        inside += d;
      }
    }
    return all ? inside / all : 0;
  }

  // the block for a track: the smallest size that holds it, the one nearest to its middle
  function blockFor(track) {
    const step = Math.ceil(track.length / TRACK_FIT_POINTS);
    const pts = track.filter((p, i) => i % step === 0 || i === track.length - 1);
    const box = { s: 90, n: -90, w: 180, e: -180 };
    for (const [lat, lon] of track) {
      box.s = Math.min(box.s, lat);
      box.n = Math.max(box.n, lat);
      box.w = Math.min(box.w, lon);
      box.e = Math.max(box.e, lon);
    }
    const midLat = (box.s + box.n) / 2;
    const midLon = (box.w + box.e) / 2;
    let widest = null;
    for (const s of Blocks.SIZES) {
      const mid = Blocks.split(Blocks.nearest(midLat, midLon, s));
      let found = null;
      let foundD = Infinity;
      let top = 0;
      for (let dr = -4; dr <= 4; dr++) {
        const c0 = Blocks.split(Blocks.nearest(midLat + dr * Blocks.DLAT, midLon, s)).c;
        for (let dc = -4; dc <= 4; dc++) {
          const id = Blocks.blockId(s, mid.r + dr, c0 + dc);
          if (!Blocks.parse(id)) {
            continue;
          }
          if (holds(id, box)) {
            if (dr * dr + dc * dc < foundD) {
              foundD = dr * dr + dc * dc;
              found = id;
            }
          } else if (s === 20) {
            const part = share(id, pts);
            if (part > top) {
              top = part;
              widest = { id, part };
            }
          }
        }
      }
      if (found) {
        return { id: found, fits: true, size: s };
      }
    }
    return widest ? Object.assign(widest, { fits: false, size: 20 }) : null;
  }

  async function useGpx(text, fallbackName) {
    $("routeError").hidden = true;
    try {
      const gpx = parseGpx(text);
      const r = await makeRoute(gpx.track, gpx.wpts, gpx.name || fallbackName);
      $("rtName").textContent = r.name;
      $("rtKm").textContent = r.km.toFixed(1) + " km";
      $("rtPoints").textContent = String(r.points);
      $("rtEps").textContent = "about " + r.epsM + " m";
      $("rtCode").textContent = r.code.length + " characters";
      $("code").value = r.code;
      $("result").hidden = false;
      drawTrack(gpx);
      const place = blockFor(gpx.track);
      if (place) {
        select(place.id, L.latLngBounds(gpx.track));
        if (place.fits) {
          $("rtFit").textContent = fill(T.routeFits, { size: place.size });
        } else {
          const out = (1 - place.part) * r.km;
          $("rtFit").textContent = fill(T.routeOutside, { out: out.toFixed(1), all: r.km.toFixed(1), pct: Math.round((1 - place.part) * 100) });
        }
      } else {
        map.fitBounds(L.latLngBounds(gpx.track));
        $("rtFit").textContent = "";
      }
      $("result").scrollIntoView({ block: "start" });
    } catch (e) {
      $("routeError").textContent = e.message;
      $("routeError").hidden = false;
      $("result").hidden = true;
      clearTrack();
    }
  }

  async function useFile(f) {
    if (!f) {
      return;
    }
    let text;
    try {
      text = await f.text();
    } catch (e) {
      $("routeError").textContent = T.badFile;
      $("routeError").hidden = false;
      return;
    }
    await useGpx(text, f.name.replace(/\.[^.]*$/, ""));
  }

  function toRoute() {
    setOpen(true);
    $("route").scrollIntoView({ block: "start" });
  }

  $("gpx").addEventListener("click", toRoute);
  $("file").addEventListener("change", async e => {
    await useFile(e.target.files[0]);
    e.target.value = "";
  });
  $("usePaste").addEventListener("click", () => useGpx($("paste").value, "Route"));
  $("clearRoute").addEventListener("click", () => {
    clearTrack();
    $("result").hidden = true;
    $("code").value = "";
  });

  let dragDepth = 0;
  const hasFile = e => e.dataTransfer && Array.from(e.dataTransfer.types).includes("Files");
  window.addEventListener("dragenter", e => {
    if (hasFile(e)) {
      dragDepth += 1;
      $("drop").hidden = false;
    }
  });
  window.addEventListener("dragover", e => {
    if (hasFile(e)) {
      e.preventDefault();
    }
  });
  window.addEventListener("dragleave", e => {
    if (hasFile(e)) {
      dragDepth -= 1;
      if (dragDepth <= 0) {
        dragDepth = 0;
        $("drop").hidden = true;
      }
    }
  });
  window.addEventListener("drop", e => {
    if (hasFile(e)) {
      e.preventDefault();
      dragDepth = 0;
      $("drop").hidden = true;
      toRoute();
      useFile(e.dataTransfer.files[0]);
    }
  });

  /* ---------- ready-made areas ---------- */

  async function loadCatalog() {
    const list = $("areas");
    const row = (name, value) => {
      const li = document.createElement("li");
      const a = document.createElement("span");
      a.textContent = name;
      li.append(a);
      if (value) {
        const v = document.createElement("span");
        v.className = "val";
        v.textContent = value;
        li.append(v);
      }
      list.append(li);
    };
    try {
      const r = await timedFetch(CATALOG_URL);
      if (!r.ok) {
        throw new Error("status " + r.status);
      }
      const c = await r.json();
      list.textContent = "";
      c.packs.forEach(p => row(p.title, p.kb + " KB, " + p.date));
    } catch (e) {
      list.textContent = "";
      row(T.catalogFailed);
    }
  }

  /* ---------- start ---------- */

  function start() {
    const hash = decodeURIComponent(location.hash.slice(1));
    const saved = store.get("mm.view", null);
    let goesToRoute = false;
    measure();
    map.invalidateSize({ animate: false });
    if (saved && Blocks.SIZES.includes(saved.size)) {
      size = saved.size;
      setSizeControl(size);
    }
    if (hash && Blocks.parse(hash)) {
      select(hash);
    } else {
      if (hash === "route") {
        goesToRoute = true;
      } else if (hash) {
        flash(T.badLink);
      }
      if (saved && Number.isFinite(saved.lat) && Number.isFinite(saved.lon) && Number.isFinite(saved.z)) {
        map.setView([saved.lat, saved.lon], saved.z, { animate: false });
      } else {
        map.setView(HOME, fitZoom(Blocks.nearest(HOME[0], HOME[1], size)), { animate: false });
      }
    }
    booted = true;
    update();
    setOpen(false);
    renderRequests();
    loadIndex();
    loadCatalog();
    if (goesToRoute) {
      toRoute();
    }
  }

  window.addEventListener("hashchange", () => {
    const id = decodeURIComponent(location.hash.slice(1));
    if (id === "route") {
      toRoute();
    } else if (Blocks.parse(id)) {
      if (id !== cur) {
        select(id);
      }
    } else if (id) {
      flash(T.badLink);
    }
  });

  start();
})();
