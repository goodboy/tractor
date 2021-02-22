#!/bin/bash
sphinx-build -b rst ./github_readme ./

mv _sphinx_readme.rst README.rst
