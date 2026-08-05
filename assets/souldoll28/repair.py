#!/usr/bin/env python3
"""
SOULDOLL_28.nif repair -- convert the mesh from SOFTWARE skinning to the
HARDWARE-skinning shape that all 33 healthy dolls (and all 25,926 hardware
meshes in the client) ship with.

WHAT IS REPAIRED, and why this and nothing else
-----------------------------------------------
SOULDOLL_28 is the only doll whose NiSkinningMeshModifier has
USE_SOFTWARE_SKINNING (flags 0x0003 vs 0x0002).  Every other render-path
anomaly in the file is a mechanical consequence of that one bit, and the
NiDataStream *type strings* prove it independently of the flag:

    slot  semantic       usage/access   meaning
    190   INDEX          (0,18)         CPU_WRITE_STATIC|GPU_READ
    191   POSITION       (1,24)         CPU_WRITE_VOLATILE|GPU_READ   <- deform OUTPUT
    192   NORMAL         (1,24)         CPU_WRITE_VOLATILE|GPU_READ   <- deform OUTPUT
    193   POSITION_BP    (1, 3)         CPU_READ|CPU_WRITE_STATIC     <- NOT GPU-readable
    194   NORMAL_BP      (1, 3)         CPU_READ|CPU_WRITE_STATIC     <- NOT GPU-readable
    195   BLENDINDICES   (1, 3)         CPU_READ|CPU_WRITE_STATIC     <- NOT GPU-readable
    196   BLENDWEIGHT    (1, 3)         CPU_READ|CPU_WRITE_STATIC     <- NOT GPU-readable
    197   TEXCOORD       (1,18)         CPU_WRITE_STATIC|GPU_READ

i.e. in this file the ONLY GPU-readable geometry is `POSITION` (slot 191),
whose stored bytes are byte-identical to the bind pose (md5 of 191 == md5 of
193) and are refreshed each frame only by the CPU deform task at SYNC_VISIBLE.
The bind pose of these dolls is authored at the character origin, so any draw
that reads POSITION without a completed deform renders a doll-shaped
silhouette at the feet.

The repair therefore rebuilds the mesh into the canonical hardware shape:
  1. one interleaved GPU-readable vertex stream [TEXCOORD, POSITION_BP,
     NORMAL_BP, BLENDINDICES, BLENDWEIGHT] at stride 48, usage/access (1,18);
  2. a BONE_PALETTE stream, usage/access (3,3), identity permutation;
  3. the POSITION / NORMAL deform-output streams dropped from the mesh;
  4. NiSkinningMeshModifier flags 0x0003 -> 0x0002;
  5. submit/complete sync points 2 -> 1 (drop the SYNC_VISIBLE deform pair);
  6. material name DefaultRag2ShaderNoSkin_Spec -> DefaultRag2ShaderSkinSpecular.

BLOCK COUNT IS DEliberately HELD AT 199
---------------------------------------
Deleting the five now-unused NiDataStream blocks would renumber every block
above them, which means finding and rewriting every Ptr/Ref in 18 distinct
block types -- and completeness of that sweep cannot be proven without a full
NIF 20.6 serializer.  Instead the block count, block order and every reference
in the file stay byte-identical; the five spare slots are rewritten as minimal
0-byte CPU-only NiDataStreams (29 bytes each, 145 bytes total) that nothing
references.  The streamer instantiates and immediately drops them.

Everything is done by SPLICING: bytes this tool does not intend to change are
copied verbatim, so "unchanged" is a property of the code, not of a re-encode.

The animation-side differences (Root NonAccum, the 2-vs-68 NiTransformControllers,
the absent NiTextKeyExtraData) are NOT touched -- they were refuted as the
mechanism, and the wearer PCNOEL_FEMALE_01.nif itself has all of them.
"""
import os, sys, struct, hashlib, json

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'dolls', 'SOULDOLL_28.nif')
OUTDIR = os.path.join(HERE, 'souldoll28_fix')
OUT = os.path.join(OUTDIR, 'SOULDOLL_28.nif')

NEW_MATERIAL = 'DefaultRag2ShaderSkinSpecular'
OLD_MATERIAL = 'DefaultRag2ShaderNoSkin_Spec'

# canonical interleaved layout, taken from SOULDOLL_01/29 (and 25,926 client meshes)
CANON = [('TEXCOORD', 0x20436, 8),
         ('POSITION_BP', 0x30437, 12),
         ('NORMAL_BP', 0x30437, 12),
         ('BLENDINDICES', 0x40108, 4),
         ('BLENDWEIGHT', 0x30437, 12)]

SYNC_POST_UPDATE, SYNC_VISIBLE, SYNC_RENDER = 0x8020, 0x8030, 0x8040
USE_SOFTWARE_SKINNING, RECOMPUTE_BOUNDS = 0x0001, 0x0002


