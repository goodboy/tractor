# tractor: distributed structured concurrency.
'''
``.. d2::`` - embed d2lang_ diagrams in sphinx docs
with build-time rendering and a committed-SVG
fallback.

Rendering policy,

- when a ``d2`` binary is found (see discovery order
  below) any out-of-date SVG is (re)rendered from its
  ``.d2`` source (normally kept in ``docs/diagrams/``)
  into ``docs/_diagrams/``,
- otherwise any pre-rendered (and git committed) SVG
  already in ``docs/_diagrams/`` is used as-is,
- when neither is possible the diagram *source* is
  emitted as a literal block so no content is ever
  silently dropped.

Binary discovery order,

- the ``D2_BIN`` env var; may contain args which are
  split via `shlex`, eg.
  ``D2_BIN='nix run nixpkgs#d2 --'``,
- the ``d2_bin`` sphinx config value,
- ``shutil.which('d2')``.

Usage,

.. code:: rst

    .. d2:: diagrams/actor_tree.d2
       :caption: A tree of ``trio``-task-trees.
       :margin:

.. _d2lang: https://d2lang.com

'''
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess as sp

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.application import Sphinx
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective

log = logging.getLogger(__name__)

# subdir (under the sphinx srcdir) holding rendered,
# git-committed, fallback SVG outputs.
_outdir: str = '_diagrams'


def find_d2(
    app: Sphinx,
) -> list[str]|None:
    '''
    Resolve the d2 render command as an argv list or
    `None` when no binary can be found.

    '''
    if env_bin := os.environ.get('D2_BIN'):
        return shlex.split(env_bin)
    if cfg_bin := app.config.d2_bin:
        return shlex.split(cfg_bin)
    if path_bin := shutil.which('d2'):
        return [path_bin]
    return None


def render_svg(
    app: Sphinx,
    src: Path,
    out: Path,
) -> bool:
    '''
    Maybe (re)render `src` -> `out`, returning
    `True` when an up-to-date SVG exists after the
    call.

    '''
    stale: bool = (
        not out.exists()
        or
        src.stat().st_mtime > out.stat().st_mtime
    )
    if not stale:
        return True
    d2cmd: list[str]|None = find_d2(app)
    if d2cmd is None:
        if out.exists():
            log.info(
                f'no d2 binary; using committed svg '
                f'for {src.name}'
            )
            return True
        return False
    out.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    argv: list[str] = (
        d2cmd
        + list(app.config.d2_args)
        + [str(src), str(out)]
    )
    try:
        proc = sp.run(
            argv,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (
        OSError,
        sp.TimeoutExpired,
    ) as err:
        log.warning(
            f'd2 invocation failed for {src.name}: '
            f'{err}'
        )
        return out.exists()
    if proc.returncode != 0:
        log.warning(
            f'd2 render error for {src.name}:\n'
            f'{proc.stderr}'
        )
        return out.exists()
    return True


class D2Diagram(SphinxDirective):
    '''
    Render a ``.d2`` source file (path relative to
    the sphinx srcdir) as an SVG figure.

    '''
    required_arguments = 1
    has_content = False
    option_spec = {
        'caption': directives.unchanged,
        'alt': directives.unchanged,
        'width': (
            directives.length_or_percentage_or_unitless
        ),
        'margin': directives.flag,
        'class': directives.class_option,
        'name': directives.unchanged,
    }

    def run(self) -> list[nodes.Node]:
        relsrc: str = self.arguments[0]
        srcdir = Path(self.env.srcdir)
        src: Path = srcdir / relsrc
        self.env.note_dependency(relsrc)
        if not src.exists():
            err = self.state_machine.reporter.error(
                f'd2 source not found: {relsrc}',
                line=self.lineno,
            )
            return [err]
        out: Path = (
            srcdir
            / _outdir
            / f'{src.stem}.svg'
        )
        if not render_svg(self.env.app, src, out):
            # last resort: emit the raw d2 source.
            log.warning(
                f'no svg available for {relsrc}; '
                f'emitting d2 source as literal block'
            )
            literal = nodes.literal_block(
                src.read_text(),
                src.read_text(),
            )
            literal['language'] = 'text'
            return [literal]
        img = nodes.image(
            uri=f'/{_outdir}/{out.name}',
            alt=self.options.get(
                'alt',
                f'd2 diagram: {src.stem}',
            ),
        )
        if width := self.options.get('width'):
            img['width'] = width
        fig = nodes.figure()
        fig += img
        classes: list[str] = (
            ['d2-diagram']
            + self.options.get('class', [])
        )
        if 'margin' in self.options:
            # NB: the bare 'margin' class is what
            # book-style themes key off for
            # right-margin placement; our custom css
            # uses 'd2-margin'.
            classes += [
                'margin',
                'd2-margin',
            ]
        fig['classes'] += classes
        if caption_txt := self.options.get('caption'):
            (
                inline_nodes,
                _msgs,
            ) = self.state.inline_text(
                caption_txt,
                self.lineno,
            )
            caption = nodes.caption(
                caption_txt,
                '',
                *inline_nodes,
            )
            fig += caption
        self.add_name(fig)
        return [fig]


def setup(app: Sphinx) -> dict:
    app.add_config_value('d2_bin', None, 'env')
    app.add_config_value('d2_args', [], 'env')
    app.add_directive('d2', D2Diagram)
    return {
        'version': '0.1.0',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
