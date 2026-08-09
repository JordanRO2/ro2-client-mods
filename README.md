# RO2 client fixes

Bug fixes for the Ragnarok Online 2 client, build **303 (2022-02-11)** — the last official
release. Crashes, broken models, inverted lighting, an item that shows the wrong costume
entirely.

Everything here fixes the client's own **files**: the executable and the `.VDK` data archives.
Nothing runs alongside the game, nothing hooks it at runtime, and no server is involved.

## Install

Download the archives from the [latest release](../../releases/latest) and drop them in:

```
<client>/Data/       ITEM.VDK  ACTOR.VDK  ITEM1.VDK  DATA.VDK
<client>/SHIPPING/   Rag2.exe
```

Back up the originals first, or keep a clean client install around — every file being replaced is
unmodified stock, so restoring is just copying them back. Check what you downloaded against
`MD5SUMS.txt` before installing.

That is the whole procedure. If you would rather build the files yourself from your own client,
see [Building it yourself](#building-it-yourself).

---

## What is fixed

### Crashes and stability

The stock client is a 32-bit process that is **not** large-address-aware, so it is capped at 2 GB
of virtual address space and dies once it crosses that — typically after an hour of play or
several zone loads. The crash wears half a dozen different faces: a write to address 0 during
zone loading, an `int 3` inside SpeedTree's allocation-failure handler, assorted null
dereferences. They are all the same underlying problem. The cap is lifted to 4 GB.

On top of that, 35 null-check guards on paths that real minidumps showed crashing — dungeon
entry, and a family of null vtable calls on network-message and UI handlers.

### Models that render wrong

**A Soulmaker's doll cast its shadow at somebody else's feet.** Of the 34 doll meshes, exactly one
— the Ancient Warrior's and Serenia's doll — was set up for CPU skinning instead of GPU. In that
mode the only geometry the graphics card can read holds the *bind pose*, and doll bind poses are
authored at the character's origin rather than in the hand. Any frame drawn before the CPU
finished deforming the mesh therefore painted a doll-shaped silhouette on the ground. Converting
the mesh to GPU skinning removes the window entirely.

**The Hanbok (Woman) costume tore apart on ranking statues.** Same root cause, different symptom.
The statue rendering path picks a GPU-skinning shader based on whether a mesh has a skinning
modifier at all — never on whether that modifier is actually GPU-capable. So the CPU-skinned dress
was handed a shader whose bone matrices were never uploaded, and it drew using whatever the
previously drawn statue had left in those registers. The result stretched away toward another
model. The dress is now GPU-skinned, which removes the precondition.

**The Chieftain Great Sword was inside-out.** The model was authored fully inverted — both its
triangle facing and its surface normals. Because the two agreed with each other, it was not
obviously broken in the file; it simply faced the wrong way as a whole. Correcting only the
triangle facing (as an earlier attempt did) fixes the silhouette and leaves the lighting reversed,
which is exactly what it looked like. Both are corrected. Five other items share that model and
are fixed with it.

**The Odinguitar back accessory, Noel versions.** The same inversion, and the same half fix from
that earlier attempt. The non-Noel versions measured correct and were left alone.

**The Stolen Dartboard showed flowers on male humans.** Not a rendering bug at all — the flower
umbrella asset was packaged under the dartboard's filename. Only the male-human variant was
affected, because that is the one file of four the game loads for that body. It shipped correct in
2014-2015 builds and broke somewhere before 2021. Replaced with the correct model.

### Everything else on the CPU-skinning list

The two bugs above turned out to be instances of one authoring accident, so the whole client was
swept for it. Of **32,949 skinned meshes, only 104** used the CPU path — the client is 99.7 % GPU
already, and those few are an oversight rather than a decision. 103 were converted: costumes,
weapons, monsters, city NPCs and player transformation models.

The remaining four are the `Thunderdrum` back accessory, deliberately left alone. Its material
declares no skinning inputs whatsoever, so converting it would delete the very data its shader
reads.

### Quality and performance

16x anisotropic filtering, full-resolution textures, high-quality shaders kept at far and mid
draw distance, no per-frame sleep, inlined hot-loop accessors, view-cone culling.

The character shaders were also reworked: specular highlights now follow the artist's gloss and
mask maps per texel instead of applying one uniform highlight to everything, which is what made
skin and cloth look like plastic.

---

## Building it yourself

You need Python 3.11+, the [VDK tool](../../../tools-ragnarok-online-2-vdk), and a clean copy of
the b303 (2022-02-11) client — `Rag2.exe` md5 `cbeccb38bc455e9dd88ded2b43af76fe`.

```bash
python exe/patch_client.py --in <stock Rag2.exe> --out Rag2.exe   # 27 fixes, 319 bytes
python tools/vdk.py build --from <stock ITEM.VDK> --apply <fix dir> --out ITEM.VDK
python tools/vdk.py install --archive ITEM.VDK --to <client>/Data
```

`tools/vdk.py` refuses to start unless the source archive's md5 is a known-stock reference, and
ends every build by re-extracting the packed archive and naming every file that differs from
stock. Both exist because a fix was once built on top of an archive that was not what it was
assumed to be, and silently carried 703 unrelated meshes into the game.

Every file in the release is reproducible this way, byte for byte, from the stock client plus this
repository. `python exe/patch_client.py --list` prints the full catalog of all 71 executable
fixes and which are enabled by default.

To pick a subset rather than everything, `apply.py` drives the same machinery from the mod
registry in `mods.toml`:

```bash
python apply.py --list      # catalog and current state
python apply.py --menu      # choose interactively, then apply
python apply.py --revert    # restore the pristine files
```

Every apply rebuilds from a pristine baseline rather than editing the installed files, so turning
a mod off genuinely removes it.

## Layout

| path | what it is |
|---|---|
| `exe/patch_client.py` | the executable fix catalog (71) and byte patcher — self-verifying, pattern-anchored |
| `assets/<fix>/` | one directory per mesh fix: the stock file, the fixed file, the repair script and a report |
| `shaders/stock/` | the 42 pristine effects. Ground truth for verification — never edited |
| `shaders/deployed/` | the 42 effects the release ships |
| `shaders/deployed_data/` | non-effect files `DATA.VDK` also carries (`Toon01.bmp`) |
| `shaders/src/` | HLSL sources |
| `shaders/tools/` | effect build, verify and splice tooling |
| `tools/vdk.py` | the shared archive pipeline: verified-stock → apply → repack → diff → install |
| `mods.toml`, `apply.py` | the selectable-mod registry and applier |
| `docs/` | engineering notes: file-format traps, past regressions, why certain things are done the way they are |

Every fix here has been tested in game. Runtime modifications — an injected DLL, a launcher,
automation — are a separate concern and are not in this repository.

## Credits and scope

Unofficial and unaffiliated with Gravity Co., Ltd. or any of the game's regional publishers.
Provided as-is; it modifies a client you already own. The game data in this repository belongs to
its owners and is here only because the fixes cannot be verified or reproduced without the
originals to compare against.
