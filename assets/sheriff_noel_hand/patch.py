#!/usr/bin/env python3
r"""
Sheriff Suit (Noel) right-hand skin fix - repoint a mis-authored bone-palette slot.

WHAT'S WRONG
------------
The Noel-race Sheriff Suit chest meshes bind ~25% of the RIGHT wrist/palm skin to
`Bip01 Prop1` (the right-hand weapon/prop ATTACHMENT bone, parented to
`Bip01 R Hand`) instead of to `Bip01 R Hand` itself. At runtime `Bip01 Prop1` is
driven by the prop/weapon system (or is absent from the base body skeleton), so
the right wrist/palm follows the prop bone rather than the hand and deforms
(stretch/collapse). The LEFT hand of the same mesh binds the mirror verts 100% to
`Bip01 L Hand` with no prop bone, and every Human variant and every control Noel
costume (Cowboy/Pirate/Navalofficer/Reinhard) is prop-free and symmetric - so this
is a genuine, isolated authoring defect on the Noel right hand, present in both
`Sheriff_Noel_Male_CHEST_02.nif` (unlimited item) and `_01.nif` (30-day item).

RO2 uses Gamebryo NIF 20.6 (NiMesh / NiDataStream). Skinning bones live in a
`NiSkinningMeshModifier` bone list; the per-vertex `BLENDINDICES` are palette-LOCAL
indices into a per-submesh region of the `BONE_PALETTE` NiDataStream, which maps
palette-local index -> bone-list index -> skeleton NiNode. In the affected mesh
(`ALL_BODY:0`, LOD0, block 332, nsub=3), submesh 1 is the right arm/hand, and its
BONE_PALETTE region entry 0 holds bone-list index 25 = `Bip01 Prop1`. Every
weighted vertex that references submesh-1 local index 0 is a right-hand wrist/palm
vertex (verified: all on the -x/right side, all nearer R Hand than L Hand, tightly
clustered <=14% of the mesh bbox from `Bip01 R Hand`).

THE FIX
-------
Repoint that ONE palette entry from `Bip01 Prop1` (bone-list index 25) to
`Bip01 R Hand` (bone-list index 45) - a single 2-byte in-place write per file
(u16 `19 00` -> `2D 00`). Nothing else changes: geometry, weights, node tree,
animation controllers, and the `Bip01 Prop1` node itself are all untouched (the
node stays for real prop attachment; only the SKIN stops binding to it). The fix
is NOT hardcoded to a byte offset - the target mesh, submesh, palette slot and
bone-list indices are all located by parsing, and the write is refused unless the
slot currently reads exactly `Bip01 Prop1` and every weighted vertex using it is a
right-hand vertex.

INPUT
-----
The currently-deployed modded ITEM.VDK (the `ITEM_VDK` mod), md5
3e576b5946a86eed045c387e85574020. This patch replaces ONLY the two Sheriff Noel
CHEST nifs inside it.

USAGE
-----
    python patch.py --in <ITEM.VDK> --out <patched ITEM.VDK>
        [--vdk-tool <VDK_Tool.exe>] [--allow-any-stock]

Refuses to start unless --in md5 is a known input (KNOWN_MD5), then extracts,
applies the two palette repoints, repacks, and verifies (a) ONLY the two target
.nif paths changed in the whole archive and (b) each fixed nif right-hand cluster
is now prop-free with submesh-1 slot 0 resolving to `Bip01 R Hand`.
Requires numpy (the repo NIF-tooling dependency) and VDK_Tool.exe.
"""
import argparse, hashlib, os, shutil, struct, subprocess, sys, tempfile
import numpy as np

KNOWN_MD5 = {
    "3e576b5946a86eed045c387e85574020": "b303 ITEM.VDK + item-model fixes",
}
TARGET_BASENAMES = ["sheriff_noel_male_chest_02.nif", "sheriff_noel_male_chest_01.nif"]
PROP_BONE = "Bip01 Prop1"
HAND_BONE = "Bip01 R Hand"

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