class R:
    def __init__(s, b, p=0): s.b, s.p = b, p
    def u8(s):  v = s.b[s.p]; s.p += 1; return v
    def u16(s): v = struct.unpack_from('<H', s.b, s.p)[0]; s.p += 2; return v
    def u32(s): v = struct.unpack_from('<I', s.b, s.p)[0]; s.p += 4; return v
    def i32(s): v = struct.unpack_from('<i', s.b, s.p)[0]; s.p += 4; return v
    def take(s, n): v = s.b[s.p:s.p + n]; s.p += n; return v
    def skip(s, n): s.p += n


def parse_header(b):
    nl = b.index(b'\n')
    r = R(b, nl + 1)
    ver = r.u32(); en = r.u8(); uver = r.u32(); nobj = r.i32()
    nt = r.u16()
    types = [r.take(r.u32()) for _ in range(nt)]
    tidx = [r.u16() for _ in range(nobj)]
    sizes = [r.u32() for _ in range(nobj)]
    nstr = r.u32(); maxlen = r.u32()
    strs = [r.take(r.u32()) for _ in range(nstr)]
    ngrp = r.u32(); groups = [r.u32() for _ in range(ngrp)]
    body = r.p
    off, p = [], body
    for i in range(nobj):
        off.append(p); p += sizes[i]
    assert p <= len(b), 'block table overruns file'
    return dict(hdrline=b[:nl + 1], ver=ver, en=en, uver=uver, nobj=nobj,
                types=types, tidx=tidx, sizes=sizes, maxlen=maxlen, strs=strs,
                groups=groups, body=body, off=off, end=p, footer=b[p:])


def build_header(h):
    o = bytearray(h['hdrline'])
    o += struct.pack('<IBIi', h['ver'], h['en'], h['uver'], h['nobj'])
    o += struct.pack('<H', len(h['types']))
    for t in h['types']:
        o += struct.pack('<I', len(t)) + t
    for x in h['tidx']:
        o += struct.pack('<H', x)
    for s in h['sizes']:
        o += struct.pack('<I', s)
    o += struct.pack('<II', len(h['strs']), h['maxlen'])
    for s in h['strs']:
        o += struct.pack('<I', len(s)) + s
    o += struct.pack('<I', len(h['groups']))
    for g in h['groups']:
        o += struct.pack('<I', g)
    return bytes(o)


def tname(h, i):
    t = h['types'][h['tidx'][i] & 0x7FFF]
    return 'NiDataStream' if t.startswith(b'NiDataStream') else t.decode('latin-1').rstrip('\x00')


def S(h, i):
    return h['strs'][i].decode('latin-1').rstrip('\x00') if 0 <= i < len(h['strs']) else None


def sidx(h, name):
    for i, s in enumerate(h['strs']):
        if s.decode('latin-1').rstrip('\x00') == name:
            return i
    return None


def parse_mesh(h, b, i):
    """Return field offsets (absolute, into b) so the block can be spliced."""
    base = h['off'][i]; r = R(b, base)
    r.i32(); r.skip(4 * r.u32()); r.i32(); r.u16(); r.skip(12 + 36 + 4)
    r.skip(4 * r.u32()); r.i32()
    nmat = r.u32()
    mat_off = r.p
    mats = [r.i32() for _ in range(nmat)]
    r.skip(4 * nmat); r.i32(); r.u8()
    r.u32(); r.u16(); r.u8(); r.skip(16)
    st_start = r.p
    ns = r.u32(); streams = []
    for _ in range(ns):
        ref = r.i32(); r.u8(); r.skip(2 * r.u16()); nc = r.u32()
        comps = [(S(h, r.i32()), r.u32()) for _ in range(nc)]
        streams.append((ref, comps))
    st_end = r.p
    nmod = r.u32(); mods = [r.i32() for _ in range(nmod)]
    assert r.p - base == h['sizes'][i], 'NiMesh %d does not consume its declared size' % i
    return dict(base=base, mat_off=mat_off, mats=mats, st_start=st_start,
                st_end=st_end, streams=streams, mods=mods, end=r.p)


def parse_ds(h, b, i):
    base = h['off'][i]; r = R(b, base)
    nb = r.u32(); clone = r.u32(); nreg = r.u32()
    regs = [(r.u32(), r.u32()) for _ in range(nreg)]
    ncf = r.u32(); fmts = [r.u32() for _ in range(ncf)]
    doff = r.p; r.skip(nb); st = r.u8()
    assert r.p - base == h['sizes'][i], 'NiDataStream %d does not consume its declared size' % i
    return dict(nbytes=nb, clone=clone, regs=regs, fmts=fmts, data_off=doff,
                streamable=st, data=b[doff:doff + nb])


