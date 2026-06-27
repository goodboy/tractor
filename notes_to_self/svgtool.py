#!/usr/bin/env python3
'''
svgtool — tiny SVG recolor + preview helper for fast logo
tweaking. Rendering goes through headless firefox so CSS
(masks, `prefers-color-scheme`, webfonts) is honored exactly
like the real docs.

Subcommands
-----------
  colors  SVG
      list the distinct fill/stroke/stop colors in an svg.

  recolor SVG OUT  old=new [old=new ...]
      literal-replace color tokens, write OUT (great for
      swapping `.stN{fill:#aaa}` style values or hex codes).

  preview SVG OUT.png [--bg C] [--w N] [--scheme light|dark]
      render an svg alone on a bg colour (default white, or
      #1a1a1a under --scheme dark) to eyeball transparency +
      shape. --scheme forces `prefers-color-scheme`.

  page    URL_or_path OUT.png [--w N --h N] [--scheme light|dark]
      screenshot a *built* html page (e.g. docs/_build/html/
      index.html). --scheme dark|light forces the page's
      color-scheme so a pydata `data-theme=auto` site renders
      its dark/light variant — the way to verify theme-adaptive
      logos without clicking the toggle.

Examples
--------
  svgtool.py colors docs/_static/tractor_logo_side.svg
  svgtool.py recolor in.svg out.svg '#FCFCFB=none' '#0A0A0A=#fff'
  svgtool.py preview out.svg /tmp/p.png --bg '#ff00ff' --w 600
  svgtool.py page docs/_build/html/index.html /tmp/dark.png \
      --w 1300 --h 900 --scheme dark
'''
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess as sp
import tempfile

FF: str = 'firefox'
_color_re = re.compile(
    r'(?:fill|stroke|stop-color)\s*[:=]\s*"?'
    r'(#[0-9a-fA-F]{3,8}|none|transparent|currentColor)'
)


def _colors(text: str) -> list[str]:
    return sorted(set(_color_re.findall(text)))


def _profile(scheme: str|None) -> str:
    '''
    Spin up a throwaway firefox profile, optionally forcing
    `prefers-color-scheme` via the content-override pref
    (0 = dark, 1 = light).

    '''
    d: str = tempfile.mkdtemp(prefix='svgtool-ff-')
    if scheme:
        val: int = {'dark': 0, 'light': 1}[scheme]
        Path(d, 'user.js').write_text(
            f'user_pref('
            f'"layout.css.prefers-color-scheme.content-override",'
            f' {val});\n'
        )
    return d


def _shot(
    url: str,
    out: str,
    w: int,
    h: int,
    scheme: str|None,
) -> None:
    prof: str = _profile(scheme)
    sp.run(
        [
            FF, '--headless', '--no-remote',
            '-profile', prof,
            '--screenshot', str(out),
            f'--window-size={w},{h}',
            url,
        ],
        stdout=sp.DEVNULL,
        stderr=sp.DEVNULL,
        timeout=90,
    )
    print(out)


def cmd_colors(a: argparse.Namespace) -> None:
    print('\n'.join(_colors(Path(a.svg).read_text())))


def cmd_recolor(a: argparse.Namespace) -> None:
    text: str = Path(a.svg).read_text()
    for pair in a.pairs:
        old, new = pair.split('=', 1)
        if old not in text:
            print(f'  ! {old!r} not found (skipped)')
            continue
        text = text.replace(old, new)
    Path(a.out).write_text(text)
    print(f'wrote {a.out}')


def cmd_preview(a: argparse.Namespace) -> None:
    bg: str = a.bg or (
        '#1a1a1a' if a.scheme == 'dark' else '#ffffff'
    )
    src: str = Path(a.svg).resolve().as_uri()
    html = Path(tempfile.mktemp(suffix='.html'))
    html.write_text(
        f'<!doctype html><html><body '
        f'style="margin:0;background:{bg}">'
        f'<img src="{src}" '
        f'style="width:{a.w}px;display:block"></body></html>'
    )
    _shot(
        html.resolve().as_uri(),
        a.out,
        a.w + 40,
        int(a.w * 0.9),
        a.scheme,
    )


# early script that pins a pydata-sphinx-theme page to a given
# mode/theme (beats the localStorage/auto default) so headless
# shots can verify theme-adaptive content without the toggle.
_force_theme = (
    '<script>try{{'
    'localStorage.setItem("mode","{t}");'
    'localStorage.setItem("theme","{t}");'
    'document.documentElement.setAttribute("data-theme","{t}");'
    'document.documentElement.setAttribute("data-mode","{t}");'
    '}}catch(e){{}}</script>'
)


def cmd_page(a: argparse.Namespace) -> None:
    src = Path(a.url)
    if a.theme and src.exists():
        # inject the force-theme script into a sibling temp copy
        # (same dir → relative _static/ asset paths still resolve).
        html: str = src.read_text()
        inj: str = _force_theme.format(t=a.theme)
        html = re.sub(
            r'(<head[^>]*>)',
            r'\1' + inj,
            html,
            count=1,
        )
        tmp = src.with_name(f'._svgtool_{a.theme}.html')
        tmp.write_text(html)
        try:
            _shot(tmp.resolve().as_uri(), a.out, a.w, a.h, None)
        finally:
            tmp.unlink(missing_ok=True)
        return
    url: str = (
        a.url
        if '://' in a.url
        else src.resolve().as_uri()
    )
    _shot(url, a.out, a.w, a.h, a.scheme)


def main() -> None:
    p = argparse.ArgumentParser(prog='svgtool.py')
    sub = p.add_subparsers(required=True)

    c = sub.add_parser('colors')
    c.add_argument('svg')
    c.set_defaults(fn=cmd_colors)

    r = sub.add_parser('recolor')
    r.add_argument('svg')
    r.add_argument('out')
    r.add_argument('pairs', nargs='+')
    r.set_defaults(fn=cmd_recolor)

    v = sub.add_parser('preview')
    v.add_argument('svg')
    v.add_argument('out')
    v.add_argument('--bg', default=None)
    v.add_argument('--w', type=int, default=600)
    v.add_argument(
        '--scheme',
        choices=['light', 'dark'],
        default=None,
    )
    v.set_defaults(fn=cmd_preview)

    g = sub.add_parser('page')
    g.add_argument('url')
    g.add_argument('out')
    g.add_argument('--w', type=int, default=1300)
    g.add_argument('--h', type=int, default=900)
    g.add_argument(
        '--scheme',
        choices=['light', 'dark'],
        default=None,
    )
    g.add_argument(
        '--theme',
        choices=['light', 'dark'],
        default=None,
        help='force a pydata data-theme (toggle-accurate)',
    )
    g.set_defaults(fn=cmd_page)

    a = p.parse_args()
    a.fn(a)


if __name__ == '__main__':
    main()
