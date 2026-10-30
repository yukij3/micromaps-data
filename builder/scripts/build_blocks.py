#!/usr/bin/env python3
"""Build the map blocks people asked for into a checkout of the data repository (docs/BLOCKS.md).

    python3 scripts/build_blocks.py --poll [--repo .]        ids waiting, comma separated; stdlib only
    python3 scripts/build_blocks.py --repo ../micromaps-data --pending /tmp/p.json [id ...]
    python3 scripts/build_blocks.py --repo ../micromaps-data --apply /tmp/p.json --commit abc1234def

The second form takes the ntfy queue, the ids on the command line and those in $BLOCK_IDS. It
writes blocks/<id>/ and lists what it did in the --pending file, which stays outside the
repository. blocks/index.json holds the commit of each block, so it is written afterwards by
the third form (web/workflows/build-blocks.yml, published to the data repository).

Whoever can reach the public topic can write to the queue: a message is only ever compared with
pipeline.blocks.parse(), and it is logged through repr().
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# stdlib only down to here and in these two: --poll runs before any dependency is installed
from pipeline import blocks, fetch  # noqa: E402
from pipeline.geo import M_PER_DEG, Frame, bbox_from_origin  # noqa: E402

QUEUE = "https://ntfy.sh/micromaps-blocks-v1/json?poll=1&since=13h"
INDEX_URL = "https://yukij3.github.io/micromaps-data/blocks/index.json"
QUEUE_BYTES = 4 << 20  # the topic is public: more than a day of real requests, then cut
# overpass-api.de asks for little traffic: it carries the rest of the pipeline, not this job
ENDPOINTS = ["https://overpass.kumi.systems/api/interpreter",
             "https://overpass.private.coffee/api/interpreter",
             "https://overpass-api.de/api/interpreter"]
OSM_TRIES = 1
OSM_TIMEOUT_S = 300  # kumi took 225 s for 1.2 MB: waiting costs them less than asking again
PAUSE_S = 10  # between Overpass requests
MAX_FAILS = 2  # in a row: the network is down, the next run tries again
MAX_TRIES = 3  # failed runs of one id in a day
DEM_ZOOM = 12


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def today():
    return datetime.datetime.now(datetime.timezone.utc).date().isoformat()


def read_queue(url=QUEUE):
    """Raw message texts of the topic, oldest first. A queue that does not answer is an empty one."""
    req = urllib.request.Request(url, headers={"User-Agent": fetch.UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read(QUEUE_BYTES)
    except (OSError, ValueError) as ex:
        log("queue: %s" % ex)
        return []
    return parse_queue(body)


def parse_queue(body):
    out = []
    for line in body.split(b"\n"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("event") == "message" and isinstance(ev.get("message"), str):
            out.append(ev["message"])
    return out


def new_index():
    return {"v": 1, "b": {}, "x": {}}


def fetch_index(url=INDEX_URL):
    """The published index, for --poll. The build reads its own checkout, so a stale or missing
    answer costs one clone at most."""
    req = urllib.request.Request(url, headers={"User-Agent": fetch.UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except (OSError, ValueError) as ex:
        log("index: %s" % ex)
        return new_index()


def index_path(repo):
    return os.path.join(repo, "blocks", "index.json")


def load_index(repo):
    path = index_path(repo)
    if not os.path.exists(path):
        return new_index()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_index(repo, index):
    path = index_path(repo)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=0, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def builds_today(index, day):
    """index["n"] = [day, blocks tried that day]: the daily budget survives between runs."""
    n = index.get("n")
    return n[1] if isinstance(n, list) and len(n) == 2 and n[0] == day else 0


def failed_today(index, day):
    """index["f"] = {id: failed tries}, for the day in index["n"]. A failure says nothing about
    the block (Overpass answers 502 for an outage and for too much data alike), so it never
    becomes a refusal, but a block no mirror can serve must not hold the queue up for 13 hours."""
    return index.get("f", {}) if builds_today(index, day) else {}


def choose(texts, index, day, max_run, max_day, explicit=()):
    """The ids to build now: valid, once each, not known yet, within both budgets. What failed
    today goes last and rests after MAX_TRIES; ids given by hand are always tried."""
    budget = max(0, min(max_run, max_day - builds_today(index, day)))
    tries = failed_today(index, day)
    out = []
    for text in texts:
        try:
            blocks.parse(text)
        except ValueError as ex:
            log("skip %.40r: %s" % (text, ex))
            continue
        if text in out or text in index["b"] or text in index["x"]:
            continue
        if tries.get(text, 0) < MAX_TRIES or text in explicit:
            out.append(text)
    out.sort(key=lambda i: tries.get(i, 0))
    if len(out) > budget:
        log("budget: %d of %d ids" % (budget, len(out)))
    return out[:budget]


def snapshot_date(doc):
    """YYYY-MM-DD of the OSM data behind an Overpass answer."""
    base = doc["osm3s"]["timestamp_osm_base"]
    if not re.match(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T", base):
        raise ValueError("snapshot date %.40r" % base)
    return base[:10]


def corner_name(lat, lon):
    return "%s%02d%s%03d" % ("N" if lat >= 0 else "S", abs(lat), "E" if lon >= 0 else "W", abs(lon))


def title_of(feats, size_m, lat, lon):
    """The named place nearest to the middle of the block, else its coordinates."""
    from pipeline.label import ascii_label
    from pipeline.osm import POINT
    best = None
    for f in feats:
        if f.kind == POINT and f.cls == 27 and f.name:
            x, y = f.pts[0]
            label = ascii_label(f.name, 10)
            if label and 0 <= x < size_m and 0 <= y < size_m:
                d = (x - size_m / 2) ** 2 + (y - size_m / 2) ** 2
                if best is None or (d, label) < best:
                    best = (d, label)
    if best:
        return best[1]
    half = size_m / 2
    return corner_name(lat + half / M_PER_DEG, lon + half / Frame(lat, lon).kx)


def under_sea(bbox, frame, size_m, raw):
    """True if the DEM has no land in the block. Terrarium holds the sea floor, so relief alone
    says nothing, and land without OSM is still a map."""
    from pipeline.contours import DemGrid
    trange = fetch.fetch_dem(bbox, raw, DEM_ZOOM)
    return float(DemGrid(raw, DEM_ZOOM, trange, frame, 0, 0, size_m, size_m, 50.0, 0).z.max()) <= 0


def measure(out):
    """(manifest, tiles, cuts) of a built package, OverLimit if it does not fit."""
    from pipeline import pack
    manifest, tiles = pack.read_package(out)
    if len(manifest) + len(tiles) > blocks.MAX_BYTES:
        raise pack.OverLimit("%d B is over the block limit" % (len(manifest) + len(tiles)))
    pkg, slots = pack.verify(manifest, tiles)
    return manifest, tiles, pack.cut(slots, tiles)


def build_block(bid, repo, work, raw):
    """Builds one block into repo. Returns ("b", stats) or ("x", reason, stats)."""
    from pipeline import pack
    from pipeline.osm import load_osm
    t0 = time.time()
    lat, lon, size_m = blocks.origin(bid)
    bbox = bbox_from_origin(lat, lon, size_m, size_m)
    light = []
    try:
        path = fetch.fetch_osm(bbox, raw, endpoints=ENDPOINTS, tries=OSM_TRIES, timeout=OSM_TIMEOUT_S)
    except RuntimeError as ex:
        log("%s: %s, trying the light query" % (bid, ex))
        path = fetch.fetch_osm(bbox, raw, endpoints=ENDPOINTS, tries=OSM_TRIES, timeout=OSM_TIMEOUT_S, light=True)
        light = ["--light"]
    with open(path, encoding="utf-8") as f:
        date = snapshot_date(json.load(f))
    frame = Frame(lat, lon)
    feats = load_osm(path, frame)
    stats = {"id": bid, "date": date, "tries": [], "light": bool(light)}
    if not feats and under_sea(bbox, frame, size_m, raw):
        stats["seconds"] = round(time.time() - t0, 1)
        return "x", "empty", stats
    title = title_of(feats, size_m, lat, lon)
    del feats
    out = os.path.join(work, bid)
    for rung in range(len(blocks.THIN)):
        subprocess.run([sys.executable, "-m", "pipeline.build", "--origin=%.6f,%.6f" % (lat, lon),
                        "--size", str(size_m), "--out", out, "--name", bid, "--raw", raw, "--no-bench",
                        "--thin", str(rung)] + light, cwd=ROOT, check=True)
        try:
            manifest, tiles, cuts = measure(out)
        except pack.OverLimit as ex:
            log("%s rung %d: %s" % (bid, rung, ex))
            size = sum(os.path.getsize(os.path.join(out, n)) for n in ("manifest.bin", "tiles.bin"))
            stats["tries"].append([rung, size])
            continue
        terrain = ", ".join(fetch.dem_sources(bbox, raw, DEM_ZOOM))
        row = pack.write_block(manifest, tiles, cuts, repo, bid, title, date, terrain)
        stats.update(title=title, kb=row["k"][0], bytes=len(manifest) + len(tiles), rung=rung,
                     manifest=len(manifest), terrain=terrain, tile_max=max(s[5] for s in pack.tile_slots(
                         pack.verify(manifest, tiles)[0])),
                     groups={c[0]: len(c[2]) for c in cuts},
                     group_max={c[0]: max(b - a for (a, b) in c[2]) for c in cuts},
                     seconds=round(time.time() - t0, 1))
        return "b", stats
    stats["seconds"] = round(time.time() - t0, 1)
    return "x", "dense", stats


def run(repo, ids, raw, work, pause=PAUSE_S):
    """Builds the ids in order and returns the pending record. A failed id is tried again while
    it is in the queue, see failed_today()."""
    pending = {"b": {}, "x": {}, "f": [], "stats": []}
    fails = 0
    for i, bid in enumerate(ids):
        try:
            res = build_block(bid, repo, work, raw)
        except (Exception, SystemExit) as ex:  # noqa: BLE001 - one block must not cost the others
            log("%s failed: %s" % (bid, ex))
            # the id is not in the index, so nothing here is published yet: git add must not find it
            shutil.rmtree(os.path.join(repo, "blocks", bid), ignore_errors=True)
            pending["f"].append(bid)
            fails += 1
            if fails >= MAX_FAILS:
                log("stopping: %d failures in a row" % fails)
                break
            continue
        fails = 0
        stats = res[-1]
        pending["stats"].append(stats)
        if res[0] == "b":
            pending["b"][bid] = [stats["title"], stats["kb"], stats["date"]]
        else:
            pending["x"][bid] = res[1]
        log(json.dumps(stats))
        if i + 1 < len(ids):
            time.sleep(pause)
    return pending


def apply_pending(index, pending, commit, day):
    """Index with the pending blocks in. commit: the data repository commit that added them."""
    tries = dict(failed_today(index, day))
    for bid in pending.get("f", ()):
        tries[bid] = tries.get(bid, 0) + 1
    index["b"].update({k: v + [commit] for k, v in pending["b"].items()})
    index["x"].update(pending["x"])
    for bid in list(pending["b"]) + list(pending["x"]):
        tries.pop(bid, None)
    index["f"] = tries
    # a failed try cost the same minutes and the same Overpass request as a build
    index["n"] = [day, builds_today(index, day) + len(pending["b"]) + len(pending["x"]) + len(pending.get("f", ()))]
    return index


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="block ids, on top of the queue")
    ap.add_argument("--repo", help="checkout of the data repository")
    ap.add_argument("--poll", action="store_true", help="print the ids waiting and stop")
    ap.add_argument("--pending", help="file for what a build did")
    ap.add_argument("--apply", metavar="PENDING", help="write a --pending file into blocks/index.json")
    ap.add_argument("--commit", help="short commit of the packs, with --apply")
    ap.add_argument("--no-queue", action="store_true", help="only the ids given")
    ap.add_argument("--max-run", type=int, default=6, help="blocks per run")
    ap.add_argument("--max-day", type=int, default=40, help="blocks per UTC day")
    ap.add_argument("--raw", default=os.path.join(ROOT, "data", "raw"), help="download cache")
    ap.add_argument("--work", help="directory for the built packages (default: temporary)")
    a = ap.parse_args(argv)

    day = today()
    if a.apply:
        if not (a.repo and a.commit and re.fullmatch(r"[0-9a-f]{7,40}", a.commit)):
            ap.error("--apply needs --repo and --commit")
        with open(a.apply, encoding="utf-8") as f:
            pending = json.load(f)
        save_index(a.repo, apply_pending(load_index(a.repo), pending, a.commit, day))
        return
    if not a.poll and not (a.repo and a.pending):
        ap.error("--repo and --pending required")

    explicit = list(a.ids) + re.split(r"[\s,]+", os.environ.get("BLOCK_IDS", "").strip())
    explicit = [t for t in explicit if t]
    texts = explicit + (read_queue() if not a.no_queue else [])
    index = load_index(a.repo) if a.repo else fetch_index()
    ids = choose(texts, index, day, a.max_run, a.max_day, explicit)
    if a.poll:
        print(",".join(ids))
        return
    if a.work:
        pending = run(a.repo, ids, a.raw, a.work)
    else:
        with tempfile.TemporaryDirectory() as work:
            pending = run(a.repo, ids, a.raw, work)
    with open(a.pending, "w", encoding="utf-8") as f:
        json.dump(pending, f)


if __name__ == "__main__":
    main()
