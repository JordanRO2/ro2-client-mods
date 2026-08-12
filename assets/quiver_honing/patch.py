#!/usr/bin/env python3
"""
Quiver honing fix — restore the honing/grinding UI on quivers that were set up for it.

WHAT'S WRONG
------------
Ten grade-4 quivers were configured to be honeable: they carry `Grinding_Trait_Able = 1`,
a full row of trait options in `NEW_TraitInfo`, grade-4 stats and mastery requirements.
The developers turned the grinding flag ON for the Eddga quiver in build 201 (2015) and for
the Basilisk quiver back in build 113 — clear intent to make them honeable content.

But every quiver keeps `Item_Type = 104` (Projectile), and the client gates the honing /
option section of the item tooltip on the item's primary type: `ItemTooltip_GetTypeNameFrom
Field520` (Rag2.exe 0x9CB6E0) maps type 104/105 to the "Projectile" label, and the tooltip
drawer only renders the honing lines for weapon/armor types. A Projectile never shows honing,
no matter how it is set up. So the grinding configuration on these quivers has been dead the
whole time — the honing UI simply never appears.

This is a data inconsistency: the item says "honeable" (flag + options + stats) while its
type says "ammo, no honing". `Item_Type` was 104 in every build the quivers ever shipped in
(verified across b113..b303), so nothing "changed the type" — the honing was enabled without
ever aligning the type, and stayed invisible.

THE FIX
-------
For each affected quiver set:
    Item_Type   104 -> 1   (Weapon; makes the tooltip stop saying "Projectile" and render
                            the honing/option section, exactly like every other honeable item)
    Weapon_Type 0   -> 1   (a valid weapon subtype — the option section also requires one;
                            Item_Type alone removes the "Projectile" label but the honing
                            lines only appear once the item is a real weapon subtype)

Everything else stays stock: name, icon, mesh, Equip_Slot (13 — still the quiver slot),
its own trait options, prices, stats. Only these two bytes change, only on the quivers that
were already flagged grindable AND actually carry trait options.

TARGETS (selected dynamically, not hardcoded): every ItemInfo row with
    Item_Type == 104  AND  Grinding_Trait_Able == 1  AND  NEW_TraitInfo trait_count > 0
On stock b303 this resolves to 10 quivers (15 rows — 5 have a duplicate table row):
    16700727 Basilisk   16700827 Ashkaron   16701610 Himmelmez   16701763 Eddga
    16701854 Witch      16701936 Lich       16702032 Mustafa     16702116 Cazar
    16702296 Serenia    16702331 Ancient Warrior
The Niflheim quiver (16702377) is grindable-flagged but has zero trait options, so its honing
would be empty; it is deliberately left out.

USAGE
-----
    python patch.py --in <stock ASSET.VDK> --out <patched ASSET.VDK>
        [--vdk-tool <VDK_Tool.exe>] [--allow-any-stock]

The build refuses to start unless `--in` md5 is a known-stock ASSET.VDK (see STOCK_MD5), the
same provenance guard tools/vdk.py uses. It ends by re-extracting the result and asserting
that ONLY ItemInfo.ct changed and ONLY Item_Type/Weapon_Type changed, on exactly the target
rows — anything else aborts without writing the output.
"""
import argparse, csv, hashlib, io, os, shutil, subprocess, sys, tempfile

# Known-stock ASSET.VDK md5s (b303 line). Extend as new verified-stock clients are added.
STOCK_MD5 = {
    "91c32367eee36dd0016a72bc001568ec": "b303-2022-02-11 ASSET.VDK (official)",
}
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


def load_traitinfo(csv_path):
    """NEW_TraitInfo -> {ItemID: row}. Read-only, so first-wins dedup is fine here."""
    hdr = None
    rows = {}
    for line in open(csv_path, encoding="utf-8-sig").read().splitlines():
        if hdr is None and line.startswith("ItemID,"):
            hdr = next(csv.reader([line]))
            continue
        if hdr:
            r = next(csv.reader([line]))
            if r and r[0].isdigit():
                rows.setdefault(r[0], dict(zip(hdr, r)))
    return rows