def parse_mod(h, b, i):
    base = h['off'][i]; r = R(b, base)
    nsp = r.u32(); sp = [r.u16() for _ in range(nsp)]
    ncp = r.u32(); cp = [r.u16() for _ in range(ncp)]
    flags_off = r.p
    flags = r.u16()
    tail = r.p  # everything from here on is copied verbatim
    r.i32(); r.skip(52); nb = r.u32(); r.skip(4 * nb); r.skip(52 * nb); r.skip(16 * nb)
    assert r.p - base == h['sizes'][i], 'modifier %d does not consume its declared size' % i
    return dict(base=base, sp=sp, cp=cp, flags=flags, flags_off=flags_off,
                tail=tail, nbones=nb, end=r.p)


def ds_block(nbytes_data, clone, regs, fmts, streamable):
    o = bytearray()
    o += struct.pack('<III', len(nbytes_data), clone, len(regs))
    for a, c in regs:
        o += struct.pack('<II', a, c)
    o += struct.pack('<I', len(fmts))
    for f in fmts:
        o += struct.pack('<I', f)
    o += nbytes_data
    o += struct.pack('<B', streamable)
    return bytes(o)


def stream_table(entries):
    """entries = [(blockref, [(stringIdx, componentIndex), ...]), ...]"""
    o = bytearray(struct.pack('<I', len(entries)))
    for ref, comps in entries:
        o += struct.pack('<i', ref)
        o += struct.pack('<B', 0)          # isPerInstance
        o += struct.pack('<H', 1)          # submesh->region map count
        o += struct.pack('<H', 0)          # map[0] = region 0
        o += struct.pack('<I', len(comps))
        for si, ci in comps:
            o += struct.pack('<iI', si, ci)
    return bytes(o)


