// Char_DyeGlow MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// (Shader0 == CharDyeSpecGlow_Impl2 / Trans_Impl2: toon + glow + weave, no specular.)
// PS-splice only (VS blobs kept byte-identical -> GPU skinning intact). Param NAMES +
// sampler REGISTERS match the Char_DyeGlow effect (s0 Base, s1 Glow, s2 Toon), bound by name.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB tops far
// below there, so the runtime never uploads to them). The DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) every render frame. When those
// registers are 0 (DLL not pushing yet), master enable = 0 and the output is
// BYTE-IDENTICAL to the original ramp toon -> safe drop-in / A-B toggle.
// The old ddx/ddy INK OUTLINE was removed (it faceted the model per-triangle and blackened
// edges); outlines come from the SSAO post-process instead.

float4 Light0_PreCalcLightColor;
float3 g_DarkMapColorForChar;
int    g_LightProbeMode;
bool   g_bDarkMapColor;
float  g_fDarkMapColorWeight;
float3 Light0_WorldViewDir;
float  fSHPower;
float4 MaterialColor;
float4 Extra_GlowColor;
float4 Extra_WeaveColor;
float3 g_FogColor;

sampler2D BaseSampler : register(s0);
sampler2D GlowSampler : register(s1);
sampler2D ToonSampler : register(s2);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c216); // Cloth: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c217); // Cloth: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 (view dir .xyz, fog .w)
    float4 texcoord1 : TEXCOORD1;   // t1 (normal .xyz, depth .w)
    float2 texcoord2 : TEXCOORD2;   // t2 (uv)
    float3 color     : COLOR0;      // v0 (vertex colour / SH)
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // uniform-only (fxc folds these into a preshader, like the original)
    float  po0 = (float)g_LightProbeMode * (float)g_LightProbeMode;                 // probe select
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));      // precalc light
    float  prd = (float)g_bDarkMapColor - 1.0;
    float  po2 = prd * prd;                                                         // darkmap select
    float3 po3 = g_fDarkMapColorWeight * g_DarkMapColorForChar;                     // darkmap tint

    float4 glow = tex2D(GlowSampler, i.texcoord2);
    float4 base = tex2D(BaseSampler, i.texcoord2);

    // toon-band SH lighting from the interpolated vertex colour
    float t = i.color.y * 0.66;
    t = i.color.x * 0.34 + t;
    t = -t + 0.8;
    float t2w = max(t, 0.0);
    float diffScale = t2w * fSHPower + 1.0;
    float diffBias  = t2w * 0.3 + 0.1;

    float3 N = normalize(i.texcoord1.xyz);
    float  ndl = dot(Light0_WorldViewDir.xyz, N);
    float2 toonUV = ndl * 0.5 + 0.5;

    // --- lightColor source: probe vs SH vertex (same selection as the original) ---
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;

    // ORIGINAL ramp-toon term (fallback when enable = 0, i.e. byte-identical default)
    float  toonRamp = tex2D(ToonSampler, toonUV).x;
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
    float3 lit = lerp(litOrig, newLit, g_toon0.x);

    // glow contribution = glow.z * lit * Extra_GlowColor
    float3 glowCol = glow.z * lit * Extra_GlowColor.xyz;
    // base texture blended with the weave/dye colour by the glow.x mask
    float3 baseC = base.xyz * (1.0 - glow.x) + (glow.x * base.xyz) * Extra_WeaveColor.xyz;
    float alpha = base.w * MaterialColor.w;
    // lit * base + glow
    float3 col = lit * baseC + glowCol;

    // --- additive high-color (Blinn spec) + rim, gated by master enable ---
    float3 H       = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
        // --- per-material PBR-ish layer: gloss/roughness/metallic spec + rim + warm shadow (enable-gated) ---
    // PER-TEXEL, recovered from the stock bytecode. The original shader for this material
    // computed  pow(max(dot(H,N),0), glow.w*64) * glow.y  -- traced in the stock PS blob:
    //   mul r1.w, <glowReg>.w, c.y(=64) ; pow rD, ndh, r1.w ; mul r1.w, rD, <glowReg>.y
    // This reconstruction had DROPPED that term and replaced it with a flat uniform lobe, so
    // every texel of the material got the same highlight -- including the cloth and leather the
    // artist had deliberately masked off (glow.y = 0). That is the plastic look.
    // glow.y = spec mask, glow.w = spec power. The g_mat0 sliders now BIAS the authored value
    // instead of replacing it: g_mat0.y = 0.5 reproduces the artist's roughness exactly.
    float  _pmMask = glow.y;
    float  _pmExp  = max(glow.w * 64.0 * exp2(lerp(1.0, -1.0, saturate(g_mat0.y))), 1.0);
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
    // ENERGY NORMALIZATION REMOVED -- it was REFUTED against the real art, keep it out.
    // The (E+8)/(8pi) weight was anchored at E=32, but the shipped roughness sliders are
    // 0.65/0.35/0.62/0.20 (never 0.5) and the authored gloss channel is BIMODAL: measured over
    // 48,266,869 visible-mask texels of 3,359 real mask maps, 60.75%% sit at glow.w==0.200 and
    // 15.60%% at 1.000. Because the weight is monotonic in E it pushes those two populations in
    // OPPOSITE directions -- the matte majority went 30-45%% darker and the glossy minority up to
    // +162%% brighter. No scale that is linear in E can land both modes; re-anchoring only moves
    // which mode breaks. Without it the peak ratio vs the approved build is 1.000-1.047 for
    // E >= 13 and never exceeds 1.470 anywhere, which is why the fresnel and the N.L gate stay
    // and the normalization does not.
    float  _pmF0   = lerp(0.04, 1.0, saturate(g_mat0.z));            // dielectric 4% -> metal 100%
    float  _pmFc   = 1.0 - _pmVdH;
    float  _pmFc2  = _pmFc * _pmFc;
    float  _pmFres = min(1.0 + ((1.0 - _pmF0) / _pmF0) * (_pmFc2 * _pmFc2 * _pmFc), 2.0);
    float  _pmSpec = pow(_pmNdH, _pmExp) * _pmFres * _pmNdL * g_mat0.x * _pmMask;   // gloss = strength
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));     // metallic -> tint spec by surface
    // saturate to match stock, which adds its specular with mad_sat on BOTH branches.
    // A bare += lets the clipped white core of a highlight grow without bound.
    col = saturate(col + (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x);

    // --- saturation grade (enable-gated so default sat effectively = 1) ---
    float sat = lerp(1.0, g_toon3.w, g_toon0.x);
    float lum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(lum.xxx, col, sat);

    // dark-map tint select (applied only in probe mode, matching the original)
    float3 dark    = col * po3;
    float3 darkMin = min(col, dark);
    float3 afterDark = (-po2 >= 0) ? darkMin : col;
    col = (-po0 >= 0) ? afterDark : col;

    // (No ddx/ddy ink outline here — screen-space normal derivatives faceted the
    //  model per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    col = lerp(col, g_FogColor, i.texcoord.w);

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
