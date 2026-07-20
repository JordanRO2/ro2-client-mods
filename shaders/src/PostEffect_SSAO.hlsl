row_major float4x4 matWorldView : WORLDVIEW;
row_major float4x4 matViewPorj : VIEWPROJECTION;
row_major float4x4 matWorldViewProj : WorldViewProjection : register(c0);
row_major float4x4 matView : VIEW;
row_major float4x4 matInvView : InvView;
row_major float4x4 matInvWorldView : InvWorldView;
row_major float4x4 matWorld : WORLD;
float4 vViewDimensions : GLOBAL;
float3 G_Ambient : GLOBAL;
float fFogDistStart_SSAO : GLOBAL = { 0.01 };
float fFogNearFarDist_SSAO : GLOBAL = { 0.2 };
texture OrgTexture <string NTM = "shader"; int NTMIndex = 0;>; // 1
texture DepthTexture <string NTM = "shader"; int NTMIndex = 1;>; // 3
sampler OrgSampler =
sampler_state
{
    Texture = <OrgTexture>; // 5
    AddressU = 3;
    AddressV = 3;
    MagFilter = 2;
    MinFilter = 2;
    MipFilter = 1;
};
sampler DepthSampler =
sampler_state
{
    Texture = <DepthTexture>; // 6
    AddressU = 3;
    AddressV = 3;
    MagFilter = 2;
    MinFilter = 2;
    MipFilter = 1;
};
// ================================================================
//  DEPTH-EDGE OUTLINE  (screen-space, subject-COLOURED silhouette rim)
//  Runs on top of the SSAO post-process. After the SSAO colour is
//  computed, the scene DEPTH is sampled in one thin cross+diagonal
//  ring, each tap linearized with the engine's own depth curve
//    Lin(d) = 1/(d*1000+4)  ==  1/((1-d)*(-1000)+1004).
//  Larger raw depth d = farther;  Lin is LARGER for near, SMALLER far.
//
//  DIRECTIONAL (outside-only): a pixel is rimmed ONLY when a neighbor
//  is NEARER than it (neighborLin > centerLin). The rim lands on the
//  BACKGROUND side of a silhouette and never eats into the subject
//  (a subject pixel's neighbors are all farther-or-equal -> no rim).
//  No sky/far-plane guard is needed: uniform sky has no nearer
//  neighbor (edge~0), and a sky pixel next to a char IS rimmed (that
//  is the char-vs-sky outline we want).
//
//  SUBJECT-COLOURED: while scanning, the OFFSET of the NEAREST
//  neighbor (max linDepth = the foreground subject that triggers the
//  edge) is tracked. The scene COLOUR is sampled in that direction
//  and the rim is painted with the subject's own colour, darkened by
//  g_OutlineTint (red char -> dark-red rim, blue char -> dark-blue rim).
//  --- TUNING CONSTANTS (edit these) ---
float g_OutlineThickness = 1.5;  // rim tap radius / thickness ("Size"), in pixels
float g_OutlineThreshold = 0.0025;  // min NEARER-neighbor linDepth gap = an edge
#define OUTLINE_SHARP   600.0   // edge ramp hardness (large = hard/binary line)
float g_OutlineStrength = 0.8;  // rim opacity / strength (0 = off .. 1 = full)
float g_OutlineTint = 0.35;  // rim brightness vs subject (0 = black, 1 = full colour)
float g_OutlineEnable = 1.0;  // mod-menu toggle (0/1), set via ID3DXEffect::SetFloat
// --- NEW live-tunable params (bare floats, no semantic; the DLL SetFloats them) ---
float g_OutlineCutoutSkip = 0.0;    // 0..1  skip internal alpha-test cutout edges (hair/foliage):
                                    //       gates on the gap RELATIVE to subject depth, so only real
                                    //       silhouettes (big relative gap) survive. 0 = off (rim all edges)
