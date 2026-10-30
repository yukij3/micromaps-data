// GPX -> route code for microMAPS (docs/FORMAT.md 9.5). Runs in the browser only: the file is
// read here and nothing is sent anywhere. Node loads this file too, for the tests.
"use strict";

const MAX_POINTS = 300;      // the watch accepts 340, Mmap.ROUTE_MAX_PTS
const MAX_CODE = 2000;       // iOS settings field, ~2048 reported
const MAX_WAYPOINTS = 10;
const UNIT_M = 4;
const M_PER_DEG = 111319.49;

const CYR = {а:"a",б:"b",в:"v",г:"g",д:"d",е:"e",ё:"e",ж:"zh",з:"z",и:"i",й:"y",к:"k",л:"l",м:"m",н:"n",
  о:"o",п:"p",р:"r",с:"s",т:"t",у:"u",ф:"f",х:"kh",ц:"ts",ч:"ch",ш:"sh",щ:"sch",ъ:"",ы:"y",ь:"",э:"e",ю:"yu",я:"ya"};

// the watch font is ASCII only
function asciiName(s, max) {
  let out = "";
  for (const ch of (s || "").normalize("NFKD")) {
    const low = ch.toLowerCase();
    if (low in CYR) {
      const t = CYR[low];
      out += ch === low ? t : t.charAt(0).toUpperCase() + t.slice(1);
    } else if (ch >= " " && ch <= "~") {
      out += ch;
    }
  }
  return out.trim().slice(0, max);
}

function toXY(lat, lon, lat0, lon0) {
  return [(lon - lon0) * Math.cos(lat0 * Math.PI / 180) * M_PER_DEG, (lat - lat0) * M_PER_DEG];
}

function lengthM(pts) {
  let sum = 0;
  for (let i = 1; i < pts.length; i++) {
    sum += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  }
  return sum;
}

// Ramer-Douglas-Peucker on an explicit stack: a 50 000 point track must not overflow the call stack
function simplify(pts, eps) {
  const keep = new Uint8Array(pts.length);
  keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    const [ax, ay] = pts[a];
    const dx = pts[b][0] - ax;
    const dy = pts[b][1] - ay;
    const l2 = dx * dx + dy * dy;
    let best = -1;
    let bestD = eps * eps;
    for (let i = a + 1; i < b; i++) {
      const px = pts[i][0] - ax;
      const py = pts[i][1] - ay;
      let d;
      if (l2 === 0) {
        d = px * px + py * py;
      } else {
        const t = Math.max(0, Math.min(1, (px * dx + py * dy) / l2));
        const qx = px - t * dx;
        const qy = py - t * dy;
        d = qx * qx + qy * qy;
      }
      if (d > bestD) {
        bestD = d;
        best = i;
      }
    }
    if (best >= 0) {
      keep[best] = 1;
      stack.push([a, best], [best, b]);
    }
  }
  const out = [];
  for (let i = 0; i < pts.length; i++) {
    if (keep[i]) {
      out.push(pts[i]);
    }
  }
  return out;
}

function pushVarint(out, v) {
  while (v >= 0x80) {
    out.push((v & 0x7f) | 0x80);
    v = Math.floor(v / 128);
  }
  out.push(v);
}

function pushZz(out, v) {
  pushVarint(out, v >= 0 ? 2 * v : -2 * v - 1);
}

function pushI32(out, v) {
  for (let i = 0; i < 4; i++) {
    out.push((v >>> (8 * i)) & 0xff);
  }
}

function pushStr(out, s) {
  out.push(s.length);
  for (let i = 0; i < s.length; i++) {
    out.push(s.charCodeAt(i));
  }
}

