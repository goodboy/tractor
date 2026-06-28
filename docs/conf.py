# tractor: distributed structured concurrency.
'''
Sphinx config for `tractor`'s documentation.

Theme-wise we ride the `pydata_sphinx_theme` (per the
research + history in #157) skinned to a minimal
black + white look via `_static/css/custom.css`; see
the local extensions under `_ext/` for our `.. d2::`
diagram and `.. margin::` aside directives.

Build locally via,

    uv run --group docs make -C docs html

'''
from importlib.metadata import version as get_version
from pathlib import Path
import sys

# local sphinx extensions live in `_ext/`:
# - `d2diagrams`: `.. d2::` diagram rendering
# - `marginalia`: `.. margin::` RHS prose-asides
sys.path.insert(
    0,
    str((Path(__file__).parent / '_ext').resolve()),
)

# -- project info ---------------------------------

project = 'tractor'
copyright = '2018-2026, Tyler Goodlet'
author = 'Tyler Goodlet'
release: str = get_version('tractor')
version: str = release

# -- general config -------------------------------

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.autosummary',
    'sphinx.ext.intersphinx',
    'sphinx.ext.viewcode',
    'sphinx.ext.todo',
    # emit a `.nojekyll` so GitHub Pages serves the `_static/`
    # + `_images/` dirs (Jekyll would otherwise drop `_`-prefixed
    # paths and break all styling/assets).
    'sphinx.ext.githubpages',
    'sphinx_design',
    'sphinx_copybutton',
    'sphinxext.opengraph',
    'sphinx_togglebutton',
    'd2diagrams',
    'marginalia',
]
templates_path = ['_templates']
exclude_patterns = [
    '_build',
    # the pypi/gh readme + its standalone generator
    # sub-project; NOT doc-tree pages.
    'README.rst',
    'github_readme',
    'Thumbs.db',
    '.DS_Store',
]
root_doc = 'index'
# TODO: flip this on + burn down the (many) warnings
# from our informal docstring style; see the autodoc
# readiness notes from the revamp's recon pass.
nitpicky = False

# -- autodoc/autosummary --------------------------

autodoc_member_order = 'bysource'
autodoc_typehints = 'description'
autosummary_generate = True

# -- intersphinx ----------------------------------

intersphinx_mapping = {
    'python': (
        'https://docs.python.org/3',
        None,
    ),
    'trio': (
        'https://trio.readthedocs.io/en/stable',
        None,
    ),
    # NOTE, msgspec's site doesn't publish an
    # `objects.inv` (404s) so no intersphinx for it.
    'pytest': (
        'https://docs.pytest.org/en/stable',
        None,
    ),
}

# -- html output ----------------------------------

html_theme = 'pydata_sphinx_theme'
html_title = 'tractor'
# canonical site root (GitHub Pages); drives <link rel=canonical>,
# og:url + any future sitemap. Update if a custom domain is added.
html_baseurl = 'https://goodboy.github.io/tractor/'
html_logo = '_static/tractor_logo_side.svg'
html_favicon = '_static/tractor_logo_side.svg'
html_static_path = ['_static']
html_css_files = ['css/custom.css']
html_show_sourcelink = False
html_theme_options = {
    # theme-adaptive navbar logo: faces transparent, linework
    # near-black on light / near-white on dark (pydata swaps by
    # the active theme). Matches the landing hero's wireframe.
    'logo': {
        'image_light': '_static/tractor_logo_nav_light.svg',
        'image_dark': '_static/tractor_logo_nav_dark.svg',
        'alt_text': 'tractor',
        # text shown to the right of the navbar logo (à la
        # polars).
        'text': 'tractor',
    },
    'github_url': 'https://github.com/goodboy/tractor',
    'navbar_align': 'content',
    'show_toc_level': 2,
    'secondary_sidebar_items': {
        '**': ['page-toc'],
        'index': [],
    },
    'use_edit_page_button': True,
    'footer_start': ['copyright'],
    'footer_end': ['theme-version'],
    'pygments_light_style': 'algol_nu',
    'pygments_dark_style': 'github-dark',
}
html_context = {
    'github_user': 'goodboy',
    'github_repo': 'tractor',
    'github_version': 'main',
    'doc_path': 'docs',
}

# -- ext: opengraph -------------------------------

ogp_site_url = 'https://goodboy.github.io/tractor/'
ogp_use_first_image = True

# -- ext: copybutton ------------------------------

copybutton_prompt_text = r'>>> |\.\.\. |\$ '
copybutton_prompt_is_regexp = True

# -- ext: todo ------------------------------------

todo_include_todos = True

# -- ext: d2diagrams (local) ----------------------

# normally resolved from PATH or the `D2_BIN` env
# var; when neither hits, the committed SVGs under
# `_diagrams/` are used as-is.
d2_bin = None
d2_args = []
