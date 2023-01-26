#!/usr/bin/env python
#
# tractor: structured concurrent "actors".
#
# Copyright 2018-eternity Tyler Goodlet.

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

from setuptools import setup

with open('docs/README.rst', encoding='utf-8') as f:
    readme = f.read()


setup(
    name="tractor",
    version='0.1.0a6dev0',  # alpha zone
    description='structured concurrrent "actors"',
    long_description=readme,
    license='AGPLv3',
    author='Tyler Goodlet',
    maintainer='Tyler Goodlet',
    maintainer_email='jgbt@protonmail.com',
    url='https://github.com/goodboy/tractor',
    platforms=['linux', 'windows'],
    packages=[
        'tractor',
        'tractor.experimental',
        'tractor.trionics',
    ],
    install_requires=[

        # trio related
        # proper range spec:
        # https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/#id5
        'trio >= 0.22',
        'async_generator',
        'trio_typing',
        'exceptiongroup',

        # tooling
        'tricycle',
        'trio_typing',

        # tooling
        'colorlog',
        'wrapt',

        # serialization
        'msgspec',

        # debug mode REPL
        'pdbpp',

        # pip ref docs on these specs:
        # https://pip.pypa.io/en/stable/reference/requirement-specifiers/#examples
        # and pep:
        # https://peps.python.org/pep-0440/#version-specifiers

        # windows deps workaround for ``pdbpp``
        # https://github.com/pdbpp/pdbpp/issues/498
        # https://github.com/pdbpp/fancycompleter/issues/37
        'pyreadline3 ; platform_system == "Windows"',


    ],
    tests_require=['pytest'],
    python_requires=">=3.9",
    keywords=[
        'trio',
        'async',
        'concurrency',
        'structured concurrency',
        'actor model',
        'distributed',
        'multiprocessing'
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
        "Framework :: Trio",
        "License :: OSI Approved :: GNU Affero General Public License v3 or later (AGPLv3+)",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.10",
        "Intended Audience :: Science/Research",
        "Intended Audience :: Developers",
        "Topic :: System :: Distributed Computing",
    ],
)
