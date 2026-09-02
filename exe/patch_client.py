#!/usr/bin/env python3
"""
RO2 client fix patcher (modular, standalone).

Builds a patched copy of the RO2 client (Rag2.exe -> ro2-fixed.exe) by applying a
set of independently-toggleable FIXES. The ORIGINAL exe is NEVER modified and no
injected DLL / mod is involved -- ro2-fixed.exe is a self-contained binary.

Design:
  * One self-documenting entry per fix: WHAT it changes, WHY (symptom/root cause),
    the EVIDENCE, and the RISK. `--list` prints this catalog.
  * Adding a fix = append a Fix(...) to FIXES. Toggle with `enabled` or `--only`.
  * Code/data fixes are PATTERN-ANCHORED (search a unique byte sequence) instead of
    trusting a raw file offset, so a fix refuses to apply on a mismatched binary
    rather than corrupting it. Header fixes edit the PE header field directly.
  * Every run reports exactly which bytes changed; re-running is idempotent
    (an already-applied fix reports SKIP, not a double-patch).

Usage:
  python patch_client.py                 # apply all ENABLED fixes -> ro2-fixed.exe
  python patch_client.py --list          # print the fix catalog + selection, no write
  python patch_client.py --only laa      # apply only these fix ids (comma-separated)
  python patch_client.py --in Rag2.exe --out ro2-fixed.exe
"""
import argparse
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Patch primitives  (each returns (result, message);
#   result True = applied, False = already-applied/skip, None = FAILED)
# ---------------------------------------------------------------------------

def _pe_characteristics_off(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e:e + 4] != b"PE\x00\x00":
        raise ValueError("not a PE file")
    return e + 4 + 18  # COFF header -> Characteristics (u16)


def set_pe_characteristic_bit(data, bit):
    """Header fix: OR `bit` into the PE COFF Characteristics field."""
    off = _pe_characteristics_off(data)
    ch = struct.unpack_from("<H", data, off)[0]
    if ch & bit:
        return False, f"already set (Characteristics=0x{ch:04X})"
    struct.pack_into("<H", data, off, ch | bit)
    return True, f"Characteristics 0x{ch:04X} -> 0x{ch | bit:04X}  (byte @0x{off:X})"


def patch_bytes(data, find_hex, repl_hex):
    """Code/data fix: replace a UNIQUE byte pattern with a same-length replacement."""
    find = bytes.fromhex(find_hex)
    repl = bytes.fromhex(repl_hex)
    if len(find) != len(repl):
        return None, "find/repl length mismatch (bug in fix definition)"
    n = data.count(find)
    if n == 0:
        if data.count(repl) >= 1:
            return False, "already applied (replacement already present)"
        return None, "PATTERN NOT FOUND -- binary mismatch, fix NOT applied"
    if n > 1:
        return None, f"pattern not unique ({n} matches) -- refusing to patch"
    i = data.find(find)
    data[i:i + len(repl)] = repl
    return True, f"patched @file-offset 0x{i:X}  ({len(repl)} bytes)"


def _rva_to_offset(data, rva):
    """Map a virtual RVA to a file offset via the PE section table (no hard-coded
    .text base). Raises if the RVA is not inside a section."""
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e:e + 4] != b"PE\x00\x00":
        raise ValueError("not a PE file")
    num_sections = struct.unpack_from("<H", data, e + 6)[0]
    opt_size = struct.unpack_from("<H", data, e + 20)[0]
    sec = e + 24 + opt_size
    for k in range(num_sections):
        s = sec + k * 40
        vaddr = struct.unpack_from("<I", data, s + 12)[0]
        vsize = struct.unpack_from("<I", data, s + 8)[0]
        rawsz = struct.unpack_from("<I", data, s + 16)[0]
        praw = struct.unpack_from("<I", data, s + 20)[0]
        if vaddr <= rva < vaddr + max(vsize, rawsz):
            return praw + (rva - vaddr)
    raise ValueError(f"RVA 0x{rva:X} not in any section")


def patch_all_bytes(data, edits):
    """Apply several UNIQUE-pattern edits as one indivisible fix, on a trial copy first.

    For sibling defects that should never ship half-applied -- e.g. the same partial null guard
    duplicated across two sibling window classes."""
    trial = bytearray(data)
    applied = skipped = 0
    for find_hex, repl_hex in edits:
        r, m = patch_bytes(trial, find_hex, repl_hex)
        if r is None:
            return None, f"{m} -- group NOT applied"
        if r is True:
            applied += 1
        else:
            skipped += 1
    if applied:
        data[:] = trial
        return True, f"{applied} edit(s) patched ({skipped} already applied)"
    return False, f"already applied ({skipped} edits)"


def patch_all_at_va(data, edits):
    """Apply SEVERAL VA-anchored edits as one indivisible fix.

    Used where a single logical change has to touch two sites that write the same output -- e.g.
    a loader's in-memory path and its disk fallback, which must agree on the D3D pool. Applying
    one without the other is a real hazard (the 2026-08 audit found `tex_pool_loc_global` editing
    only the dead half of such a pair, and `tex_pool_loc_inmem` silently leaving its fallback
    MANAGED), so this runs on a trial copy and commits only if every edit resolves."""
    trial = bytearray(data)
    applied = skipped = 0
    for va, find_hex, repl_hex in edits:
        r, m = patch_at_va(trial, va, find_hex, repl_hex)
        if r is None:
            return None, f"@VA 0x{va:X}: {m} -- group NOT applied"
        if r is True:
            applied += 1
        else:
            skipped += 1
    if applied:
        data[:] = trial
        return True, f"{applied} edit(s) patched ({skipped} already applied)"
    return False, f"already applied ({skipped} edits)"


def patch_at_va(data, va, find_hex, repl_hex, imagebase=0x400000):
    """VA-anchored fix: verify + replace bytes at a SPECIFIC virtual address. Use this
    (instead of patch_bytes) when the target byte window is NOT unique in the image, so
    a pattern search can't safely anchor it. Still self-verifies against the original
    bytes at that VA, so it refuses to patch a mismatched binary."""
    find = bytes.fromhex(find_hex)
    repl = bytes.fromhex(repl_hex)
    if len(find) != len(repl):
        return None, "find/repl length mismatch (bug in fix definition)"
    try:
        off = _rva_to_offset(data, va - imagebase)
    except ValueError as ex:
        return None, str(ex)
    cur = data[off:off + len(find)]
    if cur == repl:
        return False, f"already applied (@file 0x{off:X})"
    if cur != find:
        return None, f"BYTES MISMATCH @VA 0x{va:X} (file 0x{off:X}): {cur.hex()} != {find_hex}"
    data[off:off + len(repl)] = repl
    return True, f"patched @VA 0x{va:X} (file 0x{off:X}, {len(repl)} bytes)"


# ---------------------------------------------------------------------------
# .patch CODE-CAVE support
# For the rare fix that CANNOT be expressed as a same-length inline edit -- e.g.
# wrapping a D3D call to add arguments. Adds one new PE section `.patch` at a fixed
# VA and redirects a call/thunk into a cave inside it. Mirrors the proven section
# mechanism in mods .../patch_all.py. This is the ONLY part of the patcher that grows
# the file / changes its structure; everything else is a same-length in-place edit.
# ---------------------------------------------------------------------------
IMAGEBASE          = 0x00400000
PATCH_SECTION_VA   = 0x01630000
PATCH_SECTION_RVA  = 0x01230000   # = PATCH_SECTION_VA - IMAGEBASE
PATCH_SECTION_SIZE = 0x00000800   # room for several caves; one shared section.
                                  # Raised from 0x400 in 2026-08 when the minidump-derived
                                  # guards filled it. Only affects binaries where WE create
                                  # the section (the stock exe has none); a build that already
                                  # ships a 0x400 .patch keeps it, and _patch_section_size()
                                  # below makes an over-large cave fail cleanly there.
#
# OPERATIONAL CONSEQUENCE, measured 2026-08 -- know this before pointing --in at a patched exe.
# ALWAYS PATCH FROM THE PRISTINE Rag2_original.exe (md5 cbeccb38...). A previously-patched
# build carries the legacy 0x400 section, so every cave from 0x410 up refuses, and because
# main() aborts on any FAIL the whole run writes nothing. Verified on both
# b303-2026-07-11/SHIPPING/Rag2.exe (d3941c67) and b303-2022-02-11/SHIPPING/Rag2.exe
# (651efa30): 3 and 2 fixes FAIL respectively, output NOT written. That is the design working
# (it refuses rather than corrupting), but it means those builds cannot be incrementally
# upgraded -- re-run against the pristine exe instead.
#   Two distinct refusal reasons show up there, and both are expected:
#     "cave 957e6d needs 0x432 but this binary's .patch is only 0x400"   <- capacity
#     "BYTES MISMATCH @VA 0xBA13A7 ... c333c0c39090 != 8b45fc8b4018"     <- that region is
#        ALREADY an inlined null-guarded getter in the 2026-07-11 build (see
#        minidump_null_guards_2026_08's why=).
#   EXCEPTION worth knowing: those two builds also carry the CORRUPTING 0x53DC59 encoding on
#   disk (see _NULLVCALL_REPAIR). To heal that without a full re-patch, run
#       python patch_client.py --in <build> --out fixed.exe --only rmi_nullvcall_guards
#   which applies 1 and skips 27, changing exactly the 5 bytes at 0x53DC59 and restoring
#   0x53DC5B for the valid path. Verified, and idempotent on a second run.

# Fixed slot map inside the `.patch` section, so multiple cave fixes coexist. Each cave
# lives at PATCH_SECTION_VA + its offset; keep slots comfortably apart.
#
#   0x000 TEXPOOL_2D     0x300 POSEGUARD      0x440 V_92190E     0x530 G_480D00
#   0x060 CF1            0x320 G_BA13A0       0x480 V_413F70     0x560 G_C38FD3
#   0x0C0 CF4            0x330 G_7A9DB8       0x4C0 V_106D2AD    0x590 G_9D9E26
#   0x120 CF5            0x350 G_7B2210       0x500 V_7B2215
#   0x180 ANISO          0x370 G_65E7E0
#   0x1C0 DEFER          0x390 G_43BB7C
#   0x220 DEFER_DATA     0x3C0 G_DBE320
#   0x280 CONECULL       0x3E0 G_975F01
#                        0x410 G_957E6D
#
# Highest occupied end is 0x5AE (G_9D9E26 + 30 B), so ~0x252 bytes remain in the 0x800
# section. Slots are declared next to the caves that own them; this map is the index.
# EVERY cave writer must call _check_cave_fits() before its first write -- a binary patched
# before 2026-08 carries a 0x400 section and everything from 0x410 up must fail cleanly there.
PATCH_OFF_TEXPOOL_2D = 0x000      # world/model 2D texture -> DEFAULT-pool wrapper


def _has_patch_section(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    n = struct.unpack_from("<H", data, e + 6)[0]
    base = e + 24 + struct.unpack_from("<H", data, e + 20)[0]
    return any(data[base + k * 40: base + k * 40 + 6] == b".patch" for k in range(n))


def _patch_section_size(data):
    """Raw size of the existing `.patch` section, or None when there is none. Cave writers must
    check against THIS, not PATCH_SECTION_SIZE -- a binary patched before the section grew still
    carries the smaller one, and silently running past its end would corrupt whatever follows."""
    e = struct.unpack_from("<I", data, 0x3C)[0]
    n = struct.unpack_from("<H", data, e + 6)[0]
    base = e + 24 + struct.unpack_from("<H", data, e + 20)[0]
    for k in range(n):
        o = base + k * 40
        if data[o:o + 6] == b".patch":
            return struct.unpack_from("<I", data, o + 16)[0]
    return None


def _check_cave_fits(data, slot, cave, label):
    """Return None if `cave` fits in the existing `.patch` at `slot`, else an error string.

    MANDATORY for every cave writer (2026-08 audit): three installers shipped without this and
    one of them was reproduced writing 66 bytes past the section end, silently growing the file
    and leaving SizeOfRawData / VirtualSize / SizeOfImage stale while returning success. The
    binaries this patcher is pointed at do NOT all carry the same section: builds patched before
    2026-08 have a 0x400 `.patch`, current ones have 0x800, and the stock exe has none."""
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    if slot + len(cave) > cap:
        return (f"cave {label} needs 0x{slot + len(cave):X} but this binary's .patch is only "
                f"0x{cap:X} -- re-patch from the stock exe to get the larger one")
    return None


def _ensure_patch_section(data):
    """Return the file offset of the `.patch` section's raw data, creating a zero-filled
    section of PATCH_SECTION_SIZE the first time. Lets several cave fixes each write their
    own slot into one shared section instead of fighting over who creates it."""
    if not _has_patch_section(data):
        ok, msg = _add_patch_section(data, bytes(PATCH_SECTION_SIZE))
        if not ok:
            return None, msg
    return _rva_to_offset(data, PATCH_SECTION_RVA), "ok"


def _add_patch_section(data, section_bytes):
    """Append a `.patch` PE section (chars CODE|EXEC|READ|WRITE) holding section_bytes
    mapped at PATCH_SECTION_VA. Refuses if one already exists or the header has no free
    slot. Returns (ok, msg)."""
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e:e + 4] != b"PE\x00\x00":
        return False, "not a PE file"
    n = struct.unpack_from("<H", data, e + 6)[0]
    sec_tbl = e + 24 + struct.unpack_from("<H", data, e + 20)[0]
    falign = struct.unpack_from("<I", data, e + 0x3C)[0]
    salign = struct.unpack_from("<I", data, e + 0x38)[0]
    hdr_off = sec_tbl + n * 40
    if any(data[hdr_off:hdr_off + 40]):
        return False, "no free slot in the section header (would clobber data)"
    raw_off = (len(data) + falign - 1) & ~(falign - 1)
    vsize = len(section_bytes)
    hdr = bytearray(40)
    hdr[0:8] = b".patch\x00\x00"
    struct.pack_into("<I", hdr, 8, vsize)               # VirtualSize
    struct.pack_into("<I", hdr, 12, PATCH_SECTION_RVA)  # VirtualAddress
    struct.pack_into("<I", hdr, 16, vsize)              # SizeOfRawData
    struct.pack_into("<I", hdr, 20, raw_off)            # PointerToRawData
    struct.pack_into("<I", hdr, 36, 0xE0000020)         # CODE|EXECUTE|READ|WRITE
    data[hdr_off:hdr_off + 40] = hdr
    struct.pack_into("<H", data, e + 6, n + 1)                              # NumberOfSections++
    struct.pack_into("<I", data, e + 0x50, PATCH_SECTION_RVA + salign)      # SizeOfImage
    if len(data) < raw_off:
        data.extend(b"\x00" * (raw_off - len(data)))
    data.extend(section_bytes)
    return True, f"added .patch @VA 0x{PATCH_SECTION_VA:X} (raw 0x{raw_off:X}, {vsize} B)"


# D3DX texture-creation thunks present in the image:
_TEX_INMEM_THUNK     = 0xDF2E2E     # jmp ds:[..InMemory]   -- non-Ex (no D3DPOOL arg)
_TEX_INMEM_EX_THUNK  = 0x1182F40    # jmp ds:[..InMemoryEx] -- has the D3DPOOL arg
_TEX_INMEM_THUNK_ORIG = "ff25385d3501"  # `jmp ds:1355D38h` (the non-Ex thunk, 6 bytes)


def _texpool_default_cave(cave_va):
    """__stdcall wrapper that impersonates D3DXCreateTextureFromFileInMemory(pDevice,
    pSrcData, size, ppTexture) but calls the *Ex overload with Pool=D3DPOOL_DEFAULT(0)
    instead of MANAGED(1) -- drops D3D's system-RAM shadow copy of every texture created
    through the non-Ex path (world/model textures). All other args mirror the non-Ex
    defaults (D3DX_DEFAULT dims/filters, full mip chain, format-from-file)."""
    body = bytes.fromhex(
        "55" "8bec"                      # push ebp; mov ebp,esp
        "ff7514"                          # push [ebp+14h]  ppTexture
        "6a00" "6a00" "6a00"              # pPalette=0; pSrcInfo=0; ColorKey=0
        "6aff" "6aff"                     # MipFilter=-1; Filter=-1 (D3DX_DEFAULT)
        "6a00"                            # Pool = D3DPOOL_DEFAULT  <-- was MANAGED(1)
        "6a00" "6a00"                     # Format=D3DFMT_UNKNOWN; Usage=0
        "6aff" "6aff" "6aff"              # MipLevels=-1; Height=-1; Width=-1 (D3DX_DEFAULT)
        "ff7510" "ff750c" "ff7508"        # SrcDataSize; pSrcData; pDevice
    )
    call_off = len(body)
    rel = (_TEX_INMEM_EX_THUNK - (cave_va + call_off + 5)) & 0xFFFFFFFF
    body += b"\xE8" + struct.pack("<I", rel)         # call ..InMemoryEx (stdcall, cleans 15 args)
    body += bytes.fromhex("8be5" "5d" "c21000")      # mov esp,ebp; pop ebp; ret 10h (clean 4 args)
    return body


def apply_texpool_default(data):
    """Cave fix: force world/model textures to D3DPOOL_DEFAULT to reclaim 32-bit address
    space (removes the MANAGED system-RAM shadow). Writes a wrapper cave into the shared
    `.patch` section and hooks the non-Ex D3DXCreateTextureFromFileInMemory thunk."""
    try:
        thunk_off = _rva_to_offset(data, _TEX_INMEM_THUNK - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[thunk_off] == 0xE9:                       # thunk already redirected
        return False, "already applied (thunk hooked)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_TEXPOOL_2D
    cave = _texpool_default_cave(cave_va)
    err = _check_cave_fits(data, PATCH_OFF_TEXPOOL_2D, cave, "texpool")
    if err:
        return None, err
    slot = sec_off + PATCH_OFF_TEXPOOL_2D
    data[slot:slot + len(cave)] = cave
    jrel = (cave_va - (_TEX_INMEM_THUNK + 5)) & 0xFFFFFFFF
    new_thunk = "e9" + struct.pack("<I", jrel).hex() + "90"   # jmp cave + NOP (fills the 6-byte thunk)
    r, m = patch_at_va(data, _TEX_INMEM_THUNK, _TEX_INMEM_THUNK_ORIG, new_thunk)
    if r is not True:
        return None, f"cave written but thunk hook failed: {m}"
    return True, f".patch cave @VA 0x{cave_va:X}; hooked InMemory thunk -> DEFAULT-pool"


def apply_geo_pool_default(data):
    """Force STATIC NiMesh geometry (character/prop/weapon/armor VB+IB) from D3DPOOL_MANAGED
    to D3DPOOL_DEFAULT, removing its system-RAM shadow. The single NiDX9 buffer creator
    sub_BE3E90 pushes Pool from a computed local var_8 (= sub_BE3960 = 1 MANAGED for static,
    0 DEFAULT for dynamic). Two 3-byte inline edits zero the pushed Pool register at both the
    CreateVertexBuffer and CreateIndexBuffer sites -> Pool becomes 0 unconditionally. Dynamic
    streams already computed 0, so only the static MANAGED case is downgraded."""
    # VB @0xBE3F3C: mov edx,[ebp-8] -> xor edx,edx; nop   (push edx then pushes 0)
    ra, ma = patch_at_va(data, 0xBE3F3C, "8b55f8526a00", "33d290526a00")
    # IB @0xBE404F: mov ecx,[ebp-8] -> xor ecx,ecx; nop   (push ecx then pushes 0)
    rb, mb = patch_at_va(data, 0xBE404F, "8b4df8518b55e4", "33c990518b55e4")
    if ra is None or rb is None:
        return None, f"VB:[{ma}] IB:[{mb}]"
    if ra is False and rb is False:
        return False, "already applied (both Pool pushes zeroed)"
    return True, f"VB {ma}; IB {mb}"


def apply_terrain_stream_2x(data):
    """Double the terrain streaming vertex-buffer pool (700k -> 1.4M verts/buffer, ~53 -> ~106 MB
    of DEFAULT+DYNAMIC VRAM). The per-buffer byte size (push 0xAAE600 @0x80D2F4) and the vertex
    flush ceiling (cmp 0xAAE60 @0x80D40D) MUST move in lockstep (bytes == verts*16), so both are
    applied together; raising the flush cap alone would overflow the buffer."""
    ra, ma = patch_at_va(data, 0x80D2F4, "6800e6aa00", "6800cc5501")   # push 0xAAE600 -> 0x155CC00
    rb, mb = patch_at_va(data, 0x80D40D, "3d60ae0a00", "3dc05c1500")   # cmp 0xAAE60 -> 0x155CC0
    if ra is None or rb is None:
        return None, f"size:[{ma}] cap:[{mb}]"
    if ra is False and rb is False:
        return False, "already applied"
    return True, f"pool size {ma}; flush cap {mb}"


def apply_camera_snappy(data):
    """Cut mouse-look yaw smoothing 0.5 -> 0.75 by repointing both `fld ds:0.5` (the shared
    0.5 literal, read by 100+ funcs, must NOT be edited in place) to the existing read-only
    0.75 float. Two sites (drag cases 2 & 3). Edit ds:0x1357D40 -> ds:0x13C4E2C (disp32 only)."""
    r1, m1 = patch_at_va(data, 0x8AAB24, "d905407d3501", "d9052c4e3c01")
    r2, m2 = patch_at_va(data, 0x8AABA7, "d905407d3501", "d9052c4e3c01")
    if r1 is None or r2 is None:
        return None, f"case2:[{m1}] case3:[{m2}]"
    if r1 is False and r2 is False:
        return False, "already applied"
    return True, f"yaw case2 {m1}; case3 {m2}"


def apply_hw_vp_puredevice(data):
    """Set BehaviorFlags to HARDWARE_VP | PUREDEVICE (0x40|0x10 = 0x50) at the VP-mode
    composer (0xBB0E6C). Works from the clean SWVP byte (`or ecx,20h`) or, if hw_vertex_proc
    is also enabled and ran first, from the already-HWVP byte (`or ecx,40h`)."""
    r, m = patch_bytes(data, "83c9208b5510890aeb4e", "83c9508b5510890aeb4e")
    if r is None:  # clean 0x20 not present -> maybe hw_vertex_proc already made it 0x40
        r, m = patch_bytes(data, "83c9408b5510890aeb4e", "83c9508b5510890aeb4e")
    return r, m


# 28 sibling RMI/UI null-vtable-call crash guards (same family as the 5 individual ones):
# a singleton-null ELSE branch that still virtual-calls through a null object. Each turns the
# branch's `xor reg,reg` into a short/near jmp that skips the null send and reconverges with the
# valid path.
#
# THE ANCHOR IS THE VA, NOT THE IDIOM. `apply=` uses patch_at_va, so the 10-byte idiom is a
# verification stamp, not a search key -- and it has to be, because 6 of these 28 idioms are NOT
# unique file-wide. (Under patch_bytes those 6 would refuse to apply, and worse, the duplicate of
# each is an UNGUARDED sibling site, so a pattern-anchored version would have been ambiguous
# rather than merely refused.) A previous version of this comment claimed the idiom was the
# anchor; it was wrong.
#
# COVERAGE IS 23%, NOT "all but 4". A census of .text for six encodings of this idiom finds 141
# occurrences: 33 guarded (these 28 plus the 5 individual fixes) and 108 UNGUARDED. An earlier
# note here said "4 medium-risk sites that reconverged mid-function/loop were dropped", which
# implied a population of ~32 and near-complete coverage. Those 4 are real, but they are 4 of
# 108, not 4 of 32.
#
# (va, find[10-byte idiom], repl[jmp + dead tail], same length.)
_NULLVCALL_GUARDS = [
    (0x77694B, "33c98b118b92b40a0000", "eb0e8b118b92b40a0000"),
    (0x78806E, "33c98b118b92000a0000", "eb0e8b118b92000a0000"),
    (0x789F97, "33c98b118b92000a0000", "eb0e8b118b92000a0000"),
    (0x787F5B, "33c98b118b92040a0000", "eb118b118b92040a0000"),
    (0x7D8321, "33c98b118b92c0090000", "eb0e8b118b92c0090000"),
    (0x4963EE, "33c98b018b808c010000", "eb0e8b018b808c010000"),
    (0x49639E, "33c98b118b9294010000", "eb5e8b118b9294010000"),
    (0x7A0E42, "33c98b118b9270050000", "eb0e8b118b9270050000"),
    (0x7A1170, "33c98b118b9278050000", "eb0e8b118b9278050000"),
    (0x631141, "33c98b118b928c000000", "eb0b8b118b928c000000"),
    (0x63173B, "33c98b118b92b4000000", "eb118b118b92b4000000"),
    (0x632154, "33c98b118b92400e0000", "eb0b8b118b92400e0000"),
    (0x6329D1, "33c98b118b9200010000", "eb118b118b9200010000"),
    (0x632C18, "33c98b118b9270010000", "eb118b118b9270010000"),
    (0x632A12, "33c98b018b9000010000", "eb0c8b018b9000010000"),
    (0x53D9B8, "33c98b118b92f0010000", "e9ac02000092f0010000"),
    (0x53DA2C, "33c98b118b9220040000", "e9380200009220040000"),
    (0x53DAA0, "33c98b118b9228040000", "e9c40100009228040000"),
    (0x53DB14, "33c98b118b9234040000", "e9500100009234040000"),
    (0x53DB88, "33c98b118b923c040000", "e9dc000000923c040000"),
    (0x53DBF9, "33c98b118b9240040000", "e96b0000009240040000"),
    # 0x53DC59 -- CORRECTED 2026-08. This was `e90b00000092f8050000` (a 5-byte near jmp), and it
    # was the one site in this table whose non-null counterpart the compiler TAIL-MERGED instead of
    # duplicating: `jmp short loc_53DC5B` at 0x53DC57 is the entry point of the VALID path, so the
    # 5-byte encoding overwrote 0x53DC5B..0x53DC5D -- bytes the healthy path jumps INTO. On the
    # common path (CAuth non-null is the normal state) the patched binary executed
    # `add byte ptr [eax],al` -- incrementing the low byte of the live CAuth+0x5C session object's
    # vptr -- then `add byte ptr [edx+5F8h],dl`, then `call edx` with edx never loaded. Memory
    # corruption followed by a wild indirect call. The 2-byte `EB 0E` form reaches the SAME target
    # (0x53DC69) while touching only the 2 bytes of the `xor ecx,ecx` it replaces, so 0x53DC5B is
    # preserved. See _NULLVCALL_REPAIR below for the already-corrupted builds.
    (0x53DC59, "33c98b118b92f8050000", "eb0e8b118b92f8050000"),
    (0x53D9F5, "33c98b018b80f4010000", "eb0b8b018b80f4010000"),
    (0x53DA69, "33c98b018b8024040000", "eb0b8b018b8024040000"),
    (0x53DADD, "33c98b018b802c040000", "eb0b8b018b802c040000"),
    (0x53DB51, "33c98b018b8038040000", "eb0b8b018b8038040000"),
    (0x53DBC5, "33c98b018b8030040000", "eb0b8b018b8030040000"),
    (0x53DC30, "33c98b018b80f4050000", "eb0b8b018b80f4050000"),
]


# Additional SOURCE states this group will accept and repair in place, keyed by VA.
#
# 0x53DC59 shipped with a corrupting 5-byte encoding (see the table entry above) and that encoding
# is already written into builds on disk -- including this patcher's own default `--in`. Those
# builds cannot self-heal from the table alone: patch_at_va would see neither the pristine `find`
# nor the corrected `repl`, return "not found", and abort the whole all-or-nothing group, silently
# dropping all 28 guards on the most common input. Accepting the broken form as a second source
# state converges every build on the safe encoding.
_NULLVCALL_REPAIR = {
    0x53DC59: "e90b00000092f8050000",   # the corrupting 5-byte near-jmp form
}


def apply_rmi_nullvcall_guards(data):
    """Apply the 28 sibling null-vtable-call guards all-or-nothing (on a trial copy first, so a
    binary mismatch aborts cleanly without a half-applied group)."""
    trial = bytearray(data)
    applied = skipped = 0
    failed = []
    for va, fh, rh in _NULLVCALL_GUARDS:
        r, m = patch_at_va(trial, va, fh, rh)
        if r is None and va in _NULLVCALL_REPAIR:
            # Not the pristine bytes and not already correct -- try the known-broken form.
            r, m = patch_at_va(trial, va, _NULLVCALL_REPAIR[va], rh)
        if r is True:
            applied += 1
        elif r is False:
            skipped += 1
        else:
            failed.append(f"0x{va:X}")
    if failed:
        return None, f"{len(failed)} guard site(s) not found ({', '.join(failed[:3])}...) -- group NOT applied"
    if applied:
        data[:] = trial
        return True, f"{applied} null-vtable-call guards applied ({skipped} already)"
    return False, f"already applied ({skipped} guards)"


# ----- CF1/CF4/CF5 NULL-this/First() guards (cave-based; ported from the b303 catalog) -----
PATCH_OFF_CF1        = 0x060
PATCH_OFF_CF4        = 0x0C0
PATCH_OFF_CF5        = 0x120
PATCH_OFF_ANISO      = 0x180
PATCH_OFF_DEFER      = 0x1C0   # deferred-equip-load rate-limiter cave
PATCH_OFF_DEFER_DATA = 0x220   # g_win_start (+0), g_count (+4)
PATCH_OFF_CONECULL   = 0x280   # view-cone cull cave (123 B incl. SLACK/COS2 constants)

# cone_cull tunables baked into the cave (edit here to re-tune, then rebuild):
_CONECULL_SLACK = 50.0   # keep objects within this many world-units BEHIND the camera
                         #   (anti pop-out slack for large/straddling geometry; dot>SLACK -> cull)
_CONECULL_COS2  = 0.1    # cos^2(cone half-angle) for SIDE culling. 0.0 = behind-hemisphere only.
                         #   *** 0.1 IS A LIVE SIDE CULL, NOT "off". *** It keeps within ~72 deg
                         #   half-angle of the view axis (a ~143 deg cone). The Fix why= used to
                         #   claim "default 0 = behind-only"; it never was. Set 0.0 for the
                         #   genuinely conservative behind-only behaviour.
                         #   The forward SIGN is confirmed live (behind-cull worked). The side
                         #   cone at 0.1 is NOT runtime-tested, and this is a cone on the object
                         #   ORIGIN with no bounding-radius term, so near/large geometry can pop
                         #   at screen edges even at this width. Tighten toward the FOV for more
                         #   FPS only if no screen-edge pop-in appears: 0.25 (~60 deg) / 0.5 (~45).


def _rel32(frm_after, to):
    return struct.pack("<i", to - frm_after)


class _Cave:
    """Tiny assembler for code caves with LABELLED short branches.

    WHY THIS EXISTS. Every rel8 displacement in the older caves below was hand-counted, and
    the 2026-08 audit found FIVE of them wrong across four caves in `minidump_null_guards_2026_08b`:
    two bails landed mid-instruction and executed `sbb [ebx+34h],bl` followed by `FF E9` = `jmp ecx`
    through a live heap pointer, and another fell out of its slot into the neighbouring cave and ran
    a `retn 8` on someone else's frame. Two more were correct only by accident -- they happened to
    land on a `js` that was never taken, or on an `xor bh,bh` whose damage a later `pop ebx` undid.

    Hand-counting the displacement is the ONE step of cave construction that nothing in this patcher
    verifies: `_rel32` targets are machine-computed, rel8 targets were typed in by hand. So caves
    written after that audit declare a label and let this class emit the byte. A label that is never
    defined, or that lands out of rel8 range, raises at build time instead of shipping.

    This does not retro-fit the caves that the audit disassembled and confirmed correct
    (CF1/CF4/CF5, POSEGUARD, the four `_g_*` caves of the first minidump batch, TEXPOOL, the four
    `_v_*` vcall caves) -- rewriting verified-correct bytes buys nothing and risks a fresh typo."""

    def __init__(self, cave_va):
        self.va = cave_va
        self._b = bytearray()
        self._pending = []      # (offset of the disp byte, label name)
        self._labels = {}

    def raw(self, hexstr, _comment=None):
        """Emit literal bytes. `_comment` is documentation only."""
        self._b += bytes.fromhex(hexstr.replace(" ", ""))
        return self

    def jcc(self, opcode_hex, label, _comment=None):
        """Short conditional jump (rel8) to a label that may be defined later."""
        self._b += bytes.fromhex(opcode_hex)
        self._pending.append((len(self._b), label))
        self._b += b"\x00"                      # placeholder, back-patched in .bytes()
        return self

    def label(self, name):
        if name in self._labels:
            raise ValueError(f"cave label '{name}' defined twice")
        self._labels[name] = len(self._b)
        return self

    def jmp_abs(self, target_va, _comment=None):
        """5-byte `jmp rel32` to an address in the original image."""
        self._b += b"\xE9" + _rel32(self.va + len(self._b) + 5, target_va)
        return self

    def bytes(self):
        out = bytearray(self._b)
        for off, name in self._pending:
            if name not in self._labels:
                raise KeyError(f"cave label '{name}' is never defined")
            disp = self._labels[name] - (off + 1)
            if not -128 <= disp <= 127:
                raise ValueError(f"cave label '{name}' is {disp} bytes away -- out of rel8 range")
            out[off] = disp & 0xFF
        return bytes(out)


def _cf1_cave(cave_va):
    # SyncMountMotion: guard First() (eax) before `mov eax,[eax+14h]`. eax==0 -> skip to 0x90B571.
    b = bytearray()
    b += bytes.fromhex("85c0")          # test eax,eax
    b += bytes.fromhex("7409")          # jz  +9  -> skip_block (cave+0x0D)
    b += bytes.fromhex("8b4014")        # mov eax,[eax+14h]   (replay)
    b += bytes.fromhex("50")            # push eax
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x90B535)   # jmp reconverge (valid)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x90B571)   # skip_block: jmp reconverge (null)
    return bytes(b)


def _cf4_cave(cave_va):
    # sub_413210: guard this(ecx) AND a2([esp+4]); null -> xor eax,eax; retn 4.
    b = bytearray()
    b += bytes.fromhex("85c9")          # test ecx,ecx
    b += bytes.fromhex("7413")          # jz  +0x13 -> safe_ret (cave+0x17)
    b += bytes.fromhex("8b442404")      # mov eax,[esp+4]  (a2; no frame yet)
    b += bytes.fromhex("85c0")          # test eax,eax
    b += bytes.fromhex("740b")          # jz  +0x0B -> safe_ret
    b += bytes.fromhex("558bec8b4508")  # push ebp; mov ebp,esp; mov eax,[ebp+8]  (replay prologue)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x413216)   # jmp reconverge
    b += bytes.fromhex("33c0c20400")    # safe_ret: xor eax,eax; retn 4
    return bytes(b)


def _cf5_cave(cave_va):
    # sub_8FD2E0: hook the faulting `mov esi,[ecx+180h]` (prologue+frame already ran);
    # this(ecx)==0 -> jmp the function's own FALSE-return epilogue at 0x8FD333.
    b = bytearray()
    b += bytes.fromhex("85c9")          # test ecx,ecx
    b += bytes.fromhex("740b")          # jz  +0x0B -> safe_ret (cave+0x0F)
    b += bytes.fromhex("8bb180010000")  # mov esi,[ecx+180h]  (replay)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x8FD2ED)   # jmp reconverge
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x8FD333)   # safe_ret: jmp FALSE-return epilogue
    return bytes(b)


PATCH_OFF_POSEGUARD  = 0x300   # NiMultiTargetPoseHandler::Update this-pointer guard


def _pose_guard_cave(cave_va):
    """__thiscall shim in front of sub_477300 (a `fstp [ecx+60h]` float setter), installed
    ONLY at the NiMultiTargetPoseHandler::Update call site 0xB19EC1.

    ecx arrives as `[[ebp+var_10]+4]`. In 5 of 31 minidumps that slot held a FLOAT bit
    pattern instead of an object pointer, so the setter wrote through it and died. The shim
    rejects the value and returns; the pose update for that one entry is skipped.

    Discriminator: MSVC `operator new` returns 8-byte-aligned blocks, so a real object here
    is always 8-aligned. All five observed bad values are 4- but NOT 8-aligned. A range test
    was measured and REJECTED: the 0x38000000-0x42000000 band the bad floats live in holds
    116 genuinely committed regions in the dumps, so range would cause false skips."""
    b = bytearray()
    b += bytes.fromhex("85c9")      # test ecx,ecx
    b += bytes.fromhex("740a")      # jz  +0x0A -> skip (cave+0x0E)
    b += bytes.fromhex("f6c107")    # test cl,7            (8-byte alignment)
    b += bytes.fromhex("7505")      # jnz +0x05 -> skip
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x477300)   # jmp the real setter (tail call)
    b += bytes.fromhex("c20400")    # skip: retn 4  (pop the float arg the caller pushed)
    return bytes(b)


