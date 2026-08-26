#!/usr/bin/env python3
r"""
Evil/Devil Wing Hat model-path fix — repair a typo in the ItemInfo Mesh field.

WHAT'S WRONG
------------
Five ItemInfo rows for the "Devil Wing Hat" and its slot variants
    13231354 Devil Wing Hat
    13231355 Devil Wing Hat - 1 Slot
    13231356 Devil Wing Hat - 2 Slot
    13231357 Devil Wing Hat - 3 Slot
    13231358 Devil Wing Hat - 3 Slot Purple
carry a Mesh path with a missing `_01` infix:
    .\Item\PCParts\HeadAccessory\%s\AngelWingHat\Black\%s_HEAD_AngelWingHat_C2.nif
No such file exists. The real model on disk is
    %s_HEAD_AngelWingHat_01_C2.nif
(e.g. Male_HEAD_AngelWingHat_01_C2.nif / Female_HEAD_AngelWingHat_01_C2.nif in
...\HeadAccessory\<gender>\AngelWingHat\Black\). The `%s` tokens are the
race/gender substitutions the client fills in at load time. Because the path
points at a non-existent file, the hat renders as a broken/absent model.

The control item "Angelwing Hat" (13231319) uses the correct `_01` convention
(`%s_HEAD_AngelWingHat_01.nif`), so `_01` is unambiguously the right infix — the
Devil Wing Hat rows simply lost it.

THE FIX
-------
In the Mesh field only, insert the missing `_01` by replacing the broken
substring with the corrected one:
    _HEAD_AngelWingHat_C2.nif  ->  _HEAD_AngelWingHat_01_C2.nif
Everything else in every row stays stock: name, icon, stats, slots, prices,
the surrounding directory path, and the Mesh2 field.

TARGETS (selected dynamically, not hardcoded): every ItemInfo row whose Mesh
field contains the exact substring `_HEAD_AngelWingHat_C2.nif` (the broken,
no-`_01` form). On the current input this resolves to exactly the 5 Devil Wing
Hat rows above. Processing is line-by-line, so any duplicate rows sharing an id
are all patched.

INPUT
-----
This patch chains ON TOP of the quiver_honing ASSET.VDK, so its known input is
that patched VDK (md5 d55112cda7f3613be5cd4d80e302ba3d), NOT stock. Rebuild it
from stock with:
    python ../quiver_honing/patch.py --in <stock ASSET.VDK> --out ASSET.honing.VDK
(stock ASSET.VDK md5 = 91c32367eee36dd0016a72bc001568ec).

USAGE
-----
    python patch.py --in <ASSET.honing.VDK> --out <patched ASSET.VDK>
        [--vdk-tool <VDK_Tool.exe>] [--allow-any-stock]

The build refuses to start unless `--in` md5 is a known input (see KNOWN_MD5),
mirroring the provenance guard the other patchers use. It ends by re-extracting
the result and asserting that ONLY ItemInfo.ct changed, that the ONLY column
that changed is Mesh, that it changed ONLY on rows whose input Mesh carried the
broken substring, and that each such row's Mesh gained exactly `_01` — anything
else aborts without writing the output.
"""
import argparse, csv, hashlib, io, os, shutil, subprocess, sys, tempfile

# Known input md5s. This patch's input is the quiver_honing output, NOT stock.
KNOWN_MD5 = {
    "d55112cda7f3613be5cd4d80e302ba3d": "b303 ASSET.VDK + quiver_honing",
}
BROKEN = "_HEAD_AngelWingHat_C2.nif"
FIXED = "_HEAD_AngelWingHat_01_C2.nif"

DEFAULT_VDK_TOOL = os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "tools-ragnarok-online-2-vdk", "publish", "win-x64", "VDK_Tool.exe")


