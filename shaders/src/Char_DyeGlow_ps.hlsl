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
    float  _pmExp  = exp2(lerp(7.0, 2.0, saturate(g_mat0.y)));        // roughness -> Blinn exp (128..4)
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    float  _pmSpec = pow(saturate(dot(N, H)), _pmExp) * g_mat0.x;   // gloss = strength
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));     // metallic -> tint spec by surface
    col += (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x;

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
