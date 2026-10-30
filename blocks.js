// The grid of map blocks (docs/BLOCKS.md). pipeline/blocks.py holds the same numbers, the tests
// compare both with pipeline/tests/data/block_grid.json. Node loads this file too.
// In a function: route.js shares the page and has its own M_PER_DEG.
"use strict";

(function (root) {
  const M_PER_DEG = 111319.49;
  const PITCH_M = 5000;
  const SIZES = [10, 20];
  const MAX_LAT = 80;
  const DLAT = PITCH_M / M_PER_DEG;
  const ID_RE = /^(10|20)-[0-9]{1,5}-[0-9]{1,5}$/;
  // the links go into the watch settings, so they are absolute
  const PAGES = "https://yukij3.github.io/micromaps-data/blocks/";
  const CDN = "https://cdn.jsdelivr.net/gh/yukij3/micromaps-data@";

  const radians = d => d * Math.PI / 180;
  const round6 = v => Number(v.toFixed(6));

  const lat0 = r => round6(r * DLAT - 90);
  const dlon = r => PITCH_M / (M_PER_DEG * Math.cos(radians(lat0(r))));
  const lon0 = (r, c) => round6(c * dlon(r) - 180);
  const blockId = (size, r, c) => size + "-" + r + "-" + c;

  // id of the block with this point in its south-west pitch cell
  function snap(lat, lon, size) {
    const r = Math.floor((lat + 90) / DLAT);
    return blockId(size, r, Math.floor((lon + 180) / dlon(r)));
  }

  // id of the block closest to having this point in its centre
  function centred(lat, lon, size) {
    const half = size * 500;
    const south = lat - half / M_PER_DEG;
    const r = Math.floor((south + 90) / DLAT);
    return snap(south, lon - half / (M_PER_DEG * Math.cos(radians(lat0(r)))), size);
  }

  // The block whose middle is nearest to this point, for the frame on the page. centred() floors,
  // so its block can end with the point on its edge; half a pitch more turns the floor into a round.
  function nearest(lat, lon, size) {
    const half = PITCH_M / 2 / M_PER_DEG;
    return centred(lat + half, lon + half / Math.cos(radians(lat)), size);
  }

  function fits(size, r, c) {
    if (!SIZES.includes(size) || r < 0 || c < 0) {
      return false;
    }
    if (Math.abs(lat0(r)) > MAX_LAT) {
      return false;
    }
    return lon0(r, c) + dlon(r) * size * 1000 / PITCH_M <= 180;
  }

  function split(id) {
    const [size, r, c] = id.split("-").map(Number);
    return { size, r, c };
  }

  // {size, r, c} of a good id, null for anything else: the id comes from a URL or the search box
  function parse(text) {
    if (typeof text !== "string" || !ID_RE.test(text)) {
      return null;
    }
    const b = split(text);
    if (blockId(b.size, b.r, b.c) !== text || !fits(b.size, b.r, b.c)) {
      return null;
    }
    return b;
  }

  // [south, west, north, east] in the frame of pipeline/geo.py
  function bounds(id) {
    const { size, r, c } = split(id);
    const s = lat0(r);
    const w = lon0(r, c);
    const m = size * 1000;
    return [s, w, s + m / M_PER_DEG, w + m / (M_PER_DEG * Math.cos(radians(s)))];
  }

  function middle(id) {
    const { size, r, c } = split(id);
    const s = lat0(r);
    const m = size * 500;
    return [s + m / M_PER_DEG, lon0(r, c) + m / (M_PER_DEG * Math.cos(radians(s)))];
  }

  const mainLink = id => PAGES + id;
  const secondLink = (id, commit) => CDN + commit + "/blocks/" + id;

  const api = { M_PER_DEG, PITCH_M, SIZES, MAX_LAT, DLAT, lat0, dlon, lon0, blockId, snap, centred, nearest, fits,
    parse, split, bounds, middle, mainLink, secondLink };
  if (typeof module !== "undefined") {
    module.exports = api;
  } else {
    root.Blocks = api;
  }
})(this);