def md5(p):
    h = hashlib.md5()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(tool, *args):
    subprocess.run([tool, *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ct_to_csv(tool, ct_path, scratch):
    """Convert a .ct to a .csv via VDK_Tool, in a scratch dir OUTSIDE any extraction tree
    (a stray sidecar left inside the extraction would get packed into the archive).
    Returns the csv path."""
    tmp = os.path.join(scratch, os.path.basename(ct_path))
    shutil.copy(ct_path, tmp)
    run(tool, "ct2csv", tmp)
    return tmp[:-3] + ".csv"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--vdk-tool", default=os.path.abspath(DEFAULT_VDK_TOOL))
    ap.add_argument("--allow-any-stock", action="store_true",
                    help="skip the known-input md5 guard (use only if you trust --in)")
    a = ap.parse_args()
    tool = a.vdk_tool

    m = md5(a.src)
    if m in KNOWN_MD5:
        print(f"input ok: {a.src}  md5={m}  ({KNOWN_MD5[m]})")
    elif a.allow_any_stock:
        print(f"WARNING: {a.src} md5={m} is not a known input ASSET.VDK — proceeding on --allow-any-stock")
    else:
        sys.exit(f"refusing to build: {a.src} md5={m} is not a known input ASSET.VDK "
                 f"(pass --allow-any-stock to override). Known: {list(KNOWN_MD5)}")

    work = tempfile.mkdtemp(prefix="evil_wing_hat_")
    try:
        ext = os.path.join(work, "ASSET")
        run(tool, "extract", a.src, ext)
        # locate ItemInfo.ct inside the extraction
        ii = None
        for root, _, files in os.walk(ext):
            for f in files:
                if f.lower() == "iteminfo.ct":
                    ii = os.path.join(root, f)
        if not ii:
            sys.exit("ItemInfo.ct not found in archive")

        scratch = os.path.join(work, "scratch")
        os.makedirs(scratch)
        ii_csv = ct_to_csv(tool, ii, scratch)

        # Process ItemInfo LINE BY LINE so duplicate rows sharing an id are ALL patched.
        # The Mesh column is located BY HEADER NAME (never hardcoded); ".index('Mesh')"
        # returns the exact "Mesh" column, not "Mesh2".
        hdr = None
        mesh = None
        out = []
        changed = []          # ids changed (order of appearance)
        for line in open(ii_csv, encoding="utf-8-sig").read().splitlines():
            if line.startswith("ID,Name,"):
                hdr = next(csv.reader([line]))
                mesh = hdr.index("Mesh")
                out.append(line)
                continue
            if hdr and line and line.split(",", 1)[0].isdigit():
                r = next(csv.reader([line]))
                if len(r) > mesh and BROKEN in r[mesh]:
                    before = r[mesh]
                    r[mesh] = r[mesh].replace(BROKEN, FIXED)
                    changed.append(r[0])
                    print(f"   {r[0]}  {r[1]}")
                    print(f"       before: {before}")
                    print(f"       after : {r[mesh]}")
                    s = io.StringIO()
                    csv.writer(s, lineterminator="").writerow(r)
                    out.append(s.getvalue())
                    continue
            out.append(line)
        uniq = sorted(set(changed))
        print(f"wing-hat rows patched: {len(changed)} rows / {len(uniq)} ids "
              f"(Mesh {BROKEN!r} -> {FIXED!r})")
        if not changed:
            sys.exit(f"no rows contain {BROKEN!r} in Mesh — is this the right ASSET.VDK?")

        open(ii_csv, "w", encoding="utf-8-sig", newline="").write("\n".join(out) + "\n")
        run(tool, "csv2ct", ii_csv)
        shutil.copy(ii_csv[:-4] + ".ct", ii)  # ONLY replace ItemInfo.ct; no sidecar left inside ext

        # sanity: no stray csv/xlsx crept into the extraction tree
        strays = [os.path.join(r, f) for r, _, fs in os.walk(ext) for f in fs
                  if f.lower().endswith((".csv", ".xlsx"))]
        if strays:
            sys.exit(f"aborting: stray sidecar files in extraction would be packed in: {strays}")

        run(tool, "pack", ext, a.out)
        _verify(tool, a.src, a.out, set(changed), work)
        print(f"OK -> {a.out}  md5={md5(a.out)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify(tool, in_vdk, patched_vdk, target_ids, work):
    """Re-extract both and assert ONLY ItemInfo.ct changed, ONLY the Mesh column,
    ONLY on rows whose INPUT Mesh contained the broken substring, and each such row's
    Mesh gained exactly `_01` (BROKEN -> FIXED)."""
    a_dir, b_dir = os.path.join(work, "vs"), os.path.join(work, "vp")
    run(tool, "extract", in_vdk, a_dir)
    run(tool, "extract", patched_vdk, b_dir)

    def tree(d):
        out = {}
        for root, _, files in os.walk(d):
            for f in files:
                p = os.path.join(root, f)
                out[os.path.relpath(p, d).lower()] = md5(p)
        return out

    ta, tb = tree(a_dir), tree(b_dir)
    diff = sorted(k for k in set(ta) | set(tb) if ta.get(k) != tb.get(k))
    if diff != ["asset\\iteminfo.ct"] and diff != ["asset/iteminfo.ct"]:
        sys.exit(f"VERIFY FAILED: files other than ItemInfo.ct differ: {diff}")

    def rows(d):
        run(tool, "ct2csv", os.path.join(d, "ASSET", "ItemInfo.ct"))
        hdr = None
        mesh = None
        m = {}
        for line in open(os.path.join(d, "ASSET", "ItemInfo.csv"),
                         encoding="utf-8-sig").read().splitlines():
            if line.startswith("ID,Name,"):
                hdr = next(csv.reader([line]))
                mesh = hdr.index("Mesh")
            elif hdr and line.split(",", 1)[0].isdigit():
                r = next(csv.reader([line]))
                m[r[0]] = r
        return m, mesh

    ra, mesh_a = rows(a_dir)
    rb, mesh_b = rows(b_dir)
    if mesh_a != mesh_b:
        sys.exit(f"VERIFY FAILED: Mesh column index moved ({mesh_a} -> {mesh_b})")
    mesh = mesh_a

    seen = set()
    for k in ra:
        if ra[k] != rb[k]:
            diffcols = [i for i in range(len(ra[k])) if ra[k][i] != rb[k][i]]
            if set(diffcols) != {mesh}:
                sys.exit(f"VERIFY FAILED: row {k} changed columns other than Mesh: {diffcols}")
            if k not in target_ids:
                sys.exit(f"VERIFY FAILED: non-target row {k} was changed")
            if BROKEN not in ra[k][mesh]:
                sys.exit(f"VERIFY FAILED: row {k} input Mesh did not contain {BROKEN!r}")
            if rb[k][mesh] != ra[k][mesh].replace(BROKEN, FIXED):
                sys.exit(f"VERIFY FAILED: row {k} Mesh is not the exact {BROKEN!r} -> {FIXED!r} fix")
            if FIXED not in rb[k][mesh]:
                sys.exit(f"VERIFY FAILED: row {k} output Mesh did not gain {FIXED!r}")
            seen.add(k)
    if seen != set(target_ids):
        sys.exit(f"VERIFY FAILED: changed rows {sorted(seen)} != targets {sorted(target_ids)}")
    print(f"verify ok: only ItemInfo.ct, only Mesh, only the {len(seen)} broken-path rows "
          f"(each gained _01)")


if __name__ == "__main__":
    main()
