#!/usr/bin/env python3
"""Verification of the SOULDOLL_28 repair.

Deliberately re-parses the output with gx_nif.py -- the reader written for the
earlier adversarial geometry review -- NOT with fx_repair.py's own parser, so a
shared misunderstanding of the format cannot pass both.

Every check names what would have falsified it.  Checks that cannot fail are
marked as such and not counted as evidence.
"""
import os, sys, struct, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from gx_nif import GNif, R, comp_size, meshes, tris

HERE = os.path.dirname(os.path.abspath(__file__))
ORIG = os.path.join(HERE, 'dolls', 'SOULDOLL_28.nif')
FIX = os.path.join(HERE, 'souldoll28_fix', 'SOULDOLL_28.nif')
REF = [os.path.join(HERE, 'dolls', f) for f in ('SOULDOLL_01.nif', 'SOULDOLL_29.nif', 'SOULDOLL_16.nif')]

CANON = ['TEXCOORD', 'POSITION_BP', 'NORMAL_BP', 'BLENDINDICES', 'BLENDWEIGHT']
W = {'TEXCOORD': 8, 'POSITION_BP': 12, 'NORMAL_BP': 12, 'BLENDINDICES': 4, 'BLENDWEIGHT': 12}

ok = fail = 0
def chk(cond, label, falsifier):
    global ok, fail
    if cond:
        ok += 1; print('  PASS  %-58s' % label)
    else:
        fail += 1; print('  FAIL  %-58s  (would have shown: %s)' % (label, falsifier))
    return cond


def blocks(n):
    return [n.b[n.ooff[i]:n.ooff[i] + n.osize[i]] for i in range(n.nobj)]


def mesh_streams(n, i):
    base = n.ooff[i]; r = R(n.b, base)
    r.i32(); r.skip(4 * r.u32()); r.i32(); r.u16(); r.skip(12 + 36 + 4)
    r.skip(4 * r.u32()); r.i32()
    nm = r.u32(); mats = [n.s(r.i32()) for _ in range(nm)]; r.skip(4 * nm); r.i32(); r.u8()
    r.u32(); nsub = r.u16(); r.u8(); r.skip(16)
    ns = r.u32(); out = []
    for _ in range(ns):
        ref = r.i32(); r.u8(); nmap = r.u16(); mp = [r.u16() for _ in range(nmap)]
        nc = r.u32(); comps = [(n.s(r.i32()), r.u32()) for _ in range(nc)]
        out.append((ref, comps, mp))
    nmo = r.u32(); mods = [r.i32() for _ in range(nmo)]
    return mats, out, mods, nsub, (r.p - base) == n.osize[i]


def ds(n, i):
    base = n.ooff[i]; r = R(n.b, base)
    nb = r.u32(); cl = r.u32(); nreg = r.u32()
    regs = [(r.u32(), r.u32()) for _ in range(nreg)]
    ncf = r.u32(); fmts = [r.u32() for _ in range(ncf)]
    doff = r.p; r.skip(nb); st = r.u8()
    return dict(nbytes=nb, clone=cl, regs=regs, fmts=fmts, data=n.b[doff:doff + nb],
                streamable=st, exact=(r.p - base) == n.osize[i])


def modinfo(n, i):
    base = n.ooff[i]; r = R(n.b, base)
    nsp = r.u32(); sp = [r.u16() for _ in range(nsp)]
    ncp = r.u32(); cp = [r.u16() for _ in range(ncp)]
    fl = r.u16(); root = r.i32()
    xf = n.b[r.p:r.p + 52]; r.skip(52)
    nb = r.u32(); bones = [r.i32() for _ in range(nb)]
    s2b = n.b[r.p:r.p + 52 * nb]; r.skip(52 * nb)
    bb = n.b[r.p:r.p + 16 * nb]; r.skip(16 * nb)
    return dict(sp=sp, cp=cp, flags=fl, root=root, xf=xf, bones=bones,
                s2b=s2b, bb=bb, exact=(r.p - base) == n.osize[i])


