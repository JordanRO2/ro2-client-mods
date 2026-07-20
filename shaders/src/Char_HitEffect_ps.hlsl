// Char_HitEffect MAIN LIT pixel shader — TOON/REALISTIC live-tunable variant (Shader0).
// PS-splice only; the GPU-skinning VS stays byte-identical (SkinBone pack untouched).
// Global param NAMES match the char effect globals so name-binding + preshader stay compatible.
//
// HitEffect specifics vs Char_Default (kept byte-faithful):
//   - dual rim-highlight branch (both branches add rim; scale 0.4 vs 0.4*saturate(dot(L,-V)))
//   - specular section (H.N)^16 * 0.2 with LightProbe / Light0_Spec branches
//   - final x2 brightness (the hit flash)
//
// Tunables live in HIGH ps_3_0 constant registers c220..c223; the DLL pushes them via
// IDirect3DDevice9::SetPixelShaderConstantF(220,...) every render frame. When those
// registers are 0 (master enable = 0) the output is BYTE-IDENTICAL to the original
// hit-flash look -> safe drop-in / A-B toggle. The toon double-shade replaces the core
// lit term (which HitEffect reuses in its rim/spec branches), so the whole flash tracks
// the toon bands when enabled.

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
sampler2D ToonSampler : register(s1);

// ---- live-tuning constants (device registers, NOT effect params) ----
float4 g_toon0 : register(c220); // x=enable       y=realisticMix z=sh1Step   w=sh1Feather
float4 g_toon1 : register(c221); // x=sh2Step      y=sh2Feather   z=rimStr     w=rimWidth
float4 g_toon2 : register(c222); // xyz=shade1Tint (rgb)          w=specStr
float4 g_toon3 : register(c223); // xyz=shade2Darken (rgb)        w=saturation
float4 g_mat0 : register(c216); // Cloth: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c217); // Cloth: rimColor.rgb * strength, rimWidth

struct PS_IN {
    float4 texcoord  : TEXCOORD0;   // t0 : xyz = view dir V, w = fog factor
    float4 texcoord1 : TEXCOORD1;   // t1 : xyz = view normal N, w = depth
    float2 texcoord2 : TEXCOORD2;   // t2 : base uv
    float3 color     : COLOR0;      // v0 : vertex colour / SH tint
};
struct PS_OUT { float4 color : COLOR0; float4 color1 : COLOR1; };

