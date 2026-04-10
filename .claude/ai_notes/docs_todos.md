# Docs TODOs

## Auto-sync README code examples with source

The `docs/README.rst` has inline code blocks that
duplicate actual example files (e.g.
`examples/infected_asyncio_echo_server.py`). Every time
the public API changes we have to manually sync both.

Sphinx's `literalinclude` directive can pull code directly
from source files:

```rst
.. literalinclude:: ../examples/infected_asyncio_echo_server.py
   :language: python
   :caption: examples/infected_asyncio_echo_server.py
```

Or to include only a specific function/section:

```rst
.. literalinclude:: ../examples/infected_asyncio_echo_server.py
   :language: python
   :pyobject: aio_echo_server
```

This way the docs always reflect the actual code without
manual syncing.

### Considerations
- `README.rst` is also rendered on GitHub/PyPI which do
  NOT support `literalinclude` - so we'd need a build
  step or a separate `_sphinx_readme.rst` (which already
  exists at `docs/github_readme/_sphinx_readme.rst`).
- Could use a pre-commit hook or CI step to extract code
  from examples into the README for GitHub rendering.
- Another option: `sphinx-autodoc` style approach where
  docstrings from the actual module are pulled in.
