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

## Delivery

These files are served to players through the launcher's language system (content-addressed
blobs under `/api/public/lang/` + `lang_manifest.txt`). On a language **Update** the launcher
downloads only the changed `FontInfo.xml` (incremental), not the whole pack.