def apply_pose_this_guard(data):
    """Redirect the single call at 0xB19EC1 through an 8-alignment guard in the `.patch` cave."""
    HOOK, ORIG = 0xB19EC1, "e83ad495ff"          # call sub_477300
    try:
        off = _rva_to_offset(data, HOOK - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if bytes(data[off:off + 1]) != b"\xE8":
        return None, f"0x{HOOK:X} is not a call (found 0x{data[off]:02X})"
    tgt = HOOK + 5 + struct.unpack_from("<i", data, off + 1)[0]
    if tgt == PATCH_SECTION_VA + PATCH_OFF_POSEGUARD:
        return False, "already applied (call already points at the guard cave)"
    if tgt != 0x477300:
        return None, f"0x{HOOK:X} calls 0x{tgt:X}, expected 0x477300"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_POSEGUARD
    cave = _pose_guard_cave(cave_va)
    err = _check_cave_fits(data, PATCH_OFF_POSEGUARD, cave, "poseguard")
    if err:
        return None, err
    slot = data[sec_off + PATCH_OFF_POSEGUARD: sec_off + PATCH_OFF_POSEGUARD + len(cave)]
    if any(slot) and bytes(slot) != cave:
        return None, f"cave slot 0x{PATCH_OFF_POSEGUARD:X} is occupied"
    data[sec_off + PATCH_OFF_POSEGUARD: sec_off + PATCH_OFF_POSEGUARD + len(cave)] = cave
    new = "e8" + struct.pack("<i", cave_va - (HOOK + 5)).hex()
    r, m = patch_at_va(data, HOOK, ORIG, new)
    if r is not True:
        return None, f"hook @0x{HOOK:X} failed: {m}"
    return True, f"pose this-guard cave @VA 0x{cave_va:X} ({len(cave)} B); 0xB19EC1 -> cave"


PATCH_OFF_G_BA13A0   = 0x320   # unknown_libname_5200 null-this getter
PATCH_OFF_G_7A9DB8   = 0x330   # sub_7A9C00 missing null check before a 42-dword copy
PATCH_OFF_G_7B2210   = 0x350   # UIShowroomWnd::OnUpdate bad vptr
PATCH_OFF_G_65E7E0   = 0x370   # UIAuctionTab1::OnUpdate bad vptr

# Nothing valid lives below the 64 KB Windows null-page reservation, so `< 0x10000` rejects
# a null pointer AND the small-garbage vptrs seen in these dumps (e.g. 0x558) in one test.
_MINPTR = 0x10000


def _g_ba13a0_cave(cave_va):
    """unknown_libname_5200: `mov eax,[ebp-4]; mov eax,[eax+18h]` with this==NULL (3 dumps,
    fault=0x18). Return 0 instead. [ebp-4] is just ecx stored one instruction earlier, so the
    cave reads ecx directly."""
    b = bytearray()
    b += bytes.fromhex("33c0")      # xor eax,eax          (the null result)
    b += bytes.fromhex("85c9")      # test ecx,ecx
    b += bytes.fromhex("7403")      # jz  +3 -> the shared jmp back
    b += bytes.fromhex("8b4118")    # mov eax,[ecx+18h]    (replay, guarded)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0xBA13AD)   # jmp epilogue (mov esp,ebp)
    return bytes(b)


def _g_7a9db8_cave(cave_va):
    """sub_7A9C00: `mov eax,[edx+154h]; mov esi,[eax+15Ch]` then `rep movsd` of 42 dwords
    (2 dumps, fault=0x15C). eax NULL -> take the function's own bail target 0x7A9ECB, which
    the `ja` two instructions earlier already uses."""
    b = bytearray()
    b += bytes.fromhex("8b825401 0000".replace(" ", ""))    # mov eax,[edx+154h]  (replay)
    b += bytes.fromhex("85c0")      # test eax,eax
    b += bytes.fromhex("740b")      # jz  +0x0B -> bail
    b += bytes.fromhex("8bb05c010000")                      # mov esi,[eax+15Ch]  (replay)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7A9DC4)   # jmp back (add esi,220h)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7A9ECB)   # bail: the function's own skip path
    return bytes(b)


def _g_7b2210_cave(cave_va):
    """UIShowroomWnd::OnUpdate: `mov eax,[ecx]; mov edx,[eax+10h]; push ecx; fstp; push 0;
    call edx` -- the vptr came back as 0x558 (fault 0x568), so a plain null test would NOT
    have caught it; the guard is `< 0x10000`. NOTE the pending `fldz` at 0x7B220E: the normal
    path consumes it with `fstp`, so the bail path must pop it (`fstp st(0)`) or the x87 stack
    leaks one slot per skipped frame."""
    b = bytearray()
    b += bytes.fromhex("8b01")      # mov eax,[ecx]        (replay; ecx checked non-null above)
    b += bytes.fromhex("3d00000100")                        # cmp eax,10000h
    b += bytes.fromhex("7208")      # jb  +8 -> bail
    b += bytes.fromhex("8b5010")    # mov edx,[eax+10h]    (replay)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7B2215)   # jmp back (push ecx)
    b += bytes.fromhex("ddd8")      # bail: fstp st(0)     (drop the pending fldz)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7B221D)   # jmp the function's else-branch
    return bytes(b)


def _g_65e7e0_cave(cave_va):
    """UIAuctionTab1::OnUpdate: `mov edx,[ecx]; mov eax,[edx+0F0h]; push ebx; call eax` with
    a bad vptr (fault=0xF0). Skip just this one virtual call and continue with the next one at
    0x65E7EB; the callee cleans its own pushed arg, so skipping push+call is stack-neutral."""
    b = bytearray()
    b += bytes.fromhex("8b11")      # mov edx,[ecx]        (replay)
    b += bytes.fromhex("81fa00000100")                      # cmp edx,10000h
    b += bytes.fromhex("7209")      # jb  +9 -> skip the call
    b += bytes.fromhex("8b82f0000000")                      # mov eax,[edx+0F0h]  (replay)
    b += bytes.fromhex("53")        # push ebx
    b += bytes.fromhex("ffd0")      # call eax
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x65E7EB)   # jmp the next virtual call
    return bytes(b)


_MORE_GUARDS = [
    # (slot, builder, hook_va, original_bytes_hex, hook_len, label)
    (PATCH_OFF_G_BA13A0, _g_ba13a0_cave, 0xBA13A7, "8b45fc8b4018",             6, "ba13a7"),
    (PATCH_OFF_G_7A9DB8, _g_7a9db8_cave, 0x7A9DB8, "8b825401 0000 8bb05c010000".replace(" ", ""), 12, "7a9db8"),
    (PATCH_OFF_G_7B2210, _g_7b2210_cave, 0x7B2210, "8b018b5010",               5, "7b2210"),
    (PATCH_OFF_G_65E7E0, _g_65e7e0_cave, 0x65E7E0, "8b118b82f000000053ffd0",  11, "65e7e0"),
]


def apply_more_null_guards(data):
    """Install the four remaining minidump-derived crash guards as `.patch` caves + hooks."""
    try:
        probe = _rva_to_offset(data, 0xBA13A7 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[probe] == 0xE9:
        return False, "already applied (ba13a7 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    done = []
    for slot, builder, hook_va, orig_hex, hook_len, label in _MORE_GUARDS:
        cave_va = PATCH_SECTION_VA + slot
        cave = builder(cave_va)
        cap = _patch_section_size(data) or PATCH_SECTION_SIZE
        if slot + len(cave) > cap:
            return None, f"cave {label} needs 0x{slot + len(cave):X} but .patch is only 0x{cap:X}"
        occupied = data[sec_off + slot: sec_off + slot + len(cave)]
        if any(occupied) and bytes(occupied) != cave:
            return None, f"cave slot 0x{slot:X} ({label}) is occupied"
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        new = "e9" + struct.pack("<i", cave_va - (hook_va + 5)).hex() + "90" * (hook_len - 5)
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"hook @0x{hook_va:X} ({label}) failed: {m}"
        done.append(label)
    return True, f"4 crash guards installed (.patch): {', '.join(done)}"


PATCH_OFF_G_43BB7C   = 0x390   # CGameActor::GetActiveVehicle garbage iterator value
PATCH_OFF_G_DBE320   = 0x3C0   # sub_DBE320 null this
PATCH_OFF_G_975F01   = 0x3E0   # UIItemHandler::OnItemDoubleClick null edi
PATCH_OFF_G_957E6D   = 0x410   # sub_957E30 strlen over a garbage Src


def _g_43bb7c_cave(cave_va):
    """CGameActor::GetActiveVehicle -- TWO of the 31 dumps, five bytes apart in one function.

    `Iterator_GetValue` returns eax, then the code does `mov ecx,[eax]`, tests ecx for NULL and,
    if non-null, `mov esi,[ecx]` / `mov edx,[esi+8]`. The observed faults were 0xBF59C68A and
    0x0235022E -- garbage that is NOT null, so the existing `test ecx,ecx` passed. Worse, the
    function's own null path (`xor esi,esi; jmp 0x43BB8F`) then reads `[esi+8]` and would fault
    at 8 as well. So every bad value is routed to 0x43BB86 instead: the function's real failure
    epilogue (`xor eax,eax; pop esi; mov esp,ebp; pop ebp; retn`), i.e. "no active vehicle".

    2026-08 AUDIT, two corrections to what this cave claimed:

    (1) THE FIRST TEST CANNOT FAIL, and the comment misnamed what it tests. At the hook, eax is
        the return of Iterator_GetValue, which returns its own out-parameter -- `lea eax,[ebp-0Ch]`
        at 0x43BB6D. It is always a STACK ADDRESS, so `cmp eax,10000h / jb` is never taken. It is
        the ADDRESS of the iterator struct, not the iterator's value. Kept (harmless, and it
        documents the shape) but no longer described as a guard that can fire.

    (2) THIS GUARD IS INERT AGAINST BOTH OF ITS OWN DUMPS. Dump 78444 has ecx=0xBF59C68A and dump
        92568 has esi=0x0235022E -- both far ABOVE 0x10000, so all three tests pass, the replayed
        loads run, and the faults reproduce unchanged. The real root cause is one frame up: the
        actor was freed and its memory reused (the observed values are IEEE floats in [-1,1]), and
        no test on the loaded pointer can repair a freed actor. The cave is retained because the
        third test does catch a genuinely small/null `esi`, but it must not be described as fixing
        the two dumps it was written for. See the Fix `why=` for the honest scope."""
    c = _Cave(cave_va)
    c.raw("3d00000100", "cmp eax,10000h  -- ADDRESS of the iterator out-param; never < 0x10000")
    c.jcc("72", "bail", "jb bail (dead by construction, see docstring)")
    c.raw("8b08", "mov ecx,[eax]  (replay)")
    c.raw("81f900000100", "cmp ecx,10000h")
    c.jcc("72", "bail")
    c.raw("8b31", "mov esi,[ecx]  (replay of 0x43BB8D)")
    c.raw("81fe00000100", "cmp esi,10000h")
    c.jcc("72", "bail")
    c.jmp_abs(0x43BB8F, "jmp back (mov eax,[eax+8])")
    c.label("bail")
    c.jmp_abs(0x43BB86, "the function's own NULL return")
    return c.bytes()


def _g_dbe320_cave(cave_va):
    """sub_DBE320: `mov edx,[ebp+var_8]; mov al,[edx+4]` where var_8 is just `this` stored in the
    prologue -- one dump, fault=4, so this==NULL. Guard at entry and return immediately; the
    function is `retn 8`, so the bail has to clean both args.

    2026-08 audit: the `retn 8` is right, and for the reason the original note did not give --
    sub_DBE320 ends at 0xDBE35F with `retn 8` and uses two stack args ([ebp+8] at 0xDBE329,
    [ebp+0Ch] at 0xDBE344). A `retn 4` would have desynced ESP by 4 on every bail. The bail
    DISPLACEMENT, however, was off by two and landed inside the `jmp 0xDBE326` rel32, on `78 FF`
    = `js -1`; that was harmless only because `test ecx,ecx` with ecx==0 clears SF so the `js` was
    never taken. Correct by accident. Now computed from the label."""
    c = _Cave(cave_va)
    c.raw("85c9", "test ecx,ecx")
    c.jcc("74", "bail", "jz bail")
    c.raw("55", "push ebp        (replay prologue)")
    c.raw("8bec", "mov ebp,esp")
    c.raw("83ec08", "sub esp,8")
    c.jmp_abs(0xDBE326, "jmp back")
    c.label("bail")
    c.raw("c20800", "retn 8  -- cleans both stack args")
    return c.bytes()


def _g_975f01_cave(cave_va):
    """UIItemHandler::OnItemDoubleClick: `mov eax,[edi+4]` with edi==NULL (one dump, fault=4).
    Bail to loc_976E65 -- the same convergence point the branch immediately above already jumps
    to -- so the handler finishes normally with the double-click ignored.

    2026-08 audit: this was the WORST of the five bad displacements. The `jz` landed one byte
    inside the `jmp 0x975F0A` rel32 and executed `sbb byte ptr [ebx+34h],bl` -- a stray WRITE
    through a live heap pointer -- immediately followed by `FF E9` = `jmp ecx`, an indirect jump
    through whatever ecx happened to hold. And it fired on exactly the condition the guard exists
    for (edi==0, which is what its own dump records), so the fix converted a clean null-deref
    access violation into a stray write plus a jump into data: strictly worse than no fix at all.
    The displacement is now computed from the label."""
    c = _Cave(cave_va)
    c.raw("85ff", "test edi,edi")
    c.jcc("74", "bail", "jz bail")
    c.raw("8b4704", "mov eax,[edi+4]  (replay)")
    c.raw("50", "push eax")
    c.raw("68a4000000", "push 0A4h")
    c.jmp_abs(0x975F0A, "jmp back (call GetInstance)")
    c.label("bail")
    c.jmp_abs(0x976E65, "the handler's own exit path")
    return c.bytes()


def _g_957e6d_cave(cave_va):
    """sub_957E30: an inline strlen (`mov dl,[eax]; inc eax; test dl,dl; jnz`) over ecx=Src.
    One dump, fault=0x144 -- ecx was 0x144, garbage but NOT null, so the `cmp ecx,edi` null test
    a few instructions earlier passed. Re-test against 0x10000 and take that same null target,
    loc_95805D.

    2026-08 audit: this guard DOES fire on its own dump (0x144 < 0x10000), so it is the one member
    of this batch with demonstrated efficacy. Its displacement was off by two and landed on
    `32 FF` = `xor bh,bh` before reaching the intended `jmp 0x95805D`; the clobber was then undone
    13 bytes later by that epilogue's `pop ebx`, so it was cosmetic rather than harmful. Fixed for
    hygiene -- the landing bytes are part of a rel32 that would change if the slot or target moved."""
    c = _Cave(cave_va)
    c.raw("81f900000100", "cmp ecx,10000h")
    c.jcc("72", "bail", "jb bail")
    c.raw("8bc1", "mov eax,ecx     (replay)")
    c.raw("c745e80f000000", "mov [ebp-18h],0Fh")
    c.raw("897de4", "mov [ebp-1Ch],edi")
    c.raw("c645d400", "mov byte [ebp-2Ch],0")
    c.jmp_abs(0x957E7D, "jmp back (lea esi,[eax+1])")
    c.label("bail")
    c.jmp_abs(0x95805D, "the function's own empty-Src path")
    return c.bytes()


_MORE_GUARDS_2 = [
    (PATCH_OFF_G_43BB7C, _g_43bb7c_cave, 0x43BB7C, "8b0885c9750b33f6eb09",          10, "43bb7c"),
    (PATCH_OFF_G_DBE320, _g_dbe320_cave, 0xDBE320, "558bec83ec08",                   6, "dbe320"),
    (PATCH_OFF_G_975F01, _g_975f01_cave, 0x975F01, "8b47045068a4000000",             9, "975f01"),
    (PATCH_OFF_G_957E6D, _g_957e6d_cave, 0x957E6D, "8bc1c745e80f000000897de4c645d400", 16, "957e6d"),
]


def apply_more_null_guards_2(data):
    """Second batch of minidump-derived crash guards (`.patch` caves + hooks).

    The capacity check is HOISTED out of the write loop (2026-08 audit). It used to run per-slot
    inside the loop, so on a binary carrying the legacy 0x400 `.patch` section the first three
    caves and hooks were written before the fourth (slot 0x410 + 34 B = 0x432 > 0x400) refused.
    main() aborts without writing the output, so nothing shipped corrupted -- but the in-memory
    image was left half-patched, which is a trap for any future caller that does not abort."""
    try:
        probe = _rva_to_offset(data, 0x43BB7C - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[probe] == 0xE9:
        return False, "already applied (43bb7c hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    built = []
    for slot, builder, hook_va, orig_hex, hook_len, label in _MORE_GUARDS_2:
        cave = builder(PATCH_SECTION_VA + slot)
        if slot + len(cave) > cap:
            return None, (f"cave {label} needs 0x{slot + len(cave):X} but this binary's .patch "
                          f"is only 0x{cap:X} -- re-patch from the stock exe to get the larger one")
        occupied = data[sec_off + slot: sec_off + slot + len(cave)]
        if any(occupied) and bytes(occupied) != cave:
            return None, f"cave slot 0x{slot:X} ({label}) is occupied"
        built.append((slot, cave, hook_va, orig_hex, hook_len, label))
    done = []
    for slot, cave, hook_va, orig_hex, hook_len, label in built:
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        cave_va = PATCH_SECTION_VA + slot
        new = "e9" + struct.pack("<i", cave_va - (hook_va + 5)).hex() + "90" * (hook_len - 5)
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"hook @0x{hook_va:X} ({label}) failed: {m}"
        done.append(label)
    return True, f"4 more crash guards installed (.patch): {', '.join(done)}"


# ---------------------------------------------------------------------------
# Virtual-CALL-target guards (the "driver crash" family).
#
# Different failure from the guards above. There the POINTER was bad and the fault
# address equalled the struct offset. Here the object pointer and the vptr both
# survive the dereference and the CALL TARGET is garbage, so execution leaves the
# image entirely and the dump blames whatever module happens to own that address --
# nvd3dum.dll, nvgpucomp32.dll, or nothing at all when the slot held 0.
#
# The `< 0x10000` test used above is USELESS for this family: 3 of the 4 observed bad
# targets are huge (0x646C6169, 0x676F4677, 0x63486F01). They are ASCII, and two are
# resolvable to exact string literals in this binary (see the Fix `why=`). So these
# guards bound the target to .text instead.
_TEXT_LO = 0x00401000        # first byte of .text
_TEXT_END = 0x0135478F + 1   # one past the last byte of .text
#
# WHAT ACTUALLY JUSTIFIES THIS BOUND (rewritten 2026-08 -- the two proofs originally given
# here were both circular, and are recorded as such so nobody re-derives them):
#   * RETIRED as a tautology: "8/8 legitimate .text targets PROCEED, so the bound is not too
#     tight". Feeding .text addresses to an `is it in .text` predicate is arithmetically
#     incapable of failing. It measures nothing.
#   * RETIRED as circular: "288 RTTI-COL vtables, 2996 slots, ZERO interior slots outside
#     .text". The method was walk-until-a-dword-is-not-in-.text, under which an interior
#     counterexample is UNREACHABLE by construction. The counts reproduce exactly; the
#     conclusion does not follow from them.
#   * WHAT SURVIVES (1): all four guarded sites are __thiscall (`this` in ECX, never pushed),
#     so none can be a COM/D3D interface whose vtable lives in d3d9.dll -- the one case that
#     would legitimately put a call target outside .text. This is the argument that carries
#     the bound.
#   * WHAT SURVIVES (2), and it CAN fail: classifying the 189 terminating dwords of that
#     vtable walk gives 187 next-COL pointers, 44 zero, 17 ASCII, 35 float/UTF-16, 4 small
#     ints, 1 .data pointer -- and ZERO that resemble a code pointer into another module.
#     That is real support for the tail-overrun reading. It still does not establish the
#     interior claim, and the sample is thin where it matters: only 161 of the 288 vtables
#     even have a slot 2 (the GetRTTI slot both big guards call).
# ROBUSTNESS NIT (known, not fixed): this bound is hardcoded while everything else in the
# patcher derives addresses from the section table via _rva_to_offset. Verified harmless
# across the whole client archive, but deriving it from the .text header would remove a
# latent trap for three lines.

PATCH_OFF_V_92190E   = 0x440   # Process_StatueNPCShaders  -- GetRTTI through a stale element
PATCH_OFF_V_413F70   = 0x480   # sub_413F60 (NiObject IsKindOf) -- 159 call sites behind it
PATCH_OFF_V_106D2AD  = 0x4C0   # sub_106D280 -- deleting dtor of the cached UI text object
PATCH_OFF_V_7B2215   = 0x500   # UIShowroomWnd::OnUpdate -- the EIP==0 dump


def _v_target_ok(reg_cmp_lo, reg_cmp_hi, jb_lo, jae_hi):
    """Emit `cmp reg,_TEXT_END / jae bail / cmp reg,_TEXT_LO / jb bail` with caller-supplied
    opcode prefixes so the same test works for EAX (short 0x3D form) and EDX (0x81 /7)."""
    b = bytearray()
    b += reg_cmp_hi + struct.pack("<I", _TEXT_END)
    b += bytes([0x73, jae_hi])
    b += reg_cmp_lo + struct.pack("<I", _TEXT_LO)
    b += bytes([0x72, jb_lo])
    return bytes(b)


def _v_92190e_cave(cave_va):
    """Process_StatueNPCShaders 0x92190E: `mov eax,[edi]; mov edx,[eax+8]; mov ecx,edi; call edx`.
    vtbl+8 is Gamebryo's `NiObject::GetRTTI` -- the caller walks the returned NiRTTI's `m_pkBase`
    chain at +4 against a .data constant, which is what identifies it. In the dump the element was
    non-NULL (the loop's own `cmp edi,esi` passed) but [edi] held 0x01358029, an UNALIGNED pointer
    into .rdata, and [that+8] is the middle of the literal "background/celestialdata" -- "iald".

    Bail target is 0x921A37, NOT the loop's own null-skip at 0x921A3A: the skip path leaves EAX
    holding the container, and by 0x92190E we have already overwritten EAX with the bad vptr, so
    jumping there would run `cmp ecx,[eax+24h]` against garbage. 0x921A37 reloads it from var_4,
    which is exactly what the function's other in-body failure branches do."""
    b = bytearray()
    b += bytes.fromhex("8b07")          # mov eax,[edi]        (replay)
    b += bytes.fromhex("3d00000100")    # cmp eax,10000h       (vptr itself)
    b += bytes.fromhex("7218")          # jb  bail
    b += bytes.fromhex("8b5008")        # mov edx,[eax+8]      (replay)
    b += _v_target_ok(b"\x81\xFA", b"\x81\xFA", 0x05, 0x0D)     # cmp edx,.text bounds
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x921913)   # jmp back (mov ecx,edi)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x921A37)   # bail: reload var_4, next element
    return bytes(b)


def _v_413f70_cave(cave_va):
    """sub_413F60 0x413F70: the same `GetRTTI` call, but inside what the disassembly shows to be
    NiObject::IsKindOf -- it calls vtbl+8, then walks `eax=[eax+4]` comparing against the NiRTTI
    passed in arg_0, returning `this` on a hit and NULL otherwise. 159 call sites feed it
    (Effect_SpawnEffectInstance, Actor_ApplyStatueNPCShaderRecursive, NiAVObject_SetColorExtra-
    DataRecursive, ...), so this one guard covers all of them.

    In the dump [esi] held 0x013580D3 and [that+8] is the middle of the literal "g_dwFogColor" --
    "wFog". The target was executable memory inside nvgpucomp32.dll, which then wrote through it
    into our own read-only .rdata; that is why the dump names the NVIDIA shader compiler.

    Bail is 0x413F6B, the function's own `xor eax,eax; pop esi; pop ebp; retn` -- i.e. exactly what
    it already returns for a NULL object, and every caller tests the result."""
    b = bytearray()
    b += bytes.fromhex("8b06")          # mov eax,[esi]        (replay)
    b += bytes.fromhex("3d00000100")    # cmp eax,10000h
    b += bytes.fromhex("7218")          # jb  bail
    b += bytes.fromhex("8b5008")        # mov edx,[eax+8]      (replay)
    b += _v_target_ok(b"\x81\xFA", b"\x81\xFA", 0x05, 0x0D)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x413F75)   # jmp back (mov ecx,esi)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x413F6B)   # bail: the function's NULL return
    return bytes(b)


def _v_106d2ad_cave(cave_va):
    """sub_106D280 0x106D2AD: `push 1; mov ecx,var_4; mov edx,[ecx]; mov ecx,var_4; mov eax,[edx];
    call eax` -- vtbl[0] with arg 1 is MSVC's scalar deleting destructor, and the function then
    clears [this+0B0h]. So it is "delete the cached object at +0B0h and forget it", reached from
    UIWindow::Destroy and UIStaticText_SetText. The object had already passed TWO null tests
    (0x106D293 and 0x106D2AB) and the call still went to 0x63486F01.

    ROOT SHARPENED 2026-08: "passed two null tests" undersells it. Dump #21 has ECX = 0x3BDD1082 --
    the OBJECT pointer is itself unaligned garbage, not a live object with a corrupted vptr. That
    leans use-after-free with heap reuse rather than type confusion for this one.

    Hooked one instruction EARLIER than the fault, at the `push 1`, so the bail does not have to
    unwind a pushed argument: it drops straight into 0x106D2C0, the path the function takes when
    the object is NULL. var_10 is dead (the function returns void) and [this+0B0h] is still
    cleared at 0x106D2CA, so the worst case is one leaked UI text object instead of a crash."""
    b = bytearray()
    b += bytes.fromhex("8b4dfc")        # mov ecx,[ebp+var_4]   (the object)
    b += bytes.fromhex("8b11")          # mov edx,[ecx]         (vptr)
    b += bytes.fromhex("81fa00000100")  # cmp edx,10000h
    b += bytes.fromhex("721a")          # jb  bail
    b += bytes.fromhex("8b02")          # mov eax,[edx]         (vtbl slot 0)
    b += _v_target_ok(b"\x3D", b"\x3D", 0x0A, 0x11)             # cmp eax,.text bounds
    b += bytes.fromhex("6a01")          # push 1                (replay, AFTER the tests)
    b += bytes.fromhex("8b4dfc")        # mov ecx,[ebp+var_4]   (this)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x106D2B9)  # jmp back (call eax)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x106D2C0)  # bail: the function's own NULL path
    return bytes(b)


def _v_7b2215_cave(cave_va):
    """UIShowroomWnd::OnUpdate 0x7B2215: the EIP==0 dump. This is the SAME site the existing
    `minidump_null_guards_2026_08` guard covers at 0x7B2210, one step further along. The two
    guards compose: that cave's `jmp 0x7B2215` lands on this hook.

    ROOT CORRECTED 2026-08: this used to say 'the vptr was fine (0x3E70BDE4) and vtbl+10h held 0',
    i.e. that a legitimate object had an unimplemented method. It did not. Dump #16 has
    ECX = 0x41737B54 and EAX = [ECX] = 0x3E70BDE4 -- as a float that is 0.235, and the object
    pointer it came from is equally garbage. This is the TAIL OF THE SAME garbage-object bug the
    0x7B2210 guard covers, one instruction later; the existing `< 0x10000` vptr test simply
    cannot catch a garbage value that large. The guard still fires correctly, but the cause is a
    bad object, not a null slot.

    Same pending-`fldz` hazard as its neighbour: 0x7B220E pushes a zero the normal path consumes
    with `fstp [esp]`, so the bail pops the x87 stack before taking 0x7B221D."""
    b = bytearray()
    b += _v_target_ok(b"\x81\xFA", b"\x81\xFA", 0x0B, 0x13)     # cmp edx,.text bounds
    b += bytes.fromhex("51")            # push ecx              (replay: reserve the float slot)
    b += bytes.fromhex("d91c24")        # fstp [esp]            (replay: consume the fldz)
    b += bytes.fromhex("6a00")          # push 0                (replay)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7B221B)   # jmp back (call edx)
    b += bytes.fromhex("ddd8")          # bail: fstp st(0)      (drop the pending fldz)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0x7B221D)   # jmp the function's else-branch
    return bytes(b)


_VCALL_GUARDS = [
    (PATCH_OFF_V_92190E,  _v_92190e_cave,  0x92190E,  "8b078b5008",     5, "92190e"),
    (PATCH_OFF_V_413F70,  _v_413f70_cave,  0x413F70,  "8b068b5008",     5, "413f70"),
    (PATCH_OFF_V_106D2AD, _v_106d2ad_cave, 0x106D2AD, "6a018b4dfc8b11", 7, "106d2ad"),
    (PATCH_OFF_V_7B2215,  _v_7b2215_cave,  0x7B2215,  "51d91c246a00",   6, "7b2215"),
]