NODE_TYPES = ('NiNode', 'NiLODNode', 'NiSwitchNode', 'NiBillboardNode')


class Rd:
    def __init__(self, b, p): self.b = b; self.p = p
    def u8(self):  v = self.b[self.p]; self.p += 1; return v
    def u16(self): v = struct.unpack_from('<H', self.b, self.p)[0]; self.p += 2; return v
    def u32(self): v = struct.unpack_from('<I', self.b, self.p)[0]; self.p += 4; return v
    def i32(self): v = struct.unpack_from('<i', self.b, self.p)[0]; self.p += 4; return v
    def skip(self, n): self.p += n
    def take(self, n): v = self.b[self.p:self.p + n]; self.p += n; return v


class Nif:
    def __init__(self, data):
        self.b = data
        nl = data.index(b'\n')
        r = Rd(data, nl + 1)
        self.version = r.u32()
        assert self.version == 0x14060000, 'unexpected NIF version %08x' % self.version
        assert r.u8() == 1, 'big-endian file'
        r.u32()
        self.nobj = r.i32()
        ntypes = r.u16(); types = []
        for _ in range(ntypes):
            s = r.take(r.u32())
            types.append('NiDataStream' if s.startswith(b'NiDataStream')
                         else s.decode('latin-1').rstrip('\x00'))
        self.otype = [types[r.u16() & 0x7FFF] for _ in range(self.nobj)]
        self.osize = [r.u32() for _ in range(self.nobj)]
        nstr = r.u32(); r.u32()
        self.strs = [r.take(r.u32()).decode('latin-1').rstrip('\x00') for _ in range(nstr)]
        r.skip(4 * r.u32())
        self.ooff = []; p = r.p
        for i in range(self.nobj):
            self.ooff.append(p); p += self.osize[i]
        assert p <= len(data), 'block table overruns file'

    def s(self, i): return self.strs[i] if 0 <= i < len(self.strs) else None

    def node(self, i):
        r = Rd(self.b, self.ooff[i])
        name = self.s(r.i32()); r.skip(4 * r.u32()); ctrl = r.i32(); r.u16()
        T = np.array(struct.unpack_from('<3f', self.b, r.p)); r.skip(12)
        M = np.array(struct.unpack_from('<9f', self.b, r.p)).reshape(3, 3); r.skip(36)
        s = struct.unpack_from('<f', self.b, r.p)[0]; r.skip(4)
        r.skip(4 * r.u32()); r.i32()
        nc = r.u32(); ch = [r.i32() for _ in range(nc)]
        return dict(name=name, ctrl=ctrl, T=T, M=M, s=s, ch=ch)

    def node_name(self, i):
        try:
            return self.node(i)['name']
        except Exception:
            return None

    def parents(self):
        par = {}
        for i, t in enumerate(self.otype):
            if t not in NODE_TYPES:
                continue
            try:
                ch = self.node(i)['ch']
            except Exception:
                continue
            for c in ch:
                if 0 <= c < self.nobj:
                    par[c] = i
        return par

    def world_positions(self):
        par = self.parents()
        nodes = {i: self.node(i) for i, t in enumerate(self.otype) if t in NODE_TYPES}
        wL = {}; wT = {}

        def comp(i):
            if i in wT:
                return
            loc = nodes[i]; L = loc['M'] * loc['s']; p = par.get(i)
            if p is None or p not in nodes:
                wL[i] = L; wT[i] = loc['T'].copy()
            else:
                comp(p); wL[i] = wL[p] @ L; wT[i] = wL[p] @ loc['T'] + wT[p]
        for i in nodes:
            comp(i)
        return {nodes[i]['name']: wT[i] for i in nodes}

    def mesh(self, i):
        r = Rd(self.b, self.ooff[i])
        name = self.s(r.i32()); r.skip(4 * r.u32()); r.i32(); r.u16(); r.skip(12 + 36 + 4)
        r.skip(4 * r.u32()); r.i32()
        nmat = r.u32(); [self.s(r.i32()) for _ in range(nmat)]; r.skip(4 * nmat); r.i32(); r.u8()
        r.u32(); r.u16(); r.u8(); r.skip(16)
        ns = r.u32(); streams = []
        for _ in range(ns):
            ref = r.i32(); r.u8(); r.skip(2 * r.u16())
            nc = r.u32(); comps = [(self.s(r.i32()), r.u32()) for _ in range(nc)]
            streams.append((ref, comps))
        nmod = r.u32(); mods = [r.i32() for _ in range(nmod)]
        return dict(name=name, streams=streams, mods=mods)

    def datastream(self, i):
        r = Rd(self.b, self.ooff[i])
        nb = r.u32(); r.u32(); nreg = r.u32()
        regs = [(r.u32(), r.u32()) for _ in range(nreg)]
        ncf = r.u32(); fmts = [r.u32() for _ in range(ncf)]
        return dict(nbytes=nb, regs=regs, fmts=fmts, data_off=r.p)

    def modifier_bones(self, i):
        r = Rd(self.b, self.ooff[i])
        nsp = r.u32(); r.skip(2 * nsp); ncp = r.u32(); r.skip(2 * ncp)
        r.u16(); r.i32(); r.skip(52)
        nb = r.u32(); return [r.i32() for _ in range(nb)]


