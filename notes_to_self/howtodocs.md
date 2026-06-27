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

## Keep it warning-free

The build is currently 0-warning — keep it that way. `-W` turns
any sphinx warning into a failure:

```
uv run --group docs sphinx-build -b html -W docs docs/_build/html
```

> The rendered version of this note lives in the contributor
> guide: `docs/project/dev-tips.rst` → "Building these docs".
