// Char_Reflection MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// (Shader4: BaseSampler + EnvSampler reflection + ToonSampler toon shading.)
//
// PS-SPLICE: only this PIXEL shader replaces the effect's big PS blobs; the GPU-skinning
// VERTEX shaders stay byte-identical (SkinBone register pack untouched -> characters stay
// visible). Param + sampler NAMES match the effect globals so the effect name-binds them
// from this shader's CTAB. Compiled ps_3_0, entry 'main'.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223; the DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) every render frame. When those
// registers are 0 (master enable = 0) the output is BYTE-IDENTICAL to the original
// reflection/ramp look -> safe drop-in / A-B toggle.

float4 Light0_PreCalcLightColor;   // c1 (min/max folded in preshader)
float3 Light0_WorldViewDir;        // c5 : view-space light dir
float  fSHPower;                   // c7
float4 MaterialColor;              // c6 : .w = material alpha
float4 Extra_WeaveColor;           // c10: .w = extra alpha weight
float4 TextureAmounts;             // c8 : .w = env reflection amount
float2 ReflectionTexTiles;         // c9 : env uv tiling
float3 g_DarkMapColorForChar;      // dark-map tint
float  g_fDarkMapColorWeight;      // dark-map weight
int    g_LightProbeMode;           // light-probe select
bool   g_bDarkMapColor;            // dark-map select
float3 g_FogColor;                 // c4 : fog colour

sampler2D BaseSampler : register(s0);   // base albedo
sampler2D EnvSampler  : register(s1);   // environment / reflection map
sampler2D ToonSampler : register(s2);   // toon ramp

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c218); // Metal: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c219); // Metal: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 : xyz = -viewDir , w = fog factor
    float4 texcoord1 : TEXCOORD1;   // t1 : xyz = view-space normal , w = depth fade
    float2 texcoord2 : TEXCOORD2;   // t2 : base uv
    float3 color     : COLOR0;      // v0 : vertex / SH colour
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // ---- uniform-only math (fxc re-folds into a preshader, as in the original) ----
    float  po0 = (float)g_LightProbeMode * (float)g_LightProbeMode;               // c0
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));    // c1
    float  prA = (float)g_bDarkMapColor - 1.0;
    float  po2 = prA * prA;                                                       // c2
    float3 po3 = g_fDarkMapColorWeight * g_DarkMapColorForChar;                   // c3

    // ---- base + SH diffuse ----
    float4 base = tex2D(BaseSampler, i.texcoord2);
    float  t1w  = i.color.y * 0.66;
    float  t1x  = i.color.x * 0.34 + t1w;
    t1x = -t1x + 0.8;
    float  t2w  = max(t1x, 0.0);
    float  diffScale = t2w * fSHPower + 1.0;
    float  diffBias  = t2w * 0.3 + 0.1;

    float3 N   = normalize(i.texcoord1.xyz);
    float  ndl = dot(Light0_WorldViewDir.xyz, N);

    // ---- reflection lookup (env) ----
    float  ndl2 = ndl + ndl;
    float2 refl = N.xy * -ndl2 + Light0_WorldViewDir.xy;   // reflect L about N in view plane
    refl = refl * ReflectionTexTiles.xy;
    float3 env  = tex2D(EnvSampler, refl).xyz * TextureAmounts.www;

    // scene light colour: probe (g_LightProbeMode==0) or per-vertex SH colour
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);
    float3 lightColor = (-po0 >= 0.0) ? po1 : lightVtx;    // -po0>=0  <=>  g_LightProbeMode==0

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

    // ---- combine: litColor*base + env ----
    float3 col   = litColor * base.xyz + env;
    float  alpha = base.w * MaterialColor.w * Extra_WeaveColor.w;

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
    float sat    = lerp(1.0, g_toon3.w, g_toon0.x);
    float satLum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(satLum.xxx, col, sat);

    // ---- dark-map darken + light-probe re-select (faithful to Shader4) ----
    float3 dark    = col * po3;
    float3 darkMin = min(col, dark);
    float3 colDark = (-po2 >= 0.0) ? darkMin : col;        // dark-map select
    col = (-po0 >= 0.0) ? colDark : col;                   // light-probe select

    // (No ddx/ddy ink outline — screen-space normal derivatives faceted the model
    //  per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    // ---- fog ----
    col = lerp(col, g_FogColor, i.texcoord.w);

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
