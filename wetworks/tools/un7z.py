"""Minimal 7z extractor (LZMA / LZMA2 / copy coders, single-coder folders). Usage: un7z.py archive outdir"""
import lzma
import os
import struct
import sys


class R:
    def __init__(self, b):
        self.b, self.i = b, 0

    def byte(self):
        v = self.b[self.i]; self.i += 1; return v

    def bytes(self, n):
        v = self.b[self.i:self.i + n]; self.i += n; return v

    def num(self):
        first = self.byte(); mask = 0x80; val = 0
        for i in range(8):
            if not first & mask:
                hi = first & (mask - 1)
                return val | (hi << (8 * i))
            val |= self.byte() << (8 * i)
            mask >>= 1
        return val

    def bits(self, n):
        out, cur, m = [], 0, 0
        for _ in range(n):
            if m == 0:
                cur, m = self.byte(), 0x80
            out.append(bool(cur & m)); m >>= 1
        return out

    def defined(self, n):
        allset = self.byte()
        return [True] * n if allset else self.bits(n)


def pack_info(r):
    pos = r.num(); n = r.num(); sizes = []
    while True:
        t = r.byte()
        if t == 0: break
        if t == 0x09: sizes = [r.num() for _ in range(n)]
        elif t == 0x0A:
            d = r.defined(n); [r.bytes(4) for x in d if x]
    return pos, sizes


def folder(r):
    coders = []
    n = r.num(); tin = tout = 0
    for _ in range(n):
        f = r.byte(); cid = r.bytes(f & 0xF)
        ni = no = 1
        if f & 0x10: ni, no = r.num(), r.num()
        props = r.bytes(r.num()) if f & 0x20 else b""
        coders.append((cid, props)); tin += ni; tout += no
    for _ in range(tout - 1): r.num(); r.num()
    npacked = tin - (tout - 1)
    if npacked > 1: [r.num() for _ in range(npacked)]
    return {"coders": coders, "nout": tout}


def coders_info(r):
    assert r.byte() == 0x0B
    nf = r.num(); assert r.byte() == 0
    folders = [folder(r) for _ in range(nf)]
    assert r.byte() == 0x0C
    for f in folders:
        f["sizes"] = [r.num() for _ in range(f["nout"])]
    while True:
        t = r.byte()
        if t == 0: break
        if t == 0x0A:
            d = r.defined(nf); [r.bytes(4) for x in d if x]
    return folders


def substreams(r, folders):
    nums = [1] * len(folders); sizes = None
    while True:
        t = r.byte()
        if t == 0: break
        if t == 0x0D: nums = [r.num() for _ in folders]
        elif t == 0x09:
            sizes = []
            for f, k in zip(folders, nums):
                part = [r.num() for _ in range(k - 1)] if k else []
                if k: part.append(f["sizes"][-1] - sum(part))
                sizes.append(part)
        elif t == 0x0A:
            total = sum(k for k in nums if k != 1) + sum(1 for k in nums if k == 1)
            d = r.defined(total); [r.bytes(4) for x in d if x]
    if sizes is None:
        sizes = [[f["sizes"][-1]] if k else [] for f, k in zip(folders, nums)]
    return sizes


def streams(r):
    info = {}
    while True:
        t = r.byte()
        if t == 0: break
        if t == 0x06: info["pack"] = pack_info(r)
        elif t == 0x07: info["folders"] = coders_info(r)
        elif t == 0x08: info["subs"] = substreams(r, info["folders"])
    return info


def decode(data, coders, size):
    for cid, props in reversed(coders):
        if cid == b"\x00":
            continue
        if cid == b"\x03\x01\x01":
            d = props[0]; lc = d % 9; d //= 9; lp = d % 5; pb = d // 5
            flt = [{"id": lzma.FILTER_LZMA1, "lc": lc, "lp": lp, "pb": pb, "dict_size": struct.unpack("<I", props[1:5])[0]}]
        elif cid == b"\x21":
            p = props[0]; ds = (2 | (p & 1)) << (p // 2 + 11) if p < 40 else 0xFFFFFFFF
            flt = [{"id": lzma.FILTER_LZMA2, "dict_size": ds}]
        else:
            raise SystemExit(f"unsupported coder {cid.hex()}")
        data = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=flt).decompress(data, max_length=size)
    return data[:size]


def unpack(b, info, base=32):
    pos, psizes = info["pack"]; off = base + pos; outs = []
    for f, ps in zip(info["folders"], psizes):
        outs.append(decode(b[off:off + ps], f["coders"], f["sizes"][-1])); off += ps
    return outs


def main(path, out):
    b = open(path, "rb").read()
    noff, nsize = struct.unpack("<QQ", b[12:28])
    r = R(b[32 + noff:32 + noff + nsize])
    t = r.byte()
    if t == 0x17:  # encoded header
        hdr = unpack(b, streams(r))[0]; r = R(hdr); t = r.byte()
    assert t == 0x01
    info, names, empty, emptyfile = {}, [], [], []
    while True:
        t = r.byte()
        if t == 0: break
        if t == 0x04: info = streams(r)
        elif t == 0x05:
            nfiles = r.num()
            while True:
                pt = r.byte()
                if pt == 0: break
                size = r.num(); data = R(r.bytes(size))
                if pt == 0x0E: empty = data.bits(nfiles)
                elif pt == 0x0F: emptyfile = data.bits(sum(empty))
                elif pt == 0x11:
                    data.byte(); raw = data.b[data.i:]
                    names = raw.decode("utf-16-le").split("\x00")[:nfiles]
    empty = empty or [False] * len(names)
    blobs = []
    if info:
        for folder_out, parts in zip(unpack(b, info), info["subs"]):
            o = 0
            for s in parts:
                blobs.append(folder_out[o:o + s]); o += s
    bi = ei = 0
    out = os.path.realpath(out)
    for name, e in zip(names, empty):
        dest = os.path.realpath(os.path.join(out, name.replace("\\", "/")))
        if not dest.startswith(out + os.sep):
            raise SystemExit(f"unsafe path {name!r}")
        if e:
            isdir = not (emptyfile[ei] if ei < len(emptyfile) else False); ei += 1
            if isdir: os.makedirs(dest, exist_ok=True)
            else: os.makedirs(os.path.dirname(dest), exist_ok=True); open(dest, "wb").close()
        else:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(dest, "wb").write(blobs[bi]); bi += 1
    print(f"extracted {bi} files")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
