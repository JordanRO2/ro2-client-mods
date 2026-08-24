#!/usr/bin/env python3
"""Add a bit of letter-spacing to the client's Source Han Sans UI fonts.

The stock Source Han Sans render feels cramped at the small UI sizes RO2 uses, and
FontInfo.xml has no tracking/spacing knob (only height / outline / shadow). So the
spacing is added in the font itself: every proportional glyph's horizontal advance is
widened by DELTA font units (em = 1000), which grows each inter-glyph gap uniformly.
Full-width CJK glyphs (advance == em) are left alone, so CJK layout is unchanged.

Only the `hmtx` table is touched. The CFF outline table is left byte-identical (it is
also unrecompilable by fonttools on this ~65k-glyph CID font, which is why this is a raw
binary patch rather than a TTFont.save()). The `hmtx` table checksum and the `head`
checkSumAdjustment are recomputed so the file stays a valid sfnt.

Source (pristine b303-2022-02-11):
  SourceHanSans.otf        md5 6e8375177e47712a78f75e6f8ece4b42  (16282896 B)
  SourceHanSans-Heavy.otf  md5 d37be888ce793a605e38b809901382db  (16643796 B)

Usage:
  python space_lang_fonts.py <stock_font_dir> <out_dir> [delta]

`stock_font_dir` is any `lang/<code>/UI/FONT/` that holds the pristine fonts.
"""
import os
import struct
import sys

DELTA = 50      # font units added to each proportional glyph's advance (em = 1000)
FULL_EM = 1000  # advance of a full-width CJK glyph; these are left unchanged
FONTS = ["SourceHanSans.otf", "SourceHanSans-Heavy.otf"]


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


def space_font(in_path, out_path, delta=DELTA):
    b = bytearray(open(in_path, "rb").read())
    num_tables = struct.unpack(">H", b[4:6])[0]
    tables = {}
    for i in range(num_tables):
        o = 12 + i * 16
        tag = bytes(b[o:o + 4]).decode("latin1")
        _, toff, tlen = struct.unpack(">III", b[o + 4:o + 16])
        tables[tag] = (o, toff, tlen)

    num_hmetrics = struct.unpack(">H", b[tables["hhea"][1] + 34:tables["hhea"][1] + 36])[0]
    hmtx_off = tables["hmtx"][1]
    changed = 0
    for i in range(num_hmetrics):
        p = hmtx_off + i * 4
        adv = struct.unpack(">H", b[p:p + 2])[0]
        if 0 < adv < FULL_EM:
            b[p:p + 2] = struct.pack(">H", min(65535, adv + delta))
            changed += 1

    hdir, hoff, hlen = tables["hmtx"]
    b[hdir + 4:hdir + 8] = struct.pack(">I", _table_checksum(b, hoff, hlen))
    head_off = tables["head"][1]
    b[head_off + 8:head_off + 12] = struct.pack(">I", 0)
    b[head_off + 8:head_off + 12] = struct.pack(">I", (0xB1B0AFBA - _file_checksum(b)) & 0xFFFFFFFF)

    open(out_path, "wb").write(b)
    return changed


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    delta = int(sys.argv[3]) if len(sys.argv) > 3 else DELTA
    os.makedirs(out, exist_ok=True)
    for name in FONTS:
        n = space_font(os.path.join(src, name), os.path.join(out, name), delta)
        print(f"{name}: widened {n} proportional glyphs by +{delta}")


if __name__ == "__main__":
    main()
