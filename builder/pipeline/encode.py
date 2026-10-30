"""MMAP v1 byte encoders (docs/FORMAT.md section 5).

Tile = class blocks of feature records. Only the coding of the per-point deltas differs
between the candidate encodings, so they can be benchmarked on identical data
(`pipeline.bench`). Packages use ENC_BITS (id 3) - see FORMAT.md 5.6 for the numbers.
"""
import struct

from .label import is_ascii_label
from .osm import LINE, POINT, kind_of

TILE_UNITS = 256

MAGIC = b"MMAP"
VERSION = 1      # vector-only package (contours as cls 9/10 lines)
VERSION_DEM = 2  # + DEM descriptor in the manifest, every tile blob starts with a flags byte

# tile v2 flags (FORMAT.md 8.2)
T_HAS_DEM = 0x01
T_HAS_VEC = 0x02
# DEM descriptor flags
DEM_CUBIC = 0x01

HAS_ELE = 0x20
HAS_NAME = 0x40
CLS_MASK = 0x1F

# encodings of the per-point deltas (id stored in the manifest header)
ENC_VARINT = 0  # dx, dy each zigzag LEB128 varint
ENC_NIBBLE = 1  # one byte (dx+8)<<4 | (dy+8) for dx,dy in -8..7; 0x88 = escape, then 2 zz-varints
ENC_BYTE = 2    # two signed bytes dx, dy (dx -127..127); dx byte 0x80 = escape, then 2 zz-varints
ENC_BITS = 3    # per feature bit width w (4-bit field), then w-bit (d + 2^(w-1)) for dx, dy; byte-padded
ENC_NAMES = {ENC_VARINT: "varint", ENC_NIBBLE: "nibble", ENC_BYTE: "byte", ENC_BITS: "bits"}
ENC_DEFAULT = ENC_BITS
NIBBLE_ESC = 0x88
BYTE_ESC = 0x80


def zigzag(n):
    return -2 * n - 1 if n < 0 else 2 * n


def unzigzag(v):
    return (v >> 1) ^ -(v & 1)


def put_varint(buf, v):
    if v < 0:
        raise ValueError("varint must be >= 0")
    while v >= 0x80:
        buf.append((v & 0x7F) | 0x80)
        v >>= 7
    buf.append(v)


def put_zz(buf, n):
    put_varint(buf, zigzag(n))


def bits_needed(v):
    """Smallest w such that v fits in w-bit two's complement range [-2^(w-1), 2^(w-1))."""
    w = 1
    while not (-(1 << (w - 1)) <= v < (1 << (w - 1))):
        w += 1
    return w


class BitWriter:
    """MSB-first bit packer."""

    def __init__(self, buf):
        self.buf = buf
        self.acc = 0
        self.nb = 0

    def put(self, v, w):
        self.acc = (self.acc << w) | v
        self.nb += w
        while self.nb >= 8:
            self.nb -= 8
            self.buf.append((self.acc >> self.nb) & 0xFF)
        self.acc &= (1 << self.nb) - 1

    def flush(self):
        if self.nb:
            self.buf.append((self.acc << (8 - self.nb)) & 0xFF)
        self.acc = 0
        self.nb = 0


def encode_deltas(buf, deltas, enc):
    if enc == ENC_BITS:
        if not deltas:
            return
        w = max(max(bits_needed(dx), bits_needed(dy)) for dx, dy in deltas)
        if w > 15:
            raise ValueError("delta too large")
        bias = 1 << (w - 1)
        bw = BitWriter(buf)
        bw.put(w, 4)
        for dx, dy in deltas:
            bw.put(dx + bias, w)
            bw.put(dy + bias, w)
        bw.flush()
        return
    for dx, dy in deltas:
        if enc == ENC_VARINT:
            put_zz(buf, dx)
            put_zz(buf, dy)
        elif enc == ENC_NIBBLE:
            if -8 <= dx <= 7 and -8 <= dy <= 7 and (dx, dy) != (0, 0):
                buf.append(((dx + 8) << 4) | (dy + 8))
            else:
                buf.append(NIBBLE_ESC)
                put_zz(buf, dx)
                put_zz(buf, dy)
        elif enc == ENC_BYTE:
            if -127 <= dx <= 127 and -128 <= dy <= 127:
                buf.append(dx & 0xFF)
                buf.append(dy & 0xFF)
            else:
                buf.append(BYTE_ESC)
                put_zz(buf, dx)
                put_zz(buf, dy)
        else:
            raise ValueError(enc)


def label_bytes(name):
    """Bytes of a package string. Labels are English-only (label.py); anything else here is a
    pipeline bug and must fail the build, not reach the watch."""
    s = name or ""
    if not is_ascii_label(s):
        raise ValueError(f"non-ASCII label {s!r}: pass it through label.ascii_label()")
    return s.encode("ascii")[:255]