def comp_size(f):
    return ((f >> 16) & 0xFF), ((f >> 8) & 0xFF)


def eff_w(W, v, k):
    """Effective weight of influence lane k. BLENDWEIGHT stores 3 explicit floats;
    the 4th influence weight is implicit = 1 - sum(the three)."""
    if k < W.shape[1]:
        return float(W[v, k])
    return 1.0 - float(W[v].sum())


def _target_mesh(nif):
    for i, t in enumerate(nif.otype):
        if t != 'NiMesh':
            continue
        m = nif.mesh(i)
        smod = [x for x in m['mods'] if 0 <= x < nif.nobj and nif.otype[x] == 'NiSkinningMeshModifier']
        if not smod:
            continue
        bones = nif.modifier_bones(smod[0]); names = [nif.node_name(b) for b in bones]
        if PROP_BONE not in names or HAND_BONE not in names:
            continue
        pal = next((r for r, c in m['streams'] if [x[0] for x in c] == ['BONE_PALETTE']), None)
        if pal is None:
            continue
        d = nif.datastream(pal); nc, bpc = comp_size(d['fmts'][0])
        if bpc != 2 or len(d['regs']) < 2:
            continue
        vals = np.frombuffer(nif.b[d['data_off']:d['data_off'] + d['nbytes']], '<u2').astype(int)
        base = d['regs'][1][0]; v0 = int(vals[base])
        if not (0 <= v0 < len(bones) and names[v0] == PROP_BONE):
            continue
        return dict(mesh_block=i, mesh=m, bones=bones, names=names, ds=d, vals=vals,
                    sub1_base=base, idx_prop=names.index(PROP_BONE), idx_hand=names.index(HAND_BONE))
    return None


def _vertex_stream(nif, m):
    for ref, comps in m['streams']:
        sems = [c[0] for c in comps]
        if 'BLENDWEIGHT' in sems and 'BLENDINDICES' in sems and 'POSITION_BP' in sems:
            d = nif.datastream(ref)
            off = {}; o = 0
            for (sem, _ix), f in zip(comps, d['fmts']):
                ncc, bpcc = comp_size(f); off[sem] = (o, ncc, bpcc); o += ncc * bpcc
            nv = d['nbytes'] // o
            raw = np.frombuffer(nif.b, np.uint8, count=o * nv, offset=d['data_off']).reshape(nv, o)
            po, _, _ = off['POSITION_BP']; bo, bnc, _ = off['BLENDINDICES']; wo, wnc, wbpc = off['BLENDWEIGHT']
            pos = raw[:, po:po + 12].copy().view(np.float32).reshape(nv, 3).astype(np.float64)
            bidx = raw[:, bo:bo + bnc].astype(np.int64)
            W = raw[:, wo:wo + wnc * wbpc].copy().view(np.float32).reshape(nv, wnc).astype(np.float64)
            sub = np.zeros(nv, np.int64)
            for si, (s, c) in enumerate(d['regs']):
                sub[s:s + c] = si
            return dict(nv=nv, pos=pos, bidx=bidx, W=W, sub=sub, regs=d['regs'])
    return None


