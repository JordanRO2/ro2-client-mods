#!/usr/bin/env python3
"""Widen the space character in the client's Source Han Sans UI fonts.

The stock Source Han Sans space glyph is a touch narrow, so words read cramped at the
small UI sizes RO2 uses, and FontInfo.xml has no word-spacing knob. So the fix is in the
font itself: the advance of the space glyph (U+0020) is widened by DELTA font units
(em = 1000). Nothing else is touched — only the space, not letter-spacing.

Only the `hmtx` table is patched (one glyph's advance). The CFF outline table is left
byte-identical (it is also unrecompilable by fonttools on this ~65k-glyph CID font, which
is why this is a raw binary patch rather than a TTFont.save()). The `hmtx` table checksum
and the `head` checkSumAdjustment are recomputed so the file stays a valid sfnt.
fonttools is used read-only, just to resolve the space glyph id from the cmap.

Source (pristine b303-2022-02-11):
  SourceHanSans.otf        md5 6e8375177e47712a78f75e6f8ece4b42  (16282896 B)
  SourceHanSans-Light.otf  md5 f3746cc4d22ab77f9b86cf351c130fc6  (16472592 B)
  SourceHanSans-Heavy.otf  md5 d37be888ce793a605e38b809901382db  (16643796 B)

Usage:
  python space_lang_fonts.py <stock_font_dir> <out_dir> [delta]

`stock_font_dir` is any `lang/<code>/UI/FONT/` that holds the pristine fonts.
"""
import os
import struct
import sys

from fontTools.ttLib import TTFont  # read-only, to find the space glyph id

DELTA = 200 # font units added to the space glyph's advance (em = 1000)
FONTS = ["SourceHanSans.otf", "SourceHanSans-Light.otf", "SourceHanSans-Heavy.otf"]


def _table_checksum(b, off, length):
    s = 0
    for i in range(off, off + length, 4):
        s = (s + struct.unpack(">I", bytes(b[i:i + 4]).ljust(4, b"\0"))[0]) & 0xFFFFFFFF
    return s


def _file_checksum(b):
    data = bytes(b)
    if len(data) % 4:
        data += b"\0" * (4 - len(data) % 4)
    s = 0
    for i in range(0, len(data), 4):
        s = (s + struct.unpack(">I", data[i:i + 4])[0]) & 0xFFFFFFFF
    return s


def widen_space(in_path, out_path, delta=DELTA):
    ft = TTFont(in_path, lazy=True)
    gid = ft.getGlyphID(ft["cmap"].getBestCmap()[0x20])
    ft.close()

    b = bytearray(open(in_path, "rb").read())
    num_tables = struct.unpack(">H", b[4:6])[0]
    tables = {}
    for i in range(num_tables):
        o = 12 + i * 16
        tag = bytes(b[o:o + 4]).decode("latin1")
        _, toff, tlen = struct.unpack(">III", b[o + 4:o + 16])
        tables[tag] = (o, toff, tlen)

    p = tables["hmtx"][1] + gid * 4
    old = struct.unpack(">H", b[p:p + 2])[0]
    b[p:p + 2] = struct.pack(">H", min(65535, old + delta))

    hdir, hoff, hlen = tables["hmtx"]
    b[hdir + 4:hdir + 8] = struct.pack(">I", _table_checksum(b, hoff, hlen))
    head_off = tables["head"][1]
    b[head_off + 8:head_off + 12] = struct.pack(">I", 0)
    b[head_off + 8:head_off + 12] = struct.pack(">I", (0xB1B0AFBA - _file_checksum(b)) & 0xFFFFFFFF)

    open(out_path, "wb").write(b)
    return gid, old, old + delta


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    delta = int(sys.argv[3]) if len(sys.argv) > 3 else DELTA
    os.makedirs(out, exist_ok=True)
    for name in FONTS:
        gid, old, new = widen_space(os.path.join(src, name), os.path.join(out, name), delta)
        print(f"{name}: space gid={gid} advance {old} -> {new}")


if __name__ == "__main__":
    main()
