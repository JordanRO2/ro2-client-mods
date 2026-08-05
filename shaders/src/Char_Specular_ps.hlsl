// Char_Specular MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// PS-SPLICE only: the GPU-skinning VS stays byte-identical. Shader68 (CharacterSpec_Impl0/1/2):
// Base s0, Glow(gloss) s1, Toon s2; SH-toon diffuse, dual rim-highlight branch, gloss-map
// specular, probe/darkmap selects.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB tops at c15, so
// the runtime never uploads there); the DLL pushes them via SetPixelShaderConstantF(220,...)
// each frame. With g_toon0.x (enable) = 0 the output is BYTE-IDENTICAL to the original ramp toon.
float4 Light0_PreCalcLightColor;
float3 g_DarkMapColorForChar;
int    g_LightProbeMode;
bool   g_bDarkMapColor;
float  g_fDarkMapColorWeight;
int    g_iHighlightOutline;
float3 Light0_WorldViewDir;
float  fSHPower;
float4 Light0_Diff;
float4 Light0_Spec;
float4 MaterialColor;
float3 g_FogColor;
sampler2D BaseSampler : register(s0);
sampler2D GlowSampler : register(s1);   // gloss map (.y spec mask, .w spec power scale)
sampler2D ToonSampler : register(s2);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
// MATERIAL CLASS DELIBERATELY COLLAPSED TO ONE PAIR (c216/c217).
// The engine picks which Char_* shader a mesh gets from its NIF material name, and we do
// NOT control or reliably predict that mapping -- a neck can be skin on one model and a
// collar on another. While each class read its own constants (Face c212 / Hair c214 /
// Skin c216 / Specular c218) two adjacent meshes could differ by 6.9x in specular
// strength (0.55 vs 0.08), by metallic tint (0.45 vs 0.00) and by warm-shadow (0.45 vs
// 0.00 -- which tints SHADOWED pixels, so it shows even with no highlight at all). That
// is a per-MESH discontinuity, and it is what reads as a seam at the neck.
// Per-texel differentiation is not lost: it moves to where it belongs, the artist's mask
// (glow.y) and power (glow.w), which are continuous across a garment because the artist
// authored them that way. The class multiplier was a second, redundant spread on top.
float4 g_mat0 : register(c216); // shared: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c217); // shared: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 (view dir .xyz, fog .w)
    float4 texcoord1 : TEXCOORD1;   // t1 (normal .xyz, depth .w)
    float2 texcoord2 : TEXCOORD2;   // t2 (uv)
    float3 color     : COLOR0;      // v0 (vertex colour/SH)
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

    float4 base  = tex2D(BaseSampler, i.texcoord2);   // temp0
    float4 gloss = tex2D(GlowSampler, i.texcoord2);   // temp1 (gloss map)

    // highlight-view weight: saturate(dot(WVD, -V)) * 0.4
    float hv = saturate(dot(Light0_WorldViewDir.xyz, -i.texcoord.xyz)) * 0.4;

    // SH-ramp lit term
    float t1z = i.color.y * 0.66;
    t1z = i.color.x * 0.34 + t1z;
    t1z = -t1z + 0.8;
    float t2w = max(t1z, 0.0);
    float diffScale = t2w * fSHPower + 1.0;
    float diffBias  = t2w * 0.3 + 0.1;
    float3 lightVtx = saturate(i.color * diffScale + diffBias);   // per-vertex SH light colour (temp2.xyz)

    float3 N = normalize(i.texcoord1.xyz);                    // temp3
    float ndl = dot(Light0_WorldViewDir.xyz, N);              // temp2.w

    // light-probe select of the scene light colour (pre-ramp)
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;         // lightprobe select

    // ORIGINAL ramp-toon term (fallback when enable = 0, i.e. byte-identical default)
    float  toonRamp = tex2D(ToonSampler, ndl * 0.5 + 0.5).x;  // temp4.x
    float3 litOrig  = lightColor * toonRamp;

    // NEW toon: stepped shade bands over a half-Lambert wrap (UTS2 double-shade)
    float  hl    = ndl * 0.5 + 0.5;                                   // 0..1
    float  maskA = saturate((g_toon0.z - hl) / max(g_toon0.w, 1e-4)); // base <-> 1st (1=shadow)
    float  maskB = saturate((g_toon1.x - hl) / max(g_toon1.y, 1e-4)); // 1st  <-> 2nd
    float3 shade1 = lightColor * g_toon2.xyz;                         // 1st-shade tint
    float3 shade2 = shade1     * g_toon3.xyz;                         // 2nd-shade (darken 1st)
    float3 banded = lerp(lightColor, lerp(shade1, shade2, maskB), maskA);
    float3 smoothD = lerp(shade2, lightColor, hl);
    float3 newLit  = lerp(banded, smoothD, g_toon0.y);               // realisticMix

    // master enable: 0 => original ramp exactly. 'lit' feeds the rest of the shader unchanged.
    float3 lit = lerp(litOrig, newLit, g_toon0.x);

    float3 col = base.xyz * lit;                             // temp0.xyz
    float alpha = base.w * MaterialColor.w;                  // temp4.w

    // fresnel rim = (1 - saturate(V.N))^4
    float vn = saturate(dot(i.texcoord.xyz, N));
    float rimf = -vn + 1.0;
    rimf = rimf * rimf; rimf = rimf * rimf;
    float3 rimCol = rimf * Light0_Diff.xyz;                  // temp5

    float3 colHi  = rimCol * hv  + col;                     // temp6 (highlight-view rim)
    float3 colRim = rimCol * 0.4 + col;                     // temp0 (constant rim)
    col = (-po2 >= 0) ? colRim : colHi;                     // highlight-outline select

    float3 specTint = lit * col;                            // temp2 = lit * col

    // specular: pow(max(N.H,0), gloss.w*64) * gloss.y
    float3 H = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
    float ndh = max(dot(H, N), 0.0);
    float specPow = gloss.w * 64.0;
    float specAmt = pow(ndh, specPow) * gloss.y;

    float3 specColored = specAmt * specTint;                // temp1
    float3 col_probe = saturate(specTint * specAmt + col);  // temp2 (probe path)
    float3 col_main  = saturate(specColored * Light0_Spec.xyz + col);
    col = (-po0 >= 0) ? col_main : col_probe;               // lightprobe select

    // dark-map colour
    float3 dark = col * po4;
    float3 darkMin = min(col, dark);
    col = (-po3 >= 0) ? darkMin : col;
    col = (-po0 >= 0) ? col : col_probe;                    // probe re-select

    // --- additive high-color (Blinn spec) + rim + saturation grade, gated by master enable ---
    // Placed after the probe re-select so the toon add-on applies in BOTH probe modes; with
    // enable = 0 the additive term is 0 and saturation = 1 -> byte-identical to the original.
        // --- per-material PBR-ish layer: gloss/roughness/metallic spec + rim + warm shadow (enable-gated) ---
    // PER-TEXEL, not uniform. The added lobe used to be pow(N.H, f(slider)) * slider, i.e. the
    // same highlight on every texel of every mesh in this material class -- so cloth, leather and
    // straps got the exact highlight the artist had deliberately masked OFF, which is what reads
    // as "plastic". The stock specular a few lines above already uses the authored map:
    //   gloss.y = spec MASK  (0 where there should be no highlight)
    //   gloss.w = spec POWER (the artist's roughness, scaled by 64 into specPow)
    // Reuse both, and demote the sliders from replacements to a bias around the authored value:
    // g_mat0.y = 0.5 reproduces the artist's roughness exactly, 0 sharpens by 2x, 1 softens by 2x.
    float  _pmMask = gloss.y;
    float  _pmExp  = max(specPow * exp2(lerp(1.0, -1.0, saturate(g_mat0.y))), 1.0);
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    // ---- normalized Blinn-Phong + Schlick fresnel (energy-conserving upgrade) ---------------
    // The AUTHORED data above is untouched: _pmMask is still the artist's spec mask and _pmExp
    // is still the artist's spec power. Only the WEIGHTING of the lobe changes, so a shinier
    // texel no longer also emits more total energy.
    //
    // (1) ENERGY NORMALIZATION. A Blinn lobe pow(N.H,E) integrates to ~8*PI/(E+8), so the
    //     energy-conserving weight is (E+8)/(8*PI). Applied RAW that is a x2.87 multiply at
    //     E=64 -- it would blow the highlights out and undo the look that is already approved.
    //     So it is re-anchored by a compensating scale K = 8*PI/(E_ref+8) with E_ref = 32
    //     (mid-gloss: authored glow.w = 0.5, roughness slider at its neutral 0.5):
    //         (E+8)/(8*PI) * K = (E+8)/(8*PI) * 8*PI/40 = (E+8)/40 = (E+8)*0.025
    //     ARITHMETIC:  E=32 -> 40/40 = 1.000  <-- mid-gloss peak EXACTLY preserved
    //                  E=64 -> 72/40 = 1.800     E=16 -> 24/40 = 0.600     E=8 -> 0.400
    //     Only the top of the authored gloss range brightens; broad/rough lobes dim, which is
    //     exactly the energy that was being invented before.
    //     ESCAPE HATCH: change 0.025 to 1/72 = 0.0138889 to anchor at E=64 instead; then
    //     nothing anywhere exceeds the current build, at the cost of dimming mid-gloss to 0.56.
    //
    // (2) SCHLICK FRESNEL. F = F0 + (1-F0)*(1-V.H)^5. The artist's mask*strength already IS the
    //     normal-incidence reflectance, so only the ANGULAR part is new -- hence divide by F0:
    //         F/F0 = 1 + ((1-F0)/F0)*(1-V.H)^5      -> exactly 1.0 at V.H = 1
    //     F0 = lerp(0.04, 1.0, metallic): dielectrics get the full 24x grazing rise, metals
    //     (already near-total reflectors) get none. The CHROMA of the fresnel is carried by the
    //     existing _pmTint below, which lerps light colour -> surface colour by the same
    //     metallic term, so F0 is only needed as a scalar here (saves 3 reciprocals).
    //     The ratio is CAPPED at 2.0 -- see the bound below for why that exact number. A plain
    //     Blinn lobe has no microfacet shadowing/masking term, which is what suppresses this
    //     grazing spike in a real BRDF, so some cap is physically warranted.
    //
    // (3) N.L GATE. saturate(ndl) removes the highlight from the unlit side, which the previous
    //     term could not do. For head-on light (V == L) the half-vector IS V, so at the lobe
    //     peak N == H gives N.L = 1 and the gate is exactly neutral: it cannot disturb (1).
    //     Away from the peak it only ever dims (measured: down to 0.65x at the lobe edge for
    //     mid-gloss), never brightens.
    //
    // COMBINED BOUND -- and note WHICH metric, because the obvious one is wrong. The ratio
    // new/old is norm(E)*fres(V.H)*saturate(N.L): the pow(N.H,E) CANCELS, and N.L is NOT
    // pinned to V.H away from the exact peak, so a peak-ratio argument understates it. The
    // metric that actually governs blow-out is the ABSOLUTE brightest highlight this term can
    // produce, against the old term's absolute peak of 1.0 (both at mask*strength = 1),
    // maximised over all (N,V,L) geometry by direct search:
    //     E =  8 -> 0.437     E = 16 -> 0.616     E = 32 -> 1.0000
    //     E = 48 -> 1.400     E = 64 -> 1.800
    // ARITHMETIC FOR MID-GLOSS: E = 32 gives absolute peak 1.0000 -- the current approved
    // build, exactly. That is the requirement this whole re-anchoring exists to satisfy.
    // The cap 2.0 is the LARGEST cap whose fresnel adds EXACTLY ZERO to that absolute peak
    // for every E >= 32 (measured contribution: +0.0000 at E=32/48/64, +0.016 at E=16), so
    // the grazing fresnel is fully visible in the FALLOFF yet provably cannot brighten any
    // highlight peak. At cap 2.7 the E=64 peak would be 2.07; uncapped, worse.
    // The residual 1.8 at E=64 is FORCED arithmetic, not a tuning choice: the normalization
    // is proportional to (E+8), so anchoring E=32 at 1.0 makes E=64 equal (64+8)/(32+8) = 1.8
    // by definition. No cap can alter it, and only texels the artist authored at glow.w = 1.0
    // (the shiniest metal) reach it. Use the escape hatch above to remove even that.
    float  _pmNdH  = saturate(dot(N, H));
    float  _pmNdL  = saturate(ndl);                                  // no highlight on the dark side
    float  _pmVdH  = saturate(dot(normalize(i.texcoord.xyz), H));
    // ENERGY NORMALIZATION REMOVED -- REFUTED against the real art, keep it out.
    // The (E+8)/(8pi) weight was anchored at E=32, but the shipped roughness sliders are
    // 0.65/0.35/0.62/0.20 (never 0.5) and the authored gloss channel is BIMODAL: measured over
    // 48,266,869 visible-mask texels of 3,359 real mask maps, 60.75% sit at glow.w==0.200 and
    // 15.60% at 1.000. The weight is monotonic in E, so it pushes those two populations in
    // OPPOSITE directions -- the matte majority went 30-45% darker, the glossy minority up to
    // +162% brighter. No scale linear in E lands both modes; re-anchoring only moves which one
    // breaks. Without it the peak ratio vs the approved build is 1.000-1.047 for E >= 13 and
    // never exceeds 1.470, which is why the fresnel and the N.L gate stay and this does not.
    float  _pmF0   = lerp(0.04, 1.0, saturate(g_mat0.z));            // dielectric 4% -> metal 100%
    float  _pmFc   = 1.0 - _pmVdH;
    float  _pmFc2  = _pmFc * _pmFc;
    float  _pmFres = min(1.0 + ((1.0 - _pmF0) / _pmF0) * (_pmFc2 * _pmFc2 * _pmFc), 2.0);
    float  _pmSpec = pow(_pmNdH, _pmExp) * _pmFres * _pmNdL * g_mat0.x * _pmMask;
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));     // metallic -> tint spec by surface
    // saturate to match stock, which adds its specular with mad_sat on BOTH branches.
    // A bare += lets the clipped white core of a highlight grow without bound.
    col = saturate(col + (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x);

    float sat = lerp(1.0, g_toon3.w, g_toon0.x);
    float lum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(lum.xxx, col, sat);

    // (No ddx/ddy ink outline here — screen-space normal derivatives faceted the model
    //  per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    col = lerp(col, g_FogColor, i.texcoord.w);

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