def plan_fix(nif_bytes):
    nif = Nif(nif_bytes)
    t = _target_mesh(nif)
    if t is None:
        raise RuntimeError("no target mesh (submesh-1 palette slot 0 != %s) found" % PROP_BONE)
    vs = _vertex_stream(nif, t['mesh'])
    if vs is None:
        raise RuntimeError("target mesh has no interleaved vertex stream")
    wpos = nif.world_positions()
    Rh, Lh = wpos[HAND_BONE], wpos['Bip01 L Hand']
    EPS = 1e-6
    vids = [v for v in range(vs['nv']) if vs['sub'][v] == 1 and
            any(int(vs['bidx'][v, k]) == 0 and eff_w(vs['W'], v, k) > EPS for k in range(4))]
    if not vids:
        raise RuntimeError("no weighted vertices reference submesh-1 slot 0")
    P = vs['pos'][vids]
    dR = np.linalg.norm(P - Rh, axis=1); dL = np.linalg.norm(P - Lh, axis=1)
    diag = float(np.linalg.norm(vs['pos'].max(0) - vs['pos'].min(0)))
    bad = int((~((P[:, 0] < 0) & (dR < dL))).sum())
    if bad:
        raise RuntimeError("SAFETY FAIL: %d slot-0 verts not right-hand (wrong side / nearer L Hand)" % bad)
    off = t['ds']['data_off'] + t['sub1_base'] * 2
    old = nif_bytes[off:off + 2]
    if struct.unpack('<H', old)[0] != t['idx_prop']:
        raise RuntimeError("SAFETY FAIL: palette slot byte does not read Prop1 index")
    return dict(offset=off, old=old, new=struct.pack('<H', t['idx_hand']),
                idx_prop=t['idx_prop'], idx_hand=t['idx_hand'], mesh_block=t['mesh_block'],
                nverts=len(vids), max_frac=100 * float(dR.max()) / diag)


def apply_fix(nif_bytes):
    plan = plan_fix(nif_bytes)
    b = bytearray(nif_bytes)
    assert b[plan['offset']:plan['offset'] + 2] == plan['old']
    b[plan['offset']:plan['offset'] + 2] = plan['new']
    return bytes(b), plan


def verify_fixed(nif_bytes):
    nif = Nif(nif_bytes)
    for i, tt in enumerate(nif.otype):
        if tt != 'NiMesh':
            continue
        m = nif.mesh(i)
        smod = [x for x in m['mods'] if 0 <= x < nif.nobj and nif.otype[x] == 'NiSkinningMeshModifier']
        if not smod:
            continue
        bones = nif.modifier_bones(smod[0]); names = [nif.node_name(b) for b in bones]
        if HAND_BONE not in names:
            continue
        pal = next((r for r, c in m['streams'] if [x[0] for x in c] == ['BONE_PALETTE']), None)
        if pal is None:
            continue
        d = nif.datastream(pal); nc, bpc = comp_size(d['fmts'][0])
        if bpc != 2 or len(d['regs']) < 2:
            continue
        vals = np.frombuffer(nif.b[d['data_off']:d['data_off'] + d['nbytes']], '<u2').astype(int)
        if names[int(vals[d['regs'][1][0]])] != HAND_BONE:
            continue
        vs = _vertex_stream(nif, m); wpos = nif.world_positions()
        Rh = wpos[HAND_BONE]; idx_prop = names.index(PROP_BONE) if PROP_BONE in names else -1
        EPS = 1e-6
        d2 = np.linalg.norm(vs['pos'] - Rh, axis=1)
        diag = float(np.linalg.norm(vs['pos'].max(0) - vs['pos'].min(0)))
        hand = [v for v in range(vs['nv']) if vs['pos'][v, 0] < 0 and d2[v] < 0.20 * diag]
        prop_hits = 0
        for v in hand:
            s = int(vs['sub'][v]); base = vs['regs'][s][0] if s < len(vs['regs']) else 0
            for k in range(4):
                if eff_w(vs['W'], v, k) > EPS:
                    gi = base + int(vs['bidx'][v, k])
                    if 0 <= gi < len(vals) and int(vals[gi]) == idx_prop:
                        prop_hits += 1
                        break
        if prop_hits:
            raise RuntimeError("VERIFY FAIL: %d right-hand verts still bind to %s" % (prop_hits, PROP_BONE))
        return dict(mesh_block=i, hand_verts=len(hand))
    raise RuntimeError("VERIFY FAIL: fixed target mesh (slot0 -> R Hand) not found")


