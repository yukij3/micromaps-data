"""FIT course reader (Garmin FIT protocol, stdlib only).

Reads the course that Garmin Connect exports as .fit (Courses -> Export -> FIT):
`record` messages give the track, `course_point` messages the waypoints and `course` the name.
`parse_fit()` returns exactly what `gpx.parse_gpx()` returns, so route.bin is built the same way.

Implemented protocol subset (FIT SDK "Flexible and Interoperable Data Transfer" protocol):
- 12- or 14-byte file header (".FIT" signature, data size, optional header CRC);
- definition / data messages, 16 local message types, per-definition architecture
  (little- or big-endian), field arrays, strings;
- compressed-timestamp record headers (5-bit offset against the last full timestamp);
- developer data fields: sizes are taken from the definition and the bytes are skipped;
- invalid sentinels per base type -> None (records without a position are dropped);
- file CRC (CRC-16, Garmin nibble table) is always checked; chained FIT files are read in turn.

    python3 -m pipeline.fit course.fit      # summary of what the reader sees
"""
import argparse
import os
import struct
from collections import namedtuple


class FitError(ValueError):
    """Malformed, truncated or corrupt FIT file."""


# global message numbers
MESG_FILE_ID = 0
MESG_RECORD = 20
MESG_COURSE = 31
MESG_COURSE_POINT = 32

TIMESTAMP_FIELD = 253
SEMICIRCLE_DEG = 180.0 / 2 ** 31

# base type number (low 5 bits of the base type byte) -> (size, struct code, invalid value)
# Floats are checked for the invalid all-ones pattern through the integer code in INVALID_RAW.
BASE_TYPES = {
    0x00: (1, "B", 0xFF),                  # enum
    0x01: (1, "b", 0x7F),                  # sint8
    0x02: (1, "B", 0xFF),                  # uint8
    0x03: (2, "h", 0x7FFF),                # sint16
    0x04: (2, "H", 0xFFFF),                # uint16
    0x05: (4, "i", 0x7FFFFFFF),            # sint32
    0x06: (4, "I", 0xFFFFFFFF),            # uint32
    0x07: (1, None, None),                 # string (NUL-terminated UTF-8)
    0x08: (4, "f", None),                  # float32
    0x09: (8, "d", None),                  # float64
    0x0A: (1, "B", 0x00),                  # uint8z
    0x0B: (2, "H", 0x0000),                # uint16z
    0x0C: (4, "I", 0x00000000),            # uint32z
    0x0D: (1, "B", 0xFF),                  # byte
    0x0E: (8, "q", 0x7FFFFFFFFFFFFFFF),    # sint64
    0x0F: (8, "Q", 0xFFFFFFFFFFFFFFFF),    # uint64
    0x10: (8, "Q", 0x0000000000000000),    # uint64z
}
FLOAT_RAW = {"f": "I", "d": "Q"}

# FIT `course_point` enum (Profile.xlsx, SDK 21.x). route.bin waypoints carry only a name
# (FORMAT.md 5.5) and the GPX path keeps only <name>, so the type is not stored in the package;
# it is used for the summary and kept in FitCourse for a future category field.
COURSE_POINT_TYPES = {
    0: "generic", 1: "summit", 2: "valley", 3: "water", 4: "food", 5: "danger", 6: "left",
    7: "right", 8: "straight", 9: "first_aid", 10: "fourth_category", 11: "third_category",
    12: "second_category", 13: "first_category", 14: "hors_category", 15: "sprint",
    16: "left_fork", 17: "right_fork", 18: "middle_fork", 19: "slight_left", 20: "sharp_left",
    21: "slight_right", 22: "sharp_right", 23: "u_turn", 24: "segment_start", 25: "segment_end",
    27: "campsite", 28: "aid_station", 29: "rest_area", 30: "general_distance", 31: "service",
    32: "energy_gel", 33: "sports_drink", 34: "mile_marker", 35: "checkpoint", 36: "shelter",
    37: "meeting_spot", 38: "overlook", 39: "toilet", 40: "shower", 41: "gear", 42: "sharp_curve",
    43: "steep_incline", 44: "tunnel", 45: "bridge", 46: "obstacle", 47: "crossing", 48: "store",
    49: "transition", 50: "navaid", 51: "transport", 52: "alert", 53: "info",
}

Message = namedtuple("Message", "num fields")  # fields: {field number: value or None}
_Def = namedtuple("_Def", "num endian fields dev_size size")  # fields: [(num, size, base)]

