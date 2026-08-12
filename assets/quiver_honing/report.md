# Quiver honing — content that was configured but never displayed

## Symptom

Grade-4 quivers (Eddga, Lich, Serenia, …) never show their honing / grinding options in the
item tooltip, even though every other grade-4 weapon and armor of the same tier does.

## Why

Honing in this client is driven off the item's **primary type**. The tooltip type-name
decoder `ItemTooltip_GetTypeNameFromField520` (`Rag2.exe` `0x9CB6E0`) reads `Item_Type` and
maps **104 → "Projectile"**; the tooltip drawer only renders the honing / option section for
weapon and armor types. A Projectile is never given the honing section, regardless of how the
item is otherwise configured.

These quivers were nevertheless **set up to be honeable**:

- `Grinding_Trait_Able = 1`
- a full row of options in `NEW_TraitInfo` (3–5 traits each)
- grade-4 stats, mastery grade/level requirements, durability

The intent is visible in the data history: `Grinding_Trait_Able` was flipped `0 → 1` on the
Eddga quiver in **build 201 (2015-04-11)**, and on the Basilisk quiver as far back as **build
113 (2014-05-07)**. Somebody turned grinding on for these items and authored their trait
options — but left `Item_Type = 104`, which suppresses the honing UI. The configuration has
been dead ever since.

Note this is **not** a case of a patch changing the type. `Item_Type` was **104 in every
build the quivers ever shipped in** (verified across b113 → b303). The honing was enabled
without ever aligning the type to a class the client will hone. The result is the same either
way — intended honeable content that the client silently refuses to display.

## Fix

Per affected quiver:

| field | stock | fixed | why |
|---|---|---|---|
| `Item_Type` | 104 (Projectile) | 1 (Weapon) | the tooltip stops printing "Projectile" and renders the honing/option section |
| `Weapon_Type` | 0 | 1 | the option section additionally requires a valid weapon subtype; `Item_Type` alone removes the label but does not bring back the lines |

Everything else is left stock: name, icon, mesh, **`Equip_Slot = 13` (still the quiver
slot)**, the item's own trait options, prices, stats. The quiver still equips as a quiver and
is still accepted as ammo; only its classification for the honing UI changes.

Verified in game on the b303 client: the fixed quivers stop showing "Projectile" and display
their honing lines.

## Targets

Selected dynamically — every `ItemInfo` row with
`Item_Type == 104` **and** `Grinding_Trait_Able == 1` **and** a non-empty `NEW_TraitInfo`
row. On stock b303 this is **10 quivers** (15 table rows; 5 have a duplicate row):

| id | name | grade | trait options |
|---|---|---|---|
| 16700727 | Basilisk Quiver | 2 | 3 |
| 16700827 | Ashkaron Quiver | 3 | 4 |
| 16701610 | Himmelmez's Quiver | 4 | 5 |
| 16701763 | Eddga Quiver | 4 | 4 |
| 16701854 | Witch Quiver | 3 | 4 |
| 16701936 | Lich Quiver | 4 | 5 |
| 16702032 | Mustafa Quiver | 3 | 4 |
| 16702116 | Cazar Quiver | 4 | 5 |
| 16702296 | Serenia's Quiver | 4 | 4 |
| 16702331 | Ancient Warrior's Quiver | 3 | 4 |

**Excluded:** the Niflheim Quiver (16702377) is flagged grindable but carries **zero** trait
options, so its honing section would be empty. It is intentionally left untouched.

## Reproduce

```
python assets/quiver_honing/patch.py --in <stock ASSET.VDK> --out ASSET.VDK
```

The patcher refuses to run unless `--in` is a known-stock `ASSET.VDK`, changes only
`Item_Type`/`Weapon_Type` on the target rows, and self-verifies at the end that **only
`ItemInfo.ct` differs** and **only those two columns changed, only on those quivers** — else it
aborts without writing output. From the official `b303-2022-02-11` `ASSET.VDK`
(md5 `91c32367eee36dd0016a72bc001568ec`) it deterministically produces
md5 `d55112cda7f3613be5cd4d80e302ba3d`.

The built `ASSET.VDK` is shipped in the GitHub release (client data archives are not committed
to the repo; see `.gitignore`). Install by dropping it in `<client>/Data/ASSET.VDK`.
