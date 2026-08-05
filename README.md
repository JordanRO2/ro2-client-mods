# ro2-client-mods

Every modification we make to the Ragnarok Online 2 client **files** (build 303), collected as
**selectable mods** with a single applier. Turn any subset on, apply, launch, revert — the
original files are always backed up and every apply rebuilds from that baseline.

Anything that changes the client *at runtime* rather than on disk — the injected DLL, the
launcher, automation — lives in `../mods-ragnarok-online-2` instead. That is the dividing line:
**this repo touches client files; that one does not.**

Target: client **b303 (2022-02-11)**, `Rag2.exe` md5 `cbeccb38bc455e9dd88ded2b43af76fe`,
Gamebryo / NiDX9 under DXVK.

## Layout

| path | what it is |
|---|---|
| `mods.toml` | the mod registry — 26 selectable mods, each bundling one or more underlying fixes |
| `apply.py` | the applier: select, apply, revert, all from a pristine baseline |
| `exe/patch_client.py` | the exe fix catalog (67 fixes) and byte patcher — self-verifying, pattern-anchored |
| `shaders/stock/` | the 42 **pristine** effects from `b303-2022-02-11-PRISTINE/Data/DATA.VDK`. Ground truth. Never edit. |
| `shaders/deployed/` | the 42 effects **currently shipping** in the play client |
| `shaders/src/` | HLSL sources for the shaders we author |
| `shaders/tools/` | build, verify and splice tooling for effects |
| `assets/<fix>/` | one directory per mesh fix: `stock/`, `fixed/`, its repair script and a report |
| `tools/vdk.py` | the shared VDK delivery pipeline (extract verified-stock → apply → repack → diff → install) |
| `textures/` | texture mods |

Three kinds of change:

| Kind | What | How it's applied |
|------|------|------------------|
| **exe** | Byte-patches to the executable — crash guards, address space (OOM), quality, FPS, culling, QoL. | `exe/patch_client.py` |
| **shader-bytepatch** | Constant-only edits inside a `.fxo`. Used for **characters**: only constant bytes change, so the vertex/skinning shader stays byte-identical. | extract → byte-edit → repack `DATA.VDK` |
| **shader-replace** | Swap a whole rebuilt `.fxo` (post/world effects — no skinning). | extract → overlay → repack `DATA.VDK` |
| **asset** | Mesh (`.nif`) repairs. | `tools/vdk.py` → repack `ITEM.VDK` |

## Quick start

```bash
python apply.py --list              # see everything + what's enabled
python apply.py --menu              # tick the ones you want, it applies them
python apply.py --apply enabled     # apply the currently-enabled set
python apply.py --revert            # restore the pristine exe + DATA.VDK
python apply.py --dry-run --apply all   # preview, change nothing
```

Point it at your client in `mods.toml` under `[client]` (`dir`, `exe`, `vdk`). Needs Python
3.11+ and the VDK tool (`[tools].vdk_tool`).

### How apply/revert stay safe

On the **first** apply the pristine `Rag2.exe` and `DATA.VDK` are copied to `backups/` with
SHA-256. Every apply then rebuilds **from that baseline**: the exe is re-patched from the
pristine copy, and `DATA.VDK` is extracted from the pristine copy, the enabled shader mods
overlaid, and repacked. So disabling a mod and re-applying genuinely removes it, and `--revert`
just restores the two pristine files. The original client is never the thing being edited — the
baseline is.

## Delivery: why the VDK pipeline refuses unverified inputs

`tools/vdk.py build --from <archive>` will not start unless that archive's md5 is a known-stock
reference. A fix was once built on top of a scratch extraction taken while a **different,
later-reverted** patch was installed; the resulting "one file changed" archive silently carried
703 unrelated modified meshes back into the game. Every individual step was correct — the input
simply was not what it was assumed to be. So: build only from an archive whose provenance you
have hashed, and finish by diffing the repacked archive against stock and naming every file that
differs. The diff runs on the archive that will actually be installed, re-extracted after
packing, and reports membership changes separately from content changes.

⚠️ **Loose files do not override the VDK.** `VDisk_FileOpenHook` (0x11392E0), installed by
`CActorMgr::Initialize` at 0xA4354D, resolves inside the archive first. Asset fixes must ship
inside the archive.

## Mods

### exe — see `python exe/patch_client.py --list` for the full per-fix catalog

- **exe-crash-stability** — *(default on)* LAA 2→4 GB (stops the long-session OOM crash cascade)
  + dungeon-enter null-deref + 34 RMI/UI null-vtable-call crash guards.
