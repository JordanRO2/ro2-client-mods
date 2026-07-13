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
PATCH_SECTION_SIZE = 0x00000400   # room for several caves; one shared section

# Fixed slot map inside the `.patch` section, so multiple cave fixes coexist. Each cave
# lives at PATCH_SECTION_VA + its offset; keep slots comfortably apart.
PATCH_OFF_TEXPOOL_2D = 0x000      # world/model 2D texture -> DEFAULT-pool wrapper


def _has_patch_section(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    n = struct.unpack_from("<H", data, e + 6)[0]
    base = e + 24 + struct.unpack_from("<H", data, e + 20)[0]
    return any(data[base + k * 40: base + k * 40 + 6] == b".patch" for k in range(n))


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
# valid path. All verified: bytes match the binary, the 10-byte idiom is the anchor, the jmp
# reconverges to a clean epilogue/cleanup boundary (medium-risk sites that land mid-function were
# dropped). (va, find[10-byte idiom], repl[jmp + dead tail], same length.)
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
    (0x53DC59, "33c98b118b92f8050000", "e90b00000092f8050000"),
    (0x53D9F5, "33c98b018b80f4010000", "eb0b8b018b80f4010000"),
    (0x53DA69, "33c98b018b8024040000", "eb0b8b018b8024040000"),
    (0x53DADD, "33c98b018b802c040000", "eb0b8b018b802c040000"),
    (0x53DB51, "33c98b018b8038040000", "eb0b8b018b8038040000"),
    (0x53DBC5, "33c98b018b8030040000", "eb0b8b018b8030040000"),
    (0x53DC30, "33c98b018b80f4050000", "eb0b8b018b80f4050000"),
]


def apply_rmi_nullvcall_guards(data):
    """Apply the 28 sibling null-vtable-call guards all-or-nothing (on a trial copy first, so a
    binary mismatch aborts cleanly without a half-applied group)."""
    trial = bytearray(data)
    applied = skipped = 0
    failed = []
    for va, fh, rh in _NULLVCALL_GUARDS:
        r, m = patch_at_va(trial, va, fh, rh)
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
                         #   0.1 => keep within ~72 deg half-angle of the view axis (a ~143 deg
                         #   cone) -- conservative side-cull, comfortably WIDER than the view
                         #   frustum so on-screen objects are not clipped. Forward-sign already
                         #   confirmed live (behind-cull worked). Tighten toward the FOV for more
                         #   FPS only if no screen-edge pop-in appears: 0.25 (~60 deg) / 0.5 (~45).


def _rel32(frm_after, to):
    return struct.pack("<i", to - frm_after)


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
        (PATCH_OFF_CF1, _cf1_cave, 0x90B447, "8b401450e9e5000000", 9),
        (PATCH_OFF_CF4, _cf4_cave, 0x413210, "558bec8b4508",       6),
        (PATCH_OFF_CF5, _cf5_cave, 0x8FD2E7, "8bb180010000",       6),
    ]
    for slot, builder, hook_va, orig_hex, hook_len in caves:
        cave_va = PATCH_SECTION_VA + slot
        cave = builder(cave_va)
        data[sec_off + slot: sec_off + slot + len(cave)] = cave
        jrel = struct.pack("<i", cave_va - (hook_va + 5))
        new = "e9" + jrel.hex() + "90" * (hook_len - 5)     # jmp cave + NOP pad
        r, m = patch_at_va(data, hook_va, orig_hex, new)
        if r is not True:
            return None, f"CF hook @0x{hook_va:X} failed: {m}"
    return True, "CF1/CF4/CF5 null-guard caves installed (.patch)"


