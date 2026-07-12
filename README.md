# ro2-client-mods

Every modification we make to the Ragnarok Online 2 client (build 303), collected as
**selectable mods** with a single applier. Turn any subset on, apply, launch, revert —
the original files are always backed up and every apply rebuilds from that baseline.

Two kinds of change live here:

| Kind | What | How it's applied |
|------|------|------------------|
| **exe** | Byte-patches to the client executable — crash/stability guards, address-space (OOM) fixes, quality, FPS/efficiency, culling, quality-of-life. | `exe/patch_client.py` (self-verifying, pattern-anchored) |
| **shader-bytepatch** | Constant-only edits inside a `.fxo` in `DATA.VDK`. Used for **characters**, because only the constant bytes change — the vertex shader / GPU-skinning stays byte-identical. | extract → byte-edit → repack `DATA.VDK` |
| **shader-replace** | Swap a whole recompiled `.fxo` (post/world effects — no skinning). | extract → overlay → repack `DATA.VDK` |
| **texture** | *(reserved for future texture mods)* | — |

## Quick start

```bash
python apply.py --list              # see everything + what's enabled
python apply.py --menu              # tick the ones you want, it applies them
python apply.py --apply enabled     # apply the currently-enabled set
python apply.py --apply all         # apply everything
python apply.py --apply shader-char-rim,exe-quality-vram   # apply exactly these
python apply.py --revert            # restore the pristine exe + DATA.VDK
python apply.py --dry-run --apply all   # preview, change nothing
```

Point it at your client in `mods.toml` under `[client]` (`dir`, `exe`, `vdk`). Needs
Python 3.11+ and the VDK tool (`[tools].vdk_tool`).

### How apply/revert stay safe
On the **first** apply, the pristine `Rag2.exe` and `DATA.VDK` are copied to `backups/`
(with SHA-256). Every apply then rebuilds **from that baseline**: the exe is re-patched
from the pristine copy, and `DATA.VDK` is extracted from the pristine copy, the enabled
shader mods overlaid, and repacked. So disabling a mod and re-applying genuinely removes
it, and `--revert` just restores the two pristine files. The original client is never the
thing being edited — the baseline is.

## Mods

### exe (see `python exe/patch_client.py --list` for the full per-fix catalog)
- **exe-crash-stability** — *(default on)* LAA 2→4 GB (stops the long-session OOM crash
  cascade) + dungeon-enter null-deref + 34 RMI/UI null-vtable-call crash guards.
- **exe-memory-oom** — world/model textures + static geometry `MANAGED`→`DEFAULT` (drops
  the system-RAM shadows that exhaust the 32-bit address space). `.patch` cave; near-free
  under DXVK, runtime-untested on raw D3D9.
- **exe-quality-vram** — full-res textures, 2048 shadow maps, 16× anisotropic, far/mid LOD.
- **exe-efficiency-fps** — no per-frame Sleep, present IMMEDIATE, drop MULTITHREADED lock,
  HARDWARE_VP+PUREDEVICE, inline hot getters, double buffer.
- **exe-performance-cull** — view-cone cull + deferred equipment-NIF loading (crowd stutter).
- **exe-quality-of-life** — skip opening movie, snappier camera, FMOD 512 channels.

### shaders
- **shader-char-rim** — character fresnel rim/edge light +37% (`0.4`→`0.55`), applied as a
  **byte-patch of the original `.fxo`** so skinning is guaranteed intact. Currently
  `Char_Default` + `Char_HitEffect`; the other 11 char shaders pack the rim constant
  differently and still need a per-shader anchor.
- **shader-ssao-strong** — SSAO deeper contact shadows (+30%).
- **shader-ocean-deep** / **shader-oceanva-enrich** — deeper/cooler + enriched water.
- **shader-terrain-crisp** — +12% detail-texture contrast.

## Why characters are byte-patch only

A full shader recompile changes the register layout the engine uploads `SkinBone` into
(the legacy 3-registers-per-bone, 90-register tight pack that modern `fxc` cannot
reproduce), which makes recompiled character shaders render **invisible** in the real
client. So character shaders are modified by editing **only** their constant bytes — every
other byte, including the vertex/skinning shader, stays identical. Post/world effects have
no skinning and are safe to recompile (`shader-replace`).

## What is not in git

The client is copyrighted, so `.gitignore` keeps every `.fxo` / `.vdk` / `.exe`, the
`backups/`, and `textures/` **out of commits**. This repo ships the **mod definitions and
tooling**; the modified shader binaries live in `shaders/` locally. `shader-bytepatch`
mods (like `shader-char-rim`) carry their edit as `find`/`repl` bytes in `mods.toml`, so
they apply from a clean clone against your own client; `shader-replace` mods need their
built `.fxo` placed in `shaders/`.

## Roadmap
- Per-shader rim anchors for the remaining 11 character shaders (byte-patch).
- Modern-from-scratch pixel shading for characters via **PS-splice** (recompile only the
  pixel shader, graft it into the original effect so the skinning VS is untouched).
- Recreated post-effects (SSAO/AA/color grade) and world shaders (water/terrain/sky).
- Texture mods.
