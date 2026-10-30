"""Reference decoder for MMAP v1/v2 packages (docs/FORMAT.md sections 5 and 8).

Deliberately written as simple index/shift loops over a byte array so it can be ported
line by line to Monkey C (ByteArray, Number, no floats in the tile path).

    pkg = load_package("out/khibiny")
    pkg.origin            -> (lat0, lon0) degrees
    pkg.size_m            -> (width_m, height_m)
    pkg.tile(0, 3, 4)     -> Tile | None   (Tile.dem: DemTile | None, version 2)
    pkg.route             -> Route | None
    pkg.latlon_to_xy(lat, lon) -> (x, y) package metres
"""
import math
import os

LINE, AREA, POINT = "LINE", "AREA", "POINT"
M_PER_DEG = 111319.49

ENC_VARINT, ENC_NIBBLE, ENC_BYTE, ENC_BITS, ENC_VEC2 = 0, 1, 2, 3, 4
TU = 256


class Feature:
    __slots__ = ("cls", "kind", "pts", "ele", "name")

    def __init__(self, cls, kind, pts, ele=None, name=None):
        self.cls = cls
        self.kind = kind
        self.pts = pts
        self.ele = ele
        self.name = name

    def __repr__(self):
        return f"Feature(cls={self.cls}, kind={self.kind}, n={len(self.pts)}, ele={self.ele}, name={self.name!r})"


class Tile:
    __slots__ = ("lod", "tx", "ty", "features", "dem")

    def __init__(self, lod, tx, ty, features, dem=None):
        self.lod = lod
        self.tx = tx
        self.ty = ty
        self.features = features
        self.dem = dem


class DemInfo:
    """Manifest DEM descriptor (FORMAT.md 8.1)."""
    __slots__ = ("n", "quantum_m", "cubic", "lod", "contour_step_m", "index_every_m")

    def __init__(self, n, quantum_m, flags, lod, contour_step_m, index_every_m):
        self.n = n
        self.quantum_m = quantum_m
        self.cubic = bool(flags & 1)
        self.lod = lod
        self.contour_step_m = contour_step_m
        self.index_every_m = index_every_m


class DemTile:
    """Decoded DEM grid of one tile: K = node values in quanta, (n+1)^2 row-major, row 0 south."""
    __slots__ = ("n", "quantum_m", "cubic", "K")

    def __init__(self, n, quantum_m, cubic, K):
        self.n = n
        self.quantum_m = quantum_m
        self.cubic = cubic
        self.K = K


class Route:
    __slots__ = ("name", "pts", "waypoints", "origin")

    def __init__(self, name, pts, waypoints, origin=None):
        self.origin = origin    # version 2: (lat0e7, lon0e7) of the route's own frame
        self.name = name
        self.pts = pts
        self.waypoints = waypoints  # [{"x", "y", "name"}]


def kind_of(cls):
    if cls < 16:
        return LINE
    if cls < 24:
        return AREA
    return POINT


class Reader:
    """Cursor over a byte array. Every method is a few byte reads and shifts."""

    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos
        self.acc = 0  # bit reader state (MSB-first), only used inside a bit-packed run
        self.nb = 0

    def bits_reset(self):
        self.acc = 0
        self.nb = 0

    def bits(self, w):
        """Read w (<= 15) bits MSB-first. acc never holds more than 22 bits (32-bit safe)."""
        while self.nb < w:
            self.acc = (self.acc << 8) | self.d[self.p]
            self.p += 1
            self.nb += 8
        self.nb -= w
        v = (self.acc >> self.nb) & ((1 << w) - 1)
        self.acc &= (1 << self.nb) - 1
        return v

    def u8(self):
        b = self.d[self.p]
        self.p += 1
        return b

    def u16(self):  # little-endian
        d, p = self.d, self.p
        self.p = p + 2
        return d[p] | (d[p + 1] << 8)

    def u32(self):
        d, p = self.d, self.p
        self.p = p + 4
        return d[p] | (d[p + 1] << 8) | (d[p + 2] << 16) | (d[p + 3] << 24)

    def i32(self):
        v = self.u32()
        if v >= 0x80000000:
            v -= 0x100000000
        return v

    def varint(self):
        v = 0
        shift = 0
        while True:
            b = self.d[self.p]
            self.p += 1
            v |= (b & 0x7F) << shift
            if b < 0x80:
                return v
            shift += 7

    def zz(self):
        v = self.varint()
        return (v >> 1) ^ -(v & 1)

    def s8(self):
        b = self.u8()
        return b - 256 if b >= 128 else b

    def string(self):
        n = self.u8()
        s = bytes(self.d[self.p:self.p + n]).decode("utf-8")
        self.p += n
        return s