#define OUTLINE_CUT_SHARP 8.0       // cutout-skip ramp hardness
float g_OutlineDistanceFade = 0.0;  // 0..1  fade (OPACITY) the outline on distant subjects (0 = same at all depths)
#define OUTLINE_DIST_SCALE 16.0     // distance curve scale (larger = reaches farther) - shared by fade + thickness
float g_OutlineDistThick = 0.0;     // 0..1  scale THICKNESS by distance: near = full (max), far = thin (0 = uniform)
#define OUTLINE_DIST_THICK_FLOOR 0.30  // far subjects keep at least this fraction of the thickness
// DISTANCE UNIFORM: keep the outline equally solid at any range. (This is the screen-space
// outline, i.e. the post-process silhouette edge -- NOT the per-material fresnel rim light in
// the Char_* shaders. The _rim* identifiers below are legacy naming for the outline's colour.)
// The ring radius is in PIXELS, so the outline's geometric width is already distance-invariant;
// what decays is its STRENGTH. The edge test compares the ABSOLUTE linDepth gap, and OLIN
// compresses with range, so a far silhouette's gap collapses toward the threshold and _edge
// fades to nothing (reads as "the outline thins out and vanishes"). Scaling the threshold by
// depth does NOT fix it: gap and threshold then shrink together and the difference stays tiny.
// The RELATIVE gap (gap / subject depth) IS scale-invariant -- a silhouette against distant
// background reads ~1 at any range -- so blending the test toward the relative form holds the
// outline solid far away. 0 = stock absolute test, 1 = fully distance-invariant.
float g_OutlineDistUniform = 0.0;
#define OUTLINE_REL_K     140.0     // relative-gap threshold per unit of g_OutlineThreshold (0.0025*140 = 0.35)
#define OUTLINE_REL_SHARP 8.0       // relative-gap ramp hardness
float g_OutlineColorR = 1.0;        // solid rim colour R (used when ColorMix > 0)
float g_OutlineColorG = 1.0;        // solid rim colour G
float g_OutlineColorB = 1.0;        // solid rim colour B
float g_OutlineColorMix = 0.0;      // 0 = subject-coloured (tint), 1 = solid RGB colour, blends between
// Linearize a raw depth sample with the engine's depth curve
#define OLIN(U) (1.0 / (tex2D(DepthSampler, (U)).x * 1000.0 + 4.0))
// ================================================================
// Shader0 Pixel_3_0 Has PRES True
struct PS_IN_Shader0
{
    float2 texcoord : TEXCOORD;
    float2 texcoord1 : TEXCOORD1;
};