def encode_name(buf, name):
    b = label_bytes(name)
    buf.append(len(b))
    buf.extend(b)


def blocks_of(features):
    """Group runs of consecutive features with the same (cls, has_ele) into blocks.
    A block is flagged HAS_NAME if any member has a name; members without one then
    store an empty name (1 byte), decoded as None."""
    runs = []
    for f in features:
        key = (f.cls, f.ele is not None)
        if runs and runs[-1][0] == key:
            runs[-1][1].append(f)
        else:
            runs.append((key, [f]))
    return [((k[0], k[1], any(f.name for f in fs)), fs) for k, fs in runs]


def encode_tile(features, enc=None, sizes=None, tu=TILE_UNITS):
    """features: objects with cls, pts (int tile units), ele, name; sorted by class.

    Returns bytes. If `sizes` is a list, (cls, bytes) is appended per feature (block
    header bytes are attributed to the block's first feature)."""
    if enc is None:
        enc = ENC_DEFAULT
    if enc == ENC_VEC2:
        return encode_tile_vec2(features, sizes, tu)
    buf = bytearray()
    blocks = blocks_of(features)
    put_varint(buf, len(features))
    put_varint(buf, len(blocks))
    cx, cy = 0, 0
    for (cls, has_ele, has_name), feats in blocks:
        start = len(buf)
        head = cls & CLS_MASK
        if has_ele:
            head |= HAS_ELE
        if has_name:
            head |= HAS_NAME
        buf.append(head)
        put_varint(buf, len(feats))
        point = kind_of(cls) == POINT
        for f in feats:
            if not point:
                put_varint(buf, len(f.pts))
            x0, y0 = f.pts[0]
            put_zz(buf, x0 - cx)
            put_zz(buf, y0 - cy)
            if not point:
                encode_deltas(buf, [(f.pts[i][0] - f.pts[i - 1][0], f.pts[i][1] - f.pts[i - 1][1])
                                    for i in range(1, len(f.pts))], enc)
            cx, cy = f.pts[-1]
            if has_ele:
                put_zz(buf, f.ele)
            if has_name:
                encode_name(buf, f.name)
            if sizes is not None:
                sizes.append((cls, len(buf) - start))
            start = len(buf)
    return bytes(buf)


def encode_route(name, pts, waypoints):
    """pts: [(x, y)] int route units (4 m); waypoints: [(x, y, name)]."""
    buf = bytearray()
    buf.append(VERSION)
    encode_name(buf, name or "")
    put_varint(buf, len(pts))
    cx, cy = 0, 0
    for x, y in pts:
        put_zz(buf, x - cx)
        put_zz(buf, y - cy)
        cx, cy = x, y
    put_varint(buf, len(waypoints))
    cx, cy = 0, 0
    for x, y, wn in waypoints:
        put_zz(buf, x - cx)
        put_zz(buf, y - cy)
        cx, cy = x, y
        encode_name(buf, wn or "")
    return bytes(buf)


HEADER_FMT = "<4sBBBBiiII"  # magic, version, nlod, name_len, encoding, lat0, lon0, width, height
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 24
LOD_FMT = "<HHHHI"  # tile_m, quantum_m, nx, ny, index_offset
LOD_SIZE = struct.calcsize(LOD_FMT)  # 12
DEM_FMT = "<BBBBHH"  # n, quantum_m, flags, lod, contour_step_m, index_every_m
DEM_SIZE = struct.calcsize(DEM_FMT)  # 8
ENTRY_FMT = "<IH"  # offset into tiles.bin, length (0 = empty tile)
ENTRY_SIZE = struct.calcsize(ENTRY_FMT)  # 6


def encode_tile_v2(dem_blob, vec_blob):
    """Tile blob of a version-2 package: u8 flags, [u16 dem_len, DEM], [v1 vector tile]."""
    out = bytearray([(T_HAS_DEM if dem_blob else 0) | (T_HAS_VEC if vec_blob else 0)])
    if dem_blob:
        if len(dem_blob) > 0xFFFF:
            raise ValueError("DEM blob too large")
        out += struct.pack("<H", len(dem_blob)) + dem_blob
    if vec_blob:
        out += vec_blob
    return bytes(out)


