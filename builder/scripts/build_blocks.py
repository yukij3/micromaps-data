#!/usr/bin/env python3
"""Build the map blocks people asked for into a checkout of the data repository (docs/BLOCKS.md).

    python3 scripts/build_blocks.py --poll [--repo .]        what a run has to do; stdlib only
    python3 scripts/build_blocks.py --repo ../micromaps-data --pending /tmp/p.json [id ...]
    python3 scripts/build_blocks.py --repo ../micromaps-data --apply /tmp/p.json --commit abc1234def
    python3 scripts/build_blocks.py --notify /tmp/p.json     tell the page and the phones; stdlib only
    python3 scripts/build_blocks.py --linger 300 --repo .    wait for one more request, print its id

The second form takes the ntfy queue, the queue saved in blocks/index.json, the ids on the
command line and those in $BLOCK_IDS. It writes blocks/<id>/ and lists what it did in the
--pending file, which stays outside the repository. blocks/index.json holds the commit of each
block, so it is written afterwards by the third form (web/workflows/build-blocks.yml, published
to the data repository). --poll prints the ids to build, or "-" when there is nothing to build
but requests to save in the index, or nothing. --linger prints the ids that came while it waited.

Whoever can reach the public topic can write to the queue: a message is only ever compared with
pipeline.blocks.parse(), and it is logged through repr(). What this script sends to ntfy holds
nothing but ids that passed parse(), titles that passed label.ascii_label() and fixed text.
"""
import argparse
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# stdlib only down to here and in these two: --poll runs before any dependency is installed
from pipeline import blocks, fetch  # noqa: E402
from pipeline.geo import M_PER_DEG, Frame, bbox_from_origin  # noqa: E402

NTFY = "https://ntfy.sh/"
QUEUE_TOPIC = "micromaps-blocks-v1"
QUEUE = NTFY + QUEUE_TOPIC + "/json?poll=1&since=13h"
# what the builder says back, docs/BLOCKS.md "Notices": one topic the page listens to, and one
# topic per block for whoever wants a push on the phone
STATUS_TOPIC = "micromaps-status-v1"
BLOCK_TOPIC = "micromaps-%s"
PAGE = "https://yukij3.github.io/micromaps-data/"
INDEX_URL = PAGE + "blocks/index.json"
QUEUE_BYTES = 4 << 20  # the topic is public: more than a day of real requests, then cut
# Postpass answers in seconds and is asked first (fetch.fetch_osm). These are the way round it:
# the main Overpass server, which has today's data, then one mirror. The mirrors were months
# behind in November 2026, and the snapshot date of each block says so.
ENDPOINTS = ["https://overpass-api.de/api/interpreter",
             "https://overpass.private.coffee/api/interpreter"]