class BitReader:
    """MSB-first bit stream over a whole tile (VEC2, FORMAT.md 5.7). Reads are <= 16 bits,
    so acc never holds more than 23 bits."""

    def __init__(self, data, bitpos=0):
        self.d = data
        self.p = bitpos >> 3
        self.acc = 0
        self.nb = 0
        if bitpos & 7:
            self.bits(bitpos & 7)

    def bitpos(self):
        return self.p * 8 - self.nb

    def bits(self, w):
        while self.nb < w:
            self.acc = (self.acc << 8) | self.d[self.p]
            self.p += 1
            self.nb += 8
        self.nb -= w
        v = (self.acc >> self.nb) & ((1 << w) - 1)
        self.acc &= (1 << self.nb) - 1
        return v

    def eg(self, k):
        z = 0
        while self.bits(1) == 0:
            z += 1
        return ((1 << (z + k)) | self.bits(z + k)) - (1 << k)

    def zeg(self, k):
        v = self.eg(k)
        return (v >> 1) ^ -(v & 1)


def from_border(side, pos, tu=TU):
    if side == 0:
        return pos, 0
    if side == 1:
        return tu, pos
    if side == 2:
        return tu - pos, tu
    return 0, tu - pos


def decode_tile_vec2(data, lod=0, tx=0, ty=0, tu=TU):
    """tu: tile side in tile units (tile_m / quantum_m, a power of two); border positions
    take log2(tu) bits."""
    pb = tu.bit_length() - 1
    r = BitReader(data)
    nblocks = r.eg(1)
    feats = []
    cx = 0
    cy = 0
    for _b in range(nblocks):
        cls = r.bits(5)
        has_ele = r.bits(1)
        has_name = r.bits(1)
        count = r.eg(2) + 1
        kind = kind_of(cls)
        for _f in range(count):
            npts = 1
            if kind == LINE:
                npts = r.eg(2) + 2
            elif kind == AREA:
                npts = r.eg(2) + 3
            if kind == LINE and r.bits(1):
                side = r.bits(2)
                x, y = from_border(side, r.bits(pb), tu)
            else:
                x = cx + r.zeg(4)
                y = cy + r.zeg(4)
            pts = [(x, y)]
            if npts > 1:
                w = r.bits(4)
                bias = 1 << (w - 1)
                for _k in range(npts - 1):
                    x += r.bits(w) - bias
                    y += r.bits(w) - bias
                    pts.append((x, y))
            cx = x
            cy = y
            ele = None
            name = None
            if has_ele:
                ele = r.zeg(6)
            if has_name:
                n = r.bits(8)
                name = bytes(r.bits(8) for _ in range(n)).decode("utf-8") or None
            feats.append(Feature(cls, kind, pts, ele, name))
    # only the zero padding of the last byte may follow
    if r.p != len(data) or r.acc != 0:
        raise ValueError(f"tile {lod}/{tx}/{ty}: bad length")
    return Tile(lod, tx, ty, feats)


