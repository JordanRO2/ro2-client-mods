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
float4 g_mat0 : register(c218); // Metal: gloss, roughness, metallic, warmShadow
float4 g_mat1 : register(c219); // Metal: rimColor.rgb * strength, rimWidth

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
