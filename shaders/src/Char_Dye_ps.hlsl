// Char_Dye MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// PS-splice only (VS blobs kept byte-identical -> GPU skinning intact). Param NAMES +
// sampler REGISTERS match the Char_Dye effect (s0 Base, s1 SpecularMap, s2 Glow, s3 Toon).
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB tops at
// c15, so the runtime never uploads there). The DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) every render frame. When these
// registers are 0 (DLL not pushing yet), master enable = 0 and the output is
// BYTE-IDENTICAL to the original ramp toon -> safe drop-in / A-B toggle.

float4 Light0_PreCalcLightColor;
float4 Light0_Diff;
float3 Light0_WorldViewDir;
float  fSHPower;
float4 MaterialColor;
float3 g_FogColor;

int    g_LightProbeMode;
bool   g_bDarkMapColor;
float3 g_DarkMapColorForChar;
float  g_fDarkMapColorWeight;

float4 Extra_WeaveColor;      // primary dye colour
float4 Extra_WeaveColor_2;    // secondary dye colour
float4 Extra_WeaveColor_3;    // tertiary dye colour

sampler2D BaseSampler        : register(s0);
sampler2D SpecularMapSampler : register(s1);
sampler2D GlowSampler        : register(s2);
sampler2D ToonSampler        : register(s3);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c216); // Cloth: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c217); // Cloth: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0: xyz = view dir, w = fog factor
    float4 texcoord1 : TEXCOORD1;   // t1: xyz = view-space normal
    float2 texcoord2 : TEXCOORD2;   // t2: base uv
    float3 color     : COLOR0;      // v0: per-vertex SH colour
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    float4 spec = tex2D(SpecularMapSampler, i.texcoord2);   // dye selector / mask
    float4 base = tex2D(BaseSampler,        i.texcoord2);   // base colour
    float4 glow = tex2D(GlowSampler,        i.texcoord2);   // .x = dye mask

    // --- lighting: keep the SH bias math, derive base scene light colour ---
    float t = max(0.8 - (0.34 * i.color.x + 0.66 * i.color.y), 0.0);
    float diffScale = t * fSHPower + 1.0;
    float diffBias  = t * 0.3 + 0.1;

    float3 N   = normalize(i.texcoord1.xyz);
    float  ndl = dot(Light0_WorldViewDir.xyz, N);

    float3 lightProbe = saturate(Light0_PreCalcLightColor.xyz);
    float3 lightVtx   = saturate(i.color.xyz * diffScale + diffBias);
    float3 lightColor = (g_LightProbeMode == 0) ? lightProbe : lightVtx;

    // ORIGINAL ramp-toon term (fallback when enable = 0, i.e. byte-identical default)
    float  toonRamp = tex2D(ToonSampler, ndl * 0.5 + 0.5).x;
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

    // --- dye colour selection (driven by the specular/gloss-map channels) ---
    bool noExtraWeave = (Extra_WeaveColor_2.w == 0.0) && (Extra_WeaveColor_3.w == 0.0);

    float3 dyeAB = (spec.x - spec.y >= 0.0) ? Extra_WeaveColor.xyz : Extra_WeaveColor_2.xyz;
    float  maxXY = max(spec.x, spec.y);
    float3 dye   = (maxXY - spec.z >= 0.0) ? dyeAB : Extra_WeaveColor_3.xyz;
    dye = noExtraWeave ? Extra_WeaveColor.xyz : dye;

    float3 dyedBase = dye * base.xyz;
    float  dyeMask  = noExtraWeave ? glow.x : max(maxXY, spec.z);
    float3 blended  = lerp(base.xyz, dyedBase, dyeMask);

    float  alpha = base.w * MaterialColor.w;

    float3 col = litColor * blended;

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

    // --- dark-map tint (only when enabled and not in light-probe mode) ---
    float3 colDark = col * (g_DarkMapColorForChar.x * g_fDarkMapColorWeight);
    col = (g_bDarkMapColor && (g_LightProbeMode == 0)) ? colDark : col;

    // (No ddx/ddy ink outline here — screen-space normal derivatives faceted the
    //  model per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    // --- fog ---
    col = lerp(col, g_FogColor, i.texcoord.w);

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