def decode_tile(data, lod=0, tx=0, ty=0, enc=ENC_BITS, tu=TU):
    """Decode one tile blob into the section-3 model (FORMAT.md 5.3/5.4/5.7)."""
    if enc == ENC_VEC2:
        return decode_tile_vec2(data, lod, tx, ty, tu)
    r = Reader(data)
    nfeat = r.varint()  # total features (lets Monkey C pre-size its arrays)
    nblocks = r.varint()
    feats = []
    cx = 0
    cy = 0
    for _b in range(nblocks):
        head = r.u8()
        cls = head & 0x1F
        kind = kind_of(cls)
        count = r.varint()
        for _f in range(count):
            npts = 1
            if kind != POINT:
                npts = r.varint()
            x = cx + r.zz()
            y = cy + r.zz()
            pts = [(x, y)]
            if npts > 1:
                if enc == ENC_BITS:
                    r.bits_reset()
                    w = r.bits(4)
                    bias = 1 << (w - 1)
                    for _k in range(npts - 1):
                        x += r.bits(w) - bias
                        y += r.bits(w) - bias
                        pts.append((x, y))
                    r.bits_reset()  # drop the padding bits of the last byte
                else:
                    for _k in range(npts - 1):
                        if enc == ENC_NIBBLE:
                            b = r.u8()
                            if b != 0x88:
                                x += (b >> 4) - 8
                                y += (b & 15) - 8
                            else:
                                x += r.zz()
                                y += r.zz()
                        elif enc == ENC_VARINT:
                            x += r.zz()
                            y += r.zz()
                        else:  # ENC_BYTE
                            b = r.u8()
                            if b != 0x80:
                                x += b - 256 if b >= 128 else b
                                y += r.s8()
                            else:
                                x += r.zz()
                                y += r.zz()
                        pts.append((x, y))
            cx = x
            cy = y
            ele = None
            name = None
            if head & 0x20:
                ele = r.zz()
            if head & 0x40:
                name = r.string()
                if name == "":
                    name = None
            feats.append(Feature(cls, kind, pts, ele, name))
    if r.p != len(data) or len(feats) != nfeat:
        raise ValueError(f"tile {lod}/{tx}/{ty}: bad length/count")
    return Tile(lod, tx, ty, feats)


def decode_tile_v2(data, lod, tx, ty, enc, dem_info, tu=TU):
    """Version-2 tile blob: u8 flags, [u16 dem_len, DEM blob], [v1 vector tile] (FORMAT.md 8.2)."""
    from . import dem as demcodec
    r = Reader(data)
    flags = r.u8()
    dt = None
    if flags & 1:
        n = r.u16()
        if dem_info is None:
            raise ValueError("DEM tile without a DEM descriptor")
        K = demcodec.decode(data[r.p:r.p + n], dem_info.n)
        dt = DemTile(dem_info.n, dem_info.quantum_m, dem_info.cubic, K)
        r.p += n
    if flags & 2:
        t = decode_tile(data[r.p:], lod, tx, ty, enc, tu)
    elif r.p != len(data):
        raise ValueError(f"tile {lod}/{tx}/{ty}: bad length")
    else:
        t = Tile(lod, tx, ty, [])
    t.dem = dt
    return t


def decode_route(data):
    r = Reader(data)
    ver = r.u8()
    origin = None
    if ver == 2:
        origin = (r.i32(), r.i32())
    elif ver != 1:
        raise ValueError(f"route version {ver}")
    name = r.string()
    n = r.varint()
    pts = []
    x = 0
    y = 0
    for _ in range(n):
        x += r.zz()
        y += r.zz()
        pts.append((x, y))
    nw = r.varint()
    wps = []
    x = 0
    y = 0
    for _ in range(nw):
        x += r.zz()
        y += r.zz()
        wps.append({"x": x, "y": y, "name": r.string()})
    return Route(name or None, pts, wps, origin)


class LodInfo:
    __slots__ = ("lod", "tile_m", "quantum_m", "nx", "ny", "index_off")

    def __init__(self, lod, tile_m, quantum_m, nx, ny, index_off):
        self.lod = lod
        self.tile_m = tile_m
        self.quantum_m = quantum_m
        self.nx = nx
        self.ny = ny
        self.index_off = index_off


