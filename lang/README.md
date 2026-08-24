# Language font unification (FontInfo.xml)

Every language's `lang/<code>/UI/FONT/FontInfo.xml` is rewritten so the client renders
all UI text with **Source Han Sans** instead of a per-language face.

## Why

Each language originally used a font that only covers its own script — Chinese used
`MSJHBD.TTF`, Japanese `meiryo.ttc`, Thai `TAHOMA.ttf`, English/Korean `AritaSans-Bold.otf`.
So a client could not render names in another language's script (e.g. the Chinese client
showed Thai player names as boxes).

The Source Han Sans build shipped with the client is **pan-Unicode** (verified from its
cmap: 44,701 codepoints — CJK 100%, Latin 100%, Thai 94%, Hangul 100%, Hiragana/Katakana
100%, Cyrillic 100%), so pointing every slot at it lets any client render any language's
text. The three weight files are already present in every `lang/<code>/UI/FONT/` folder.

## Mapping

Per `FontParam` slot, by its `height` (the signal for the slot's role):

- **height >= 13** (titles, over-head names, big numbers) -> `SourceHanSans-Heavy.otf`
- **everything else** (7-12px body/UI text) -> `SourceHanSans.otf` (Regular)
- slots already using a `SourceHanSans*` file are left unchanged
- `SourceHanSans-Light.otf` is available but unused: at 7-11px with the engine's 1px
  outline, Light strokes read too thin.

Only `FontName="..."` values change; heights, outline/shadow, colours and the euc-kr
encoding are untouched. Source: the pristine `b303-2022-02-11` FontInfo files.

## Sizing, scaling and spacing

Beyond the font swap, `FontInfo.xml` and the fonts themselves carry three tweaks:

- **Sizes ×0.9.** Every slot's `height` is scaled to 90 % (rounded), for slightly smaller,
  denser UI text.
- **`UseFontScaling="false"` on every slot.** Source Han Sans is a large-em CJK font; the
  stock client only ever used it in the two slots that also set this flag, which stops the
  engine from re-scaling it and misplacing individual glyphs (e.g. an `s` sitting higher than
  its neighbours). Since every slot now uses a Source Han Sans weight, all carry the flag.
  Height is still honoured — the stock Source Han Sans slots set both.
- **HUD numbers bigger (`FontParam index 11` → height 9).** The inventory stack-count, the
  HUD level / zeny / diamond, and the top-left player-frame level all reference font index 11
  in the `.DLG` UI files. Index 11 is shared by ~185 controls across 27 DLGs, so bumping its
  height also enlarges other small HUD texts on that slot (HP/SP, hotkey labels, job level,
  other currencies).

## Font word-spacing (the space glyph)

`FontInfo.xml` has no word-spacing knob and the stock space glyph is a touch narrow, so
the space character (U+0020) is widened in the fonts: its advance is bumped by 50 units
(em = 1000) in all three Source Han Sans weights — only the space, not letter-spacing.
Only the `hmtx` table is patched (the CFF outlines are byte-identical); reproduce with
`tools/space_lang_fonts.py <pristine_font_dir> <out_dir>`.

## Delivery

These files are served to players through the launcher's language system (content-addressed
blobs under `/api/public/lang/` + `lang_manifest.txt`). On a language **Update** the launcher
downloads only what changed — the `FontInfo.xml` on a size/scaling tweak, or the three
space-widened Source Han Sans blobs when the fonts change — not the whole pack. The font
blobs are shared across every language, so they download once.