FitPoint = namedtuple("FitPoint", "lat lon alt dist")  # degrees, metres (alt/dist may be None)
FitCoursePoint = namedtuple("FitCoursePoint", "lat lon name type dist")  # type: enum int or None
FitCourse = namedtuple("FitCourse", "name file_type points course_points")


def is_fit(head):
    """True if the bytes start with a FIT file header."""
    return len(head) >= 12 and head[0] in (12, 14) and head[8:12] == b".FIT"


def crc16(data, crc=0):
    """FIT CRC (CRC-16/ARC: poly 0x8005 reflected, init 0), nibble table as in the FIT SDK."""
    t = _CRC_TABLE
    for b in data:
        tmp = t[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ t[b & 0xF]
        tmp = t[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc ^= tmp ^ t[(b >> 4) & 0xF]
    return crc


_CRC_TABLE = (0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
              0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400)


def _decode_field(raw, base, endian):
    """Field bytes -> value (scalar, list for arrays, str) with invalid sentinels -> None."""
    bt = BASE_TYPES.get(base & 0x1F)
    if bt is None:
        return bytes(raw)  # unknown base type: opaque bytes
    size, code, invalid = bt
    if code is None:  # string
        s = bytes(raw).split(b"\0", 1)[0].decode("utf-8", "replace")
        return s or None
    if len(raw) % size:
        return None  # size does not fit the base type: the profile says treat as invalid
    n = len(raw) // size
    vals = list(struct.unpack(f"{endian}{n}{code}", raw))
    if code in FLOAT_RAW:
        bits = struct.unpack(f"{endian}{n}{FLOAT_RAW[code]}", raw)
        allones = (1 << (8 * size)) - 1
        vals = [None if b == allones else v for v, b in zip(vals, bits)]
    else:
        vals = [None if v == invalid else v for v in vals]
    if n == 1:
        return vals[0]
    return None if all(v is None for v in vals) else vals


def iter_messages(data):
    """Yields Message(global number, fields) for every data message; raises FitError."""
    data = memoryview(bytes(data))
    pos = 0
    nfile = 0
    while pos < len(data):
        if nfile and not any(data[pos:]):
            break  # zero padding after the last file
        yield from _iter_file(data, pos, nfile)
        hsize = data[pos]
        pos += hsize + struct.unpack_from("<I", data, pos + 4)[0] + 2
        nfile += 1
    if nfile == 0:
        raise FitError("empty file")


def _iter_file(data, pos, nfile):
    where = f" (chained file {nfile + 1} at byte {pos})" if nfile else ""
    if len(data) - pos < 12:
        raise FitError(f"truncated header: {len(data) - pos} bytes{where}")
    hsize = data[pos]
    if hsize not in (12, 14) or bytes(data[pos + 8:pos + 12]) != b".FIT":
        raise FitError(f"not a FIT file: no 12/14-byte header with '.FIT' signature{where}")
    if len(data) - pos < hsize:
        raise FitError(f"truncated header: {len(data) - pos} of {hsize} bytes{where}")
    dsize = struct.unpack_from("<I", data, pos + 4)[0]
    if hsize == 14:
        hcrc = struct.unpack_from("<H", data, pos + 12)[0]
        if hcrc and hcrc != crc16(data[pos:pos + 12]):
            raise FitError(f"bad header CRC: stored 0x{hcrc:04X}, computed 0x{crc16(data[pos:pos + 12]):04X}"
                           f"{where}")
    end = pos + hsize + dsize
    if end + 2 > len(data):
        raise FitError(f"truncated: header announces {hsize + dsize + 2} bytes, "
                       f"file has {len(data) - pos}{where}")
    fcrc = struct.unpack_from("<H", data, end)[0]
    calc = crc16(data[pos:end])
    if fcrc != calc:
        raise FitError(f"bad file CRC: stored 0x{fcrc:04X}, computed 0x{calc:04X} "
                       f"(file corrupt or modified){where}")

    defs = {}
    last_ts = None
    p = pos + hsize
    while p < end:
        h = data[p]
        p += 1
        if h & 0x80:  # compressed timestamp header: data message, local type 0..3
            local = (h >> 5) & 0x03
            offset = h & 0x1F
            ts = None
            if last_ts is not None:  # no reference timestamp yet: leave the timestamp unset
                ts = (last_ts & ~0x1F) + offset
                if offset < (last_ts & 0x1F):
                    ts += 0x20  # 5-bit rollover
                last_ts = ts
        elif h & 0x40:  # definition message
            local = h & 0x0F
            if p + 5 > end:
                raise FitError(f"truncated definition message at byte {p - 1}{where}")
            arch = data[p + 1]
            if arch > 1:
                raise FitError(f"bad architecture {arch} at byte {p}{where}")
            endian = ">" if arch else "<"
            num, nf = struct.unpack_from(f"{endian}HB", data, p + 2)
            p += 5
            if p + 3 * nf > end:
                raise FitError(f"truncated definition message at byte {p - 6}{where}")
            fields = [(data[p + 3 * i], data[p + 3 * i + 1], data[p + 3 * i + 2]) for i in range(nf)]
            p += 3 * nf
            dev_size = 0
            if h & 0x20:  # developer data fields: (field num, size, developer data index)
                if p + 1 > end:
                    raise FitError(f"truncated definition message{where}")
                nd = data[p]
                p += 1
                if p + 3 * nd > end:
                    raise FitError(f"truncated definition message{where}")
                dev_size = sum(data[p + 3 * i + 1] for i in range(nd))
                p += 3 * nd
            defs[local] = _Def(num, endian, fields, dev_size, sum(f[1] for f in fields) + dev_size)
            continue
        else:  # normal data message
            local = h & 0x0F
            ts = None
        d = defs.get(local)
        if d is None:
            raise FitError(f"data message at byte {p - 1} uses undefined local type {local}{where}")
        if p + d.size > end:
            raise FitError(f"truncated data message at byte {p - 1}{where}")
        out = {}
        for fnum, fsize, base in d.fields:
            out[fnum] = _decode_field(data[p:p + fsize], base, d.endian)
            p += fsize
        p += d.dev_size  # developer fields are skipped
        if h & 0x80:
            out[TIMESTAMP_FIELD] = ts
        elif out.get(TIMESTAMP_FIELD) is not None:
            last_ts = out[TIMESTAMP_FIELD]
        yield Message(d.num, out)


def _semi(v):
    return None if v is None else v * SEMICIRCLE_DEG


def _scaled(v, scale, offset=0):
    return None if v is None else v / scale - offset


def read_course(src):
    """Path or bytes -> FitCourse. Records / course points without a position are skipped."""
    if isinstance(src, (bytes, bytearray, memoryview)):
        data = src
    else:
        with open(src, "rb") as f:
            data = f.read()
    name = file_type = None
    points, cpts = [], []
    for m in iter_messages(data):
        f = m.fields
        if m.num == MESG_RECORD:
            lat, lon = _semi(f.get(0)), _semi(f.get(1))
            if lat is None or lon is None:
                continue
            alt = _scaled(f.get(78), 5, 500)
            if alt is None:
                alt = _scaled(f.get(2), 5, 500)
            points.append(FitPoint(lat, lon, alt, _scaled(f.get(5), 100)))
        elif m.num == MESG_COURSE_POINT:
            lat, lon = _semi(f.get(2)), _semi(f.get(3))
            if lat is None or lon is None:
                continue
            nm = f.get(6)
            nm = nm.strip() if isinstance(nm, str) else None
            cpts.append(FitCoursePoint(lat, lon, nm or None, f.get(5), _scaled(f.get(4), 100)))
        elif m.num == MESG_COURSE and name is None:
            nm = f.get(5)
            if isinstance(nm, str) and nm.strip():
                name = nm.strip()
        elif m.num == MESG_FILE_ID and file_type is None:
            file_type = f.get(0)
    return FitCourse(name, file_type, points, cpts)


def parse_fit(src):
    """Same contract as gpx.parse_gpx: (name, [(lat, lon)], [(lat, lon, name)])."""
    c = read_course(src)
    if not c.points:
        raise FitError("no record messages with a position (not a course or activity file?)")
    return (c.name, [(p.lat, p.lon) for p in c.points],
            [(q.lat, q.lon, q.name) for q in c.course_points])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Summarize a FIT course as the pipeline reads it.")
    ap.add_argument("fit")
    a = ap.parse_args(argv)
    try:
        c = read_course(a.fit)
    except FitError as e:
        raise SystemExit(f"{a.fit}: {e}")
    print(f"{os.path.basename(a.fit)}: name={c.name!r} file_type={c.file_type} "
          f"points={len(c.points)} course_points={len(c.course_points)}")
    if c.points:
        d = c.points[-1].dist
        print(f"  first {c.points[0].lat:.6f},{c.points[0].lon:.6f}  last {c.points[-1].lat:.6f},"
              f"{c.points[-1].lon:.6f}" + (f"  distance {d / 1000:.2f} km" if d is not None else ""))
    for q in c.course_points:
        print(f"  {COURSE_POINT_TYPES.get(q.type, q.type)!s:16} {q.lat:.6f},{q.lon:.6f}  {q.name or ''}")


if __name__ == "__main__":
    main()
