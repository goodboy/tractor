# Configuration file for the Sphinx documentation builder.

# this config is for the rst generation extension and thus
# requires only basic settings:
# https://github.com/sphinx-contrib/restbuilder

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
# import os
# import sys
# sys.path.insert(0, os.path.abspath('.'))

# Warn about all references to unknown targets
nitpicky = True

# The master toctree document.
master_doc = '_sphinx_readme'

# -- Project information -----------------------------------------------------

project = 'tractor'
copyright = '2018, Tyler Goodlet'
author = 'Tyler Goodlet'

# The full version, including alpha/beta/rc tags
release = '0.0.0a0.dev0'

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.intersphinx',
    'sphinx.ext.todo',
    'sphinxcontrib.restbuilder',

]

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']
