#!/usr/bin/env python3
"""Widen the word-space in the client's Source Han Sans UI fonts.

FontInfo.xml has no word-spacing knob and the stock space glyph is a touch narrow, so
words read cramped at the small UI sizes RO2 uses. The fix is in the font: the advance of
the space glyph (U+0020) is set to WIDTH font units (em = 1000). Nothing else is touched
— only the space, not letter-spacing. The glyph stays empty (no outline), so it is
invisible; only its advance changes.

WHY THE CFF CHARSTRING, NOT hmtx
--------------------------------
Source Han Sans is a CID-keyed CFF/OpenType font, and the client's text pipeline
(HarfBuzz shaping over a FreeType face) takes the glyph advance for U+0020 from the
**Type2 charstring width** inside the `CFF ` table, NOT from the `hmtx` table. Patching
`hmtx` alone has *no effect in game* (verified). So this script rewrites the space glyph's
charstring to `<WIDTH - nominalWidthX> endchar` (an empty glyph carrying only its width),
and sets `hmtx` to the same value for consistency with other tools. The stock advance is
225; a comfortable value is ~290.

WHY A RAW BINARY PATCH
----------------------
fonttools cannot re-save these fonts: they carry ~65k `cidNNNN` glyph names whose SIDs
exceed 65535, which the CFF charset formats (all 16-bit) cannot represent, so `TTFont.save`
raises. Instead this splices the one charstring in place and fixes up the CFF CharStrings
INDEX offsets, the TopDict Private-DICT offset, and the `CFF `/`hmtx` table checksums plus
the `head` checkSumAdjustment, keeping the file a valid sfnt. fonttools is used read-only,
only to resolve the space glyph id and the FDArray nominalWidthX/defaultWidthX.

Source (pristine b303-2022-02-11):
  SourceHanSans.otf        md5 6e8375177e47712a78f75e6f8ece4b42  (16282896 B)
  SourceHanSans-Light.otf  md5 f3746cc4d22ab77f9b86cf351c130fc6  (16472592 B)
  SourceHanSans-Heavy.otf  md5 d37be888ce793a605e38b809901382db  (16643796 B)

Output at WIDTH=290 (what is deployed):
  SourceHanSans.otf        md5 41d19fb334a607786d5eec12a7f80f4c  (16282897 B)
  SourceHanSans-Light.otf  md5 0013acf130b54931f53ea6cef720de54  (16472592 B)
  SourceHanSans-Heavy.otf  md5 7ab8dc1f1913e00b89399a61878f3c15  (16643799 B)

Usage:
  python space_lang_fonts.py <stock_font_dir> <out_dir> [width]

`stock_font_dir` is any `lang/<code>/UI/FONT/` that holds the pristine fonts.
"""
import os
import struct
import sys

from fontTools.ttLib import TTFont  # read-only: resolve space gid + private dict

WIDTH = 290  # space advance in font units (em = 1000); stock is 225
FONTS = ["SourceHanSans.otf", "SourceHanSans-Light.otf", "SourceHanSans-Heavy.otf"]


def _index_end(d, pos):
    """Return the byte offset just past a CFF INDEX starting at ``pos``."""
    count = struct.unpack(">H", d[pos:pos + 2])[0]
    if count == 0:
        return pos + 2, 0, None, None
    off_size = d[pos + 2]
    base = pos + 3

    def rd(i):
        return int.from_bytes(d[base + i * off_size:base + (i + 1) * off_size], "big")

    return base + (count + 1) * off_size - 1 + rd(count), count, off_size, base


def _parse_dict(b):
    ops, st, i = {}, [], 0
    while i < len(b):
        b0 = b[i]
        if b0 <= 21:
            key = 1200 + b[i + 1] if b0 == 12 else b0
            ops[key] = list(st)
            st = []
            i += 2 if b0 == 12 else 1
        elif b0 == 28:
            st.append(struct.unpack(">h", b[i + 1:i + 3])[0]); i += 3
        elif b0 == 29:
            st.append(struct.unpack(">i", b[i + 1:i + 5])[0]); i += 5
        elif b0 == 30:  # real
            i += 1
            while i < len(b):
                bb = b[i]; i += 1
                if (bb & 0xF) == 0xF or (bb >> 4) == 0xF:
                    break
            st.append(0.0)
        elif 32 <= b0 <= 246:
            st.append(b0 - 139); i += 1
        elif 247 <= b0 <= 250:
            st.append((b0 - 247) * 256 + b[i + 1] + 108); i += 2
        elif 251 <= b0 <= 254:
            st.append(-(b0 - 251) * 256 - b[i + 1] - 108); i += 2
        else:
            i += 1
    return ops


def _enc_t2_int(v):
    """Encode an integer as a Type2 charstring operand."""
    if -107 <= v <= 107:
        return bytes([v + 139])
    if 108 <= v <= 1131:
        v -= 108
        return bytes([(v >> 8) + 247, v & 0xFF])
    if -1131 <= v <= -108:
        v = -v - 108
        return bytes([(v >> 8) + 251, v & 0xFF])
    if -32768 <= v <= 32767:
        return bytes([28, (v >> 8) & 0xFF, v & 0xFF])
    return bytes([255]) + struct.pack(">i", v << 16)


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


