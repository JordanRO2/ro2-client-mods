// Char_Ani_Dye MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// (Shader0 / CharacterDef_Impl0/1/2 technique: animated-dye toon char, no specular.)
// PS-splice only: only the PIXEL shader is spliced; the GPU-skinning VS blobs are left
// byte-identical. All globals/samplers declared BY NAME so the effect's name-binding + the
// re-folded preshader stay compatible. Sampler registers match the original PS:
//   s0 = BaseSampler, s1 = GlowSampler, s2 = ToonSampler.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (pushed by the DLL via
// SetPixelShaderConstantF(220,...)). enable (g_toon0.x) = 0 => BYTE-IDENTICAL to the
// original ramp look. The old ddx/ddy INK OUTLINE was removed (it faceted the model
// per-triangle and blackened edges); outlines come from the SSAO post-process.

float4 Light0_PreCalcLightColor;
float3 g_DarkMapColorForChar;
int    g_LightProbeMode;
bool   g_bDarkMapColor;
float  g_fDarkMapColorWeight;
float3 Light0_WorldViewDir;
float  fSHPower;
float4 MaterialColor;
float3 g_FogColor;
float4 Extra_WeaveColor;
float  DyeColorAnimationValue;

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
    float4 texcoord  : TEXCOORD0;   // t0: view dir .xyz, fog factor .w
    float4 texcoord1 : TEXCOORD1;   // t1: normal
    float2 texcoord2 : TEXCOORD2;   // t2: uv
    float3 color     : COLOR0;      // v0: vertex colour / SH term
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // --- uniform-only (fxc re-folds these into a preshader, like the original) ---
    float3 po0 = DyeColorAnimationValue * Extra_WeaveColor.xyz;                 // dye colour
    float  po1 = (float)g_LightProbeMode * (float)g_LightProbeMode;             // probe selector
    float3 po2 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));  // clamped precalc light
    float  pr  = (float)g_bDarkMapColor - 1.0;
    float  po3 = pr * pr;                                                       // darkmap selector
    float3 po4 = g_fDarkMapColorWeight * g_DarkMapColorForChar;                 // dark-map weight

    float4 glow = tex2D(GlowSampler, i.texcoord2);   // r0 : glow.x drives the dye blend
    float4 base = tex2D(BaseSampler, i.texcoord2);   // r1

    // SH-ish vertex-colour lighting term (original temp0.yzw = i.color.zyx * scale + bias)
    float s = i.color.y * 0.66;
    s = i.color.x * 0.34 + s;
    s = -s + 0.8;
    float t2w = max(s, 0.0);
    float diffScale = t2w * fSHPower + 1.0;
    float diffBias  = t2w * 0.3 + 0.1;

    // toon ramp from N.L
    float3 N = normalize(i.texcoord1.xyz);
    float ndl = dot(Light0_WorldViewDir.xyz, N);
    float2 toonUV = ndl * 0.5 + 0.5;

    // --- lightColor source: probe vs SH vertex (same selection as the original; the
    //     original swizzled i.color.zyx and un-swizzled it via lit.zyx, so the effective
    //     vertex light is simply saturate(i.color * scale + bias)) ---
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);
    float3 lightColor = (-po1 >= 0) ? po2 : lightVtx;

    // ORIGINAL ramp-toon term (fallback when enable = 0, i.e. byte-identical default)
    float  toonRamp = tex2D(ToonSampler, toonUV).x;
    float3 litOrig  = lightColor * toonRamp;

    // NEW toon: stepped shade bands over a half-Lambert wrap (UTS2 double-shade)
    float  hl    = ndl * 0.5 + 0.5;
    float  maskA = saturate((g_toon0.z - hl) / max(g_toon0.w, 1e-4));
    float  maskB = saturate((g_toon1.x - hl) / max(g_toon1.y, 1e-4));
    float3 shade1 = lightColor * g_toon2.xyz;
    float3 shade2 = shade1     * g_toon3.xyz;
    float3 banded = lerp(lightColor, lerp(shade1, shade2, maskB), maskA);
    float3 smoothD = lerp(shade2, lightColor, hl);
    float3 newLit  = lerp(banded, smoothD, g_toon0.y);              // realisticMix

    // master enable: 0 => original ramp exactly
    float3 litColor = lerp(litOrig, newLit, g_toon0.x);

    // dye / glow blend of the base colour (unchanged dye selection)
    float oneMinusGlow = -glow.x + 1.0;                   // temp2.w
    float3 dyeTerm = (glow.x * base.xyz) * po0;           // temp3.xyz
    float3 baseC = base.xyz * oneMinusGlow + dyeTerm;     // temp1.xyz

    float alpha = base.w * MaterialColor.w;               // temp3.w

    // lit * base, then dark-map only in probe mode (matches the original select)
    float3 colBase = baseC * litColor;
    float3 dark    = colBase * po4;
    float3 darkMin = min(colBase, dark);
    float3 darkSel = (-po3 >= 0) ? darkMin : colBase;
    float3 col     = (-po1 >= 0) ? darkSel : colBase;

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

    // (No ddx/ddy ink outline here — removed; outlines come from the SSAO post-process.)

    col = lerp(col, g_FogColor, i.texcoord.w);            // temp3.xyz

    o.color  = float4(col, alpha);
    o.color1 = (0.0).xxxx;                                // original writes oC1 = 0
    return o;
}
