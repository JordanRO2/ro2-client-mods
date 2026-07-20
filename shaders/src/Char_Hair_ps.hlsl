// Char_Hair MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant.
// PS-SPLICE only: the GPU-skinning vs_2_0 stays byte-identical (SkinBone pack untouched ->
// characters stay visible). Impl0 / Shader20 == Shader32: Base + Glow + Toon, SH-toon diffuse,
// highlight rim, glow-driven specular, light-probe + darkmap selects, CustomHairColor tint.
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223 (the effect CTAB tops at c15, so
// the runtime never uploads there). The DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) each render frame. When these registers
// are 0 (DLL not pushing yet) master enable = 0 and the output is BYTE-IDENTICAL to the
// original ramp toon -> safe drop-in / A-B toggle.

float4 Light0_PreCalcLightColor;
float4 Light0_Diff;
float4 Light0_Spec;
float3 Light0_WorldViewDir;
float4 MaterialColor;
float  fSHPower;
float3 g_FogColor;
float3 g_DarkMapColorForChar;
float  g_fDarkMapColorWeight;
bool   g_bDarkMapColor;
int    g_LightProbeMode;
int    g_iHighlightOutline;
float4 CustomHairColor;

sampler2D BaseSampler : register(s0);
sampler2D GlowSampler : register(s1);
sampler2D ToonSampler : register(s2);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c214); // Hair: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c215); // Hair: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0: .xyz = -viewDir (view space), .w = fog factor
    float4 texcoord1 : TEXCOORD1;   // t1: .xyz = normal (view space), .w = depth
    float2 texcoord2 : TEXCOORD2;   // t2: base/glow uv
    float3 color     : COLOR0;      // v0: SH vertex colour
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // ---- uniform-only folds (match the original effect preshader) ----
    float  po0 = (float)g_LightProbeMode * (float)g_LightProbeMode;         // probe select (==0 -> probe path)
    float3 po1 = min((1.0).xxx, max((0.0).xxx, Light0_PreCalcLightColor.xyz));
    float  po2 = (float)g_iHighlightOutline * (float)g_iHighlightOutline;   // highlight select
    float  pr3 = (float)g_bDarkMapColor - 1.0;
    float  po3 = pr3 * pr3;                                                 // darkmap select
    float3 po4 = g_fDarkMapColorWeight * g_DarkMapColorForChar;

    float4 base = tex2D(BaseSampler, i.texcoord2);
    float4 glow = tex2D(GlowSampler, i.texcoord2);

    // rim weight used by the highlight-on path
    float rimW = saturate(dot(Light0_WorldViewDir.xyz, -i.texcoord.xyz)) * 0.4;

    // SH-toon diffuse term from the vertex colour
    float sh = max(-(i.color.x * 0.34 + i.color.y * 0.66) + 0.8, 0.0);
    float diffScale = sh * fSHPower + 1.0;
    float diffBias  = sh * 0.3 + 0.1;
    float3 lightVtx = saturate(i.color * diffScale + diffBias);   // per-vertex SH light colour

    float3 N   = normalize(i.texcoord1.xyz);
    float  ndl = dot(Light0_WorldViewDir.xyz, N);

    // light-probe select of the scene light colour (pre-ramp)
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;             // probe-mode 0 -> PreCalcLightColor

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
    float3 smoothD = lerp(shade2, lightColor, hl);
    float3 newLit  = lerp(banded, smoothD, g_toon0.y);               // realisticMix

    // master enable: 0 => original ramp exactly. 'shaded' feeds the rest of the shader unchanged.
    float3 shaded = lerp(litOrig, newLit, g_toon0.x);

    float3 C = base.xyz * shaded;
    float  alpha = base.w * MaterialColor.w;

    // highlight fresnel rim (original additive contribution)
    float fres = 1.0 - saturate(dot(i.texcoord.xyz, N));
    fres = fres * fres; fres = fres * fres;            // (1 - dot(-V,N))^4
    float3 rimCol = fres * Light0_Diff.xyz;
    float3 rimOn  = rimCol * rimW + C;
    float3 rimOff = rimCol * 0.4 + C;
    float3 litrim = (-po2 >= 0) ? rimOff : rimOn;      // highlight select -> lit + rim

    // glow-driven half-vector specular
    float3 S = shaded * litrim;
    float3 H = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
    float  sp = pow(max(dot(H, N), 0.0), glow.w * 64.0) * glow.y;
    float3 specR1  = sp * S;
    float3 colOff  = saturate(S * sp + litrim);                        // probe-off spec
    float3 colOn   = saturate(specR1 * Light0_Spec.xyz + litrim);      // probe-on spec
    float3 col = (-po0 >= 0) ? colOn : colOff;                         // probe select

    float3 hairOff = colOff * CustomHairColor.xyz;                     // probe-off final (darkmap skipped)
    col = col * CustomHairColor.xyz;

    float3 dark    = col * po4;
    float3 darkMin = min(col, dark);
    col = (-po3 >= 0) ? darkMin : col;                                 // darkmap select
    col = (-po0 >= 0) ? col : hairOff;                                 // probe select

    // --- additive high-color (Blinn spec) + rim + saturation grade, gated by master enable ---
    // Placed after the probe re-select so the toon add-on applies in BOTH probe modes; with
    // enable = 0 the additive term is 0 and saturation = 1 -> byte-identical to the original.
        // --- per-material PBR-ish layer: gloss/roughness/metallic spec + rim + warm shadow (enable-gated) ---
    float  _pmExp  = exp2(lerp(7.0, 2.0, saturate(g_mat0.y)));        // roughness -> Blinn exp (128..4)
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    float  _pmSpec = pow(saturate(dot(N, H)), _pmExp) * g_mat0.x;   // gloss = strength
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));     // metallic -> tint spec by surface
    col += (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x;

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
