# Engineering notes

Working knowledge for anyone changing the fixes rather than installing them. Almost everything
here exists because the corresponding mistake was actually made and reached the game — the notes
are cheaper than repeating them.

## Delivery

**Loose files do not override the VDK.** `VDisk_FileOpenHook` (0x11392E0), installed by
`CActorMgr::Initialize` at 0xA4354D, resolves inside the archive first and only reaches the disk
implementation on failure. An asset fix must ship inside the archive; dropping a `.nif` next to
the client does nothing.

**`tools/vdk.py` refuses unverified inputs on purpose.** A fix was once built on top of a scratch
extraction taken while a different, later-reverted patch was installed. The resulting "one file
changed" archive silently carried 703 unrelated modified meshes back into the game. Every
individual step had been correct — the input simply was not what it was assumed to be. So the
build will not start unless the source archive's md5 is a known-stock reference, and it ends by
re-extracting the *packed* archive and naming every file that differs, reporting membership
changes separately from content changes.

**Reproducibility has to be tested, not assumed.** Twice the repository looked complete and was
not. `DATA.VDK` modifies 25 files, only 24 of which are effects — the 25th is
`SHADERS/Data/Textures/Toon01.bmp`, 824 bytes, and without it a rebuild does not reproduce the
shipping archive. And `effect_const_null_getter` collided with `minidump_null_guards_2026_08` at
VA 0xBA13A0, which made the default executable build abort outright. Both were found only by
actually rebuilding and comparing hashes.

## Executable patches

**Never hand-write short jump displacements in a code cave.** Two batches shipped broken that way.
Assemble branches from labels in two passes and disassemble the built section before trusting it.

The `.patch` section is sized at build time; a fix that needs a cave must check it fits. Building
from an older patched executable rather than the pristine one gives a smaller section and the
check correctly refuses.

## Shaders

Two build kinds, and confusing them breaks the game:

* **PS-splice** — the 13 `Char_*` effects. Only the lit pixel shader is recompiled and spliced
  into the stock `.fxo`, leaving every vertex shader byte-identical. This is not a stylistic
  choice: the character vertex shader uses a legacy 90-register `SkinBone` tight pack that modern
  `fxc` cannot reproduce, and recompiling a whole character effect makes characters **invisible**.
* **Full effect** — `PostEffect_SSAO`, compiled with `fxc /T fx_2_0`. Reproducible: compiling the
  unmodified source regenerates the shipped `.fxo` byte for byte.

`shaders/tools/verify.py` checks four things, each of which reached the game once:

1. vertex shaders byte-identical to stock on spliced effects;
2. no collapse of distinct per-technique pixel shaders (this destroyed `Rag2ObjectShader_VCAB`:
   3 distinct vertex shaders became 1, 4 pixel shaders became 2);
3. no float that has been through an int32 round trip — reading `-0.3f`'s bit pattern
   (`0xBE99999A`) as an int and re-emitting it as a float literal gives `0xCE82CCCD` = −1.09e9,
   which as a `MipMapLodBias` pins mip 0 at every distance. That was the "no mipmaps" bug;
4. shader model ≤ 3, the D3D9 ceiling under DXVK.

Other traps:

* **Pass render states matter as much as shader code.** A recompile once dropped `FogEnable = 0`
  on 32 passes. It appears in 219 of 219 stock passes and `=1` in none, because the engine turns
  fog on (`RenderState::ApplyFogProperty` 0xBBD7D0) and every effect turns it back off. Diff pass
  states, not only sampler states.
* **`MipMapLodBias = 3197737370;` is correct**, not a leftover bug — that u32 is `0xBE99999A` =
  −0.3, and D3DX stores the raw DWORD.
* Registers **c212–c223** are reserved for live-tuning constants; effect constant tables top out
  around c15, so the runtime never uploads there.
* The `GlowSampler` map is packed: **r** weave/dye mask, **g** specular mask, **b** glow,
  **a** gloss. Measured over 48.2M visible-mask texels of 3,359 maps the gloss channel is
  **bimodal** — 60.75 % at 0.200, 15.60 % at 1.000 — so any weighting that is monotonic in the
  exponent pushes those two populations in opposite directions. An energy-normalisation attempt
  was refuted on exactly that basis.

## NIF 20.6

Each of these produced a confidently wrong answer at least once:

* `NiAVObject::flags` is a **USHORT**, not a uint. Reading it as a uint shifts every later field
  and yields garbage material names.
* Vertex stride is **not universally 48**: 48/normal@20, 52/normal@24 (a `COLOR u8x4` comes
  first), 40/@12, 12/@0. Derive it per stream from the `NiDataStream` format words.
* `NiDataStream` is often **multi-region**. The vertex count is `numBytes/stride`; reading only
  `regions[0]` truncates.
* Triangle indices may be submesh-**relative**. `absolute = idx.max() < nv` is backwards for
  exactly the relative meshes — use per-region range containment.
* Skinning bone bounds are gated by `Flags & 2`, not `Flags & 1`.
* `NiSkinningMeshModifier::Flags & 1` is `USE_SOFTWARE_SKINNING`.
* Some shipped assets already carry **NaN normals**. Pre-existing; not your bug.

**There is no 30-bone ceiling.** 1,695 shipped hardware-skinned meshes carry 34 or more bones, up
to 86; `BONE_PALETTE` is what lets a mesh exceed the per-draw window. A claim that a mesh had to
stay CPU-skinned because of its bone count was false.

**Noel is not interchangeable with human.** Noel meshes are a uniform **0.799 scale** of the human
mesh and repositioned. Two Noel files being byte-identical to each other means Noel male and
female share a body, not that the accessory ignores the body. Human male and female, by contrast,
genuinely do share one asset — the gendered filenames are a naming convention.

## Diagnosing a "looks wrong" report

**Item name → mesh file.** Item names come from the server, not the client tables. The bridge is
`Data/1/LANG/1/string/string_item_name.tbl` (UTF-16), which maps a `String_Item_Name` id to the
display name; **strip the first two digits** for the 8-digit item id; then look it up in
`ASSET/ItemInfo.xlsx`, column `Mesh`. That column is often a **template with `%s` placeholders**
that the client fills with race and gender, so **four files can back one item** — a bug that hits
only one race or gender is usually one of those four files rather than the item.

**Inside-out models: winding and normals are different defects and can co-occur.** Compute
geometric normals from triangle winding, then measure two things: `dot(geometric, stored)` for
internal consistency, and `dot(geometric, centroid→face)` for which one is actually wrong. The
first alone cannot tell you the answer — a fully inverted model scores +0.95 on it. Always run
known-good controls of the same kind; healthy two-handed swords score +0.95 and +0.20…+0.40
respectively. Outwardness is weak on flat or open shells, where the usable signal is the sign flip
against a healthy sibling variant rather than the magnitude.

## A warning about normal edits

A geometrically correct weld of the neck-ring normals was built, shipped, and reverted: it
produced visible holes. These shaders do stepped **toon banding**, not smooth shading, so a normal
change does not shade slightly differently — past a step it flips to a whole different band. The
weld moved 769 normals by more than 30°. The seam metric improved by 98.8 % and the result looked
worse. Cap any future normal work at single-digit degrees and look at it in game before shipping.