def widen_space(in_path, out_path, width=WIDTH):
    # --- read-only metadata via fonttools ---
    ft = TTFont(in_path)
    sp_name = ft["cmap"].getBestCmap()[0x20]
    sp_gid = ft.getGlyphOrder().index(sp_name)
    priv = ft["CFF "].cff[ft["CFF "].cff.fontNames[0]].CharStrings[sp_name].private
    nominal, default = priv.nominalWidthX, priv.defaultWidthX
    ft.close()

    # new charstring: empty glyph carrying only its width. CFF advance = nominal + operand.
    new_cs = (b"" if width == default else _enc_t2_int(width - nominal)) + b"\x0e"

    d = bytearray(open(in_path, "rb").read())
    n = struct.unpack(">H", d[4:6])[0]
    tables = []
    for i in range(n):
        o = 12 + i * 16
        tag = d[o:o + 4]
        _, off, ln = struct.unpack(">III", d[o + 4:o + 16])
        tables.append((o, tag, off, ln))
    cff_off = next(t[2] for t in tables if t[1] == b"CFF ")
    cff_len = next(t[3] for t in tables if t[1] == b"CFF ")

    # CFF: header -> Name INDEX -> TopDict INDEX
    pos = cff_off + d[cff_off + 2]
    pos, _, _, _ = _index_end(d, pos)          # skip Name INDEX
    _, cnt, osz, base = _index_end(d, pos)      # TopDict INDEX
    td = bytes(d[base + (cnt + 1) * osz - 1 + int.from_bytes(d[base:base + osz], "big"):
                 base + (cnt + 1) * osz - 1 + int.from_bytes(d[base + osz:base + 2 * osz], "big")])
    ops = _parse_dict(td)
    cs_rel = ops[17][-1]
    priv_size, priv_rel = ops[18][0], ops[18][1]

    CS = cff_off + cs_rel
    count = struct.unpack(">H", d[CS:CS + 2])[0]
    off_size = d[CS + 2]
    b2 = CS + 3

    def rdoff(i):
        return int.from_bytes(d[b2 + i * off_size:b2 + (i + 1) * off_size], "big")

    ds = b2 + (count + 1) * off_size - 1
    # CharStrings must be immediately followed by the (last) Private DICT
    assert (ds + rdoff(count)) - cff_off == priv_rel, "unexpected CFF layout"

    o_lo, o_hi, o_end = rdoff(sp_gid), rdoff(sp_gid + 1), rdoff(count)
    delta = len(new_cs) - (o_hi - o_lo)
    region = bytes(d[ds + rdoff(0):ds + o_end])
    b0 = rdoff(0)
    new_region = region[:o_lo - b0] + new_cs + region[o_hi - b0:]
    new_off = bytearray()
    for i in range(count + 1):
        new_off += (rdoff(i) + (delta if i > sp_gid else 0)).to_bytes(off_size, "big")
    new_charstrings = bytes(d[CS:CS + 3]) + bytes(new_off) + new_region

    # bump the TopDict Private-DICT offset operand (same 5-byte `\x1d`+int32 encoding)
    pre = bytearray(d[cff_off:CS])
    i = pre.find(b"\x1d" + priv_rel.to_bytes(4, "big"))
    assert i != -1, "Private offset operand not found"
    pre[i:i + 5] = b"\x1d" + (priv_rel + delta).to_bytes(4, "big")

    priv_data = bytes(d[cff_off + priv_rel:cff_off + priv_rel + priv_size])
    new_cff = bytes(pre) + new_charstrings + priv_data
    pad = (4 - (len(new_cff) % 4)) % 4
    shift = len(new_cff) + pad - cff_len
    nf = bytearray(d[:cff_off]) + new_cff + b"\0" * pad + bytes(d[cff_off + cff_len:])

    # sfnt directory: CFF length + shift every table after it
    for diro, tag, off, ln in tables:
        if tag == b"CFF ":
            struct.pack_into(">I", nf, diro + 12, len(new_cff))
        elif off > cff_off:
            struct.pack_into(">I", nf, diro + 8, off + shift)
    hmtx_off = next(off + (shift if off > cff_off else 0)
                    for diro, tag, off, ln in tables if tag == b"hmtx")
    struct.pack_into(">H", nf, hmtx_off + sp_gid * 4, max(0, min(65535, width)))

    # recompute CFF + hmtx checksums, then head.checkSumAdjustment
    head_off = None
    for i in range(n):
        o = 12 + i * 16
        tag = nf[o:o + 4]
        _, off, ln = struct.unpack(">III", nf[o + 4:o + 16])
        if tag in (b"CFF ", b"hmtx"):
            struct.pack_into(">I", nf, o + 4, _table_checksum(nf, off, ln))
        if tag == b"head":
            head_off = off
    struct.pack_into(">I", nf, head_off + 8, 0)
    struct.pack_into(">I", nf, head_off + 8, (0xB1B0AFBA - _file_checksum(nf)) & 0xFFFFFFFF)

    open(out_path, "wb").write(nf)
    return sp_gid, nominal + (0 if width == default else (width - nominal))


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, out = sys.argv[1], sys.argv[2]
    width = int(sys.argv[3]) if len(sys.argv) > 3 else WIDTH
    os.makedirs(out, exist_ok=True)
    for name in FONTS:
        gid, adv = widen_space(os.path.join(src, name), os.path.join(out, name), width)
        print(f"{name}: space gid={gid} CFF advance -> {adv}")


if __name__ == "__main__":
    main()
