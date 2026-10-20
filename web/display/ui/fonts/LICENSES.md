# HUD fonts (fallout.nav)

All fonts are licensed under the **SIL Open Font License 1.1** (OFL). Full licence texts are next to them (`OFL-*.txt`).
Downloaded on 2026-10-20 from the official Google Fonts repository <https://github.com/google/fonts> (branch `main`).
Font files are unmodified. No network access is needed at runtime.

| File | Family / authors | Licence | Source (raw URL) | Last commit of the directory | SHA-256 |
|---|---|---|---|---|---|
| `RussoOne-Regular.ttf` | Russo One — Jovanny Lemonad (Reserved Font Name "Russo") | OFL 1.1, `OFL-RussoOne.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/russoone/RussoOne-Regular.ttf` | `14f3bd097a44db854285fae061ae0fe6eb1a583b` | `bc0abcc660bd8b7ad3000ecb2898a27c58a29a50f7ec81652fa12e75148d09df` |
| `Oswald-wght.ttf` (named `Oswald[wght].ttf` upstream, renamed only to keep URLs simple) | Oswald — The Oswald Project Authors; variable, weights 200–700 | OFL 1.1, `OFL-Oswald.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/oswald/Oswald%5Bwght%5D.ttf` | `8b0a1d0f5983c89bc2b93f1b5fb55f9e252744b5` | `5b38c246e255a12f5712d640d56bcced0472466fc68983d2d0410ec0457c2817` |
| `BlackOpsOne-Regular.ttf` | Black Ops One — James Grieshaber, Eben Sorkin (The Black-Ops Project Authors) | OFL 1.1, `OFL-BlackOpsOne.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/blackopsone/BlackOpsOne-Regular.ttf` | `6ce172f74aa355ea43eb964fa4a91570a4d3064d` | `282a825b5f294377387e3969f765408157dbea8da0f5d0aae68c6bc704b145b3` |

Usage in the HUD (registered by `loadUi()` via `FontFace` as `FnavRusso`, `FnavOswald`, `FnavStencil`):

- **Russo One** — small labels, rhumbs, indicators, source tags (heavy industrial sans, most legible at 25 px).
- **Oswald** (600–700) — large speed and heading digits (tall and narrow: maximum glyph height for minimum width).
- **Black Ops One** — stencil accents: cardinal letters N/E/S/W and the NO GPS / NO LINK warnings.

If a file fails to load, the HUD falls back to system fonts (see `FONT_*` stacks in `theme.js`).

OFL note: "Russo" is a Reserved Font Name. We redistribute the files unchanged; any modification
(subsetting, conversion) requires renaming the font.