def _aniso_cave(cave_va):
    # Injected after sub_DDC350's MIP-filter SetSamplerState: complete that call, then set
    # D3DSAMP_MAXANISOTROPY=16 for the same stage, then rejoin the epilogue. this =
    # CRenderStateMgr @ ds:0x15C1D64; SetSamplerState @0x45F740 (retn 0Ch, callee-clean).
    b = bytearray()
    b += bytes.fromhex("8b0d641d5c01")                       # mov ecx,[15C1D64]  (displaced MIP this-load)
    b += b"\xE8" + _rel32(cave_va + len(b) + 5, 0x45F740)    # call SetSamplerState  (finish MIP call)
    b += bytes.fromhex("6a10")                               # push 16   (MaxAnisotropy)
    b += bytes.fromhex("6a03")                               # push 3    (D3DSAMP_MAXANISOTROPY)
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
    clamp it to LINEAR harmlessly."""
    try:
        h = _rva_to_offset(data, 0xDDC426 - IMAGEBASE)
    except ValueError as ex:
        return None, str(ex)
    if data[h] == 0xE9:
        return False, "already applied (aniso hook present)"
    r1, m1 = patch_at_va(data, 0xDDC3C5, "c745f402000000", "c745f403000000")   # LINEAR -> ANISOTROPIC
    if r1 is None:
        return None, f"filter const: {m1}"
    sec_off, msg = _ensure_patch_section(data)
    if sec_off is None:
        return None, msg
    cave_va = PATCH_SECTION_VA + PATCH_OFF_ANISO
    cave = _aniso_cave(cave_va)
    data[sec_off + PATCH_OFF_ANISO: sec_off + PATCH_OFF_ANISO + len(cave)] = cave
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
            "           skips the null send and reconverges with the valid path (which jumps\n"
            "           in independently). Applied as a verified group, all-or-nothing.\n"
            "EVIDENCE : all 28 byte-verified against the image; reconvergence targets sampled\n"
            "           in IDA land on clean epilogue/cleanup boundaries with balanced ESP.\n"
            "           4 medium-risk sites that reconverged mid-function/loop were dropped.\n"
            "RISK     : low. Only fires on the null-singleton path (which crashed before);\n"
            "           the request silently no-ops."
        ),
        apply=lambda d: apply_rmi_nullvcall_guards(d),
    ),

    Fix(
        id="effect_const_null_getter",
        module="stability",
        enabled=True,
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
        id="cf_null_guards",
        module="stability",
        enabled=False,
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
            "           disassemble correctly. These are the proven catalog crash fixes\n"
            "           (patch_all.py ships them by default).\n"
            "RISK     : low (only the null path changes). Cave-based -> off by default to\n"
            "           keep the default build section-free; enable for the extra coverage."
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
        id="no_shadow_map",
        module="performance",
        enabled=False,
        title="Disable shadow map render (Render_ShadowMap -> return 0)  [big FPS]",
        why=(
            "WHAT     : Render_ShadowMap (0x814350) returns 0 at entry -> skips all\n"
            "           dynamic shadow-map rendering. Largest single FPS lever here.\n"
            "RET FORM : __thiscall(this, a2); real epilogue `pop ebp; retn 4`. Return\n"
            "           `xor eax,eax; retn 4`.\n"
            "EVIDENCE : 20-byte fn; disasm 0x814350 -> `pop ebp; retn 4`.\n"
            "RISK     : med (visual: no character/object shadows at all)."
        ),
        apply=lambda d: patch_bytes(d, "558bec8b492c85c974065de960",
                                       "33c0c204002c85c974065de960"),
    ),
    Fix(
        id="no_char_shadow",
        module="performance",
        enabled=False,
        title="Skip per-actor shadow NIF load (CGameActor::LoadCharacterShadow)",
        why=(
            "WHAT     : CGameActor::LoadCharacterShadow (0x8F6C50) returns at entry,\n"
            "           skipping the per-actor char_shadow.nif load.\n"
            "RET FORM : __thiscall(this) with NO stack args -> bare `ret` (verified:\n"
            "           real epilogue `mov esp,ebp; pop ebp; ret` at fn end). Return\n"
            "           `xor eax,eax; ret`.\n"
            "RISK     : low. From a clean start no shadow is ever loaded, so the\n"
            "           existing-shadow release it also skips never has work to do."
        ),
        apply=lambda d: patch_bytes(d, "558bec5153568bf1578d", "33c0c35153568bf1578d"),
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
        id="no_terrain_decals",
        module="performance",
        enabled=False,
        title="Disable terrain projected decals (CTerrain::RenderDecals skip path)",
        why=(
            "WHAT     : at 0x814237 `CTerrain::RenderDecals` tests its decal-enabled flag\n"
            "           (`cmp [ecx+3Ch],0`) and `jz` past the decal draw. Flip that `jz`\n"
            "           (74) to `jmp` (EB), same rel8 (0x29), so the decal renderer\n"
            "           (sub_809770) is never called -- ground-projected decals off.\n"
            "EVIDENCE : disasm 0x814233 = `cmp [ecx+3Ch],0; jz loc_814262`. Both paths\n"
            "           converge on one `pop ebp; retn 24h` epilogue (9 stack args), so\n"
            "           forcing the jump is stack-correct; the sole caller ignores eax.\n"
            "           This reuses the game's own decals-off branch.\n"
            "RISK     : low (cosmetic: no ground decals/blob marks)."
        ),
        apply=lambda d: patch_bytes(d, "74298b4528d94524", "eb298b4528d94524"),
    ),

    # ----- GPU (opt-in) ---------------------------------------------------
    Fix(
        id="hw_vertex_proc",
        module="gpu",
        enabled=False,
        title="Force hardware vertex processing (SOFTWARE_VP -> HARDWARE_VP)",
        why=(
            "WHAT     : at 0xBB0E6C the device-behavior flags are built with\n"
            "           `or ecx,20h` (D3DCREATE_SOFTWARE_VERTEXPROCESSING). Flip the\n"
            "           immediate to 40h (HARDWARE) so vertex work runs on the GPU.\n"
            "EVIDENCE : disasm 0x BB0E6C = `or ecx,20h; mov [arg8],ecx`.\n"
            "RISK     : low-med. On a GPU without HW VP support device creation could\n"
            "           fail; any modern GPU is fine. Original left untouched to revert."
        ),
        apply=lambda d: patch_bytes(d, "83c9208b5510890aeb4e", "83c9408b5510890aeb4e"),
    ),
    Fix(
        id="hw_skinning",
        module="gpu",
        enabled=False,
        title="Enable hardware skinning (bypass conservative <=4 bone check)",
        why=(
            "WHAT     : at 0xBDCCD3 a `jbe` skips the GPU-skin path when a partition\n"
            "           has <=4 bones. Change `jbe`->`jmp` to always take that path.\n"
            "EVIDENCE : disasm 0xBDCCD3 = `jbe short +3Bh` before the software/error\n"
            "           branch; b303 catalog notes 'tested and working'.\n"
            "RISK     : med. Relies on the bone-palette shader handling >4 bones;\n"
            "           opt-in and reversible."
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
            "EVIDENCE : disasm 0x814D53 = `ja default_case` above `jmp jpt[eax*4]`.\n"
            "RISK     : med. It forces a switch default; visually removes distance fog.\n"
            "           Enable and eyeball -- purely cosmetic, reversible."
        ),
        apply=lambda d: patch_bytes(d, "7727ff2485384e81", "eb27ff2485384e81"),
    ),
    Fix(
        id="max_lod_far",
        module="visual",
        enabled=False,
        title="Keep high-quality shader at far LOD (don't downgrade)",
        why=(
            "WHAT     : at 0x4611CE a `jnp` after an fcomp branches to the low-quality\n"
            "           far-shader assignment. NOP it (`90 90`) to fall through and\n"
            "           keep the high-quality shader at distance. Pairs with max_lod_mid.\n"
            "EVIDENCE : disasm 0x4611CE = `jnp; fcomp st(1); fnstsw; test ah,41h`.\n"
            "RISK     : low-med (a bit more GPU cost far away)."
        ),
        apply=lambda d: patch_bytes(d, "7b1dd8d9dfe0f6c4", "9090d8d9dfe0f6c4"),
    ),
    Fix(
        id="max_lod_mid",
        module="visual",
        enabled=False,
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
    Fix(
        id="enchant_no_camdist",
        module="gameplay",
        enabled=False,
        title="Remove enchant-UI camera-distance gate (UIFXEnchantWnd)",
        why=(
            "WHAT     : at 0x44B65F a `jnz` disables the enchant window when the camera\n"
            "           is farther than a threshold. NOP it (`90 90`) so it never\n"
            "           disables on distance.\n"
            "EVIDENCE : disasm 0x44B65F = `jnz; mov edx,[esi]; mov eax,[edx+0Ch]...`.\n"
            "           `jnz` is very common, so the anchor runs to 39 B (through the\n"
            "           function tail + alignment) to be unique -- a build-specific but\n"
            "           self-verifying pattern (mismatch => refuses, never corrupts).\n"
            "RISK     : low (niche UI convenience)."
        ),
        apply=lambda d: patch_bytes(d,
            "750b8b168b420c6a008bceffd05e8be55dc3cccccccccccccccccccccccccccccc558bec83ec68",
            "90908b168b420c6a008bceffd05e8be55dc3cccccccccccccccccccccccccccccc558bec83ec68"),
    ),

    # ----- STARTUP / AUDIO (opt-in) ---------------------------------------
    Fix(
        id="skip_opening_movie",
        module="startup",
        enabled=False,
        title="Skip the opening-movie popup on char-select entry",
        why=(
            "WHAT     : at 0x639D27 (CStageCharSelect OnEnter) a 5-byte `call\n"
            "           CheckOpeningMovieConfig` auto-opens the opening-movie UI window\n"
            "           (GetOrCreateWindow id 0x73) on first entry. NOP the call\n"
            "           (E8 rel32 -> 90 90 90 90 90).\n"
            "EVIDENCE : disasm 0x639D27 = `call CheckOpeningMovieConfig; mov eax,1; pop\n"
            "           esi; ret` -- the callee's return is immediately overwritten by\n"
            "           `mov eax,1`, and the callee's only effect is opening that window,\n"
            "           so NOPing the call is control-flow-safe.\n"
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
    # ***HIGH RISK*** DEFAULT-pool textures are LOST on a D3D device reset (exclusive-
    # fullscreen alt-tab, resolution / display change) and are NOT auto-restored (MANAGED
    # is). These UI textures are cached as raw IDirect3DTexture9* with no observed
    # recreation handler -> after a reset the cached pointer can dangle -> missing UI or a
    # crash on the next draw. Lower risk in windowed / borderless (no device reset).
    # Benefit is MODEST (UI/localized textures only, ~tens of MB, content-dependent).
    # NOTE: the BIG lever (the world/model texture creator sub_BEE0B0) is NOT inline-
    # patchable -- it calls the non-Ex D3DXCreateTextureFromFileInMemory which has no Pool
    # argument (MANAGED is hard-wired in d3dx9_42.dll); removing that shadow needs a D3D
    # call redirect / IAT hook and belongs in the mod DLL, not this inline patcher.
    # See docs/memory-analysis.md. Enable these only to experiment, ideally windowed.
    Fix(
        id="tex_pool_ui_inmem",
        module="memory",
        enabled=False,
        title="UI textures MANAGED->DEFAULT (in-memory path) -- drop RAM shadow [risky]",
        why=(
            "WHAT     : sub_1084400 UI/localized texture loader, in-memory (VDK) path ->\n"
            "           D3DXCreateTextureFromFileInMemoryEx. Pool `push 1` (MANAGED) at\n"
            "           0x108472C -> `push 0` (DEFAULT), removing the system-RAM shadow.\n"
            "MEM      : reclaims each such texture's size from the 32-bit address space.\n"
            "RISK     : HIGH -- device-lost (see section header). Enable windowed only.\n"
            "EVIDENCE : disasm of the 15-arg push block; Pool is the constant 6a01. Byte\n"
            "           window not unique -> VA-anchored patch."
        ),
        apply=lambda d: patch_at_va(d, 0x108471F,
            "8d8d40fdffff516a006a016a016a016a", "8d8d40fdffff516a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_ui_disk",
        module="memory",
        enabled=False,
        title="UI textures MANAGED->DEFAULT (disk fallback path) [risky]",
        why=(
            "WHAT     : same loader (sub_1084400), disk fallback -> D3DXCreateTexture\n"
            "           FromFileExW; Pool `push 1`->`push 0` at 0x1084794. Pair with\n"
            "           tex_pool_ui_inmem so both load paths agree.\n"
            "RISK     : HIGH -- device-lost. Window identical to the in-memory site, so\n"
            "           VA-anchored."
        ),
        apply=lambda d: patch_at_va(d, 0x1084787,
            "8d8d40fdffff516a006a016a016a016a", "8d8d40fdffff516a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_loc_inmem",
        module="memory",
        enabled=False,
        title="Localized textures MANAGED->DEFAULT (CLocalization loader) [risky]",
        why=(
            "WHAT     : CLocalization::LoadLocalizedTexture in-memory create; Pool\n"
            "           `push 1`->`push 0` at 0x1083F67.\n"
            "RISK     : HIGH -- device-lost; localized textures also reload on language\n"
            "           change. VA-anchored (non-unique window)."
        ),
        apply=lambda d: patch_at_va(d, 0x1083F5A,
            "8d8d40feffff516a006a016a016a016a", "8d8d40feffff516a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_loc_global",
        module="memory",
        enabled=False,
        title="Localized cached texture MANAGED->DEFAULT (global unk_15C9134) [risky]",
        why=(
            "WHAT     : sub_1086340 localized loader (A8R8G8B8, single global pointer);\n"
            "           Pool `push 1`->`push 0` at 0x108636B.\n"
            "RISK     : HIGH -- device-lost; the texture lives in one global with no\n"
            "           observed reset-time recreation. VA-anchored."
        ),
        apply=lambda d: patch_at_va(d, 0x108635C,
            "6834915c016a006a006a006a016a016a016a", "6834915c016a006a006a006a016a016a006a"),
    ),
    Fix(
        id="tex_pool_shader_effect",
        module="memory",
        enabled=False,
        title="Effect/material textures MANAGED->DEFAULT (NiD3DXEffectShader::LoadTexture2D)",
        why=(
            "WHAT     : NiD3DXEffectShader::LoadTexture2D (0xBA0DD0) loads the textures\n"
            "           referenced by .fx effects/materials (diffuse/normal/specular/\n"
            "           lookup/ramp) via D3DXCreateTextureFromFileExA with Pool `push 1`\n"
            "           (MANAGED) at 0xBA0DFE. Flip to `push 0` (DEFAULT), dropping the\n"
            "           system-RAM shadow.\n"
            "MEM      : the largest still-MANAGED texture site after the bulk world path\n"
            "           (already handled by texpool_world_default). Reclaims each effect\n"
            "           texture's size from the address space.\n"
            "EVIDENCE : disasm 0xBA0DF0 shows the 14-arg push block; Pool=`6a01`@0xBA0DFE,\n"
            "           Usage=`6a00`@0xBA0E02 (=0 -> static, safe). VA-anchored (the\n"
            "           `6a01` window is not unique).\n"
            "RISK     : HIGH (device-reset: DEFAULT textures lost on fullscreen alt-tab /\n"
            "           resolution change; effect textures reload when the effect does).\n"
            "           Safest windowed / under DXVK (device-lost never signalled)."
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
            "           (no RAM shadow), mirroring every other non-Ex default. Roughly\n"
            "           HALVES the texture footprint in the address space -- the biggest\n"
            "           single memory lever found, and the direct partner to `laa`.\n"
            "EVIDENCE : sub_BEE0B0 decompiles to D3DXCreateTextureFromFileInMemory(dev,\n"
            "           buf, size, &tex) (case 3); the *Ex thunk exists at 0x1182F40; the\n"
            "           non-Ex thunk has 2 callers, both texture loaders.\n"
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
            "           Still runtime-untested -- validate alt-tab / resolution change /\n"
            "           crowded zones. Near-free under DXVK (no device reset)."
        ),
        apply=lambda d: apply_geo_pool_default(d),
    ),
    Fix(
        id="cache_retention_30s",
        module="memory",
        enabled=False,
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
    # Modern PCs leave the client's 2013-era budgets unused. These spend the abundant
    # VRAM (all address-space-safe: they touch DEFAULT-pool / render-target video memory,
    # not the 32-bit system-RAM shadow the OOM cares about). Best paired with the pool
    # flips above (which move the working set into VRAM in the first place).
    Fix(
        id="tex_force_max_detail",
        module="quality",
        enabled=False,
        title="Force max texture detail (mip-levels-to-skip = 0, full-resolution textures)",
        why=(
            "WHAT     : the Gamebryo 'mipmap levels to skip' global (word_15BD008, the\n"
            "           in-game Texture-Detail setting) makes CreateSurfFromRendererData\n"
            "           drop the top v8 = min(skip, mipCount-1) full-res mip levels of\n"
            "           every texture. Its only writer is the setter sub_BBA9A0. Force it\n"
            "           to store 0 -> no mips skipped -> full-resolution textures resident.\n"
            "HOW      : at 0xBBA9A3 `mov eax,[ebp+arg_0]` -> `xor eax,eax; nop`, so the\n"
            "           following `mov word_15BD008, eax` writes 0 whatever the config\n"
            "           slider says. (VA-anchored; the `mov` window includes the global.)\n"
            "MEM      : VRAM only (textures are DEFAULT pool after the flips above) —\n"
            "           address-space-safe. No change if detail is already maxed.\n"
            "RISK     : low. Pins Texture-Detail to max (overrides the slider); costs VRAM\n"
            "           + a little texture-load time."
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
            "RISK     : low. Sharper/less-aliased shadows; small GPU fill cost in the shadow\n"
            "           pass. a3=0 at the sole call site, so only the primary RT is made."
        ),
        apply=lambda d: patch_at_va(d, 0x815C91,
            "6a0a50e8b7d99100", "6a0b50e8b7d99100"),
    ),
    Fix(
        id="shadowmap_4096",
        module="quality",
        enabled=False,
        title="Shadow-map resolution 1024 -> 4096 (sharpest real-time shadows)",
        why=(
            "WHAT     : same lever as shadowmap_2048 -- the whole-scene real-time shadow map\n"
            "           is a single 2^N render target (N = literal `push 0Ah` = 10 -> 1024)\n"
            "           passed to the shadow-RT factory sub_1133650. Bump N to 12 -> 4096x4096\n"
            "           (16x the texels of stock 1024). Verified in sub_1133650: v61=(1<<a2)\n"
            "           and the RT is created sub_C6D8E0(v61,v61,..); the projection matrix +\n"
            "           texel size all derive from the same 2^N, so it rescales consistently.\n"
            "HOW      : at 0x815C91 `push 0Ah` (6a0a) -> `push 0Ch` (6a0c).\n"
            "MEM      : VRAM only (one RT surface, DEFAULT pool) -- address-space-safe. ~4096^2\n"
            "           depth surface (order 64-96 MB VRAM); fine on a modern GPU.\n"
            "RISK     : low on modern GPUs. Mutually exclusive with shadowmap_2048 (same 2\n"
            "           bytes) -- enable only ONE. Small extra shadow-pass fill cost."
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
            "MEM      : VRAM/AGP only (DEFAULT+DYNAMIC pool) — address-space-safe.\n"
            "RISK     : low-med. Benefit shows only when terrain streaming nears the ceiling\n"
            "           (large/high-detail scenes, higher draw distance). Both edits applied\n"
            "           together; raising the flush cap alone would overflow the buffer."
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
            "RISK     : low. A few hundred more 2D point draws/updates per frame; trivial\n"
            "           on a modern CPU/GPU. (Don't push to absurd values.)"
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
        enabled=False,
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
            "           that lock is wasted CPU. NOP the `or edx,4` @0xBAE791 so the flag\n"
            "           is never set -> lock-free runtime path.\n"
            "EVIDENCE : disasm 0xBAE78B `mov edx,[+464h]; or edx,4 (83ca04); mov [+464h],edx`\n"
            "           feeding IDirect3D9::CreateDevice BehaviorFlags. NOP -> re-stores the\n"
            "           unchanged flags.\n"
            "QUALITY  : neutral (identical output; only removes internal locking).\n"
            "RISK     : ***MEDIUM -- correctness, not visual.*** Safe ONLY if no background\n"
            "           thread ever calls the D3D device. This client HAS async resource\n"
            "           loader threads; if any of them creates/locks a D3D resource off the\n"
            "           render thread, dropping MULTITHREADED can corrupt runtime state.\n"
            "           Test heavy zoning / model streaming before relying on it."
        ),
        apply=lambda d: patch_at_va(d, 0xBAE791,
            "83ca048b855cffffff", "9090908b855cffffff"),
    ),
    Fix(
        id="hw_vp_puredevice",
        module="gpu",
        enabled=False,
        title="HARDWARE_VP + PUREDEVICE (cheaper Set* calls; superset of hw_vertex_proc)",
        why=(
            "WHAT     : at the VP-flag composer (0xBB0E6C) widen `or ecx,20h` (SWVP) to\n"
            "           `or ecx,50h` = D3DCREATE_HARDWARE_VERTEXPROCESSING(0x40) |\n"
            "           D3DCREATE_PUREDEVICE(0x10). A pure device drops the runtime's\n"
            "           queryable shadow-state + per-call Set* validation, so the many\n"
            "           per-frame SetRenderState/SetTexture/SetTransform calls are cheaper.\n"
            "EVIDENCE : sub_BB0DE0 case 0 natively composes 0x50 (HWVP|PUREDEVICE), proving\n"
            "           the engine ships a pure-device path; PUREDEVICE requires HWVP (also\n"
            "           forced here). Output is bit-identical (pure device only removes\n"
            "           Get*-readback capability, no drawing change).\n"
            "QUALITY  : neutral.\n"
            "RISK     : medium. A pure device FAILS IDirect3DDevice9::Get* (GetRenderState/\n"
            "           GetTransform/...); safe only if the renderer tracks its own state\n"
            "           (typical Gamebryo, and implied by the native 0x50 mode). MUTUALLY\n"
            "           EXCLUSIVE with `hw_vertex_proc` -- this is its superset, so enable\n"
            "           ONE (enabling both still resolves to 0x50)."
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
        id="anisotropic_filtering",
        module="gpu",
        enabled=False,
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
            "COVERAGE : the fixed-function/model/terrain path (sub_DDC350). The modern\n"
            "           shader scene declares its samplers inside the .fx effect files\n"
            "           (data, not the exe), so those aren't covered by this exe patch --\n"
            "           for the whole scene, force AF via DXVK config on the `Lastest`\n"
            "           build (`d3d9.samplerAnisotropy = 16`).\n"
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
        enabled=False,
        title="Inline hot-loop singleton getters (call -> mov) -- quality-neutral CPU",
        why=(
            "WHAT     : Render_GameWorld's per-actor loop calls CModeMgr/CConfig/CCutscene\n"
            "           ::GetInstance -- each a one-instruction `mov eax,[global]; ret` --\n"
            "           through a real `call rel32` several times per visible actor per\n"
            "           frame. Replace `call getter` (E8 rel32) with the getter's own body\n"
            "           `mov eax,[global]` (A1 abs32), same 5 bytes, removing a call+ret.\n"
            "EVIDENCE : the 3 getters are verified pure `mov eax,[g]; ret` (no lazy init):\n"
            "           CModeMgr@0x8A8A30, CConfig@0xA43F80, CCutscene@0x462EF0. 5 hot sites\n"
            "           in Render_GameWorld, each call target confirmed. Applied all-or-none.\n"
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
        enabled=False,
        title="[.patch cave] View-cone cull: skip world objects behind the camera (make far draw distance cheap)",
        why=(
            "WHAT     : SceneManager_Cull_And_Render (0x806D00) submits EVERY world object\n"
            "           within the fog-far radius each frame with only a distance test -- no\n"
            "           frustum/direction test. So objects beside and BEHIND the camera (never\n"
            "           visible) still cost draw calls. This cave adds a forward-dot cone cull\n"
            "           at the per-object hook 0x80757F: cull anything more than SLACK units\n"
            "           behind the camera (and, if COS2>0, outside the view cone). Pairs with\n"
            "           max_view_distance -- the cone is what makes a long draw distance affordable.\n"
            "HOW      : 123-byte cave in the .patch section; hook overwrites `mov ecx,[ebx+88h]`.\n"
            "           forward = camera world matrix col0 [ebx+64/70/7C] (Right=col1, Up=col2,\n"
            "           both verified); deltas cam-obj already live at [ebp-2E4/-2E0/-2DC]. Cull\n"
            "           jumps to 0x807DB4 (the engine's OWN cull tail -> child-queue clear), keep\n"
            "           re-execs the displaced insn -> 0x807585. FPU balanced on every exit.\n"
            "TUNE     : _CONECULL_SLACK (default 50) and _CONECULL_COS2 (default 0 = behind-only,\n"
            "           no side cull) at the top of the file, then rebuild.\n"
            "VERIFY   : the ONE unprovable-statically bit is the forward-vector SIGN. On first run\n"
            "           of a build with this on: if the world IN FRONT of you vanishes/flickers as\n"
            "           you turn, the sign is inverted -> tell me and I flip the two `jb`s (1 byte\n"
            "           each). If front looks normal (only a small FPS gain), the sign is correct.\n"
            "RISK     : med, runtime-untested. OFF by default; fully reversible (separate exe).\n"
            "           Keep COS2=0 until the sign is confirmed; only then tighten the cone."
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
        enabled=False,
        title="Remove the 30-unit target-range cap (target/nameplate distant enemies) [gameplay]",
        why=(
            "WHAT     : CCharacter_CanTarget (0x4D81A0) ends with `return distance <= 30.0`\n"
            "           (double @0x1358008). Beyond 30 units an actor is UNTARGETABLE (no\n"
            "           nameplate / click / tab-target) -- the 'far band' where distant enemies\n"
            "           seem absent even though they ARE in the client (the render loop iterates\n"
            "           the FULL actor registry: AreaInfoMgr_IsInAreaBounds -> CActorRegistry+4).\n"
            "           NOP the distance-reject branch so CanTarget ignores range.\n"
            "HOW      : 0x4D8396 `jz loc_4D8259` (0F 84 BD FE FF FF) -> 6x NOP (90). Falls through\n"
            "           to `pop edi; pop esi; mov al,1; retn` after the earlier (non-distance)\n"
            "           validity checks. The 30.0 double is SHARED (~11 xrefs) so it is NOT\n"
            "           edited -- only this single CanTarget branch is neutralized. Disk-verified.\n"
            "PAIR     : complements actor_no_far_cull (renders the distant MODEL); this makes the\n"
            "           rendered distant enemy selectable/named so you can actually see/confirm it.\n"
            "RISK     : gameplay change -- lets you target/attack enemies at any range in the zone\n"
            "           (affects all CanTarget callers). Opt-in; reversible. For a finite cap\n"
            "           instead, ask for a cave that repoints the fcomp to a private 1500.0 double."
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
            "           Over budget -> return 0, which the caller already treats as 'not\n"
            "           ready, retry next frame' (mov [esi+257h],al), so the model just pops\n"
            "           in a frame or two later instead of stalling.\n"
            "SAFE     : returns 0 BEFORE any SEH/local frame is installed; ecx=this preserved;\n"
            "           proceed path replays the exact prologue and reconverges at 0x9272A5.\n"
            "RISK     : low-moderate. Converts a big hitch + alloc spike into a smooth\n"
            "           trickle; the tradeoff is slightly delayed model pop-in under load."
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
    #   redundancy left; the big neutral wins are the shipped HWVP/HWSKIN/no_particle_sort.
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