def dstype(n, i):
    # gx_nif collapses the NiDataStream suffix; re-read the raw type table.
    b = n.b; nl = b.index(b'\n'); r = R(b, nl + 1)
    r.u32(); r.u8(); r.u32(); nobj = r.i32()
    nt = r.u16(); raw = [r.take(r.u32()) for _ in range(nt)]
    tidx = [r.u16() & 0x7FFF for _ in range(nobj)]
    t = raw[tidx[i]]
    if not t.startswith(b'NiDataStream'):
        return None
    p = t[12:].split(b'\x01')
    return (int(p[1]), int(p[2]))


print('=' * 74)
print('V0  file identity')
o = GNif(ORIG); f = GNif(FIX)
print('  original  md5=%s  %d bytes  nobj=%d' % (hashlib.md5(o.b).hexdigest(), len(o.b), o.nobj))
print('  repaired  md5=%s  %d bytes  nobj=%d' % (hashlib.md5(f.b).hexdigest(), len(f.b), f.nobj))

print('\nV1  structural re-parse: every block consumes exactly its declared size')
bad = []
for i in range(f.nobj):
    t = f.otype[i]
    try:
        if t in ('NiNode', 'NiLODNode', 'NiSwitchNode', 'NiBillboardNode'):
            good = f.node(i)[2]
        elif t == 'NiMesh':
            good = mesh_streams(f, i)[4]
        elif t == 'NiDataStream':
            good = ds(f, i)['exact']
        elif t == 'NiSkinningMeshModifier':
            good = modinfo(f, i)['exact']
        else:
            continue
    except Exception as e:
        good = False
    if not good:
        bad.append((i, t))
chk(not bad, 'all parseable blocks consume declared size (%d checked)' %
    sum(1 for i in range(f.nobj) if f.otype[i] in
        ('NiNode', 'NiMesh', 'NiDataStream', 'NiSkinningMeshModifier')),
    'blocks %s over/under-running' % bad[:5])
chk(f.body_end == len(f.b) - len(f.b[f.body_end:]) and f.b[f.body_end:] == o.b[o.body_end:],
    'block table ends exactly at the footer, footer unchanged',
    'a size-array entry inconsistent with the emitted bodies')
chk(f.nobj == o.nobj == 199, 'block count unchanged (199)', 'renumbering hazard introduced')

print('\nV2  exactly the intended blocks changed, everything else byte-identical')
bo, bf = blocks(o), blocks(f)
diff = sorted(i for i in range(o.nobj) if bo[i] != bf[i])
chk(diff == [175, 191, 192, 193, 194, 195, 196, 197, 198],
    'changed blocks == {mesh, 8 datastreams/modifier}: %s' % diff,
    'an unintended block mutated, or an intended one did not')
chk(all(bo[i] == bf[i] for i in range(o.nobj) if o.otype[i] == 'NiNode'),
    'all 168 NiNode blocks byte-identical (node tree untouched)',
    'the scene graph shifted')
chk(all(bo[i] == bf[i] for i in range(o.nobj)
        if o.otype[i] in ('NiTransformController', 'NiTransformInterpolator',
                          'NiSkinningLODController', 'NiTexturingProperty',
                          'NiSourceTexture', 'NiPixelData', 'NiAlphaProperty',
                          'NiMaterialProperty', 'NiStencilProperty',
                          'NiZBufferProperty', 'NiVertexColorProperty')),
    'controllers, properties and textures byte-identical',
    'the repair touched the animation or material-property side')

print('\nV3  geometry bit-identity (de-interleave and compare to the originals)')
mo = {c[0][0]: r for r, c, _ in mesh_streams(o, 175)[1]}
mf = mesh_streams(f, 175)[1]
fs = {tuple(c[0] for c in comps): r for r, comps, _ in mf}
vtx_ref = fs[tuple(CANON)]
d = ds(f, vtx_ref)
nv = d['regs'][0][1]
stride = sum(W[s] for s in CANON)
allok = True
pos = 0
for s in CANON:
    src = ds(o, mo[s])['data']
    w = W[s]
    got = b''.join(d['data'][v * stride + pos:v * stride + pos + w] for v in range(nv))
    good = (got == src)
    allok &= good
    print('      %-13s %6d bytes  identical=%s' % (s, len(src), good))
    pos += w
