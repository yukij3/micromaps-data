"""Publish an MMAP package as a downloadable pack (docs/FORMAT.md section 9).

    python3 -m pipeline.pack out/lovozero --repo ../micromaps-data --id lovozero \
        --title Lovozero --date 2026-10-30 --terrain "ArcticDEM, GMTED2010"

Writes into <repo>/packs/<id>/<date>/:
  manifest.bin, tiles.bin        the machine-readable database (ODbL 4.6)
  4k/pack.json, 4k/g000.json...  what the watch downloads, groups up to 4 KB
  2k/...                         the same pack cut into 2 KB groups (fallback after -402/-403)
and adds the pack to <repo>/catalog.json. A published directory never changes: a new snapshot
is a new <date>, so a download that resumes can not mix two versions.

Every tile goes through pipeline/decode.py first and the groups are put back together and
compared with tiles.bin: the hash on the watch can not catch a pack that was wrong when built.
"""
import argparse
import base64
import hashlib
import json
import os
import shutil
import struct
import sys

from .decode import Package, unpack_cut

BUILT_IN = "khibiny"
MAGIC = b"MMPK"
FORMAT = 1
SLICES = (("4k", 4096), ("2k", 2048))
# the watch refuses anything else: Pack.Head, Mmap.Package.check()
MAX_GROUP = 8192
MAX_GROUPS = 200
MAX_MANIFEST = 6144


class OverLimit(SystemExit):
    """The pack does not fit the watch: the end of a normal build, a thinner rung for a block."""


def tile_slots(pkg):
    """[(slot, lod, tx, ty, off, len)] for every non-empty tile; slot = global index."""
    out = []
    base = 0
    for L in pkg.lods:
        for ty in range(L.ny):
            for tx in range(L.nx):
                e = pkg.tile_entry(L.lod, tx, ty)
                if e is not None:
                    out.append((base + ty * L.nx + tx, L.lod, tx, ty, e[0], e[1]))
        base += L.nx * L.ny
    return out


def make_groups(slots, group_bytes):
    """Contiguous runs of tiles.bin, in offset order. Returns [(start, end)]."""
    blobs = sorted((s[4], s[4] + s[5]) for s in slots)
    groups = []
    for (a, b) in blobs:
        if groups and group_bytes > 0 and groups[-1][1] == a and b - groups[-1][0] <= group_bytes:
            groups[-1] = (groups[-1][0], b)
        else:
            groups.append((a, b))
    return groups


def sha4(data):
    return hashlib.sha1(data).digest()[:4]


def _str(s):
    raw = s.encode("ascii")
    if len(raw) > 255:
        raise ValueError("string too long: %r" % s)
    return bytes([len(raw)]) + raw


def pack_header(pack_id, date, title, terrain, slice_kb, tiles, groups, manifest):
    """Binary pack header, FORMAT.md 9.2."""
    h = bytearray(MAGIC)
    h += struct.pack("<BBHIH", FORMAT, slice_kb, len(groups), len(tiles), len(manifest))
    for s in (pack_id, date, title, terrain):
        h += _str(s)
    for (a, b) in groups:
        h += struct.pack("<IH", a, b - a) + sha4(tiles[a:b])
    h += manifest
    h += sha4(bytes(h))
    return bytes(h)


def group_json(data):
    return json.dumps({"d": base64.b64encode(data).decode("ascii")}, separators=(",", ":"))


def verify(manifest, tiles):
    """Decode every tile: raises if the package is broken."""
    pkg = Package(manifest, tiles, None)
    if pkg.encoding != 4 or pkg.version != 2:
        raise SystemExit("only version 2 packages with tile encoding 4 can be published")
    if len(manifest) > MAX_MANIFEST:
        raise OverLimit("manifest of %d B is over the watch limit %d" % (len(manifest), MAX_MANIFEST))
    slots = tile_slots(pkg)
    for s in slots:
        pkg.tile(s[1], s[2], s[3])
    return pkg, slots


def read_package(pkg_dir):
    with open(os.path.join(pkg_dir, "manifest.bin"), "rb") as f:
        manifest = f.read()
    with open(os.path.join(pkg_dir, "tiles.bin"), "rb") as f:
        tiles = f.read()
    return manifest, tiles