def apply_vcall_target_guards(data):
    """Install the four virtual-CALL-target guards as `.patch` caves + hooks."""
    try:
        probe = _rva_to_offset(data, 0x413F70 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[probe] == 0xE9:
        return False, "already applied (413f70 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    done = []
    for slot, builder, hook_va, orig_hex, hook_len, label in _VCALL_GUARDS:
        cave_va = PATCH_SECTION_VA + slot
        cave = builder(cave_va)
        if slot + len(cave) > cap:
            return None, (f"cave {label} needs 0x{slot + len(cave):X} but this binary's .patch "
                          f"is only 0x{cap:X} -- re-patch from the stock exe to get the larger one")
        occupied = data[sec_off + slot: sec_off + slot + len(cave)]
        if any(occupied) and bytes(occupied) != cave:
            return None, f"cave slot 0x{slot:X} ({label}) is occupied"
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        new = "e9" + struct.pack("<i", cave_va - (hook_va + 5)).hex() + "90" * (hook_len - 5)
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"hook @0x{hook_va:X} ({label}) failed: {m}"
        done.append(label)
    return True, f"4 virtual-call-target guards installed (.patch): {', '.join(done)}"


# ---------------------------------------------------------------------------
# STRUCTURE-INVARIANT guards (2026-08). A third failure family, distinct from both of the
# above.
#
#   * the `< 0x10000` guards catch a pointer that is null-ish.
#   * the `.text`-bound guards catch a CALL TARGET that left the image.
#   * these two catch a pointer that is STRUCTURALLY IMPOSSIBLE -- misaligned for the type it
#     is supposed to address. That matters because both of their recorded faults involve
#     values far above 0x10000 that no threshold test can see: a `std::vector::begin()` that
#     is 2 mod 4, and an `NiTArray::m_pBase` that is 1 mod 4. A real heap allocation for a
#     4-byte-element array cannot be either.
#
# Both are one-crash (N=1) guards. They are shipped as GUARDS, not as root-cause fixes: an
# alignment test says "this pointer is corrupt", not "here is why it went corrupt".
PATCH_OFF_G_480D00   = 0x530   # CEffectMgr::UpdateTargetEffects -- misaligned vector begin
PATCH_OFF_G_C38FD3   = 0x560   # NiTArray::GetIndexOf -- misaligned m_pBase
PATCH_OFF_G_9D9E26   = 0x590   # UIItemHandler_OnInputEvent -- unguarded [+0x98]->[+0x118]


def _g_480d00_cave(cave_va):
    """CEffectMgr::UpdateTargetEffects (0x4803A0) -- dump Rag2.exe.86860.1784978011, a WRITE
    fault at 0x480D00 (`mov dword ptr [ecx+28h],0`) with ecx=0x10000 and fault=0x10028.

    THE INVARIANT. The function walks a std::map<int, std::vector<T*>>; the node layout is
    pinned by the inlined ++it at 0x480D35 (_Left@0 / _Parent@4 / _Right@8 / key@0xC /
    vector{begin@0x10, end@0x14, cap@0x18} / _Color@0x20 / _Isnil@0x21). The loop cursor ebx
    starts at begin=[edi+0x10] and advances ONLY by `add ebx,4` at 0x480D29, so begin is
    congruent to ebx mod 4. The recorded ebx = 0x015493BA is 2 mod 4, therefore begin was 2
    mod 4 -- misaligned no matter how far the cursor had advanced. A real vector<T*>::begin()
    is always 4-aligned, so `test byte ptr [edi+0x10],3` catches this fault and CANNOT fire on
    a healthy vector. `begin > end` is added as a second impossible-on-healthy test.

    Corroboration from the image itself: ebx = 0x015493BA lies in real read-only .rdata
    (0x1355000-0x1599DD8), and the dword there is literally 0x00010000 -- the "object pointer"
    the code then dereferenced was a constant baked into read-only data, read off-alignment.

    ONE HOOK COVERS FOUR LOOPS. The function has four identical vector loops selected by the
    node key (7 / 0Bh / 0Ch / default). 0x4803E0 is their shared head, and all four read the
    vector from [edi+0x10]/[edi+0x14], so validating here validates once for all of them.

    ORDER IS LOAD-BEARING. The 6-byte hook covers `mov eax,[edi+0Ch]` + `cmp eax,7`, and the
    very next instruction (0x4803E6 `jne 0x4805B2`) consumes THAT cmp's flags. So every guard
    test must run BEFORE the replayed cmp, and nothing may touch flags between it and the jump
    back -- otherwise the four-way key dispatch goes down the wrong arm. eax is dead on entry
    (it is loaded here), so using it as scratch is free."""
    c = _Cave(cave_va)
    c.raw("f6471003", "test byte ptr [edi+10h],3   -- begin misaligned => node is corrupt")
    c.jcc("75", "bail", "jnz bail")
    c.raw("8b4710", "mov eax,[edi+10h]           -- begin")
    c.raw("3b4714", "cmp eax,[edi+14h]           -- begin > end is impossible on a healthy vector")
    c.jcc("77", "bail", "ja bail")
    c.raw("8b470c", "mov eax,[edi+0Ch]           (replay)")
    c.raw("83f807", "cmp eax,7                   (replay -- MUST be last: 0x4803E6 reads these flags)")
    c.jmp_abs(0x4803E6, "jmp back")
    c.label("bail")
    c.jmp_abs(0x480D35, "the inlined ++it: skip this corrupt node, keep iterating")
    return c.bytes()


def _g_c38fd3_cave(cave_va):
    """sub_C38F90 -- shape is Gamebryo's NiTArray::GetIndexOf(&value, startIdx) (an INFERENCE
    from the code shape; the IDB carries it as sub_*). One dump: `mov edx,[edx+eax*4]` at
    0xC38FD3 with index 0 and m_pBase = 0x03F83C4D.

    THE INVARIANT. The layout is proved by its sibling sub_C38ED0 (SetAt): m_pBase@+4,
    m_usSize@+0x0A (u16), m_usESize@+0x0C. The faulting access is scale-4 indexed, so the
    elements are 4 bytes wide -- and 0x03F83C4D is 1 mod 4, which cannot be the base of such
    an array. Note this alignment premise is inferred from the INDEXING, not from the
    allocator, and it rejects only 3 of 4 uniformly random garbage bases. It is a guard.

    THE BAIL IS THE FUNCTION'S OWN. 0xC38FE2 = `or eax,0FFFFFFFFh; mov esp,ebp; pop ebp;
    retn 8` -- the "not found" return, already the target of two existing branches (0xC38F9F
    je, 0xC38FC4 jge). Returning -1 also makes the caller sub_C38DB0 do `cmp [ebp-4],-1;
    je 0xC38DED`, which SKIPS the following call to sub_C38ED0 -- so the guard removes a
    further read through the same bad base. (An earlier note claimed it removes a WRITE; the
    accesses through m_pBase at 0xC38F30/0xC38F54 are `cmp dword[ecx+edx*4],0`, i.e. reads.
    A write past 0xC38F6C may exist but was not located -- do not claim it.)
    The loop in sub_C38D50 is count-bounded, so bailing cannot hang.

    Context: the caller chain is a particle-system teardown on a corrupt or already-freed
    simulator (sub_C38DB0 <- sub_C38D50 <- ... <- NiPSSimulator::Detach <-
    NiPSMeshParticleSystem::DestructorThunk <- NiRefObject::DeleteThis), this = 0x6BB08BCC.

    `jbe` rather than `jb`: costs nothing on a new guard and covers 0x10000 exactly, which the
    sibling 0x480D00 dump shows is a value that really does occur."""
    c = _Cave(cave_va)
    c.raw("8b5104", "mov edx,[ecx+4]             (replay) -- m_pBase")
    c.raw("f6c203", "test dl,3                   -- 4-byte elements cannot have a base 1..3 mod 4")
    c.jcc("75", "bail", "jnz bail")
    c.raw("81fa00000100", "cmp edx,10000h")
    c.jcc("76", "bail", "jbe bail")
    c.raw("8b4d08", "mov ecx,[ebp+8]             (replay)")
    c.jmp_abs(0xC38FD3, "jmp back")
    c.label("bail")
    c.jmp_abs(0xC38FE2, "the function's own 'not found' return (or eax,-1; retn 8)")
    return c.bytes()


def _g_9d9e26_cave(cave_va):
    """UIItemHandler_OnInputEvent (0x9D8EB0) 0x9D9E26: `mov edx,[esi+98h]; mov eax,[edx+118h]`.

    EVIDENCE IS INFERENTIAL-FROM-SIBLING, NOT CRASH-DERIVED -- say so plainly. No dump lands
    here. What makes it worth guarding is that it is the SAME field chain, with the same
    {2,3,5,8} enum compare set, as the CONFIRMED 3-dump crash at 0x9CC85A
    (`mov eax,[esi+98h]` ... `cmp dword ptr [eax+118h],8`), where the dumps prove eax==0 at
    runtime -- and that chain is already fixed by `null_check_jumps_into_deref_9cc843`.

    UNIQUENESS IS MEASURED, and independently reproduced: of the `mov r,[X+0x98]` sites in the
    image, 186 null-test immediately, and this is the ONLY one that then dereferences +0x118
    with no guard at all. The same function null-tests [esi+98h] at six OTHER sites
    (0x9D8FFC, 0x9D9048, 0x9D9052, 0x9D90EF, 0x9D90F9, 0x9D9C70) -- so the omission here reads
    as an oversight, not a proven invariant.

    The two functions are siblings in one UI item-handler family: sub_9CC520 is vtable-only
    (0x13D9174, 0x13D96CC) and UIItemHandler_OnInputEvent is vtable-referenced at 0x13DA154 and
    thunked from 0x9CAA64/0x9CAB74/0x9CACA4/0x9CADD4 -- the same 0x9CAxxx-0x9CCxxx block.

    Hook safety measured: scanning every direct branch/call in .text, ZERO land strictly inside
    the 12-byte hook, and the hook point is itself a basic-block head (preceded by the jmp at
    0x9D9E21). Bail 0x9D9E45 is already the target of the jne at 0x9D9E3A and begins
    `mov eax,1; mov [esi+134h],eax; jmp loc_9D91E5`. Resume 0x9D9E32 is a valid boundary
    (`cmp eax,2`), and it sets its own flags so the guard's cmp cannot leak into it."""
    c = _Cave(cave_va)
    c.raw("8b9698000000", "mov edx,[esi+98h]     (replay)")
    c.raw("81fa00000100", "cmp edx,10000h")
    c.jcc("72", "bail", "jb bail")
    c.raw("8b8218010000", "mov eax,[edx+118h]    (replay)")
    c.jmp_abs(0x9D9E32, "jmp back")
    c.label("bail")
    c.jmp_abs(0x9D9E45, "the function's own 'no item' continuation")
    return c.bytes()


_STRUCT_GUARDS = [
    (PATCH_OFF_G_480D00, _g_480d00_cave, 0x4803E0, "8b470c83f807",             6, "480d00"),
    (PATCH_OFF_G_C38FD3, _g_c38fd3_cave, 0xC38FCD, "8b51048b4d08",             6, "c38fd3"),
    (PATCH_OFF_G_9D9E26, _g_9d9e26_cave, 0x9D9E26, "8b96980000008b8218010000", 12, "9d9e26"),
]


def apply_struct_invariant_guards(data):
    """Install the three structure-invariant crash guards (`.patch` caves + hooks).
    Capacity and occupancy are checked for EVERY slot before the first byte is written."""
    try:
        probe = _rva_to_offset(data, 0x4803E0 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[probe] == 0xE9:
        return False, "already applied (480d00 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    built = []
    for slot, builder, hook_va, orig_hex, hook_len, label in _STRUCT_GUARDS:
        cave = builder(PATCH_SECTION_VA + slot)
        err = _check_cave_fits(data, slot, cave, label)
        if err:
            return None, err
        occupied = data[sec_off + slot: sec_off + slot + len(cave)]
        if any(occupied) and bytes(occupied) != cave:
            return None, f"cave slot 0x{slot:X} ({label}) is occupied"
        built.append((slot, cave, hook_va, orig_hex, hook_len, label))
    done = []
    for slot, cave, hook_va, orig_hex, hook_len, label in built:
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        cave_va = PATCH_SECTION_VA + slot
        new = "e9" + struct.pack("<i", cave_va - (hook_va + 5)).hex() + "90" * (hook_len - 5)
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"hook @0x{hook_va:X} ({label}) failed: {m}"
        done.append(label)
    return True, f"3 structure-invariant guards installed (.patch): {', '.join(done)}"


PATCH_OFF_AF_GLOBAL  = 0x5C0   # SetSamplerState chokepoint: LINEAR -> ANISOTROPIC + MaxAniso

_SSS = 0x45F740   # CRenderStateMgr::SetSamplerState(sampler, type, value), thiscall, retn 0Ch


def _af_global_cave(cave_va):
    """Hook on the ONE place every sampler-state write funnels through.

    Why here and not sub_DDC350: sub_DDC350 has exactly one caller (sub_DD5D40) whose own callers
    all sit in the SpeedTree band 0xDC1B10..0xDC3EE0 -- it configures VEGETATION only, never the
    terrain or world meshes. The world's sampler state is declared in the .fx assets (211/211
    samplers set Min/Mag/MipFilter) and routed through ID3DXEffectStateManager into THIS function,
    so anything written in fixed-function land is overwritten on every effect pass. 0x45F740 is
    downstream of both, which is why the upgrade belongs here.

    On entry (before the prologue) esp+0 = retaddr, +4 = sampler, +8 = type, +0xC = value; ecx is
    the CRenderStateMgr. Index math in the original (`edx*7*2 + ebx`) confirms arg order
    sampler/type/value.

    Behaviour: rewrite MAGFILTER(5) and MINFILTER(6) from LINEAR(2) to ANISOTROPIC(3), and on the
    MIN call also issue MAXANISOTROPY(10)=16 for the same sampler -- without that, ANISOTROPIC
    degenerates to LINEAR and the whole thing buys nothing. MIPFILTER(7) is deliberately NOT
    touched: ANISOTROPIC is not a legal MIPFILTER value.

    The nested call re-enters this same hook with type=10, matches neither 5 nor 6, and falls
    straight through -- no recursion beyond one level. ecx is saved across it because the callee is
    thiscall and may clobber it; the callee cleans its own 12 bytes, so esp balances."""
    b = bytearray()
    fixups = []                             # (offset of the rel8 byte, "label")
    labels = {}

    def jcc(opcode_hex, label):
        """Emit a short conditional jump with a rel8 to be computed in pass 2.

        Hand-typing these displacements is precisely how the previous batch of caves got
        it wrong, so they are never written by hand here."""
        b.extend(bytes.fromhex(opcode_hex))
        fixups.append((len(b), label))
        b.append(0x00)

    b += bytes.fromhex("8b442408")          # mov eax,[esp+8]        type
    b += bytes.fromhex("83f806")            # cmp eax,6              MINFILTER
    jcc("74", "maybe")                      # je  maybe
    b += bytes.fromhex("83f805")            # cmp eax,5              MAGFILTER
    jcc("75", "tail")                       # jne tail
    labels["maybe"] = len(b)
    b += bytes.fromhex("837c240c02")        # cmp dword [esp+0Ch],2  LINEAR?
    jcc("75", "tail")                       # jne tail
    b += bytes.fromhex("c744240c03000000")  # mov dword [esp+0Ch],3  -> ANISOTROPIC
    b += bytes.fromhex("83f806")            # cmp eax,6              only on the MIN call
    jcc("75", "tail")                       # jne tail
    b += bytes.fromhex("51")                # push ecx               (save this)
    b += bytes.fromhex("8b542408")          # mov edx,[esp+8]        sampler (esp moved by the push)
    b += bytes.fromhex("6a10")              # push 16                MaxAnisotropy
    b += bytes.fromhex("6a0a")              # push 10                D3DSAMP_MAXANISOTROPY
    b += bytes.fromhex("52")                # push edx
    b += b"\xE8" + _rel32(cave_va + len(b) + 5, _SSS)   # call SetSamplerState (callee cleans 12)
    b += bytes.fromhex("59")                # pop ecx
    labels["tail"] = len(b)                 # replay the overwritten prologue and rejoin
    b += bytes.fromhex("55")                # push ebp
    b += bytes.fromhex("8bec")              # mov ebp,esp
    b += bytes.fromhex("8b5508")            # mov edx,[ebp+8]
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, _SSS + 6)

    for at, label in fixups:                # pass 2
        disp = labels[label] - (at + 1)
        if not 0 <= disp <= 127:
            raise ValueError(f"rel8 to {label} out of range ({disp})")
        b[at] = disp
    return bytes(b)


def apply_af_global(data):
    """Install the SetSamplerState-chokepoint AF upgrade."""
    try:
        off = _rva_to_offset(data, _SSS - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (chokepoint hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_AF_GLOBAL
    cave = _af_global_cave(cave_va)
    if PATCH_OFF_AF_GLOBAL + len(cave) > cap:
        return None, (f"cave needs 0x{PATCH_OFF_AF_GLOBAL + len(cave):X} but .patch is only "
                      f"0x{cap:X} -- re-patch from the stock exe")
    occupied = data[sec_off + PATCH_OFF_AF_GLOBAL: sec_off + PATCH_OFF_AF_GLOBAL + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_AF_GLOBAL:X} is occupied"
    data[sec_off + PATCH_OFF_AF_GLOBAL: sec_off + PATCH_OFF_AF_GLOBAL + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (_SSS + 5)).hex() + "90"
    r, m = patch_at_va(data, _SSS, "558bec8b5508", new)
    if r is not True:
        return None, f"chokepoint hook failed: {m}"
    return True, f"AF chokepoint cave @0x{cave_va:X} ({len(cave)} B); 0x45F740 hooked"


# ---------------------------------------------------------------------------
# RE-ENTRANT-TEARDOWN guard (2026-08). UIItemSlot_AttachTooltip (0x9DA420) makes a chain of
# virtual calls on the tooltip object it stores at *(this+0x15C). The `vtable[+8](obj,2,..)` call
# at 0x9DA518 re-enters and writes 0 to that slot, so the next reload+deref at 0x9DA51D/0x9DA523
# makes a virtual call through NULL. Guard the reload; on NULL, skip the 2nd show call.
PATCH_OFF_ATT_9DA51D = 0x600   # UIItemSlot_AttachTooltip: re-entrant NULL tooltip object


def _att_9da51d_cave(cave_va):
    """UIItemSlot_AttachTooltip 0x9DA51D..0x9DA523.

    SITE (live, NOT from the 31-dump set): a Rag2.exe attached to x64dbg faulted at EIP
    0x9DA523 `mov eax,[ecx]` with ECX=0, during rapid item-slot refresh driven by the
    auto-pyramid. ECX had just been loaded `mov ecx,[esi+15Ch]` (the slot's tooltip object).
    A debugger was attached, so the crash reporter wrote no dump -- provenance is the live
    register state, N=1.

    WHY NULL. The function sets *(this+0x15C)=a2 at 0x9DA468 (a2 is null-checked non-null at
    entry) and dereferences it four times. It survives the first three. The
    `vtable[+8](obj, 2, [esi+150h]==0)` call at 0x9DA518 tears the tooltip object down and
    writes 0 to *(this+0x15C) -- the SAME store the function's own cleanup takes at 0x9DA450 --
    so the reload at 0x9DA51D for the second `vtable[+8](obj, 1, ..)` call reads NULL and
    0x9DA523 dies. Fault is a constant 0 (not a large garbage value), so `test`/`jz` suffices;
    no 0x10000 threshold is needed.

    HOOK. 6 bytes `mov ecx,[esi+15Ch]` at 0x9DA51D. The only xref into 0x9DA51D..0x9DA522 is
    fall-through from 0x9DA51A (mid-basic-block, nothing branches in). EDX (`[esi+7Ch]`,
    consumed by the replayed `and edx,1`) is loaded at 0x9DA51A, before the hook, so it is live.

    BAIL. 0x9DA530 (`mov ecx,[esi+1D0h]`) -- the next block, independent of *(this+0x15C) and
    already null-guarded. The epilogue's `mov eax,[esi+15Ch]` then returns 0, which the original
    returns anyway. Cost of the guard firing: the tooltip's final show-state is not applied for
    that one hover frame. Never a crash."""
    c = _Cave(cave_va)
    c.raw("8b8e5c010000", "mov ecx,[esi+15Ch]  (replay) -- tooltip object")
    c.raw("85c9", "test ecx,ecx")
    c.jcc("74", "bail", "jz bail -- torn down by the vtable[+8](,2,) call; skip the 2nd show call")
    c.raw("8b01", "mov eax,[ecx]       (replay 0x9DA523) -- vptr")
    c.raw("8b4008", "mov eax,[eax+8]    (replay 0x9DA525) -- vtable[+8]")
    c.raw("83e201", "and edx,1          (replay 0x9DA528)")
    c.raw("52", "push edx")
    c.raw("6a01", "push 1")
    c.jmp_abs(0x9DA52E, "jmp back to `call eax`")
    c.label("bail")
    c.jmp_abs(0x9DA530, "the [esi+1D0h] block -- already null-guarded")
    return c.bytes()


def apply_attach_tooltip_guard(data):
    """Install the UIItemSlot_AttachTooltip re-entrant-NULL guard as a `.patch` cave + hook."""
    try:
        off = _rva_to_offset(data, 0x9DA51D - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (0x9DA51D hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_ATT_9DA51D
    cave = _att_9da51d_cave(cave_va)
    if PATCH_OFF_ATT_9DA51D + len(cave) > cap:
        return None, (f"cave needs 0x{PATCH_OFF_ATT_9DA51D + len(cave):X} but .patch is only "
                      f"0x{cap:X} -- re-patch from the stock exe")
    occupied = data[sec_off + PATCH_OFF_ATT_9DA51D: sec_off + PATCH_OFF_ATT_9DA51D + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_ATT_9DA51D:X} is occupied"
    data[sec_off + PATCH_OFF_ATT_9DA51D: sec_off + PATCH_OFF_ATT_9DA51D + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (0x9DA51D + 5)).hex() + "90"
    r, m = patch_at_va(data, 0x9DA51D, "8b8e5c010000", new)
    if r is not True:
        return None, f"hook @0x9DA51D failed: {m}"
    return True, f"AttachTooltip guard cave @0x{cave_va:X} ({len(cave)} B); 0x9DA51D hooked"


# ---------------------------------------------------------------------------
# HandlePolishingPacket NULL guard (2026-08). The S2C polishing/honing packet handler loads
# *(this+336) and immediately dereferences it (`mov eax,[eax+15Ch]`) BEFORE its own null check
# on the result -- so a NULL polishing context faults reading [0x15C].
PATCH_OFF_G_6D0E0B = 0x640   # HandlePolishingPacket: NULL *(this+336) deref


def _g_6d0e0b_cave(cave_va):
    """HandlePolishingPacket 0x6D0E0B..0x6D0E11 -- `mov eax,[ebx+150h]; mov eax,[eax+15Ch]`.

    SITE: from an UNPATCHED-client dump set ("Nama"), EIP 0x6D0E11, read fault 0x15C. The code
    dereferences *(this+336) (loading [.+0x15C]) before the function's own `if(!v5) return 0`
    check, so when *(this+336) is NULL it reads [0 + 0x15C] and dies.

    GUARD: after loading eax = *(this+336), if NULL bail to the function's OWN return-0 path at
    0x6D0D7D (`xor al,al; pop ebx; <security-cookie epilogue>`). That path is reachable because
    the hook sits after the prologue `push ebx` @0x6D0D76, so its `pop ebx` balances the stack.
    eax is dead on entry here (it is loaded at the hook), so using it as the test reg is free.

    HOOK: 6 bytes `mov eax,[ebx+150h]` @0x6D0E0B. Only xref in is fall-through from 0x6D0E09
    (mid-basic-block); 0x6D0E17 (resume) and 0x6D0D7D (bail) are instruction heads."""
    c = _Cave(cave_va)
    c.raw("8b8350010000", "mov eax,[ebx+150h]  (replay) -- *(this+336) polishing context")
    c.raw("85c0", "test eax,eax")
    c.jcc("74", "bail", "jz bail -- context is NULL")
    c.raw("8b805c010000", "mov eax,[eax+15Ch]  (replay 0x6D0E11)")
    c.jmp_abs(0x6D0E17, "jmp back")
    c.label("bail")
    c.jmp_abs(0x6D0D7D, "the function's own return 0 (xor al,al; pop ebx; cookie epilogue)")
    return c.bytes()


def apply_polishing_null_guard(data):
    """Install the HandlePolishingPacket NULL guard as a `.patch` cave + hook."""
    try:
        off = _rva_to_offset(data, 0x6D0E0B - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (0x6D0E0B hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_G_6D0E0B
    cave = _g_6d0e0b_cave(cave_va)
    if PATCH_OFF_G_6D0E0B + len(cave) > cap:
        return None, (f"cave needs 0x{PATCH_OFF_G_6D0E0B + len(cave):X} but .patch is only "
                      f"0x{cap:X} -- re-patch from the stock exe")
    occupied = data[sec_off + PATCH_OFF_G_6D0E0B: sec_off + PATCH_OFF_G_6D0E0B + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_G_6D0E0B:X} is occupied"
    data[sec_off + PATCH_OFF_G_6D0E0B: sec_off + PATCH_OFF_G_6D0E0B + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (0x6D0E0B + 5)).hex() + "90"
    r, m = patch_at_va(data, 0x6D0E0B, "8b8350010000", new)
    if r is not True:
        return None, f"hook @0x6D0E0B failed: {m}"
    return True, f"Polishing NULL guard cave @0x{cave_va:X} ({len(cave)} B); 0x6D0E0B hooked"


# ---------------------------------------------------------------------------
# Two more NULL guards (2026-08) from the "Zhong" dump set (192 stock-client dumps) -- the two
# top recurring sites not already covered by other guards.
PATCH_OFF_G_90B447 = 0x680   # SyncMountMotion: List::First(vehicle) NULL -> [+0x14]
PATCH_OFF_G_7A356B = 0x6C0   # EquipmentUI_UpdateUI: skill-name map miss -> NULL -> [+0x1C]


def _g_90b447_cave(cave_va):
    """CGameActor::SyncMountMotion 0x90B447 -- `mov eax,[eax+14h]` where eax = the return of
    `Concurrency::List::First(GetActiveVehicle(this))`. Zhong dumps: 25x, the TOP site. First()
    returned NULL (the active vehicle's model list is empty) and it read [0 + 0x14].
    GUARD: null-check the First() result; if NULL, bail to the function's own return at 0x90B59D
    (skip the mount-motion sync this frame -- cosmetic; the vehicle state does not change within
    the function, so returning early is safe). On the normal path replay `mov eax,[eax+14h];
    push eax` and jmp to loc_90B535 exactly as the original. Hook: 9 bytes (mov+push+jmp)
    @0x90B447; nothing branches into the range (only sequential flow)."""
    c = _Cave(cave_va)
    c.raw("85c0", "test eax,eax  -- First(vehicle) result")
    c.jcc("74", "bail", "jz bail -- vehicle model-list is empty")
    c.raw("8b4014", "mov eax,[eax+14h]  (replay 0x90B447)")
    c.raw("50", "push eax           (replay)")
    c.jmp_abs(0x90B535, "jmp loc_90B535 (the original target)")
    c.label("bail")
    c.jmp_abs(0x90B59D, "the function's own return")
    return c.bytes()


def apply_syncmount_null_guard(data):
    """Install the SyncMountMotion First()-NULL guard as a `.patch` cave + hook."""
    try:
        off = _rva_to_offset(data, 0x90B447 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (0x90B447 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_G_90B447
    cave = _g_90b447_cave(cave_va)
    if PATCH_OFF_G_90B447 + len(cave) > cap:
        return None, f"cave needs 0x{PATCH_OFF_G_90B447 + len(cave):X} but .patch is only 0x{cap:X}"
    occupied = data[sec_off + PATCH_OFF_G_90B447: sec_off + PATCH_OFF_G_90B447 + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_G_90B447:X} is occupied"
    data[sec_off + PATCH_OFF_G_90B447: sec_off + PATCH_OFF_G_90B447 + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (0x90B447 + 5)).hex() + "90" * 4
    r, m = patch_at_va(data, 0x90B447, "8b401450e9e5000000", new)
    if r is not True:
        return None, f"hook @0x90B447 failed: {m}"
    return True, f"SyncMountMotion guard cave @0x{cave_va:X} ({len(cave)} B); 0x90B447 hooked"


def _g_7a356b_cave(cave_va):
    """EquipmentUI_UpdateUI 0x7A356B -- `cmp dword ptr [ebx+1Ch],8` (the SSO length check of a
    std::wstring) where ebx = a skill-name entry from a std::map lookup (key 94). Zhong dumps:
    14x. The lookup missed, so ebx (v17) was NULL and it read [0 + 0x1C].
    GUARD: null-check ebx; if NULL, bail to the loop-continue at 0x7A3622 (skip this skill's
    icon update, advance to the next slot). On the normal path replay `cmp [ebx+1Ch],8;
    lea eax,[ebx+8]` and jmp to 0x7A3572 -- the `jb` there consumes the cmp's flags, and `lea`
    does not touch flags, so they survive. Hook: 7 bytes @0x7A356B; only sequential flow in."""
    c = _Cave(cave_va)
    c.raw("85db", "test ebx,ebx  -- skill-name entry (v17)")
    c.jcc("74", "bail", "jz bail -- map lookup missed")
    c.raw("837b1c08", "cmp [ebx+1Ch],8   (replay) -- SSO length")
    c.raw("8d4308", "lea eax,[ebx+8]    (replay)")
    c.jmp_abs(0x7A3572, "jmp back (the jb that reads these flags)")
    c.label("bail")
    c.jmp_abs(0x7A3622, "the loop-continue (skip this skill)")
    return c.bytes()


def apply_equip_ui_null_guard(data):
    """Install the EquipmentUI_UpdateUI skill-name-NULL guard as a `.patch` cave + hook."""
    try:
        off = _rva_to_offset(data, 0x7A356B - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (0x7A356B hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_G_7A356B
    cave = _g_7a356b_cave(cave_va)
    if PATCH_OFF_G_7A356B + len(cave) > cap:
        return None, f"cave needs 0x{PATCH_OFF_G_7A356B + len(cave):X} but .patch is only 0x{cap:X}"
    occupied = data[sec_off + PATCH_OFF_G_7A356B: sec_off + PATCH_OFF_G_7A356B + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_G_7A356B:X} is occupied"
    data[sec_off + PATCH_OFF_G_7A356B: sec_off + PATCH_OFF_G_7A356B + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (0x7A356B + 5)).hex() + "90" * 2
    r, m = patch_at_va(data, 0x7A356B, "837b1c088d4308", new)
    if r is not True:
        return None, f"hook @0x7A356B failed: {m}"
    return True, f"EquipmentUI guard cave @0x{cave_va:X} ({len(cave)} B); 0x7A356B hooked"


# ---------------------------------------------------------------------------
# UISkillIcon::ClearLinkedUI NULL-linked-UI guard (2026-08). Sibling of the AttachTooltip
# re-entrant-teardown crash: the auto-pyramid churns UI slots so fast that the linked-UI object
# at *(this+0x15C) is released re-entrantly, then this release-vcall runs on NULL.
PATCH_OFF_G_9D72A6 = 0x700   # UISkillIcon::ClearLinkedUI: NULL *(this+0x15C) release vcall

PATCH_OFF_CAMDEDUP = 0x720   # camera-upload dedup: 8 B of state at 0x720, code at 0x728


def _camdedup_cave(code_va, data_va):
    """NiDX9Renderer::SetCameraData (sub_BB2C70) -- skip the call when nothing it sets has changed.

    WHAT THE FUNCTION DOES. Its entire externally visible effect is three D3D9 calls:
    SetTransform(D3DTS_VIEW, this+2032), SetTransform(D3DTS_PROJECTION, this+2096) and, when a
    render target is bound, SetViewport. Everything else is bookkeeping into `this`.

    WHY THE EARLY-OUT IS SOUND, not a guess. The function CACHES every one of its own inputs, in
    the same call that consumes them:
        a5 -> this+2160    a4 -> this+2176    a3 -> this+2192    a2 -> this+2208
        a6 -> this+2444    (qmemcpy 0x1C)     a7 -> this+2472    (0x10, incl. the return at +2484)
    So if all six inputs are byte-identical to those caches, the D3D state currently bound was
    computed from exactly these values -- re-issuing the three calls cannot change anything. This
    is a much stronger claim than a hash of the camera object: we compare against the values the
    function itself last consumed.

    WHY IT MATTERS HERE. sub_C407D0 -> sub_C56250 -> sub_C57570 -> this runs for EVERY actor,
    vehicle, attachment and terrain node, BEFORE culling, so a crowded frame issues hundreds to
    thousands of byte-identical uploads. On native D3D9 SetTransform is a cheap state write; under
    a D3D9->D3D11/12 (dgVoodoo2) or D3D9->Vulkan (DXVK) translation layer each one invalidates the
    wrapper's derived matrices and fixed-function constant buffer, and SetViewport re-binds
    viewport state. That is one core busy with the GPU starved -- the measured "GPU ~10%,
    CPU ~10%, frame rate capped" signature.

    IDENTITY GUARD. Two extra comparisons keep the memo honest across state we do NOT own:
    the renderer `this` (so a second renderer instance can never match another's caches) and
    this+1784, the bound render target (the viewport is derived from its width/height, so a
    render-target switch -- shadow pass, UI render-to-texture -- must invalidate). Both live in
    the 8 bytes at `data_va` and are refreshed on every non-matching call.

    CONTROL FLOW. The hook replaces 10 bytes at 0xBB2C88 (the `mov eax,[ebp-3Ch]` +
    `movzx ecx,[eax+56Ch]` pair), which this cave replays. The +1388 guard is honoured unchanged.
    On a full match we load the value the body would have returned (this+2484) and jump to
    loc_BB339D -- the function's OWN epilogue, which pops edi/esi and runs the /GS cookie check.
    We never `retn` from the cave: the frame has a stack cookie and two pushed registers.

    REGISTERS. eax/ecx/edx are caller-scratch; esi/edi were pushed at 0xBB2C83/84 and are restored
    by that same epilogue, so `repe cmpsd` may use them freely. `cld` is emitted because the string
    compares require DF=0.

    LAYOUT. `Lupdate` sits in the MIDDLE on purpose: with 188 bytes of code and one common bail
    target, an end-to-end rel8 branch would be out of range. Every branch here is <=101 bytes, and
    _Cave raises at build time if that ever stops being true."""
    THIS_SLOT = struct.pack("<I", data_va).hex()
    RT_SLOT   = struct.pack("<I", data_va + 4).hex()

    def block(c, src_hex, cache_off, dwords, note):
        c.raw(src_hex, "mov esi,[ebp+..]   ; " + note)
        c.raw("8db8" + struct.pack("<I", cache_off).hex(),
              "lea edi,[eax+%Xh]" % cache_off)
        c.raw("b9" + struct.pack("<I", dwords).hex(), "mov ecx,%d" % dwords)
        c.raw("f3a7", "repe cmpsd")
        c.jcc("75", "Lupdate", "jne -> inputs differ, run the real body")

    c = _Cave(code_va)
    c.raw("8b45c4",         "mov eax,[ebp-3Ch]           ; this   (replay of the hooked bytes)")
    c.raw("0fb6886c050000", "movzx ecx,byte [eax+56Ch]   ; +1388 guard (replay)")
    c.raw("85c9",           "test ecx,ecx")
    c.jcc("75", "Lback",    "guard set -> let the original test/jnz reach the epilogue")
    c.raw("fc",             "cld                         ; repe cmpsd requires DF=0")
    c.raw("3b05" + THIS_SLOT, "cmp eax,[lastThis]")
    c.jcc("75", "Lupdate")
    c.raw("8b90f8060000",   "mov edx,[eax+6F8h]          ; this+1784 = bound render target")
    c.raw("3b15" + RT_SLOT, "cmp edx,[lastRT]")
    c.jcc("75", "Lupdate")
    block(c, "8b7514", 0x870, 3, "a5 vs this+2160")
    block(c, "8b7510", 0x880, 3, "a4 vs this+2176")
    block(c, "8b750c", 0x890, 3, "a3 vs this+2192")
    c.jcc("eb", "Lb456",    "unconditional jmp over the trampolines (EB = jmp rel8)")

    c.label("Lupdate")
    c.raw("8b45c4",           "mov eax,[ebp-3Ch]")
    c.raw("a3" + THIS_SLOT,   "mov [lastThis],eax")
    c.raw("8b90f8060000",     "mov edx,[eax+6F8h]")
    c.raw("8915" + RT_SLOT,   "mov [lastRT],edx")
    c.raw("33c9",             "xor ecx,ecx                 ; guard was clear -> fall through")
    c.label("Lback")
    c.raw("8b45c4",           "mov eax,[ebp-3Ch]           ; eax = this, as the original had it")
    c.jmp_abs(0xBB2C92,       "back to `test ecx,ecx`")

    c.label("Lb456")
    block(c, "8b7508", 0x8A0, 3, "a2 vs this+2208")
    block(c, "8b7518", 0x98C, 7, "a6 frustum vs this+2444")
    block(c, "8b751c", 0x9A8, 4, "a7 viewport vs this+2472")
    c.raw("8b80b4090000", "mov eax,[eax+9B4h]          ; this+2484 = the body's return value")
    c.jmp_abs(0xBB339D,   "the function's own epilogue (pop edi/esi, /GS check, retn 18h)")
    return c.bytes()


def _apply_camera_upload_dedup(data):
    HOOK_VA  = 0xBB2C88
    ORIG_HEX = "8b45c40fb6886c050000"
    try:
        probe = _rva_to_offset(data, HOOK_VA - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[probe] == 0xE9:
        return False, "already applied (BB2C88 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    slot    = PATCH_OFF_CAMDEDUP
    data_va = PATCH_SECTION_VA + slot
    code_va = data_va + 8
    cave    = _camdedup_cave(code_va, data_va)
    blob    = b"\x00" * 8 + cave           # 8 B of zeroed state, then the code
    err = _check_cave_fits(data, slot, blob, "CAMDEDUP")
    if err:
        return None, err
    occupied = data[sec_off + slot: sec_off + slot + len(blob)]
    if any(occupied) and bytes(occupied) != blob:
        return None, "cave slot 0x%X (CAMDEDUP) is occupied" % slot
    data[sec_off + slot: sec_off + slot + len(blob)] = blob
    new = "e9" + struct.pack("<i", code_va - (HOOK_VA + 5)).hex() + "90" * 5
    r, m = patch_at_va(data, HOOK_VA, ORIG_HEX, new)
    if r is not True:
        return None, "hook @0x%X failed: %s" % (HOOK_VA, m)
    return True, "camera-upload dedup installed (.patch cave %d B @0x%X)" % (len(cave), code_va)



def _g_9d72a6_cave(cave_va):
    """UISkillIcon::ClearLinkedUI 0x9D72A6 -- `mov ecx,[esi+15Ch]; mov edx,[ecx]; mov eax,[edx+8];
    push 1; push 20h; call eax` = the `vtable[+8](linkedUI, 32, 1)` release call on the object at
    *(this+0x15C). Caught LIVE in x64dbg (EIP 0x9D72AC, ECX=0), driven by AutoPyramid::Update via
    the overlay window proc -- the same re-entrant-teardown class as the 0x9DA51D AttachTooltip
    guard: the object is freed/nulled mid-refresh and this release then vcalls NULL.
    GUARD: null-check *(this+0x15C); if NULL, skip the release and reconverge at 0x9D72B7 (the
    next block reloads its own pointer from [esi+1D0h], so skipping a release of an already-gone
    object is safe). Normal path replays the load, deref, and vcall unchanged.
    HOOK: 6 bytes `mov ecx,[esi+15Ch]` @0x9D72A6. A jump from 0x9D7251 targets 0x9D72A6 (the hook
    START = the E9), which is fine; nothing lands INSIDE 0x9D72A7..0x9D72AB."""
    c = _Cave(cave_va)
    c.raw("8b8e5c010000", "mov ecx,[esi+15Ch]  (replay) -- linked-UI object")
    c.raw("85c9", "test ecx,ecx")
    c.jcc("74", "done", "jz done -- linked-UI already released; skip the vcall")
    c.raw("8b11", "mov edx,[ecx]       (replay 0x9D72AC) -- vptr")
    c.raw("8b4208", "mov eax,[edx+8]    (replay 0x9D72AE) -- vtable[+8]")
    c.raw("6a01", "push 1")
    c.raw("6a20", "push 20h")
    c.raw("ffd0", "call eax            (the release vcall)")
    c.label("done")
    c.jmp_abs(0x9D72B7, "reconverge (the [esi+1D0h] block)")
    return c.bytes()


def apply_skillicon_clearlinked_guard(data):
    """Install the UISkillIcon::ClearLinkedUI NULL-linked-UI guard as a `.patch` cave + hook."""
    try:
        off = _rva_to_offset(data, 0x9D72A6 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[off] == 0xE9:
        return False, "already applied (0x9D72A6 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    cave_va = PATCH_SECTION_VA + PATCH_OFF_G_9D72A6
    cave = _g_9d72a6_cave(cave_va)
    if PATCH_OFF_G_9D72A6 + len(cave) > cap:
        return None, f"cave needs 0x{PATCH_OFF_G_9D72A6 + len(cave):X} but .patch is only 0x{cap:X}"
    occupied = data[sec_off + PATCH_OFF_G_9D72A6: sec_off + PATCH_OFF_G_9D72A6 + len(cave)]
    if any(occupied) and bytes(occupied) != cave:
        return None, f"cave slot 0x{PATCH_OFF_G_9D72A6:X} is occupied"
    data[sec_off + PATCH_OFF_G_9D72A6: sec_off + PATCH_OFF_G_9D72A6 + len(cave)] = cave
    new = "e9" + struct.pack("<i", cave_va - (0x9D72A6 + 5)).hex() + "90"
    r, m = patch_at_va(data, 0x9D72A6, "8b8e5c010000", new)
    if r is not True:
        return None, f"hook @0x9D72A6 failed: {m}"
    return True, f"ClearLinkedUI guard cave @0x{cave_va:X} ({len(cave)} B); 0x9D72A6 hooked"


def apply_cf_null_guards(data):
    """Install the CF1/CF4/CF5 NULL-this/First() crash guards as `.patch` caves + hooks.
    Each hook jmps into a cave that null-checks the pointer, then either replays the overwritten
    instruction(s) and jumps back, or skips to a safe return / reconvergence point."""
    try:
        h1 = _rva_to_offset(data, 0x90B447 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[h1] == 0xE9:
        return False, "already applied (CF1 hook present)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    caves = [
        (PATCH_OFF_CF1, _cf1_cave, 0x90B447, "8b401450e9e5000000", 9, "CF1"),
        (PATCH_OFF_CF4, _cf4_cave, 0x413210, "558bec8b4508",       6, "CF4"),
        (PATCH_OFF_CF5, _cf5_cave, 0x8FD2E7, "8bb180010000",       6, "CF5"),
    ]
    # BOUND + OCCUPANCY CHECKS ADDED 2026-08, hoisted ahead of every write. This was the only
    # cave installer with neither. Reproduced on a binary carrying a 0x40-byte `.patch`: it wrote
    # 66 bytes PAST the section end, silently grew the file, left SizeOfRawData / VirtualSize /
    # SizeOfImage stale, and returned True with a success message -- after which the three hooks
    # would have jmp'd into unmapped memory, the exact opposite of a crash guard. Latent at the
    # current layout (ends at 0x134, under both the legacy 0x400 and the current 0x800), but the
    # sibling installers all refuse cleanly in that situation and this one did not.
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    built = []
    for slot, builder, hook_va, orig_hex, hook_len, label in caves:
        cave = builder(PATCH_SECTION_VA + slot)
        if slot + len(cave) > cap:
            return None, (f"cave {label} needs 0x{slot + len(cave):X} but this binary's .patch "
                          f"is only 0x{cap:X} -- re-patch from the stock exe to get the larger one")
        occupied = data[sec_off + slot: sec_off + slot + len(cave)]
        if any(occupied) and bytes(occupied) != cave:
            return None, f"cave slot 0x{slot:X} ({label}) is occupied"
        built.append((slot, cave, hook_va, orig_hex, hook_len))
    for slot, cave, hook_va, orig_hex, hook_len in built:
        cave_va = PATCH_SECTION_VA + slot
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        jrel = struct.pack("<i", cave_va - (hook_va + 5))
        new = "e9" + jrel.hex() + "90" * (hook_len - 5)     # jmp cave + NOP pad
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"CF hook @0x{hook_va:X} failed: {m}"
    return True, "CF1/CF4/CF5 null-guard caves installed (.patch)"


_D3DSAMP_MAXANISOTROPY = 0x0A   # NOT 3. See _aniso_cave.


def _aniso_cave(cave_va):
    """Injected after sub_DDC350's MIP-filter SetSamplerState: complete that call, then set
    D3DSAMP_MAXANISOTROPY=16 for the same stage, then rejoin the epilogue. this =
    CRenderStateMgr @ ds:0x15C1D64; SetSamplerState @0x45F740 (retn 0Ch, callee-clean).

    2026-08 AUDIT -- THE ENUM WAS WRONG AND THE FIX DELIVERED ZERO AF. This cave used to push 3,
    which is D3DSAMP_ADDRESSW, not D3DSAMP_MAXANISOTROPY (10). Two consequences, both bad:
      * MaxAnisotropy was NEVER set, so it stayed at its default of 1 and the D3DTEXF_ANISOTROPIC
        filter set by the inline half degenerated to LINEAR -- precisely the failure this cave's
        own comment says it exists to prevent. The fix bought nothing.
      * it wrote 16 into ADDRESSW, which is not even a valid D3DTEXTUREADDRESS (1..5).
    The binary pins the enum independently: the SAME function issues types 5 / 6 / 7 for
    MAG / MIN / MIP, which is stock D3DSAMPLERSTATETYPE numbering, so MAXANISOTROPY can only be 10.
    NOTE: the broken form is already deployed in the shipping Rag2.exe (d3941c67), where the cave
    at .patch 0x1630180 carries `6a03` verbatim -- apply_anisotropic_filtering rewrites the slot,
    so re-running the patcher on such a build repairs it."""
    b = bytearray()
    b += bytes.fromhex("8b0d641d5c01")                       # mov ecx,[15C1D64]  (displaced MIP this-load)
    b += b"\xE8" + _rel32(cave_va + len(b) + 5, 0x45F740)    # call SetSamplerState  (finish MIP call)
    b += bytes.fromhex("6a10")                               # push 16   (MaxAnisotropy value)
    b += bytes([0x6A, _D3DSAMP_MAXANISOTROPY])               # push 0Ah  (D3DSAMP_MAXANISOTROPY)
    b += bytes.fromhex("8b550c")                             # mov edx,[ebp+0Ch]  (stage = arg_4)
    b += bytes.fromhex("52")                                 # push edx
    b += bytes.fromhex("8b0d641d5c01")                       # mov ecx,[15C1D64]
    b += b"\xE8" + _rel32(cave_va + len(b) + 5, 0x45F740)    # call SetSamplerState  (set MaxAnisotropy)
    b += b"\xE9" + _rel32(cave_va + len(b) + 5, 0xDDC431)    # jmp back to epilogue (mov esp,ebp;pop ebp;retn)
    return bytes(b)


def apply_anisotropic_filtering(data):
    """Enable 16x anisotropic filtering on the fixed-function texture path (sub_DDC350):
    (1) inline flip the LINEAR(2) min/mag/mip filter constant -> ANISOTROPIC(3), and
    (2) a `.patch` cave that also sets D3DSAMP_MAXANISOTROPY=16 (which the client never sets,
    so ANISOTROPIC would otherwise degenerate to LINEAR). MIP=3 is out-of-spec but drivers/DXVK
    clamp it to LINEAR harmlessly.

    REPAIRS THE 2026-08 ENUM BUG ON ALREADY-HOOKED BUILDS. The old version early-returned
    "already applied" the moment it saw the 0xE9 hook byte, so a binary carrying the broken cave
    (`push 3` = ADDRESSW instead of `push 10` = MAXANISOTROPY -- see _aniso_cave) could never be
    healed by re-running the patcher: it reported success and left the dead cave in place. Now the
    hook byte only tells us the hook exists; the CAVE is compared and rewritten if it differs."""
    try:
        h = _rva_to_offset(data, 0xDDC426 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_ANISO
    cave = _aniso_cave(cave_va)
    cap = _patch_section_size(data) or PATCH_SECTION_SIZE
    if PATCH_OFF_ANISO + len(cave) > cap:
        return None, (f"aniso cave needs 0x{PATCH_OFF_ANISO + len(cave):X} but this binary's "
                      f".patch is only 0x{cap:X} -- re-patch from the stock exe")
    slot = sec_off + PATCH_OFF_ANISO
    if data[h] == 0xE9:                                   # hook present: repair the cave only
        if bytes(data[slot:slot + len(cave)]) == cave:
            return False, "already applied (aniso hook + current cave present)"
        data[slot:slot + len(cave)] = cave
        return True, (f"aniso cave REPAIRED @0x{cave_va:X} "
                      f"(D3DSAMP_MAXANISOTROPY was miscoded as ADDRESSW)")
    r1, m1 = patch_at_va(data, 0xDDC3C5, "c745f402000000", "c745f403000000")   # LINEAR -> ANISOTROPIC
    if r1 is None:
        return None, f"filter const: {m1}"
    # sub_DDC350 feeds var_C to THREE SetSamplerState calls: MAGFILTER(5), MINFILTER(6) and
    # MIPFILTER(7). D3DTEXF_ANISOTROPIC is legal for MAG/MIN but NOT for MIP -- the only legal
    # MIPFILTER values are NONE(0), POINT(1) and LINEAR(2). Feeding it 3 is out of spec, and the
    # observed result in play was mipmapping switching OFF entirely (heavy shimmer at distance).
    # Reported by the operator after installing the corrected AF fix; the old broken AF never set
    # anything, which is why this only surfaced now.
    # So pin the MIP call to LINEAR while MIN/MAG stay ANISOTROPIC = standard trilinear + AF.
    # `mov ecx,[ebp+var_C]; push ecx` (8b 4d f4 51) -> `push 2` + 2 NOPs, same 4 bytes.
    r2, m2 = patch_at_va(data, 0xDDC41C, "8b4df451", "6a029090")
    if r2 is None:
        return None, f"mip filter: {m2}"
    data[slot:slot + len(cave)] = cave
    jrel = struct.pack("<i", cave_va - (0xDDC426 + 5))
    new = "e9" + jrel.hex() + "90"     # jmp cave + NOP (fills the 6-byte this-load)
    r3, m3 = patch_at_va(data, 0xDDC426, "8b0d641d5c01", new)
    if r3 is not True:
        return None, f"aniso hook: {m3}"
    return True, f"16x AF: LINEAR->ANISOTROPIC + MaxAnisotropy cave @0x{cave_va:X}"


# Inline the trivial single-instruction singleton getters at hot per-actor call sites in
# Render_GameWorld: `call getter` (E8 rel32) -> `mov eax,[global]` (A1 abs), same length, same
# result (eax=singleton, no flags, net-zero esp -- getters are pure `mov eax,[g]; ret`, verified
# no lazy-init). Removes a call+ret per visible actor per frame. Output-identical.
_SINGLETON_GETTER_INLINES = [
    (0x821D46, "e8e56c0800", "a1c4155b01"),  # call CModeMgr::GetInstance   -> mov eax,[15B15C4]
    (0x821DDC, "e89f212200", "a19c645b01"),  # call CConfig::GetInstance    -> mov eax,[15B649C]
    (0x821DF6, "e885212200", "a19c645b01"),  # call CConfig::GetInstance    -> mov eax,[15B649C]
    (0x821E20, "e8cb10c4ff", "a1d8e65a01"),  # call CCutscene::GetInstance  -> mov eax,[15AE6D8]
    (0x821A0D, "e81e700800", "a1c4155b01"),  # call CModeMgr::GetInstance   -> mov eax,[15B15C4]
]


def _defer_load_cave(cave_va, data_va):
    """Rate-limiter for CActorMotion::LoadEquipmentModels: allow BUDGET_N synchronous NIF loads
    per WINDOW_MS (GetTickCount), else return 0 ('not ready, retry next frame' -- the caller
    already re-polls). Preserves ecx=this; the proceed path replays the displaced prologue and
    reconverges at 0x9272A5. g_win_start/g_count are two DWORDs in the section (zero-init)."""
    g_win = data_va + 0
    g_cnt = data_va + 4
    b = bytearray()
    b += bytes.fromhex("51")                                     # push ecx  (save this)
    b += bytes.fromhex("ff1590523501")                           # call ds:[1355290] GetTickCount
    b += b"\x8b\x0d" + struct.pack("<I", g_win)                  # mov ecx,[g_win_start]
    b += bytes.fromhex("8bd0")                                   # mov edx,eax
    b += bytes.fromhex("2bd1")                                   # sub edx,ecx   (now - window_start)
    b += bytes.fromhex("83fa21")                                 # cmp edx,33  (WINDOW_MS)
    b += bytes.fromhex("720f")                                   # jb same_window (+0x0F)
    b += b"\xa3" + struct.pack("<I", g_win)                      # mov [g_win_start],eax
    b += b"\xc7\x05" + struct.pack("<I", g_cnt) + b"\x00\x00\x00\x00"  # mov [g_count],0
    b += b"\xa1" + struct.pack("<I", g_cnt)                      # same_window: mov eax,[g_count]
    b += bytes.fromhex("83f802")                                 # cmp eax,2  (BUDGET_N)
    b += bytes.fromhex("7311")                                   # jae over_budget (+0x11)
    b += bytes.fromhex("40")                                     # inc eax
    b += b"\xa3" + struct.pack("<I", g_cnt)                      # mov [g_count],eax
    b += bytes.fromhex("59")                                     # pop ecx  (restore this)
    b += bytes.fromhex("558bec6aff")                             # push ebp; mov ebp,esp; push -1  (displaced)
    b += b"\xe9" + _rel32(cave_va + len(b) + 5, 0x9272A5)        # jmp reconverge
    b += bytes.fromhex("5933c0c3")                               # over_budget: pop ecx; xor eax,eax; ret
    return bytes(b)


def apply_deferred_load(data):
    """Cave: spread synchronous equipment-NIF loads over frames to kill the crowd-entry stutter.
    Hooks CActorMotion::LoadEquipmentModels @0x9272A0."""
    try:
        h = _rva_to_offset(data, 0x9272A0 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[h] == 0xE9:
        return False, "already applied"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_DEFER
    data_va = PATCH_SECTION_VA + PATCH_OFF_DEFER_DATA
    cave = _defer_load_cave(cave_va, data_va)
    err = _check_cave_fits(data, PATCH_OFF_DEFER, cave, "deferred_load")
    if err:
        return None, err
    if PATCH_OFF_DEFER + len(cave) > PATCH_OFF_DEFER_DATA:
        return None, "deferred_load cave overruns its own data slot"
    data[sec_off + PATCH_OFF_DEFER: sec_off + PATCH_OFF_DEFER + len(cave)] = cave
    jrel = struct.pack("<i", cave_va - (0x9272A0 + 5))
    r, m = patch_at_va(data, 0x9272A0, "558bec6aff", "e9" + jrel.hex())
    if r is not True:
        return None, f"hook failed: {m}"
    return True, f"deferred equip-load cave @0x{cave_va:X} (<=2 sync loads / 33ms)"


def apply_inline_getters(data):
    """Apply the singleton-getter inlines all-or-nothing (trial copy first)."""
    trial = bytearray(data)
    applied = skipped = 0
    failed = []
    for va, fh, rh in _SINGLETON_GETTER_INLINES:
        r, m = patch_at_va(trial, va, fh, rh)
        if r is True:
            applied += 1
        elif r is False:
            skipped += 1
        else:
            failed.append(f"0x{va:X}")
    if failed:
        return None, f"{len(failed)} getter site(s) not found ({', '.join(failed[:2])}) -- not applied"
    if applied:
        data[:] = trial
        return True, f"{applied} getter calls inlined ({skipped} already)"
    return False, f"already applied ({skipped})"


# ---------------------------------------------------------------------------
# Fix registry
# ----- view-cone cull (SceneManager_Cull_And_Render) -----------------------
def _conecull_cave(cave_va):
    """View-cone cull cave for the per-object submit loop of SceneManager_Cull_And_Render
    (0x806D00). Hooked at 0x80757F, which in the original is `mov ecx,[ebx+88h]` (6 B). All
    facts below were verified against the clean image (disasm + on-disk bytes):
      * At the hook: ebx = the camera used by THIS cull pass (ebx=[ebp+var_1D4] @0x8074c6,
        untouched), esi = object, ebp = frame, and the x87 stack is EMPTY (fcomp @0x80756e
        popped the last value). Free: eax/ecx/edx/edi. Must preserve esi/ebx/ebp.
      * The camera-object deltas are already resident and only read since being written:
        dx = cam.x-obj.x = [ebp-0x2E4], dy = [ebp-0x2E0], dz = [ebp-0x2DC]
        (fstp'd at 0x8074f0 / 0x807510 / 0x807529).
      * Camera world FORWARD = column 0 of the world 3x3 = [ebx+0x64]/[ebx+0x70]/[ebx+0x7C]
        (Right = col1 via Matrix_GetRightVector, Up = col2 via Matrix_GetUpVector -> col0 is
        the remaining look axis). Its SIGN (col0 = look dir vs its negation) is the one thing
        not statically provable; if inverted the cull removes what's IN FRONT (obvious + a
        1-byte flip of the two `jb`s). dot = delta . forward; object IN FRONT <=> dot < 0.
      * CULL jumps to 0x807DB4 -- the target of the engine's OWN distance cull (`jp` @0x807579),
        which runs the mandatory per-object child-queue process + clear before reaching the loop
        increment. Jumping to the increment (0x808171) instead would leak that bookkeeping.
      * KEEP re-executes the displaced `mov ecx,[ebx+88h]` then jumps to 0x807585 (next insn),
        so the submit path is byte-identical to the unpatched flow.
    Behind-hemisphere cull (dot > SLACK) plus an optional forward cone (COS2 > 0). Every exit
    leaves the FPU empty. fcomip is used so no fnstsw/sahf and eax is not even touched."""
    KEEP, CULL = 0x807585, 0x807DB4
    slack_va, cos2_va = cave_va + 115, cave_va + 119
    b = bytearray()
    b += bytes.fromhex("d9851cfdffff")                     # 0   fld  [ebp-2E4]  dx
    b += bytes.fromhex("d88b64000000")                     # 6   fmul [ebx+64]   *fx
    b += bytes.fromhex("d98520fdffff")                     # 12  fld  [ebp-2E0]  dy
    b += bytes.fromhex("d88b70000000")                     # 18  fmul [ebx+70]   *fy
    b += bytes.fromhex("dec1")                             # 24  faddp st1,st
    b += bytes.fromhex("d98524fdffff")                     # 26  fld  [ebp-2DC]  dz
    b += bytes.fromhex("d88b7c000000")                     # 32  fmul [ebx+7C]   *fz
    b += bytes.fromhex("dec1")                             # 38  faddp -> st0=dot
    b += b"\xd9\x05" + struct.pack("<I", slack_va)         # 40  fld  [SLACK]
    b += bytes.fromhex("dff1")                             # 46  fcomip st,st1  CF=(SLACK<dot); pops SLACK
    b += bytes.fromhex("723a")                             # 48  jb .cull_pop (+0x3A -> 108)
    b += bytes.fromhex("d8c8")                             # 50  fmul st,st  dot^2
    b += bytes.fromhex("d9851cfdffff")                     # 52  fld  [ebp-2E4]
    b += bytes.fromhex("d8c8")                             # 58  fmul st,st  dx^2
    b += bytes.fromhex("d98520fdffff")                     # 60  fld  [ebp-2E0]
    b += bytes.fromhex("d8c8")                             # 66  fmul st,st  dy^2
    b += bytes.fromhex("dec1")                             # 68  faddp
    b += bytes.fromhex("d98524fdffff")                     # 70  fld  [ebp-2DC]
    b += bytes.fromhex("d8c8")                             # 76  fmul st,st  dz^2
    b += bytes.fromhex("dec1")                             # 78  faddp -> st0=lenSq, st1=dot^2
    b += b"\xd8\x0d" + struct.pack("<I", cos2_va)          # 80  fmul [COS2] -> COS2*lenSq
    b += bytes.fromhex("dff1")                             # 86  fcomip st,st1  CF=(COS2*lenSq<dot^2); pops
    b += bytes.fromhex("ddd8")                             # 88  fstp st(0)  pop dot^2 -> empty
    b += bytes.fromhex("7205")                             # 90  jb .keep (+5 -> 97)
    b += b"\xe9" + struct.pack("<i", CULL - (cave_va + 97))    # 92  jmp CULL (cone-cull, FPU empty)
    b += bytes.fromhex("8b8b88000000")                     # 97  .keep: mov ecx,[ebx+88] (displaced)
    b += b"\xe9" + struct.pack("<i", KEEP - (cave_va + 108))   # 103 jmp KEEP
    b += bytes.fromhex("ddd8")                             # 108 .cull_pop: fstp st(0) pop dot -> empty
    b += b"\xe9" + struct.pack("<i", CULL - (cave_va + 115))   # 110 jmp CULL
    b += struct.pack("<f", _CONECULL_SLACK)                # 115 SLACK dd
    b += struct.pack("<f", _CONECULL_COS2)                 # 119 COS2 dd
    assert len(b) == 123, len(b)
    return bytes(b)


def apply_conecull(data):
    """Cave fix: add a view-cone cull to SceneManager_Cull_And_Render. Writes the cave into the
    shared `.patch` section and hooks 0x80757F. Behind-camera world objects (beyond SLACK) stop
    being submitted -> makes a long draw distance cheap. See _conecull_cave for the full proof."""
    try:
        hook_off = _rva_to_offset(data, 0x80757F - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[hook_off] == 0xE9:
        return False, "already applied (hook in place)"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_CONECULL
    cave = _conecull_cave(cave_va)
    err = _check_cave_fits(data, PATCH_OFF_CONECULL, cave, "conecull")
    if err:
        return None, err
    slot = sec_off + PATCH_OFF_CONECULL
    data[slot:slot + len(cave)] = cave
    jrel = (cave_va - (0x80757F + 5)) & 0xFFFFFFFF
    new_hook = "e9" + struct.pack("<I", jrel).hex() + "90"   # jmp cave + NOP (fills the 6-byte hook)
    r, m = patch_at_va(data, 0x80757F, "8b8b88000000", new_hook)
    if r is not True:
        return None, f"cave written but hook failed: {m}"
    return True, (f".patch cave @VA 0x{cave_va:X}; cone cull hooked @0x80757F "
                  f"(SLACK={_CONECULL_SLACK}, COS2={_CONECULL_COS2}); cull->0x807DB4")


# ---------------------------------------------------------------------------

class Fix:
    def __init__(self, id, module, title, enabled, why, apply):
        self.id = id
        self.module = module
        self.title = title
        self.enabled = enabled
        self.why = why
        self.apply = apply  # callable(bytearray) -> (result, message)


FIXES = [
    # =======================================================================
    Fix(
        id="laa",
        module="memory",
        enabled=True,
        title="Large-Address-Aware (2 GB -> 4 GB address space)",
        why=(
            "SYMPTOM  : client crashes after ~1h / several zone loads. Minidumps land in\n"
            "           CZoneData::LoadZoneData (writes to address 0), the SpeedTree render\n"
            "           new-failure handler int3 (sub_4751B0), and assorted null-derefs --\n"
            "           several different crash signatures, ONE underlying cause.\n"
            "ROOT     : the exe is NOT LargeAddressAware, so the 32-bit process is capped at\n"
            "           2 GB of virtual ADDRESS SPACE. Measured live at 1.82 GB and climbing;\n"
            "           when it crosses 2 GB the next `operator new` (~846 KB) fails, the CRT\n"
            "           calls the new-failure handler which executes `int 3` -> crash. Note\n"
            "           physical RAM was only 807 MB -- this is address space, not RAM.\n"
            "FIX      : set IMAGE_FILE_LARGE_ADDRESS_AWARE (0x20) in the PE Characteristics.\n"
            "           On 64-bit Windows the process then gets up to 4 GB -> ~2.2 GB of extra\n"
            "           headroom at current usage -> the OOM crash cascade stops. Header-only,\n"
            "           no code change, fully reversible.\n"
            "EVIDENCE : Get-Process VirtualMemorySize = 1862 MB / 2048; crash EIP 0x4751b4 is\n"
            "           the int3 in the new-handler, reached via msvcr100!_callnewh; call\n"
            "           stack Render_GameWorld -> CSpeedTreeWrapper::UpdateTextureAndRender.\n"
            "           (Structurally re-confirmed 2026-08: 0x4751B0 = `push ebp; mov ebp,esp;\n"
            "           int3; mov eax,[ebp+8]; pop ebp; retn`, so a breakpoint exception does\n"
            "           report EIP 0x4751B4 exactly as claimed.)\n"
            "SCOPE OF THAT EVIDENCE (2026-08) : the SYMPTOM and EVIDENCE above describe an\n"
            "           EARLIER dump set. NONE of the current 31 minidumps faults at 0x4751B4,\n"
            "           and all 31 come from a build that ALREADY has LAA. So 'the OOM crash\n"
            "           cascade stops' must not be read as covering those 31 crashes -- they are\n"
            "           a different population, handled by the guard fixes below.\n"
            "RISK     : low. A 32-bit app that stashes flags in pointer bit31 could misbehave\n"
            "           above 2 GB; original exe is untouched, so revert = launch Rag2.exe."
        ),
        apply=lambda d: set_pe_characteristic_bit(d, 0x0020),
    ),

    # =======================================================================
    # The fixes below were reviewed against the IDA database of the clean b303
    # client (Rag2.exe SHA256 5f6e2115...) one-by-one: original bytes read from
    # the image, the anchor pattern grown until UNIQUE, and calling-convention /
    # ret-N / branch sense confirmed from the decompiler + real epilogue before
    # shipping. Ported from the b303 catalog (wiki/client/optimization-patches.md,
    # mods .../patch_all.py) but CORRECTED where that source was wrong (see
    # no_grass). Everything except the crash fix ships disabled -> opt in per taste.
    # =======================================================================

    # ----- CRASH / STABILITY (on by default, like laa) --------------------
    Fix(
        id="dungeon_enter_nullderef",
        module="stability",
        enabled=True,
        title="SendDungeonEnterRequest: return 0 instead of a guaranteed NULL deref",
        why=(
            "SYMPTOM  : intermittent crash when entering a dungeon (~15 reported).\n"
            "ROOT     : the tail at 0x6F69AB runs `mov eax,[esi+4]; xor ecx,ecx;\n"
            "           mov edx,[ecx]` -- ecx is forced to 0 and then dereferenced,\n"
            "           an unconditional read of address 0 whenever that path is hit.\n"
            "FIX      : replace the 18-byte tail with `xor eax,eax; pop esi; ret`\n"
            "           (return 0) + NOP pad. The pop esi balances the prologue push;\n"
            "           the original ret is a bare `ret`, so no arg cleanup changes.\n"
            "EVIDENCE : disasm 0x6F69AB shows the null read; function ends `pop esi;\n"
            "           retn` (no ret N). Byte pattern unique in the image.\n"
            "RISK     : low. The request silently no-ops instead of crashing; the\n"
            "           crashing path produced no useful work anyway."
        ),
        apply=lambda d: patch_bytes(d, "8b460433c98b118b92b40f000050ffd25ec3",
                                       "31c05ec39090909090909090909090909090"),
    ),

    # Five sibling null-vtable-call guards (same bug family as the one above).
    # Pattern: an MSVC RMI/packet sender does `if (CAuth::GetInstance() && rmi) obj=rmi;
    # else obj=0; (*(*obj+N))(obj,..)`. On the ELSE (interface-not-ready) branch obj==0,
    # yet the shared virtual call `mov edx,[ecx]; mov edx,[edx+N]; call edx` still runs ->
    # guaranteed null-vtable deref (crash) when auth/RMI isn't up. Each null branch's own
    # `xor ecx,ecx` (33 C9) is replaced with a 2-byte short `jmp` that skips ONLY the null
    # send and reconverges with the valid path right after `call edx`; the valid branch is
    # byte-for-byte untouched (it jumps to the shared code independently -- confirmed by an
    # inbound jump xref). ESP stays balanced (the vtable stub cleans its own pushed arg,
    # so skipping push+call is net-zero) and each function's natural return value/epilogue
    # is preserved. Verified per-site: reconvergence target lands on a real instruction
    # boundary, disp32 in the anchor makes the pattern unique. All are UI/network reachable.
    Fix(
        id="minidump_null_guards_2026_08b",
        module="stability",
        enabled=True,
        title="Four further crash guards (garbage-not-null pointers) from the same dump set",
        why=(
            "SYMPTOM  : 5 more of the 31 minidumps, four sites:\n"
            "             0x43BB8D + 0x43BB92 x2  CGameActor::GetActiveVehicle\n"
            "             0xDBE335          x1  sub_DBE320, fault 0x004 (null this)\n"
            "             0x975F01          x1  UIItemHandler::OnItemDoubleClick, fault 0x004\n"
            "             0x957E80          x1  sub_957E30, fault 0x144 (inline strlen)\n"
            "ROOT     : three of the four already HAVE a null test that passed, because the\n"
            "           bad pointer was garbage rather than zero: 0xBF59C68A and 0x0235022E\n"
            "           in GetActiveVehicle, 0x144 in the strlen. A `test reg,reg` cannot\n"
            "           see those, so the guards compare against 0x10000 (the Windows\n"
            "           null-page reservation) instead.\n"
            "FIX      : hook each site into a `.patch` cave that re-validates, replays the\n"
            "           overwritten instructions on the good path, and otherwise takes an\n"
            "           exit the function already owns:\n"
            "             0x43BB7C -> 0x43BB86, its own `xor eax,eax; ...; retn` (= no vehicle)\n"
            "             0xDBE320 -> `retn 8` before the frame is built\n"
            "             0x975F01 -> loc_976E65, the same target the branch above uses\n"
            "             0x957E6D -> loc_95805D, the function's own empty-Src path\n"
            "EVIDENCE : GetActiveVehicle is worth the detail -- it is 2 dumps in ONE function\n"
            "           and its existing null path is itself broken: `xor esi,esi` then\n"
            "           `mov edx,[esi+8]` would fault at 8, so routing bad values there would\n"
            "           only move the crash. They go to the real failure epilogue instead.\n"
            "           All four regions verified untouched by our optimization patches.\n"
            "*** 2026-08 AUDIT: FIVE BAD DISPLACEMENTS FIXED, AND TWO OF THESE FOUR GUARDS ARE\n"
            "    INERT AGAINST THE VERY DUMPS THEY WERE WRITTEN FOR. Read before trusting the\n"
            "    SYMPTOM list above. ***\n"
            "  DISPLACEMENTS (fixed): five rel8 bails were hand-counted wrong. 0x975F01's landed\n"
            "           mid-instruction on `sbb [ebx+34h],bl` + `FF E9` = `jmp ecx` -- a stray\n"
            "           write plus an indirect jump through a live heap pointer, ON EXACTLY THE\n"
            "           edi==0 CONDITION ITS OWN DUMP RECORDS, i.e. strictly worse than no fix.\n"
            "           0x43BB7C's two bails fell out of the slot into the neighbouring cave and\n"
            "           executed a `retn 8` on GetActiveVehicle's live frame. The other two were\n"
            "           correct only by accident. All five are now computed from a label by the\n"
            "           _Cave assembler, so this class of bug cannot recur silently.\n"
            "  EFFICACY (NOT fixed -- it is not fixable by a threshold):\n"
            "           * 0x43BB7C is INERT on both its dumps. ecx=0xBF59C68A and esi=0x0235022E\n"
            "             are far ABOVE 0x10000, so every test passes and the faults reproduce\n"
            "             unchanged. Its first test is additionally DEAD BY CONSTRUCTION: eax\n"
            "             there is `lea eax,[ebp-0Ch]`, always a stack address. The real cause is\n"
            "             a freed actor whose memory was reused (the observed values are IEEE\n"
            "             floats in [-1,1]); no pointer test repairs that. 0x43BBA6 and 0x43BBA9\n"
            "             in the same function also remain unguarded.\n"
            "           * 0x975F01 fires correctly now, but only on a true null.\n"
            "           * 0x957E6D (ecx=0x144) and 0xDBE320 (this==0) DO catch their dumps.\n"
            "             These two are the ones carrying this fix.\n"
            "  So: 2 of 4 guards are load-bearing, 2 are cheap insurance against a null that has\n"
            "  not been observed. The SYMPTOM/FIX sections must not be read as 'these 5 dumps are\n"
            "  fixed' -- 3 of the 5 are not.\n"
            "LIMITS   : guards, not root causes. A garbage pointer that happens to be above\n"
            "           0x10000 still gets through -- and for 0x43BB7C that is the actual case,\n"
            "           not a hypothetical.\n"
            "RISK     : low. No new control flow is invented -- every bail target is one the\n"
            "           function already branches to."
        ),
        apply=apply_more_null_guards_2,
    ),
    Fix(
        id="minidump_null_guards_2026_08",
        module="stability",
        enabled=True,
        title="Four more crash guards derived from the 2026-07/08 minidump set",
        why=(
            "SYMPTOM  : 7 of 31 minidumps, four distinct sites, all ACCESS_VIOLATION on a\n"
            "           pointer that was never validated:\n"
            "             0xBA13AA x3  fault 0x018  null `this` in a 17-byte getter\n"
            "             0x7A9DBE x2  fault 0x15C  null base, then a 42-dword rep movsd\n"
            "             0x7B2212 x1  fault 0x568  vptr came back as 0x558\n"
            "             0x65E7E2 x1  fault 0x0F0  null vptr in a UI update\n"
            "ROOT     : each site loads a pointer out of a struct and dereferences it in\n"
            "           the next instruction with no test in between. The constant fault\n"
            "           addresses (equal to the struct offset every time) are what rules\n"
            "           out memory pressure or a race.\n"
            "FIX      : hook each site to a small `.patch` cave that re-tests the pointer,\n"
            "           replays the overwritten instructions when it is good, and takes a\n"
            "           safe path when it is not:\n"
            "             0xBA13A7 -> return 0 (the getter's own natural null result)\n"
            "             0x7A9DB8 -> 0x7A9ECB, the bail target the `ja` above already uses\n"
            "             0x7B2210 -> 0x7B221D, the function's own else-branch\n"
            "             0x65E7E0 -> 0x65E7EB, skipping only that one virtual call\n"
            "EVIDENCE : 0x7B2212's bad vptr was 0x558, NOT null -- a plain `test` would not\n"
            "           have caught it -- so those two guards compare against 0x10000, the\n"
            "           Windows null-page reservation, below which nothing valid can live.\n"
            "           0x7B2210 carries a pending `fldz` that the normal path consumes\n"
            "           with `fstp`, so its bail path pops the x87 stack (`fstp st(0)`)\n"
            "           first; without that the FPU stack would leak a slot per skipped\n"
            "           frame. 0x65E7E0 skips `push ebx; call eax` together, and the callee\n"
            "           cleans its own arg, so ESP is unchanged either way.\n"
            "CORRECTED 2026-08 : 'All four regions verified untouched by our own optimization\n"
            "           patches' is FALSE for 0xBA13A0. In b303-2026-07-11 Rag2.exe (d3941c67)\n"
            "           that exact 17-byte region is ALREADY an inlined null-guarded getter\n"
            "           (558bec51894dfc8b45fc8b40188be55dc3 -> 85c974048b4118c333c0c39090909090\n"
            "           9090), so on that binary this fix FAILs cleanly on byte mismatch and\n"
            "           main() aborts without writing. The claim holds for the pristine exe and\n"
            "           the 2022 build only.\n"
            "LIMITS   : guards, not root-cause fixes. They stop the crash and let the\n"
            "           frame continue with one skipped update; they do not explain why\n"
            "           the pointers go bad.\n"
            "RISK     : low. Each bail path is a target the function itself already\n"
            "           branches to, so no new control flow is invented."
        ),
        apply=apply_more_null_guards,
    ),
    Fix(
        id="vcall_target_guards_2026_08",
        module="stability",
        enabled=False,   # see FLIP CRITERION below -- wants one play session first
        title="Reject a virtual-call target that is not inside .text (the four 'driver' crashes)",
        why=(
            "SYMPTOM  : 4 of 31 minidumps do not fault inside Rag2.exe at all, and three of\n"
            "           them get filed against an NVIDIA DLL:\n"
            "             EIP 0x646C6169  'nvd3dum.dll'      DEP/execute\n"
            "             EIP 0x676F4677  'nvgpucomp32.dll'  write to 0x013580D3\n"
            "             EIP 0x63486F01  'nvd3dum.dll'      DEP/execute\n"
            "             EIP 0x00000000  no module          DEP/execute\n"
            "           None of them is a driver bug. In all four the LAST Rag2.exe return\n"
            "           address is at [ESP] and is preceded by an indirect `call reg`, so the\n"
            "           client jumped there itself:\n"
            "             0x00921915 call edx   Process_StatueNPCShaders   (edx=0x646C6169)\n"
            "             0x00413F77 call edx   sub_413F60 = NiObject IsKindOf (0x676F4677)\n"
            "             0x0106D2B9 call eax   sub_106D280                (eax=0x63486F01)\n"
            "             0x007B221B call edx   UIShowroomWnd::OnUpdate    (edx=0)\n"
            "ROOT     : type confusion / use-after-free on the callee object. The object\n"
            "           pointer is a live heap address and passes every null test the code\n"
            "           already has (0x106D280 tests it TWICE), but its first dword is not a\n"
            "           vtable pointer -- it is a `const char*` into our own literal pool, so\n"
            "           `[vptr + slot]` reads TEXT and calls it. Proven byte-exact against\n"
            "           the file, not inferred:\n"
            "             0x01358029 + 8 = 0x01358031 -> 69 61 6C 64 = 'iald', the middle of\n"
            "                              the literal \"background/celestialdata\"\n"
            "             0x013580D3 + 8 = 0x013580DB -> 77 46 6F 67 = 'wFog', the middle of\n"
            "                              the literal \"g_dwFogColor\"\n"
            "           Both bogus vptrs are UNALIGNED (0x..29, 0x..D3), which no real vtable\n"
            "           pointer ever is. The garbage target then lands in whichever module\n"
            "           owns that address -- nvd3dum.dll is 68 MB, so ASCII lands in it easily\n"
            "           -- and THAT is the only reason the driver is named.\n"
            "FIX      : hook each site so the loaded call target is bounds-checked against\n"
            "           .text (0x00401000..0x0135478F) before the `call`, then replay the\n"
            "           overwritten instructions or take an exit the function already owns:\n"
            "             0x92190E  -> 0x921A37, the in-body 'skip this element' path\n"
            "             0x413F70  -> 0x413F6B, the function's own `return NULL`\n"
            "             0x106D2AD -> 0x106D2C0, the path it takes when the object is NULL\n"
            "             0x7B2215  -> 0x7B221D, the function's own else-branch\n"
            "EVIDENCE : the `< 0x10000` test the other guards use is USELESS here -- 3 of the\n"
            "           4 bad targets are huge. Hence the .text bound, and that bound is\n"
            "           MEASURED: 288 RTTI-COL-anchored vtables in .rdata, 2996 slots, and\n"
            "           ZERO interior slots outside .text (189 out-of-.text dwords all sit\n"
            "           past the end of a vtable, none sandwiched between .text entries).\n"
            "           All four sites are __thiscall (`this` in ECX, never pushed), so none\n"
            "           can be a COM/D3D interface whose vtable lives in d3d9.dll.\n"
            "           0x92190E's bail deliberately does NOT reuse the loop's own null-skip\n"
            "           at 0x921A3A: that path needs EAX to still hold the container, and by\n"
            "           0x92190E EAX has been overwritten with the bad vptr. 0x921A37 reloads\n"
            "           it from var_4. 0x106D2AD is hooked one instruction before the fault so\n"
            "           the bail never has to unwind the `push 1`. 0x7B2215 carries the same\n"
            "           pending `fldz` as its 0x7B2210 neighbour, so its bail pops x87 too.\n"
            "LIMITS   : guards, not root causes -- they stop the jump, not the corruption\n"
            "           that produced it. One dump in the set is NOT covered and cannot be:\n"
            "           the nvoglv32.dll fault at 0x5C79A299 is on a DRIVER-created worker\n"
            "           thread (stack 0x1C479FB4-0x1C480000, not the main thread's), and its\n"
            "           entire stack holds 0 Rag2.exe return addresses -- only d3d9.dll and\n"
            "           nvoglv32.dll. That one really is the driver's.\n"
            "RISK     : moderate, and higher than the other guard batches for ONE reason:\n"
            "           0x413F70 is NiObject::IsKindOf with 159 call sites, several on the\n"
            "           render path, so a wrong bound would misfire widely rather than in one\n"
            "           window. Ships DISABLED.\n"
            "*** THE MITIGATION FOR THAT RISK IS REFUTED (2026-08). *** This RISK line used to\n"
            "           end 'Bailing there returns NULL = not that type, which every caller\n"
            "           already handles, so the failure mode is a missing effect, not a crash.'\n"
            "           A dataflow pass over all 159 call sites finds at least FOUR that\n"
            "           dereference the result with NO null test: 0x51D063 InitPostEffectList_\n"
            "           Type1, 0x51D113 _Type2 and 0x51D1D3 _Type3 (all\n"
            "           `call sub_413F60; add esp,8; cmp dword ptr [eax+55Ch],0FFFF0300h` -- a\n"
            "           read at 0x55C off NULL), plus 0x4F7AEF\n"
            "           Actor_ApplyStatueNPCShaderRecursive (`mov esi,eax; mov eax,[esi];\n"
            "           mov edx,[eax+9Ch]`). Several more pass the result straight in as `this`\n"
            "           with no test: 0xB3AFA4 NiLightDimmerController::Update, 0xB3AFEE\n"
            "           ::UpdateValue, 0xB3B477 NiLightColorController::UpdateValue, 0xB352EC\n"
            "           NiMorphWeightsController::SynchronizePoseInterpolator.\n"
            "           So a false positive at 0x413F70 converts one rare crash into a NEW crash\n"
            "           on the post-effect / statue-shader / Ni*Controller paths. The named risk\n"
            "           is UNDERSTATED, not overstated.\n"
            "           NARROWER ALTERNATIVE, if this is ever wanted: the ONE caller actually\n"
            "           observed in dump #19 is 0xBE12B2 (via NiDX9Renderer::RenderMesh), and it\n"
            "           DOES test the result (`mov [ebp-14h],eax; cmp [ebp-14h],0;\n"
            "           je 0xBE12DB`). Guarding that single call site instead of the shared\n"
            "           159-site helper carries none of this blast radius.\n"
            "           FLIP CRITERION: do NOT flip the 0x413F70 guard on the old rationale.\n"
            "           Either narrow it to 0xBE12B2, or first confirm over a session that loads\n"
            "           a character, zones, opens the showroom/auction UI and fights -- with\n"
            "           effects, vehicles, lights and post-processing all still rendering."
        ),
        apply=apply_vcall_target_guards,
    ),
    Fix(
        id="attach_tooltip_reentrant_null_9da51d",
        module="stability",
        enabled=True,
        title="UIItemSlot_AttachTooltip: NULL virtual-call after a re-entrant tooltip teardown",
        why=(
            "SYMPTOM  : hard crash reading address 0x00000000. Caught LIVE in x64dbg (NOT in the\n"
            "           31-dump set): EIP 0x9DA523 `mov eax,[ecx]`, ECX=0, during rapid item-slot\n"
            "           refresh driven by the auto-pyramid. A debugger was attached, so the crash\n"
            "           reporter wrote no dump -- provenance is the live register state, N=1.\n"
            "ROOT     : UIItemSlot_AttachTooltip (0x9DA420) stores the tooltip object in\n"
            "           *(this+0x15C) (a2, null-checked non-null at entry) and makes four virtual\n"
            "           calls on it. The `vtable[+8](obj, 2, [esi+150h]==0)` call at 0x9DA518\n"
            "           re-enters and writes 0 to *(this+0x15C) -- the same store the function's\n"
            "           own cleanup takes at 0x9DA450 -- so the reload at 0x9DA51D for the next\n"
            "           `vtable[+8](obj, 1, ..)` call reads NULL and 0x9DA523 dereferences it.\n"
            "           A constant fault of exactly 0 (not a large garbage pointer) means a plain\n"
            "           null, so `test ecx,ecx`/`jz` catches it; no 0x10000 threshold is needed.\n"
            "FIX      : hook the 6-byte `mov ecx,[esi+15Ch]` at 0x9DA51D into a cave that reloads\n"
            "           the object and, if it is NULL, bails to 0x9DA530 -- the next block, which\n"
            "           is independent of *(this+0x15C) and already null-guards its own pointers.\n"
            "           The skipped call is only the tooltip's 2nd show-state; the epilogue then\n"
            "           returns *(this+0x15C)=0, which the original returns anyway.\n"
            "EVIDENCE : hook point verified as a clean mid-basic-block -- the only xref into\n"
            "           0x9DA51D..0x9DA522 is fall-through from 0x9DA51A, and 0x9DA52E/0x9DA530\n"
            "           are both instruction heads. EDX (`[esi+7Ch]`, consumed by the replayed\n"
            "           `and edx,1`) is loaded at 0x9DA51A, before the hook, so it stays live.\n"
            "RISK     : low. The guard only changes the NULL path (which crashed before); on the\n"
            "           normal path the cave is byte-for-byte the original sequence. Worst case\n"
            "           the tooltip's final show-state is not applied for that one hover frame."
        ),
        apply=apply_attach_tooltip_guard,
    ),
    Fix(
        id="polishing_packet_null_guard_6d0e0b",
        module="stability",
        enabled=True,
        title="HandlePolishingPacket: NULL polishing context dereferenced before its own null check",
        why=(
            "SYMPTOM  : hard crash reading address 0x0000015C. From an UNPATCHED-client dump set\n"
            "           (\"Nama crashes after exe\"): EIP 0x6D0E11 `mov eax,[eax+15Ch]`, read fault\n"
            "           0x15C. (That set also shows the 4 already-guarded sites -- GetActiveVehicle,\n"
            "           the SpeedTree OOM int3, UIShowroom vptr -- which only fault on a client that\n"
            "           does NOT have these patches, confirming the source exe was stock.)\n"
            "ROOT     : HandlePolishingPacket (0x6D0D60) does `mov eax,[ebx+150h]` = *(this+336),\n"
            "           then `mov eax,[eax+15Ch]` -- dereferencing that pointer BEFORE the\n"
            "           function's own `if(!v5) return 0` check on the loaded value. When\n"
            "           *(this+336) (the polishing UI context) is NULL, it reads [0 + 0x15C].\n"
            "FIX      : hook the 6-byte `mov eax,[ebx+150h]` @0x6D0E0B into a cave that reloads\n"
            "           the pointer and, if NULL, bails to the function's OWN return-0 path at\n"
            "           0x6D0D7D (`xor al,al; pop ebx; cookie epilogue`). Reachable because the\n"
            "           hook is after the prologue `push ebx`, so the pop balances the stack.\n"
            "EVIDENCE : only xref into 0x6D0E0B..0x6D0E10 is fall-through from 0x6D0E09; the\n"
            "           resume (0x6D0E17) and bail (0x6D0D7D) are both instruction heads; eax is\n"
            "           dead on entry (loaded at the hook).\n"
            "RISK     : low. Only the NULL path changes (it crashed before); on the normal path\n"
            "           the cave is byte-for-byte the original two loads. A NULL context means the\n"
            "           polishing window is not open, so returning 0 (no-op) is the correct result."
        ),
        apply=apply_polishing_null_guard,
    ),
    Fix(
        id="syncmount_first_null_guard_90b447",
        module="stability",
        enabled=False,   # SUPERSEDED: CF1 in `cf_null_guards` already guards this exact First()+0x14
                         # site (bail 0x90B571). Enabling both makes cf_null_guards skip (its probe
                         # sees this hook first) and LOSES CF4/CF5. Kept disabled for the record.
                         # The 25x Zhong hits are only because that client is stock (no CF1).
        title="[DISABLED — superseded by CF1] CGameActor::SyncMountMotion First() NULL guard",
        why=(
            "SYMPTOM  : the #1 recurring crash in the 192-dump \"Zhong\" set (stock client) -- 25\n"
            "           dumps at EIP 0x90B447 `mov eax,[eax+14h]`, read fault at a small offset.\n"
            "ROOT     : eax is the return of `Concurrency::List::First(GetActiveVehicle(this))`.\n"
            "           When the active vehicle's model list is empty, First() returns NULL and\n"
            "           the code reads [0 + 0x14] with no null check.\n"
            "FIX      : hook the 9-byte `mov eax,[eax+14h]; push eax; jmp loc_90B535` @0x90B447\n"
            "           into a cave that null-checks the First() result; if NULL it bails to the\n"
            "           function's own return at 0x90B59D (the vehicle state does not change\n"
            "           within the function, so returning early just skips one frame of mount\n"
            "           sync -- cosmetic). The normal path replays the two ops and jumps to\n"
            "           loc_90B535 unchanged.\n"
            "EVIDENCE : nothing branches into 0x90B447..0x90B44F (only sequential flow); the\n"
            "           resume (loc_90B535) and bail (0x90B59D, the `pop edi` epilogue) are heads.\n"
            "RISK     : low -- only the NULL path changes; normal path is byte-identical."
        ),
        apply=apply_syncmount_null_guard,
    ),
    Fix(
        id="equip_ui_skillname_null_guard_7a356b",
        module="stability",
        enabled=True,
        title="EquipmentUI_UpdateUI: NULL skill-name map entry dereferenced at [+0x1C]",
        why=(
            "SYMPTOM  : the #3 recurring crash in the \"Zhong\" set -- 14 dumps at EIP 0x7A356B\n"
            "           `cmp dword ptr [ebx+1Ch],8`.\n"
            "ROOT     : ebx is a skill-name std::wstring entry fetched from a std::map lookup\n"
            "           (key 94). When the lookup misses, the code sets the entry to NULL and\n"
            "           still runs the wstring SSO check `cmp [ebx+1Ch],8`, reading [0 + 0x1C].\n"
            "FIX      : hook the 7-byte `cmp [ebx+1Ch],8; lea eax,[ebx+8]` @0x7A356B into a cave\n"
            "           that null-checks ebx; if NULL it bails to the loop-continue at 0x7A3622\n"
            "           (skip this skill's icon update, advance to the next slot). The normal\n"
            "           path replays cmp+lea and jumps to 0x7A3572 -- the `jb` there consumes\n"
            "           the cmp's flags and `lea` leaves them intact, so they survive.\n"
            "EVIDENCE : only sequential flow enters 0x7A356B..0x7A3571; 0x7A3622 is the loop\n"
            "           tail (`mov ebx,[var_4]; ... add ebx,18h`), i.e. skip-to-next-slot.\n"
            "RISK     : low -- only the NULL path changes; normal path is byte-identical."
        ),
        apply=apply_equip_ui_null_guard,
    ),
    Fix(
        id="skillicon_clearlinked_null_guard_9d72a6",
        module="stability",
        enabled=True,
        title="UISkillIcon::ClearLinkedUI: NULL linked-UI object release-vcall (auto-pyramid)",
        why=(
            "SYMPTOM  : hard crash reading address 0x00000000. Caught LIVE in x64dbg: EIP\n"
            "           0x9D72AC `mov edx,[ecx]`, ECX=0, with the call stack going through\n"
            "           ro2mods.AutoPyramid::Update -> the overlay window proc -> rag2's UI.\n"
            "ROOT     : `mov ecx,[esi+15Ch]; mov edx,[ecx]; mov eax,[edx+8]; push 1; push 20h;\n"
            "           call eax` = the `vtable[+8](linkedUI, 32, 1)` release call on the object\n"
            "           at *(this+0x15C). The auto-pyramid refreshes UI slots fast enough that\n"
            "           the object is freed/nulled re-entrantly, then this release vcalls NULL.\n"
            "           Same class as the 0x9DA51D AttachTooltip guard, a sibling site.\n"
            "FIX      : hook the 6-byte `mov ecx,[esi+15Ch]` @0x9D72A6 into a cave that\n"
            "           null-checks the object; if NULL it skips the release and reconverges at\n"
            "           0x9D72B7 (the next block reloads its own pointer from [esi+1D0h], so\n"
            "           skipping a release of an already-gone object is safe).\n"
            "EVIDENCE : the only branch into the hook region targets 0x9D72A6 itself (the E9\n"
            "           start), not the interior; 0x9D72B7 is an instruction head.\n"
            "RISK     : low -- only the NULL path changes; normal path is byte-identical."
        ),
        apply=apply_skillicon_clearlinked_guard,
    ),
    Fix(
        id="struct_invariant_guards_2026_08",
        module="stability",
        enabled=True,
        title="[.patch caves] Three guards on pointers that are STRUCTURALLY impossible, not just null",
        why=(
            "SYMPTOM  : two more of the 31 minidumps, plus one un-crashed sibling site:\n"
            "             0x480D00  write, fault 0x10028, ecx=0x10000  CEffectMgr::UpdateTargetEffects\n"
            "             0xC38FD3  read,  edx=0x03F83C4D              sub_C38F90 (NiTArray::GetIndexOf*)\n"
            "             0x9D9E26  (no dump -- see EVIDENCE)          UIItemHandler_OnInputEvent\n"
            "ROOT     : the first two are pointers that no THRESHOLD test can catch, because the\n"
            "           bad values are large. What is wrong with them is their SHAPE: a\n"
            "           std::vector::begin() that is 2 mod 4, and a 4-byte-element array base\n"
            "           that is 1 mod 4. Neither can come from a real allocation, so both are\n"
            "           caught by an alignment test that provably cannot fire on healthy data.\n"
            "FIX      : hook each into a `.patch` cave that applies the invariant, replays the\n"
            "           overwritten instructions on the good path, and otherwise takes an exit\n"
            "           the function already owns:\n"
            "             0x4803E0 -> 0x480D35, the inlined ++it (skip the corrupt node)\n"
            "             0xC38FCD -> 0xC38FE2, the function's own `or eax,-1; retn 8`\n"
            "             0x9D9E26 -> 0x9D9E45, its own 'no item' continuation\n"
            "EVIDENCE : 0x480D00 is the strongest of the three. The loop cursor advances only by\n"
            "           `add ebx,4`, so begin == ebx (mod 4); the recorded ebx is 2 mod 4, hence\n"
            "           begin was misaligned regardless of how far the walk had gone. And the\n"
            "           address it dereferenced sits in read-only .rdata where the dword is\n"
            "           literally 0x00010000 -- a constant read off-alignment. ONE hook covers\n"
            "           all FOUR of that function's identical vector loops.\n"
            "           0xC38FD3's bail additionally makes its caller SKIP a further read through\n"
            "           the same bad base.\n"
            "           0x9D9E26 has NO CRASH DUMP -- its evidence is inferential-from-sibling:\n"
            "           it is the only unguarded instance in the image of the same\n"
            "           [+0x98]->[+0x118] chain whose sibling at 0x9CC85A crashed 3 times with\n"
            "           eax==0, and the same function null-tests that field at six other sites.\n"
            "           Do not cite it as crash-derived.\n"
            "LIMITS   : GUARDS, NOT ROOT CAUSES, and all three are N=1 or N=0. An alignment test\n"
            "           says 'this pointer is corrupt', never why it went corrupt -- 0x480D00's\n"
            "           real cause is upstream memory corruption, and 0xC38FD3's is a teardown of\n"
            "           an already-freed particle simulator. The 0xC38FD3 alignment premise is\n"
            "           inferred from scale-4 INDEXING rather than from the allocator, and it\n"
            "           rejects only 3 of 4 uniformly random garbage bases. 'Catches future\n"
            "           instances' is NOT established for any of the three.\n"
            "RISK     : low. No new control flow is invented -- every bail is a target the\n"
            "           function already branches to -- and each guard's test is one that cannot\n"
            "           fire on a well-formed structure. Never runtime-tested."
        ),
        apply=apply_struct_invariant_guards,
    ),
    Fix(
        id="alloc_fail_writes_null_55af44",
        module="stability",
        enabled=True,
        title="String_ResizeBuffer: an allocation-failure branch that jumps ONTO a write to address 0",
        why=(
            "SYMPTOM  : no dump in the current set -- this is a STATIC find, but of the same\n"
            "           class as the confirmed 0x9CC843 bug and with the same shape: a null test\n"
            "           whose branch lands on the dereference it exists to prevent.\n"
            "ROOT     : at 0x55AF3E String_ResizeBuffer (0x55AEF0) does\n"
            "           `call HeapAllocWrapper; add esp,4; test eax,eax; jz short 0x55AF53`.\n"
            "           0x55AF53 is `mov [eax],esi`. So on allocation FAILURE the code jumps over\n"
            "           the two-field header init and straight onto a WRITE to address 0, then\n"
            "           continues `lea esi,[eax+8]` and memcpy's into address 8.\n"
            "           The null path is real, not theoretical: HeapAllocWrapper (0xEFEF30) is\n"
            "           literally `HeapAlloc(GetProcessHeap(), 0, n)` -- dwFlags=0, so no\n"
            "           HEAP_GENERATE_EXCEPTIONS; it RETURNS NULL rather than raising.\n"
            "WHY IT MATTERS HERE : an allocation that fails is exactly the 32-bit address-space\n"
            "           exhaustion regime this whole catalog exists for. `laa` makes it rarer;\n"
            "           this makes it survivable.\n"
            "FIX      : `74 0D` -> `74 64` at 0x55AF44, retargeting the jz to 0x55AFAA\n"
            "           (`pop edi; pop esi; pop ebp; retn 4`).\n"
            "EVIDENCE : 0x55AFAA is ALREADY the target of the jz at 0x55AF33 (the same-size\n"
            "           early-out), so it is a state the function already produces. Stack proven\n"
            "           balanced there: the prologue is push ebp / mov ebp,esp / push esi /\n"
            "           push edi with NO ebx, and the only `push ebx` (0x55AF57) and its\n"
            "           `pop ebx` (0x55AFA9) both lie strictly INSIDE the span the retarget\n"
            "           skips. No x87 anywhere on the path. Anchor unique in both builds.\n"
            "RETURN CONTRACT : the new bail returns eax=0 (the failed allocation). That is\n"
            "           already producible by the existing bail (via `xor eax,eax` at 0x55AF2A\n"
            "           when esi==0), so it stays inside the function's existing contract, and\n"
            "           the important postcondition holds -- [edi] keeps the OLD buffer, so the\n"
            "           string object stays valid.\n"
            "RISK     : very low. ONE byte, on a branch taken only when the allocation failed --\n"
            "           a path that today is a guaranteed write to address 0. When the\n"
            "           allocation succeeds, not one byte of executed code differs."
        ),
        apply=lambda d: patch_bytes(d, "85c0740dc70000000000c74004010000008930",
                                       "85c07464c70000000000c74004010000008930"),
    ),
    Fix(
        id="npc_dialog_partial_null_guard",
        module="stability",
        enabled=True,
        title="UINpcDialog(2)Wnd::OnCreate: a guarded FP block followed by an UNguarded virtual call",
        why=(
            "SYMPTOM  : no dump in the current set -- static, same class as 0x9CC843.\n"
            "ROOT     : the classic partially-guarded source shape, `if (p) { ...math... }\n"
            "           p->SetVisible(...)`. At 0x75CA4D UINpcDialogWnd::OnCreate (0x75C9C0)\n"
            "           does `call UIWindow__FindChildControl; mov edi,eax; test edi,edi;\n"
            "           jz short 0x75CA7E`. The jz skips ONLY the guarded FP block\n"
            "           (0x75CA56-0x75CA7D) and lands on 0x75CA7E `mov eax,[edi]; mov edx,[eax+8];\n"
            "           push 1; push 20h; mov ecx,edi; call edx` -- a virtual call on a NULL\n"
            "           object. 0x75F304 in the sibling class UINpcDialog2Wnd::OnCreate is the\n"
            "           byte-for-byte same defect.\n"
            "FIX      : `74 28` -> `74 35` at both sites, retargeting to the NEXT\n"
            "           FindChildControl (0x75CA8B / 0x75F33B).\n"
            "EVIDENCE : the correct bail demonstrably exists -- the very next statement in the\n"
            "           same function does the identical FindChildControl and DOES\n"
            "           `test edi,edi; jz loc_75CB25`. Semantics corroborated by the control\n"
            "           names: 0x75CA44 pushes 'NPC_DIALOG_BUTTON_SELECT_2' and the retarget\n"
            "           lands on the lookup of 'NPC_DIALOG_IMAGE_TITLE_ICON1' (the sibling uses\n"
            "           'NPC_DIALOG2_*').\n"
            "           FPU-safe: the skipped block is self-balancing (fld/fstp pairs, then\n"
            "           fsubp and __ftol2_sse, whose 0xACB990 does `fstp qword [esp]`), and the\n"
            "           retarget only skips ADDITIONAL non-FPU instructions. The extra span\n"
            "           0x75CA7E..0x75CA8A has its `push 1` / `push 20h` consumed by the very\n"
            "           `call edx` that is skipped with them, so ESP is balanced.\n"
            "ANCHOR   : the 20-byte anchors are MANDATORY, not defensive. The 8-byte form\n"
            "           `7428d94740d95dc4` matches BOTH sites, and even 13 bytes still matches\n"
            "           both; only reaching back to the string push and the call rel32 makes\n"
            "           each unique. Applied all-or-nothing.\n"
            "RISK     : very low. One byte per site, on a branch taken only when the control was\n"
            "           not found -- today a guaranteed null virtual call."
        ),
        apply=lambda d: patch_all_bytes(d, [
            ("681c5f3b018bcee840c390008bf885ff7428d947",     # UINpcDialogWnd  @0x75CA54
             "681c5f3b018bcee840c390008bf885ff7435d947"),
            ("6878623b018bcee8909a90008bf885ff7428d947",     # UINpcDialog2Wnd @0x75F304
             "6878623b018bcee8909a90008bf885ff7435d947"),
        ]),
    ),
    Fix(
        id="pose_handler_this_guard",
        module="stability",
        enabled=False,   # DISABLED 2026-08: the 8-alignment premise was FALSIFIED, and if it is
                         # wrong this guard rejects EVERY entry and silently kills the pose path
                         # for all actors on all frames. See "PREMISE REFUTED" in why=.
        title="[DISABLED -- premise refuted] NiMultiTargetPoseHandler::Update: reject a float used as `this`",
        why=(
            "SYMPTOM  : the single most common crash in the 2026-07/08 minidump set --\n"
            "           5 of 31 dumps, all writing through a bad pointer at EIP 0x477323.\n"
            "ROOT     : sub_477300 is a `__thiscall` float setter ending in\n"
            "           `fstp dword ptr [ecx+60h]`. At the 0xB19EC1 call site inside\n"
            "           NiMultiTargetPoseHandler::Update, `this` comes from\n"
            "           `[[ebp+var_10]+4]` -- and that slot held a FLOAT, not a pointer.\n"
            "           The five faulting addresses decode as 0.000134, 0.032264,\n"
            "           0.488712, 1.097362 and 1.115463: animation blend weights.\n"
            "           The guard just above (sub_B1C2B0) only checks a u16 type tag at\n"
            "           [obj+2], so a mistyped/stale entry passes it and the +4 field is\n"
            "           then trusted as an object.\n"
            "FIX      : route THAT ONE call through a 17-byte `.patch` cave that rejects\n"
            "           ecx==0 and ecx not 8-byte aligned, then tail-jumps to the real\n"
            "           setter. On reject it does `retn 4` -- the setter's own arg\n"
            "           cleanup -- so the stack is identical either way and that entry's\n"
            "           pose update is simply skipped for the frame.\n"
            "EVIDENCE : all 5 dumps carry the SAME return address 0xB19EC6 on the stack\n"
            "           and the SAME ESP (0x1AEB28), so the call path is fixed. All five\n"
            "           observed bad values are 4- but NOT 8-aligned (5/5), so the guard\n"
            "           WOULD reject all five. A range test was measured and REJECTED: the\n"
            "           0x38000000-0x42000000 band those floats live in contains 116\n"
            "           genuinely committed regions in the dumps, so a range check would\n"
            "           cause false skips. All cave mechanics are verified: 17 B, fully\n"
            "           decodes, zero mid-instruction targets, both rejects land on the\n"
            "           `retn 4`, `test cl,7` is the right test with both senses correct,\n"
            "           and sub_477300 is x87-neutral so the reject path needs no FPU\n"
            "           cleanup. At the TYPE level the premise is also supported: at\n"
            "           0xB19C9B..0xB19CAC the same `record+4` field is dereferenced as a\n"
            "           vptr and virtual-called via [vptr+98h], so a float there really is\n"
            "           a type confusion.\n"
            "*** PREMISE REFUTED 2026-08 -- THIS IS WHY THE FIX IS NOW DISABLED. ***\n"
            "           The why= used to assert: 'MSVC `operator new` returns 8-byte-aligned\n"
            "           blocks, so a real object here is always 8-aligned'. That is a\n"
            "           CRT-heap claim, and this is a Gamebryo build with its OWN allocator.\n"
            "           Counterexample from the dumps themselves, with a control that could\n"
            "           have failed (var_10 recovered from stack[116] == EDX at fault time in\n"
            "           5/5): var_1D0 is the `this` of NiMultiTargetPoseHandler::Update\n"
            "           itself -- a live, valid, polymorphic object dereferenced at\n"
            "           +84h/+88h/+8Ah throughout the function -- and its values\n"
            "           (0x097D2394, 0x098AEEC4, 0x098D50DC, 0x097E4DB4, 0x097E9814) are 4-\n"
            "           but NOT 8-aligned in 5 of 5. Base rate across all 31 dumps: of 98\n"
            "           heap-looking 4-aligned register values only 58 (59.2%) are 8-aligned.\n"
            "           The discriminator is also ONE-SIDED: 5 negative samples, ZERO positive\n"
            "           samples of a legitimate `[record+4]`.\n"
            "           FAILURE MODE IF WRONG: the guard rejects EVERY entry and the pose path\n"
            "           is silently dead for ALL actors on ALL frames. The old RISK line --\n"
            "           'worst case one skipped pose update on one entry in one frame' --\n"
            "           understated that by orders of magnitude.\n"
            "TO RE-ENABLE : produce ONE positive sample of a legitimate `[record+4]` and\n"
            "           measure its alignment (breakpoint 0xB19EC1 in x64dbg and log ecx over\n"
            "           a play session, or find the allocation site of the objects whose\n"
            "           vtable slot +98h is called at 0xB19CAC). If they are not reliably\n"
            "           8-aligned, switch to a discriminator that is -- e.g. require [ecx] to\n"
            "           be a vptr inside .rdata AND [[ecx]] inside .text, which is exactly the\n"
            "           test `vcall_target_guards_2026_08` already builds.\n"
            "LIMITS   : this is a GUARD, not a root-cause fix -- it does not explain why\n"
            "           the +4 slot goes stale. ~1 in 8 bad float patterns are 8-aligned\n"
            "           by chance and would still get through. Only the 0xB19EC1 call site is\n"
            "           affected; the 20 other callers of sub_477300 are untouched, including\n"
            "           0xB1A7C4, a SECOND call site inside this same function.\n"
            "RISK     : see PREMISE REFUTED. Not shippable until a positive sample exists."
        ),
        apply=apply_pose_this_guard,
    ),
    Fix(
        id="null_check_jumps_into_deref_9cc843",
        module="stability",
        enabled=True,
        title="sub_9CC520: a null check whose branch lands ON the dereference it guards",
        why=(
            "SYMPTOM  : hard crash reading address 0x00000118. Seen 3 times in the\n"
            "           2026-07/08 minidump set (07-16, 08-01, 08-02) -- 3 of 31 dumps --\n"
            "           always at EIP 0x9CC85A and always fault=0x118 EXACTLY. A constant\n"
            "           fault address that equals a struct offset means a null base, not\n"
            "           memory pressure and not a race.\n"
            "ROOT     : 0x9CC83B loads `eax = [esi+98h]` and tests it for NULL. The\n"
            "           `jz short loc_9CC85A` at 0x9CC843 is meant to skip the block, but\n"
            "           0x9CC85A is `cmp dword ptr [eax+118h], 8` -- INSIDE that block and\n"
            "           the very dereference the test exists to prevent. The null path\n"
            "           therefore reads [0 + 0x118] and dies.\n"
            "FIX      : retarget that one branch: 74 15 -> 74 24, i.e. `jz 0x9CC869`\n"
            "           instead of `jz 0x9CC85A`. 0x9CC869 is `test eax,eax` followed by\n"
            "           `jz short loc_9CC8D3`, so with eax==0 control falls straight to\n"
            "           the function's normal epilogue. Nothing else moves.\n"
            "EVIDENCE : the skipped span 0x9CC845..0x9CC869 is only mov/cmp/jcc -- NO push\n"
            "           -- so ESP is unchanged and the epilogue (`pop esi;\n"
            "           __security_check_cookie; retn`, a bare ret with no arg cleanup)\n"
            "           stays balanced. The skipped block is a sound/effect call chain\n"
            "           (sub_107D570 / sub_107D640 / sub_106C220) that could not have run\n"
            "           with a null object anyway. Anchor pattern occurs EXACTLY ONCE in\n"
            "           both Rag2.exe and Rag2_original.exe (file offset 0x5CBC3B).\n"
            "           This region is untouched by any patch -- it is a stock game bug.\n"
            "BASELINE CORRECTED 2026-08 (the conclusion above survives, the supporting\n"
            "           sentence did not): the byte diff between the two binaries is 200 bytes\n"
            "           over ~55-61 coalesced regions, but the attribution was inverted --\n"
            "           51 runs / 182 B are in .text and 10 runs / 18 B in the PE header, with\n"
            "           ZERO bytes in .rdata. And that diff is not 'our optimization patches':\n"
            "           it largely consists of THIS PATCHER's own stability guards already\n"
            "           applied (0x6F69AB, 0x67D982, 0x6A5664, 0x6B5227, 0x709A51, 0x7727FC and\n"
            "           the 0x53D9xx cluster), so it was never a clean control for 'stock vs\n"
            "           optimized'. The substantive claim (this region untouched) is measured\n"
            "           directly and still holds.\n"
            "RISK     : low. One branch displacement. The null path stops crashing and\n"
            "           returns normally; the non-null path is byte-identical."
        ),
        apply=lambda d: patch_bytes(d, "8b869800000085c074158b881801000083f905",
                                       "8b869800000085c074248b881801000083f905"),
    ),
    Fix(
        id="send_equip_nullvcall",
        module="stability",
        enabled=True,
        title="UICharacterWnd equipment-send: skip null-vtable call (crash guard)",
        why=(
            "SYMPTOM  : crash on equipment update when the RMI/auth layer is not ready.\n"
            "ROOT     : 0x67D982 else-branch does `xor ecx,ecx; mov edx,[ecx]; ...; call\n"
            "           edx` through a null object.\n"
            "FIX      : 33 C9 -> EB 0E: jump past the null send to 0x67D992 (cookie\n"
            "           epilogue), function returns its usual al=1. Valid path (entered\n"
            "           at 0x67D984 via its own jump) is unchanged.\n"
            "RISK     : low. The send silently no-ops instead of crashing, only when the\n"
            "           interface is null (the crashing state)."
        ),
        apply=lambda d: patch_bytes(d, "33c98b118b92c805", "eb0e8b118b92c805"),
    ),
    Fix(
        id="ui_send_nullvcall_6b5110",
        module="stability",
        enabled=True,
        title="UI sender sub_6B5110: skip null-vtable call (crash guard)",
        why=(
            "SYMPTOM  : crash from a UI-triggered send when RMI/auth is null.\n"
            "ROOT     : 0x6B5227 else-branch null-vtable call (`+0BC4h`).\n"
            "FIX      : 33 C9 -> EB 0E: jump to the epilogue at 0x6B5237 (void return).\n"
            "RISK     : low (void function; no return value at stake)."
        ),
        apply=lambda d: patch_bytes(d, "33c98b118b92c40b", "eb0e8b118b92c40b"),
    ),
    Fix(
        id="ui_send_nullvcall_7099f0",
        module="stability",
        enabled=True,
        title="UI sender sub_7099F0: skip null-vtable call (crash guard)",
        why=(
            "SYMPTOM  : crash from a UI-triggered send when RMI/auth is null.\n"
            "ROOT     : 0x709A51 else-branch null-vtable call (`+788h`).\n"
            "FIX      : 33 C9 -> EB 0E: jump to 0x709A61; the function's own local\n"
            "           container cleanup + void return still run.\n"
            "RISK     : low (void function)."
        ),
        apply=lambda d: patch_bytes(d, "33c98b118b928807", "eb0e8b118b928807"),
    ),
    Fix(
        id="ui_send_nullvcall_6a55f0",
        module="stability",
        enabled=True,
        title="UI updater sub_6A55F0: skip null-vtable call (crash guard)",
        why=(
            "SYMPTOM  : crash during a UI value refresh when RMI/auth is null.\n"
            "ROOT     : 0x6A5664 else-branch null-vtable call (`+0F24h`).\n"
            "FIX      : 33 C9 -> EB 0E: jump to 0x6A5674; the trailing UIEditBox refresh\n"
            "           runs on both paths and the function still returns its constant 1.\n"
            "RISK     : low."
        ),
        apply=lambda d: patch_bytes(d, "33c98b118b92240f", "eb0e8b118b92240f"),
    ),
    Fix(
        id="ui_msg_nullvcall_7726f0",
        module="stability",
        enabled=True,
        title="UI message dispatch sub_7726F0: skip null-vtable call (crash guard)",
        why=(
            "SYMPTOM  : crash in a UI event/message dispatch when RMI/auth is null.\n"
            "ROOT     : 0x7727FC null branch (a duplicated block, entered via jz) does\n"
            "           the null-vtable call (`+0AE8h`).\n"
            "FIX      : 33 C9 -> EB 0B (0x0B not 0x0E -- the pushed arg is loaded before\n"
            "           the xor here): jump to 0x772809, `mov eax,1; ...; retn 4`.\n"
            "RISK     : low. This block is exclusively the null path; the valid block\n"
            "           (0x7727DD) is separate and untouched."
        ),
        apply=lambda d: patch_bytes(d, "33c98b118b92e80a", "eb0b8b118b92e80a"),
    ),
    Fix(
        id="rmi_nullvcall_guards",
        module="stability",
        enabled=True,
        title="28 more RMI/UI null-vtable-call crash guards (same family, batched)",
        why=(
            "SYMPTOM  : hard client crashes on network/UI/state events (login<->char-select,\n"
            "           disconnect, map/channel change, party/skill/arena RMI) when a\n"
            "           singleton (CAuth or its +5Ch session object) is null.\n"
            "ROOT     : 28 more sender sites of the SAME idiom as the 5 guards above -- the\n"
            "           singleton-null ELSE branch still virtual-calls through a null object\n"
            "           (`xor reg,reg; mov edx,[reg]; ...; call edx`). Includes the party-\n"
            "           update dispatch (0x4963xx) and the big RMI switch cluster (0x53D9xx).\n"
            "FIX      : each turns the branch's `xor reg,reg` into a short/near jmp that\n"
            "           skips the null send and reconverges with the valid path. Applied as a\n"
            "           verified group, all-or-nothing (on a trial copy first).\n"
            "*** 2026-08: ONE SITE WAS CORRUPTING MEMORY ON THE HEALTHY PATH. *** The why= used\n"
            "           to say each guard 'reconverges with the valid path (which jumps in\n"
            "           independently)' and that targets were 'sampled in IDA'. That premise is\n"
            "           exactly what produced the bug, and the unsampled site was the wrong one:\n"
            "           at 0x53DC59 the compiler TAIL-MERGED the non-null counterpart instead of\n"
            "           duplicating it, so `jmp short loc_53DC5B` at 0x53DC57 -- the VALID path's\n"
            "           entry -- jumps INTO the window the 5-byte near-jmp overwrote. The common\n"
            "           path then executed `add byte ptr [eax],al` on the live CAuth+0x5C session\n"
            "           object followed by `call edx` with edx never loaded. Corrected to a\n"
            "           2-byte `EB 0E` reaching the same target; see the table entry and\n"
            "           _NULLVCALL_REPAIR, which also heals builds already carrying the bad form.\n"
            "EVIDENCE : all 28 byte-verified at their VA. Every rewritten byte was checked for\n"
            "           incoming non-flow xrefs. The 21 two-byte `EB` sites are safe BY\n"
            "           CONSTRUCTION -- the patch exactly covers the 2-byte `xor`, so the\n"
            "           instruction boundary at VA+2 is preserved and no scan is needed. For the\n"
            "           7 near-jmp sites the target was re-derived individually.\n"
            "           Stack balance: for the EB sites the skipped span is matched push(es) plus\n"
            "           one indirect call, so ESP is identical either way. For the E9 sites the\n"
            "           spans are large (up to 218 instructions / 37 calls) and contain whole\n"
            "           other jump-table cases -- that code is not 'skipped', it is UNREACHABLE\n"
            "           on this path both before and after the patch.\n"
            "           12 of the 28 targets have a real inbound branch xref; the other 16 are\n"
            "           either the null block's own trailing `jmp` or the instruction right after\n"
            "           the skipped call. (An earlier note claimed 26 of 28 were pre-existing\n"
            "           branch targets; measured, it is 12.)\n"
            "COVERAGE : 33 of 141 sites of this idiom in .text are guarded (these 28 + the 5\n"
            "           individual fixes) = 23%. 108 remain UNGUARDED. The old '4 medium-risk\n"
            "           sites were dropped' implied near-complete coverage; those 4 are real but\n"
            "           they are 4 of 108.\n"
            "RISK     : low-to-moderate. Only fires on the null-singleton path (which crashed\n"
            "           before) and the request silently no-ops -- but note this family has ZERO\n"
            "           crash-dump corroboration: not one of the 31 minidumps lands at any of\n"
            "           these 28 sites, nor at ANY of the 141 sites of this idiom. The whole\n"
            "           batch rests on a structural argument (each patched path is an\n"
            "           unconditional AV at address 0 in the stock binary), not on observation."
        ),
        apply=lambda d: apply_rmi_nullvcall_guards(d),
    ),
    Fix(
        id="cf_null_guards",
        module="stability",
        enabled=True,   # flipped 2026-08: the minidump set PROVES CF5 fires -- 0x8FD2E7 is
                        # one of the 31 dumps (read [ecx+180h] with this==NULL). CF1/CF4
                        # ride along; they are the same guard family as the other
                        # stability fixes that already ship on.
        title="[.patch caves] CF1/CF4/CF5 NULL-this/First() crash guards (from the b303 catalog)",
        why=(
            "SYMPTOM  : three recurring crashes catalogued in the b303 patcher:\n"
            "           CF1 SyncMountMotion (25), CF4 sub_413210 (6), CF5 sub_8FD2E0 (4).\n"
            "ROOT     : each dereferences a pointer without a NULL check -- CF1 uses\n"
            "           First()'s result (`mov eax,[eax+14h]`), CF4/CF5 use a null `this`.\n"
            "FIX      : these need a code cave (a NULL test + branch is longer than the\n"
            "           original bytes), which this patcher now supports via a `.patch`\n"
            "           section. Each hook jmps to a cave that tests the pointer and either\n"
            "           replays the original instruction(s) and jumps back (valid), or\n"
            "           skips to a safe return / reconvergence (null): CF1 -> 0x90B571,\n"
            "           CF4 -> `xor eax,eax; retn 4`, CF5 -> the fn's own FALSE epilogue.\n"
            "EVIDENCE : ret forms read from the real epilogues (CF4 retn 4, CF5 retn/C3);\n"
            "           reconvergence targets validated by xref; cave bytes verified to\n"
            "           disassemble correctly -- all three caves assemble to exactly what\n"
            "           their comments claim, every internal jz displacement lands on its\n"
            "           label, all five reconvergence targets are real instruction\n"
            "           boundaries, and both bail targets (0x90B571, 0x8FD333) are\n"
            "           PRE-EXISTING branch targets rather than invented addresses.\n"
            "PROVENANCE CORRECTED 2026-08 : the old line credited these as 'the proven catalog\n"
            "           crash fixes (patch_all.py ships them by default)'. They were RE-DERIVED\n"
            "           here, and where this port diverges from that catalog it diverges because\n"
            "           the catalog is WRONG: patch_all's CF1 bails to 0x90B5A0, skipping three\n"
            "           pops, and its CF5 bails pre-prologue. Both were silently corrected in\n"
            "           this implementation. Take the credit, do not cite the source as proof.\n"
            "*** THIS FIX IS ON BY DEFAULT. *** The RISK line used to end 'Cave-based -> off by\n"
            "           default to keep the default build section-free; enable for the extra\n"
            "           coverage', while enabled=True. It is in fact the ONLY reason a default\n"
            "           build grows a `.patch` section at all. A reader trusting that sentence\n"
            "           believed the default build was section-free; it is not.\n"
            "SCOPE    : CF1 guards 0x90B447 ONLY. The same function has three further derefs of\n"
            "           a First() result (0x90B531, 0x90B550, 0x90B569) that stay unguarded.\n"
            "RISK     : low (only the null path changes), with one caveat the old line omitted:\n"
            "           CF1/CF4/CF5 use `test reg,reg` (exact null only), whereas every\n"
            "           minidump-derived guard in this file uses `cmp reg,10000h` because several\n"
            "           observed bad pointers were garbage rather than zero. CF5's single dump is\n"
            "           exactly null so `test` suffices there; CF1 and CF4 have NO dump in this\n"
            "           set, so for them the weaker predicate is unvalidated."
        ),
        apply=lambda d: apply_cf_null_guards(d),
    ),

    # ----- PERFORMANCE: render-feature kill switches (opt-in) -------------
    Fix(
        id="no_grass",
        module="performance",
        enabled=False,
        title="Disable grass loading (CZoneData::LoadGrassData -> early return)",
        why=(
            "WHAT     : make CZoneData::LoadGrassData (0xA29800) return immediately,\n"
            "           skipping the per-zone grass mesh/instance load. Notable FPS +\n"
            "           RAM win on grassy maps.\n"
            "RET FORM : the function is __thiscall(this, a2) -> 1 stack arg -> `retn 4`,\n"
            "           and it normally returns al=1. Return code = `mov al,1; retn 4`.\n"
            "CORRECTED: the b303 catalog/patch_all.py ship `ret 8` here -- that is a\n"
            "           BUG. The real epilogue is `retn 4` (0xA2A287) and the only\n"
            "           caller (InitializeZone 0xA2A3DC) pushes ONE arg then `test al`.\n"
            "           `ret 8` over-cleans 4 bytes (corrupts InitializeZone's stack)\n"
            "           AND leaves al garbage (al==0 aborts zone init). This fix uses\n"
            "           the verified `retn 4` + al=1.\n"
            "RISK     : low-med (visual: no grass). Stack/return now provably correct."
        ),
        apply=lambda d: patch_bytes(d, "558bec6aff6846eb", "b001c204006846eb"),
    ),
    # NOTE (2026-08 audit): the next two were catalogued as "water reflection" and "terrain
    # shadow map". Neither is. Both tail-jmp into SpeedTree VEGETATION draw paths. The BYTES are
    # safe and unchanged -- only the descriptions were wrong -- but enabling them for the stated
    # visual effect would have produced a completely different one.
    Fix(
        id="no_speedtree_pass_a",
        module="performance",
        enabled=False,
        title="Skip SpeedTree vegetation pass A (sub_813E10 -> return 0)  [was: no_water_reflect]",
        why=(
            "RENAMED 2026-08 from `no_water_reflect`. IT HAS NOTHING TO DO WITH WATER.\n"
            "WHAT     : sub_813E10 returns 0 at entry.\n"
            "WHAT IT ACTUALLY SKIPS : 0x813E27 tail-jmps to RenderQueue_DrawBatches_WithOverride\n"
            "           (0x94D1C0), whose only meaningful callees are sub_DB6E20 and\n"
            "           SpeedTree_RenderBatch (0xDCE270). It is one of two SpeedTree vegetation\n"
            "           passes on the object at [[this+4Ch]+1F0h]. Nothing in the chain touches a\n"
            "           reflection target, so 'skipping the reflection render' and the old\n"
            "           'Visual: flat water' risk line were both unsupported.\n"
            "WHICH PASS: NOT ESTABLISHED. This function and its sibling `no_speedtree_pass_b` are\n"
            "           byte-identical except for the trailing jmp target, and the discriminating\n"
            "           information (the render-state override argument) was never resolved. Do\n"
            "           not upgrade either to a confident name.\n"
            "RET FORM : __thiscall(this, a2); real epilogue `pop ebp; retn 4`. Return\n"
            "           `xor eax,eax; retn 4` -- 0 is what it already returns when the target is\n"
            "           absent, so callers handle it.\n"
            "EVIDENCE : 25-byte anchor, because it shares its 6-byte prologue with the sibling\n"
            "           and they differ only in the trailing jmp displacement.\n"
            "RISK     : low, but the VISIBLE EFFECT IS LESS VEGETATION, not flat water."
        ),
        apply=lambda d: patch_bytes(d,
            "558bec8b414c85c074128b80f001000085c074088bc85de994",
            "33c0c204004c85c074128b80f001000085c074088bc85de994"),
    ),
    Fix(
        id="no_speedtree_pass_b",
        module="performance",
        enabled=False,
        title="Skip SpeedTree vegetation pass B (sub_813DF0 -> return 0)  [was: no_terrain_shadow]",
        why=(
            "RENAMED 2026-08 from `no_terrain_shadow`. IT IS NOT TERRAIN SELF-SHADOWING.\n"
            "WHAT     : sub_813DF0 returns 0 at entry.\n"
            "WHAT IT ACTUALLY SKIPS : tail-jmps to CSpeedTreeWrapper::UpdateTextureAndRender\n"
            "           (0x94BD80) -> SpeedTree_RenderBatch. Same object as `no_speedtree_pass_a`\n"
            "           ([[this+4Ch]+1F0h]); this is the other of the two vegetation passes.\n"
            "           'Visual: terrain self-shadowing gone' was unsupported.\n"
            "WHICH PASS: NOT ESTABLISHED -- see the sibling's why=.\n"
            "RET FORM : __thiscall(this, a2); real epilogue `pop ebp; retn 4`.\n"
            "EVIDENCE : 25-byte anchor to disambiguate from the sibling (identical prologue).\n"
            "RISK     : low, but the VISIBLE EFFECT IS LESS VEGETATION."
        ),
        apply=lambda d: patch_bytes(d,
            "558bec8b414c85c074128b80f001000085c074088bc85de974",
            "33c0c204004c85c074128b80f001000085c074088bc85de974"),
    ),
    Fix(
        id="no_shadow_map",
        module="performance",
        enabled=False,   # BROKEN -- DO NOT ENABLE. See why=. Retained only as a documented
                         # negative result so the idea is not re-proposed from the same bad name.
        title="[BROKEN -- DO NOT ENABLE] sub_814350 entry-return: not a renderer, and it skips a live erase",
        why=(
            "STATUS   : BROKEN. Zero upside, real downside. Kept disabled and documented rather\n"
            "           than deleted, so the next reader does not re-derive it from the name.\n"
            "WAS      : sold as 'disable dynamic shadow-map rendering, largest single FPS lever'.\n"
            "           Both halves are false.\n"
            "NOT A RENDERER : sub_814350 tail-jmps to sub_11354C0, whose real work is a call to\n"
            "           TerrainNodeMap_EraseRange (0x817DA0) -- a textbook red-black-tree\n"
            "           erase-range. There is no draw call anywhere in the chain.\n"
            "NOT IN THE RENDER LOOP : its sole caller is CPlayer::Clear_ShadowMapEffects, reached\n"
            "           only from CPlayer::Destructor and CPlayer::Process_BodyEffects (itself\n"
            "           driven by equipment/motion changes, not a per-frame tick). So the FPS\n"
            "           benefit is zero, not 'big'.\n"
            "THE HARM : the decompilation is `Render_ShadowMap(*(sub_81EC50() + 688), player+596)`\n"
            "           -- the RECEIVER is a singleton member and the ARGUMENT is the player's own\n"
            "           list, i.e. the erase removes singleton-owned map entries keyed to this\n"
            "           player. Skipping it on the destructor path leaves entries keyed off a\n"
            "           FREED CPlayer inside a singleton that outlives it. The rest of\n"
            "           Clear_ShadowMapEffects cleans only the player's OWN structures, so nothing\n"
            "           downstream removes them.\n"
            "RISK     : would be high, for no gain. Do not enable."
        ),
        apply=lambda d: patch_bytes(d, "558bec8b492c85c974065de960",
                                       "33c0c204002c85c974065de960"),
    ),
    Fix(
        id="no_char_shadow",
        module="performance",
        enabled=False,
        title="Skip per-actor shadow NIF load (CGameActor::LoadCharacterShadow) [anchor corrected]",
        why=(
            "WHAT     : skips the per-actor char_shadow.nif load inside\n"
            "           CGameActor::LoadCharacterShadow (0x8F6C50).\n"
            "CORRECTED 2026-08 -- THE ENTRY-RETURN FORM WAS BROKEN. It used to return at the\n"
            "           function's first instruction (`558bec5153568bf1578d` -> `33c0c3...`).\n"
            "           That skipped `mov dword ptr [esi+29Ch],0` at 0x8F6C5F, which is the ONLY\n"
            "           initialiser of that field on the construction path: CGameActor_Constructor\n"
            "           writes 20+ members including +288h/+28Ch/+290h/+298h but never +29Ch, and\n"
            "           its only memset covers 0x428..0x548. Dominance confirms it is not optional\n"
            "           -- removing the block containing the LoadCharacterShadow call leaves the\n"
            "           ctor's single normal return block unreachable (only the five\n"
            "           _CxxThrowException blocks survive), so on every non-throwing construction\n"
            "           the call runs. Then CGameActor_Destructor -> sub_8F30F0 does\n"
            "           `mov ecx,[esi+29Ch]; test ecx,ecx; jz; mov eax,[ecx]; mov edx,[eax+4];\n"
            "           call edx` -- a virtual call through uninitialised Gamebryo heap, guarded\n"
            "           only by `test reg,reg`, which is exactly the garbage-not-null mode this\n"
            "           catalog has documented repeatedly (0xBF59C68A, 0x0235022E, 0x558, 0x144\n"
            "           all pass such a test). Dominance on the destructor too: removing that\n"
            "           block leaves ZERO of its 7 `retn` blocks reachable, so it is on the\n"
            "           unconditional teardown path of EVERY actor.\n"
            "FIX      : anchor moved INSIDE the function to 0x8F6C8C, flipping the `jz` at\n"
            "           0x8F6C92 (74 54) to `jmp` (EB 54), rel8 unchanged. That keeps the +29Ch\n"
            "           zeroing at 0x8F6C5F AND the existing-shadow release block\n"
            "           (0x8F6C69-0x8F6C8A), and skips only Singleton1_GetInstance +\n"
            "           CNifLoader::LoadNifFile + the `mov [esi+29Ch],eax` store.\n"
            "EVIDENCE : find count = 1 / repl count = 0 in BOTH Rag2_original.exe and the\n"
            "           optimizations build. Target 0x8F6CE8 is an instruction head (`pop edi`)\n"
            "           with 4 existing xrefs, and its epilogue `pop edi; pop esi; pop ebx;\n"
            "           mov esp,ebp; pop ebp; retn` matches the ecx,ebx,esi,edi pushed by the\n"
            "           prologue before 0x8F6C92. Balanced.\n"
            "OPEN     : nobody has proven the 0x560-byte allocation is not zeroed -- the site is\n"
            "           `push 0; push 560h; call sub_DB67B0`, which bottoms out in a virtual on\n"
            "           the Gamebryo memory-manager singleton that was not resolved. The inference\n"
            "           that it is NOT zeroed is strong three ways (LoadCharacterShadow's leading\n"
            "           zero-store, sub_8F30F0's trailing zero-store, and the ctor's ~25 explicit\n"
            "           zero-stores would all be dead stores otherwise), but it is an inference.\n"
            "RISK     : low. Never runtime-tested."
        ),
        apply=lambda d: patch_bytes(d, "8b461c83f8087454", "8b461c83f808eb54"),
    ),
    Fix(
        id="no_khara_list_sort",
        module="performance",
        enabled=False,
        title="Skip the Khara/CashMall list sort (sub_408D30 comparator -> 'equal')",
        why=(
            "RENAMED 2026-08 from `no_particle_sort`. THERE ARE NO PARTICLES IN THIS CALL GRAPH.\n"
            "WHAT     : sub_408D30 returns 0, so std::sort sees every pair as equal and performs\n"
            "           no swaps.\n"
            "WHAT IT ACTUALLY SORTS : its only 3 uses are as the `std::_Introsort_unchecked`\n"
            "           predicate from CKharaMgr::FilterCharactersBy{ID,Type,Class} (callers\n"
            "           CashMall_HandleEvent / CashMall_FilterAndDisplay). It switches on a UI\n"
            "           sort-mode global (unk_15AE56C) whose 5 writers are all Khara/UI\n"
            "           (RefreshCharacterList writes 0Bh, which is > 4 and so drives the switch to\n"
            "           its DEFAULT case, a wide-string name comparison). Second reader is\n"
            "           CKharaData::Compare @0x40E922.\n"
            "WHY THE OLD NAME WAS WRONG : the `ParticleSystem::*` labels around 0x408D30 in the\n"
            "           IDB are provably misapplied -- 'ParticleSystem::GetInstance' @0x408CF0\n"
            "           merely reads the same UI sort-mode int, and the 'SetSortMode' next to it\n"
            "           is a no-arg getter. Offset/neighbourhood is not identity.\n"
            "RET FORM : __cdecl(a1, a2) comparator -> caller cleans args -> bare `ret`.\n"
            "EFFECT   : an unsorted Khara / CashMall UI list. FPS benefit is ZERO -- this is not\n"
            "           a render-path or per-frame cost. There are no 'transparency ordering\n"
            "           artifacts in dense effects'; that entire risk line described a subsystem\n"
            "           this fix does not touch.\n"
            "RISK     : low, and cosmetic-only (a list appears in arbitrary order)."
        ),
        apply=lambda d: patch_bytes(d, "558beca16ce55a01", "33c0c3a16ce55a01"),
    ),
    Fix(
        id="no_terrain_decals",
        module="performance",
        enabled=False,
        title="Disable terrain projected decals (CTerrain::RenderDecals skip path)",
        why=(
            "WHAT     : at 0x814237 `CTerrain::RenderDecals` tests `[ecx+3Ch]` and `jz` past the\n"
            "           decal gather. Flip that `jz` (74) to `jmp` (EB), same rel8 (0x29), so\n"
            "           sub_809770 is never called -- ground-projected decals off.\n"
            "NOT A FLAG (corrected 2026-08): `cmp dword ptr [ecx+3Ch],0` is a DWORD compare and\n"
            "           [ecx+3Ch] is a subobject POINTER (Get_MinX dereferences it), not a\n"
            "           'decal-enabled flag'. The branch being forced is therefore the game's own\n"
            "           'terrain data absent' path, which is if anything a better justification.\n"
            "NOR A RENDERER: sub_809770 issues no draw calls. It gets a singleton via sub_81EC50,\n"
            "           iterates a global object array (count at +984, base at +980), RTTI-filters\n"
            "           each entry against &unk_15BF9C4 and calls sub_809630 -- the receiver-object\n"
            "           gather that fills the CALLER's red-black tree. Skipping it leaves that\n"
            "           tree empty; the decals simply never get submitted downstream.\n"
            "EVIDENCE : target 0x814262 = `pop ebp` = the function's own epilogue, an instruction\n"
            "           head the `jz` ALREADY targets. Stack balance measured, not assumed: the\n"
            "           skipped span pushes 4 (`push eax`) + 16 (`sub esp,10h`) = 20 bytes and\n"
            "           sub_809770's ret form is `retn 14h` = 20. Exact. x87 balance: exactly one\n"
            "           `fld [ebp+arg_1C]` @0x81423C and one `fstp [esp+0Ch]` @0x81424B, both\n"
            "           fully inside the skipped span. Arity: epilogue `retn 24h` = 9 dwords, and\n"
            "           the call site in sub_A4AB70 pushes 4+16+16 = 36. Exact. The caller's own\n"
            "           empty-result guard is real -- 0xA4AC7E does `mov eax,[ebp+var_6C];\n"
            "           mov esi,[eax]; cmp esi,eax; jz loc_A4AF27` right after the call.\n"
            "IN THE RENDER PATH : yes -- sub_A4AB70 <- Render_GameActors. The FPS framing is\n"
            "           honest for this one, unlike its neighbours in this block.\n"
            "RISK     : low (cosmetic: no ground decals/blob marks)."
        ),
        apply=lambda d: patch_bytes(d, "74298b4528d94524", "eb298b4528d94524"),
    ),

    # ----- GPU (opt-in) ---------------------------------------------------
    # THE NEXT TWO FIXES REST ON A PREMISE THAT IS FALSE, and it is the same premise for both.
    # sub_BB0DE0 is a 7-case switch (jumptable @0xBB0ECC) and 0xBB0E6C is CASE 3 = HAL|SWVP.
    # NiDX9Renderer::Initialize is a DEGRADATION LADDER: on CreateDevice failure it does mode++
    # @0xBAE36 and retries, terminating at mode 4 with "Could not create hardware device of any
    # type - FAILING" (0x13FC4A8). The live chain 0xA43668 -> 0x110BC80 -> call 0x1139CA0
    # @0x110BDC3 passes 14 args (add esp,0x38) and arg7 -- the mode -- is the immediate `push 0`
    # at 0x110BDA1. So the client ALREADY starts at mode 0 = HAL|HWVP|PUREDEVICE. Case 3 is only
    # ever reached AFTER mode 0 has already failed. Both fixes are therefore a no-op where the
    # game works, and where they do execute they delete the last working fallback rung.
    Fix(
        id="hw_vertex_proc",
        module="gpu",
        enabled=False,
        title="[NO-OP OR HARMFUL] Force HW vertex processing on the SWVP fallback rung",
        why=(
            "WHAT     : at 0xBB0E6C the device-behavior flags are built with `or ecx,20h`\n"
            "           (D3DCREATE_SOFTWARE_VERTEXPROCESSING). Flips the immediate to 40h\n"
            "           (HARDWARE). Bytes and encoding are correct.\n"
            "THE PREMISE IS FALSE (2026-08 audit). This is not 'the' device-behaviour site; it is\n"
            "           case 3 of a 7-case switch, and case 3 is the SOFTWARE-VP rung of a\n"
            "           fallback ladder. The client hardcodes `push 0` at 0x110BDA1, i.e. it\n"
            "           starts at mode 0 = HAL|HWVP|PUREDEVICE. So:\n"
            "             - where the game already works, case 3 never executes -> NO-OP, no FPS;\n"
            "             - where it does execute, mode 0 has already failed, and this patch makes\n"
            "               the last rung demand the same hardware VP that just failed, so the\n"
            "               ladder runs out and the renderer hard-fails at startup.\n"
            "RISK     : the old line read 'device creation could fail; any modern GPU is fine',\n"
            "           which has the consequence exactly backwards. The correct statement is:\n"
            "           this can only ever convert a graceful software-VP degradation into a\n"
            "           startup failure. On a machine that needs the ladder, that is fatal; on a\n"
            "           machine that does not, it does nothing at all. Do not enable for FPS."
        ),
        apply=lambda d: patch_bytes(d, "83c9208b5510890aeb4e", "83c9408b5510890aeb4e"),
    ),
    Fix(
        id="hw_skinning",
        module="gpu",
        enabled=False,
        title="Suppress the fixed-function >4-bones rejection (sub_BDCCD3 jbe -> jmp)",
        why=(
            "WHAT     : at 0xBDCCD3 `jbe`->`jmp`. Bytes, encoding, stack and reconvergence are\n"
            "           all sound: target 0xBDCD10 = `jmp 0xBDCC84` is the loop-continue and is\n"
            "           the jbe's OWN target, and the orphaned span is a self-contained cdecl\n"
            "           call with `add esp,0x18`.\n"
            "THE BRANCH SENSE WAS DOCUMENTED BACKWARDS (2026-08 audit). `jbe` on <=4 bones is the\n"
            "           ACCEPT/continue path. The FALL-THROUGH is the ERROR path: it logs\n"
            "           'Submesh %d requires more than %d bones on mesh...' (0x13FED20) and\n"
            "           returns FALSE. There is no software-skinning branch anywhere here, so the\n"
            "           old 'skips the GPU-skin path ... before the software/error branch' was\n"
            "           half wrong and the fix does NOT 'enable hardware skinning'.\n"
            "WRONG SUBSYSTEM TOO : the containing function is\n"
            "           NiD3DDefaultShader::CreateSemanticAdapterTable -- the DX9 FIXED-FUNCTION\n"
            "           shader (siblings: 'DX9 Fixed Function Pipeline', 'Instancing not supported\n"
            "           on fixed function pipepline'), not a GPU-skin path.\n"
            "WHAT THE BYTES REALLY DO : suppress the engine's own capability rejection, so the FF\n"
            "           path builds a vertex declaration for a submesh it cannot represent -- D3D9\n"
            "           fixed function blends at most 4 matrices per vertex.\n"
            "RISK     : med. Expect SILENTLY DROPPED BONE INFLUENCES (deformation artifacts on\n"
            "           >4-bone submeshes), not 'hardware skinning enabled'. The old note relied\n"
            "           on a b303-catalog 'tested and working' claim that was never reproduced\n"
            "           here. Opt-in and reversible."
        ),
        apply=lambda d: patch_bytes(d, "763b8b4d08e8a35d", "eb3b8b4d08e8a35d"),
    ),

    # ----- VISUAL (opt-in) ------------------------------------------------
    Fix(
        id="max_view_distance",
        module="visual",
        enabled=False,
        title="Remove fog-distance reduction (force max view distance)",
        why=(
            "WHAT     : at 0x814D53 a `ja` guards the fog/view-distance switch and\n"
            "           normally selects a reduced-distance case. `ja`->`jmp` forces\n"
            "           the default (no-reduction / max-distance) case always.\n"
            "EVIDENCE : target 0x814D7C = `fldz`, which is BOTH the ja's own default AND\n"
            "           jumptable[4]. Semantics confirmed: the byte at [settings+0x250] selects\n"
            "           t = 0.8/0.6/0.4/0.2/0.0 and the result is max(B-(B-A)*t, A), so t=0 really\n"
            "           is the maximum. The skipped span has no push/call; every case pushes\n"
            "           exactly one x87 value and so does `fldz`. Balanced.\n"
            "EQUIVALENCE (2026-08): because jumptable[4] ALREADY points at 0x814D7C, this patch is\n"
            "           exactly equivalent to setting the in-game view-distance option to its top\n"
            "           value. It cannot produce a state the game does not already support.\n"
            "RISK     : LOW, not med -- the previous 'med' was rated on 'it forces a switch\n"
            "           default', which the equivalence above retires.\n"
            "           'Visually removes distance fog' is UNVERIFIED and probably wrong: what is\n"
            "           proven is a min/max lerp written to 0x159F830 / 0x159F82C behind the\n"
            "           unnamed getters 0x8142C0 / 0x8142D0. Enable and eyeball."
        ),
        apply=lambda d: patch_bytes(d, "7727ff2485384e81", "eb27ff2485384e81"),
    ),
    Fix(
        id="max_lod_far",
        module="visual",
        enabled=True,   # FIELD-PROVEN: present in the b303-2026-07-11 build the operator has been
                        # playing on, so this is shipped configuration, not a suggestion. The
                        # catalog default must reproduce the real client -- a plain
                        # `python patch_client.py` used to omit it and silently build a
                        # DIFFERENT exe from the one in use.
        title="Keep high-quality shader at far LOD (don't downgrade)",
        why=(
            "WHAT     : at 0x4611CE a `jnp` after an fcom branches to the low-quality\n"
            "           far-shader assignment. NOP it (`90 90`) to fall through.\n"
            "EVIDENCE : the 8-byte anchor window disassembles from 0x4611CE as\n"
            "           `jnp; fcomp st(1); fnstsw; test ah,41h`. Note the jnp is DRIVEN by the\n"
            "           PRECEDING non-popping `fcom st(1); fnstsw ax; test ah,5` at\n"
            "           0x4611C7-0x4611CB, not by the fcomp shown after it.\n"
            "           FP balance verified on all exits (X>50 routes fcomp-pop -> jne 0x4611F4 ->\n"
            "           fstp st(0)-pop = 2 pops for 2 pushes); NaN behaves identically patched and\n"
            "           unpatched (ah&5 = 5 sets PF, so the jnp was not taken either way).\n"
            "           Constants read from the binary: 0x1388068 = double 50.0, 0x1388060 = float\n"
            "           10.0, 0x1388040 = 'DefaultRag2_ObjectShader_Far'.\n"
            "PRECISION: 'keep the high-quality shader at distance' is loose. The patch selects no\n"
            "           shader; it leaves edi = [esi+0xC8], the object's own/base shader name\n"
            "           loaded at 0x4611A5. If that field is NULL the existing guard\n"
            "           `test edi,edi; je 0x461226` skips the assignment entirely, so for such\n"
            "           objects the real outcome is NO SHADER OVERRIDE, not 'high quality'.\n"
            "RISK     : low-med (a bit more GPU cost far away). Pairs with max_lod_mid."
        ),
        apply=lambda d: patch_bytes(d, "7b1dd8d9dfe0f6c4", "9090d8d9dfe0f6c4"),
    ),
    Fix(
        id="max_lod_mid",
        module="visual",
        enabled=True,   # FIELD-PROVEN: present in the b303-2026-07-11 build the operator has been
                        # playing on, so this is shipped configuration, not a suggestion. The
                        # catalog default must reproduce the real client -- a plain
                        # `python patch_client.py` used to omit it and silently build a
                        # DIFFERENT exe from the one in use.
        title="Skip middle-LOD shader downgrade (keep high quality at mid range)",
        why=(
            "WHAT     : at 0x4611E4 a `jnz` guards the mid-quality shader assignment\n"
            "           (`mov edi,1388020h`). `jnz`->`jmp` always skips that assignment,\n"
            "           keeping the higher-quality shader at mid range.\n"
            "EVIDENCE : disasm 0x4611E4 = `jnz +10h` over `mov edi,1388020h`.\n"
            "RISK     : low-med (slightly more GPU cost at mid range)."
        ),
        apply=lambda d: patch_bytes(d, "7510bf20803801eb", "eb10bf20803801eb"),
    ),

    # ----- GAMEPLAY (opt-in; changes behaviour, not just visuals) ---------
    # THE NEXT TWO ARE INERT. Kept because the immediates are real and someone will find them
    # again, but they change a UI LABEL, not a cap. See either why= for the measurement.
    Fix(
        id="shop_slot_label_base",
        module="cosmetic",
        enabled=False,
        title="[INERT] Raise the base operand of the private-shop '%d/%d' slot label 10 -> 50",
        why=(
            "RENAMED 2026-08 from `target_limit_base`. IT IS NOT A TARGET LIMIT AND IT LIMITS\n"
            "NOTHING.\n"
            "WHAT     : at 0x4DA4DB `mov esi,0Ah` -> `mov esi,32h`. The byte edit is safe.\n"
            "WHAT IT ACTUALLY FEEDS : sub_4DA4D0 has exactly TWO code xrefs and no data refs\n"
            "           (exhaustive for a non-vtable function), 0x6AAB1B and 0x6ACB3D\n"
            "           (PrivateShop_RefreshSellerItemListUI). Both format L\"%d/%d\" into a\n"
            "           private-shop UI label, the numerator being the size of the shop's item\n"
            "           vector. NOTHING BRANCHES ON THE VALUE.\n"
            "WHERE THE REAL RULE LIVES : the enforcing copy is INLINED in\n"
            "           CTradeData::ValidateTradeItemAdd (0x4A2050, gate at 0x4A208B, error string\n"
            "           id 8809) and duplicated again in GetMaxTradeSlots (0x4A1C10). Neither is\n"
            "           touched by this fix or its sibling.\n"
            "EFFECT   : the label claims 50 while the client still blocks at 10 and the server\n"
            "           enforces its own cap regardless. Patching one of the two byte-identical\n"
            "           clones also makes labels disagree with each other.\n"
            "RISK     : NOT a 'gameplay change' -- that RISK line was materially false. Cosmetic\n"
            "           only, and it desynchronises the label from the real cap."
        ),
        apply=lambda d: patch_bytes(d, "be0a000000e85bed", "be32000000e85bed"),
    ),
    Fix(
        id="shop_slot_label_high",
        module="cosmetic",
        enabled=False,
        title="[INERT] Raise the level-30+ operand of the same '%d/%d' label 20 -> 100",
        why=(
            "RENAMED 2026-08 from `target_limit_high`. Same function, same single consumer, same\n"
            "verdict as `shop_slot_label_base` -- read that why=.\n"
            "WHAT     : at 0x4DA4F4 `mov esi,14h` -> `mov esi,64h`.\n"
            "EVIDENCE : reached via `cmp [player+1D8h],1Eh; jl` just above 0x4DA4F4.\n"
            "RISK     : cosmetic only. 'Enable together with shop_slot_label_base' does not make\n"
            "           either of them a gameplay change."
        ),
        apply=lambda d: patch_bytes(d, "be140000008bc65ec3cccccc8b4154",
                                       "be640000008bc65ec3cccccc8b4154"),
    ),
    Fix(
        id="enchant_no_camdist",
        module="gameplay",
        enabled=False,
        title="Remove the enchant-UI distance gate (UICharEnhanceExchangeWnd) [sense corrected]",
        why=(
            "WHAT     : removes the distance gate that closes the enchant window.\n"
            "CORRECTED 2026-08 -- THE OLD BYTES SHIPPED THE EXACT OPPOSITE EFFECT. The fix used to\n"
            "           NOP the branch (`75 0B` -> `90 90`), on the belief that the `jnz` WAS the\n"
            "           gate. It is not. At 0x44B65F `test ah,41h; jnz` is taken when\n"
            "           dist <= 5.0f -- the jump is the DO-NOTHING path. The window-close sits on\n"
            "           the FALL-THROUGH (0x44B661 `mov edx,[esi]; mov eax,[edx+0Ch]; push 0;\n"
            "           mov ecx,esi; call eax`), where vtable slot +0x0C resolves to\n"
            "           UITradeWnd::CloseWindow(this,0) and, with a2==0, dispatches vtable+0x100 =\n"
            "           UICharEnhanceExchangeWnd::OnClose (0x44E640: ResetEnchant +\n"
            "           ClearSourceSlot). The only caller is UICharEnhanceExchangeWnd::OnFrame,\n"
            "           first instruction, EVERY FRAME. So NOPping the branch closed and cleared\n"
            "           the enchant window continuously and made it unusable -- including at\n"
            "           distance 0, where the stock code would have jumped. Hex-Rays agrees:\n"
            "           `if ( v9 > thr ) (*(this+12))(this, 0);`.\n"
            "FIX      : `75 0B` -> `EB 0B` (jmp short 0x44B66C), which takes the do-nothing path\n"
            "           unconditionally. 0x44B66C is an address the function already branches to\n"
            "           from 0x44B5C0 and 0x44B5F7 and is a real instruction head (`pop esi`).\n"
            "           No pending x87 -- the `fcomp` already popped.\n"
            "NAMING   : the IDB calls this UIFXEnchantWnd::CheckCameraDistance, but the distance\n"
            "           is measured from GetPlayerPointer()->vtable[+0x1C] to a position cached at\n"
            "           this+0x148/0x14C/0x150. Treat it as a player-distance-from-anchor gate;\n"
            "           the 'camera' label is inherited from an unverified IDB name.\n"
            "EVIDENCE : the anchor runs to 39 B (through the function tail + alignment) because\n"
            "           `75 0B` alone is not unique. Self-verifying: a mismatch refuses.\n"
            "RISK     : low (niche UI convenience). Never runtime-tested in the corrected form."
        ),
        apply=lambda d: patch_bytes(d,
            "750b8b168b420c6a008bceffd05e8be55dc3cccccccccccccccccccccccccccccc558bec83ec68",
            "eb0b8b168b420c6a008bceffd05e8be55dc3cccccccccccccccccccccccccccccc558bec83ec68"),
    ),

    # ----- STARTUP / AUDIO (opt-in) ---------------------------------------
    Fix(
        id="skip_opening_movie",
        module="startup",
        enabled=False,
        title="Skip the opening-movie popup on char-select entry",
        why=(
            "WHAT     : at 0x639D27 a 5-byte `call CheckOpeningMovieConfig` auto-opens the\n"
            "           opening-movie UI window (GetOrCreateWindow id 0x73) on first entry.\n"
            "           NOP the call (E8 rel32 -> 90 90 90 90 90).\n"
            "EVIDENCE : `call ...; mov eax,1; pop esi; ret` -- the callee's return is immediately\n"
            "           overwritten by `mov eax,1`, so the value is provably dead. The call site\n"
            "           pushes NO stack args (ecx-only), the enclosing block has one `push esi` /\n"
            "           one `pop esi` and ends in a plain `retn`, and 0x639C60 has exactly one\n"
            "           caller and zero data refs. Control-flow- and stack-safe.\n"
            "CORRECTED 2026-08 -- two prose errors, bytes unaffected:\n"
            "         * NOT 'CStageCharSelect OnEnter'. The block containing 0x639D27 starts at\n"
            "           0x639CF0, is not a defined function in the IDB, and its vtable slot\n"
            "           (.rdata 0x1399968) sits among CStageOpening_Enter /\n"
            "           CStageOpening_Destructor / CStageOpening_TransitionToCharSelect -- this is\n"
            "           CStageOpening's vtable.\n"
            "         * the callee does MORE than open window 0x73: it also calls\n"
            "           Cutscene_PlayMovieById (0x7D3040) and persists a 'seen' flag into CConfig\n"
            "           (0x639CD1-0x639CE5).\n"
            "CONSEQUENCE OF THAT SECOND POINT : with the call NOPed the seen-flag is never\n"
            "           written, so REMOVING this patch later brings the movie back.\n"
            "RISK     : low (skips a one-time startup popup)."
        ),
        apply=lambda d: patch_bytes(d, "e834ffffffb80100", "9090909090b80100"),
    ),
    Fix(
        id="fmod_channels_512",
        module="audio",
        enabled=False,
        title="Raise FMOD max channels 256 -> 512 (fewer cut sounds)",
        why=(
            "WHAT     : CSoundMgr::Initialize calls FMOD::EventSystem::init with\n"
            "           maxchannels=256 (`push 100h` @0x629989). Change the immediate to\n"
            "           200h (512) so busy scenes (raids, crowded towns) stop stealing/\n"
            "           cutting voices.\n"
            "EVIDENCE : disasm 0x629989 = `push 100h; push ecx; call FMOD..EventSystem::\n"
            "           init(int maxchannels,...)`; decompile confirms the literal 256 is\n"
            "           maxchannels. Only the imm32 changes; length identical.\n"
            "RISK     : low. FMOD virtual voices are cheap; real-voice mix stays bounded.\n"
            "           A larger valid channel count does not fail init."
        ),
        apply=lambda d: patch_bytes(d, "680001000051e8bc", "680002000051e8bc"),
    ),

    # ----- MEMORY / ADDRESS SPACE (opt-in; HIGH risk, read carefully) -----
    # These flip UI/localized textures from D3DPOOL_MANAGED (1) to D3DPOOL_DEFAULT (0),
    # which removes the system-RAM SHADOW copy D3D keeps for MANAGED textures -> reclaims
    # that texture's bytes from the scarce 32-bit ADDRESS SPACE (the client's OOM root
    # cause that `laa` also fights). Verified via IDA: each site pushes a literal `6a 01`
    # (Pool=MANAGED) into a D3DXCreateTextureFromFile*Ex call; we flip the immediate to
    # `6a 00` (DEFAULT). The 16/18-byte window is NOT unique (a sibling call has the same
    # bytes), so these use patch_at_va (VA-anchored) instead of a pattern search.
    #
    # ***HIGH RISK*** DEFAULT-pool textures are LOST on a D3D device reset and are NOT
    # auto-restored (MANAGED is).
    #
    # THE FAILURE MODE IS NOT A DANGLING POINTER (corrected 2026-08). This header used to say
    # "the cached pointer can dangle -> missing UI or a crash on the next draw". That cannot
    # happen, because NOTHING RELEASES THESE TEXTURES: a BFS to depth 8 from Recreate /
    # LostDeviceRestore / both registered callbacks finds zero release sites (with 3 controls
    # that DID hit), and none of the six loaders is ever taken as a function pointer. What
    # actually happens is worse and quieter: IDirect3DDevice9::Reset fails with
    # D3DERR_INVALIDCALL while ANY DEFAULT-pool resource is outstanding, so at 0xBAF900 Recreate
    # takes the HRESULT<0 branch (0xBAF90B), returns false, and [renderer+0x56C] is never
    # cleared -- it is cleared only at 0xBAF933, on success. LostDeviceRestore then retries
    # forever. The result is a PERMANENTLY frozen/black device, not a one-off draw crash.
    #
    # "Lower risk in windowed / borderless (no device reset)" OVERSTATES the safety: windowed
    # D3D9 devices are still lost on TDR, driver update and display-topology change, and this
    # client does offer exclusive fullscreen (the windowed flag is byte [displaySettings+0x0C],
    # read at 0x110BD1D in Renderer_CreateDeviceFromDisplaySettings).
    #
    # NONE OF THIS HAS BEEN RUN. The D3DERR_INVALIDCALL consequence is documented D3D9 behaviour
    # plus the code at 0xBAF900 / 0xBAF90B / 0xBAF933. DXVK is more permissive and may not
    # exhibit it at all.
    #
    # Benefit is MODEST (UI/localized textures only, ~tens of MB, content-dependent).
    # PRIORITY NOTE: of the five inline fixes below, `tex_pool_loc_inmem` is the only one with
    # real memory value (135 call sites: icons, minimap, portraits, equip icons). The two "ui"
    # ones buy a few cash-shop images and `tex_pool_loc_global` buys one small tga.
    # NOTE: the BIG lever (the world/model texture creator sub_BEE0B0) is NOT inline-
    # patchable -- it calls the non-Ex D3DXCreateTextureFromFileInMemory which has no Pool
    # argument (MANAGED is hard-wired in d3dx9_42.dll); removing that shadow needs a D3D
    # call redirect / IAT hook and belongs in the mod DLL, not this inline patcher.
    # See docs/memory-analysis.md. Enable these only to experiment, ideally windowed.
    Fix(
        id="tex_pool_ui_inmem",
        module="memory",
        enabled=False,
        title="Cash-mall textures MANAGED->DEFAULT (in-memory path) -- drop RAM shadow [risky]",
        why=(
            "WHAT     : sub_1084400 in-memory (VDK) path -> D3DXCreateTextureFromFileInMemoryEx.\n"
            "           Pool `push 1` (MANAGED) at 0x108472C -> `push 0` (DEFAULT), removing the\n"
            "           system-RAM shadow.\n"
            "SCOPE CORRECTED 2026-08: this is NOT 'the UI/localized texture loader'. sub_1084400\n"
            "           has exactly ONE caller and the only chain into it is\n"
            "           UICashMallWnd::OnCreate -> CashMall_LoadQuestText (0x66A180) ->\n"
            "           sub_107CAE0 -> sub_1084400. The MEM benefit is A FEW CASH-SHOP IMAGES.\n"
            "MEM      : reclaims each such texture's size from the 32-bit address space.\n"
            "RISK     : HIGH -- see the section header for the real failure mode (Reset fails\n"
            "           permanently; it is NOT a dangling pointer). Enable windowed only.\n"
            "EVIDENCE : Pool confirmed as arg 9 of 15 by counting the complete push block back\n"
            "           from the call and cross-checking two landmark args (Format, ppTexture).\n"
            "           Byte window not unique -> VA-anchored patch."
        ),
        apply=lambda d: patch_at_va(d, 0x108471F,
            "8d8d40fdffff516a006a016a016a016a", "8d8d40fdffff516a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_ui_disk",
        module="memory",
        enabled=False,
        title="Cash-mall textures MANAGED->DEFAULT (disk fallback path) [risky]",
        why=(
            "WHAT     : same loader (sub_1084400), disk fallback -> D3DXCreateTextureFromFileExW;\n"
            "           Pool `push 1`->`push 0` at 0x1084794.\n"
            "PAIRING IS LOAD-BEARING : 0x1084714 and 0x1084775 write the SAME ppTexture var_2A0,\n"
            "           so the two creates are the VDK path and its disk fallback for one texture.\n"
            "           Enable with tex_pool_ui_inmem or not at all -- and this half genuinely\n"
            "           matters, because MALLADV ships loose.\n"
            "SCOPE    : cash-mall images only; see tex_pool_ui_inmem.\n"
            "RISK     : HIGH -- device-lost, see section header. Window identical to the\n"
            "           in-memory site, so VA-anchored."
        ),
        apply=lambda d: patch_at_va(d, 0x1084787,
            "8d8d40fdffff516a006a016a016a016a", "8d8d40fdffff516a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_loc_inmem",
        module="memory",
        enabled=False,
        title="Localized textures MANAGED->DEFAULT (CLocalization, BOTH paths) -- best of this group",
        why=(
            "WHAT     : CLocalization's localized-texture loader. Flips Pool `push 1`->`push 0`\n"
            "           on BOTH of its creation paths.\n"
            "WHY THIS ONE IS THE PRIORITY : 135 call sites feed this loader -- icons, minimap,\n"
            "           portraits, equip icons. It is the only member of this five-fix group with\n"
            "           real memory value, and it previously carried the SHORTEST why= in the\n"
            "           group while two negligible cash-mall fixes carried the longest. That\n"
            "           priority inversion is corrected here.\n"
            "COMPLETED 2026-08 : the fix used to patch only the in-memory create at 0x1083F67 and\n"
            "           leave the SAME function's disk fallback (D3DXCreateTextureFromFileExA\n"
            "           @0x1083FEF, Pool `push 1` @0x1083FCF) as MANAGED -- writing the same\n"
            "           ppTexture var_1A0. Its sibling tex_pool_ui_* paired both of its paths;\n"
            "           this one did not, and did not say so. Both are now flipped together, so\n"
            "           the two load paths agree.\n"
            "EVIDENCE : in-memory window at 0x1083F5A, Pool = arg 9 of 15 at 0x1083F67; disk\n"
            "           window at 0x1083FC2, Pool at 0x1083FCF. Both verified byte-exact.\n"
            "RISK     : HIGH -- device-lost, see section header. Localized textures also reload\n"
            "           on language change. VA-anchored (non-unique windows)."
        ),
        apply=lambda d: patch_all_at_va(d, [
            (0x1083F5A, "8d8d40feffff516a006a016a016a016a",   # in-memory (VDK) create
                        "8d8d40feffff516a006a016a016a006a"),
            (0x1083FC2, "8d8d40feffff516a006a016a016a016a",   # disk fallback, same ppTexture
                        "8d8d40feffff516a006a016a016a006a"),
        ]),
    ),
    Fix(
        id="tex_pool_loc_global",
        module="memory",
        enabled=False,
        title="ui/Texture/box.tga MANAGED->DEFAULT (global unk_15C9134) [risky; site corrected]",
        why=(
            "WHAT     : sub_10862D0 loads one global A8R8G8B8 texture into a single cached\n"
            "           pointer. Flips Pool `push 1`->`push 0`.\n"
            "CORRECTED 2026-08 -- IT WAS PATCHING THE DEAD HALF. sub_10862D0 has TWO byte-identical\n"
            "           18-byte windows, and the fix edited only the DISK FALLBACK at 0x108635C.\n"
            "           The guard at 0x1086353 (`cmp unk_15C9134,0 / jnz 0x1086396`) skips that\n"
            "           create whenever the VDK in-memory create at 0x108633E already succeeded --\n"
            "           and the asset ui/Texture/box.tga ships INSIDE Data/UI.VDK (present as\n"
            "           Data/UI_UNPACKED/UI/Texture/box.tga; the loose root ui/ and UI/ folders\n"
            "           contain no .tga at all). So on a normal install the patched instruction\n"
            "           NEVER EXECUTED. The live in-memory site at 0x1086304 is now patched too,\n"
            "           and the 0x108635C edit is kept for loose-file installs.\n"
            "PROSE FIXES : the function is sub_10862D0 (the old note cited 'sub_1086340', which is\n"
            "           not an instruction boundary -- it is inside the 5-byte call at 0x108633E);\n"
            "           and the asset is NOT localized, it is one global tga of a few KB.\n"
            "HONESTY  : even fully fixed this reclaims one small texture. Its cost/benefit is poor\n"
            "           next to tex_pool_loc_inmem.\n"
            "RISK     : HIGH -- device-lost, see section header. VA-anchored."
        ),
        apply=lambda d: patch_all_at_va(d, [
            (0x1086304, "6834915c016a006a006a006a016a016a016a",   # LIVE in-memory (VDK) create
                        "6834915c016a006a006a006a016a016a006a"),
            (0x108635C, "6834915c016a006a006a006a016a016a016a",   # disk fallback (loose installs)
                        "6834915c016a006a006a006a016a016a006a"),
        ]),
    ),
    Fix(
        id="tex_pool_shader_effect",
        module="memory",
        enabled=False,
        title="Effect/material textures MANAGED->DEFAULT (NiD3DXEffectShader::LoadTexture2D)",
        why=(
            "WHAT     : NiD3DXEffectShader::LoadTexture2D (0xBA0DB0) loads the textures\n"
            "           referenced by .fx effects/materials (diffuse/normal/specular/\n"
            "           lookup/ramp) via D3DXCreateTextureFromFileExA with Pool `push 1`\n"
            "           (MANAGED) at 0xBA0DFE. Flip to `push 0` (DEFAULT), dropping the\n"
            "           system-RAM shadow.\n"
            "MEM      : reclaims each effect texture's size from the address space. (An earlier\n"
            "           note called this 'the largest still-MANAGED texture site'; that was never\n"
            "           measured and is probably wrong -- CLocalization alone has 135 call sites.\n"
            "           Claim withdrawn rather than replaced with another unmeasured one.)\n"
            "EVIDENCE : the most cleanly evidenced of this group. 14-argument push block;\n"
            "           Pool=`6a01`@0xBA0DFE, Usage=`6a00`@0xBA0E02 (=0, so DEFAULT is legal with\n"
            "           no render-target implication). Function start is 0xBA0DB0 -- the previously\n"
            "           cited 0xBA0DD0 is not an instruction boundary (it is inside the 7-byte\n"
            "           `mov [ebp+var_10],0` at 0xBA0DCC).\n"
            "           The 22-byte window IS unique (count 1); VA-anchoring is harmless but not\n"
            "           required here, contrary to the old note.\n"
            "RISK     : HIGH (device-reset -- see the section header for the real, permanent\n"
            "           failure mode). 'Effect textures reload when the effect does' was offered\n"
            "           as mitigation and IS NOT A RECOVERY PATH: the reload gate is\n"
            "           `cmp [node+0x38],0 / jnz` at 0xB9D2C4 and nothing outside the constructor\n"
            "           ever zeroes +0x38. Safest windowed / under DXVK."
        ),
        apply=lambda d: patch_at_va(d, 0xBA0DF0,
            "8d55f8526a006a006a006aff6aff6a016a006a006aff",
            "8d55f8526a006a006a006aff6aff6a006a006a006aff"),
    ),
    Fix(
        id="texpool_world_default",
        module="memory",
        enabled=False,
        title="[.patch cave] World/model textures MANAGED->DEFAULT -- the big address-space win",
        why=(
            "WHAT     : the bulk of world/model/terrain textures are created via the\n"
            "           non-Ex D3DXCreateTextureFromFileInMemory (in sub_BEE0B0), which\n"
            "           has NO D3DPOOL argument -> D3D forces D3DPOOL_MANAGED and keeps a\n"
            "           full system-RAM SHADOW of every texture. In a 32-bit process that\n"
            "           shadow is the address-space cost behind the 2 GB / 4 GB OOM.\n"
            "HOW      : this is the ONE fix that needs a code cave (adds a `.patch` PE\n"
            "           section). It hooks the non-Ex thunk (0xDF2E2E) to a cave that\n"
            "           re-issues the call through the *Ex overload with Pool=DEFAULT(0)\n"
            "           (no RAM shadow), mirroring every other non-Ex default.\n"
            "SCOPE CORRECTED 2026-08 -- read this before believing the size of the win. The old\n"
            "           text said 'the bulk of world/model/terrain textures' and 'roughly HALVES\n"
            "           the texture footprint'. Neither was measured, and the scope is narrower:\n"
            "           the hub NiDX9SourceTextureData::LoadFromFile (0xBEC5E0) has THREE creation\n"
            "           branches, and the hooked D3DX branch is reached only when sub_BEE950(a1)==0\n"
            "           AND the flag this[65] is set. Its siblings CreateSurfFromRendererData\n"
            "           (0xBED610) and CreateSurf (0xBED0E0) both create MANAGED via device\n"
            "           CreateTexture and are UNTOUCHED, as are the cube and volume D3DX creators\n"
            "           in the same hub. Honest scope: '2D textures loaded through the non-Ex D3DX\n"
            "           path, plus guild emblems. Share of the working set NOT MEASURED.'\n"
            "SECOND CALLER : the non-Ex thunk has 2 rel32 callers and 0 direct IAT calls, but the\n"
            "           second is GuildEmblemCache_AddOrUpdate -> D3D9_CreateTextureFromMemoryBuffer\n"
            "           (0x1086E90). Guild emblems therefore also flip to DEFAULT -- a user-visible\n"
            "           device-reset side effect outside the 'world/model' framing.\n"
            "EVIDENCE : the cave's 15-argument order was re-derived arg-by-arg and is correct\n"
            "           (Pool is arg 9; every other value matches the documented non-Ex defaults),\n"
            "           the frame offsets are corroborated by the real call site at 0x1086EBF, the\n"
            "           `ret 10h` matches the 4 args the impersonated __stdcall import would have\n"
            "           cleaned, the inner *Ex call is itself stdcall (cleans its own 60 B), the\n"
            "           HRESULT passes through EAX untouched, and there is no x87 on the path.\n"
            "           IAT 0x1355D38 resolves to d3dx9_42!D3DXCreateTextureFromFileInMemory and\n"
            "           0x1355DA0 to ...InMemoryEx.\n"
            "RISK     : HIGH -- D3DPOOL_DEFAULT textures are LOST on a device reset\n"
            "           (exclusive-fullscreen alt-tab / resolution change) and Gamebryo\n"
            "           may not recreate them -> textures could vanish or crash on reset.\n"
            "           Much safer in WINDOWED / borderless (no device reset). ***This is\n"
            "           the only fix that has NOT been runtime-tested by the author -- the\n"
            "           cave bytes are verified to disassemble correctly and the patch\n"
            "           applies cleanly, but you MUST test it live (load the game,\n"
            "           change zones, alt-tab, change resolution) before relying on it.***\n"
            "           Fully reversible: the original exe is untouched."
        ),
        apply=lambda d: apply_texpool_default(d),
    ),
    Fix(
        id="geopool_static_default",
        module="memory",
        enabled=False,
        title="Static NiMesh geometry MANAGED->DEFAULT -- the big geometry address-space win",
        why=(
            "WHAT     : character/prop/weapon/armor STATIC mesh vertex+index buffers are\n"
            "           created D3DPOOL_MANAGED, so D3D keeps a full system-RAM shadow of\n"
            "           each in the scarce 32-bit address space. This forces them to\n"
            "           D3DPOOL_DEFAULT (VRAM-only, no shadow).\n"
            "HOW      : the single NiDX9 buffer creator sub_BE3E90 pushes Pool from a\n"
            "           computed local (var_8 = sub_BE3960 = `(this[0x34] & 8)==0` -> 1\n"
            "           MANAGED for static, 0 DEFAULT for dynamic). Two 3-byte inline\n"
            "           edits zero the pushed Pool register: VB @0xBE3F3C `mov edx,[ebp-8]`\n"
            "           -> `xor edx,edx; nop`; IB @0xBE404F `mov ecx,[ebp-8]` ->\n"
            "           `xor ecx,ecx; nop`. Dynamic streams already push 0, so ONLY the\n"
            "           static MANAGED case changes. (VA-anchored; the `mov` is not unique.)\n"
            "MEM      : the largest remaining address-space win after `laa` and the world\n"
            "           texture cave -- order ~100-300 MB of VB/IB RAM shadow in populated\n"
            "           zones. Terrain geometry is already DEFAULT+DYNAMIC (untouched).\n"
            "EVIDENCE : decompile sub_BE3E90 shows Pool=v12=sub_BE3960 for both the vtbl+104\n"
            "           (CreateVertexBuffer) and vtbl+108 (CreateIndexBuffer) calls; disasm\n"
            "           confirms the `8B 55 F8`/`8B 4D F8` Pool loads and sub_BE3960's\n"
            "           `and 8; jz -> mov eax,1` static->MANAGED logic.\n"
            "RISK     : MEDIUM, but LOWER than the texture flips: DEFAULT buffers are lost\n"
            "           on device reset, BUT dynamic streams already ride this exact DEFAULT\n"
            "           path through sub_BE3E90 and are recreated (via sub_BE3BC0) on the\n"
            "           this[0x3C]==0 rebuild, so static buffers inherit that machinery.\n"
            "           SHARPENED 2026-08 -- 'inherit that machinery' holds in the main case\n"
            "           (NiDX9Renderer::Recreate -> sub_BE4300 = free + DX9Allocate + re-upload\n"
            "           from the engine's own system copy this[64], with the static branch\n"
            "           explicitly `(this[52]&8)==0`), but NOT unconditionally: sub_BE4410 can\n"
            "           FREE that system copy (`sub_DB6E20(this[64]); this[64]=0`) when flags bit1\n"
            "           is clear and bit2 set. A stream whose shadow was dropped has nothing to\n"
            "           restore from after a device reset -- MANAGED used to cover that case.\n"
            "           Still runtime-untested -- validate alt-tab / resolution change /\n"
            "           crowded zones. Near-free under DXVK (no device reset)."
        ),
        apply=lambda d: apply_geo_pool_default(d),
    ),
    Fix(
        id="cache_retention_30s",
        module="memory",
        enabled=True,   # FIELD-PROVEN: measured present in the b303-2026-07-11 Rag2.exe the operator
                        # plays on (byte-level diff vs the pristine exe). Shipped configuration,
                        # not a suggestion -- the default build must reproduce the real client.
        title="Shrink resource-cache cooldown 180s -> 30s (bounds session-long RAM high-water)",
        why=(
            "WHAT     : the shared DataCache (models/NIF, skinned meshes, terrain textures,\n"
            "           equipment effects) does NOT free a resource when its last owner\n"
            "           releases it -- DataCache_ReleaseData moves it to a 'cooldown' map\n"
            "           with expiry = now + 180000 ms, and it is only freed later (when\n"
            "           expired AND refcount==1). So every asset that goes out of range or\n"
            "           is swapped stays resident for 3 MINUTES after last use.\n"
            "WHY      : with no size cap on the cooldown map, this is a large, continuously\n"
            "           refilled RAM high-water -- a session-long driver of the 32-bit OOM\n"
            "           (gear churn in crowded towns, rapid zone traversal). Shrinking the\n"
            "           window ~6x bounds the cooldown footprint.\n"
            "HOW      : at 0x5181C8 `add eax, 2BF20h` (180000) -> `add eax, 7530h` (30000).\n"
            "SAFE     : the refcount==1 guard in DataCache_CleanupOldEntries still prevents\n"
            "           freeing anything in use; a zone change already full-flushes. Only\n"
            "           cost: re-loading an asset from disk if revisited after 30 s.\n"
            "RISK     : low. Pure revisit-optimization window; minor extra disk I/O only.\n"
            "           A top lever for the OOM -- consider enabling on memory-tight setups."
        ),
        apply=lambda d: patch_at_va(d, 0x5181C8, "0520bf02008d7b04", "05307500008d7b04"),
    ),

    # ----- QUALITY / USE MORE VRAM (opt-in) -------------------------------
    # Modern PCs leave the client's 2013-era budgets unused. Most of these spend abundant
    # VRAM and are address-space-safe (they touch DEFAULT-pool / render-target video memory,
    # not the 32-bit system-RAM shadow the OOM cares about).
    # ***EXCEPTION: `tex_force_max_detail` IS NOT ADDRESS-SPACE-SAFE.*** It used to sit under
    # this banner; it does not belong here. Read its why= before enabling it alongside `laa`.
    Fix(
        id="tex_force_max_detail",
        module="quality",
        enabled=False,   # keep it that way -- see FIELD TEST below
        title="Force max texture detail (mip-levels-to-skip = 0) -- LOOKS WORSE IN PLAY, do not enable",
        why=(
            "FIELD TEST (operator, 2026-08): tried in-game and it LOOKS VISUALLY WORSE.\n"
            "           That is the deciding evidence and it overrides the static analysis\n"
            "           below -- a fix whose whole purpose is 'more detail' that degrades the\n"
            "           image is not a quality fix, whatever the disassembly says.\n"
            "           Plausible mechanism, NOT verified: skipping mip levels is also what\n"
            "           makes minification stable, so forcing skip=0 restores top-resolution\n"
            "           mips that then alias and shimmer on distant/oblique surfaces. If\n"
            "           anyone revisits this, that is the hypothesis to test first -- and it\n"
            "           would mean the right lever is anisotropic filtering or a small\n"
            "           negative LOD bias, not the skip count.\n"
            "           DO NOT re-enable on the strength of the reasoning below alone.\n"
            "\n"
            "WHAT     : the Gamebryo 'mipmap levels to skip' global (word_15BD008, the\n"
            "           in-game Texture-Detail setting) makes CreateSurfFromRendererData\n"
            "           drop the top v8 = min(skip, mipCount-1) full-res mip levels of\n"
            "           every texture. Its only writer is the setter sub_BBA9A0 (verified\n"
            "           exhaustively: 4 refs to the global image-wide, exactly 1 store).\n"
            "           Force it to store 0 -> no mips skipped -> full-resolution textures.\n"
            "HOW      : at 0xBBA9A3 `mov eax,[ebp+arg_0]` -> `xor eax,eax; nop`, so the\n"
            "           following `mov word_15BD008, eax` writes 0 whatever the config\n"
            "           slider says. (VA-anchored; the `mov` window includes the global.)\n"
            "           sub_BBA9A0's sole caller sub_BB26F0 is referenced only from two .rdata\n"
            "           vtable slots, i.e. dispatched virtually -- so patching the implementation\n"
            "           catches every path.\n"
            "MEM -- CORRECTED 2026-08, THE OLD LINE WAS FALSE AND INVERTED THE MEMORY STORY.\n"
            "           It read 'VRAM only (textures are DEFAULT pool after the flips above) --\n"
            "           address-space-safe'. They are not DEFAULT. The textures whose mip levels\n"
            "           this restores are created by IDirect3DDevice9::CreateTexture (vtbl+0x5C)\n"
            "           with a HARD-CODED `push 1` = D3DPOOL_MANAGED, at 0xBED23F in\n"
            "           NiDX9SourceTextureData::CreateSurf and again at 0xBED74B in\n"
            "           ::CreateSurfFromRendererData. NO fix in this catalog touches that call --\n"
            "           `texpool_world_default` hooks the D3DX InMemory thunk and every\n"
            "           `tex_pool_*` edits a D3DX loader push. Applying the ENTIRE catalog leaves\n"
            "           both literals untouched (a control that could have failed, and did not).\n"
            "           So these stay MANAGED with a full system-RAM shadow no matter what else\n"
            "           is enabled, and skip=1 -> 0 roughly QUADRUPLES their bytes in BOTH VRAM\n"
            "           and the 32-bit address space.\n"
            "RISK     : MEDIUM-HIGH on memory-tight setups, not 'low'. This fix works AGAINST\n"
            "           `laa` and can precipitate the very OOM this catalog exists to fight.\n"
            "           It also pins Texture-Detail to max (overriding the slider) and costs a\n"
            "           little texture-load time. Consumer logic confirmed as exactly\n"
            "           min(skip, mipCount-1) in both CreateSurf and CreateSurfFromRendererData."
        ),
        apply=lambda d: patch_at_va(d, 0xBBA9A3,
            "8b4508a308d05b01", "33c090a308d05b01"),
    ),
    Fix(
        id="shadowmap_2048",
        module="quality",
        enabled=False,
        title="Shadow-map resolution 1024 -> 2048 (sharper dynamic shadows)",
        why=(
            "WHAT     : the whole-scene shadow map is a single 2^N render target; N is a\n"
            "           literal `push 0Ah` (=10 -> 1<<10 = 1024) passed to the shadow-RT\n"
            "           factory sub_1133650 from CZoneData::SetZoneRenderQuality. Bump N\n"
            "           to 11 -> 2048x2048. The projection matrix + texel size all derive\n"
            "           from the same 2^N, so it rescales consistently.\n"
            "HOW      : at 0x815C91 `push 0Ah` (6a0a) -> `push 0Bh` (6a0b). (For 4096 use\n"
            "           0Ch; 2048 is safe on any modern GPU.)\n"
            "MEM      : VRAM only (one extra RT surface, DEFAULT pool) — address-space-safe.\n"
            "REQUIRES THE PRISTINE b303 (noted 2026-08). This fix expects the stock `6a0a`, and\n"
            "           the Optimizations builds -- including this patcher's own default --in --\n"
            "           ALREADY ship `6a0c` (4096). There, `--only shadowmap_2048` is a guaranteed\n"
            "           hard FAIL and main() exits without writing. That is the self-verification\n"
            "           working as designed (it refuses rather than corrupting), but the fix is\n"
            "           non-functional on that input: point --in at Rag2_original.exe (cbeccb38)\n"
            "           to use it, or use shadowmap_4096 which is already what those builds have.\n"
            "RISK     : low. Sharper/less-aliased shadows; small GPU fill cost in the shadow\n"
            "           pass. a3=0 at the sole call site, so only the primary RT is made."
        ),
        apply=lambda d: patch_at_va(d, 0x815C91,
            "6a0a50e8b7d99100", "6a0b50e8b7d99100"),
    ),
    Fix(
        id="shadowmap_4096",
        module="quality",
        enabled=True,   # FIELD-PROVEN: measured present in the b303-2026-07-11 Rag2.exe the operator
                        # plays on (byte-level diff vs the pristine exe). Shipped configuration,
                        # not a suggestion -- the default build must reproduce the real client.
        title="Shadow-map resolution 1024 -> 4096 (sharpest real-time shadows)",
        why=(
            "WHAT     : same lever as shadowmap_2048 -- the whole-scene real-time shadow map\n"
            "           is a single 2^N render target (N = literal `push 0Ah` = 10 -> 1024)\n"
            "           passed to the shadow-RT factory sub_1133650. Bump N to 12 -> 4096x4096\n"
            "           (16x the texels of stock 1024). Verified in sub_1133650: v61=(1<<a2)\n"
            "           and the RT is created sub_C6D8E0(v61,v61,..); the projection matrix +\n"
            "           texel size all derive from the same 2^N, so it rescales consistently.\n"
            "HOW      : at 0x815C91 `push 0Ah` (6a0a) -> `push 0Ch` (6a0c).\n"
            "MEM      : VRAM only (one RT surface, DEFAULT pool) -- address-space-safe (this RT is\n"
            "           never CPU-Lock()ed). Pixel format resolved as DOUBLE_COLOR_32 (4 B/texel,\n"
            "           1 mip, no companion depth surface), so 4096^2 is EXACTLY 64 MiB -- the\n"
            "           earlier 'order 64-96 MB' hedge is retired.\n"
            "RISK     : low on modern GPUs. Mutually exclusive with shadowmap_2048 (same 2\n"
            "           bytes) -- enable only ONE, and that is structurally enforced: the second\n"
            "           patch_at_va sees neither its find nor its repl, returns None, and main()\n"
            "           exits without writing. Small extra shadow-pass fill cost.\n"
            "NOTE     : the Optimizations builds already ship `6a0c`, so against the default --in\n"
            "           this fix SKIPs as a no-op."
        ),
        apply=lambda d: patch_at_va(d, 0x815C91,
            "6a0a50e8b7d99100", "6a0c50e8b7d99100"),
    ),
    Fix(
        id="terrain_stream_2x",
        module="quality",
        enabled=False,
        title="Double the terrain streaming VB pool (700k -> 1.4M verts; denser far terrain)",
        why=(
            "WHAT     : the terrain streams through 5 DYNAMIC vertex buffers of 700000\n"
            "           verts each (~53 MB VRAM); when all 5 fill, remaining terrain verts\n"
            "           are DROPPED. Double the per-buffer size + flush ceiling (1.4M\n"
            "           verts, ~106 MB) so denser / farther terrain streams without dropout.\n"
            "HOW      : two same-length immediates kept in lockstep (bytes = verts*16):\n"
            "           buffer size push 0xAAE600 -> 0x155CC00 @0x80D2F4, and flush cmp\n"
            "           0xAAE60 -> 0x155CC0 @0x80D40D.\n"
            "MEM -- CORRECTED 2026-08. The old line read 'VRAM/AGP only (DEFAULT+DYNAMIC pool) --\n"
            "           address-space-safe'. That is refuted by the code's own\n"
            "           `Lock(0,0,&p,D3DLOCK_DISCARD)` at 0x80D37C: a buffer the CPU maps and\n"
            "           rewrites WHOLE every frame lives in CPU-visible AGP/PCIe memory charged to\n"
            "           the 32-bit process VA -- the exact budget `laa` exists to relieve. Cost is\n"
            "           +56 MB nominal (112 MB total across the 5 buffers), MORE with DISCARD\n"
            "           renaming, and it therefore interacts with `laa` rather than being free.\n"
            "RISK     : MEDIUM, and the cost is UNCONDITIONAL. The old line framed it as\n"
            "           conditional ('benefit shows only when streaming nears the ceiling'), but\n"
            "           because the lock is whole-buffer DISCARD the driver rename churn scales\n"
            "           with buffer SIZE, not with bytes written -- it is paid every frame even\n"
            "           when the extra capacity is never used.\n"
            "UNGUARDED FAILURE PATH (not previously disclosed): CreateVertexBuffer's HRESULT is\n"
            "           never checked (0x80D2FA is followed directly by `mov [esi+0x14],0`), and\n"
            "           VertexBuffer_FillFromData null-guards only slot 0. A failed allocation of\n"
            "           buffers 1-4 null-derefs at 0x80D377 -- the same 'fault on a small address'\n"
            "           signature as 18 of the 31 minidumps. Doubling 11.2 MB to 22.4 MB per\n"
            "           buffer materially raises the odds of reaching that path.\n"
            "           Both edits are applied together; raising the flush cap alone would\n"
            "           overflow the buffer. Call frequency was never established -- do not read\n"
            "           any 'per frame' framing into the benefit."
        ),
        apply=lambda d: apply_terrain_stream_2x(d),
    ),
    Fix(
        id="weather_particles_3x",
        module="quality",
        enabled=False,
        title="Denser weather particles 300 -> 900 (ParticleEffectWnd)",
        why=(
            "WHAT     : ParticleEffectWnd preallocates 300 screen-space weather/ambient\n"
            "           particles (snow/rain/petals). The constructor loop `for(i=0;i<300)`\n"
            "           heap-allocates one particle each and push_backs into a growable\n"
            "           std::vector. Raise the cap 300->900 for a 3x denser overlay.\n"
            "HOW      : at 0x9EB67C `cmp eax, 12Ch` (300) -> `cmp eax, 384h` (900).\n"
            "SAFE     : no fixed array -- the backing store is a std::vector<Particle*>\n"
            "           that self-grows (geometric reserve) and each particle is its own\n"
            "           operator new(44); Update/Render iterate the vector's actual size\n"
            "           `(end-begin)>>2`, not the constant. Raising it can't overflow.\n"
            "MEM      : ~28 KB extra heap (negligible for the 32-bit address space).\n"
            "RISK -- COST MODEL CORRECTED 2026-08. The old line said 'a few hundred more 2D point\n"
            "           draws/updates per frame; trivial'. Wrong in KIND, not just degree:\n"
            "           Render2D_DrawQuadFromRect flushes the batcher after EVERY SINGLE QUAD\n"
            "           (call 0x1098180 / append 0x1097E70 / unconditional flush 0x109165D), so\n"
            "           900 particles is ~900 DrawIndexedPrimitiveUP submissions per frame in a\n"
            "           single-threaded D3D9 client -- not 600 extra points inside one batch.\n"
            "           Still likely fine on a modern CPU, but it is a draw-call cost, and it is\n"
            "           the reason not to push this to absurd values. Correctness is unaffected."
        ),
        apply=lambda d: patch_at_va(d, 0x9EB67C,
            "3d2c0100000f8c09ffffff", "3d840300000f8c09ffffff"),
    ),

    # ----- EFFICIENCY: quality-neutral FPS (better resource usage) --------
    # These raise FPS by cutting wasted work / unlocking the frame rate, WITHOUT changing
    # a single rendered pixel (unlike the render kill-switches, which trade quality).
    Fix(
        id="no_frame_sleep",
        module="efficiency",
        enabled=True,   # FIELD-PROVEN: present in the b303-2026-07-11 build the operator has been
                        # playing on, so this is shipped configuration, not a suggestion. The
                        # catalog default must reproduce the real client -- a plain
                        # `python patch_client.py` used to omit it and silently build a
                        # DIFFERENT exe from the one in use.
        title="Remove the per-frame Sleep(0) yield in the main loop (smoother, less jitter)",
        why=(
            "WHAT     : the main loop (_wWinMain) calls kernel32!Sleep(0) once per rendered\n"
            "           frame on the no-message path. Sleep(0) is a syscall + a cooperative\n"
            "           yield that can lose the rest of the scheduler quantum to background\n"
            "           threads. NOP the `push ebx; call ds:Sleep` (7 bytes) so the loop\n"
            "           runs straight into the next frame.\n"
            "EVIDENCE : disasm 0xA50FC2 = `push ebx(=0 dwMilliseconds); call ds:[Sleep]`\n"
            "           (13552C8h = Sleep import). It is NOT a hard FPS cap (arg is 0) and\n"
            "           there is no other frametime limiter in the tick (GameTick's timer is\n"
            "           a QPC read for animation delta only). Both push and call are NOPped\n"
            "           together (Sleep is __stdcall -> keeps esp balanced).\n"
            "QUALITY  : neutral (identical frame; only the yield disappears).\n"
            "RISK     : low-med. The game thread now busy-runs one core at 100% (more\n"
            "           heat/power) -- normal for an uncapped loop. Modest FPS + much\n"
            "           smoother pacing under CPU load."
        ),
        apply=lambda d: patch_at_va(d, 0xA50FC2, "53ff15c8523501", "90909090909090"),
    ),
    Fix(
        id="camera_upload_dedup",
        module="efficiency",
        enabled=False,
        title="Skip the per-object camera upload when nothing about the camera changed",
        why=(
            "WHAT     : NiDX9Renderer::SetCameraData (sub_BB2C70) is reached once per actor, per\n"
            "           vehicle, per attachment and per terrain node, BEFORE culling, via\n"
            "           sub_C407D0 -> sub_C56250 -> sub_C57570. Its whole visible effect is three\n"
            "           D3D9 calls: SetTransform(VIEW), SetTransform(PROJECTION) and SetViewport.\n"
            "           A crowded frame therefore issues hundreds to thousands of byte-identical\n"
            "           uploads. A .patch cave at 0xBB2C88 compares the six inputs against the\n"
            "           caches the function itself writes and, on a full match, jumps to the\n"
            "           function's own epilogue.\n"
            "EVIDENCE : the function caches every input it consumes -- a5->this+2160, a4->+2176,\n"
            "           a3->+2192, a2->+2208, a6->+2444 (qmemcpy 0x1C), a7->+2472 (0x10, and the\n"
            "           returned dword at +2484). Identical inputs therefore mean the bound D3D\n"
            "           state was computed from exactly these values, so the three calls are\n"
            "           provably redundant. Confirmed as the top finding of the 2026-09-02 IDA\n"
            "           audit (adversarially verified; see wiki client/performance-audit-2026-09.md).\n"
            "           The memo also compares the renderer `this` and the bound render target\n"
            "           (this+1784), so a second renderer or a render-target switch invalidates it.\n"
            "QUALITY  : neutral by construction -- the skipped calls would have re-set state that\n"
            "           is already bound. Nothing about what is drawn changes.\n"
            "WHY IT PAYS HERE: under a D3D9->D3D11/12 or ->Vulkan translation wrapper each\n"
            "           SetTransform invalidates derived matrices and the fixed-function constant\n"
            "           buffer; this is the measured 'GPU ~10%, CPU ~10%, capped' signature.\n"
            "RISK     : medium, and UNMEASURED IN GAME AS OF THIS WRITING. The residual risk is\n"
            "           state we do not own: if anything between two identical calls sets\n"
            "           D3DTS_VIEW/PROJECTION or the viewport by another path, skipping leaves it\n"
            "           stale. The render-target term covers the shadow pass and UI\n"
            "           render-to-texture, which are the known switchers. TEST: shadow pass, UI\n"
            "           render-to-texture, camera cuts, vehicle/mount cameras, zoning."
        ),
        apply=_apply_camera_upload_dedup,
    ),
    Fix(
        id="present_immediate",
        module="efficiency",
        enabled=False,
        title="Force present interval IMMEDIATE (uncap FPS above the monitor refresh)",
        why=(
            "WHAT     : the D3DPRESENT_PARAMETERS builder (sub_BB0370) sets Presentation\n"
            "           Interval from a config enum via sub_BBA6F0. Replace the 5-byte\n"
            "           `call sub_BBA6F0` @0xBB0536 with `mov eax, 80000000h` so\n"
            "           PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE for every\n"
            "           device create/reset -> Present() no longer waits for vblank.\n"
            "EVIDENCE : disasm 0xBB0532 `mov edx,[arg]; push edx; call sub_BBA6F0; add\n"
            "           esp,4; mov [pp+34h],eax`. Skipping the (side-effect-free) enum map\n"
            "           and loading 0x80000000 keeps esp balanced (the add esp,4 still\n"
            "           cleans the pushed edx). sub_BB0370 is the sole pp builder for both\n"
            "           CreateDevice and Reset, so one edit is durable.\n"
            "QUALITY  : neutral (present interval governs WHEN the backbuffer flips, never\n"
            "           what is drawn). Tearing above refresh is the defined IMMEDIATE\n"
            "           tradeoff, not a fidelity reduction.\n"
            "FULLSCREEN CAVEAT (added 2026-08): InitializePresentParams RE-VALIDATES the interval\n"
            "           at its tail, AFTER the patched store. Windowed whitelists {0, IMMEDIATE,\n"
            "           1} at 0xBB0681 so IMMEDIATE survives. FULLSCREEN does\n"
            "           `if ((interval & caps.PresentationIntervals@+0x14) == 0) interval = 1`\n"
            "           at 0xBB06AB, so on an adapter that does not advertise IMMEDIATE the patch\n"
            "           SILENTLY DEGRADES TO VSYNC ON. It fails safe, but it can be a no-op.\n"
            "RISK     : low. Uncaps FPS (60Hz-pinned scene -> as fast as the HW allows);\n"
            "           more GPU/CPU/power. No-op if the config already runs vsync off."
        ),
        apply=lambda d: patch_at_va(d, 0xBB0536, "e8b5a10000", "b800000080"),
    ),
    Fix(
        id="d3d_no_multithread",
        module="efficiency",
        enabled=False,
        title="Drop D3DCREATE_MULTITHREADED (remove per-D3D-call runtime locking)",
        why=(
            "WHAT     : the device creator ORs D3DCREATE_MULTITHREADED (0x4) into the\n"
            "           BehaviorFlags when a config bit is set. That flag makes the D3D9\n"
            "           runtime take a global critical section around EVERY API call\n"
            "           (Draw*/Set*/Present/Lock). The render loop is single-threaded, so\n"
            "           that lock is wasted CPU. NOP the `or edx,4` so the flag is never set.\n"
            "ADDRESS CORRECTED 2026-08 -- THE OLD SITE WAS A SILENT NO-OP. It patched 0xBAE791,\n"
            "           which sits in NiDX9Renderer::Initialize @0xBAE5E0, whose only caller\n"
            "           NiDX9Renderer::Create @0xBAE4B0 has ZERO references in the entire file\n"
            "           (no rel32, no stored pointer, and sub_BADD60 ends `retn` @0xBAE4A1 with\n"
            "           int3 padding, so there is no fall-through either). It is dead code.\n"
            "           patch_at_va succeeded and printed '[APPLIED] ... 3 bytes' while changing\n"
            "           nothing the game runs. The LIVE renderer init is the duplicate\n"
            "           NiDX9Renderer::Initialize_2 @0x1139E70 (Create @0x1139CA0 <-\n"
            "           Renderer_CreateDeviceFromDisplaySettings @0x110BC80, call at 0x110BDC3),\n"
            "           whose MULTITHREADED site is `or eax,4` at 0x113A081. That is now the\n"
            "           anchor. Nothing branches into 0x113A082/0x113A083 (0x113A084 is reached\n"
            "           only by fall-through; the `je` at 0x113A073 targets 0x113A090, outside\n"
            "           the window).\n"
            "QUALITY  : neutral (identical output; only removes internal locking).\n"
            "RISK     : ***MEDIUM -- correctness, not visual -- AND THE RATING IS UNVALIDATED.***\n"
            "           OPEN QUESTION, not a checked box: does any background thread call the D3D\n"
            "           device? Nobody has answered this. A callgraph BFS from the thread entry\n"
            "           points returned 0 D3D sinks, but the SAME method's control BFS from\n"
            "           _wWinMain/GameTick also returned 0 of 45 known sinks -- the graph cannot\n"
            "           see virtual dispatch, so that negative is void, not reassuring. Do not\n"
            "           read the old 'safe ONLY if...' phrasing as implying it was verified.\n"
            "SECOND OPEN QUESTION : the flag is gated on a config boolean --\n"
            "           `movzx eax,[edx+0x18]; test eax,eax; je +9; or [ebp-0x24],0x20` at\n"
            "           0x110BCEA. If that display-settings byte is always 0 in a shipped config,\n"
            "           MULTITHREADED is never set and this fix is a no-op even at the corrected\n"
            "           address. Measure it before claiming an FPS win.\n"
            "           Test heavy zoning / model streaming before relying on it."
        ),
        apply=lambda d: patch_at_va(d, 0x113A081,
            "83c8048b8d28ffffff", "9090908b8d28ffffff"),
    ),
    Fix(
        id="hw_vp_puredevice",
        module="gpu",
        enabled=False,
        title="[NO-OP OR HARMFUL] HARDWARE_VP + PUREDEVICE on the SWVP fallback rung",
        why=(
            "WHAT     : at 0xBB0E6C widen `or ecx,20h` (SWVP) to `or ecx,50h` =\n"
            "           D3DCREATE_HARDWARE_VERTEXPROCESSING(0x40) | D3DCREATE_PUREDEVICE(0x10).\n"
            "EVIDENCE : sub_BB0DE0 case 0 really does natively compose 0x50 -- that part of the\n"
            "           old EVIDENCE line is TRUE and was re-verified. The engine does ship a\n"
            "           HAL pure-device mode.\n"
            "THE BENEFIT ALREADY EXISTS UNPATCHED (2026-08 audit). That is exactly the problem.\n"
            "           The mode argument is the hardcoded `push 0` at 0x110BDA1, so the client\n"
            "           ALREADY creates its device as case 0 = HAL|HWVP|PUREDEVICE. The cheaper\n"
            "           Set*/SetRenderState/SetTransform calls this fix advertises are already in\n"
            "           effect. 0xBB0E6C is case 3 of a 7-case switch and case 3 is the\n"
            "           SOFTWARE-VP rung of a degradation ladder (see the hw_vertex_proc header\n"
            "           comment).\n"
            "SO WHAT DOES IT DO : it makes the LAST rung byte-identical to rung 0 -- which, by\n"
            "           construction, has already failed by the time rung 3 is reached. The only\n"
            "           reachable outcome is converting a graceful SWVP degradation into a\n"
            "           startup failure, with no successor rung to catch it.\n"
            "QUALITY  : neutral.\n"
            "RISK     : the old 'medium' understated it. PUREDEVICE additionally requires\n"
            "           D3DDEVCAPS_PUREDEVICE and CreateDevice fails without it -- which its own\n"
            "           sibling documents but this entry omitted, and which is TERMINAL here\n"
            "           because rung 3 has no successor. Do not enable for FPS: there is no FPS\n"
            "           to win. MUTUALLY EXCLUSIVE with `hw_vertex_proc` (enabling both still\n"
            "           resolves to 0x50)."
        ),
        apply=lambda d: apply_hw_vp_puredevice(d),
    ),

    # ----- INPUT LATENCY (opt-in) -----------------------------------------
    Fix(
        id="camera_snappy_look",
        module="input",
        enabled=False,
        title="Reduce mouse-look yaw smoothing (0.5 -> 0.75) for a more responsive camera",
        why=(
            "WHAT     : the follow-camera yaw is exponentially smoothed: each frame the\n"
            "           rendered angle moves only 50% toward the mouse-pointed target, so\n"
            "           the view keeps gliding ~2-4 frames after the mouse stops = input\n"
            "           lag. Raise the interpolation factor 0.5 -> 0.75 so it tracks the\n"
            "           mouse much more closely.\n"
            "HOW      : both drag-look cases do `fld ds:0x1357D40` (the SHARED 0.5 float,\n"
            "           read by 100+ unrelated funcs -- must NOT be edited in place). Repoint\n"
            "           the fld's disp32 to the existing read-only 0.75 float at 0x13C4E2C\n"
            "           (`d905407d3501` -> `d9052c4e3c01`) at 0x8AAB24 and 0x8AABA7.\n"
            "EVIDENCE : UpdateInterpolation does cur = (1-f)*cur + f*target; f is +148 (yaw).\n"
            "           0.5/0.75/1.0 confirmed on-disk at 0x1357D40/0x13C4E2C/0x1357CEC.\n"
            "RISK     : low. Only the follow-camera yaw responsiveness. For a full 1:1\n"
            "           instant look use 0x1357CEC (1.0) instead: `d905ec7c3501`. Pitch is\n"
            "           smoothed harder (0.25) and needs a cave -- not yet included.\n"
            "           Does not touch the shared 0.5 constant."
        ),
        apply=lambda d: apply_camera_snappy(d),
    ),

    # ----- D3D9 rendering improvements (opt-in) ---------------------------
    Fix(
        id="af_global_chokepoint",
        module="gpu",
        enabled=False,   # PREMISE REFUTED + REDUNDANT -- see REFUTED below. Kept, not deleted,
                         # because the cave assembles and verifies correctly and someone will
                         # rediscover 0x45F740 and think it is the global hook. It is not.
        title="[DISABLED: premise refuted] 16x AF hooked at CRenderStateMgr::SetSamplerState",
        why=(
            "REFUTED (2026-08-03), TWICE OVER, BOTH MEASURED:\n"
            "  1. 0x45F740 IS NOT A CHOKEPOINT. It has **5 xrefs from 3 functions**, and 3 of\n"
            "     the 5 are sub_DDC350 -- the SpeedTree vegetation setter this fix was written\n"
            "     to route around. The other two are sub_B9B2E0 and sub_BF54B0. The world's\n"
            "     sampler state never passes through it. The class is a shadow-state cache:\n"
            "     SetSamplerState writes the pending value at [this + (type + 14*sampler)*8 +\n"
            "     0x19A4] and CRenderStateMgr::ApplySamplerState (2 callers) compares it with\n"
            "     the applied copy at +0x19A0 before calling the device. Gamebryo's NiDX9\n"
            "     renderer keeps its own state cache and does not use this one.\n"
            "  2. IT IS REDUNDANT REGARDLESS. The client ships DXVK (SHIPPING/d3d9.dll, v3.0.1)\n"
            "     and dxvk.conf already carries `d3d9.samplerAnisotropy = 16`, echoed back in\n"
            "     the 'Effective configuration' block of Rag2_d3d9.log. AF is forced for the\n"
            "     whole scene at the translation layer, above anything this exe can do.\n"
            "LESSON   : 'both paths must funnel through the state manager' was an assumption\n"
            "           about how the engine is built, not a measurement. One xref query would\n"
            "           have killed it before the cave was written. Count the callers first.\n"
            "\n"
            "--- original rationale, kept for the record ---\n"
            "SYMPTOM  : the operator reported the game looking unfiltered/shimmery. The existing\n"
            "           `anisotropic_filtering` fix changed nothing, because it patches the wrong\n"
            "           function.\n"
            "ROOT     : sub_DDC350 -- what anisotropic_filtering edits -- has exactly ONE caller\n"
            "           (sub_DD5D40), whose own callers all live in the SpeedTree band\n"
            "           0xDC1B10..0xDC3EE0. It configures VEGETATION samplers, never terrain or\n"
            "           world meshes. The world's filter state is declared in the .fx assets\n"
            "           (211/211 samplers set Min/Mag/MipFilter) and pushed through an\n"
            "           ID3DXEffectStateManager into CRenderStateMgr::SetSamplerState, so any\n"
            "           fixed-function write is overwritten on every effect pass.\n"
            "FIX      : hook 0x45F740 itself -- the single point BOTH paths funnel through. The\n"
            "           cave rewrites MAGFILTER(5) and MINFILTER(6) from LINEAR(2) to\n"
            "           ANISOTROPIC(3), and on the MIN call also issues MAXANISOTROPY(10)=16 for\n"
            "           the same sampler. Without that second write ANISOTROPIC degenerates to\n"
            "           LINEAR (D3D default MaxAnisotropy is 1) and the change buys nothing --\n"
            "           which is exactly how the old fix failed.\n"
            "EVIDENCE : arg order sampler/type/value is forced by the function's own index maths\n"
            "           (`edx*7*2 + ebx` == type + 14*sampler). State numbering is grounded twice:\n"
            "           the same function issues 5/6/7 for MAG/MIN/MIP, and the statically linked\n"
            "           D3DX9 effect parser's enum table at .rdata 0x1480708 spells out\n"
            "           MAGFILTER..MAXANISOTROPY = 5..10.\n"
            "           MIPFILTER(7) is deliberately NOT touched: ANISOTROPIC is not a legal\n"
            "           MIPFILTER value, and feeding it one is what turns mipmapping off.\n"
            "           The nested call re-enters this hook with type=10, matches neither 5 nor 6\n"
            "           and falls through -- one level of recursion, no loop. ecx is saved across\n"
            "           it (thiscall may clobber) and the callee cleans its own 12 bytes.\n"
            "LIMITS   : NOT runtime-tested. It is derived from disassembly only, and this is the\n"
            "           third attempt at getting AF working on this client -- the previous two\n"
            "           looked right on paper too. If the image looks wrong, disable this first.\n"
            "           It also does not explain the operator's 'no mipmaps' report: every static\n"
            "           check says world mipmapping should already be on (185/211 effect samplers\n"
            "           declare MipFilter=LINEAR, 99.93% of ZONE1 terrain .dds ship full chains,\n"
            "           and the mip-skip global is identically 0).\n"
            "RISK     : medium. Touches EVERY sampler write in the engine, UI included. A 16x AF\n"
            "           request costs fill rate on weak GPUs. Fully reversible."
        ),
        apply=apply_af_global,
    ),
    Fix(
        id="anisotropic_filtering",
        module="gpu",
        enabled=True,   # FIELD-PROVEN: measured present in the b303-2026-07-11 Rag2.exe the operator
                        # plays on (byte-level diff vs the pristine exe). Shipped configuration,
                        # not a suggestion -- the default build must reproduce the real client.
        title="[.patch cave] Enable 16x anisotropic filtering (crisp textures at angles)",
        why=(
            "WHAT     : the client filters textures at best LINEAR (trilinear) and NEVER\n"
            "           sets D3DSAMP_MAXANISOTROPY -> no anisotropic filtering, so ground/\n"
            "           terrain/walls blur at grazing angles. Enable 16x AF -- the single\n"
            "           biggest image-quality feature a 2013 client lacks, ~free on any\n"
            "           modern GPU.\n"
            "HOW      : (1) inline flip the LINEAR(2) filter constant at 0xDDC3C5 ->\n"
            "           ANISOTROPIC(3) (only the LINEAR path; POINT stays POINT); (2) a\n"
            "           `.patch` cave after sub_DDC350's MIP SetSamplerState injects\n"
            "           SetSamplerState(stage, MAXANISOTROPY, 16) so ANISOTROPIC actually\n"
            "           does 16x (else it degenerates to LINEAR). Both device calls go\n"
            "           through the client's own SetSamplerState (0x45F740, retn 0Ch).\n"
            "COVERAGE -- NARROWER THAN THE LINE ABOVE CLAIMED (measured 2026-08-03).\n"
            "           This is **SpeedTree vegetation only**, not 'model/terrain'.\n"
            "           sub_DDC350 has exactly one caller, sub_DD5D40, and every caller of\n"
            "           THAT sits in 0xDC1B10..0xDC3EE0, the SpeedTree band. Terrain and\n"
            "           world meshes never reach it: they declare their samplers inside the\n"
            "           .fx effects (data, not the exe) and the engine pushes those through\n"
            "           its own NiDX9 state cache.\n"
            "REDUNDANT: this client already ships DXVK (SHIPPING/d3d9.dll v3.0.1) with\n"
            "           `d3d9.samplerAnisotropy = 16` in dxvk.conf, confirmed in the\n"
            "           'Effective configuration' block of Rag2_d3d9.log. AF is forced for\n"
            "           the whole scene there, above anything this patch reaches. Kept\n"
            "           enabled only because it is measured present in the exe the operator\n"
            "           plays on and the default build must reproduce that binary.\n"
            "           See also `af_global_chokepoint`, disabled for the same reason.\n"
            "RISK     : medium. Cave verified to disassemble; **not runtime-tested.** MIP=3\n"
            "           is out-of-spec but drivers clamp it to LINEAR harmlessly."
        ),
        apply=lambda d: apply_anisotropic_filtering(d),
    ),
    Fix(
        id="backbuffer_double",
        module="gpu",
        enabled=False,
        title="BackBufferCount 1 -> 2 (double buffering; smoother present with vsync off)",
        why=(
            "WHAT     : the D3DPRESENT_PARAMETERS builder clamps a 0 BackBufferCount to 1\n"
            "           (single buffer). Set it to 2 so the GPU has a spare back buffer --\n"
            "           smoother frame delivery, especially paired with present_immediate.\n"
            "HOW      : at 0xBB05C9 `mov [edx+0Ch], 1` -> `mov [edx+0Ch], 2` (the count==0\n"
            "           default path). SwapEffect is already DISCARD, which supports N>=1\n"
            "           back buffers.\n"
            "RISK     : low. Same-length, within the existing 1..3 clamp. Costs a few MB\n"
            "           VRAM + up to ~1 frame of latency. Only takes effect if the video\n"
            "           config leaves the count at 0/default."
        ),
        apply=lambda d: patch_at_va(d, 0xBB05C9, "c7420c01000000eb13", "c7420c02000000eb13"),
    ),

    # ----- OPTIMIZATION (CPU / draw calls) --------------------------------
    Fix(
        id="inline_singleton_getters",
        module="efficiency",
        enabled=True,   # FIELD-PROVEN: present in the b303-2026-07-11 build the operator has been
                        # playing on, so this is shipped configuration, not a suggestion. The
                        # catalog default must reproduce the real client -- a plain
                        # `python patch_client.py` used to omit it and silently build a
                        # DIFFERENT exe from the one in use.
        title="Inline hot-loop singleton getters (call -> mov) -- quality-neutral CPU",
        why=(
            "WHAT     : Render_GameWorld's per-actor loop calls CModeMgr/CConfig/CCutscene\n"
            "           ::GetInstance -- each a one-instruction `mov eax,[global]; ret` --\n"
            "           through a real `call rel32` several times per visible actor per\n"
            "           frame. Replace `call getter` (E8 rel32) with the getter's own body\n"
            "           `mov eax,[global]` (A1 abs32), same 5 bytes, removing a call+ret.\n"
            "EVIDENCE : the 3 getters are verified pure `mov eax,[g]; ret` (no lazy init):\n"
            "           CModeMgr@0x8A8A30, CConfig@0xA43F80, CCutscene@0x462EF0. Each\n"
            "           replacement's abs32 equals that target's own operand (5/5 match).\n"
            "           Applied all-or-none. Absolute addressing is provably safe here:\n"
            "           DllCharacteristics 0x8100 has no DYNAMIC_BASE and the image has NO\n"
            "           .reloc section, so it can only load at 0x400000.\n"
            "PRECISION: 4 of the 5 sites are in Render_GameWorld's per-actor loop; the 5th\n"
            "           (0x821A0D) is in the function's ENTRY block, so it runs once per frame,\n"
            "           not once per actor.\n"
            "QUALITY  : output-identical (writes only eax, no flags, net-zero esp).\n"
            "RISK     : very low. Pure CPU micro-opt in a hot loop; no behaviour change."
        ),
        apply=lambda d: apply_inline_getters(d),
    ),
    Fix(
        id="reduce_object_draw_distance",
        module="performance",
        enabled=False,
        title="Cull world objects at 0.75x fog-far distance (fewer draw calls) [some popping]",
        why=(
            "WHAT     : the scene cull loop submits world objects out to the fog-FAR\n"
            "           distance (a getter @0x8142C0 returning global 0x159F830). Scale that\n"
            "           getter's output by 0.75 so distant objects stop being submitted at\n"
            "           75% of the fog-far range -> ~fewer per-frame object draw calls\n"
            "           outdoors. Fog/lighting/far-clip are UNCHANGED (the getter has one\n"
            "           xref; the real far-clip/fog use a separate local).\n"
            "HOW      : the getter body `fld [0x159F830]; ret` + its CC padding is rewritten\n"
            "           in place to `fld [0x159F830]; fmul [0x13C4E2C(=0.75)]; ret` (13 bytes\n"
            "           into the 7-byte body + padding; no cave, reuses the 0.75 constant).\n"
            "RISK     : NOT zero-quality -- mid-distance objects pop out earlier while the\n"
            "           fog band still shows (subtle at 0.75; more aggressive at 0.5). A\n"
            "           draw-distance/FPS tradeoff -- opt in if you want the frames."
        ),
        apply=lambda d: patch_at_va(d, 0x8142C0,
            "d90530f85901c3cccccccccccc", "d90530f85901d80d2c4e3c01c3"),
    ),
    Fix(
        id="cone_cull",
        module="performance",
        enabled=True,   # FIELD-PROVEN: present in the b303-2026-07-11 build the operator has been
                        # playing on, so this is shipped configuration, not a suggestion. The
                        # catalog default must reproduce the real client -- a plain
                        # `python patch_client.py` used to omit it and silently build a
                        # DIFFERENT exe from the one in use.
        title="[.patch cave] View-cone cull: skip world objects behind the camera (make far draw distance cheap)",
        why=(
            "WHAT     : SceneManager_Cull_And_Render (0x806D00) submits EVERY world object\n"
            "           within the fog-far radius each frame with only a distance test -- no\n"
            "           frustum/direction test. So objects beside and BEHIND the camera (never\n"
            "           visible) still cost draw calls. This cave adds a forward-dot cone cull\n"
            "           at the per-object hook 0x80757F: cull anything more than SLACK units\n"
            "           behind the camera (and, if COS2>0, outside the view cone).\n"
            "SCOPE CORRECTED 2026-08: 'the cone is what makes a long draw distance affordable' is\n"
            "           FALSE FOR THIS HOOK. 0x80756E does `fcomp ds:13B5218` (= 160.0) then\n"
            "           `jp 0x807DB4`, so NO node beyond 160 world units ever reaches 0x80757F.\n"
            "           The fog-far / 480 gate that `max_view_distance` moves is a different and\n"
            "           EARLIER test at 0x80736C. This cave cannot make a long draw distance\n"
            "           affordable, because it never sees the distant nodes.\n"
            "HOW      : 123-byte cave in the .patch section; hook overwrites `mov ecx,[ebx+88h]`.\n"
            "           forward = camera world matrix col0 [ebx+64/70/7C] (Right=col1, Up=col2,\n"
            "           both verified); deltas cam-obj already live at [ebp-2E4/-2E0/-2DC]. Cull\n"
            "           jumps to 0x807DB4 (the engine's OWN cull tail -> child-queue clear), keep\n"
            "           re-execs the displaced insn -> 0x807585. FPU balanced on every exit.\n"
            "TUNE     : _CONECULL_SLACK and _CONECULL_COS2 at the top of the file, then rebuild.\n"
            "*** READ THE SHIPPED VALUES ***  This why= used to say 'default 0 = behind-only, no\n"
            "           side cull' and 'keep COS2=0 until the sign is confirmed'. The file has\n"
            "           shipped _CONECULL_COS2 = 0.1 -- a LIVE ~143 deg side cone -- so the doc\n"
            "           described a strictly weaker patch than the bytes produce. The tunable's\n"
            "           own comment ('sign already confirmed live') also contradicted the\n"
            "           'runtime-untested / confirm the sign first' text below it. Both are now\n"
            "           stated as they are: the behind-cull sign was confirmed live; the SIDE\n"
            "           cone at 0.1 has not been.\n"
            "NO RADIUS TERM : the cone is an origin-POINT test, while the engine's own fog gate is\n"
            "           radius-aware ([esi+150h]-[esi+30h]). A near or large node whose ORIGIN is\n"
            "           >71.6 deg off-axis but whose geometry is on screen WILL pop. 'Comfortably\n"
            "           wider than the view frustum so on-screen objects are not clipped' covers\n"
            "           the angle only, not the extent.\n"
            "VERIFY   : if the world IN FRONT of you vanishes/flickers as you turn, the forward\n"
            "           sign is inverted -> flip the two `jb`s (1 byte each).\n"
            "RISK     : med. Behind-cull sign confirmed live; the side cone (COS2=0.1) and the\n"
            "           radius blind spot are NOT runtime-tested. Set COS2=0.0 for behind-only if\n"
            "           you see edge pop-in. OFF by default; fully reversible (separate exe)."
        ),
        apply=lambda d: apply_conecull(d),
    ),
    Fix(
        id="actor_no_far_cull",
        module="performance",
        enabled=False,
        title="Draw actors/enemies at any distance (remove Render_GameActors far-cull) [perf cost in crowds]",
        why=(
            "WHAT     : enemies/actors do NOT use the world-object fog-far loop; they are hard\n"
            "           distance-culled in Render_GameActors (0x821FA0): at 0x8224D4 a `jp` skips\n"
            "           the draw when distToPlayer^2 >= drawDist^2. NOP that jump (6 bytes) so\n"
            "           every actor that passes the visibility/LOS checks is drawn regardless of\n"
            "           distance -> you see distant enemies instead of them popping out.\n"
            "HOW      : 0x8224D4 `jp loc_8222B8` (0F 8A DE FD FF FF) -> 6x NOP (90). Verified on\n"
            "           disk + disasm (fcompp/fnstsw/test ah,5 distance compare just above).\n"
            "RISK     : med. Only a distance cull is removed (visibility/LOS still apply), but in\n"
            "           a dense city/crowd this submits the whole area's actor list = FPS cost.\n"
            "           Best paired with cone_cull. OFF by default; reversible."
        ),
        apply=lambda d: patch_at_va(d, 0x8224D4, "0f8adefdffff", "909090909090"),
    ),
    Fix(
        id="actor_target_no_distance_cap",
        module="gameplay",
        enabled=True,   # FIELD-PROVEN: measured present in the b303-2026-07-11 Rag2.exe the operator
                        # plays on (byte-level diff vs the pristine exe). Shipped configuration,
                        # not a suggestion -- the default build must reproduce the real client.
        title="Remove the 30-unit target-range cap (target/nameplate distant enemies) [gameplay]",
        why=(
            "WHAT     : CCharacter_CanTarget (0x4D81A0) ends with `return distance <= 30.0`\n"
            "           (double @0x1358008). NOP the distance-reject branch so it ignores range.\n"
            "SCOPE CORRECTED 2026-08 -- IT IS THE TAB-TARGET CYCLER ONLY. The old text claimed\n"
            "           'no nameplate / click / tab-target' and 'affects all CanTarget callers'.\n"
            "           CCharacter_CanTarget has exactly ONE code xref and no data xrefs, and the\n"
            "           chain is CGameMode::HandleInput_KeyAction -> CTargetSelector_SelectTarget\n"
            "           -> CanTarget. Nameplates and mouse click-to-target do NOT go through it.\n"
            "           Real effect: distant enemies become TAB-SELECTABLE, and nothing else.\n"
            "HOW      : 0x4D8396 `jz loc_4D8259` (0F 84 BD FE FF FF) -> 6x NOP (90). 0x4D8259 IS\n"
            "           the reject (`xor al,al; pop edi; pop esi; pop ebx; mov esp,ebp; pop ebp;\n"
            "           retn 8`, 23 refs), so NOPping is the correct edit -- note this is the\n"
            "           MIRROR IMAGE of the enchant_no_camdist bug, where the jump was the\n"
            "           do-nothing path and NOPping shipped the opposite effect. Fall-through pops\n"
            "           the same three registers and also `retn 8`; the x87 stack is clean because\n"
            "           the `fld` at 0x4D8388 is consumed by the popping `fcomp`. Hex-Rays\n"
            "           confirms `return v29 <= unk_1358008;`. The 30.0 double is SHARED (12\n"
            "           xrefs, not ~11) so it is NOT edited. Already applied in the 6-section\n"
            "           Rag2.exe -- the very build the 31 minidumps come from -- with zero crash\n"
            "           evidence in it.\n"
            "PAIR     : complements actor_no_far_cull (renders the distant MODEL).\n"
            "RISK     : gameplay change. Also lets the player ORIGINATE attack/skill requests at\n"
            "           arbitrary range; the server range-checks and rejects those, so the\n"
            "           exposure is a rejected request, not an exploit. Opt-in; reversible. For a\n"
            "           finite cap instead, ask for a cave repointing the fcomp to a private\n"
            "           1500.0 double."
        ),
        apply=lambda d: patch_at_va(d, 0x4D8396, "0f84bdfeffff", "909090909090"),
    ),
    Fix(
        id="deferred_equip_load",
        module="efficiency",
        enabled=False,
        title="[.patch cave] Spread equipment-NIF loading over frames (kill crowd-entry stutter)",
        why=(
            "WHAT     : CActorMotion::LoadEquipmentModels (0x9272A0) is the ONLY main-thread\n"
            "           SYNCHRONOUS asset load in the render path -- it calls CNifLoader::\n"
            "           LoadNifFile (disk read + parse) inline for each actor's body/weapon/\n"
            "           equipment .nif. When a crowd of differently-geared actors streams\n"
            "           into range in one frame, that's a burst of synchronous loads = a\n"
            "           visible hitch. (World-model streaming is already async PPL, so this\n"
            "           is the sole sync loader worth deferring.)\n"
            "HOW      : a `.patch` cave at the function entry rate-limits to <=2 sync loads\n"
            "           per 33 ms (GetTickCount window, 2 DWORDs of state in the section).\n"
            "           Over budget -> return 0.\n"
            "*** THE RETRY PREMISE IS FALSE (2026-08 audit) -- THIS IS THE MAIN OPEN RISK. ***\n"
            "           The old text said the caller 'already treats [0] as not ready, retry next\n"
            "           frame (mov [esi+257h],al)'. That store is a STATUS RECORD, not a retry:\n"
            "           UpdateMotionState has exactly 2 xrefs (0x918FBB, 0x919970), both\n"
            "           immediately after a CActorMotion constructor, i.e. it is a SETUP call, and\n"
            "           the only readers/writers of +0x257 image-wide are inside UpdateMotionState\n"
            "           itself. On CSkillMotionInfo::SetupActorMotions -> LoadMotionSequence\n"
            "           (0x918FBB) the return value is DISCARDED outright, and the route is\n"
            "           event-driven (OnNPC_In_RMI2506 / OnPC_In_RMI2504 /\n"
            "           AnsAppearanceChange_RMI3046 -- mount, respawn, job change), which is\n"
            "           EXACTLY the crowd burst this fix targets. Same for one-shot UI previews\n"
            "           (0x6B318B CreateCharacterPreviewActor, UIFXEnchantWnd::CreatePreviewActor).\n"
            "           A budget-denied 0 on those paths is NEVER re-driven, so the failure mode\n"
            "           is not 'pops in a frame later' -- it is a POSSIBLY PERMANENT missing\n"
            "           equipment model on that actor until something else re-triggers the load.\n"
            "PARTIAL FIX ONLY : the limiter caps the NUMBER OF CALLS that may stall per frame; it\n"
            "           does not split the burst INSIDE one call. A single heavily-geared actor\n"
            "           still does up to 4 synchronous LoadNifFile and still stalls a full frame.\n"
            "SAFE     : returns 0 BEFORE any SEH/local frame is installed; ecx=this preserved;\n"
            "           proceed path replays the exact prologue and reconverges at 0x9272A5. The\n"
            "           data slot is zero in the produced exe, so the first call resets the window\n"
            "           and PROCEEDS (no startup stall); it is a pure sliding window needing no\n"
            "           zone reset, and the unsigned sub/jb makes the 49.7-day tick wrap safe.\n"
            "RISK     : MODERATE, not low-moderate. Converts a big hitch into a trickle, but see\n"
            "           the retry premise above: on the event-driven and UI-preview routes a\n"
            "           denied load may never be retried. Do not enable without watching for\n"
            "           missing weapons/armour on freshly spawned or job-changed actors."
        ),
        apply=lambda d: apply_deferred_load(d),
    ),

    # =======================================================================
    # Documented DEAD-ENDS (kept so we don't re-try them):
    #
    # * newhandler_notrap / CF3 -- NOP the `int 3` in the new-failure handler
    #   (sub_4751B0, int3 @ 0x4751B3). REJECTED: sub_4751B0 returns arg_0 (non-zero)
    #   to operator new = 'retry the allocation'; without the trap an OOM becomes an
    #   INFINITE retry loop (hang) since nothing frees memory. `laa` stops new from
    #   failing at all, so this trap never fires -> unnecessary AND harmful.
    #     patch_bytes(d, "558BECCC8B45085DC3", "558BEC908B45085DC3")
    #
    # * CF6 (EquipmentUI NULL skill-data, 0x7A356B) -- the b303 inline patch adds
    #   `test ebx,ebx; jz 0x7A37BE; lea eax,[ebx+8]`, but 0x7A37BE itself does
    #   `mov [ebx-4],cx` -- it re-dereferences the very pointer being null-guarded,
    #   so on null ebx it still crashes (just relocated). Not ported until the skip
    #   target is re-derived to a site that does not touch ebx.
    #
    # NOT PORTABLE inline (need a new .patch PE section / code cave -> out of scope
    # for this DLL-free inline patcher): CF1/CF4/CF5 NULL-`this` guards, the
    # Carmack fast-math thunks (FAST_*, _CIsqrt/_CIsin/_CIcos), RADIUS/CAMERA cull,
    # DIST_EQUIP/DEFERRED equipment loaders, and the equip/icon preloaders. See
    # wiki/client/optimization-patches.md.
    #
    # MEMORY leads evaluated and NOT shipped (see docs/memory-analysis.md):
    # * The BIG win -- removing the D3DPOOL_MANAGED system-RAM shadow on the bulk of
    #   world/model textures -- is NOT inline-patchable: sub_BEE0B0 (in NiDX9Source
    #   TextureData::LoadFromFile) calls the non-Ex D3DXCreateTextureFromFileInMemory,
    #   which has no Pool argument (MANAGED hard-wired in d3dx9_42.dll). Needs a D3D IAT
    #   hook / call redirect -> belongs in the mod DLL, not this patcher.
    # * pe SizeOfStackReserve 1MiB->512KiB (file 0x178): reclaims ~512KiB VA per default-
    #   stack thread, but risks a stack-overflow crash on deep engine call paths -> needs
    #   a runtime peak-stack measurement first; not shipped.
    # * EvictManagedResources() on zone load: needs a cave, and it frees only VRAM (the
    #   D3D runtime keeps the system-RAM backing), so it does NOT relieve the 2GB address
    #   space -> mod-DLL VRAM-hygiene at best, not shipped here.
    # * 16bpp texture-format cuts (A8R8G8B8 -> A4R4G4B4) exist on a few small UI textures
    #   (safe, MANAGED) but save only a few hundred KB; the broader UI path can BACKFIRE
    #   (forces FROM_FILE DXT sources to decompress) -> not shipped.
    #
    # Efficiency leads evaluated and NOT shipped:
    # * D3DCREATE_FPU_PRESERVE off (`or ecx,2` @0xBAE76E): NOT quality-neutral -- it drops the
    #   process-wide x87 FPU control word to 24-bit precision, changing all float math (camera/
    #   anim/physics) with ~zero FPS upside. Keep FPU_PRESERVE.
    # * Redundant singleton-getter re-calls in Render_GameWorld (0x821B51) / Render_GameActors
    #   (0x822BC2): strictly safe but ~1 call/frame -- negligible, not worth catalog noise.
    # * Per-frame D3D state changes are ALREADY state-change-filtered (CRenderStateMgr::Set*
    #   compare-before-set), and debug render passes already early-out -- no clean neutral CPU
    #   redundancy left.
    #   (This line used to end "the big neutral wins are the shipped HWVP/HWSKIN/no_particle_sort".
    #    Corrected 2026-08: none of those three is shipped -- all are enabled=False -- and none is
    #    a neutral CPU win. hw_vertex_proc and hw_vp_puredevice patch a fallback rung that never
    #    executes on a working client; hw_skinning suppresses a capability rejection in the
    #    fixed-function shader; and no_particle_sort was renamed no_khara_list_sort because it
    #    sorts a UI list, not particles. The real shipped neutral wins are no_frame_sleep and
    #    inline_singleton_getters.)
    # * NiSmartPointer LOCK-prefixed AddRef/Release are wasted on the single-threaded render
    #   loop but the refcount fn-pointers are shared with async loader threads (correctness
    #   risk) and are indirect calls (no cheap inline swap) -> mod-DLL territory.
    #
    # Analyzed & ready to port (not yet shipped, cave-based):
    # * DEFERRED_LOAD (EQ-DEFER-01): rate-limit the sole main-thread synchronous asset load,
    #   CActorMotion::LoadEquipmentModels @0x9272A0 (-> CNifLoader::LoadNifFile -> NiStream::
    #   LoadFromFile). A GetTickCount-gated cave at the entry (return 0 == 'not ready, retry'
    #   when over budget) spreads a crowd-entry NIF stampede over frames -> less stutter. The
    #   model-STREAMING path (UpdateObjectsInRange/LoadModelData) is already async (PPL), so
    #   equipment is the only sync loader worth deferring.
    # * CAM-PITCH: mouse-look PITCH is smoothed at 0.25 (vs yaw 0.5). No isolated inline edit
    #   (the ctor fans one 0.25 fld out to 3 factors); needs a small cave to set camera+152 =
    #   0.75 after the ctor init, to match camera_snappy_look on both axes.
    # * Zone-teardown leak (#2): the dedicated lens crashed mid-run (unverified). Re-check
    #   whether CZoneData::InitializeAndLoadZone frees the old zone before LoadZoneData;
    #   cache_retention_30s already bounds much of the session-long high-water regardless.
    #
    # D3D leads evaluated and NOT shipped:
    # * Gamma/brightness (SetGammaRamp): the binary imports NO gamma API -> not possible.
    # * MSAA (D3DMULTISAMPLE): high risk -- the depth/RT setup isn't multisample-typed; would
    #   need a cave + matching depth surface; excluded.
    # * Drop LOCKABLE_BACKBUFFER flag (0xBB050E): a small perf win, but the client requests
    #   LOCKABLE only in the no-MSAA case, implying it may Lock() the backbuffer (screenshots)
    #   -> could break readback; not shipped without confirming no lock path.
    # * D3DXCreateEffect compile flags: the Flags arg is caller-supplied, not a constant.
    # * LOD-bias (D3DSAMP_MIPMAPLODBIAS): a small negative bias sharpens textures but risks
    #   shimmer/moire; prefer anisotropic_filtering. Portable as an extra cave line if wanted.
    # * The modern shader scene's sampler filters live in the .fx effect DATA, not the exe;
    #   forcing AF there needs a DXVK config (d3d9.samplerAnisotropy) or a global 0x45F740 hook.
    #
    # Optimization leads analyzed & ready to port (not yet shipped, cave-based):
    # * Additive frustum/behind-camera cull cave at SceneManager_Cull_And_Render (~0x80736C): the
    #   per-object loop has NO frustum test, so side/behind objects within fog-far are submitted
    #   every frame -- a cave dot-product cull would cut them ZERO-quality (unlike reduce_object_
    #   draw_distance which pops). Correctness hinges on sourcing the camera basis; medium risk.
    # * CPU caves: hoist the per-actor camera-position recompute out of the Render_GameWorld /
    #   Render_GameActors loops (invariant per frame, C1/C4), and square-distance the per-object
    #   fsqrt range tests (C3). Output-identical but each needs a per-loop cave rewrite.
    # * CAMERA hemisphere cull (catalog): a BEHAVIOUR change (replaces the fog-far distance cull),
    #   mutually exclusive with reduce_object_draw_distance -- not shipped.

    # ---- ported from the ro2-client-mods applier copy (2026-08-04 consolidation) ----
    # These six existed only in that copy. effect_const_null_getter is referenced by the
    # enabled-by-default exe-crash-stability mod, so dropping it would silently weaken it.

    Fix(
        id="effect_const_null_getter",
        module="stability",
        enabled=False,   # SUPERSEDED: minidump_null_guards_2026_08 already patches VA 0xBA13A0.
        #                 Applying both fails -- the second finds the first's jmp instead of
        #                 stock bytes. Kept defined so older mods.toml ids still resolve.
        title="NiD3DXEffectShader::ProcessShaderConstant: null-safe the constant-source getter (crash guard)",
        why=(
            "SYMPTOM  : crash in the render thread while applying an effect's shader constants\n"
            "           (Render_GameWorld -> NiD3DXEffectShader::ProcessShaderConstant @0xB9CE18).\n"
            "ROOT     : sub_BA13A0 is a generic accessor `return this[6]` (this+0x18). In the\n"
            "           ProcessShaderConstant semantic switch (this[22]), cases 8 and 0xB deref\n"
            "           the constant's SOURCE object (a2[2]) through this getter WITHOUT a null\n"
            "           check. When the source is null -- e.g. a shader samples a texture/resource\n"
            "           the rendered object does not provide, like an EnvironmentMap on a\n"
            "           non-reflective object -- `mov eax,[eax+0x18]` reads address 0x18 -> AV.\n"
            "FIX      : make the getter null-safe: `if(this) return this[6]; else return 0`. The\n"
            "           downstream `if(v85)` then just skips the unset constant, harmless. A\n"
            "           same-length 17-byte rewrite (test ecx,ecx; jz +ret0; mov eax,[ecx+18];\n"
            "           ret; xor eax,eax; ret + NOP pad). The 9 other callers pass a valid this,\n"
            "           so they behave exactly as before.\n"
            "EVIDENCE : live crash EIP 0xBA13AA `mov eax,[eax+0x18]` with eax(this)=0; caller\n"
            "           0xB9CE18 case 8; live-patched the getter and the assembly verified clean.\n"
            "RISK     : low. A null constant-source now yields 'no value' instead of a crash."
        ),
        apply=lambda d: patch_at_va(d, 0xBA13A0, "558bec51894dfc8b45fc8b40188be55dc3",
                                       "85c974048b4118c333c0c3909090909090"),
    ),
    Fix(
        id="no_particle_sort",
        module="performance",
        enabled=False,
        title="Skip particle depth sort (ParticleData::Compare -> 'equal')",
        why=(
            "WHAT     : ParticleData::Compare (0x408D30) returns 0 -> the sort sees\n"
            "           every pair as equal, so no swaps happen (CPU sort skipped).\n"
            "RET FORM : __cdecl(a1, a2) comparator -> caller cleans args -> bare `ret`.\n"
            "           Return `xor eax,eax; ret`.\n"
            "RISK     : low-med. Saves CPU on heavy particle scenes; may cause minor\n"
            "           transparency ordering artifacts in dense effects."
        ),
        apply=lambda d: patch_bytes(d, "558beca16ce55a01", "33c0c3a16ce55a01"),
    ),
    Fix(
        id="no_terrain_shadow",
        module="performance",
        enabled=False,
        title="Disable terrain shadow map (Render_TerrainShadowMap -> return 0)",
        why=(
            "WHAT     : Render_TerrainShadowMap (0x813DF0) returns 0 at entry.\n"
            "RET FORM : __thiscall(this, a2); real epilogue `pop ebp; retn 4`. Return\n"
            "           `xor eax,eax; retn 4`.\n"
            "EVIDENCE : disasm 0x813DF0 -> `pop ebp; retn 4`. 25-byte anchor to\n"
            "           disambiguate from no_water_reflect (identical prologue).\n"
            "RISK     : low (visual: terrain self-shadowing gone)."
        ),
        apply=lambda d: patch_bytes(d,
            "558bec8b414c85c074128b80f001000085c074088bc85de974",
            "33c0c204004c85c074128b80f001000085c074088bc85de974"),
    ),
    Fix(
        id="no_water_reflect",
        module="performance",
        enabled=False,
        title="Disable water reflection pass (Render_WaterReflection -> return 0)",
        why=(
            "WHAT     : Render_WaterReflection (0x813E10) returns 0 at entry, skipping\n"
            "           the reflection render. FPS win near water.\n"
            "RET FORM : __thiscall(this, a2); real epilogue `pop ebp; retn 4`. Return\n"
            "           `xor eax,eax; retn 4` -- 0 is exactly the value it returns when\n"
            "           the reflection target is absent, so callers already handle it.\n"
            "EVIDENCE : disasm 0x813E10 -> ...`pop ebp; retn 4`. Shares its 6-byte\n"
            "           prologue with no_terrain_shadow, so the anchor is 25 B to be\n"
            "           unique (they differ only in the trailing jmp displacement).\n"
            "RISK     : low (visual: flat water)."
        ),
        apply=lambda d: patch_bytes(d,
            "558bec8b414c85c074128b80f001000085c074088bc85de994",
            "33c0c204004c85c074128b80f001000085c074088bc85de994"),
    ),
    Fix(
        id="target_limit_base",
        module="gameplay",
        enabled=False,
        title="Raise base target limit 10 -> 50 (CPlayer target limit)",
        why=(
            "WHAT     : at 0x4DA4DB `mov esi,0Ah` sets the default max-target count.\n"
            "           Change the immediate 0Ah->32h (10 -> 50).\n"
            "EVIDENCE : disasm 0x4DA4DB = `mov esi,0Ah; call GetPlayer; cmp lvl,1Eh...`.\n"
            "RISK     : gameplay change; pairs with target_limit_high (level 30+ path)."
        ),
        apply=lambda d: patch_bytes(d, "be0a000000e85bed", "be32000000e85bed"),
    ),
    Fix(
        id="target_limit_high",
        module="gameplay",
        enabled=False,
        title="Raise level-30+ target limit 20 -> 100",
        why=(
            "WHAT     : at 0x4DA4F4 `mov esi,14h` sets the level-30+ max-target count.\n"
            "           Change 14h->64h (20 -> 100).\n"
            "EVIDENCE : reached via `cmp [player+1D8h],1Eh; jl` just above 0x4DA4F4.\n"
            "RISK     : gameplay change. Enable together with target_limit_base."
        ),
        apply=lambda d: patch_bytes(d, "be140000008bc65ec3cccccc8b4154",
                                       "be640000008bc65ec3cccccc8b4154"),
    ),
]


# ---------------------------------------------------------------------------
def _load(path):
    with open(path, "rb") as f:
        return bytearray(f.read())


def main():
    # Default paths point at the b303-2022-02-11-Optimizations client relative to this
    # repo (../clients/...). Override with --in/--out for any other client build.
    _client = "../clients/ClientArchive/b303-2022-02-11-Optimizations/SHIPPING"
    ap = argparse.ArgumentParser(description="RO2 modular client fix patcher")
    ap.add_argument("--in", dest="inp", default=f"{_client}/Rag2.exe", help="input exe (untouched)")
    ap.add_argument("--out", default=f"{_client}/ro2-fixed.exe", help="patched output exe")
    ap.add_argument("--only", default=None, help="comma-separated fix ids to apply")
    ap.add_argument("--list", action="store_true", help="print the fix catalog and exit")
    a = ap.parse_args()
    only = set(s.strip() for s in a.only.split(",")) if a.only else None

    def selected(fx):
        # `--only <ids>` applies exactly those fixes, even if enabled=False (so opt-in
        # fixes can be tested without editing the file); otherwise apply all enabled.
        if only is not None:
            return fx.id in only
        return fx.enabled

    if a.list:
        print("RO2 client fix catalog  ([x]=would apply, [ ]=off)\n")
        for fx in FIXES:
            print(f"[{'x' if selected(fx) else ' '}] {fx.id}   <{fx.module}>   {fx.title}")
            for line in fx.why.splitlines():
                print(f"        {line}")
            print()
        return

    if not os.path.exists(a.inp):
        sys.exit(f"input not found: {a.inp}")

    data = _load(a.inp)
    applied, skipped, failed = [], [], []
    print(f"Patching a copy of {a.inp} ...\n")
    for fx in FIXES:
        if not selected(fx):
            continue
        res, msg = fx.apply(data)
        head = f"{fx.id} <{fx.module}>  {fx.title}"
        if res is True:
            applied.append(fx); print(f"  [APPLIED] {head}\n            {msg}")
        elif res is False:
            skipped.append(fx); print(f"  [SKIP   ] {head}\n            {msg}")
        else:
            failed.append(fx); print(f"  [FAIL   ] {head}\n            {msg}")

    if failed:
        sys.exit(f"\n{len(failed)} fix(es) FAILED -- {a.out} NOT written (nothing corrupted).")
    if not applied and not skipped:
        sys.exit("no fixes selected (see --list).")

    with open(a.out, "wb") as f:
        f.write(data)
    orig = _load(a.inp)
    diffs = [i for i in range(min(len(orig), len(data))) if orig[i] != data[i]]
    print(f"\nWrote {a.out}  ({len(data)} bytes; {len(diffs)} byte(s) changed"
          f"{' @ ' + ', '.join(hex(x) for x in diffs) if len(diffs) <= 12 else ''})")
    print(f"Applied: {len(applied)}   Skipped(already-applied): {len(skipped)}")
    print("Original left untouched.  Launch ro2-fixed.exe in place of Rag2.exe to test.")


if __name__ == "__main__":
    main()