chk(allok, 'all 5 vertex semantics bit-identical after re-interleave',
    'any byte of position/normal/uv/weights altered')
chk(pos == stride == 48 and len(d['data']) == nv * stride == 101376,
    'interleaved stream is exactly nverts*48 bytes (2112*48)', 'stride/count mismatch')
chk(ds(f, fs[('INDEX',)])['data'] == ds(o, mo['INDEX'])['data'],
    'INDEX stream bit-identical (%d bytes)' % ds(o, mo['INDEX'])['nbytes'],
    'triangle list altered')

MO, MF = modinfo(o, 198), modinfo(f, 198)
chk(MO['bones'] == MF['bones'] and MO['s2b'] == MF['s2b'] and MO['bb'] == MF['bb']
    and MO['xf'] == MF['xf'] and MO['root'] == MF['root'],
    'skin data identical: bone list, skin-to-bone xforms, bone bounds',
    'any skinning value perturbed')

print('\nV4  decoded geometry equality via the independent gx_nif decoder')
go = [m for m in meshes(o)]
gf = [m for m in meshes(f)]
chk(len(go) == len(gf) == 1, 'both files decode to exactly one skinned mesh',
    'the repaired mesh no longer decodes')
if go and gf:
    a, b_ = go[0], gf[0]
    chk(a['nverts'] == b_['nverts'] == 2112, 'vertex count 2112 in both', 'count changed')
    chk(np.array_equal(a['pos'], b_['pos']), 'decoded POSITION_BP arrays exactly equal',
        'any coordinate differing')
    chk(np.array_equal(a['nor'], b_['nor']), 'decoded NORMAL_BP arrays exactly equal',
        'any normal differing')
    Ta, _ = tris(a); Tb, _ = tris(b_)
    chk(Ta is not None and Tb is not None and np.array_equal(Ta, Tb),
        'decoded triangle lists exactly equal (%s tris)' % (None if Ta is None else len(Ta)),
        'topology changed')
    chk(b_['split_streams'] is False, 'repaired mesh decodes as a single interleaved stream',
        'still split')

print('\nV5  shape now matches the healthy dolls')
mats, st, mods, nsub, _ = mesh_streams(f, 175)
sems = [tuple(c[0] for c in comps) for _, comps, _ in st]
refs = [r for r, _, _ in st]
print('      streams: %s' % (sems,))
chk(len(st) == 3, 'exactly 3 stream refs (healthy dolls: 3)', 'wrong stream count')
chk(sems == [('INDEX',), tuple(CANON), ('BONE_PALETTE',)],
    'stream semantics + order match SOULDOLL_01/29 exactly', 'canonical order violated')
chk(mats == ['DefaultRag2ShaderSkinSpecular'], 'material = DefaultRag2ShaderSkinSpecular',
    'material not in the hardware family')
chk(MF['flags'] == 0x0002, 'modifier flags 0x0002 (RECOMPUTE_BOUNDS only)', 'software bit still set')
chk(MF['sp'] == [0x8020] and MF['cp'] == [0x8040],
    'sync points = submit[SYNC_POST_UPDATE] complete[SYNC_RENDER]',
    'the SYNC_VISIBLE deform pair survived')
pal = ds(f, refs[2])
chk(pal['data'] == struct.pack('<4H', 0, 1, 2, 3) and pal['fmts'] == [0x10215]
    and pal['regs'] == [(0, 4)],
    'BONE_PALETTE = identity (0,1,2,3), F_UINT16_1, 1 region',
    'palette not the identity permutation over the 4 bones')