def cut(slots, tiles):
    """[(slice name, limit, groups)] of a pack, checked against what the watch takes."""
    cuts = []
    for (name, limit) in SLICES:
        groups = make_groups(slots, limit)
        if b"".join(tiles[a:b] for (a, b) in groups) != tiles:
            raise SystemExit("groups do not cover tiles.bin")
        big = max(b - a for (a, b) in groups)
        if big > MAX_GROUP:
            raise OverLimit("group of %d B is over the watch limit %d" % (big, MAX_GROUP))
        if len(groups) > MAX_GROUPS:
            raise OverLimit("%d groups are over the watch limit %d" % (len(groups), MAX_GROUPS))
        cuts.append((name, limit, groups))
    return cuts


def write_cuts(out, pack_id, title, date, terrain, manifest, tiles, cuts):
    for (name, limit, groups) in cuts:
        d = os.path.join(out, name)
        os.makedirs(d)
        with open(os.path.join(d, "pack.json"), "w", encoding="ascii") as f:
            f.write(group_json(pack_header(pack_id, date, title, terrain, limit // 1024, tiles, groups, manifest)))
        for i, (a, b) in enumerate(groups):
            with open(os.path.join(d, "g%03d.json" % i), "w", encoding="ascii") as f:
                f.write(group_json(tiles[a:b]))


def write_pack(pkg_dir, repo, pack_id, title, date, terrain):
    manifest, tiles = read_package(pkg_dir)
    pkg, slots = verify(manifest, tiles)
    cuts = cut(slots, tiles)

    rel = "packs/%s/%s" % (pack_id, date)
    out = os.path.join(repo, rel)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    shutil.copy(os.path.join(pkg_dir, "manifest.bin"), out)
    shutil.copy(os.path.join(pkg_dir, "tiles.bin"), out)
    if os.path.exists(os.path.join(pkg_dir, "route.bin")):
        shutil.copy(os.path.join(pkg_dir, "route.bin"), out)
    write_cuts(out, pack_id, title, date, terrain, manifest, tiles, cuts)
    return {
        "id": pack_id,
        "title": title,
        "path": rel,
        "kb": (len(tiles) + len(manifest) + 512) // 1024,
        "date": date,
        "terrain": terrain,
        "lat": pkg.origin[0],
        "lon": pkg.origin[1],
        "w": pkg.size_m[0],
        "h": pkg.size_m[1],
    }


def write_block(manifest, tiles, cuts, repo, block_id, title, date, terrain):
    """Publish a block (docs/BLOCKS.md): the cuts only, each one holds the whole database and
    decode.unpack_cut() gives manifest.bin + tiles.bin back. Returns the catalog row."""
    day = date.replace("-", "")
    path = "%s.%s" % (block_id, day)
    home = os.path.join(repo, "blocks", block_id)
    out = os.path.join(home, path)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    write_cuts(out, block_id, title, date, terrain, manifest, tiles, cuts)
    for (name, _limit, _groups) in cuts:
        if unpack_cut(os.path.join(out, name)) != (manifest, tiles):
            raise SystemExit("cut %s does not give the package back" % name)
    row = {"v": FORMAT, "n": [title], "p": [path], "k": [(len(tiles) + len(manifest) + 512) // 1024]}
    with open(os.path.join(home, "catalog.json"), "w", encoding="ascii") as f:
        json.dump(row, f, separators=(",", ":"))
        f.write("\n")
    return row


def update_catalog(repo, entry):
    """catalog.json: parallel arrays for the watch (names, paths, KB), `packs` for people."""
    path = os.path.join(repo, "catalog.json")
    packs = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            packs = json.load(f)["packs"]
    packs = [p for p in packs if p["id"] != entry["id"]] + [entry]
    packs.sort(key=lambda p: p["title"])
    # the map built into the app is published for the licence, not offered for download
    down = [p for p in packs if p["id"] != BUILT_IN]
    cat = {
        "v": FORMAT,
        "n": [p["title"] for p in down],
        "p": [p["path"] for p in down],
        "k": [p["kb"] for p in down],
        "packs": packs,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=1)
        f.write("\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package")
    ap.add_argument("--repo", required=True, help="checkout of the public data repository")
    ap.add_argument("--id", required=True)
    ap.add_argument("--title", required=True, help="short ASCII name, the watch menu shows it")
    ap.add_argument("--date", required=True, help="OSM snapshot date, YYYY-MM-DD")
    ap.add_argument("--terrain", required=True, help="DEM sources of this area, pipeline.fetch --sources")
    a = ap.parse_args(argv)
    if len(a.title) > 10:
        raise SystemExit("--title is over 10 characters, the watch screens cut it")
    entry = write_pack(a.package, a.repo, a.id, a.title, a.date, a.terrain)
    update_catalog(a.repo, entry)
    print(json.dumps(entry))


if __name__ == "__main__":
    main()