def main():
    b = open(SRC, 'rb').read()
    src_md5 = hashlib.md5(b).hexdigest()
    h = parse_header(b)
    assert h['ver'] == 0x14060000 and h['en'] == 1 and h['uver'] == 0
    assert all((x & 0x8000) == 0 for x in h['tidx']), 'a block-type index has the high bit set'

    # ---- locate the mesh, its modifier and its streams -------------------
    mi = [i for i in range(h['nobj']) if tname(h, i) == 'NiMesh']
    assert len(mi) == 1, 'expected exactly one NiMesh, got %d' % len(mi)
    mi = mi[0]
    M = parse_mesh(h, b, mi)
    modi = [x for x in M['mods'] if tname(h, x) == 'NiSkinningMeshModifier']
    assert len(modi) == 1
    modi = modi[0]
    MOD = parse_mod(h, b, modi)
    assert MOD['flags'] == (USE_SOFTWARE_SKINNING | RECOMPUTE_BOUNDS), \
        'source modifier flags are 0x%04x, not the expected 0x0003' % MOD['flags']
    assert MOD['sp'] == [SYNC_POST_UPDATE, SYNC_VISIBLE]
    assert MOD['cp'] == [SYNC_RENDER, SYNC_VISIBLE]

    by_sem = {}
    for ref, comps in M['streams']:
        assert len(comps) == 1, 'source mesh is expected to be fully de-interleaved'
        by_sem[comps[0][0]] = ref
    need = ['INDEX', 'POSITION', 'NORMAL'] + [c[0] for c in CANON]
    for s in need:
        assert s in by_sem, 'missing source stream %s' % s

    DS = {ref: parse_ds(h, b, ref) for ref in by_sem.values()}
    idx_ref = by_sem['INDEX']
    nv = DS[by_sem['POSITION_BP']]['regs'][0][1]
    for sem, fmt, w in CANON:
        d = DS[by_sem[sem]]
        assert d['fmts'] == [fmt], '%s format %s != canonical 0x%x' % (sem, d['fmts'], fmt)
        assert d['nbytes'] == nv * w and d['regs'] == [(0, nv)]

    # the deform outputs must be byte-identical to the bind pose -- if they are
    # not, dropping them would lose authored data and the repair must stop.
    assert DS[by_sem['POSITION']]['data'] == DS[by_sem['POSITION_BP']]['data'], \
        'POSITION differs from POSITION_BP -- deform output is not the bind pose'
    assert DS[by_sem['NORMAL']]['data'] == DS[by_sem['NORMAL_BP']]['data'], \
        'NORMAL differs from NORMAL_BP -- deform output is not the bind pose'

    # ---- build the new stream payloads -----------------------------------
    stride = sum(w for _, _, w in CANON)
    assert stride == 48
    src = [(DS[by_sem[s]]['data'], w) for s, _, w in CANON]
    inter = bytearray(nv * stride)
    pos = 0
    for v in range(nv):
        for data, w in src:
            inter[pos:pos + w] = data[v * w:(v + 1) * w]
            pos += w
    assert pos == len(inter)
    inter = bytes(inter)

    nbones = MOD['nbones']
    palette = struct.pack('<%dH' % nbones, *range(nbones))

    # ---- header edits: type table -----------------------------------------
    def find_type(usage, access):
        want = b'NiDataStream\x01%d\x01%d' % (usage, access)
        for i, t in enumerate(h['types']):
            if t == want:
                return i
        return None

    T_GPU_VTX = find_type(1, 18)          # CPU_WRITE_STATIC|GPU_READ, USAGE_VERTEX
    T_CPU_VTX = find_type(1, 3)           # CPU_READ|CPU_WRITE_STATIC, USAGE_VERTEX
    assert T_GPU_VTX is not None and T_CPU_VTX is not None
    T_PAL = find_type(3, 3)               # USAGE_USER, CPU_READ|CPU_WRITE_STATIC
    if T_PAL is None:
        h['types'].append(b'NiDataStream\x013\x013')
        T_PAL = len(h['types']) - 1

    # ---- header edits: string table ---------------------------------------
    for s in ['BONE_PALETTE', NEW_MATERIAL]:
        if sidx(h, s) is None:
            h['strs'].append(s.encode('latin-1'))
    h['maxlen'] = max(len(s) for s in h['strs'])

    # ---- slot assignment (block count and order are unchanged) ------------
    vtx_ref = by_sem['POSITION']          # slot 191 becomes the interleaved stream
    pal_ref = by_sem['NORMAL']            # slot 192 becomes the bone palette
    spare = [by_sem[s] for s, _, _ in CANON]   # 193..197 become orphans

    new_bodies = {}
    new_bodies[vtx_ref] = ds_block(inter, 0, [(0, nv)], [f for _, f, _ in CANON], 1)
    new_bodies[pal_ref] = ds_block(palette, 0, [(0, nbones)], [0x10215], 1)
    for r_ in spare:
        new_bodies[r_] = ds_block(b'', 0, [(0, 0)], [0x30437], 1)

    h['tidx'][vtx_ref] = T_GPU_VTX
    h['tidx'][pal_ref] = T_PAL
    for r_ in spare:
        h['tidx'][r_] = T_CPU_VTX

    # ---- rewrite the NiMesh block by splicing -----------------------------
    mesh_old = b[M['base']:M['end']]
    rel_mat = M['mat_off'] - M['base']
    rel_s0 = M['st_start'] - M['base']
    rel_s1 = M['st_end'] - M['base']
    newtab = stream_table([
        (idx_ref, [(sidx(h, 'INDEX'), 0)]),
        (vtx_ref, [(sidx(h, s), 0) for s, _, _ in CANON]),
        (pal_ref, [(sidx(h, 'BONE_PALETTE'), 0)]),
    ])
    mesh_new = bytearray(mesh_old)
    struct.pack_into('<i', mesh_new, rel_mat, sidx(h, NEW_MATERIAL))
    mesh_new = bytes(mesh_new[:rel_s0]) + newtab + bytes(mesh_new[rel_s1:])
    new_bodies[mi] = mesh_new

    # ---- rewrite the modifier block by splicing ---------------------------
    mod_old = b[MOD['base']:MOD['end']]
    prefix = (struct.pack('<I', 1) + struct.pack('<H', SYNC_POST_UPDATE) +
              struct.pack('<I', 1) + struct.pack('<H', SYNC_RENDER) +
              struct.pack('<H', RECOMPUTE_BOUNDS))
    new_bodies[modi] = prefix + mod_old[MOD['tail'] - MOD['base']:]

    # ---- reassemble --------------------------------------------------------
    bodies = []
    for i in range(h['nobj']):
        blk = new_bodies.get(i, b[h['off'][i]:h['off'][i] + h['sizes'][i]])
        bodies.append(blk)
        h['sizes'][i] = len(blk)
    out = build_header(h) + b''.join(bodies) + h['footer']

    os.makedirs(OUTDIR, exist_ok=True)
    open(OUT, 'wb').write(out)

    rep = dict(source=SRC, source_md5=src_md5, source_size=len(b),
               output=OUT, output_md5=hashlib.md5(out).hexdigest(),
               output_size=len(out), nverts=nv, nbones=nbones,
               mesh_block=mi, modifier_block=modi,
               index_stream=idx_ref, interleaved_stream=vtx_ref,
               palette_stream=pal_ref, orphan_streams=spare,
               changed_blocks=sorted(new_bodies), stride=stride,
               material_old=OLD_MATERIAL, material_new=NEW_MATERIAL,
               flags_old='0x%04x' % MOD['flags'], flags_new='0x%04x' % RECOMPUTE_BOUNDS)
    open(os.path.join(OUTDIR, 'repair_report.json'), 'w').write(json.dumps(rep, indent=2))
    for k, v in rep.items():
        print('%-20s %s' % (k, v))


if __name__ == '__main__':
    main()
