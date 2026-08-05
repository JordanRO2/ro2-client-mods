#!/usr/bin/env python3
"""
Build the modified RO2 shader effects from `src/*.hlsl` into `build/`.

Two kinds of shader live here and they are built differently:

  * PS-SPLICE (the 13 Char_* effects). Only the lit PIXEL shader is recompiled; the
    result is spliced into the STOCK .fxo, leaving every vertex shader byte-identical.
    This is not a style choice -- the character vertex shader uses a legacy 90-register
    SkinBone tight-pack that modern fxc cannot reproduce, so recompiling the whole
    effect makes characters invisible. Splicing is the only safe route.

  * FULL EFFECT (PostEffect_SSAO). Compiled with `fxc /T fx_2_0`. Proven reproducible:
    compiling the UNMODIFIED source regenerates the shipped .fxo byte-for-byte, so any
    difference in the output is ours and nothing else's.

Always run `verify.py` afterwards. Building is not evidence that the result is sound.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE  = os.path.dirname(os.path.abspath(__file__))
ROOT  = os.path.dirname(HERE)
SRC   = os.path.join(ROOT, "src")
STOCK = os.path.join(ROOT, "stock")
BUILD = os.path.join(ROOT, "build")

# Effects compiled as a whole effect rather than spliced.
FULL_EFFECT = {"PostEffect_SSAO"}

FXC_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\fxc.exe",
    r"C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\fxc.exe",
]


def find_fxc():
    for p in FXC_CANDIDATES:
        if os.path.exists(p):
            return p
    from shutil import which
    p = which("fxc")
    if p:
        return p
    sys.exit("fxc.exe not found -- install the Windows SDK or edit FXC_CANDIDATES")


def run_fxc(fxc, args):
    """Invoke fxc.

    Note for anyone running this from Git Bash: MSYS rewrites arguments that look like
    paths, so `/T` becomes `C:/Program Files/Git/T` and fxc reports "too many files".
    Setting MSYS_NO_PATHCONV disables that. Harmless everywhere else.
    """
    env = dict(os.environ, MSYS_NO_PATHCONV="1", MSYS2_ARG_CONV_EXCL="*")
    return subprocess.run([fxc] + args, capture_output=True, text=True, env=env)


def slots(asm_path):
    if not os.path.exists(asm_path):
        return None
    import re
    for line in open(asm_path, encoding="utf-8", errors="replace"):
        m = re.search(r"approximately (\d+) instruction", line)
        if m:
            return int(m.group(1))
    return None


def build_one(fxc, name, keep_asm=False):
    src = os.path.join(SRC, name + ("_ps.hlsl" if name not in FULL_EFFECT else ".hlsl"))
    if not os.path.exists(src):
        src_alt = os.path.join(SRC, name + ".hlsl")
        if os.path.exists(src_alt):
            src = src_alt
        else:
            return name, False, "no source"
    out_fxo = os.path.join(BUILD, name + ".fxo")

    if name in FULL_EFFECT:
        r = run_fxc(fxc, ["/T", "fx_2_0", "/Fo", out_fxo, src])
        if r.returncode != 0:
            return name, False, (r.stderr or r.stdout).strip().splitlines()[-1:]
        return name, True, "full effect"

    ps_bin = os.path.join(BUILD, "_ps", name + ".bin")
    ps_asm = os.path.join(BUILD, "_ps", name + ".asm")
    os.makedirs(os.path.dirname(ps_bin), exist_ok=True)
    r = run_fxc(fxc, ["/T", "ps_3_0", "/E", "main", "/Fo", ps_bin, "/Fc", ps_asm, src])
    if r.returncode != 0:
        return name, False, (r.stderr or r.stdout).strip().splitlines()[-1:]

    base = os.path.join(STOCK, name + ".fxo")
    if not os.path.exists(base):
        return name, False, "no stock .fxo to splice into"

    # growsplice replaces every PS blob at or above --min with the new one. For CHARACTER
    # effects that is correct and field-validated: their lit PS blobs are all the same
    # shader. It is NOT correct for world OBJECT/TERRAIN effects, which carry several
    # DISTINCT lit PS (one per technique) and would be collapsed into one -- that already
    # destroyed Rag2ObjectShader_VCAB once (3 distinct VS -> 1). verify.py checks for it.
    psplice = os.path.join(HERE, "psplice.py")
    r = subprocess.run([sys.executable, psplice, "growsplice", base, ps_bin, out_fxo,
                        "--min", "1000"], capture_output=True, text=True)
    if r.returncode != 0:
        return name, False, (r.stderr or r.stdout).strip().splitlines()[-1:]
    if not keep_asm:
        for p in (ps_asm,):
            if os.path.exists(p):
                os.remove(p)
    return name, True, f"spliced ({slots(ps_asm) or '?'} slots)"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("only", nargs="*", help="effect names to build (default: all in src/)")
    ap.add_argument("--keep-asm", action="store_true", help="keep the .asm listings")
    ap.add_argument("--clean", action="store_true", help="wipe build/ first")
    a = ap.parse_args()

    if a.clean and os.path.isdir(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(BUILD, exist_ok=True)

    names = a.only or sorted({
        f[:-len("_ps.hlsl")] if f.endswith("_ps.hlsl") else f[:-len(".hlsl")]
        for f in os.listdir(SRC) if f.endswith(".hlsl")
    })

    fxc = find_fxc()
    ok = bad = 0
    for n in names:
        name, good, msg = build_one(fxc, n, a.keep_asm)
        print(f"  {'ok  ' if good else 'FAIL'} {name:<26} {msg}")
        ok, bad = (ok + 1, bad) if good else (ok, bad + 1)
    print(f"\n{ok} built, {bad} failed -> {BUILD}")
    print("Now run:  python tools/verify.py")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