PS_OUT main(PS_IN i)
{
    PS_OUT o = (PS_OUT)0;

    // uniform-only (fxc folds these into a preshader, like the original)
    float  po0 = g_LightProbeMode * g_LightProbeMode;              // c0
    float3 po1 = saturate(Light0_PreCalcLightColor.xyz);          // c1
    float  po2 = g_iHighlightOutline * g_iHighlightOutline;       // c2
    float  prd = (float)g_bDarkMapColor - 1.0;
    float  po3 = prd * prd;                                       // c3
    float3 po4 = g_fDarkMapColorWeight * g_DarkMapColorForChar;   // c4

    float3 viewDir = i.texcoord.xyz;                              // V
    float3 N       = normalize(i.texcoord1.xyz);                  // normal

    float4 base = tex2D(BaseSampler, i.texcoord2);

    // back-facing light term used to scale the highlight rim
    float ndlBack  = saturate(dot(Light0_WorldViewDir.xyz, -viewDir));
    float rimScale = ndlBack * 0.4;

    // vertex-colour / SH tint  ->  diffuse scale & bias
    float sh = i.color.y * 0.66;
    sh = i.color.x * 0.34 + sh;
    sh = -sh + 0.8;
    sh = max(sh, 0.0);
    float diffScale = sh * fSHPower + 1.0;
    float diffBias  = sh * 0.3 + 0.1;

    // toon ramp from N.L
    float ndl = dot(Light0_WorldViewDir.xyz, N);

    // scene light colour: probe (g_LightProbeMode==0) or per-vertex SH colour
    float3 lightVtx   = saturate(i.color * diffScale + diffBias);   // natural RGB order
    float3 lightColor = (-po0 >= 0) ? po1 : lightVtx;               // -po0>=0 <=> g_LightProbeMode==0

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

    // master enable: 0 => original ramp exactly. HitEffect reuses 'lit' in its rim/spec branches.
    float3 lit = lerp(litOrig, newLit, g_toon0.x);

    float3 col   = base.xyz * lit;
    float  alpha = base.w * MaterialColor.w;

    // fresnel rim (1 - V.N)^4
    float fres = saturate(dot(viewDir, N));
    fres = -fres + 1.0;
    fres = fres * fres; fres = fres * fres;
    float3 rimCol = fres * Light0_Diff.xyz;

    float3 colHi  = rimCol * 0.4      + col;   // highlightOutline == 0
    float3 colHiB = rimCol * rimScale + col;   // highlightOutline != 0
    col = (-po2 >= 0) ? colHi : colHiB;

    // specular (H.N)^16 * 0.2
    float3 litCol  = lit * col;
    float3 H       = normalize(viewDir + Light0_WorldViewDir.xyz);
    float  hn      = max(dot(H, N), 0.0);
    float  spec    = pow(hn, 16.0) * 0.2;
    float3 specCol = spec * litCol;
    float3 colNoTint = saturate(litCol * spec + col);                    // probe branch
    float3 colSpec   = saturate(specCol * Light0_Spec.xyz + col);        // non-probe branch
    col = (-po0 >= 0) ? colSpec : colNoTint;

    // dark-map colour weighting
    float3 dark    = col * po4;
    float3 darkMin = min(col, dark);
    col = (-po3 >= 0) ? darkMin : col;
    col = (-po0 >= 0) ? col : colNoTint;       // probe re-select

    // --- additive high-color (Blinn spec) + rim + saturation grade, gated by master enable ---
    // NOTE: placed AFTER the dark-map/probe re-select above. That re-select restores colNoTint
    // in the non-probe branch and would discard any earlier additions; with enable=0 this block
    // is a no-op (adds 0, sat=1) so the original hit-flash stays byte-identical either way.
    float3 Hs      = normalize(i.texcoord.xyz + Light0_WorldViewDir.xyz);
        // --- per-material PBR-ish layer: gloss/roughness/metallic spec + rim + warm shadow (enable-gated) ---
    float  _pmExp  = exp2(lerp(7.0, 2.0, saturate(g_mat0.y)));        // roughness -> Blinn exp (128..4)
    float  _pmRimA = 1.0 - saturate(dot(N, i.texcoord.xyz));
    float  _pmRimW = pow(_pmRimA, exp2(lerp(3.0, 0.0, g_mat1.w))) * saturate(hl);
    float  _pmSpec = pow(saturate(dot(N, Hs)), _pmExp) * g_mat0.x;   // gloss = strength
    col = lerp(col, col * float3(1.15, 0.93, 0.80), g_mat0.w * saturate(1.0 - hl) * g_toon0.x); // warm shadow
    float3 _pmTint = lerp(lightColor, col, saturate(g_mat0.z));     // metallic -> tint spec by surface
    col += (_pmSpec * _pmTint + g_mat1.xyz * _pmRimW) * g_toon0.x;

    float sat    = lerp(1.0, g_toon3.w, g_toon0.x);
    float satLum = dot(col, float3(0.299, 0.587, 0.114));
    col = lerp(satLum.xxx, col, sat);

    // (No ddx/ddy ink outline — screen-space normal derivatives faceted the model
    //  per-triangle and darkened edges. Outlines come from the SSAO post-process.)

    col = lerp(col, g_FogColor, i.texcoord.w);
    col = col + col;                           // HitEffect x2 brightness (hit flash)

    o.color  = float4(col, alpha);
    o.color1 = float4(i.texcoord1.www, 1.0);
    return o;
}
