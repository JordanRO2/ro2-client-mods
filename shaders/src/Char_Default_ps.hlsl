// Char_Default MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// PS-splice only (VS blobs kept byte-identical -> GPU skinning intact). Global param NAMES
// match the char effect globals so name-binding + preshader stay compatible. Samplers:
//   s0 = BaseSampler, s1 = ToonSampler.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB tops far below,
// so the runtime never uploads there). The DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) each frame. When these registers are 0
// (DLL not pushing yet), master enable = 0 and the output is BYTE-IDENTICAL to the original
// ramp toon -> safe drop-in / A-B toggle.
//
// ============================================================================================
// SPECULAR: corrected 2026-08-03. The previous note that "Char_Default has no mask map, so it
// computes no specular and any lobe is invented" is WRONG, and the disassembly says so.
//
// Evidence, from the PRISTINE stock blobs (scratchpad/aud2/stock__Char_Default__*.asm):
//   * Char_Default ships THREE distinct lit PS. Two of them — 0d04e2ae (51 slots) and
//     1c349e4c (54 slots) — contain this exact tail:
//         add   r4.xyz, t0, c8        ; H = V + L        (c8 = Light0_WorldViewDir)
//         nrm   r5.xyz, r4
//         dp3   r0.w,  r5, r2         ; H.N              (r2 = normalize(t1))
//         max   r1.w,  r0.w, c12.x    ; max(H.N, 0)
//         pow   r0.w,  r1.w, c13.y    ; ^16
//         mul   r0.w,  r0.w, c13.z    ; *0.2
//         mul   r2.xyz, r0.w, r1      ; * (litColor*col)
//         mad_sat r1.xyz, r1, r0.w, r0            ; branch A: no Light0_Spec
//         mad_sat r0.xyz, r2, c7,  r0             ; branch B: * Light0_Spec (c7)
//         cmp   r0.xyz, -c0.x, r0, r1             ; g_LightProbeMode==0 ? B : A
//     The third (f7f847d6, 36 slots) has no pow at all. The 6-slot 28129009 is the Z-only pass.
//   * That is BYTE-FOR-BYTE the same sequence as Char_Specular's masked lobe
//     (stock__Char_Specular__038cdd31.asm), with only the two mask-map reads swapped for
//     literals:  exponent glow.w*64 -> 16,  mask glow.y -> 0.2.
//     So Char_Default is not "a shader without specular"; it is the SAME lighting model
//     running on default constants because it has no mask map to read them from.
//   * The pow here is NOT the fog term. Fog in this shader is a plain lrp against c5.
//
// What was actually wrong with the old code was the TINT, not the existence of the lobe:
//   old:   col += pow(N.H, 2^lerp(7,2,rough)) * gloss * lerp(lightColor, col, metallic)
//          with the Skin class fixed at gloss 0.08 / rough 0.62 / metallic 0.00, i.e. a
//          neutral highlight of constant intensity laid over dark albedo -> plastic wrap.
//   stock: col += pow(N.H, 16) * 0.2 * (litColor * col)
//          the lobe inherits the surface's own lit albedo, so dark matte gear gets a dark
//          highlight. That albedo modulation is per-texel real data (BaseSampler), not a
//          heuristic, and it is the game's own choice.
// Fix: drop the invented lobe, restore the stock one verbatim (ungated — see below).
//
// Side effect worth knowing: gating the lobe behind g_toon0.x meant that at enable=0 this
// shader was stock MINUS stock's specular, so the advertised "byte-identical A/B fallback"
// was not true. It is true again now.
//
// UNRESOLVED (deliberately neutralised, not answered): whether the engine ever writes a
// non-zero Light0_Spec at runtime. The effect's stored default is 0,0,0,0 — but so is
// Light0_Diff's, and that one demonstrably drives the visible rim, so a 0 default proves
// nothing. By restoring BOTH stock branches and the stock select, we render whatever stock
// renders either way; no behaviour of ours depends on the answer.
// ============================================================================================
float4 Light0_PreCalcLightColor;
float3 g_DarkMapColorForChar;
int    g_LightProbeMode;
bool   g_bDarkMapColor;
float  g_fDarkMapColorWeight;
int    g_iHighlightOutline;
float3 Light0_WorldViewDir;
float  fSHPower;
float4 Light0_Diff;
float4 Light0_Spec;                 // restored: the stock PS uses this (c7 in the stock blob)
float4 MaterialColor;
float3 g_FogColor;
sampler2D BaseSampler : register(s0);
sampler2D ToonSampler : register(s1);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
// c216/c217 = the "Skin" material class (DefaultRag2ShaderSkin, ~794 meshes).
// Only .w of g_mat0 (warmShadow) is still read — .x/.y/.z (gloss/roughness/metallic) drove
// the invented specular lobe that was removed; specular now comes from the stock math.
float4 g_mat0 : register(c216); // Skin: [gloss, roughness, metallic UNUSED], warmShadow
float4 g_mat1 : register(c217); // Skin: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 (view dir, .w = fog)
    float4 texcoord1 : TEXCOORD1;   // t1 (normal, .w = depth)
    float2 texcoord2 : TEXCOORD2;   // t2 (uv)
    float3 color     : COLOR0;      // v0 (vertex colour / SH)
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;
    // uniform-only (fxc folds into a preshader, like the original)
    float  po0 = g_LightProbeMode * g_LightProbeMode;
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));
    float  po2 = g_iHighlightOutline * g_iHighlightOutline;
    float  pr  = (float)g_bDarkMapColor - 1.0;
    float  po3 = pr * pr;
    float3 po4 = g_fDarkMapColorWeight * g_DarkMapColorForChar;

    float4 base = tex2D(BaseSampler, i.texcoord2);
    float t1w = i.color.y * 0.66;
    float t1x = i.color.x * 0.34 + t1w;
    t1x = -t1x + 0.8;
    float t2w = max(t1x, 0.0);
    float diffScale = t2w * fSHPower + 1.0;
    float diffBias  = t2w * 0.3 + 0.1;
    float3 lightVtx = saturate(i.color * diffScale + diffBias);

    float3 N = normalize(i.texcoord1.xyz);
    float ndl = dot(Light0_WorldViewDir.xyz, N);
    float rim = saturate(dot(i.texcoord.xyz, N));
    rim = -rim + 1.0;
    rim = rim * rim; rim = rim * rim;            // (1 - N.V)^4  edge factor
    float3 rimCol = rim * Light0_Diff.xyz;

    float2 toonUV = ndl * 0.5 + 0.5;
    float4 toon = tex2D(ToonSampler, toonUV);

    // scene light colour: probe vs per-vertex SH (matches the original select)
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;

    // ORIGINAL ramp-toon term (fallback when enable = 0, i.e. byte-identical default)
    float  toonRamp = toon.x;
    float3 litOrig  = lightColor * toonRamp;

    // NEW toon: stepped shade bands over a half-Lambert wrap (UTS2 double-shade)
    float  hl    = ndl * 0.5 + 0.5;                                   // 0..1
    float  maskA = saturate((g_toon0.z - hl) / max(g_toon0.w, 1e-4)); // base <-> 1st (1=shadow)
    float  maskB = saturate((g_toon1.x - hl) / max(g_toon1.y, 1e-4)); // 1st  <-> 2nd
    float3 shade1 = lightColor * g_toon2.xyz;                         // 1st-shade tint
    float3 shade2 = shade1     * g_toon3.xyz;                         // 2nd-shade (darken 1st)
    float3 banded = lerp(lightColor, lerp(shade1, shade2, maskB), maskA);

    // NEW realistic: smooth half-Lambert over the same palette
    float3 smoothD = lerp(shade2, lightColor, hl);
    float3 newLit  = lerp(banded, smoothD, g_toon0.y);               // realisticMix

    // master enable: 0 => original ramp exactly
    float3 litColor = lerp(litOrig, newLit, g_toon0.x);

    float3 col = base.xyz * litColor;
    float alpha = base.w * MaterialColor.w;

    // highlight-outline additive rim (g_iHighlightOutline gate — original, kept)
    float3 colRim = rimCol * 0.4 + col;           // original additive highlight rim (0.4)
    col = (-po2 >= 0) ? colRim : col;

    // ---- STOCK specular, restored verbatim (see header note) ----------------------
    // This is the same instruction sequence as Char_Specular's masked lobe, with the
    // two mask-map inputs replaced by the stock's own literals:
    //     Char_Specular : pow(max(H.N,0), glow.w*64) * glow.y
    //     Char_Default  : pow(max(H.N,0), 16       ) * 0.2
    // and, in BOTH, the lobe is tinted by litColor*col — the surface's own lit albedo.
    // That albedo tint is the whole reason stock matte gear does not read as plastic,
    // and it is exactly what the removed invented lobe was missing.
    // NOT gated by g_toon0.x: stock applies it unconditionally, so gating it is what
    // made enable=0 differ from stock.
    float3 specBase  = litColor * col;                                  // stock r1 = litColor * col
    float3 H         = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
    float  hn        = max(dot(H, N), 0.0);
    float  spec      = pow(hn, 16.0) * 0.2;
    float3 colNoTint = saturate(specBase * spec + col);                 // non-probe branch
    float3 colSpec   = saturate(specBase * spec * Light0_Spec.xyz + col);  // probe branch
    col = (-po0 >= 0) ? colSpec : colNoTint;

    // dark-map darken + probe re-select (stock order: both come BEFORE any extra term,
    // because the re-select restores colNoTint and would discard anything added earlier)
    float3 dark = col * po4;
    float3 darkMin = min(col, dark);
    col = (-po3 >= 0) ? darkMin : col;
    col = (-po0 >= 0) ? col : colNoTint;          // stock probe re-select (was a no-op here)

    // --- fresnel rim + warm shadow, gated by master enable ---
    // Kept: both are driven by real per-pixel geometry (N.V) / the existing shade term,
    // and stock already carries a fresnel rim of its own (rimCol*0.4 above). The
    // gloss/roughness/metallic channels (g_mat0.x/.y/.z) are NO LONGER read for spec —
    // this shader has no gloss or spec-mask texture to justify them (samplers are only
    // s0 BaseSampler + s1 ToonSampler; the effect's GlossMap param is never bound to a
    // sampler this PS declares). Spec now comes from the stock lobe above.
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    // ---- SAME LIGHTING MODEL AS THE MASK-CARRYING CHAR SHADERS -------------------
    // WHY THIS EXISTS: the head uses Char_Face and the body/neck uses this shader. Char_Face
    // adds a fresnel-modulated, N.L-gated specular layer on top of its stock lobe. If this
    // shader only carries the stock lobe, the two halves of the SAME SKIN are lit by two
    // different models and the difference is visible as a seam at the neck -- which is exactly
    // what the operator reported. So the model must be identical here; only the DATA differs.
    //
    // This shader has no mask map (samplers are s0 Base + s1 Toon only), so its authored
    // values are the stock literals the lobe above already uses: power 16, mask 0.2. Those
    // are the same two numbers Char_Specular reads from glow.w*64 and glow.y -- the stock
    // sequences are otherwise byte-for-byte identical -- so using them here is restoring the
    // artist's intent for an unmasked material, not inventing one.
    float  _pmMask = 0.2;                                            // stock literal (= glow.y)
    float  _pmExp  = max(16.0 * exp2(lerp(1.0, -1.0, saturate(g_mat0.y))), 1.0);  // (= glow.w*64)
    float  _pmNdH  = max(dot(H, N), 0.0);
    float  _pmNdL  = saturate(ndl);                                  // no highlight on the dark side
    float  _pmVdH  = saturate(dot(normalize(i.texcoord.xyz), H));
    float  _pmF0   = lerp(0.04, 1.0, saturate(g_mat0.z));            // dielectric 4% -> metal 100%
    float  _pmFc   = 1.0 - _pmVdH;
    float  _pmFc2  = _pmFc * _pmFc;
    float  _pmFres = min(1.0 + ((1.0 - _pmF0) / _pmF0) * (_pmFc2 * _pmFc2 * _pmFc), 2.0);
    float  _pmSpec = pow(_pmNdH, _pmExp) * _pmFres * _pmNdL * g_mat0.x * _pmMask;
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));      // metallic -> tint by surface
    // saturate to match stock, which adds its specular with mad_sat on BOTH branches.
    col = saturate(col + (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x);

    // --- saturation grade (enable-gated so default sat effectively = 1) ---
    float sat = lerp(1.0, g_toon3.w, g_toon0.x);
    float lum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(lum.xxx, col, sat);

    // (No ddx/ddy ink outline here — screen-space normal derivatives faceted the
    //  model per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    col = lerp(col, g_FogColor, i.texcoord.w);

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
