# RO2 client — VRAM / RAM usage map & leak analysis

Static analysis of `Rag2.exe` (b303-2022-02-11, imagebase `0x400000`) behind the
confirmed **32-bit 2 GB address-space OOM**. The `laa` fix (see `../README.md`) buys
headroom (2 GB → 4 GB); this document maps **where the memory goes** so targeted fixes
can attack the root cause.

> **Status: PENDING runtime confirmation.** The static map below ranks candidates;
> which one actually dominates needs an x64dbg PrivateBytes diff across zone changes
> (see [Runtime confirmation plan](#runtime-confirmation-plan)).

## TL;DR

- The crash site — `Render_GameWorld` (`0x8219e0`) → `CSpeedTreeWrapper::UpdateTextureAndRender`
  (`0x94bd80`) — is a **symptom, not the leak**. It allocates ~nothing per frame; `operator new`
  simply fails there because the address space is already near the 2 GB cap.
- **Biggest lever:** every game texture is created in **`D3DPOOL_MANAGED`**, so D3D keeps a
  full **system-RAM shadow copy** → each texture costs its size **twice** in the 2 GB space.
  Any ref held across a zone change pins both copies.
- **Second:** zone teardown may not run before reload → per-zone blocks leak per map change.

The engine is Gamebryo/NiDX9; `d3d9.dll` is loaded via `LoadLibrary`/`GetProcAddress`
(no import-table entry). Texture creation flows through `d3dx9_42` thunks
(IAT `0x1355d04..0x1355da8`); geometry buffers go through device COM vtable calls.
Ref-counting thunks: `sub_135533C` = RefPtr-dec (release), `sub_1355340` = RefPtr-inc.

## VRAM — video-memory allocation sites

| # | Site | Subsystem | What / scale | Pool | Notes |
|---|---|---|---|---|---|
| V1 | **`sub_BEE0B0`** @ `0xbee0b0` (in `NiDX9SourceTextureData::LoadFromFile` @ `0xbec5e0`) | **All game textures** (models, terrain, UI DDS) | Primary texture creator — `D3DXCreateTextureFromFileInMemory` (2D), `…CubeTexture…`, `…VolumeTexture…`; image loaded whole into RAM first | **MANAGED** (4-arg overload defaults Usage=0, Pool=MANAGED) | **Highest-value VRAM item.** MANAGED = RAM shadow → **2× cost** per texture. Full mip chain (not capped here). |
| V2 | `NiDX9SourceTextureData::LoadFromFile` slow path @ `0xbec5e0` (`sub_BD8BB0` → renderer `CreateTexture` COM `*device+40` @ `0xbecb1c`) | Non-DDS textures | Format/usage/pool computed at runtime from `a2` render-state flags | **runtime-determined** (unresolved statically) | Max-dimension gates: `sub_BEE990/9B0/9D0`; oversize rejected via `sub_C5BC00`. |
| V3 | **`sub_1084400`** (from `CLocalization::LoadLocalizedTexture` @ `0x1083c90`) | **UI / localized textures** | `D3DXCreateTextureFromFileInMemoryEx` / `…FromFileExW` @ `0x108475d` | **MANAGED**, MipLevels=1, Fmt=-3 | **Cached & ref-counted** (hash map `sub_108A080/108A100`; cache hit `++*v44`) → UI textures reuse. Not a growth source. |
| V4 | Render targets: `RenderTarget_Create` @ `0x52e1a0`, `RenderTargetGroup_Initialize` @ `0x51b360`, `CPostEffectSMAA::CreateRenderTargets` @ `0x45fd80`, `BrightPass…` @ `0x526870`, `UICharEnhanceExchangeWnd::Setup3DRenderTarget` @ `0x4508b0`, `UICharacterEquipLinkControl::CreateRenderTarget` @ `0x9cede0` | Post-FX / UI 3D previews | Full-screen + preview RTs | **DEFAULT** (RTs must be) | Fixed count, res-scaled; `CheckRenderTargetResolution` @ `0x5295f0` gates size. UI preview RTs accumulate if recreated without release. |
| V5 | Geometry VB/IB: `NiDX9RenderedTextureData::Create` @ `0xbe78b0`; terrain `TerrainMesh_CreateGeometry` @ `0x647bd0`, `CTerrain::GenerateTerrainGeometry` @ `0x80fad0`, `VertexBuffer_FillFromData` @ `0x80d310`, `UpdateVertexBuffer` @ `0x4894c0` | Terrain / models | Device `CreateVertexBuffer`/`CreateIndexBuffer` (COM vtable, inside NiDX9 lib) | Gamebryo VB/IB mgr | Scales with `terrain_w*terrain_h` chunks + streamed model count. Exact COM call site not pinned statically. |

## RAM — system-heap allocation sites

### Per-zone (in `CZoneData::LoadZoneData` @ `0x8171f0`, once per zone load)

| Site | Size | Subsystem | Scale |
|---|---|---|---|
| `operator new(1196)` @ `0x81721f` → `CTerrain::Constructor` | 1196 B | Terrain root | fixed |
| `operator new[](304 * terrain_w * terrain_h + 4)` @ `0x817820` | **304 B × chunks** | **Terrain chunk array** (`CTerrainChunk::Initialize`) | **count-scaled** — dominant per-zone terrain block; w,h from `*(zone+60)+8/+12` |
| `operator new(262284)` @ `0x81796b` → `sub_A29310` | **256 KB** | **Grass/vegetation grid** (16384 cells × 16 B = 128×128) | fixed / zone |
| `new(536)`→`CZoneMgr`, `new(64)`→`CAreaInfoMgr`, `new(40)`→`CWeatherMgr` | small | zone managers | fixed |
| `CZoneData::LoadGrassData` @ `0xa29800` | RB-tree + growable vectors (`sub_A28DC0`/`sub_A29230`) | Grass cluster/instance data | **count-scaled** by grass instances |

### Model streaming (per-frame, distance-driven)

- `CZoneMgr::UpdateObjectsInRange` @ `0x80af10` — streams world models in/out by distance;
  schedules async `LoadModelData` and `UnloadModel` (`0x809ad0`). Async task at node `v31[98]`.
- `CZoneMgr::UnloadModel` @ `0x809ad0` **does** detach + remove from spatial grid +
  `operator delete` wrapper + release render-node ref + `DataCache_ReleaseData` (unload path present).

### Fixed / global

- Per-tree hash table inside each `CSpeedTreeWrapper` (`this[16]`/`this[17]`), cleared by
  `CSpeedTreeWrapper::Cleanup` @ `0x94a4d0`.

## Ranked leak candidates

1. **MANAGED-pool texture shadows never trimmed across zones — HIGH.** `sub_BEE0B0` creates
   every texture MANAGED → lives twice. Release is purely ref-count driven; any ref held past a
   zone change (UI element, cached material, SpeedTree/terrain not fully torn down) pins both.
   **Single largest lever on the 1.82 GB pressure.**
2. **~~Per-map-change zone leak~~ — REFUTED (2026-07).** `LoadZoneData` overwrites `this+40/60/…`
   without freeing only because it always runs on a FRESH zero-initialized `CZoneData` (ctor
   `0x8155A0` zeroes those fields). The zone-change owner **`CLoginMode::ChangeMap` (`0x827B80`)
   fully tears the old zone down first**: at `0x82893B` it calls the old `CZoneData`'s scalar-
   deleting destructor (`0x815840`, vtable[0], delete-flag 1) on `[CLoginMode+2B0h]`, nulls the
   slot, and only *then* `operator new(0x180)`s the new one. That destructor frees every heavy
   field — terrain chunk array (`this+40`, array-delete), the 262284-byte grass grid + its 16384
   cells and grass instance list (`sub_A293E0` + `operator delete`), CZoneMgr/terrain/weather/
   area-info. **So the zone object is NOT the session-long OOM driver** — do NOT add a "free the
   old zone" patch (double-free). The residual growth is shared/global caches (models/anim/
   texture/sound), which `cache_retention_30s` bounds (180 s → 30 s cooldown).
3. **Async model load/unload race in `UpdateObjectsInRange` — MED.** A model scheduled to load, moved
   out of range before the async load completes, may never get a matching `UnloadModel` → retains
   geometry (V5) + textures (V1).
4. **UI 3D-preview render targets — LOW-MED.** char-enhance / equip-link / costume-compose RTs
   (DEFAULT pool) accumulate if a window recreates its RT without releasing the old one.

## Candidate fixes → `patch_client.py`

> **Reviewed in IDA (2026-07, multi-agent pass).** The headline idea below — flipping the
> world/model texture pool inline — turned out **NOT to be inline-patchable**. Corrected
> findings:

1. **Eliminate the MANAGED RAM shadow — the biggest lever. NOT inline, but DONE via a
   `.patch` code cave (`texpool_world_default`, shipped opt-in).**
   `sub_BEE0B0` (in `NiDX9SourceTextureData::LoadFromFile`) calls the **non-Ex**
   `D3DXCreateTextureFromFileInMemory` (and Cube/Volume variants) — a 4-arg API with **no
   `D3DPOOL` parameter**; MANAGED is hard-wired inside `d3dx9_42.dll`. So there is no immediate
   to flip inline. **Solution:** the patcher now adds a `.patch` PE section with a cave that
   hooks the non-Ex thunk (`0xDF2E2E`) and re-issues the call through the existing `…Ex` thunk
   (`0x1182F40`) with **`Pool = D3DPOOL_DEFAULT`** (all other args mirror the non-Ex defaults).
   This drops the system-RAM shadow for the whole non-Ex texture path → ~halves texture
   address-space. The cave was verified to disassemble correctly and applies cleanly; **it has
   NOT been runtime-tested** (see risk). **HIGH risk:** DEFAULT resources are lost on device
   reset (fullscreen alt-tab / resolution change) and Gamebryo may not recreate them → best in
   windowed / borderless. This is the direct partner to `laa` for the 2 GB OOM.
   - *Also inline (shipped, opt-in, module `memory`):* the **UI / localized** texture paths use
     the `…Ex` overloads with a literal `push 1` (MANAGED); `tex_pool_ui_inmem`,
     `tex_pool_ui_disk`, `tex_pool_loc_inmem`, `tex_pool_loc_global` flip those to `push 0`
     (DEFAULT) via `patch_at_va`. Same device-lost risk, smaller (UI-only) benefit.
2. **`EvictManagedResources()` on zone load — VRAM only, does NOT help the 2 GB address space.**
   It frees the GPU copy of MANAGED resources but the D3D runtime keeps the **system-RAM backing
   store** (the address-space cost). Needs a cave. Mod-DLL VRAM hygiene at best; not shipped.
3. **Texture mip / dimension caps — no same-length edit available.** Every `…Ex` create already
   passes `MipLevels=1`, and `sub_BEE990/9B0/9D0` are plain device-cap getters (not gates); the
   create calls pass `Width/Height=-1` (`push imm8`), so a real cap needs `push imm32` (longer).
   A few small UI textures force uncompressed 32bpp and can be cut to 16bpp (`A4R4G4B4`) safely,
   but save only a few hundred KB — not shipped.
4. **Per-zone heap allocs (grass grid 256 KB `sub_A29310`, terrain chunk array `0x817820`) — NOT
   safely shrinkable.** Their sizes are either fixed struct sizes whose constructors write the full
   extent (shrinking = heap overflow) or `304 * w*h` computed from runtime counts (a cap desyncs
   the loop bounds). `no_grass` already skips grass loading wholesale, which is the safe lever.
5. **`pe` SizeOfStackReserve 1 MiB → 512 KiB (file `0x178`)** reclaims ~512 KiB VA per default-stack
   thread, but risks a stack-overflow crash on the engine's deep call paths → gate on a runtime
   peak-stack measurement before use; documented, not shipped.

**Bottom line for the 2 GB OOM:** after `laa`, the largest address-space win is the D3D
texture-creation pool flip on the main `sub_BEE0B0` path. It cannot be an inline byte edit, but
`texpool_world_default` now does it here with a `.patch` code cave (forces `D3DPOOL_DEFAULT`).
It is **opt-in and runtime-untested** because of the device-reset hazard — validate it live
(zone changes, alt-tab, resolution change), ideally windowed. A mod-DLL D3D hook remains the
more robust long-term home (it could recreate DEFAULT textures on device-lost, which the static
cave cannot).

## Full MANAGED-resource pool map (deep multi-agent pass, 2026-07)

Exhaustive enumeration of every D3D resource-creation site, to know exactly where the
system-RAM shadow lives and which sites are safe to flip to `DEFAULT`.

**Textures — ALL go through the D3DX helper family; the device COM `CreateTexture`
(vtbl+0x5C) is NEVER used for textures** (verified: zero `FF /2 5C` dispatches). So the
MANAGED-texture surface is fully enumerable:

| site | api | pool | status |
|---|---|---|---|
| `sub_BEE0B0` (2D, world/model/character — **the bulk**) | non-Ex InMemory (no pool arg) | MANAGED | **caved → DEFAULT** (`texpool_world_default`; the thunk hook also covers the 2nd non-Ex caller `sub_1086E90`) |
| `NiD3DXEffectShader::LoadTexture2D` @0xBA0DFE | FromFileExA, `push 1` | MANAGED | **inline flip shipped** (`tex_pool_shader_effect`) — largest secondary site |
| UI/localized loaders (`sub_1084400`, `CLocalization`, `sub_10862D0`) | InMemoryEx/FromFileEx, `push 1` | MANAGED | **inline flips shipped** (`tex_pool_ui_*`, `tex_pool_loc_*`), primary paths |
| cube (`0xBEE45E`) / volume (`0xBEE4A6`) | non-Ex Cube/Volume InMemory | MANAGED | NOT shipped — need a `GetProcAddress`-resolved Ex cube/volume cave (no Ex thunk imported) for only a few MB; low priority |
| render targets (device `CreateTexture` Usage=RT; `D3DXCreateRenderToSurface`; Gamebryo `RenderTarget_Create` @0x52E1A0) | — | DEFAULT already | **DENYLIST — never flip** (MANAGED+RT is invalid) |
| PNG→DDS converter `sub_1086AC0` @0x1086CAF | `D3DXCreateTexture`, `push 2` | SYSTEMMEM | **DENYLIST — LockRect'd, must stay lockable** |

**Safe-flip rule (used by all inline flips):** only a site whose pushed **`Usage==0` and pushed
`Pool==1` (MANAGED)** is flipped; render targets carry `Usage=1` and are already `DEFAULT`.

**Geometry:**
- Terrain streaming VB (`VertexBuffer_InitializeAll` @0x80D2B0, ~53 MB, 5×700k verts) is
  **already `D3DPOOL_DEFAULT` + `D3DUSAGE_DYNAMIC`** → no RAM shadow, nothing to reclaim, must
  stay DEFAULT. The largest single geometry allocation is already optimal.
- Navmesh debug VB/IB (`0x45AE20`) is MANAGED but ~144 B, debug-only → not worth it.
- **Static NiMesh geometry — FOUND & shipped (`geopool_static_default`).** Character/prop/
  weapon/armor static VB+IB are created **`D3DPOOL_MANAGED`** (RAM-shadowed). The single NiDX9
  buffer creator `sub_BE3E90` pushes `Pool = sub_BE3960(this) = (this[0x34] & 8)==0` → **1
  (MANAGED) for static, 0 (DEFAULT) for dynamic**; Usage is `8` (WRITEONLY), `|=0x200` when
  dynamic. Two 3-byte inline edits zero the pushed Pool register (VB `mov edx,[ebp-8]`@0xBE3F3C
  → `xor edx,edx;nop`; IB `mov ecx,[ebp-8]`@0xBE404F → `xor ecx,ecx;nop`) so it is `DEFAULT`
  unconditionally — dynamic already pushed 0, so only the static MANAGED case changes.
  **~100–300 MB of VB/IB RAM shadow reclaimed in populated zones** — the largest win after `laa`
  and the texture cave. Lower device-reset risk than the texture flips because dynamic streams
  already ride this DEFAULT path and are rebuilt via `sub_BE3BC0` on the `this[0x3C]==0` recreate.
  (The `NiDataStream` factory `unk_15C0BF0`/`vtable+1` route is a static dead-end — its vtable at
  `unk_13FD498` is runtime-filled/`0xFF` in the IDB — but the actual D3D create is the lazy
  renderer-side `sub_BE3E90`, which IS readable.)

**DXVK note:** the `clients/ClientArchive/Lastest` upgraded build runs D3D9→Vulkan via DXVK,
where a device reset is effectively never signalled. Under DXVK, every `DEFAULT`-pool flip here
(and the world cave) is **much safer** — the device-lost hazard that makes these opt-in largely
does not apply. On that build the memory fixes are close to free wins.

## Using more VRAM (the complement)

Moving the working set to `DEFAULT` frees 32-bit address space and shifts it to VRAM, which is
abundant on a modern PC. The opt-in `quality` fixes then *fill* that spare VRAM (all
address-space-safe — they spend DEFAULT-pool / render-target video memory, never the system-RAM
shadow): `tex_force_max_detail` (mip-levels-to-skip → 0, full-res textures), `shadowmap_2048`
(shadow RT 1024² → 2048²), `terrain_stream_2x` (terrain streaming VB pool 700k → 1.4M verts).

Two "use more VRAM" levers are **config-driven / not static-patchable** and were deliberately
not shipped: the world **view/streaming distance** (`CZoneMgr::UpdateObjectsInRange` reads a
config float at `unk_135806C`, populated at runtime — must be raised via the config or a live
x64dbg write, and it also grows the resident scene-graph in address space) and **anisotropic
filtering** (the client never calls `SetSamplerState(D3DSAMP_MAXANISOTROPY)`, so enabling AF
needs an injected call, not an immediate edit — mod-DLL territory).

## Could not resolve statically (needs runtime / x64dbg)

- D3DPOOL for the non-DDS renderer texture path (V2) — runtime render-state flags.
- Runtime magnitudes: terrain `w*h` chunk count, grass instance count, streamed model count, live
  managed-texture bytes — all data-driven per zone.
- Exact device `CreateVertexBuffer`/`CreateIndexBuffer` COM call sites (inside the static NiDX9 lib).
- Whether zone teardown actually runs before reload (leak #2) and whether async model unloads always
  pair with loads (leak #3).

## Runtime confirmation plan

Attach x64dbg to the running client and:

1. Set breakpoints on `sub_BEE0B0` (`0xbee0b0`, texture create) and `CZoneData_Destructor`
   (`0x81a1b0`, zone teardown).
2. Note `GetProcessMemoryInfo` **PrivateBytes** (or `Get-Process … PrivateMemorySize64`) at a baseline.
3. Change zone twice; diff PrivateBytes across each change and count texture-creates vs zone-destructs.
   - PrivateBytes grows per map change **and** `CZoneData_Destructor` is NOT hit → **leak #2** dominates.
   - Destructor IS hit but memory still grows / many textures re-created and not released → **leak #1**.
4. Pick the matching candidate fix above, add it to `patch_client.py`, rebuild `ro2-fixed.exe`, retest.
