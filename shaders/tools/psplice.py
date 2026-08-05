#!/usr/bin/env python3
"""PS-splice: replace the PIXEL-shader bytecode blobs inside a D3D9 fx_2_0 effect (.fxo)
while leaving the VERTEX shaders (GPU-skinning) byte-identical.

Effect layout (from DXDecompiler FX9): shaders live in the footer as a sequence of
StateBlobs [5 DWORD meta][blobSize][data padded/4], linked to passes by index, NOT by
absolute offset. So a PS blob can be rewritten in place as long as it stays the SAME byte
size (blobSize unchanged) -> nothing shifts, no offset table to fix, VS blobs untouched.
A shorter modern PS is padded up to the exact target size with a D3D9 comment token
(0xFFFE|len<<16) inserted before the END token (runtime skips comments).

Usage:
  psplice.py list       <in.fxo>
  psplice.py splice     <in.fxo> <newps.bin> <out.fxo> [--min 1000]  # replace PS blobs, SAME size (pad)
  psplice.py growsplice <in.fxo> <newps.bin> <out.fxo> [--min 1000]  # replace PS blobs, ANY size (resize)

growsplice resizes each target PS blob to exactly len(newps): it rewrites the blobSize DWORD
(at off-4) and replaces the data; everything after shifts. SAFE because blobs are footer,
index-linked (no absolute-offset table) and past the header creation-data field, and VS blobs
are only relocated (their bytes stay byte-identical -> GPU skinning intact). Blobs are grown in
reverse offset order so the tail shift never invalidates a not-yet-processed offset.
"""
import struct, sys

VS_HI, PS_HI = 0xFFFE, 0xFFFF
VERS = {0x0100,0x0101,0x0102,0x0103,0x0104,0x0200,0x0201,0x0202,0x0203,0x0300,0x0301}
END = b"\xFF\xFF\x00\x00"

def find_blobs(d):
    """Locate shader bytecode blobs by version token; blobSize is the DWORD at off-4."""
    blobs=[]; i=0; n=len(d)
    while i<=n-4:
        w=struct.unpack_from('<I',d,i)[0]
        hi,lo=w>>16, w&0xFFFF
        if hi in (VS_HI,PS_HI) and lo in VERS:
            size=struct.unpack_from('<I',d,i-4)[0] if i>=4 else -1
            # sanity: blob of `size` bytes must end at a 4-byte boundary within the file and
            # contain an END token as its last 4 bytes
            if 0 < size <= n-i and size%4==0 and d[i+size-4:i+size]==END:
                kind='PS' if hi==PS_HI else 'VS'
                blobs.append({'off':i,'size':size,'kind':kind,'ver':f"{lo>>8}_{lo&0xFF}",
                              'meta':struct.unpack_from('<5I',d,i-24) if i>=24 else None})
                i+=size; continue
        i+=4
    return blobs

def pad_shader(code, target):
    """Pad a shader bytecode (ending in END) up to `target` bytes with a comment token."""
    cur=len(code)
    if cur==target: return code
    if cur>target: raise ValueError(f"new PS {cur} > target {target}; cannot fit")
    need=target-cur
    if need%4: raise ValueError("target not 4-aligned vs shader")
    ndw=need//4 - 1                      # comment token (1 dw) + ndw payload dwords = need bytes
    if ndw<0: raise ValueError("need>=4")
    tok=struct.pack('<I',((ndw<<16)|0xFFFE))
    padded=code[:-4] + tok + (b"\x00"*(ndw*4)) + code[-4:]   # comment inserted before END
    assert len(padded)==target
    return padded

def main():
    cmd=sys.argv[1]; d=bytearray(open(sys.argv[2],'rb').read())
    blobs=find_blobs(d)
    if cmd=='list':
        for b in blobs:
            m=b['meta']; tag=f"tech={m[0]} pass={m[1]} asn={m[3]} type={m[4]}" if m else ""
            print(f"  {b['kind']} ps_{b['ver']}  off=0x{b['off']:06X} size={b['size']:5d}  {tag}")
        print(f"  -> {sum(x['kind']=='VS' for x in blobs)} VS, {sum(x['kind']=='PS' for x in blobs)} PS")
        return
    if cmd=='splice':
        newps=open(sys.argv[3],'rb').read(); out=sys.argv[4]
        mn=int(sys.argv[sys.argv.index('--min')+1]) if '--min' in sys.argv else 1000
        if not newps.endswith(END): raise ValueError("new PS must be raw D3D9 bytecode ending in END token")
        rep=0
        for b in blobs:
            if b['kind']=='PS' and b['size']>=mn:
                d[b['off']:b['off']+b['size']]=pad_shader(newps,b['size'])
                rep+=1
        open(out,'wb').write(d)
        print(f"spliced {rep} PS blob(s) >= {mn} B; VS blobs untouched -> {out} ({len(d)} B)")
        return
    if cmd=='growsplice':
        newps=open(sys.argv[3],'rb').read(); out=sys.argv[4]
        mn=int(sys.argv[sys.argv.index('--min')+1]) if '--min' in sys.argv else 1000
        if not newps.endswith(END): raise ValueError("new PS must be raw D3D9 bytecode ending in END token")
        if len(newps)%4: raise ValueError("new PS not 4-byte aligned")
        targets=[b for b in blobs if b['kind']=='PS' and b['size']>=mn]
        # reverse offset order: growing a blob shifts everything after it, so lower offsets must
        # be processed last (they stay valid until then).
        for b in sorted(targets, key=lambda x:x['off'], reverse=True):
            off=b['off']
            d[off-4:off]=struct.pack('<I',len(newps))     # rewrite blobSize DWORD
            d[off:off+b['size']]=newps                     # replace data (resizes the buffer)
        open(out,'wb').write(d)
        print(f"growspliced {len(targets)} PS blob(s) >= {mn} B to {len(newps)} B each; "
              f"VS bytes untouched -> {out} ({len(d)} B)")

if __name__=='__main__': main()
