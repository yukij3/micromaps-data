"""Benchmark the candidate point-delta encodings on the real tiles + general-purpose
compressors for reference (the watch has no decompressor; these are only a yardstick)."""
import bz2
import gzip
import lzma
import shutil
import subprocess
import time

from .decode import decode_tile
from .encode import ENC_BITS, ENC_BYTE, ENC_NAMES, ENC_NIBBLE, ENC_VARINT, encode_tile


def _delta_stats(lod_tiles):
    n = nib = byte1 = 0
    for tiles in lod_tiles.values():
        for fs in tiles.values():
            for f in fs:
                for i in range(1, len(f.pts)):
                    dx = f.pts[i][0] - f.pts[i - 1][0]
                    dy = f.pts[i][1] - f.pts[i - 1][1]
                    n += 1
                    if -8 <= dx <= 7 and -8 <= dy <= 7:
                        nib += 1
                    if -63 <= dx <= 63 and -63 <= dy <= 63:
                        byte1 += 1
    return {"deltas": n, "fit_nibble_pct": round(100.0 * nib / max(n, 1), 1),
            "fit_1byte_varint_pct": round(100.0 * byte1 / max(n, 1), 1)}


def _compress_ext(cmd, data):
    if not shutil.which(cmd[0]):
        return None
    try:
        return len(subprocess.run(cmd, input=data, capture_output=True, check=True).stdout)
    except (OSError, subprocess.CalledProcessError):
        return None


def run(lod_tiles, tiles_blob, log=print):
    res = {"encodings": {}, "delta_stats": _delta_stats(lod_tiles)}
    for enc in (ENC_VARINT, ENC_NIBBLE, ENC_BYTE, ENC_BITS):
        r = {}
        tot = 0
        dec_t = 0.0
        npts = 0
        for lod, tiles in lod_tiles.items():
            sizes = []
            for key, fs in tiles.items():
                b = encode_tile(fs, enc)
                sizes.append(len(b))
                t0 = time.perf_counter()
                t = decode_tile(b, lod, key[0], key[1], enc)
                dec_t += time.perf_counter() - t0
                assert [f.pts for f in t.features] == [list(map(tuple, f.pts)) for f in fs]
                npts += sum(len(f.pts) for f in fs)
            r[f"lod{lod}_bytes"] = sum(sizes)
            r[f"lod{lod}_max_tile"] = max(sizes) if sizes else 0
            tot += sum(sizes)
        r["total_bytes"] = tot
        r["bytes_per_point"] = round(tot / max(npts, 1), 3)
        r["py_decode_us_per_point"] = round(1e6 * dec_t / max(npts, 1), 3)
        res["encodings"][ENC_NAMES[enc]] = r
        log(f"bench {ENC_NAMES[enc]:9s} {r}")
    # reference compressors over the chosen (bits) tiles.bin, whole-blob and per-tile
    comp = {"raw": len(tiles_blob), "gzip9": len(gzip.compress(tiles_blob, 9)),
            "xz9e": len(lzma.compress(tiles_blob, preset=9 | lzma.PRESET_EXTREME)),
            "bzip2": len(bz2.compress(tiles_blob, 9))}
    br = _compress_ext(["brotli", "-c", "-q", "11", "-"], tiles_blob)
    if br is not None:
        comp["brotli11"] = br
    zs = _compress_ext(["zstd", "-c", "-19", "-q", "-"], tiles_blob)
    if zs is not None:
        comp["zstd19"] = zs
    # per tile (what a per-resource compression could achieve)
    per_tile_gz = 0
    per_tile_br = 0
    for lod, tiles in lod_tiles.items():
        for fs in tiles.values():
            b = encode_tile(fs, ENC_BITS)
            per_tile_gz += len(gzip.compress(b, 9))
            if br is not None:
                per_tile_br += _compress_ext(["brotli", "-c", "-q", "11", "-"], b)
    comp["gzip9_per_tile_sum"] = per_tile_gz
    if br is not None:
        comp["brotli11_per_tile_sum"] = per_tile_br
    # same compressors over the byte-aligned varint encoding (entropy coders like those better)
    vblob = b"".join(encode_tile(fs, ENC_VARINT) for tiles in lod_tiles.values() for fs in tiles.values())
    comp["varint_raw"] = len(vblob)
    comp["varint_gzip9"] = len(gzip.compress(vblob, 9))
    vb = _compress_ext(["brotli", "-c", "-q", "11", "-"], vblob)
    if vb is not None:
        comp["varint_brotli11"] = vb
    res["compressors_on_tiles"] = comp
    log(f"bench compressors {comp}")
    log(f"bench deltas {res['delta_stats']}")
    return res