float4 Shader0(PS_IN_Shader0 i) : COLOR
{
    float4 color = (float4)0;
    float4 temp0 = (float4)0, temp1 = (float4)0;
    float3 temp2 = (float3)0;
    // preshader (reconstructed; fxc re-folds uniform-only math into a preshader)
    float4 _po0 = 0;
    float4 _po1 = 0;
    float4 _po2 = 0;
    float4 _po3 = 0;
    float4 _po4 = 0;
    float4 _po5 = 0;
    float4 _po6 = 0;
    float4 _po7 = 0;
    float4 _po8 = 0;
    float4 _po9 = 0;
    float4 _po10 = 0;
    float4 _po11 = 0;
    float4 _po12 = 0;
    float4 _po13 = 0;
    float4 _po14 = 0;
    float4 _po15 = 0;
    float4 _po16 = 0;
    float4 _po17 = 0;
    float4 _po18 = 0;
    float4 _po19 = 0;
    float4 _po20 = 0;
    float4 _po21 = 0;
    float4 _po22 = 0;
    float4 _po23 = 0;
    float4 _po24 = 0;
    float4 _po25 = 0;
    float4 _po26 = 0;
    float4 _po27 = 0;
    float4 _po28 = 0;
    float4 _po29 = 0;
    float4 _po30 = 0;
    float4 _po31 = 0;
    float4 _po32 = 0;
    _po0.x = (1.0 / fFogNearFarDist_SSAO.x);
    _po1.xy = (vViewDimensions.xy * (-1.106057047843933));
    _po2.x = (vViewDimensions.x * (0.1311510056257248));
    _po2.y = (vViewDimensions.y * (-1.4942549467086792));
    _po3.xy = (vViewDimensions.xy * (0.9126440286636353));
    _po4.x = (vViewDimensions.x * (-1.4770660400390625));
    _po4.y = (vViewDimensions.y * (-0.26129698753356934));
    _po5.xy = (vViewDimensions.xy * (1.2656500339508057));
    _po6.x = (vViewDimensions.x * (-0.38944199681282043));
    _po6.y = (vViewDimensions.y * (1.4485629796981812));
    _po7.xy = (vViewDimensions.xy * (-0.6913220286369324));
    _po8.x = (vViewDimensions.x * (1.408964991569519));
    _po8.y = (vViewDimensions.y * (0.5146039724349976));
    _po9.xy = (vViewDimensions.xy * (-2.310899019241333));
    _po10.x = (vViewDimensions.x * (1.0597079992294312));
    _po10.y = (vViewDimensions.y * (-2.264292001724243));
    _po11.xy = (vViewDimensions.xy * (0.7481020092964172));
    _po12.x = (vViewDimensions.x * (-2.162966012954712));
    _po12.y = (vViewDimensions.y * (-1.2536259889602661));
    _po13.xy = (vViewDimensions.xy * (2.441718101501465));
    _po14.x = (vViewDimensions.x * (-1.4379409551620483));
    _po14.y = (vViewDimensions.y * (2.0450730323791504));
    _po15.xy = (vViewDimensions.xy * (-0.32112398743629456));
    _po16.x = (vViewDimensions.x * (1.9115159511566162));
    _po16.y = (vViewDimensions.y * (1.6112430095672607));
    _po17.xy = (vViewDimensions.xy * (-4.4961700439453125));
    _po18.x = (vViewDimensions.x * (3.1899681091308594));
    _po18.y = (vViewDimensions.y * (-3.1739730834960938));
    _po19.xy = (vViewDimensions.xy * (-0.20821300148963928));
    _po20.x = (vViewDimensions.x * (-2.882906913757324));
    _po20.y = (vViewDimensions.y * (-3.455264091491699));
    _po21.xy = (vViewDimensions.xy * (4.4597601890563965));
    _po22.x = (vViewDimensions.x * (-3.6940948963165283));
    _po22.y = (vViewDimensions.y * (2.569758892059326));
    _po23.xy = (vViewDimensions.xy * (0.9880819916725159));
    _po24.x = (vViewDimensions.x * (2.2369279861450195));
    _po24.y = (vViewDimensions.y * (3.904632091522217));
    _po25.xy = (vViewDimensions.xy * (-8.097622871398926));
    _po26.x = (vViewDimensions.x * (7.716606140136719));
    _po26.y = (vViewDimensions.y * (-3.564265012741089));
    _po27.xy = (vViewDimensions.xy * (-3.28239107131958));
    _po28.x = (vViewDimensions.x * (-2.8759219646453857));
    _po28.y = (vViewDimensions.y * (-7.998692035675049));
    _po29.xy = (vViewDimensions.xy * (7.523637771606445));
    _po30.x = (vViewDimensions.x * (-8.219512939453125));
    _po30.y = (vViewDimensions.y * (2.165550947189331));
    _po31.xy = (vViewDimensions.xy * (4.5980329513549805));
    _po32.x = (vViewDimensions.x * (1.4385939836502075));
    _po32.y = (vViewDimensions.y * (8.377376556396484));
    // Def c34
    // Def c35
    // Def c36
    // Def c37
    // Def c38
    // Dcl r5.x v0.xy
    // Dcl r5.yxxx v1.xy
    // Dcl v0.x s0
    // Dcl v0.x s1
    // Add r0.xy c1 v1
    temp0.xy = _po1.xy + i.texcoord1.xy;
    // Tex r0 r0 s1
    temp0 = tex2D(DepthSampler, temp0);
    // Mad r0.x r0.x c36.x c36.y
    temp0.x = temp0.x * 1000 + 4;
    // Rcp r0.x r0.x
    temp0.x = 1 / temp0.x;
    // Tex r1 v1 s1
    temp1 = tex2D(DepthSampler, i.texcoord1);
    // Mad r0.y r1.x c36.x c36.y
    temp0.y = temp1.x * 1000 + 4;
    // Add r0.z r1.x -c33.x
    temp0.z = temp1.x + -fFogDistStart_SSAO.x;
    temp0.z = saturate(temp0.z);
    // Mov r1.z c36.z
    temp1.z = 1;
    // Mad r0.z r0.z -c0.x r1.z
    temp0.z = temp0.z * -_po0.x + temp1.z;
    temp0.z = saturate(temp0.z);
    // Rcp r0.y r0.y
    temp0.y = 1 / temp0.y;
    // Mul r0.y r0.y c36.y
    temp0.y = temp0.y * 4;
    // Mad r0.x r0.x c36.y -r0.y
    temp0.x = temp0.x * 4 + -temp0.y;
    // Mad r0.w r0.x -c34.x c34.y
    temp0.w = temp0.x * -38 + 1;
    temp0.w = saturate(temp0.w);
    // Mul r0.w r0.w r0.w
    temp0.w = temp0.w * temp0.w;
    // Mad r1.x r0.x c34.z c34.w
    temp1.x = temp0.x * 150 + -0.048;
    temp1.x = saturate(temp1.x);
    // Mul r0.x r0.x c36.w
    temp0.x = temp0.x * 34;
    // Mad r0.x r0.x -r0.x c36.z
    temp0.x = temp0.x * -temp0.x + 1;
    // Mul r0.w r0.w r1.x
    temp0.w = temp0.w * temp1.x;
    // Mul r0.w r0.x r0.w
    temp0.w = temp0.x * temp0.w;
    // Cmp r0.x r0.x r0.w c35.x
    temp0.x = (temp0.x >= 0) ? temp0.w : 0;
    // Add r1.xy c2 v1
    temp1.xy = _po2.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c3 v1
    temp1.xy = _po3.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c4 v1
    temp1.xy = _po4.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c5 v1
    temp1.xy = _po5.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c6 v1
    temp1.xy = _po6.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c7 v1
    temp1.xy = _po7.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c8 v1
    temp1.xy = _po8.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c9 v1
    temp1.xy = _po9.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c10 v1
    temp1.xy = _po10.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c11 v1
    temp1.xy = _po11.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c12 v1
    temp1.xy = _po12.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c13 v1
    temp1.xy = _po13.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c14 v1
    temp1.xy = _po14.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c15 v1
    temp1.xy = _po15.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c16 v1
    temp1.xy = _po16.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.y
    temp1.x = temp1.x * 0.83;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c17 v1
    temp1.xy = _po17.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c18 v1
    temp1.xy = _po18.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c19 v1
    temp1.xy = _po19.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c20 v1
    temp1.xy = _po20.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c21 v1
    temp1.xy = _po21.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c22 v1
    temp1.xy = _po22.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c23 v1
    temp1.xy = _po23.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c24 v1
    temp1.xy = _po24.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.z
    temp1.x = temp1.x * 0.56;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c25 v1
    temp1.xy = _po25.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c26 v1
    temp1.xy = _po26.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c27 v1
    temp1.xy = _po27.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c28 v1
    temp1.xy = _po28.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c29 v1
    temp1.xy = _po29.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c30 v1
    temp1.xy = _po30.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c31 v1
    temp1.xy = _po31.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.w r0.w c36.y -r0.y
    temp0.w = temp0.w * 4 + -temp0.y;
    // Mad r1.x r0.w -c34.x c34.y
    temp1.x = temp0.w * -38 + 1;
    temp1.x = saturate(temp1.x);
    // Mul r1.x r1.x r1.x
    temp1.x = temp1.x * temp1.x;
    // Mad r1.y r0.w c34.z c34.w
    temp1.y = temp0.w * 150 + -0.048;
    temp1.y = saturate(temp1.y);
    // Mul r0.w r0.w c36.w
    temp0.w = temp0.w * 34;
    // Mad r0.w r0.w -r0.w c36.z
    temp0.w = temp0.w * -temp0.w + 1;
    // Mul r1.x r1.x r1.y
    temp1.x = temp1.x * temp1.y;
    // Mul r1.x r0.w r1.x
    temp1.x = temp0.w * temp1.x;
    // Mul r1.x r1.x c35.w
    temp1.x = temp1.x * 0.31;
    // Cmp r0.w r0.w r1.x c35.x
    temp0.w = (temp0.w >= 0) ? temp1.x : 0;
    // Add r0.x r0.w r0.x
    temp0.x = temp0.w + temp0.x;
    // Add r1.xy c32 v1
    temp1.xy = _po32.xy + i.texcoord1.xy;
    // Tex r1 r1 s1
    temp1 = tex2D(DepthSampler, temp1);
    // Mad r0.w r1.x c36.x c36.y
    temp0.w = temp1.x * 1000 + 4;
    // Rcp r0.w r0.w
    temp0.w = 1 / temp0.w;
    // Mad r0.y r0.w c36.y -r0.y
    temp0.y = temp0.w * 4 + -temp0.y;
    // Mad r0.w r0.y -c34.x c34.y
    temp0.w = temp0.y * -38 + 1;
    temp0.w = saturate(temp0.w);
    // Mul r0.w r0.w r0.w
    temp0.w = temp0.w * temp0.w;
    // Mad r1.x r0.y c34.z c34.w
    temp1.x = temp0.y * 150 + -0.048;
    temp1.x = saturate(temp1.x);
    // Mul r0.y r0.y c36.w
    temp0.y = temp0.y * 34;
    // Mad r0.y r0.y -r0.y c36.z
    temp0.y = temp0.y * -temp0.y + 1;
    // Mul r0.w r0.w r1.x
    temp0.w = temp0.w * temp1.x;
    // Mul r0.w r0.y r0.w
    temp0.w = temp0.y * temp0.w;
    // Mul r0.w r0.w c35.w
    temp0.w = temp0.w * 0.31;
    // Cmp r0.y r0.y r0.w c35.x
    temp0.y = (temp0.y >= 0) ? temp0.w : 0;
    // Add r0.x r0.y r0.x
    temp0.x = temp0.y + temp0.x;
    // Mul r0.x r0.x c37.x
    temp0.x = temp0.x * 0.075;
    temp0.x = saturate(temp0.x);
    // Pow r1.x r0.x c37.y
    temp1.x = pow(temp0.x, 0.72);
    // Mul r0.x r0.z r1.x
    temp0.x = temp0.z * temp1.x;
    // Mad r0.y r0.x -c37.z c37.w
    temp0.y = temp0.x * -0.45 + 1;
    // Tex r1 v0 s0
    temp1 = tex2D(OrgSampler, i.texcoord);
    // Mul r2.xyz r0.y r1
    temp2.xyz = temp0.y * temp1;
    // Mul r0.xyw r0.x r2.xyzz
    temp0.xyw = temp0.x * temp2;
    // Mad r0.xyw r0 c38.xyzz r2.xyzz
    temp0.xyw = temp0.xyw * float3(-0.060000002, -0.029999971, 0.059999943) + temp2;
    // Cmp oC0.xyz -r0.z r1 r0.xyww
    color.xyz = (-temp0.z >= 0) ? temp1 : temp0.xyw;
    // ============ SUBJECT-COLOURED OUTLINE (directional) =============
    float2 _ouv = i.texcoord1.xy;         // depth-target UV
    float2 _ots = vViewDimensions.xy;     // texel size (1/w, 1/h)
    float  _oCl = OLIN(_ouv);             // center linear depth (LARGE=near, SMALL=far)
    // base tap radius (= max thickness)
    float  ex0 = _ots.x * g_OutlineThickness, ey0 = _ots.y * g_OutlineThickness, l;
    // DISTANCE THICKNESS: cheap 4-tap probe at base radius finds the nearest subject
    // depth (_pN), then scale the ring radius so NEAR subjects get full thickness and
    // FAR subjects a thinner rim. _distT = 1 when g_OutlineDistThick = 0 (uniform).
    float _pN = _oCl;
    _pN = max(_pN, OLIN(_ouv+float2( ex0,0.0)));
    _pN = max(_pN, OLIN(_ouv+float2(-ex0,0.0)));
    _pN = max(_pN, OLIN(_ouv+float2(0.0, ey0)));
    _pN = max(_pN, OLIN(_ouv+float2(0.0,-ey0)));
    float _distT = lerp(1.0, max(OUTLINE_DIST_THICK_FLOOR, saturate(_pN * OUTLINE_DIST_SCALE)), g_OutlineDistThick);
    float  ex = ex0 * _distT, ey = ey0 * _distT;
    float  _oEdge = 0.0;                  // directional edge (only NEARER neighbors count)
    float  _oBest = _oCl;                 // nearest (max linDepth) neighbor found so far
    float2 _oOff  = float2(0.0, 0.0);     // offset toward that nearest neighbor = the subject
    // one thin ring: track outside-only edge AND the direction of the nearest subject
    l = OLIN(_ouv+float2( ex,0.0)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2( ex,0.0);}
    l = OLIN(_ouv+float2(-ex,0.0)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2(-ex,0.0);}
    l = OLIN(_ouv+float2(0.0, ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2(0.0, ey);}
    l = OLIN(_ouv+float2(0.0,-ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2(0.0,-ey);}
    l = OLIN(_ouv+float2( ex, ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2( ex, ey);}
    l = OLIN(_ouv+float2( ex,-ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2( ex,-ey);}
    l = OLIN(_ouv+float2(-ex, ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2(-ex, ey);}
    l = OLIN(_ouv+float2(-ex,-ey)); _oEdge=max(_oEdge,l-_oCl); if(l>_oBest){_oBest=l;_oOff=float2(-ex,-ey);}
    // hard edge factor (outside-only: >0 only where a nearer neighbor exists)
    // _relGap = gap RELATIVE to the subject's own depth. Scale-invariant: ~1 for a real
    // silhouette against distant background at ANY range, << 1 for internal cutout edges.
    float _relGap = _oEdge / max(_oBest, 1e-6);
    // ABSOLUTE test (stock). Note this is why Threshold feels dead: for a near silhouette
    // _oEdge (~0.2) dwarfs the slider's whole 0..0.01 span, and SHARP is 600, so the term
    // saturates to 1 across the entire travel. It also collapses to 0 at range.
    float _edgeAbs = saturate((_oEdge - g_OutlineThreshold) * OUTLINE_SHARP);
    // RELATIVE test: the same Threshold re-expressed in relative units (0.0025 -> 0.35). Here
    // the slider's travel spans "outline everything" .. "outline nothing" and, because the
    // relative gap does not shrink with range, the result is identical near and far.
    float _edgeRel = saturate((_relGap - g_OutlineThreshold * OUTLINE_REL_K) * OUTLINE_REL_SHARP);
    float _edge = lerp(_edgeAbs, _edgeRel, g_OutlineDistUniform);
    _edge *= g_OutlineEnable;
    // CUTOUT SKIP: internal alpha-test cutout edges (hair, foliage, capes) have a
    // nearer neighbour but only a SMALL gap RELATIVE to the subject's own depth,
    // whereas a real silhouette (subject vs far background) has a gap ~= the subject
    // depth (relative gap ~1). Gate on the relative gap so internal edges drop out as
    // g_OutlineCutoutSkip rises; 0 keeps the gate wide open (== old behaviour).
    _edge *= saturate(1.0 - (g_OutlineCutoutSkip - _relGap) * OUTLINE_CUT_SHARP);
    // DISTANCE FADE: _oBest is the subject's linear depth (LARGE=near, SMALL=far).
    // At g_OutlineDistanceFade=0 -> no fade; higher -> distant subjects fade out.
    _edge *= lerp(1.0, saturate(_oBest * OUTLINE_DIST_SCALE), g_OutlineDistanceFade);
    // subject colour: sample the scene colour toward the nearest foreground neighbor.
    // Colour UV is i.texcoord; depth UV is i.texcoord1 -- both are full-screen post
    // UVs, so the same texel offset (_oOff = vViewDimensions*g_OutlineThickness) applies to both.
    float3 _fgCol = tex2D(OrgSampler, i.texcoord + _oOff).xyz;
    // rim colour: blend the subject's OWN colour (darkened by g_OutlineTint) toward a
    // solid RGB colour by g_OutlineColorMix (0 = subject-coloured, 1 = solid RGB).
    float3 _rimSubject = _fgCol * g_OutlineTint;
    float3 _rimSolid   = float3(g_OutlineColorR, g_OutlineColorG, g_OutlineColorB);
    float3 _rim = lerp(_rimSubject, _rimSolid, g_OutlineColorMix);
    color.xyz = lerp(color.xyz, _rim, _edge * g_OutlineStrength);
    // =============== END SUBJECT-COLOURED OUTLINE ================
    // Mov oC0.w c36.z
    color.w = 1;
    // End

    return color;
}

// Shader1 Vertex_3_0 Has PRES False
struct VS_IN_Shader1
{
    float4 position : POSITION;
    float4 texcoord : TEXCOORD;
    float4 texcoord1 : TEXCOORD1;
};

struct VS_OUT_Shader1
{
    // texcoordout0 o0
    float4 position : POSITION;
    // texcoordout1 o1
    float2 texcoord : TEXCOORD;
    // texcoordout2 o2
    float2 texcoord1 : TEXCOORD1;
};

VS_OUT_Shader1 Shader1(VS_IN_Shader1 i)
{
    VS_OUT_Shader1 o = (VS_OUT_Shader1)0;
    float4 temp0 = (float4)0;
    // Dcl r0.x v0
    // Dcl r5.x v1
    // Dcl r5.yxxx v2
    // Dcl r0.x o0
    // Dcl r5.x o1.xy
    // Dcl r5.yxxx o2.xy
    // Mov r0.x c0.x
    temp0.x = matWorldViewProj[0].x;
    // Mov r0.y c1.x
    temp0.y = matWorldViewProj[1].x;
    // Mov r0.z c2.x
    temp0.z = matWorldViewProj[2].x;
    // Mov r0.w c3.x
    temp0.w = matWorldViewProj[3].x;
    // Dp4 o0.x v0 r0
    o.position.x = dot(i.position, temp0);
    // Mov r0.x c0.y
    temp0.x = matWorldViewProj[0].y;
    // Mov r0.y c1.y
    temp0.y = matWorldViewProj[1].y;
    // Mov r0.z c2.y
    temp0.z = matWorldViewProj[2].y;
    // Mov r0.w c3.y
    temp0.w = matWorldViewProj[3].y;
    // Dp4 o0.y v0 r0
    o.position.y = dot(i.position, temp0);
    // Mov r0.x c0.z
    temp0.x = matWorldViewProj[0].z;
    // Mov r0.y c1.z
    temp0.y = matWorldViewProj[1].z;
    // Mov r0.z c2.z
    temp0.z = matWorldViewProj[2].z;
    // Mov r0.w c3.z
    temp0.w = matWorldViewProj[3].z;
    // Dp4 o0.z v0 r0
    o.position.z = dot(i.position, temp0);
    // Mov r0.x c0.w
    temp0.x = matWorldViewProj[0].w;
    // Mov r0.y c1.w
    temp0.y = matWorldViewProj[1].w;
    // Mov r0.z c2.w
    temp0.z = matWorldViewProj[2].w;
    // Mov r0.w c3.w
    temp0.w = matWorldViewProj[3].w;
    // Dp4 o0.w v0 r0
    o.position.w = dot(i.position, temp0);
    // Mov o1.xy v1
    o.texcoord = i.texcoord.xy;
    // Mov o2.xy v2
    o.texcoord1 = i.texcoord1.xy;
    // End

    return o;
}

technique PostEffect_SSAO <bool UsesNiRenderState = 1;>
{
    pass P0
    {
        VertexShader = compile vs_3_0 Shader1(); // 7
        PixelShader = compile ps_3_0 Shader0(); // 8
    }
}