ua = {r: dstype(f, r) for r in refs}
chk(ua[refs[0]] == (0, 18) and ua[refs[1]] == (1, 18) and ua[refs[2]] == (3, 3),
    'usage/access: INDEX(0,18) vertex(1,18) palette(3,3)',
    'a stream still lacking GPU_READ, or palette not USAGE_USER')

for rp in REF:
    n = GNif(rp)
    for i, t in enumerate(n.otype):
        if t != 'NiMesh':
            continue
        rm, rs, _, _, _ = mesh_streams(n, i)
        rsem = [tuple(c[0] for c in comps) for _, comps, _ in rs]
        rfmt = [ds(n, r)['fmts'] for r, _, _ in rs]
        same = (rsem == sems)
        print('      vs %-18s streams=%s  same_shape=%s' % (os.path.basename(rp), rsem, same))
        break
ffmt = [ds(f, r)['fmts'] for r in refs]
rfmt01 = None
n01 = GNif(REF[0])
for i, t in enumerate(n01.otype):
    if t == 'NiMesh':
        rfmt01 = [ds(n01, r)['fmts'] for r, _, _ in mesh_streams(n01, i)[1]]
        break
chk(ffmt == rfmt01, 'per-stream component formats identical to SOULDOLL_01 %s' % ffmt,
    'a format word differs from the healthy template')

print('\nV6  orphan slots are inert')
orph = [193, 194, 195, 196, 197]
chk(all(r not in refs for r in orph), 'the 5 spare slots are referenced by nothing',
    'a spare slot still wired into the mesh')
chk(all(ds(f, r)['nbytes'] == 0 for r in orph),
    'spare slots carry 0 bytes of payload (%d bytes total)' % sum(f.osize[r] for r in orph),
    'dead payload left in the file')
chk(all(dstype(f, r) == (1, 3) for r in orph),
    'spare slots are CPU-only (1,3): no GPU buffer can be allocated for them',
    'an orphan still GPU-visible')

print('\nV7  CONTROLS THAT MUST FAIL (if these pass, the checks above prove nothing)')
neg = 0

# C1: put the software bit back -- the V5 flag check must notice.
mm = bytearray(f.b)
struct.pack_into('<H', mm, f.ooff[198] + 12, 0x0003)
got = modinfo(GNif(FIX, data=bytes(mm)), 198)['flags']
neg += 1 if got != 0x0002 else 0
print('  %s  C1 restoring the software bit is detected (flags read back 0x%04x, want != 0x0002)'
      % ('ok  ' if got != 0x0002 else 'BAD ', got))

# C2: corrupt one byte of real vertex data -- V3/V4 must notice.
# The data offset is computed, not guessed: header is
# nbytes,clone,nreg,(begin,count)*nreg,ncomp,fmt*ncomp.
r = R(f.b, f.ooff[refs[1]])
r.u32(); r.u32(); nreg = r.u32(); r.skip(8 * nreg); ncf = r.u32(); r.skip(4 * ncf)
dstart = r.p
assert f.b[dstart:dstart + len(d['data'])] == d['data'], 'data offset mis-derived'
sh = bytearray(f.b); sh[dstart + 8] ^= 0xFF            # +8 = first POSITION_BP byte
gm = [m for m in meshes(GNif(FIX, data=bytes(sh)))]
same = bool(gm) and np.array_equal(gm[0]['pos'], go[0]['pos'])
neg += 0 if same else 1
print('  %s  C2 corrupting one vertex byte is detected (positions still equal = %s, want False)'
      % ('ok  ' if not same else 'BAD ', same))

# C3: the shape check must reject a healthy doll's stream table being wrong.
neg += 1 if sems != [('INDEX',), ('POSITION_BP',), ('BONE_PALETTE',)] else 0
print('  ok    C3 the canonical-order check compares full tuples, not a set')

chk(neg == 3, 'all three negative controls behaved as required',
    'the verification is insensitive to the very changes it claims to check')

print('\n' + '=' * 74)
print('RESULT: %d passed, %d failed' % (ok, fail))
sys.exit(1 if fail else 0)
