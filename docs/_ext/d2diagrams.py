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

A render that is *attempted and fails* (a ``d2`` bin is
present but errors on the source) is NOT silently
degraded to the stale committed SVG: it raises a
docutils error (so ``sphinx-build -W`` fails the
build). The render is also atomic — a failed/torn
render can never clobber a good committed SVG — so the
last-good diagram survives a bad edit.

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

import enum
import os
from pathlib import Path
import shlex
import shutil
import subprocess as sp
import tempfile

from docutils import nodes
from docutils.parsers.rst import directives
from sphinx.application import Sphinx
from sphinx.util import logging
from sphinx.util.docutils import SphinxDirective

log = logging.getLogger(__name__)

# subdir (under the sphinx srcdir) holding rendered,
# git-committed, fallback SVG outputs.
_outdir: str = '_diagrams'

# per-build map of {output-svg-name: source-relpath} used
# to detect 2 distinct `.d2` sources colliding on a single
# output stem; reset on each `builder-inited` (see `setup`).
_seen_outputs: dict[str, str] = {}


class RenderResult(enum.Enum):
    '''
    Outcome of a `render_svg()` call.

    '''
    OK = 'ok'                # fresh SVG exists on disk
    FELL_BACK = 'fell_back'  # no bin; serving committed SVG
    NO_OUTPUT = 'no_output'  # no bin AND no committed SVG
    FAILED = 'failed'        # bin present, render errored


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
) -> RenderResult:
    '''
    Maybe (re)render `src` -> `out` and report the
    outcome as a `RenderResult`.

    A render is performed only when `out` is missing or
    older than `src` AND a `d2` binary is available. The
    write is atomic (temp-file + `os.replace`) so a
    failed or torn render never clobbers an existing
    (committed) SVG.

    '''
    fresh: bool = (
        out.exists()
        and
        src.stat().st_mtime <= out.stat().st_mtime
    )
    if fresh:
        return RenderResult.OK
    d2cmd: list[str]|None = find_d2(app)
    if d2cmd is None:
        # no binary: fall back to whatever is committed,
        # else signal the caller to emit the raw source.
        if out.exists():
            log.info(
                f'no d2 binary; using committed svg '
                f'for {src.name}'
            )
            return RenderResult.FELL_BACK
        return RenderResult.NO_OUTPUT
    out.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    # render into a sibling temp-file first so a bad
    # `d2` exit (or a crash mid-write) leaves any prior
    # committed SVG fully intact.
    fd, tmpname = tempfile.mkstemp(
        dir=str(out.parent),
        prefix=f'.{out.stem}.',
        suffix='.svg.tmp',
    )
    os.close(fd)
    tmp = Path(tmpname)
    argv: list[str] = (
        d2cmd
        + list(app.config.d2_args)
        + [str(src), str(tmp)]
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
        tmp.unlink(missing_ok=True)
        log.warning(
            f'd2 invocation failed for {src.name}: '
            f'{err}'
        )
        return RenderResult.FAILED
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        log.warning(
            f'd2 render error for {src.name}:\n'
            f'{proc.stderr}'
        )
        return RenderResult.FAILED
    # atomic swap-in of the freshly rendered SVG.
    os.replace(tmp, out)
    return RenderResult.OK


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
        # collision guard: two distinct sources must not
        # map onto the same output stem within a build.
        prior: str|None = _seen_outputs.get(out.name)
        if (
            prior is not None
            and
            prior != relsrc
        ):
            err = self.state_machine.reporter.error(
                f'd2 output collision: {relsrc!r} and '
                f'{prior!r} both render to '
                f'{_outdir}/{out.name}; rename one '
                f'`.d2` stem',
                line=self.lineno,
            )
            return [err]
        _seen_outputs[out.name] = relsrc

        result: RenderResult = render_svg(
            self.env.app,
            src,
            out,
        )
        if result is RenderResult.FAILED:
            # loud, build-failing (under `-W`) signal; the
            # prior committed SVG, if any, is untouched.
            err = self.state_machine.reporter.error(
                f'd2 render failed for {relsrc} (see '
                f'build log); the committed svg, if any, '
                f'was left untouched',
                line=self.lineno,
            )
            return [err]
        if result is RenderResult.NO_OUTPUT:
            # last resort: emit the raw d2 source so no
            # content is ever silently dropped.
            log.warning(
                f'no svg available for {relsrc}; '
                f'emitting d2 source as literal block'
            )
            src_text: str = src.read_text()
            literal = nodes.literal_block(
                src_text,
                src_text,
            )
            literal['language'] = 'text'
            return [literal]
        # OK | FELL_BACK -> embed the SVG figure.
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


def _reset_seen(
    app: Sphinx,
) -> None:
    '''
    Clear the per-build output-collision map at the
    start of each build.

    '''
    _seen_outputs.clear()


def setup(app: Sphinx) -> dict:
    app.add_config_value('d2_bin', None, 'env')
    app.add_config_value('d2_args', [], 'env')
    app.add_directive('d2', D2Diagram)
    app.connect('builder-inited', _reset_seen)
    return {
        'version': '0.1.0',
        # NB: run() writes the rendered SVG during the
        # READ phase; the temp-file + `os.replace` swap
        # keeps that atomic so concurrent renders of the
        # same diagram (under `sphinx-build -j`) can't
        # tear the output file.
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
