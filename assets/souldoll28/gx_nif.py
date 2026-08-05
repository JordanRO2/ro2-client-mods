#!/usr/bin/env python3
"""THIRD, independent NIF 20.6 reader -- written for the adversarial geometry
review of the round-2 neck weld.  Deliberately imports NOTHING from nifread.py,
nifprobe.py, vnif.py, w2_lib.py, weld_lib.py.

Every structural assumption is asserted, not trusted:
  * each NiMesh / NiDataStream block must consume EXACTLY its declared size
  * stride is the sum of the per-component (ncomp * bytes) words, and
    numBytes % stride must be 0
  * vertex count is numBytes // stride, and sum(region counts) must equal it
  * NiAVObject::flags is read as a USHORT

Index base (absolute vs submesh-relative) is decided by per-region range
containment and the decision is reported, never guessed.
"""
import struct, math, os
import numpy as np
from collections import defaultdict

NODE_TYPES = ('NiNode', 'NiLODNode', 'NiSwitchNode', 'NiBillboardNode')


class R:
    __slots__ = ('b', 'p')

    def __init__(self, b, p):
        self.b = b; self.p = p

    def u8(self):
        v = self.b[self.p]; self.p += 1; return v

    def u16(self):
        v = struct.unpack_from('<H', self.b, self.p)[0]; self.p += 2; return v

    def u32(self):
        v = struct.unpack_from('<I', self.b, self.p)[0]; self.p += 4; return v

    def i32(self):
        v = struct.unpack_from('<i', self.b, self.p)[0]; self.p += 4; return v

    def skip(self, n):
        self.p += n

    def take(self, n):
        v = self.b[self.p:self.p + n]; self.p += n; return v


class GNif:
    def __init__(self, path, data=None):
        self.path = path
        self.b = data if data is not None else open(path, 'rb').read()
        b = self.b
        nl = b.index(b'\n')
        self.hdrline = b[:nl].decode('ascii', 'replace')
        r = R(b, nl + 1)
        self.version = r.u32()
        assert self.version == 0x14060000, 'unexpected version %08x in %s' % (self.version, path)
        self.endian = r.u8()
        assert self.endian == 1, 'big-endian file %s' % path
        self.uver = r.u32()
        self.nobj = r.i32()
        ntypes = r.u16()
        types = []
        for _ in range(ntypes):
            s = r.take(r.u32())
            # NiDataStream type strings in 20.6 carry an 8-byte binary
            # usage/access suffix; keep only the class name.
            if s.startswith(b'NiDataStream'):
                types.append('NiDataStream')
            else:
                types.append(s.decode('latin-1').rstrip('\x00'))
        self.otype = []
        for _ in range(self.nobj):
            t = r.u16() & 0x7FFF
            self.otype.append(types[t])
        self.osize = [r.u32() for _ in range(self.nobj)]
        nstr = r.u32(); r.u32()                       # count, max length
        self.strs = []
        for _ in range(nstr):
            L = r.u32()
            self.strs.append(r.take(L).decode('latin-1').rstrip('\x00') if L else '')
        r.skip(4 * r.u32())                           # groups
        self.ooff = []
        p = r.p
        for i in range(self.nobj):
            self.ooff.append(p)
            p += self.osize[i]
        self.body_end = p
        # A footer follows; the body must at least fit inside the file.
        assert p <= len(b), 'block table overruns file %s' % path

    def s(self, i):
        return self.strs[i] if 0 <= i < len(self.strs) else None

    # ------------------------------------------------------------------
    def node(self, i):
        base = self.ooff[i]
        r = R(self.b, base)
        name = self.s(r.i32())
        r.skip(4 * r.u32())          # extra data
        r.i32()                      # controller
        r.u16()                      # NiAVObject::flags  (USHORT)
        r.skip(12 + 36 + 4)          # translation / rotation / scale
        r.skip(4 * r.u32())          # properties
        r.i32()                      # collision object
        nc = r.u32()
        ch = [r.i32() for _ in range(nc)]
        r.skip(4 * r.u32())          # effects
        return name, ch, (r.p - base) == self.osize[i]

    def parents(self):
        par = {}
        for i, t in enumerate(self.otype):
            if t not in NODE_TYPES:
                continue
            try:
                nm, ch, ok = self.node(i)
            except Exception:
                continue
            if not ok:
                continue
            for c in ch:
                if 0 <= c < self.nobj:
                    par[c] = i
        return par

    def mesh(self, i):
        base = self.ooff[i]
        r = R(self.b, base)
        name = self.s(r.i32())
        r.skip(4 * r.u32())
        r.i32()
        r.u16()                                        # flags = USHORT
        r.skip(12 + 36 + 4)
        r.skip(4 * r.u32())
        r.i32()
        nmat = r.u32()
        mats = [self.s(r.i32()) for _ in range(nmat)]
        r.skip(4 * nmat)
        r.i32()                                        # active material
        r.u8()                                         # materialNeedsUpdate
        prim = r.u32()
        nsub = r.u16()
        r.u8()                                         # instanced
        r.skip(16)                                     # bound
        ns = r.u32()
        streams = []
        for _ in range(ns):
            ref = r.i32()
            r.u8()
            r.skip(2 * r.u16())                        # submesh->region map
            ncomp = r.u32()
            comps = [(self.s(r.i32()), r.u32()) for _ in range(ncomp)]
            streams.append((ref, comps))
        nmod = r.u32()
        mods = [r.i32() for _ in range(nmod)]
        return dict(idx=i, name=name, mats=mats, prim=prim, nsub=nsub,
                    streams=streams, mods=mods,
                    exact=(r.p - base) == self.osize[i])

    def datastream(self, i):
        assert self.otype[i] == 'NiDataStream'
        base = self.ooff[i]
        r = R(self.b, base)
        nb = r.u32()
        r.u32()                                        # cloning behaviour
        nreg = r.u32()
        regions = [(r.u32(), r.u32()) for _ in range(nreg)]
        ncf = r.u32()
        fmts = [r.u32() for _ in range(ncf)]
        doff = r.p
        r.skip(nb)
        r.u8()                                         # streamable
        return dict(idx=i, nbytes=nb, regions=regions, fmts=fmts, data_off=doff,
                    exact=(r.p - base) == self.osize[i])


