# How to build + view the docs

The site is `sphinx` + `pydata-sphinx-theme`, with diagrams in
[`d2`](https://d2lang.com) (our local `.. d2::` directive) and
*every* code block `literalinclude`-d straight from `examples/`
(never copy-pasted — what you read is what CI runs).

## TL;DR

```
uv run --group docs make -C docs html
firefox docs/_build/html/index.html
```

## Nix users

`d2` (the diagram renderer) is deliberately kept **out** of the
default dev-shell so casual envs stay lean; it lives in an opt-in
`docs` shell:

```
# enter the docs shell (puts `d2`, `uv` + python on PATH)
nix develop .#docs

# ...then build (diagrams re-render from docs/diagrams/*.d2)
uv run --group docs make -C docs html
```

one-shot, without staying in the shell:

```
nix develop .#docs -c uv run --group docs make -C docs html
```

## Live-reload while editing

Rebuilds + refreshes the browser on every save:

```
nix develop .#docs -c uv run --with sphinx-autobuild \
    --group docs sphinx-autobuild docs docs/_build/html
# then open http://127.0.0.1:8000
```

## Share it on your LAN

To let someone on your subnet view the docs, bind the server to
all interfaces (`--host 0.0.0.0`) instead of just localhost, then
hand them `http://<your-lan-ip>:8000`.

Live-reload, LAN-visible:

```
nix develop .#docs -c uv run --with sphinx-autobuild --group docs \
    sphinx-autobuild docs docs/_build/html --host 0.0.0.0 --port 8000
```

Or just statically serve an already-built `docs/_build/html` (no
rebuild-on-save):

```
python -m http.server -d docs/_build/html --bind 0.0.0.0 8000
```

Find the IP to give them (first one is usually your LAN iface):

```
hostname -I
```

> Heads-up: this is an unauthenticated static server bound to
> every interface — fine on a trusted LAN, but don't leave it
> running on an untrusted/public network.

## Diagrams (`d2`)

- `.d2` sources live in `docs/diagrams/`; their rendered SVGs are
  git-committed under `docs/_diagrams/` as a fallback.
- with a `d2` binary on `PATH` (the `.#docs` shell, or set
  `D2_BIN='nix run nixpkgs#d2 --'`) any stale SVG re-renders at
  build time.
- with NO binary, the committed SVGs are served as-is, so CI and
  casual builds need no `d2` at all.
- a `.d2` that *fails to compile* is a hard build error under
  `sphinx-build -W` (the last-good committed SVG is left intact).

## Tweaking the logo

The logo is plain SVG — colours are just editable text, so change
them, save, and autobuild repaints the live page. Three contexts
use it, coloured three different ways (an `<img>`-embedded svg
can't read the page's CSS, so only the *inlined* hero can follow
the live light/dark toggle):

| context | file(s) | how it's coloured |
|---|---|---|
| **landing hero** | `docs/_static/tractor_logo_hero.html` (inlined into `index.rst`) + the `svg.hero-logo` rule in `docs/_static/css/custom.css` | linework is `fill: currentColor`, so it takes whatever `color:` the CSS sets — currently `var(--pst-color-text-base)` (the theme text colour). **← recolour on a whim by editing that one `color:` line.** |
| **navbar** | `docs/_static/tractor_logo_nav_light.svg` + `…_nav_dark.svg`, wired via `html_theme_options["logo"]` in `docs/conf.py` | baked fills — near-black lines on light, near-white on dark; pydata swaps them by theme |
| **README** | `docs/_static/tractor_logo_wire.svg` | one baked neutral-grey (GitHub/PyPI can't read the theme), with inline fills so it survives GitHub's svg sanitiser |

The shape's "faces" are `fill: none` everywhere, so the page
background shows through — that's the wireframe look. The original
filled `tractor_logo_side.svg` is kept as the source to recolour
from (and as the favicon).

## svgtool: recolor + preview svgs

`notes_to_self/svgtool.py` is a tiny helper for iterating on the
logo (or any svg). It renders through headless firefox, so masks,
`currentColor` and theme CSS look exactly like the built site.

```
# list the distinct colours in an svg
python notes_to_self/svgtool.py colors docs/_static/tractor_logo_side.svg

# write a recoloured copy (literal token swaps)
python notes_to_self/svgtool.py recolor in.svg out.svg \
    '#0A0A0A=currentColor' '#FCFCFB=none'

# render an svg alone on a bg colour (reveals transparency)
python notes_to_self/svgtool.py preview out.svg /tmp/p.png --bg '#ff00ff'

# screenshot a BUILT page, forcing light/dark (see below)
python notes_to_self/svgtool.py page docs/_build/html/index.html \
    /tmp/dark.png --theme dark
```

### Why `page --theme dark`? (verifying dark mode headlessly)

The non-obvious bit, for the sphinx-rusty:

- pydata-sphinx-theme decides light-vs-dark **in the browser** at
  page load, from a value saved in `localStorage` (or your OS
  setting if you've never clicked the toggle).
- a fresh headless screenshot starts with an *empty* `localStorage`,
  so it always renders the default (light) — you could never grab
  the dark variant.
- `--theme dark` sidesteps that: it writes a throwaway copy of the
  built HTML with a one-line injected `<script>` that pre-sets
  `localStorage` (+ the `data-theme` attribute) to `dark`, then
  screenshots that copy. pydata reads "dark" → renders dark.
  (`--theme light` forces the other way.)

So the loop for any theme-adaptive tweak is: build → `svgtool page
<built-html> out.png --theme dark` → look. That's how the
wireframe logo got checked in both modes without clicking a thing.

## Keep it warning-free

The build is currently 0-warning — keep it that way. `-W` turns
any sphinx warning into a failure:

```
uv run --group docs sphinx-build -b html -W docs docs/_build/html
```

> The rendered version of this note lives in the contributor
> guide: `docs/project/dev-tips.rst` → "Building these docs".