OSM_TRIES = 1
OSM_TIMEOUT_S = 200  # the query itself gives up after 180 s
RUN_S = 15 * 60  # no new block after this: a run without Postpass must still end inside its job
PAUSE_S = 2  # between blocks: one request at a time, and not back to back
MAX_FAILS = 2  # in a row: the network is down, the next run tries again
MAX_TRIES = 3  # failed runs of one id in a day
DEM_ZOOM = 12
RUNG_WORKERS = 3  # thinner rungs built side by side once rung 0 does not fit
QUEUE_DAYS = 7  # a request nobody could build is forgotten after this
MAX_QUEUE = 200  # saved requests; the topic is public, the index is not a place to fill up
LIVE_WAIT_S = 300  # for GitHub Pages to serve what was pushed, before anybody is told
WHY = {"dense": "too much map for the watch", "empty": "no map data there"}


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
    the block (the map data servers answer 502 for an outage and for too much data alike), so it
    never becomes a refusal, but a block nobody can serve must not hold the queue up."""
    return index.get("f", {}) if builds_today(index, day) else {}


def saved_queue(index):
    """index["q"] = {id: unix time it was first seen}: requests that outlive the 12 hours ntfy
    keeps a message, oldest first. Read back as hostile as the topic: a hand edit is one too."""
    q = index.get("q")
    if not isinstance(q, dict):
        return []
    return sorted((k for k in q if isinstance(q[k], int)), key=lambda k: (q[k], k))


def waiting(texts, index):
    """The ids somebody is waiting for: valid, once each, neither built nor refused. The saved
    queue first, it has waited longest."""
    out = []
    for text in list(saved_queue(index)) + list(texts):
        try:
            blocks.parse(text)
        except ValueError as ex:
            log("skip %.40r: %s" % (text, ex))
            continue
        if text not in out and text not in index["b"] and text not in index["x"]:
            out.append(text)
    return out


def choose(texts, index, day, max_run, max_day, explicit=()):
    """The ids to build now: waiting, within both budgets. What failed today goes last and rests
    after MAX_TRIES; ids given by hand are always tried."""
    budget = max(0, min(max_run, max_day - builds_today(index, day)))
    tries = failed_today(index, day)
    out = [i for i in waiting(texts, index) if tries.get(i, 0) < MAX_TRIES or i in explicit]
    out.sort(key=lambda i: tries.get(i, 0))
    if len(out) > budget:
        log("budget: %d of %d ids" % (budget, len(out)))
    return out[:budget]


def unsaved(texts, index):
    """Waiting ids the index does not hold yet: reason enough to write it."""
    q = index.get("q") if isinstance(index.get("q"), dict) else {}
    return [i for i in waiting(texts, index) if i not in q]


def snapshot_date(doc):
    """YYYY-MM-DD of the OSM data behind an Overpass answer (or a Postpass one in that shape)."""
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


def run_build(cmd, procs):
    """pipeline.build in a process of its own. procs collects it, so a rung nobody needs any
    more can be stopped."""
    # stdout belongs to the workflow, it reads the answer of this script there
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=sys.stderr)
    procs.append(p)
    if p.wait() != 0:
        raise subprocess.CalledProcessError(p.returncode, cmd)


def fit_rung(bid, lat, lon, size_m, raw, work, light):
    """The first rung of blocks.THIN the block fits on: (rung, (manifest, tiles, cuts), tries),
    rung None if it fits on none. tries = [[rung, bytes]] of the rungs before it. Rung 0 runs
    alone, most blocks fit there; the thinner ones run side by side and the lowest that fits
    wins, so the result is the one a rung after rung build gives."""
    from pipeline import pack
    procs = []
    stop = threading.Event()

    def one(rung):
        if stop.is_set():
            return None
        out = os.path.join(work, "%s.%d" % (bid, rung))
        run_build([sys.executable, "-m", "pipeline.build", "--origin=%.6f,%.6f" % (lat, lon),
                   "--size", str(size_m), "--out", out, "--name", bid, "--raw", raw, "--no-bench",
                   "--thin", str(rung)] + light, procs)
        try:
            return measure(out), 0
        except pack.OverLimit as ex:
            log("%s rung %d: %s" % (bid, rung, ex))
            return None, sum(os.path.getsize(os.path.join(out, n)) for n in ("manifest.bin", "tiles.bin"))

    tries = []
    with concurrent.futures.ThreadPoolExecutor(RUNG_WORKERS) as pool:
        try:
            rungs = [pool.submit(one, 0)]
            if rungs[0].result()[0] is None:
                rungs += [pool.submit(one, r) for r in range(1, len(blocks.THIN))]
            for rung, job in enumerate(rungs):
                fit, size = job.result()
                if fit is not None:
                    return rung, fit, tries
                tries.append([rung, size])
            return None, None, tries
        finally:
            # the rungs past the answer, and all of them after a failure
            stop.set()
            for p in procs:
                if p.poll() is None:
                    p.terminate()


def build_block(bid, repo, work, raw):
    """Builds one block into repo. Returns ("b", stats) or ("x", reason, stats)."""
    from pipeline import pack
    from pipeline.osm import load_osm
    t0 = time.time()
    lat, lon, size_m = blocks.origin(bid)
    bbox = bbox_from_origin(lat, lon, size_m, size_m)
    light = []
    try:
        path = fetch.fetch_osm(bbox, raw, endpoints=ENDPOINTS, tries=OSM_TRIES, timeout=OSM_TIMEOUT_S,
                               postpass=True)
    except RuntimeError as ex:
        log("%s: %s, trying the light query" % (bid, ex))
        path = fetch.fetch_osm(bbox, raw, endpoints=ENDPOINTS, tries=OSM_TRIES, timeout=OSM_TIMEOUT_S,
                               light=True, postpass=True)
        light = ["--light"]
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    date = snapshot_date(doc)
    source = "postpass" if doc.get("generator") == "Postpass" else "overpass"
    del doc
    frame = Frame(lat, lon)
    feats = load_osm(path, frame)
    stats = {"id": bid, "date": date, "tries": [], "light": bool(light), "source": source,
             "fetch_s": round(time.time() - t0, 1)}
    if not feats and under_sea(bbox, frame, size_m, raw):
        stats["seconds"] = round(time.time() - t0, 1)
        return "x", "empty", stats
    title = title_of(feats, size_m, lat, lon)
    del feats
    # before the rungs start: they share the cache, and the first one would fetch it for all
    fetch.fetch_dem(bbox, raw, DEM_ZOOM)
    rung, fit, stats["tries"] = fit_rung(bid, lat, lon, size_m, raw, work, light)
    if fit is None:
        stats["seconds"] = round(time.time() - t0, 1)
        return "x", "dense", stats
    manifest, tiles, cuts = fit
    terrain = ", ".join(fetch.dem_sources(bbox, raw, DEM_ZOOM))
    row = pack.write_block(manifest, tiles, cuts, repo, bid, title, date, terrain)
    stats.update(title=title, kb=row["k"][0], bytes=len(manifest) + len(tiles), rung=rung,
                 manifest=len(manifest), terrain=terrain, tile_max=max(s[5] for s in pack.tile_slots(
                     pack.verify(manifest, tiles)[0])),
                 groups={c[0]: len(c[2]) for c in cuts},
                 group_max={c[0]: max(b - a for (a, b) in c[2]) for c in cuts},
                 seconds=round(time.time() - t0, 1))
    return "b", stats


def run(repo, ids, raw, work, pause=PAUSE_S):
    """Builds the ids in order and returns the pending record. A failed id is tried again while
    it is in the queue, see failed_today()."""
    pending = {"b": {}, "x": {}, "f": [], "stats": []}
    fails = 0
    t0 = time.time()
    for i, bid in enumerate(ids):
        if time.time() - t0 > RUN_S:
            log("stopping: out of time, %d ids wait for the next run" % (len(ids) - i))
            break
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


def apply_pending(index, pending, commit, day, now=None):
    """Index with the pending blocks in. commit: the data repository commit that added them."""
    now = int(time.time()) if now is None else now
    tries = dict(failed_today(index, day))
    for bid in pending.get("f", ()):
        tries[bid] = tries.get(bid, 0) + 1
    index["b"].update({k: v + [commit] for k, v in pending["b"].items()})
    index["x"].update(pending["x"])
    for bid in list(pending["b"]) + list(pending["x"]):
        tries.pop(bid, None)
    index["f"] = tries
    # a failed try cost the same minutes and the same requests as a build
    index["n"] = [day, builds_today(index, day) + len(pending["b"]) + len(pending["x"]) + len(pending.get("f", ()))]
    # the saved queue: what waited before and what this run saw, less what has an answer now
    # and what nobody could build in QUEUE_DAYS
    old = index.get("q") if isinstance(index.get("q"), dict) else {}
    q = {}
    for bid in saved_queue(index) + list(pending.get("q", ())):
        try:
            blocks.parse(bid)
        except ValueError:
            continue
        seen = old.get(bid, now)
        if bid not in index["b"] and bid not in index["x"] and now - seen < QUEUE_DAYS * 86400 and len(q) < MAX_QUEUE:
            q.setdefault(bid, seen)
    index["q"] = q
    return index


# ---- notices: what the builder says back (docs/BLOCKS.md). Best effort, a notice that does not
# go out costs nothing but the wait: the page asks for the index by itself as well.

def say(topic, message, **more):
    """One ntfy message as JSON: no header carries anything that came from outside."""
    body = json.dumps(dict(more, topic=topic, message=message)).encode()
    req = urllib.request.Request(NTFY, data=body, headers={"User-Agent": fetch.UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        return True
    except (OSError, ValueError) as ex:
        log("notice to %s: %s" % (topic, ex))
        return False


def status(**event):
    """For the pages that are open: {"v": 1, "w": [ids]} when a build starts, {"v": 1, "b": [ids],
    "x": {id: reason}, "f": [ids]} when its results are published. A hint, never a fact: anyone
    can write to the topic, and the page only takes it as a reason to read the index again."""
    return say(STATUS_TOPIC, json.dumps(dict(event, v=1), separators=(",", ":"), sort_keys=True))


def is_live(pending, url=INDEX_URL):
    """Does the published index hold every answer of this run?"""
    req = urllib.request.Request("%s?t=%d" % (url, time.time()), headers={"User-Agent": fetch.UA,
                                                                       "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            index = json.loads(r.read())
        return all(k in index.get("b", {}) for k in pending["b"]) and all(k in index.get("x", {}) for k in pending["x"])
    except (OSError, ValueError, AttributeError):
        return False


def notify(pending, wait_s=LIVE_WAIT_S, pause=6):
    """Tell the page and the phones what a run did, once GitHub Pages serves it: the link the
    page hands out points there."""
    if not (pending["b"] or pending["x"] or pending.get("f")):
        return
    end = time.time() + wait_s
    while (pending["b"] or pending["x"]) and not is_live(pending):
        if time.time() >= end:
            log("pages: not live after %d s, telling anyway" % wait_s)
            break
        time.sleep(pause)
    status(b=sorted(pending["b"]), x=pending["x"], f=list(pending.get("f", ())))
    for bid, row in sorted(pending["b"].items()):
        say(BLOCK_TOPIC % bid, "Block %s (%s, %d KB) is ready. The link for the watch is on the microMAPS page."
            % (bid, row[0], row[1]), title="microMAPS", tags=["white_check_mark"], click=PAGE + "#" + bid)
    for bid, why in sorted(pending["x"].items()):
        say(BLOCK_TOPIC % bid, "Block %s could not be built: %s." % (bid, WHY.get(why, "it does not fit the watch")),
            title="microMAPS", tags=["x"], click=PAGE + "#" + bid)


def linger(seconds, index, url=None):
    """Wait up to `seconds` for a request the index has no answer for. People ask for blocks in
    twos and threes, and the next scheduled run may be an hour away. True if one came."""
    end = time.time() + seconds
    since = int(time.time())
    url = url or NTFY + QUEUE_TOPIC + "/json?since=%d"
    while time.time() < end:
        try:
            req = urllib.request.Request(url % since, headers={"User-Agent": fetch.UA})
            # the stream sends a keepalive line every 45 s
            with urllib.request.urlopen(req, timeout=70) as r:
                while time.time() < end:
                    line = r.readline(65536)
                    if not line:
                        break
                    for text in parse_queue(line):
                        if waiting([text], dict(index, q={})):
                            return True
        except (OSError, ValueError) as ex:
            log("queue stream: %s" % ex)
        time.sleep(min(15, max(0, end - time.time())))
    return False


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ids", nargs="*", help="block ids, on top of the queue")
    ap.add_argument("--repo", help="checkout of the data repository")
    ap.add_argument("--poll", action="store_true", help="print the ids waiting and stop")
    ap.add_argument("--pending", help="file for what a build did")
    ap.add_argument("--apply", metavar="PENDING", help="write a --pending file into blocks/index.json")
    ap.add_argument("--commit", help="short commit of the packs, with --apply")
    ap.add_argument("--notify", metavar="PENDING", help="send the notices of a --pending file")
    ap.add_argument("--linger", type=int, metavar="SECONDS", help="wait for a new request, then as --poll")
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
    if a.notify:
        with open(a.notify, encoding="utf-8") as f:
            notify(json.load(f))
        return
    poll = a.poll or a.linger is not None
    if not poll and not (a.repo and a.pending):
        ap.error("--repo and --pending required")

    explicit = list(a.ids) + re.split(r"[\s,]+", os.environ.get("BLOCK_IDS", "").strip())
    explicit = [t for t in explicit if t]
    index = load_index(a.repo) if a.repo else fetch_index()
    texts = explicit + (read_queue() if not a.no_queue else [])
    ids = choose(texts, index, day, a.max_run, a.max_day, explicit)
    if a.linger is not None:
        # what failed a minute ago waits for the next scheduled run, not for the next round of this one
        again = failed_today(index, day)
        ids = [i for i in ids if i not in again]
        if not ids and linger(a.linger, index):
            ids = [i for i in choose(read_queue(), index, day, a.max_run, a.max_day) if i not in again]
        print(",".join(ids))
        return
    if poll:
        print(",".join(ids) if ids else "-" if unsaved(texts, index) else "")
        return
    if ids:
        status(w=ids)
    if a.work:
        pending = run(a.repo, ids, a.raw, a.work)
    else:
        with tempfile.TemporaryDirectory() as work:
            pending = run(a.repo, ids, a.raw, work)
    pending["q"] = waiting(texts, index)
    with open(a.pending, "w", encoding="utf-8") as f:
        json.dump(pending, f)


if __name__ == "__main__":
    main()