def encode_manifest(name, lat0, lon0, width_m, height_m, lods, enc=None, dem=None):
    """lods: list of dicts {tile_m, quantum_m, nx, ny, entries: [(offset, length)] row-major
    (index = ty * nx + tx)}. dem: None (version 1) or dict {n, quantum_m, cubic, lod,
    contour_step_m, index_every_m} (version 2, FORMAT.md 8.1). Returns bytes.
    The vector encoding (`enc`, default VEC2) is independent of the version."""
    if enc is None:
        enc = ENC_DEFAULT
    nb = label_bytes(name)
    out = bytearray(struct.pack(HEADER_FMT, MAGIC, VERSION if dem is None else VERSION_DEM, len(lods),
                                len(nb), enc, int(round(lat0 * 1e7)), int(round(lon0 * 1e7)), width_m, height_m))
    out += nb
    idx_off = len(out) + LOD_SIZE * len(lods) + (0 if dem is None else DEM_SIZE)
    descs = bytearray()
    index = bytearray()
    for L in lods:
        descs += struct.pack(LOD_FMT, L["tile_m"], L["quantum_m"], L["nx"], L["ny"], idx_off + len(index))
        for off, ln in L["entries"]:
            index += struct.pack(ENTRY_FMT, off, ln)
    if dem is not None:
        descs += struct.pack(DEM_FMT, dem["n"], dem["quantum_m"], DEM_CUBIC if dem["cubic"] else 0, dem["lod"],
                             dem["contour_step_m"], dem["index_every_m"])
    return bytes(out + descs + index)


# ---------------------------------------------------------------------------------------
# VEC2 (encoding 4, FORMAT.md 5.7): one MSB-first bit stream per tile, no byte padding
# between features, exp-Golomb headers, line starts on the tile border as (side, pos).

ENC_VEC2 = 4
ENC_NAMES[ENC_VEC2] = "vec2"
ENC_DEFAULT = ENC_VEC2
TU = 256  # default tile units per tile side (v1 LOD0 4 m, LOD1 16 m); 8 m LOD0: 128


def put_eg(bw, v, k):
    """Order-k exp-Golomb: v' = v + 2^k written as (bitlen(v') - k - 1) zeros, then v'."""
    if v < 0:
        raise ValueError("eg value must be >= 0")
    v += 1 << k
    n = v.bit_length()
    if n - k - 1:
        bw.put(0, n - k - 1)
    bw.put(v, n)


def border_of(pt, tu=TU):
    """(side, pos) for a point on the tile border, else None (tu = tile side in units).
    S: (pos, 0)  E: (tu, pos)  N: (tu - pos, tu)  W: (0, tu - pos), pos 0..tu-1."""
    x, y = pt
    if y == 0 and 0 <= x < tu:
        return 0, x
    if x == tu and 0 <= y < tu:
        return 1, y
    if y == tu and 0 < x <= tu:
        return 2, tu - x
    if x == 0 and 0 < y <= tu:
        return 3, tu - y
    return None


def vec2_blocks(features):
    """Runs of consecutive features with the same (cls, has_ele, has_name)."""
    runs = []
    for f in features:
        key = (f.cls, f.ele is not None, bool(f.name))
        if runs and runs[-1][0] == key:
            runs[-1][1].append(f)
        else:
            runs.append((key, [f]))
    return runs


def encode_tile_vec2(features, sizes=None, tu=TU):
    """VEC2 tile blob. `sizes` as in encode_tile (bytes rounded per feature, header to the
    first feature of the block). tu: tile side in units (power of two, tile_m / quantum_m);
    border positions take log2(tu) bits."""
    if tu & (tu - 1):
        raise ValueError("tile units must be a power of two")
    pb = tu.bit_length() - 1
    buf = bytearray()
    bw = BitWriter(buf)
    blocks = vec2_blocks(features)
    put_eg(bw, len(blocks), 1)
    cx, cy = 0, 0
    mark = 0
    for (cls, has_ele, has_name), feats in blocks:
        bw.put(cls & CLS_MASK, 5)
        bw.put(int(has_ele), 1)
        bw.put(int(has_name), 1)
        put_eg(bw, len(feats) - 1, 2)
        kind = kind_of(cls)
        for f in feats:
            pts = f.pts
            if kind != POINT:
                put_eg(bw, len(pts) - (2 if kind == LINE else 3), 2)
            b = border_of(pts[0], tu) if kind == LINE else None
            if kind == LINE:
                bw.put(int(b is not None), 1)
            if b is not None:
                bw.put(b[0], 2)
                bw.put(b[1], pb)
            else:
                put_eg(bw, zigzag(pts[0][0] - cx), 4)
                put_eg(bw, zigzag(pts[0][1] - cy), 4)
            if len(pts) > 1:
                deltas = [(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts))]
                w = max(max(bits_needed(dx), bits_needed(dy)) for dx, dy in deltas)
                if w > 15:
                    raise ValueError("delta too large")
                bias = 1 << (w - 1)
                bw.put(w, 4)
                for dx, dy in deltas:
                    bw.put(dx + bias, w)
                    bw.put(dy + bias, w)
            if has_ele:
                put_eg(bw, zigzag(f.ele), 6)
            if has_name:
                nb = label_bytes(f.name)
                bw.put(len(nb), 8)
                for ch in nb:
                    bw.put(ch, 8)
            cx, cy = pts[-1]
            if sizes is not None:
                pos = len(buf) * 8 + bw.nb
                sizes.append((cls, (pos - mark) / 8))
                mark = pos
    bw.flush()
    return bytes(buf)
