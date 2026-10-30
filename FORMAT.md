# microMAPS package format (MMAP v1 / v2) — contract

This file is the contract between the pipeline (`pipeline/`), the PC reference
renderer (`preview/`) and the watch app (`app/`). Byte-level layout is owned by the
pipeline and documented in section 5 by whoever implements it; sections 1–4 are fixed.

## 1. Coordinate frame

- A package covers a rectangle. Origin = its **south-west corner** `(lat0, lon0)`.
- Local metres (equirectangular around the origin, fine for areas ≤ ~100 km):
  - `x = (lon - lon0) * cos(lat0) * 111319.49`  (east, metres)
  - `y = (lat - lat0) * 111319.49`               (north, metres)
- Screen y grows downward; renderers flip.

## 2. Tiles and levels of detail

| LOD | tile size | unit (quantum) | in-tile coords | used when view width |
|-----|-----------|----------------|----------------|----------------------|
| 0   | 1024 m    | 8 m (v1: 4 m)  | 0..128 (v1: 0..256) | ≤ ~2.5 km       |
| 1   | 4096 m    | 16 m           | 0..256         | > ~2.5 km            |

The quantum is read from the manifest (LOD descriptor), never assumed: in-tile units
`TU = tile_m / quantum` (a power of two). LOD0 at 8 m is the pixel generalization
(`pipeline/build.py --q0 8`, DP ε = 8 m: 1 px at 8 m/px, 2 px at the default 4 m/px);
`--q0 4` gives the v1 layout. The route keeps 4 m units (section 6).

Tile `(lod, tx, ty)` covers `x ∈ [tx·S, (tx+1)·S)`, `y ∈ [ty·S, (ty+1)·S)`.
Geometry is clipped to the tile rectangle (points may lie exactly on the edge = TU).
Missing tiles mean "empty".

## 3. Decoded feature model (what every decoder returns)

```
Tile = { lod, tx, ty, features: [Feature] }      # features sorted by class (draw order)
Feature = { cls: int, kind: LINE|AREA|POINT,
            pts: [(x, y), ...],                  # in-tile integer units, y north-up
            ele: int|None,                       # metres (contours, peaks)
            name: str|None }                     # short ASCII label (peaks, places), optional
```
AREA = one closed outer ring (last point != first; closing implied). Holes are not
supported in v1 (drop small islands / split rings).

## 4. Feature classes (`cls`)

| cls | kind  | meaning (OSM source)                                         |
|-----|-------|--------------------------------------------------------------|
| 1   | LINE  | major road: motorway, trunk, primary (+_link)                |
| 2   | LINE  | secondary, tertiary                                          |
| 3   | LINE  | minor road: unclassified, residential, living_street         |
| 4   | LINE  | track, service (unpaved/vehicle tracks)                      |
| 5   | LINE  | path, footway, bridleway, steps, cycleway (trails)           |
| 6   | LINE  | railway (rail, narrow_gauge) ; aerialway (lifts) also here   |
| 7   | LINE  | waterway: river, canal                                       |
| 8   | LINE  | waterway: stream, ditch, drain                               |
| 9   | LINE  | contour, minor interval                                      |
| 10  | LINE  | contour, index (every 5th interval) — has `ele`              |
| 16  | AREA  | water: natural=water, landuse=reservoir, riverbank           |
| 17  | AREA  | wetland                                                      |
| 18  | AREA  | glacier / permanent snow                                     |
| 24  | POINT | peak / volcano (`ele`, `name`)                               |
| 25  | POINT | alpine_hut, wilderness_hut, shelter                          |
| 26  | POINT | spring, drinking_water                                       |
| 27  | POINT | place (city/town/village/hamlet) (`name`)                    |
| 28  | POINT | saddle / mountain_pass (`ele`)                               |

Contours: LOD0 minor interval 20 m (index 100 m); LOD1 minor 50 m (index 250 m).
Draw order is by `cls` descending within areas first, then lines, then points
(renderer decides exact order; see `docs/DESIGN.md`).

## 5. Byte layout

Owned by the pipeline (`pipeline/encode.py`); the reference decoder is
`pipeline/decode.py`, written so it can be ported line by line to Monkey C.
All multi-byte fixed-width integers are **little-endian**. No floats, no compression.

