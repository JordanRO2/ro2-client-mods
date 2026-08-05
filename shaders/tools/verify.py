#!/usr/bin/env python3
"""
Verify built effects against the stock originals before anything is shipped.

Every check here exists because the corresponding mistake was actually made and actually
reached the game. Read the comments before deciding a failure is spurious.

Usage:  python tools/verify.py [build|deployed]     (default: build)
"""
import glob
import hashlib
import os
import struct
import sys

HERE  = os.path.dirname(os.path.abspath(__file__))
ROOT  = os.path.dirname(HERE)
STOCK = os.path.join(ROOT, "stock")

U = lambda d, o: struct.unpack_from("<I", d, o)[0]

# -0.3f re-emitted through a float->int32->float round trip. See check_lod_bias.
CORRUPT_BIAS = struct.pack("<I", 0xCE82CCCD)
GOOD_BIAS    = struct.pack("<I", 0xBE99999A)


def blobs(path):
    """Every shader blob in an .fxo, as (kind, model, sha1).

    Blobs live in the fx_2_0 footer as index-linked StateBlobs; each starts with a version
    token followed by a comment token holding the CTAB. Walking token-by-token to the END
    marker is what makes this exact rather than a heuristic scan.
    """
    d = open(path, "rb").read()
    out = []
    for o in range(0, len(d) - 4, 4):
        v = U(d, o)
        if (v >> 16) not in (0xFFFF, 0xFFFE):
            continue
        mj, mn = (v >> 8) & 0xFF, v & 0xFF
        if mj not in (1, 2, 3) or mn > 4:
            continue
        if o + 12 > len(d) or (U(d, o + 4) & 0xFFFF) != 0xFFFE or d[o + 8:o + 12] != b"CTAB":
            continue
        p = o + 4
        while p < len(d) - 4:
            t = U(d, p)
            if t == 0x0000FFFF:
                p += 4
                break
            # comment tokens carry a 15-bit dword count; instructions carry a 4-bit one
            p += 4 + 4 * (((t >> 16) & 0x7FFF) if (t & 0xFFFF) == 0xFFFE
                          else ((t >> 24) & 0x0F))
        out.append(("VS" if (v >> 16) == 0xFFFE else "PS", f"{mj}_{mn}",
                    hashlib.sha1(d[o:p]).hexdigest()[:10]))
    return out


# Effects that were built as a FULL recompile rather than a PS splice, historically or by
# design. Their vertex shaders legitimately differ from stock, so the byte-identity rule
# does not apply to them -- but nothing may be ADDED to this set without a reason, and a
# Char_* effect must never appear here (see check_vs_identical).
FULL_RECOMPILED = {
    "PostEffect_SSAO", "Rag2ObjectShader_Default", "Rag2ObjectShader_VCAB1_Glow",
    "SkyDome", "SpeedGrass", "Terrain",
}


def check_vs_identical(name, cand):
    """Vertex shaders must be byte-identical to stock -- for SPLICED effects.

    The character VS uses a legacy 90-register SkinBone tight-pack that modern fxc cannot
    reproduce. A VS change in a Char_* effect means it was recompiled rather than spliced,
    and characters render INVISIBLE in game. That is the failure this guards.

    Full-effect recompiles (see FULL_RECOMPILED) legitimately regenerate their vertex
    shaders. For those the rule is inverted: the check is that they are NOT a Char_*
    effect, since no character effect may ever be built that way.
    """
    s = [b for b in blobs(os.path.join(STOCK, name + ".fxo")) if b[0] == "VS"]
    c = [b for b in blobs(cand) if b[0] == "VS"]
    if s == c:
        return True, f"{len(c)} VS identical"
    if name.startswith("Char_"):
        return False, f"VS CHANGED on a CHARACTER effect ({len(s)} stock vs {len(c)}) -- SKINNING WILL BREAK"
    if name in FULL_RECOMPILED:
        return True, f"{len(c)} VS recompiled (full-effect build, expected)"
    return False, (f"VS CHANGED ({len(s)} stock vs {len(c)}) and {name} is not a known "
                   f"full-effect build -- either it was built wrong or FULL_RECOMPILED needs updating")


def check_no_variant_collapse(name, cand):
    """Distinct PS count must not drop below stock.

    World OBJECT/TERRAIN effects carry several DISTINCT lit pixel shaders, one per
    technique. `growsplice` replaces them all with one shader, collapsing the variants.
    That is correct for the Char_* effects (their lit blobs really are one shader, and it
    is field-validated) but it silently broke Rag2ObjectShader_VCAB, whose 3 distinct
    vertex shaders became 1 and 4 pixel shaders became 2.
    """
    su = len({b[2] for b in blobs(os.path.join(STOCK, name + ".fxo")) if b[0] == "PS"})
    cu = len({b[2] for b in blobs(cand) if b[0] == "PS"})
    if name.startswith("Char_"):
        return True, f"PS {su}->{cu} (char: collapse expected)"
    return (cu >= su, f"PS {su}->{cu}" if cu >= su else f"PS VARIANTS COLLAPSED {su}->{cu}")


def check_lod_bias(name, cand):
    """No float that has been through an int32 round trip.

    A decompile->recompile pipeline once read -0.3f's bit pattern (0xBE99999A) as an
    int32 (-1097229926) and re-emitted it as a float literal (0xCE82CCCD = -1.09e9).
    That value as a MipMapLodBias pins mip 0 at every distance -- it was the "no mipmaps"
    bug. Scan for the exact corrupted pattern.
    """
    n = open(cand, "rb").read().count(CORRUPT_BIAS)
    return (n == 0, "clean" if n == 0 else f"{n} CORRUPT float(int32(bits)) values")


def check_model_ceiling(name, cand):
    """ps_3_0 / vs_3_0 is the D3D9 ceiling under DXVK."""
    bad = [b for b in blobs(cand) if int(b[1].split("_")[0]) > 3]
    return (not bad, "ok" if not bad else f"{len(bad)} blobs above SM3")


CHECKS = [
    ("VS byte-identical to stock", check_vs_identical),
    ("no new variant collapse",    check_no_variant_collapse),
    ("no int32-round-trip floats", check_lod_bias),
    ("shader model <= 3",          check_model_ceiling),
]


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "build"
    cand_dir = os.path.join(ROOT, which)
    if not os.path.isdir(cand_dir):
        sys.exit(f"no such directory: {cand_dir}")

    files = sorted(glob.glob(os.path.join(cand_dir, "*.fxo")))
    if not files:
        sys.exit(f"no .fxo in {cand_dir} -- run tools/build.py first")

    print(f"verifying {len(files)} effect(s) in {which}/ against stock/\n")
    failures = 0
    for f in files:
        name = os.path.basename(f)[:-4]
        if not os.path.exists(os.path.join(STOCK, name + ".fxo")):
            print(f"  {name:<34} SKIP (no stock counterpart)")
            continue
        if open(f, "rb").read() == open(os.path.join(STOCK, name + ".fxo"), "rb").read():
            continue  # unmodified, nothing to check
        results = []
        for label, fn in CHECKS:
            ok, msg = fn(name, f)
            if not ok:
                failures += 1
                results.append(f"*** {label}: {msg} ***")
            else:
                results.append(msg)
        print(f"  {name:<34} {' | '.join(results)}")

    print(f"\n{'PASS -- safe to package' if failures == 0 else f'FAIL -- {failures} problem(s), DO NOT SHIP'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
