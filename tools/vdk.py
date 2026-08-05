#!/usr/bin/env python3
"""
Ship an asset fix inside a VDK archive, safely.

Everything in this repository -- shaders and meshes alike -- reaches the game the same way:
extract an archive, overwrite some files, repack, install. That pipeline lives here once so
that the guard below cannot be forgotten in one place while being present in another.

  THE GUARD, and why it exists
  ----------------------------
  A fix was once built on top of a scratch extraction that had been made while a DIFFERENT,
  later-reverted patch was installed. The resulting "one file changed" archive silently
  carried 703 unrelated modified meshes back into the game. Nothing in the build reported a
  problem, because every step was individually correct -- the input was simply not what it
  was assumed to be.

  So: `--from` must name an archive whose md5 is a KNOWN STOCK reference (see STOCK below),
  or the build refuses to start. Provenance is checked, never assumed.

  And the build always ends by diffing the repacked archive against the stock extraction and
  printing the exact list of files that differ. If that list is not what you intended, the
  archive does not get installed. A build that reports "ok" without naming what it changed
  is not evidence of anything.

Usage
-----
  python tools/vdk.py build  --from <stock.VDK> --apply <dir> --out <patched.VDK>
  python tools/vdk.py verify --from <stock.VDK> --check <patched.VDK>
  python tools/vdk.py install --archive <patched.VDK> --to <client Data dir>

`--apply <dir>` is a tree whose layout mirrors the archive, e.g.
`assets/souldoll28/fixed/SOULDOLL_28.nif` maps to `ITEM\\PCPARTS\\Weapon\\SOULDOLL_28.nif`
via the `in_archive_path` recorded in that fix's report; pass `--map` to give it explicitly.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Known-stock archives, all from clients/ClientArchive/b303-2022-02-11-PRISTINE/Data.
# An archive not in this table is NOT a valid build input, however plausible its name.
# To add one: hash it out of the PRISTINE tree, never out of a play client.
STOCK = {
    "c82ccc877218e2586400ce6c1a151925": ("ITEM.VDK", 1128650077),
    "51477073c54cb475da73d3fe4e978a59": ("DATA.VDK",   71449138),
}

VDK_TOOL = os.path.normpath(os.path.join(
    ROOT, "..", "tools-ragnarok-online-2-vdk", "publish", "win-x64", "VDK_Tool.exe"))


def md5(path, _bufsize=1 << 22):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(_bufsize)
            if not b:
                return h.hexdigest()
            h.update(b)


def require_stock(path):
    """Refuse to proceed unless `path` is a known-stock archive. This is the guard."""
    if not os.path.exists(path):
        sys.exit(f"no such archive: {path}")
    d = md5(path)
    if d not in STOCK:
        sys.exit(
            f"REFUSING TO BUILD.\n"
            f"  {path}\n"
            f"  md5 {d} is not a known-stock archive.\n"
            f"  Known stock: " + ", ".join(f"{v[0]} {k}" for k, v in STOCK.items()) + "\n"
            f"  Building on top of an unverified archive is how unrelated changes get shipped.\n"
            f"  Take the archive from clients/ClientArchive/b303-2022-02-11-PRISTINE/Data,\n"
            f"  or add its hash to STOCK in this file if you have verified its provenance.")
    name, size = STOCK[d]
    actual = os.path.getsize(path)
    if actual != size:
        sys.exit(f"md5 matches {name} but size does not ({actual} vs {size}) -- refusing")
    print(f"  input verified stock: {name} md5 {d}")
    return name


def vdk(*args):
    if not os.path.exists(VDK_TOOL):
        sys.exit(f"VDK_Tool.exe not found at {VDK_TOOL} -- build tools-ragnarok-online-2-vdk")
    r = subprocess.run([VDK_TOOL, *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"VDK_Tool {args[0]} failed:\n{r.stderr or r.stdout}")


def extract(archive, dest):
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    vdk("x", archive, dest)
    return dest


def tree(root):
    return {os.path.relpath(os.path.join(dp, f), root)
            for dp, _, fs in os.walk(root) for f in fs}


def diff(a_root, b_root):
    """(differing, missing_from_b, added_in_b). Compares CONTENT, not just names.

    Membership is compared first and reported separately: a fix that silently drops or adds
    an archive entry is a different failure from one that changes a file, and collapsing the
    two hides it.
    """
    fa, fb = tree(a_root), tree(b_root)
    both = sorted(fa & fb)
    differ = [r for r in both
              if md5(os.path.join(a_root, r)) != md5(os.path.join(b_root, r))]
    return differ, sorted(fa - fb), sorted(fb - fa)


def load_map(apply_dir, explicit):
    """Map local files under `apply_dir` to their in-archive paths."""
    if explicit:
        return dict(p.split("=", 1) for p in explicit)
    report = os.path.join(os.path.dirname(apply_dir.rstrip("/\\")), "repair_report.json")
    if os.path.exists(report):
        r = json.load(open(report, encoding="utf-8"))
        if "in_archive_path" in r:
            files = [f for _, _, fs in os.walk(apply_dir) for f in fs]
            if len(files) == 1:
                return {files[0]: r["in_archive_path"]}
    sys.exit(f"cannot determine in-archive paths for {apply_dir} -- pass --map local=IN\\ARCHIVE\\PATH")


def cmd_build(a):
    print(f"building {a.out}")
    require_stock(a.src)

    work = os.path.join(ROOT, "_work")
    os.makedirs(work, exist_ok=True)
    stock_dir = os.path.join(work, "stock")
    build_dir = os.path.join(work, "build")

    print("  extracting stock ...")
    extract(a.src, stock_dir)
    if os.path.isdir(build_dir):
        shutil.rmtree(build_dir)
    shutil.copytree(stock_dir, build_dir)

    mapping = load_map(a.apply, a.map)
    for local, inarch in mapping.items():
        src = os.path.join(a.apply, local)
        dst = os.path.join(build_dir, inarch.replace("\\", os.sep))
        if not os.path.exists(dst):
            sys.exit(f"{inarch} is not in the archive -- check the mapping")
        shutil.copyfile(src, dst)
        print(f"  applied {local} -> {inarch}")

    print("  packing ...")
    if os.path.exists(a.out):
        os.remove(a.out)
    vdk("pack", build_dir, a.out)

    print("  verifying ...")
    ok = report_diff(a.src, a.out, set(mapping.values()), work)
    print(f"  output: {a.out}  md5 {md5(a.out)}  {os.path.getsize(a.out)} B")
    return 0 if ok else 1


def report_diff(stock_archive, patched_archive, intended, work):
    """Re-extract the PACKED archive and diff it against stock. The check is on the archive
    that will actually be installed, not on the directory it was built from -- a pack step
    that lost or mangled an entry would be invisible otherwise."""
    stock_dir = os.path.join(work, "stock")
    if not os.path.isdir(stock_dir):
        extract(stock_archive, stock_dir)
    check_dir = extract(patched_archive, os.path.join(work, "check"))

    differ, missing, added = diff(stock_dir, check_dir)
    print(f"    entries {len(tree(stock_dir))} -> {len(tree(check_dir))}"
          f"   lost {len(missing)}   added {len(added)}")
    print(f"    differing files: {len(differ)}")
    for r in differ[:20]:
        mark = "ok " if r.replace("/", "\\") in intended else "*** UNINTENDED ***"
        print(f"      {mark} {r}")
    if len(differ) > 20:
        print(f"      ... and {len(differ) - 20} more")

    unintended = [r for r in differ if r.replace("/", "\\") not in intended]
    missing_intended = [p for p in intended
                        if p.replace("\\", os.sep) not in {d.replace("/", os.sep) for d in differ}]
    ok = not (unintended or missing or added or missing_intended)
    if unintended:
        print(f"    FAIL: {len(unintended)} file(s) changed that no fix asked to change")
    if missing_intended:
        print(f"    FAIL: intended change did not land: {missing_intended}")
    if missing or added:
        print(f"    FAIL: archive membership changed")
    print("    " + ("PASS -- exactly the intended files differ" if ok else "DO NOT INSTALL"))
    return ok


def cmd_verify(a):
    require_stock(a.src)
    work = os.path.join(ROOT, "_work")
    os.makedirs(work, exist_ok=True)
    return 0 if report_diff(a.src, a.check, set(a.intended or []), work) else 1


def cmd_install(a):
    dest = os.path.join(a.to, os.path.basename(a.archive).split(".")[0] + ".VDK")
    if not os.path.isdir(a.to):
        sys.exit(f"no such directory: {a.to}")
    if os.path.exists(dest):
        bak = dest + ".bak"
        if not os.path.exists(bak):
            print(f"  backing up existing -> {bak}  (md5 {md5(dest)})")
            shutil.copyfile(dest, bak)
        else:
            print(f"  backup already exists, keeping it: {bak}  (md5 {md5(bak)})")
    shutil.copyfile(a.archive, dest)
    print(f"  installed {dest}  md5 {md5(dest)}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="extract stock, apply a fix, repack, verify")
    b.add_argument("--from", dest="src", required=True, help="a KNOWN-STOCK .VDK")
    b.add_argument("--apply", required=True, help="directory of replacement files")
    b.add_argument("--out", required=True)
    b.add_argument("--map", nargs="*", help="local=IN\\ARCHIVE\\PATH pairs")

    v = sub.add_parser("verify", help="diff a built archive against stock")
    v.add_argument("--from", dest="src", required=True)
    v.add_argument("--check", required=True)
    v.add_argument("--intended", nargs="*", help="in-archive paths expected to differ")

    i = sub.add_parser("install", help="copy an archive into a client Data dir")
    i.add_argument("--archive", required=True)
    i.add_argument("--to", required=True, help="the client's Data directory")

    a = ap.parse_args()
    return {"build": cmd_build, "verify": cmd_verify, "install": cmd_install}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