def trait_count(nt_rows, item_id):
    d = nt_rows.get(item_id)
    if not d:
        return 0
    return sum(1 for i in range(15) if d.get(f"Trait_Kind{i}") not in ("-1", None, ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--vdk-tool", default=os.path.abspath(DEFAULT_VDK_TOOL))
    ap.add_argument("--allow-any-stock", action="store_true",
                    help="skip the known-stock md5 guard (use only if you trust --in)")
    a = ap.parse_args()
    tool = a.vdk_tool

    m = md5(a.src)
    if m in STOCK_MD5:
        print(f"stock ok: {a.src}  md5={m}  ({STOCK_MD5[m]})")
    elif a.allow_any_stock:
        print(f"WARNING: {a.src} md5={m} is not a known stock ASSET.VDK — proceeding on --allow-any-stock")
    else:
        sys.exit(f"refusing to build: {a.src} md5={m} is not a known-stock ASSET.VDK "
                 f"(pass --allow-any-stock to override). Known: {list(STOCK_MD5)}")

    work = tempfile.mkdtemp(prefix="quiver_honing_")
    try:
        ext = os.path.join(work, "ASSET")
        run(tool, "extract", a.src, ext)
        # locate the two tables inside the extraction
        ii = nt = None
        for root, _, files in os.walk(ext):
            for f in files:
                if f.lower() == "iteminfo.ct":
                    ii = os.path.join(root, f)
                elif f.lower() == "new_traitinfo.ct":
                    nt = os.path.join(root, f)
        if not ii or not nt:
            sys.exit("ItemInfo.ct / NEW_TraitInfo.ct not found in archive")

        scratch = os.path.join(work, "scratch")
        os.makedirs(scratch)
        nt_rows = load_traitinfo(ct_to_csv(tool, nt, scratch))
        ii_csv = ct_to_csv(tool, ii, scratch)

        # Process ItemInfo LINE BY LINE so duplicate rows sharing an id are ALL patched
        # (5 of the target quivers have a duplicate table row). No deduping.
        hdr = None
        it = gr = wt = None
        out = []
        changed = []
        for line in open(ii_csv, encoding="utf-8-sig").read().splitlines():
            if line.startswith("ID,Name,"):
                hdr = next(csv.reader([line]))
                it = hdr.index("Item_Type")
                gr = hdr.index("Grinding_Trait_Able")
                wt = hdr.index("Weapon_Type")
                out.append(line)
                continue
            if hdr and line and line.split(",", 1)[0].isdigit():
                r = next(csv.reader([line]))
                if r[it] == "104" and r[gr] == "1" and trait_count(nt_rows, r[0]) > 0:
                    r[it] = "1"
                    r[wt] = "1"
                    changed.append(r[0])
                    s = io.StringIO()
                    csv.writer(s, lineterminator="").writerow(r)
                    out.append(s.getvalue())
                    continue
            out.append(line)
        uniq = sorted(set(changed))
        print(f"quivers patched: {len(changed)} rows / {len(uniq)} ids -> Item_Type=1, Weapon_Type=1")
        for i in uniq:
            print(f"   {i}")
        if not changed:
            sys.exit("no target quivers found — is this the right ASSET.VDK?")

        open(ii_csv, "w", encoding="utf-8-sig", newline="").write("\n".join(out) + "\n")
        run(tool, "csv2ct", ii_csv)
        shutil.copy(ii_csv[:-4] + ".ct", ii)  # ONLY replace ItemInfo.ct; no sidecar left inside ext

        # sanity: no stray csv/xlsx crept into the extraction tree
        strays = [os.path.join(r, f) for r, _, fs in os.walk(ext) for f in fs
                  if f.lower().endswith((".csv", ".xlsx"))]
        if strays:
            sys.exit(f"aborting: stray sidecar files in extraction would be packed in: {strays}")

        run(tool, "pack", ext, a.out)
        _verify(tool, a.src, a.out, set(changed), it, wt, work)
        print(f"OK -> {a.out}  md5={md5(a.out)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify(tool, stock_vdk, patched_vdk, target_ids, it, wt, work):
    """Re-extract both and assert ONLY ItemInfo.ct changed, ONLY Item_Type/Weapon_Type,
    ONLY on the target rows (104->1 / 0->1)."""
    a_dir, b_dir = os.path.join(work, "vs"), os.path.join(work, "vp")
    run(tool, "extract", stock_vdk, a_dir)
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
        m = {}
        for line in open(os.path.join(d, "ASSET", "ItemInfo.csv"),
                         encoding="utf-8-sig").read().splitlines():
            if line.startswith("ID,Name,"):
                hdr = next(csv.reader([line]))
            elif hdr and line.split(",", 1)[0].isdigit():
                r = next(csv.reader([line]))
                m[r[0]] = r
        return m

    ra, rb = rows(a_dir), rows(b_dir)
    for k in ra:
        if ra[k] != rb[k]:
            diffcols = [i for i in range(len(ra[k])) if ra[k][i] != rb[k][i]]
            if set(diffcols) - {it, wt}:
                sys.exit(f"VERIFY FAILED: row {k} changed columns other than Item_Type/Weapon_Type")
            if k not in target_ids:
                sys.exit(f"VERIFY FAILED: non-target row {k} was changed")
            if not (ra[k][it] == "104" and rb[k][it] == "1"):
                sys.exit(f"VERIFY FAILED: row {k} Item_Type transition is not 104->1")
    print("verify ok: only ItemInfo.ct, only Item_Type/Weapon_Type, only target quivers")


if __name__ == "__main__":
    main()
