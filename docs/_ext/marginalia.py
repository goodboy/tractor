# tractor: distributed structured concurrency.
'''
``.. margin::`` - prose-anchored, right-margin asides.

A theme-agnostic vendoring of `sphinx_book_theme`'s
`Margin` directive: a `docutils` `Sidebar` subclass
which tags the node with a ``margin`` class; placement
is then pure CSS (see ``_static/css/custom.css``)
allowing use on any theme incl. our
`pydata_sphinx_theme`.

Usage,

.. code:: rst

    .. margin:: An optional title

       Aside content; text, figures, whatever.

'''
from docutils import nodes
from docutils.parsers.rst.directives.body import (
    Sidebar,
)
from sphinx.application import Sphinx


class Margin(Sidebar):
    '''
    Notes/figures placed in the right margin, anchored
    at the current point in the prose flow.

    '''
    required_arguments = 0
    optional_arguments = 1

    def run(self) -> list[nodes.Node]:
        if not self.arguments:
            self.arguments = ['']
        out: list[nodes.Node] = super().run()
        out[0].attributes['classes'].append('margin')
        return out


def setup(app: Sphinx) -> dict:
    app.add_directive('margin', Margin)
    return {
        'version': '0.1.0',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