def _tree(d):
    out = {}
    for root, _, files in os.walk(d):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, d).replace('\\', '/').lower()] = md5(p)
    return out


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
        print(f"WARNING: {a.src} md5={m} is not a known input ITEM.VDK - proceeding on --allow-any-stock")
    else:
        sys.exit(f"refusing to build: {a.src} md5={m} is not a known input ITEM.VDK "
                 f"(pass --allow-any-stock to override). Known: {list(KNOWN_MD5)}")

    work = tempfile.mkdtemp(prefix="sheriff_noel_hand_")
    try:
        ext = os.path.join(work, "ITEM")
        print(f"extracting {a.src} -> {ext} ...")
        run(tool, "extract", a.src, ext)

        found = {}
        for root, _, files in os.walk(ext):
            for f in files:
                if f.lower() in TARGET_BASENAMES:
                    found.setdefault(f.lower(), []).append(os.path.join(root, f))
        rels = []
        for bn in TARGET_BASENAMES:
            hits = found.get(bn, [])
            if len(hits) != 1:
                sys.exit(f"expected exactly 1 '{bn}' in archive, found {len(hits)}: {hits}")
            path = hits[0]
            fixed, plan = apply_fix(open(path, "rb").read())
            open(path, "wb").write(fixed)
            verify_fixed(fixed)
            rel = os.path.relpath(path, ext).replace('\\', '/').lower()
            rels.append(rel)
            print(f"  fixed {rel}")
            print(f"     palette slot repointed at nif offset {plan['offset']} (0x{plan['offset']:X}): "
                  f"{plan['idx_prop']} ({PROP_BONE}) -> {plan['idx_hand']} ({HAND_BONE})")
            print(f"     right-hand verts using slot 0 = {plan['nverts']} "
                  f"(all <= {plan['max_frac']:.1f}% of bbox from R Hand)")

        print(f"packing -> {a.out} ...")
        run(tool, "pack", ext, a.out)
        _verify(tool, a.src, a.out, rels, work)
        print(f"OK -> {a.out}  md5={md5(a.out)}  size={os.path.getsize(a.out)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify(tool, in_vdk, out_vdk, target_rels, work):
    a_dir, b_dir = os.path.join(work, "vin"), os.path.join(work, "vout")
    run(tool, "extract", in_vdk, a_dir)
    run(tool, "extract", out_vdk, b_dir)
    ta, tb = _tree(a_dir), _tree(b_dir)
    diff = sorted(k for k in set(ta) | set(tb) if ta.get(k) != tb.get(k))
    want = sorted(target_rels)
    if diff != want:
        sys.exit(f"VERIFY FAILED: files other than the two target nifs differ.\n  changed={diff}\n  expected={want}")
    for rel in target_rels:
        info = verify_fixed(open(os.path.join(b_dir, *rel.split('/')), "rb").read())
        print(f"verify ok: {rel} right hand prop-free ({info['hand_verts']} hand verts, 0 bind {PROP_BONE})")
    print(f"verify ok: exactly {len(diff)} files changed in the archive - the two Sheriff Noel nifs, nothing else")


if __name__ == "__main__":
    main()