def comp_size(f):
    """(numComponents, bytes-per-component) from a NiDataStream format word."""
    return ((f >> 16) & 0xFF), ((f >> 8) & 0xFF)


def layout(n, ref, comps):
    """-> dict(off={sem:(byte_off, ncomp, bpc)}, stride, nverts, data_off, regions)
    or None if anything fails to validate."""
    if not (0 <= ref < n.nobj) or n.otype[ref] != 'NiDataStream':
        return None
    d = n.datastream(ref)
    if not d['exact'] or len(d['fmts']) != len(comps):
        return None
    off = {}
    o = 0
    for (sem, _ix), f in zip(comps, d['fmts']):
        nc, bpc = comp_size(f)
        off[sem] = (o, nc, bpc)
        o += nc * bpc
    if o <= 0 or d['nbytes'] % o:
        return None
    nv = d['nbytes'] // o
    reg_ok = (not d['regions']) or (sum(c for _b, c in d['regions']) == nv)
    return dict(off=off, stride=o, nverts=nv, data_off=d['data_off'],
                regions=d['regions'], reg_ok=reg_ok, ds=ref, nbytes=d['nbytes'])


def lod_of(parent_name, mesh_name):
    s = parent_name or mesh_name or ''
    if s.endswith('_02'):
        return 2
    if s.endswith('_01'):
        return 1
    return 0


def meshes(n, buf=None):
    """Every NiMesh with a POSITION_BP + NORMAL_BP stream, decoded."""
    if buf is None:
        buf = n.b
    par = n.parents()
    out = []
    for i, t in enumerate(n.otype):
        if t != 'NiMesh':
            continue
        try:
            m = n.mesh(i)
        except Exception:
            continue
        if not m['exact']:
            continue
        # A mesh may interleave every semantic in ONE stream (the common case) or
        # give each semantic its OWN stream (blocks 295/348/396 of
        # SiegeRobe_Female_CHEST_02).  Requiring POSITION_BP and NORMAL_BP in the
        # SAME stream silently drops the split-layout meshes -- so both are
        # handled and the layout is recorded.
        V = Vp = I = None
        others = []
        for ref, comps in m['streams']:
            sems = [c[0] for c in comps]
            L = layout(n, ref, comps)
            if L is None:
                continue
            if 'POSITION_BP' in L['off'] and 'NORMAL_BP' in L['off'] and V is None:
                V = Vp = L
            elif sems == ['INDEX']:
                I = L
            else:
                if 'NORMAL_BP' in L['off'] and V is None:
                    V = L
                if 'POSITION_BP' in L['off'] and Vp is None:
                    Vp = L
                others.append((sems, L))
        if V is None or Vp is None or V['nverts'] != Vp['nverts']:
            continue
        split = V is not Vp
        if split:
            others = [(s, L) for s, L in others if L is not V and L is not Vp]
        po, pnc, pbpc = Vp['off']['POSITION_BP']
        no, nnc, nbpc = V['off']['NORMAL_BP']
        if (pnc, pbpc) != (3, 4) or (nnc, nbpc) != (3, 4):
            continue
        nv, st, do = V['nverts'], V['stride'], V['data_off']
        pst, pdo = Vp['stride'], Vp['data_off']
        praw = np.frombuffer(buf, np.uint8, count=pst * nv, offset=pdo).reshape(nv, pst)
        raw = np.frombuffer(buf, np.uint8, count=st * nv, offset=do).reshape(nv, st)
        pos = praw[:, po:po + 12].copy().view(np.float32).reshape(nv, 3).astype(np.float64)
        nor = raw[:, no:no + 12].copy().view(np.float32).reshape(nv, 3).astype(np.float64)
        idx = None
        if I is not None:
            ibpc = I['stride']
            if ibpc in (2, 4):
                idx = np.frombuffer(buf, '<u2' if ibpc == 2 else '<u4',
                                    count=I['nverts'], offset=I['data_off']).astype(np.int64)
        pn = None
        if i in par:
            try:
                pn = n.node(par[i])[0]
            except Exception:
                pn = None
        pn = pn.split('[')[0] if pn else None
        out.append(dict(blk=i, name=m['name'], parent=pn, lod=lod_of(pn, m['name']),
                        prim=m['prim'], nsub=m['nsub'], mods=m['mods'],
                        mat=(m['mats'][0] if m['mats'] else None),
                        nverts=nv, stride=st, data_off=do, pos_off=po, nrm_off=no,
                        pos_stride=pst, pos_data_off=pdo, split_streams=split,
                        vregions=V['regions'], vreg_ok=V['reg_ok'],
                        iregions=(I['regions'] if I else []),
                        idx=idx, pos=pos, nor=nor, vlay=V, play=Vp, other=others))
    return out