class Package:
    def __init__(self, manifest, tiles_blob, route_blob=None):
        r = Reader(manifest)
        if bytes(manifest[0:4]) != b"MMAP":
            raise ValueError("bad magic")
        r.p = 4
        self.version = r.u8()
        nlod = r.u8()
        name_len = r.u8()
        self.encoding = r.u8()
        lat0 = r.i32() / 1e7
        lon0 = r.i32() / 1e7
        self.origin = (lat0, lon0)
        self.size_m = (r.u32(), r.u32())
        self.name = bytes(manifest[r.p:r.p + name_len]).decode("utf-8")
        r.p += name_len
        self.lods = []
        for i in range(nlod):
            self.lods.append(LodInfo(i, r.u16(), r.u16(), r.u16(), r.u16(), r.u32()))
        self.dem = None
        if self.version >= 2:
            self.dem = DemInfo(r.u8(), r.u8(), r.u8(), r.u8(), r.u16(), r.u16())
        elif self.version != 1:
            raise ValueError("unsupported version %d" % self.version)
        self._manifest = manifest
        self._tiles = tiles_blob
        self._route_blob = route_blob
        self._route = None
        self._kx = math.cos(math.radians(lat0)) * M_PER_DEG

    def tile_entry(self, lod, tx, ty):
        """(offset, length) into tiles.bin or None."""
        L = self.lods[lod]
        if tx < 0 or ty < 0 or tx >= L.nx or ty >= L.ny:
            return None
        r = Reader(self._manifest, L.index_off + (ty * L.nx + tx) * 6)
        off = r.u32()
        ln = r.u16()
        if ln == 0:
            return None
        return off, ln

    def tile_bytes(self, lod, tx, ty):
        e = self.tile_entry(lod, tx, ty)
        if e is None:
            return None
        return self._tiles[e[0]:e[0] + e[1]]

    def tile(self, lod, tx, ty):
        b = self.tile_bytes(lod, tx, ty)
        if b is None:
            return None
        L = self.lods[lod]
        tu = L.tile_m // L.quantum_m
        if self.version == 1:
            return decode_tile(b, lod, tx, ty, self.encoding, tu)
        return decode_tile_v2(b, lod, tx, ty, self.encoding, self.dem, tu)

    @property
    def route(self):
        if self._route is None and self._route_blob:
            self._route = decode_route(self._route_blob)
        return self._route

    def latlon_to_xy(self, lat, lon):
        """Degrees -> package metres (x east, y north) per FORMAT.md section 1."""
        return ((lon - self.origin[1]) * self._kx, (lat - self.origin[0]) * M_PER_DEG)

    def xy_to_latlon(self, x, y):
        return (self.origin[0] + y / M_PER_DEG, self.origin[1] + x / self._kx)


def load_package(path):
    """path: package directory containing manifest.bin, tiles.bin and optional route.bin."""
    with open(os.path.join(path, "manifest.bin"), "rb") as f:
        manifest = f.read()
    with open(os.path.join(path, "tiles.bin"), "rb") as f:
        tiles = f.read()
    route = None
    rp = os.path.join(path, "route.bin")
    if os.path.exists(rp):
        with open(rp, "rb") as f:
            route = f.read()
    return Package(manifest, tiles, route)


if __name__ == "__main__":
    import sys

    p = load_package(sys.argv[1] if len(sys.argv) > 1 else "out/khibiny")
    print("origin", p.origin, "size", p.size_m, "name", p.name, "enc", p.encoding)
    for L in p.lods:
        n = 0
        nf = 0
        for ty in range(L.ny):
            for tx in range(L.nx):
                t = p.tile(L.lod, tx, ty)
                if t:
                    n += 1
                    nf += len(t.features)
        print(f"lod {L.lod}: {L.nx}x{L.ny} grid, {n} tiles, {nf} features")
    if p.dem:
        print(f"dem: n {p.dem.n} q {p.dem.quantum_m} m cubic {p.dem.cubic} lod {p.dem.lod} "
              f"contours {p.dem.contour_step_m}/{p.dem.index_every_m} m")
    if p.route:
        print("route", p.route.name, len(p.route.pts), "pts", len(p.route.waypoints), "waypoints")
