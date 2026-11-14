// Who asks for which blocks: requests of the last 12 hours and the builder's saved queue on the
// map, a ripple where a request comes in, and a plate with the latest events. Reads the public
// ntfy topics with one stream (docs/BLOCKS.md, Requests and Notices) and blocks/index.json
// through the hook at the end of app.js. Node loads the checks below for the tests.
//
// Anyone can write to both topics. So a message counts only as a block id that passes
// Blocks.parse or a status object made of such ids; no text of a message is ever shown, counts
// and lists are capped, the same message is taken once, and a dead ntfy leaves the page as it was.
"use strict";

(function (root) {
  const B = typeof module !== "undefined" && module.exports ? require("./blocks.js") : root.Blocks;

  const REQ_TOPIC = "micromaps-blocks-v1";
  const STATUS_TOPIC = "micromaps-status-v1";
  const WINDOW_MS = 12 * 3600000;         // ntfy keeps a message this long
  const BUILDING_MS = 10 * 60000;          // a round that took a block this long ago is over
  const MERGE_MS = 10 * 60000;             // the same word about the same block within this is one event
  const MAX_TEXT = 4096;                   // ntfy's own limit of a message
  const MAX_IDS = 400;                     // block ids remembered from the request topic
  const MAX_SEEN = 2000;                   // message ids remembered, so a replay is taken once
  const MAX_EVENTS = 80;
  const MAX_PER_STATUS = 50;               // ids taken from one status message
  const MAX_QUEUE = 300;                   // ids taken from index.json q (the builder keeps 200)
  const MSG_ID_RE = /^[A-Za-z0-9]{1,32}$/;
  const DAY_RE = /^\d{4}-\d{2}-\d{2}$/;
  const own = (o, k) => !!o && Object.prototype.hasOwnProperty.call(o, k);
  const plain = v => !!v && typeof v === "object" && !Array.isArray(v);

  const isId = t => typeof t === "string" && t.length <= 16 && B.parse(t) !== null;

  // A line of the ntfy stream or poll: {key, at, topic, text} of one of our messages, or null.
  function envelope(m, now) {
    if (!plain(m) || m.event !== "message" || (m.topic !== REQ_TOPIC && m.topic !== STATUS_TOPIC)) {
      return null;
    }
    if (typeof m.id !== "string" || !MSG_ID_RE.test(m.id) || !Number.isInteger(m.time)) {
      return null;
    }
    const at = m.time * 1000;
    if (at < now - WINDOW_MS - 3600000 || at > now + 300000 || typeof m.message !== "string" || m.message.length > MAX_TEXT) {
      return null;
    }
    return { key: m.id, at, topic: m.topic, text: m.message };
  }

  // the body of a request is a block id and nothing else, as for the builder
  const parseRequest = text => (isId(text) ? text : null);

  // a status message, rebuilt from its good ids: {v, w, b, f, x} or null
  function parseStatus(text) {
    let j;
    try {
      j = JSON.parse(text);
    } catch (e) {
      return null;
    }
    if (!plain(j) || j.v !== 1) {
      return null;
    }
    const list = v => (Array.isArray(v) ? Array.from(new Set(v.slice(0, MAX_PER_STATUS).filter(isId))) : []);
    const x = {};
    if (plain(j.x)) {
      for (const id of Object.keys(j.x).slice(0, MAX_PER_STATUS)) {
        if (isId(id)) {
          x[id] = j.x[id] === "empty" || j.x[id] === "dense" ? j.x[id] : "other";
        }
      }
    }
    const s = { v: 1, w: list(j.w), b: list(j.b), f: list(j.f), x };
    return s.w.length + s.b.length + s.f.length + Object.keys(x).length ? s : null;
  }

  // what the stream told so far, kept small whatever comes in
  function Feed() {
    this.seen = new Map();     // message id: true, oldest first
    this.asked = new Map();    // block id: {first, last, n}
    this.said = new Map();     // block id: {w, b, x, f, why}, times of the builder's words
    this.events = [];          // newest first: {at, kind, id, why, n}
    this.last = "";            // newest message id, for since= on a new stream
    this.lastAt = 0;
    this.version = 0;          // goes up when a block id comes into asked or leaves it
  }

  // one parsed line of the stream; gives back {ask: id} or {status} for a new message, else null
  Feed.prototype.take = function (m, now) {
    const e = envelope(m, now);
    if (!e || this.seen.has(e.key)) {
      return null;
    }
    this.seen.set(e.key, true);
    if (this.seen.size > MAX_SEEN) {
      this.seen.delete(this.seen.keys().next().value);
    }
    if (e.at >= this.lastAt) {
      this.last = e.key;
      this.lastAt = e.at;
    }
    if (e.topic === REQ_TOPIC) {
      const id = parseRequest(e.text);
      if (!id) {
        return null;
      }
      const a = this.asked.get(id);
      if (a) {
        a.first = Math.min(a.first, e.at);
        a.last = Math.max(a.last, e.at);
        a.n += 1;
      } else {
        this.version += 1;
        this.asked.set(id, { first: e.at, last: e.at, n: 1 });
        if (this.asked.size > MAX_IDS) {
          let old = null;
          for (const [k, v] of this.asked) {
            if (!old || v.last < this.asked.get(old).last) {
              old = k;
            }
          }
          this.asked.delete(old);
        }
      }
      this.note("asked", id, e.at);
      return { ask: id, at: e.at };
    }
    const s = parseStatus(e.text);
    if (!s) {
      return null;
    }
    const word = (id, k, why) => {
      const w = this.said.get(id) || {};
      w[k] = Math.max(w[k] || 0, e.at);
      if (why) {
        w.why = why;
      }
      this.said.set(id, w);
    };
    for (const id of s.w) {
      word(id, "w");
      this.note("building", id, e.at);
    }
    for (const id of s.b) {
      word(id, "b");
      this.note("built", id, e.at);
    }
    for (const id of Object.keys(s.x)) {
      word(id, "x", s.x[id]);
      this.note("refused", id, e.at, s.x[id]);
    }
    for (const id of s.f) {
      word(id, "f");
      this.note("failed", id, e.at);
    }
    if (this.said.size > MAX_IDS * 2) {
      this.prune(now);
    }
    return { status: s, at: e.at };
  };

  Feed.prototype.note = function (kind, id, at, why) {
    for (const ev of this.events) {
      if (ev.kind === kind && ev.id === id && Math.abs(ev.at - at) < MERGE_MS) {
        ev.at = Math.max(ev.at, at);
        ev.n += 1;
        this.events.sort((p, q) => q.at - p.at);
        return;
      }
    }
    this.events.push({ at, kind, id, why: why || "", n: 1 });
    this.events.sort((p, q) => q.at - p.at);
    this.events.length = Math.min(this.events.length, MAX_EVENTS);
  };

  Feed.prototype.prune = function (now) {
    const edge = now - WINDOW_MS;
    for (const [id, a] of this.asked) {
      if (a.last < edge) {
        this.asked.delete(id);
        this.version += 1;
      }
    }
    for (const [id, w] of this.said) {
      if (Math.max(w.w || 0, w.b || 0, w.x || 0, w.f || 0) < edge) {
        this.said.delete(id);
      }
    }
    this.events = this.events.filter(ev => ev.at >= edge);
  };

  // somebody asked for this block in the window
  Feed.prototype.has = function (id, now) {
    const a = isId(id) ? this.asked.get(id) : null;
    return !!a && a.last >= now - WINDOW_MS;
  };

  // the latest request still in the window: {id, at} or null
  Feed.prototype.latest = function (now) {
    let best = null;
    for (const [id, a] of this.asked) {
      if (a.last >= now - WINDOW_MS && (!best || a.last > best.at)) {
        best = { id, at: a.last };
      }
    }
    return best;
  };

  // Blocks asked for and without an answer yet: requests of the window and the saved queue of
  // the index, less what the index has built or refused. As for app.js, a word of the builder is
  // a hint and only the index says a block is done: the page reads it again after such a word.
  Feed.prototype.waiting = function (index, now) {
    const since = new Map();
    for (const [id, a] of this.asked) {
      if (a.last >= now - WINDOW_MS) {
        since.set(id, a.first);
      }
    }
    if (index && plain(index.q)) {
      for (const id of Object.keys(index.q).slice(0, MAX_QUEUE)) {
        if (!isId(id)) {
          continue;
        }
        const t = index.q[id];
        const at = Number.isInteger(t) && t > 1.6e9 && t * 1000 < now + 86400000 ? t * 1000 : now;
        since.set(id, Math.min(since.has(id) ? since.get(id) : Infinity, at));
      }
    }
    const out = [];
    for (const [id, at] of since) {
      if (index && (own(index.b, id) || own(index.x, id))) {
        continue;
      }
      const w = this.said.get(id) || {};
      const answered = Math.max(w.b || 0, w.x || 0);
      const building = !!w.w && now - w.w < BUILDING_MS && w.w > Math.max(answered, w.f || 0);
      out.push({ id, at, building });
    }
    return out.sort((p, q) => q.at - p.at);
  };

  // in the window: distinct blocks asked for, and built by word of the builder
  Feed.prototype.counts = function (now) {
    const edge = now - WINDOW_MS;
    let asked = 0;
    let built = 0;
    for (const a of this.asked.values()) {
      asked += a.last >= edge ? 1 : 0;
    }
    for (const w of this.said.values()) {
      built += (w.b || 0) >= edge ? 1 : 0;
    }
    return { asked, built };
  };

  // newest built blocks of the index: [{id, title, date}], at most n
  function newest(index, n) {
    if (!index || !plain(index.b)) {
      return [];
    }
    const out = [];
    for (const id of Object.keys(index.b)) {
      const row = index.b[id];
      if (isId(id) && Array.isArray(row) && DAY_RE.test(String(row[2]))) {
        out.push({ id, title: cleanName(row[0]), date: String(row[2]) });
      }
    }
    return out.sort((p, q) => (p.date < q.date ? 1 : p.date > q.date ? -1 : 0)).slice(0, n);
  }

  // a name from elsewhere, as plain short text: no control characters, no markup
  function cleanName(v) {
    return typeof v === "string" ? v.replace(/[\u0000-\u001f\u007f-\u009f<>]/g, "").replace(/\s+/g, " ").trim().slice(0, 40) : "";
  }

  function ago(ms, now) {
    const s = Math.max(0, now - ms) / 1000;
    if (s < 60) {
      return "just now";
    }
    if (s < 3600) {
      return Math.floor(s / 60) + " min ago";
    }
    return Math.floor(s / 3600) + " h ago";
  }

  const core = { REQ_TOPIC, STATUS_TOPIC, WINDOW_MS, MAX_IDS, MAX_SEEN, MAX_EVENTS, envelope, parseRequest, parseStatus, Feed,
    newest, cleanName, ago };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = core;
    return;
  }
  if (!root.MMPage || !root.L || typeof document === "undefined") {
    return;
  }

  /* ---------- the page ---------- */

  const P = root.MMPage;
  const map = P.map;
  const STREAM = "https://ntfy.sh/" + REQ_TOPIC + "," + STATUS_TOPIC;
  const NOMINATIM = "https://nominatim.openstreetmap.org/reverse";
  const RECT_ZOOM = 9;                     // from here a block is drawn as itself, below it as a mark
  const CELL_PX = 56;                      // marks closer than this are one mark with a count
  const MAX_RECTS = 300;
  const PULSE_GAP_MS = 1500;
  const PULSE_MS = 2400;
  const RELOAD_GAP_MS = 60000;
  const RELOAD_AFTER_MS = 15000;           // GitHub Pages serves the new index a little after the word
  const PLACE_GAP_MS = 1100;               // Nominatim: at most one request a second
  const PLACE_BUDGET = 8;                  // place names asked for in one visit, at most
  const LIST_EVENTS = 12;
  const LIST_NEW = 5;
  const T = {
    title: "Requests",
    last: "Last request {ago}",
    none: "No requests in the last 12 h",
    off: "Live requests are not available",
    sub: "{n} in 12 h · {w} waiting",
    subNone: "{w} waiting",
    kind: { asked: "Requested", building: "Building", built: "Built", refused: "Refused", failed: "Retry", waiting: "Waiting" },
    why: { empty: "no map data", dense: "too dense" },
    sumAsked: "Asked, 12 h", sumBuilt: "Built, 12 h", sumWait: "Waiting now",
    recent: "Last 12 hours", newest: "Newest blocks", quiet: "Nothing in the last 12 hours.",
    key: "Hatched on the map: asked for, not built yet.",
    markOne: "Requested block {id}", markMany: "{n} requested blocks, zoom in",
    live: "Live", connecting: "Connecting",
  };
  const fill = (text, values) => text.replace(/\{(\w+)\}/g, (m, k) => values[k]);
  const reduced = !!P.reduced;
  const narrow = matchMedia("(max-width: 1119px)");      // activity.css: the plate under the brand

  const feed = new Feed();
  let streaming = false;   // this file carries the status topic for app.js
  let es = null;
  let openedAt = 0;
  let tries = 0;
  let retry = 0;
  let lastPulse = 0;
  let lastReload = 0;
  let reloadDue = 0;
  let isOpen = false;
  let shown = false;
  let drawDue = false;

  /* ---------- the plate ---------- */

  const $ = id => document.getElementById(id);
  const make = (tag, cls, text) => {
    const el = document.createElement(tag);
    if (cls) {
      el.className = cls;
    }
    if (text) {
      el.textContent = text;
    }
    return el;
  };

  // the hatch of a requested block, one pixel stairs as on the watch
  const stairs = n => {
    let d = "";
    for (let i = 0; i < n; i++) {
      d += "M" + i + " " + (n - 1 - i) + "h1v1h-1z";
    }
    return d;
  };
  const defs = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  defs.setAttribute("width", "0");
  defs.setAttribute("height", "0");
  defs.setAttribute("aria-hidden", "true");
  defs.style.position = "absolute";
  defs.innerHTML = '<defs><pattern id="mm-hatch" width="8" height="8" patternUnits="userSpaceOnUse"><path d="' + stairs(8) +
    '"/></pattern><pattern id="mm-hatch-hot" width="4" height="4" patternUnits="userSpaceOnUse"><path d="' + stairs(4) + '"/></pattern></defs>';
  document.body.append(defs);

  const plate = make("section", "act");
  plate.id = "activity";
  plate.hidden = true;
  plate.setAttribute("aria-label", T.title);
  plate.innerHTML =
    '<button type="button" id="actHead" class="act-head" aria-expanded="false" aria-controls="actBody">' +
      '<span id="actLive" class="act-live" aria-hidden="true"></span>' +
      '<span class="act-lines"><strong id="actLine"></strong><span id="actSub"></span></span>' +
      '<svg class="act-caret" viewBox="0 0 11 6" width="11" height="6" aria-hidden="true"><path d="M0 0h11v1h-1v1h-1v1h-1v1h-1v1h-1v1h-1v-1h-1v-1h-1v-1h-1v-1h-1v-1h-1z"/></svg>' +
    "</button>" +
    '<div id="actBody" class="act-body" hidden>' +
      '<dl class="act-sum"><div><dt></dt><dd id="actAsked"></dd></div><div><dt></dt><dd id="actBuilt"></dd></div><div><dt></dt><dd id="actWait"></dd></div></dl>' +
      '<h3 id="actRecentH"></h3><ul id="actList" class="act-list"></ul>' +
      '<h3 id="actNewH"></h3><ul id="actNew" class="act-list"></ul>' +
      '<p class="act-key"><svg viewBox="0 0 14 14" width="14" height="14" aria-hidden="true"><rect class="act-swatch" x="1" y="1" width="12" height="12"/></svg><span id="actKey"></span></p>' +
    "</div>";
  const dts = plate.querySelectorAll(".act-sum dt");
  dts[0].textContent = T.sumAsked;
  dts[1].textContent = T.sumBuilt;
  dts[2].textContent = T.sumWait;
  $("stage").append(plate);
  $("actRecentH").textContent = T.recent;
  $("actNewH").textContent = T.newest;
  $("actKey").textContent = T.key;

  function setOpen(v) {
    isOpen = v;
    $("actHead").setAttribute("aria-expanded", String(v));
    $("actBody").hidden = !v;
    plate.classList.toggle("open", v);
    if (v) {
      ui();
    }
  }
  $("actHead").addEventListener("click", () => setOpen(!isOpen));
  plate.addEventListener("keydown", e => {
    if (e.key === "Escape" && isOpen) {
      e.stopPropagation();
      setOpen(false);
      $("actHead").focus();
    }
  });

  function go(id) {
    if (narrow.matches) {
      setOpen(false);
    }
    P.select(id);
  }

  /* ---------- names ---------- */

  const places = new Map();     // block id: a place name, "" when there is none
  const placeQueue = [];
  let placeBusy = false;
  let placeLast = 0;
  let placeLeft = PLACE_BUDGET;

  function titleOf(id) {
    const index = P.index();
    if (index && own(index.b, id) && Array.isArray(index.b[id])) {
      return cleanName(index.b[id][0]);
    }
    return places.get(id) || "";
  }

  // asked only while the plate is open, one at a time, a second apart, a few per visit
  function wantPlace(id) {
    if (!titleOf(id) && !places.has(id) && !placeQueue.includes(id) && placeQueue.length < 3 && placeLeft > 0) {
      placeQueue.push(id);
      pumpPlaces();
    }
  }

  async function pumpPlaces() {
    if (placeBusy || !placeQueue.length || placeLeft <= 0) {
      return;
    }
    placeBusy = true;
    const id = placeQueue.shift();
    const wait = placeLast + PLACE_GAP_MS - Date.now();
    if (wait > 0) {
      await new Promise(done => setTimeout(done, wait));
    }
    placeLast = Date.now();
    placeLeft -= 1;
    const [lat, lon] = B.middle(id);
    let name = "";
    try {
      const stop = new AbortController();
      const timer = setTimeout(() => stop.abort(), 10000);
      const r = await fetch(NOMINATIM + "?format=jsonv2&zoom=10&accept-language=en&lat=" + lat.toFixed(4) + "&lon=" + lon.toFixed(4),
        { signal: stop.signal }).finally(() => clearTimeout(timer));
      if (r.ok) {
        const j = await r.json();
        const a = plain(j) && plain(j.address) ? j.address : {};
        const where = ["village", "town", "city", "hamlet", "municipality", "county", "state_district", "state", "region"]
          .map(k => cleanName(a[k])).find(Boolean) || cleanName(plain(j) ? j.name : "");
        const country = cleanName(a.country);
        name = cleanName(where && country && where !== country ? where + ", " + country : where || country);
      }
    } catch (e) {
      // no name, the id stays
    }
    places.set(id, name);
    placeBusy = false;
    ui();
    pumpPlaces();
  }

  /* ---------- the map ---------- */

  // the order of all panes is in app.js
  map.createPane("activity").style.zIndex = 425;        // over the built blocks, under the route
  map.createPane("activityMarks").style.zIndex = 445;   // over the frame of the block
  const renderer = L.svg({ pane: "activity", padding: 0.6 });
  const rects = L.layerGroup().addTo(map);
  const marks = L.layerGroup().addTo(map);
  const pulses = L.layerGroup().addTo(map);

  function markIcon(n, hot, building) {
    const count = n > 1 ? '<span class="act-n">' + P.microSvg(String(Math.min(n, 99)), 2) + "</span>" : "";
    return L.divIcon({
      className: "act-mark" + (hot ? " hot" : "") + (building ? " building" : ""),
      iconSize: [22, 22],
      iconAnchor: [11, 11],
      html: '<svg viewBox="0 0 22 22" width="22" height="22" aria-hidden="true"><rect x="4" y="4" width="14" height="14" fill="#fff" stroke="#fff" stroke-width="6"/><rect class="act-box" x="4" y="4" width="14" height="14"/></svg>' + count,
    });
  }

  function draw() {
    drawDue = false;
    rects.clearLayers();
    marks.clearLayers();
    const now = Date.now();
    const list = feed.waiting(P.index(), now);
    // the newest request that still waits stands out
    let hotId = "";
    let hotAt = 0;
    for (const w of list) {
      const a = feed.asked.get(w.id);
      const at = a ? a.last : w.at;
      if (at > hotAt) {
        hotAt = at;
        hotId = w.id;
      }
    }
    const z = map.getZoom();
    const view = map.getBounds().pad(0.3);
    if (z >= RECT_ZOOM) {
      let n = 0;
      for (const w of list) {
        const [s, west, north, e] = B.bounds(w.id);
        if (s < view.getNorth() && north > view.getSouth() && west < view.getEast() && e > view.getWest()) {
          rects.addLayer(L.rectangle([[s, west], [north, e]], {
            pane: "activity", renderer, interactive: false, weight: 2, color: "#000", opacity: 1, fill: true, fillOpacity: 1,
            className: "act-req" + (w.id === hotId ? " hot" : "") + (w.building ? " building" : ""),
          }));
          if (++n >= MAX_RECTS) {
            break;
          }
        }
      }
      return;
    }
    // a mark for each cell of the screen that holds requested blocks, with their count
    const cells = new Map();
    for (const w of list) {
      const ll = B.middle(w.id);
      const p = map.project(ll, z);
      const key = Math.floor(p.x / CELL_PX) + ":" + Math.floor(p.y / CELL_PX);
      const c = cells.get(key) || { ids: [], x: 0, y: 0, hot: false, building: false };
      c.ids.push(w.id);
      c.x += p.x;
      c.y += p.y;
      c.hot = c.hot || w.id === hotId;
      c.building = c.building || w.building;
      cells.set(key, c);
    }
    const wide = map.getBounds().pad(0.5);
    const built = P.builtMarks ? P.builtMarks() : [];
    for (const c of cells.values()) {
      const n = c.ids.length;
      const x = c.x / n;
      let y = c.y / n;
      // a mark that would lie on a plate of built blocks goes just under it, so both stay readable
      const right = n > 1 ? 14 + 2 * (7 * String(Math.min(n, 99)).length - 1) : 11;
      for (let k = 0; k < 4; k++) {
        const hit = built.find(m => x - 11 < m.x + m.hw && x + right > m.x - m.hw && y - 11 < m.y + m.hh && y + 11 > m.y - m.hh);
        if (!hit) {
          break;
        }
        y = hit.y + hit.hh + 12;
      }
      const ll = map.unproject([x, y], z);
      if (!wide.contains(ll)) {
        continue;
      }
      const m = L.marker(ll, {
        pane: "activityMarks", keyboard: true, riseOnHover: true,
        title: n > 1 ? fill(T.markMany, { n }) : fill(T.markOne, { id: c.ids[0] }),
        alt: "", icon: markIcon(n, c.hot, c.building),
      });
      m.on("click", () => {
        if (n === 1) {
          go(c.ids[0]);
        } else {
          const box = L.latLngBounds(c.ids.map(id => {
            const [s, w, north, e] = B.bounds(id);
            return [[s, w], [north, e]];
          }).flat());
          map.fitBounds(box, { maxZoom: RECT_ZOOM + 1, padding: [40, 40], animate: !reduced });
        }
      });
      marks.addLayer(m);
    }
  }

  function redraw() {
    if (!drawDue) {
      drawDue = true;
      requestAnimationFrame(draw);
    }
  }

  map.on("moveend", redraw);
  map.on("click", () => {
    if (isOpen && narrow.matches) {
      setOpen(false);     // on a phone the open plate covers most of the map
    }
  });

  // squares that open out from a block that was just asked for; still, and shorter, without motion
  function pulse(id) {
    const now = Date.now();
    if (now - lastPulse < PULSE_GAP_MS || pulses.getLayers().length >= 3 || document.hidden) {
      return;
    }
    const ll = B.middle(id);
    if (!map.getBounds().pad(0.2).contains(ll)) {
      return;
    }
    lastPulse = now;
    const m = L.marker(ll, {
      pane: "activityMarks", interactive: false, keyboard: false,
      icon: L.divIcon({ className: "act-pulse" + (reduced ? " still" : ""), iconSize: [24, 24], iconAnchor: [12, 12], html: "<i></i><i></i><i></i>" }),
    });
    pulses.addLayer(m);
    setTimeout(() => pulses.removeLayer(m), reduced ? 4000 : PULSE_MS);
  }

  /* ---------- the plate, filled ---------- */

  function label(id) {
    return titleOf(id) || id;
  }

  function row(list, kind, id, right, why) {
    const li = make("li");
    const b = make("button");
    b.type = "button";
    b.className = "act-row k-" + kind;
    const left = make("span", "act-what");
    left.append(make("span", "act-kind", T.kind[kind]));
    const name = titleOf(id);
    left.append(make("span", name ? "act-name" : "act-name mono", name || id));
    if (why) {
      left.append(make("span", "act-why", T.why[why] || ""));
    }
    b.append(left, make("span", "act-when", right));
    b.addEventListener("click", () => go(id));
    li.append(b);
    list.append(li);
  }

  function ui() {
    const now = Date.now();
    const index = P.index();
    const top = feed.latest(now);
    const waiting = feed.waiting(index, now);
    const counts = feed.counts(now);
    const down = !streaming || (!es && tries > 2);
    if (!shown && (index || openedAt || !streaming)) {
      shown = true;
      plate.hidden = false;
      document.body.classList.add("mm-act");
    }
    $("actLive").className = "act-live" + (es && openedAt && es.readyState === 1 ? " on" : "");
    $("actLive").title = es && es.readyState === 1 ? T.live : T.connecting;
    if (top) {
      $("actLine").textContent = fill(T.last, { ago: ago(top.at, now) });
      $("actSub").textContent = label(top.id) + " · " + fill(T.sub, { n: counts.asked, w: waiting.length });
    } else {
      $("actLine").textContent = down ? T.off : T.none;
      $("actSub").textContent = fill(T.subNone, { w: waiting.length });
    }
    if (!isOpen) {
      return;
    }
    $("actAsked").textContent = down && !counts.asked ? "–" : String(counts.asked);
    $("actBuilt").textContent = down && !counts.built ? "–" : String(counts.built);
    $("actWait").textContent = String(waiting.length);
    const list = $("actList");
    list.textContent = "";
    const events = feed.events.filter(ev => ev.at >= now - WINDOW_MS).slice(0, LIST_EVENTS);
    for (const ev of events) {
      row(list, ev.kind, ev.id, ago(ev.at, now), ev.kind === "refused" ? ev.why : "");
    }
    // the saved queue keeps requests older than ntfy does
    const told = new Set(events.map(ev => ev.id));
    for (const w of waiting) {
      if (list.children.length >= LIST_EVENTS) {
        break;
      }
      if (!told.has(w.id)) {
        row(list, "waiting", w.id, ago(w.at, now));
      }
    }
    if (!list.children.length) {
      list.append(make("li", "act-quiet", T.quiet));
    }
    const fresh = $("actNew");
    fresh.textContent = "";
    for (const b of newest(index, LIST_NEW)) {
      row(fresh, "built", b.id, new Date(b.date + "T12:00:00Z").toLocaleDateString("en-GB", { day: "numeric", month: "short" }));
    }
    $("actNewH").hidden = !fresh.children.length;
    // names for the newest few that have none
    for (const ev of events.slice(0, 3)) {
      wantPlace(ev.id);
    }
  }

  let uiDue = false;
  let toldVersion = 0;
  function soon() {
    redraw();
    if (!uiDue) {
      uiDue = true;
      requestAnimationFrame(() => {
        uiDue = false;
        ui();
        if (feed.version !== toldVersion) {
          toldVersion = feed.version;
          document.dispatchEvent(new Event("mm:activity"));   // app.js renders the sheet again
        }
      });
    }
  }

  function flashHead() {
    plate.classList.remove("new");
    void plate.offsetWidth;
    plate.classList.add("new");
    setTimeout(() => plate.classList.remove("new"), 1200);
  }

  /* ---------- the stream ---------- */

  function take(m) {
    const now = Date.now();
    const got = feed.take(m, now);
    if (!got) {
      return;
    }
    const fresh = openedAt && got.at >= openedAt - 15000;
    if (got.ask) {
      if (fresh) {
        pulse(got.ask);
        flashHead();
      }
    } else {
      P.status(got.status, got.at);
      const done = got.status.b.length + Object.keys(got.status.x).length;
      // the builder answered: the index on GitHub Pages has it soon after, read it once then
      if (fresh && done && !reloadDue && now - lastReload > RELOAD_GAP_MS) {
        reloadDue = setTimeout(() => {
          reloadDue = 0;
          lastReload = Date.now();
          P.reload();
        }, RELOAD_AFTER_MS);
      }
    }
    soon();
  }

  function connect() {
    clearTimeout(retry);
    retry = 0;
    if (es) {
      return;
    }
    const since = feed.last || "12h";
    es = new EventSource(STREAM + "/sse?since=" + encodeURIComponent(since));
    es.onopen = () => {
      openedAt = Date.now();
      soon();
    };
    es.onmessage = e => {
      if (typeof e.data !== "string" || e.data.length > MAX_TEXT * 2) {
        return;
      }
      try {
        take(JSON.parse(e.data));
      } catch (err) {
        // not a line of ntfy
      }
    };
    // our own retry, slower each time: a dead ntfy gets no stream of reconnects from this page
    es.onerror = () => {
      const lived = openedAt && Date.now() - openedAt > 60000;
      es.close();
      es = null;
      openedAt = 0;
      tries = lived ? 1 : tries + 1;
      retry = setTimeout(connect, Math.min(600000, 5000 * Math.pow(3, tries - 1)));
      soon();
    };
  }

  // no EventSource: read the last 12 hours once, and again every few minutes while in view
  async function poll() {
    try {
      const since = feed.last || "12h";
      const r = await fetch(STREAM + "/json?poll=1&since=" + encodeURIComponent(since));
      if (r.ok) {
        const text = await r.text();
        for (const line of text.split("\n").slice(0, MAX_SEEN)) {
          if (line && line.length <= MAX_TEXT * 2) {
            try {
              take(JSON.parse(line));
            } catch (e) {
              // skip it
            }
          }
        }
      }
    } catch (e) {
      // ntfy is away, the index still says what waits
    }
    soon();
  }

  document.addEventListener("mm:index", soon);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      if (streaming && !es && retry) {
        connect();      // a phone drops the stream of a tab in the background
      }
      soon();
    }
  });
  let ticks = 0;
  setInterval(() => {
    feed.prune(Date.now());
    ticks += 1;
    if (!document.hidden) {
      soon();           // "5 min ago" moves on, a round that took a block ends
      if (!streaming && ticks % 10 === 0) {
        poll();
      }
    }
  }, 30000);

  // what app.js may ask: whether it carries the status topic, and whether a block was asked for
  root.Activity = { carries: () => streaming, asked: id => feed.has(id, Date.now()) };
  if ("EventSource" in root) {
    streaming = true;
    connect();
    P.relisten();       // app.js closes its own stream on the status topic, if it opened one already
  } else {
    poll();
  }
  soon();
})(this);