- **exe-memory-oom** — world/model textures + static geometry `MANAGED`→`DEFAULT`. `.patch` cave;
  near-free under DXVK, runtime-untested on raw D3D9.
- **exe-quality-vram** — full-res textures, 16× anisotropic, far/mid LOD.
- **exe-efficiency-fps** — no per-frame Sleep, present IMMEDIATE, drop MULTITHREADED lock,
  HARDWARE_VP+PUREDEVICE, inline hot getters, double buffer.
- **exe-performance-cull** — view-cone cull + deferred equipment-NIF loading (crowd stutter).
- **exe-quality-of-life** — skip opening movie, snappier camera, FMOD 512 channels.

### shaders

```
python shaders/tools/build.py            # all effects with a source
python shaders/tools/verify.py           # ALWAYS run before packaging
```

**PS-splice** — the 13 `Char_*` effects. Only the lit pixel shader is recompiled and spliced into
the stock `.fxo`, so every vertex shader stays byte-identical. Not a preference: the character VS
uses a legacy 90-register SkinBone tight-pack that modern `fxc` cannot reproduce, and recompiling
a whole character effect makes characters **invisible**.

**Full effect** — `PostEffect_SSAO`, compiled with `fxc /T fx_2_0`. Reproducible: compiling the
unmodified source regenerates the shipped `.fxo` byte-for-byte.

`verify.py` checks four things, each of which reached the game once: vertex shaders
byte-identical to stock, no collapse of distinct per-technique pixel shaders, no float that has
been through an int32 round trip (that was the "no mipmaps" bug), and shader model ≤ 3.

Traps worth knowing:

- **Pass render states matter as much as shaders.** A recompile once dropped `FogEnable = 0` on
  32 passes. It appears in 219 of 219 stock passes and `=1` in none, because the engine turns fog
  on (`RenderState::ApplyFogProperty` 0xBBD7D0) and every effect turns it back off.
- **`MipMapLodBias = 3197737370;` is CORRECT** — that u32 is `0xBE99999A` = −0.3, and D3DX stores
  the raw DWORD.
- Registers **c212–c223** are reserved for the injected DLL's live-tuning constants.
- The `GlowSampler` map is packed: **r** weave/dye mask, **g** spec mask, **b** glow, **a** gloss.
  Over 48.2M visible-mask texels of 3,359 maps the gloss channel is **bimodal** — 60.75 % at
  0.200, 15.60 % at 1.000 — so any weighting monotonic in the exponent pushes the two populations
  in opposite directions.

### assets

**`assets/souldoll28`** — Soulmaker doll stray shadow. `SOULDOLL_28.nif` is the only one of 34
doll meshes with `USE_SOFTWARE_SKINNING` set (flags `0x0003` vs `0x0002`). Its only GPU-readable
geometry is a `POSITION` stream whose bytes are byte-identical to the bind pose, refreshed each
frame only by the CPU deform task — and doll bind poses are authored at the character origin, so
a draw that reads it before the deform completes renders a doll silhouette at the feet. The
repair rebuilds the mesh into the hardware-skinning shape the other 33 use; its other changes
(dropping the deform-output streams, adding a `BONE_PALETTE`, the material swap) are mechanical
consequences of that one bit. Affects items `16702340` and `16702305`.

**Status: installed, in-game result not yet confirmed.** If the doll renders deformed, revert.

#### NIF 20.6 format traps

- `NiAVObject::flags` is a **USHORT**, not a uint — reading it as uint shifts every later field.
- Vertex stride is **not universally 48**: 48/normal@20, 52/normal@24, 40/@12, 12/@0.
- `NiDataStream` is often **multi-region**; count is `numBytes/stride`.
- Triangle indices may be submesh-**relative**; use per-region range containment.
- Skinning bone-bounds are gated by `Flags & 2`, not `Flags & 1`.

⚠️ **A geometrically correct normal weld was built, shipped and reverted — it produced holes.**
These shaders do stepped **toon banding**, so a normal change past a step flips to a whole
different band. The weld moved 769 normals by >30°; the seam metric improved 98.8 % and it looked
worse. Cap future normal work at single-digit degrees and look at it in game before shipping.

## What is not in git

`.gitignore` keeps built `.fxo`, `.vdk`, `.exe`, `backups/` and local applier state out of
commits. `shaders/stock/` and `shaders/deployed/` **are** committed — they are the reference the
build verifies against, and without them nothing here is reproducible.