### 5.1 Primitives

| name    | encoding |
|---------|----------|
| `u8/u16/u32/i32` | fixed width, little-endian (`i32` two's complement) |
| `varint` | unsigned LEB128: 7 bits per byte, low group first, bit 7 = "more bytes follow". Values here are < 2^31 (≤ 5 bytes) |
| `zz`     | signed int as zigzag varint: `v = n >= 0 ? 2n : -2n-1`; decode `n = (v >> 1) ^ -(v & 1)` |
| `str`    | `u8 len` + `len` bytes UTF-8; the pipeline writes printable ASCII only (len 0 = no string) |
| `bits(w)`| w-bit unsigned field from an MSB-first bit stream (see 5.4) |

### 5.2 Package files

```
out/<pkg>/manifest.bin        header + tile index for all LODs
out/<pkg>/tiles.bin           all tile blobs concatenated (LOD0 row-major, then LOD1)
out/<pkg>/tiles/<lod>/<tx>_<ty>.bin   the same blobs as separate files (= tiles.bin slices)
out/<pkg>/route.bin           optional route blob (section 6 / 5.5)
out/<pkg>/size_report.{md,json}
```

Why both: the watch can only load a whole resource and has ~128 KB in total, so tiles must
be loaded one at a time; a single concatenated resource would not fit. The canonical
artifact is `tiles.bin` + the manifest index (offset/length per tile), which lets any
packer (one resource per tile, a few tiles per resource, base64 strings, …) slice it
without re-encoding; `tiles/` holds the per-tile slices for convenience. A tile blob is
self-contained (cursor starts at 0,0) and needs nothing else from the manifest except the
encoding id. Max tile sizes are in `size_report.md` (LOD0 ≤ 6 KB, LOD1 ≤ 12 KB enforced).

### 5.3 manifest.bin

| offset | type | field |
|---|---|---|
| 0  | 4 × u8 | magic `"MMAP"` |
| 4  | u8  | version = 2 (VEC2 tiles), 1 (bits tiles) |
| 5  | u8  | `nlod` (= 2) |
| 6  | u8  | `name_len` |
| 7  | u8  | tile encoding id: **4 = vec2** (MMAP v2, section 5.7; default), **3 = bits** (MMAP v1, 5.4); 0 varint, 1 nibble, 2 byte: benchmark only |
| 8  | i32 | `lat0` × 1e7 (SW corner, degrees) |
| 12 | i32 | `lon0` × 1e7 |
| 16 | u32 | width, metres (x extent) |
| 20 | u32 | height, metres (y extent) |
| 24 | name_len × u8 | package name, ASCII |
| 24+name_len | nlod × 12 B | LOD descriptors, LOD 0 first |

LOD descriptor (12 bytes):

| offset | type | field |
|---|---|---|
| 0 | u16 | tile size, metres (1024 / 4096) |
| 2 | u16 | quantum, metres (4 / 16) |
| 4 | u16 | `nx` = ceil(width / tile) |
| 6 | u16 | `ny` = ceil(height / tile) |
| 8 | u32 | `index_off`: byte offset of this LOD's index inside manifest.bin |

Index: `nx × ny` entries of 6 bytes, row-major from the south-west, entry
`(tx, ty)` at `index_off + (ty * nx + tx) * 6`:

| offset | type | field |
|---|---|---|
| 0 | u32 | offset of the tile blob in `tiles.bin` |
| 4 | u16 | length in bytes; **0 = empty / missing tile** |

Geometry is clipped to the package rectangle, so the last tile column/row is partly empty.
For 100 × 100 km the index is ~60 KB; such packages should split the manifest per LOD1
tile (v2) - not needed for v1 test areas.

### 5.4 Tile blob (encoding 3 = bits)

```
tile    := varint nfeat              total features in the tile (for pre-sizing arrays)
           varint nblocks
           block × nblocks
block   := u8 head                   bits 0-4 cls, bit 5 HAS_ELE, bit 6 HAS_NAME, bit 7 = 0
           varint count              features in this block (all same cls)
           feature × count
feature := [varint npts]             only if kind != POINT (LINE >= 2, AREA >= 3)
           zz dx0, zz dy0            first point minus cursor
           [deltas]                  only if npts > 1, see below
           [zz ele]                  if HAS_ELE (metres)
           [str name]                if HAS_NAME (len 0 -> name = None)
deltas  := bit stream, MSB first, starting at the next byte:
           bits(4) w                 1..15, width of every delta field in this feature
           (npts-1) × ( bits(w) dx+B , bits(w) dy+B )   with B = 1 << (w-1)
           zero padding to the next byte boundary
```

- `kind` is implied by `cls`: `cls < 16` LINE, `16 ≤ cls < 24` AREA, `cls ≥ 24` POINT.
- Points are in tile units (0..256, y north-up). The **cursor** starts at (0,0) for each tile
  and after each feature is set to that feature's last point; `x0 = cursor.x + dx0`, and
  every following point adds its delta to the previous one.
- Blocks appear in ascending `cls` (a class may span several blocks, e.g. peaks with and
  without `ele`). The decoded feature list is the concatenation of all blocks, i.e. sorted
  by `cls` (section 3).
- Which classes carry what: `ele` on cls 10 (index contours) and on peaks/saddles when OSM has
  it; `name` on peaks, saddles, huts, places (max 32 bytes, printable ASCII: `name:en`, else `int_name`,
  else `name` transliterated, `pipeline/label.py`; the build refuses anything else).
- A LINE whose last point equals its first is a closed loop (contours).
- AREA rings are clipped to the tile with Sutherland-Hodgman, so a ring may run along
  the tile border (x or y = 0 or 256). Fill them normally; renderers that stroke area
  outlines should skip edges lying on the tile border.
- `w` ≤ 10 in practice (|delta| ≤ 256); the bit accumulator never holds more than 22 bits,
  safe for 32-bit Monkey C `Number`.

Reference decode of the delta loop (directly portable to Monkey C):

```
acc = 0; nb = 0;                       // bit reader, reset per feature
function bits(w) {
    while (nb < w) { acc = (acc << 8) | data[p]; p += 1; nb += 8; }
    nb -= w;
    var v = (acc >> nb) & ((1 << w) - 1);
    acc &= (1 << nb) - 1;
    return v;
}
w = bits(4); B = 1 << (w - 1);
for (k = 1; k < npts; k++) { x += bits(w) - B; y += bits(w) - B; emit(x, y); }
acc = 0; nb = 0;                       // padding of the last byte is skipped (p already past it)
```

### 5.5 route.bin

Coordinates in 4 m units from the package origin (may be negative or exceed the package).

```
u8   version = 1
str  name
varint npts
npts × (zz dx, zz dy)        first point relative to (0,0), then deltas
varint nwpt
nwpt × (zz dx, zz dy, str name)   each relative to the previous waypoint (first to (0,0))
```

The route is DP-simplified with ε = 2 m before quantization.

### 5.6 Encoding choice (measured on the Khibiny package, both LODs, identical features)

| delta encoding | tiles total | max LOD0 tile | B/point | per-point decode work |
|---|---:|---:|---:|---|
| zigzag varint dx, dy | 84 981 | 2 261 | 2.22 | 2 varint reads (byte, test, shift) + 2 unzigzag |
| nibble pair (-8..7) + escape | 96 644 | 2 230 | 2.53 | 1 byte, 1 compare; escape → 2 varints |
| signed byte pair + escape | 84 673 | 2 250 | 2.21 | 2 bytes, 1 compare, sign fix |
| **per-feature width bit-pack** | **75 794** | **2 024** | **1.98** | 2 × (refill check, shift, mask, subtract) |

Only 33 % of deltas fit a nibble pair (DP with ε = 1 quantum leaves long segments; most deltas
need 5-7 bits), so nibble coding loses. Bit-packing with one width per feature is 11 % smaller
than varint/byte at ~1.5-2× their ALU work per point, still only byte reads and shifts; a tile
has ≤ ~1000 points, so decode is dominated by drawing, not by the bit reader. For reference,
general-purpose compressors (unavailable on the watch) give: gzip -9 of the varint
encoding 65.0 KB, brotli -11 62.6 KB; on the bit-packed blob they gain only 2-5 %.

### 5.7 Tile blob, MMAP v2 (encoding 4 = vec2)

Same feature model and the same per-feature delta bit-pack as 5.4, but the whole tile is one
MSB-first bit stream: no byte padding between features, no `nfeat`, headers in exp-Golomb,
and lines that start on the tile border store the start as (side, pos).

```
vec2    := eg1(nblocks)  block × nblocks  zero padding to a byte
block   := bits5 cls, bits1 HAS_ELE, bits1 HAS_NAME, eg2(count - 1), feature × count
feature := [LINE|AREA]  eg2(npts - minpts)          minpts: LINE 2, AREA 3 (POINT: npts = 1)
           [LINE]       bits1 border
                          border = 1: bits2 side, bits_P pos      P = log2(TU): 7 on 8 m LOD0, 8 on LOD1
                                      S (pos, 0)  E (TU, pos)  N (TU - pos, TU)  W (0, TU - pos)
                          border = 0: eg4(zz(x0 - cx)), eg4(zz(y0 - cy))
           [AREA|POINT] eg4(zz(x0 - cx)), eg4(zz(y0 - cy))
           [npts > 1]   bits4 w, (npts - 1) × (bits_w dx + B, bits_w dy + B),  B = 1 << (w - 1)
           [HAS_ELE]    eg6(zz(ele))
           [HAS_NAME]   bits8 len, len × bits8 (ASCII; len 0 = None)
           cursor := last point (starts at (0, 0) per tile, carries across blocks)
eg_k(v) := v' = v + 2^k written as (bitlen(v') - k - 1) zero bits, then v' (bitlen(v') bits)
           read: z = zeros before the first 1 bit (that 1 is the top bit of v');
                 v' = (1 << (z + k)) | bits(z + k); v = v' - 2^k
```

- A block is a run of features with equal (cls, HAS_ELE, HAS_NAME), blocks in ascending `cls`
  (FORMAT 5.4 order), so decoded features are identical to the v1 encoding of the same input.
- Every field is ≤ 16 bits, so the bit accumulator stays ≤ 23 bits (32-bit `Number` safe).
- Only zero bits may follow the last feature (checked by both decoders).
- Watch memory layout (`app/source/MmapDecode.mc`): the blob plus a **block index**
  (3 Numbers per block: bit position of the first feature, count, head | cursor x << 8 |
  cursor y << 17), built in one pass at load; layers iterate one class with
  `seek/next/body` (no per-feature index).
- Khibiny 10 × 10 km: tiles 75 794 → 65 183 B (−14 %; LOD1 also drops the minor contours,
  `pipeline/build.py` `drop_after_order`, which style.json never draws at LOD1), max LOD0
  tile 2 024 → 1 670 B. Decoded features are identical to v1 (LOD1 minus cls 9), previews
  are pixel-identical.

## 6. Route

A route (course/track) is a separate blob in the package frame (metres, 4 m units),
decoded as `Route = { name, pts: [(x, y)], waypoints: [{x, y, name}] }`
(x, y in 4 m units from the package origin, north-up).
Sources: GPX/FIT exported from Garmin Connect (the same course the user loads onto the
watch natively), or a phone/web request (v2). GPX: `trk` (else `rte`) points, `wpt` names.
FIT: `record` positions, `course_point` names/positions, `course` name; course point types
are not stored (waypoints have no category). Both give identical route.bin for the same course
(`pipeline/README.md`, «Course input»).

## 7. Reference test area

Khibiny, Kola peninsula: 10 × 10 km, SW corner `67.590 N, 33.580 E`
(Kirovsk, Lake Bolshoy Vudyavr, Vudyavrchorr / Aikuaivenchorr peaks, ski lifts, trails).

## 8. Version 2: relief as a DEM grid (contours traced on the watch)

`pipeline.build --contours dem` (default) writes **version 2**. LOD0 carries no contour
vectors (cls 9/10); instead every LOD0 tile holds a small elevation grid and the watch
traces 20 m contours (100 m index) with integer marching squares, every frame, only over
the cells under the screen. LOD1 keeps vector index contours (cls 10, 250 m) and drops
cls 9, which no zoom level draws. All other features are byte-identical to version 1.
Reference: `pipeline/dem.py` (codec, integer marching squares), `pipeline/decode.py`
(`decode_tile_v2`), `preview/render.py` (`dem_layer`), watch: `MmapDecode.mc` `DemGrid`,
`MapRenderer.mc` `demLayer`. Parameter choice: `scripts/dem_compare.py`.

### 8.1 manifest.bin (version 2)

Same header with `version = 2`; after the LOD descriptors (before the tile index) an 8-byte
DEM descriptor:

| offset | type | field |
|---|---|---|
| 0 | u8  | `n`: grid cells per 1024 m tile (16 → 64 m), `(n+1)^2` nodes |
| 1 | u8  | `q`: quantum, metres (2) |
| 2 | u8  | flags: bit 0 = cubic ×2 upsampling on the watch at zi ≤ `cubic_zmax` |
| 3 | u8  | LOD carrying the DEM (0) |
| 4 | u16 | contour step, metres (20) |
| 6 | u16 | index contour every, metres (100) |

### 8.2 Tile blob (version 2, every LOD)

```
tile_v2 := u8 flags                 bit 0 HAS_DEM, bit 1 HAS_VEC
           [HAS_DEM] u16 dem_len, dem_len bytes (8.3)
           [HAS_VEC] vector tile in the manifest's encoding (4 = vec2, 5.7; 3 = bits, 5.4)
                     up to the end of the blob
```

### 8.3 DEM grid

Node `(i, j)` (i east, j north, row 0 = south edge) sits at
`(tx·1024 + i·1024/n, ty·1024 + j·1024/n)` package metres; neighbouring tiles share their
edge nodes, so contours join exactly. `K = floor(z / q)` from the pipeline DEM (10 m grid,
σ 15 m, bilinear). The watch reconstructs the bin centre `z' = K·q + q/2` (never exactly on a
20 m level for q = 2).

```
dem   := i16 K0 (LE)                node (0, 0)
         bits, MSB first            (n+1)^2 - 1 residuals, row-major, zero-padded to a byte
prediction  row 0:    P = K[i-1] + (i >= 2 ? K[i-1] - K[i-2] : 0)
            column 0: P = K[j-1] + (j >= 2 ? K[j-1] - K[j-2] : 0)
            else:     P = W + S - SW  (planar)
residual    e = K - P, m = zigzag(e); adaptive Golomb-Rice (LOCO-I):
            k = min{k : N << k >= A}; q = m >> k
            q < 20:  q zeros, 1, k low bits of m
            q >= 20: 20 zeros, m as 16 bits (no terminator)
            then A += |e|; N += 1; if N == 32: A >>= 1, N >>= 1   (start A = 4, N = 1)
```

Watch-side values are integers in 1/256 m: raw node `(K·q·2 + q)·128`; with cubic ×2 the
(2n+1)^2 grid is `pipeline.dem.upsample2` (kernel (−1, 9, 9, −1)/16 per axis, linear in the
first/last interval so tile edges depend only on edge nodes). Marching squares: a node is
"above" level L when `v >= L`; saddles are resolved by the mean of the four corners; the
crossing point on an edge is `t = ((L − v0) << 8) / (v1 − v0)` (Q8) between the screen
positions of the two nodes. Contours are drawn as dots anchored to the screen
(`(m + level#) % period == 0` along the segment's major axis), so unordered segments still
form an even dotted line.

Khibiny: 100 LOD0 grids = 12 229 B (max 183 B), package 38 747 B vs 76 974 B in version 1.

## 9. Downloaded packs (v2): static host, Storage, route from the phone

The watch downloads packs itself with `makeWebRequest` through Garmin Connect from a public static
host. There is no server and no companion app. The map built into the PRG stays: the app works
right after install. Source: `pipeline/pack.py`, `scripts/build_packs.py`, watch: `app/source/Pack.mc`.

### 9.1 Host layout

```
catalog.json                     {"v":1,"n":[titles],"p":[paths],"k":[KB],"packs":[details]}
packs/<id>/<date>/manifest.bin   5.3, the database (ODbL 4.6)
packs/<id>/<date>/tiles.bin      5.2
packs/<id>/<date>/<slice>/pack.json   {"d":"<base64 pack header>"}, slice = 4k or 2k
packs/<id>/<date>/<slice>/g000.json   {"d":"<base64 group>"}
blocks/index.json                the blocks built on request, docs/BLOCKS.md
blocks/<id>/catalog.json         {"v":1,"n":[title],"p":["<id>.<YYYYMMDD>"],"k":[KB]}, a host of one row
blocks/<id>/<id>.<YYYYMMDD>/<slice>/pack.json, g000.json...
```

A block has no row in the top-level `catalog.json` and its directory holds only the two slices: no
`manifest.bin` or `tiles.bin`, which would double the repository. The group files are the
machine-readable database, a slice holds the whole package: `python3 -m tools.decode --rebuild
<slice dir> <out dir>` writes both files back (`pipeline/decode.py`, `unpack_cut`).

A published `<date>` directory never changes, so a resumed download can not mix two versions.
Default host `cdn.jsdelivr.net/gh/yukij3/micromaps-data@main` (`.json` is `application/json`,
`raw.githubusercontent.com` is `text/plain`), setting `dataUrl`; GitHub Pages holds the same
tree and is tried when the catalog fails. Limits on the watch: 200 groups, 8 KB per group, packs up
to 20 × 20 km (the index lives in the heap).

### 9.2 Pack header

All integers little-endian.

| offset | type | field |
|---|---|---|
| 0 | 4 × u8 | magic `"MMPK"` |
| 4 | u8 | format = 1 |
| 5 | u8 | slice, KB |
| 6 | u16 | group count |
| 8 | u32 | size of `tiles.bin` |
| 12 | u16 | manifest length |
| 14 | 4 × str | id, snapshot date, title, terrain sources |
| ... | count × 10 B | u32 start, u16 length, 4 B hash of each group (contiguous from 0) |
| ... | | `manifest.bin` |
| end - 4 | 4 B | hash of everything before it |

Hash: first 4 bytes of SHA-1 (`Cryptography.Hash` is native on the watch).

### 9.3 Download

One request at a time. `Storage` keys: `mh` header, `mp` path/slice, `mn` groups done, `ma` slot in
use, `g<i>` one `ByteArray` per group. The count is saved after each group, so a dropped link costs
one group. The JSON response is released before `setValue` (one timer tick). `-402` / `-403` on the 4k
slice restart the pack from the 2k slice. A pack is usable only when `mn` equals the group count.
The map stops while the Maps screen is open: tiles dropped, GPS and compass off.

### 9.4 Checks before the decoder

Broken tile data ends in Array Out Of Bounds, which `try` does not catch, so:

1. Build: every tile passes `pipeline/decode.py`, the groups must add up to `tiles.bin`.
2. Header: magic, format, own hash, contiguous table, lengths.
3. Manifest (`Package.check`): version 2, encoding 4, LOD sizes and DEM parameters equal the app's
   constants, index inside the manifest, every tile inside the data.
4. Group: length and hash before `setValue`.
5. A missing group at read time is a skipped tile, not a crash.

### 9.5 Route string

`web/route/` turns a GPX file into a string in the browser; the user pastes it into the app setting
`routeIn`. The string is base64 of a version 2 route blob + 4 hash bytes. Not polyline: its alphabet
has `\`, which iOS settings fields were reported to refuse.

```
u8   version = 2
i32  lat0 × 1e7, i32 lon0 × 1e7    the first point: the route has its own origin
str  name (ASCII)
varint npts, npts × (zz dx, zz dy)  4 m units, x = (lon - lon0) * cos(lat0) * 111319.49
varint nwpt, nwpt × (zz dx, zz dy, str name)
4 B  first bytes of SHA-1 of the above
```

The page simplifies (RDP) until the route has at most 300 points and the string at most 2000
characters; the watch accepts 340 (route budget, `docs/device-audit.md`). The watch checks hash and
bounds, stores the blob as `rt`, clears the setting and remembers the hash in `rh` in case the
phone sends the old value again. `Route.rebase` moves it into the frame of the map in use, x scaled by
cos(lat0 route) / cos(lat0 map). One route is kept. The built-in demo route is a debug fixture and
is not loaded in release builds: it was generated from map data and nobody walked it.
