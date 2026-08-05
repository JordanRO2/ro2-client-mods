// Char_Statue MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// (Shader0/4/8/12/16/32 are all byte-identical.) PS-splice only (VS blobs kept
// byte-identical -> GPU skinning intact). Param NAMES match the Char_Statue effect
// globals; samplers pinned to the SAME registers (BaseSampler s0, ToonSampler s1).
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB never
// reaches there, so the runtime never uploads to them). The DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) every render frame. When those
// registers are 0 (DLL not pushing yet), master enable = 0 and the output is
// BYTE-IDENTICAL to the original statue (luminance * stone tint) -> safe A-B toggle.
//
// Statue note: after lighting, the shader collapses colour to luminance * stone tint
// (0.5,0.45,0.37). The toon double-shade bands the *brightness* that feeds that
// luminance, but the shade TINTS (g_toon2/g_toon3 rgb) are folded to luminance by the
// stone step, so only their brightness survives. Only the saturation grade is applied to
// the FINAL stone colour.
//
// 2026-08-03: the additive spec/rim layer that used to sit here was REMOVED. It was applied
// after the stone conversion precisely so that its highlights would survive being
// desaturated — which is another way of saying it was overriding the effect's defining
// operation. The stock PS does not even declare the view-direction interpolant. Full
// argument and a one-line revert recipe are in main().

float4 Light0_PreCalcLightColor;   // preshader: min(1,max(0,...))  (probe light colour)
int    g_LightProbeMode;           // preshader: g_LightProbeMode^2  (probe cmp select)
float3 Light0_WorldViewDir;        // view-space light direction (toon lookup)
float  fSHPower;                   // SH diffuse power
float4 MaterialColor;              // .w = material alpha

sampler2D BaseSampler : register(s0);
sampler2D ToonSampler : register(s1);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
// g_mat0/g_mat1 (c218/c219) are deliberately NOT declared here any more: this shader adds no
// specular, rim or warm-shadow term. See the block comment in main() for why.

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 : view dir (-normalize(viewPos)); original PS ignored it,
                                    //      but the unchanged VS emits it, so we read it for spec/rim
    float3 texcoord1 : TEXCOORD1;   // t1 : view-space normal (the shader already uses this)
    float2 texcoord2 : TEXCOORD2;   // t2 : base UV
    float3 color     : COLOR0;      // v0 : vertex colour / SH term
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // ---- uniform-only math (fxc re-folds this into a preshader, like the original) ----
    float  po0 = (float)g_LightProbeMode * (float)g_LightProbeMode;      // c0.x
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz)); // c1

    // ---- base texture + SH diffuse (faithful to the original statue PS) ----
    float4 base = tex2D(BaseSampler, i.texcoord2);
    float t1w = i.color.y * 0.66;
    float t1x = i.color.x * 0.34 + t1w;
    t1x = -t1x + 0.8;
    float t2w = max(t1x, 0.0);
    float diffScale = t2w * fSHPower + 1.0;
    float diffBias  = t2w * 0.3 + 0.1;

    float3 N   = normalize(i.texcoord1.xyz);
    float  ndl = dot(Light0_WorldViewDir.xyz, N);

    // scene light colour: probe (g_LightProbeMode==0) or per-vertex SH colour
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;   // -po0>=0  <=>  g_LightProbeMode==0

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

    float3 texColor = base.xyz * litColor;
    float  alpha    = base.w * MaterialColor.w;

    // ---- statue: luminance (0.3R + 0.6G + 0.1B) * stone tint (0.5, 0.45, 0.37) ----
    float lum = texColor.x * 0.3 + texColor.y * 0.6 + texColor.z * 0.1;
    float3 col = lum * float3(0.5, 0.45, 0.37);

    // --- NO added specular / rim / warm-shadow layer here. Removed 2026-08-03. -------------
    // The whole per-material block was deleted rather than re-tinted, because every one of
    // its terms is view-dependent shading that this effect deliberately throws away.
    // Evidence from the stock blob (scratchpad/aud2/stock__Char_Statue__61e95fa1.asm, 27 slots
    // — the only lit PS; the other, 28129009 at 6 slots, is the Z-only pass):
    //   * There is no `pow` instruction anywhere in it. No specular, no fresnel rim, no fog.
    //   * It does not even declare t0. Its dcls are `dcl t1.xyz` (normal), `dcl t2.xy` (uv),
    //     `dcl v0.xyz` — so the VIEW DIRECTION never reaches the stock pixel shader. Every
    //     term we were adding (Blinn H = V+L, fresnel N.V) is a function of a vector the
    //     stock shader does not read. That is as close to a proof of "invented" as this
    //     material gets, and it is a control that could have failed: had t0 been declared,
    //     a view-dependent term would at least have been arguable.
    //   * Its last four instructions collapse the shaded colour to a single luminance
    //     (0.3R + 0.6G + 0.1B) and multiply by the stone tint c7.wzyx = (0.5,0.45,0.37), then
    //     write `mov r0, c6.x` (= 0) to oC1. The effect is a flat, colourless, orientation-
    //     independent stone look by construction.
    // The Specular material class this shader was wired to (c218/c219) is the shiniest of the
    // four — gloss 0.55, roughness 0.20 (Blinn exponent 64), metallic 0.45, rim 0.35 at
    // (0.95,0.97,1.00) — so a petrified character was being given a tight travelling
    // highlight and a cool white edge glow. There is no per-texel signal to drive any of it
    // from either: the only samplers are s0 BaseSampler and s1 ToonSampler.
    //
    // REVERT NOTE: this is the one place where I went past "remove the specular lobe" and
    // also dropped the rim and warm-shadow. If the cool rim on statues was wanted as a
    // deliberate stylistic choice, restore just these two lines — nothing else depends on it:
    //     float _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    //     col += g_mat1.xyz * pow(_pmRimA, exp2(lerp(3.0,0.0,g_mat1.w))) * saturate(hl) * g_toon0.x;
    // -------------------------------------------------------------------------------------

    // --- saturation grade (enable-gated so default sat effectively = 1) ---
    float sat    = lerp(1.0, g_toon3.w, g_toon0.x);
    float satLum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(satLum.xxx, col, sat);

    // (No ddx/ddy ink outline here — screen-space normal derivatives faceted the
    //  model per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    o.color  = float4(col, alpha);
    o.color1 = (float4)0;                          // original statue PS writes oC1 = 0
    return o;
}