def index_base(m):
    """'single' | 'absolute' | 'relative' | None(ambiguous or broken).

    FALSIFIABLE: if both containment predicates hold on every region and the
    two readings are NOT byte-identical, the answer is None -- the mesh is
    reported, not guessed.
    """
    idx, vr, ir, nv = m['idx'], m['vregions'], m['iregions'], m['nverts']
    if idx is None:
        return None, 'no index stream'
    if len(vr) <= 1 or len(ir) != len(vr):
        ok = idx.size == 0 or (idx.min() >= 0 and idx.max() < nv)
        return ('single' if ok else None), ('' if ok else 'out of range')
    a = rl = True
    same = True
    for (vb, vc), (ib, ic) in zip(vr, ir):
        sub = idx[ib:ib + ic]
        if sub.size == 0:
            continue
        lo, hi = int(sub.min()), int(sub.max())
        if not (lo >= vb and hi < vb + vc):
            a = False
        if not (hi < vc):
            rl = False
        if vb != 0:
            same = False
    if a and rl:
        return ('absolute' if same else None), 'both'
    if a:
        return 'absolute', ''
    if rl:
        return 'relative', ''
    return None, 'neither'


def tris(m):
    mode, why = index_base(m)
    if mode is None:
        return None, mode
    idx, vr, ir, nv = m['idx'], m['vregions'], m['iregions'], m['nverts']
    if mode == 'single':
        T = idx[:idx.size // 3 * 3].reshape(-1, 3)
    else:
        parts = []
        for (vb, vc), (ib, ic) in zip(vr, ir):
            sub = idx[ib:ib + ic]
            if mode == 'relative':
                sub = sub + vb
            parts.append(sub[:sub.size // 3 * 3].reshape(-1, 3))
        T = np.concatenate(parts) if parts else np.zeros((0, 3), np.int64)
    if T.size and T.max() >= nv:
        return None, mode
    return T, mode


def open_boundary(m, T, Q=2e-4):
    """Vertex indices that are an endpoint of an edge used by exactly one
    triangle, after welding vertices that share a quantised position."""
    if T is None or T.size == 0:
        return set()
    nv = m['nverts']
    key = np.rint(m['pos'] / Q).astype(np.int64)
    rep = {}
    repof = np.empty(nv, np.int64)
    for v in range(nv):
        k = (int(key[v, 0]), int(key[v, 1]), int(key[v, 2]))
        repof[v] = rep.setdefault(k, v)
    cnt = defaultdict(int)
    for a, b, c in T:
        ra, rb, rc = repof[a], repof[b], repof[c]
        for x, y in ((ra, rb), (rb, rc), (rc, ra)):
            if x != y:
                cnt[(x, y) if x < y else (y, x)] += 1
    open_e = set(e for e, k in cnt.items() if k == 1)
    rim = set()
    for a, b, c in T:
        for x, y in ((a, b), (b, c), (c, a)):
            rx, ry = repof[x], repof[y]
            if rx == ry:
                continue
            if ((rx, ry) if rx < ry else (ry, rx)) in open_e:
                rim.add(int(x)); rim.add(int(y))
    return rim


def ang(a, b):
    la = math.sqrt(a[0] * a[0] + a[1] * a[1] + a[2] * a[2])
    lb = math.sqrt(b[0] * b[0] + b[1] * b[1] + b[2] * b[2])
    if la == 0 or lb == 0 or la != la or lb != lb:
        return float('nan')
    d = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (la * lb)
    return math.degrees(math.acos(max(-1.0, min(1.0, d))))