function blob(pts, wpts, name, lat0e7, lon0e7) {
  const out = [2];
  pushI32(out, lat0e7);
  pushI32(out, lon0e7);
  pushStr(out, name);
  // deltas of the rounded units: rounding errors do not add up along the route
  let units = pts.map(p => [Math.round(p[0] / UNIT_M), Math.round(p[1] / UNIT_M)]);
  units = units.filter((u, i) => i === 0 || i === units.length - 1 || u[0] !== units[i - 1][0] || u[1] !== units[i - 1][1]);
  pushVarint(out, units.length);
  let cx = 0;
  let cy = 0;
  for (const [x, y] of units) {
    pushZz(out, x - cx);
    pushZz(out, y - cy);
    cx = x;
    cy = y;
  }
  pushVarint(out, wpts.length);
  cx = 0;
  cy = 0;
  for (const w of wpts) {
    const x = Math.round(w.x / UNIT_M);
    const y = Math.round(w.y / UNIT_M);
    pushZz(out, x - cx);
    pushZz(out, y - cy);
    pushStr(out, w.name);
    cx = x;
    cy = y;
  }
  return { bytes: Uint8Array.from(out), points: units.length };
}

function base64(bytes) {
  let s = "";
  for (let i = 0; i < bytes.length; i++) {
    s += String.fromCharCode(bytes[i]);
  }
  return btoa(s);
}

// track: [[lat, lon], ...]; wpts: [{lat, lon, name}, ...]. Tightens the simplification until the
// code fits both limits, so a track of any length gives a code, at some accuracy.
async function makeRoute(track, wpts, name) {
  if (track.length < 2) {
    throw new Error("The file has no track or route points.");
  }
  const lat0e7 = Math.round(track[0][0] * 1e7);
  const lon0e7 = Math.round(track[0][1] * 1e7);
  const lat0 = lat0e7 / 1e7;
  const lon0 = lon0e7 / 1e7;
  const xy = track.map(p => toXY(p[0], p[1], lat0, lon0));
  const way = wpts.slice(0, MAX_WAYPOINTS).map(w => {
    const [x, y] = toXY(w.lat, w.lon, lat0, lon0);
    return { x, y, name: asciiName(w.name, 16) };
  });
  const title = asciiName(name, 24) || "Route";
  for (let eps = 2; ; eps *= 1.15) {
    const pts = simplify(xy, eps);
    const b = blob(pts, way, title, lat0e7, lon0e7);
    if (b.points <= MAX_POINTS && (b.bytes.length + 4) * 4 / 3 + 4 <= MAX_CODE) {
      const sha = new Uint8Array(await crypto.subtle.digest("SHA-1", b.bytes));
      const all = new Uint8Array(b.bytes.length + 4);
      all.set(b.bytes);
      all.set(sha.subarray(0, 4), b.bytes.length);
      return { code: base64(all), points: b.points, epsM: Math.round(eps), km: lengthM(xy) / 1000, name: title };
    }
    if (eps > 5000) {
      throw new Error("This track does not fit a route code.");
    }
  }
}

// browser: DOMParser. Not every exporter uses the same namespace, so names are matched bare.
function parseGpx(text) {
  const doc = new DOMParser().parseFromString(text, "application/xml");
  if (doc.getElementsByTagName("parsererror").length) {
    throw new Error("This is not a valid GPX file.");
  }
  const point = e => [parseFloat(e.getAttribute("lat")), parseFloat(e.getAttribute("lon"))];
  const valid = p => Number.isFinite(p[0]) && Number.isFinite(p[1]);
  let track = Array.from(doc.getElementsByTagName("trkpt")).map(point).filter(valid);
  if (track.length < 2) {
    track = Array.from(doc.getElementsByTagName("rtept")).map(point).filter(valid);
  }
  const child = (e, tag) => {
    const c = e.getElementsByTagName(tag)[0];
    return c ? c.textContent : "";
  };
  const wpts = Array.from(doc.getElementsByTagName("wpt")).map(e => {
    const p = point(e);
    return { lat: p[0], lon: p[1], name: child(e, "name") };
  }).filter(w => valid([w.lat, w.lon]));
  const first = doc.getElementsByTagName("trk")[0] || doc.getElementsByTagName("rte")[0];
  return { track, wpts, name: first ? child(first, "name") : "" };
}

if (typeof module !== "undefined") {
  module.exports = { makeRoute, simplify, asciiName };
}
