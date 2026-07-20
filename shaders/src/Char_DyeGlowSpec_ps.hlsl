// Char_DyeGlowSpec MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// (Shader32, the PS that CharDyeSpecGlow_Impl0 compiles: glow + specular + built-in
//  highlight rim + dye.) ps_3_0, entry 'main'.
//
// PS-splice only: ONLY the pixel shader is replaced; the GPU-skinning vertex shader stays
// byte-identical. Param NAMES + sampler REGISTERS match the effect's globals so the
// name-binding + preshader stay compatible. Samplers: Base=s0, Glow=s1, Toon=s2.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (pushed by the DLL via
// SetPixelShaderConstantF(220,...)). enable (g_toon0.x) = 0 => BYTE-IDENTICAL to the
// original ramp look. The old ddx/ddy INK OUTLINE was removed (faceted / blackened edges).
// This shader keeps its OWN built-in highlight rim + specular (part of the faithful
// reconstruction); the toon high-color/rim/saturation is an ADDITIVE layer on top, gated
// by enable.

float4 Light0_PreCalcLightColor;   // ambient*diffuse precalc (light-probe path)
float4 Light0_Diff;                // highlight rim colour
float4 Light0_Spec;                // specular colour
float3 Light0_WorldViewDir;        // view-space light dir
float3 g_DarkMapColorForChar;
int    g_LightProbeMode;
bool   g_bDarkMapColor;
float  g_fDarkMapColorWeight;
int    g_iHighlightOutline;
float  fSHPower;
float4 MaterialColor;              // .w -> alpha
float4 Extra_GlowColor;            // dye: glow tint
float4 Extra_WeaveColor;           // dye: weave/base tint
float3 g_FogColor;

sampler2D BaseSampler : register(s0);
sampler2D GlowSampler : register(s1);
sampler2D ToonSampler : register(s2);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c218); // Metal: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c219); // Metal: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0: xyz = view dir, w = fog factor
    float4 texcoord1 : TEXCOORD1;   // t1: xyz = view-space normal, w = depth
    float2 texcoord2 : TEXCOORD2;   // t2: uv
    float3 color     : COLOR0;      // v0: vertex SH colour
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // uniform-only math (fxc re-folds into a preshader, like the original)
    float  po0 = (float)g_LightProbeMode * (float)g_LightProbeMode;
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));
    float  po2 = (float)g_iHighlightOutline * (float)g_iHighlightOutline;
    float  pr  = (float)g_bDarkMapColor - 1.0;
    float  po3 = pr * pr;
    float3 po4 = g_fDarkMapColorWeight * g_DarkMapColorForChar;

    float4 glow = tex2D(GlowSampler, i.texcoord2);   // r0: r=weave mask, g=spec mask, b=glow, a=gloss
    float4 base = tex2D(BaseSampler, i.texcoord2);   // r1

    // toon SH lighting term
    float sh = i.color.x * 0.34 + i.color.y * 0.66;
    sh = -sh + 0.8;
    sh = max(sh, 0.0);
    float diffScale = sh * fSHPower + 1.0;
    float diffBias  = sh * 0.3 + 0.1;

    float3 N = normalize(i.texcoord1.xyz);
    float ndl = dot(Light0_WorldViewDir.xyz, N);
    float2 toonUV = ndl * 0.5 + 0.5;

    // --- lightColor source: probe vs SH vertex (same selection as the original) ---
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;

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
    float3 lit = lerp(litOrig, newLit, g_toon0.x);

    // glow additive + dye
    float3 glowContrib = (glow.z * lit) * Extra_GlowColor.xyz;
    float3 weave = (glow.x * base.xyz) * Extra_WeaveColor.xyz;
    float3 baseBlended = base.xyz * (1.0 - glow.x) + weave;
    float  alpha = base.w * MaterialColor.w;
    float3 col = lit * baseBlended + glowContrib;

    // built-in highlight rim (bright) -- native to this shader, kept faithful
    float hlStrength = saturate(dot(Light0_WorldViewDir.xyz, -i.texcoord.xyz)) * 0.4;
    float rim = 1.0 - saturate(dot(i.texcoord.xyz, N));
    rim = rim * rim; rim = rim * rim;                 // (1 - N.V)^4
    float3 rimCol = rim * Light0_Diff.xyz;
    float3 colHiA = rimCol * hlStrength + col;
    float3 colHiB = rimCol * 0.4 + col;
    col = (-po2 >= 0) ? colHiB : colHiA;

    float3 litCol = lit * col;

    // specular (native)
    float3 H = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
    float specTerm = pow(max(dot(H, N), 0.0), glow.w * 64.0);
    float specMask = specTerm * glow.y;
    float3 r2 = saturate(litCol * specMask + col);
    float3 r0 = saturate((specMask * litCol) * Light0_Spec.xyz + col);
    float3 colSpec = (-po0 >= 0) ? r0 : r2;

    // dark map
    float3 dark = colSpec * po4;
    float3 darkMin = min(colSpec, dark);
    float3 colDark = (-po3 >= 0) ? darkMin : colSpec;

    float3 finalCol = (-po0 >= 0) ? colDark : r2;

    // --- additive toon high-color (Blinn spec) + rim + saturation, gated by master enable.
    //     Applied to the resolved finalCol (this shader's dark-map is entangled in the mode
    //     select), so enable = 0 collapses exactly to the original resolved colour. ---
    // (per-material spec reuses H from the native block above -- same normalize(view+light))
        // --- per-material PBR-ish layer: gloss/roughness/metallic spec + rim + warm shadow (enable-gated) ---
    float  _pmExp  = exp2(lerp(7.0, 2.0, saturate(g_mat0.y)));        // roughness -> Blinn exp (128..4)
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    // Mask-driven: gate the per-material spec by the artist's spec mask (glow.y) so the
    // Specular knob only boosts the metal/shiny areas the texture marks, not a flat overlay
    // across the whole model. glow.y is the same _a G-channel the native spec uses (line ~119).
    float  _pmSpec = pow(saturate(dot(N, H)), _pmExp) * g_mat0.x * glow.y;   // gloss=strength x spec mask
    finalCol = lerp(finalCol, finalCol * float3(1.15, 0.93, 0.80), g_mat0.w * (1.0 - hl) * g_toon0.x); // warm shadow (hl in [0,1] -> saturate redundant)
    float3 _pmTint = lerp(lightColor, finalCol, saturate(g_mat0.z));     // metallic -> tint spec by surface
    finalCol += (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x;

    float sat = lerp(1.0, g_toon3.w, g_toon0.x);
    float lum = dot(finalCol, float3(0.299, 0.587, 0.114));
    finalCol = lerp(lum.xxx, finalCol, sat);

    // (No ddx/ddy ink outline here — removed; outlines come from the SSAO post-process.)

    finalCol = lerp(finalCol, g_FogColor, i.texcoord.w);

    o.color  = float4(finalCol, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
