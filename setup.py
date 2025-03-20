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
    description='structured concurrent `trio`-"actors"',
    long_description=readme,
    license='AGPLv3',
    author='Tyler Goodlet',
    maintainer='Tyler Goodlet',
    maintainer_email='goodboy_foss@protonmail.com',
    url='https://github.com/goodboy/tractor',
    platforms=['linux', 'windows'],
    packages=[
        'tractor',
        'tractor.experimental',  # wacky ideas
        'tractor.trionics',  # trio extensions
        'tractor.msg',  # lowlevel data types
        'tractor._testing',  # internal cross-subsys suite utils
        'tractor.devx',  # "dev-experience"
    ],
    install_requires=[

        # trio related
        # proper range spec:
        # https://packaging.python.org/en/latest/discussions/install-requires-vs-requirements/#id5
        'trio == 0.24',

        # 'async_generator',  # in stdlib mostly!
        # 'trio_typing',  # trio==0.23.0 has type hints!
        # 'exceptiongroup',  # in stdlib as of 3.11!

        # tooling
        'stackscope',
        'tricycle',
        'trio_typing',
        'colorlog',
        'wrapt',

        # IPC serialization
        'msgspec',

        # debug mode REPL
        'pdbp',

        # TODO: distributed transport using
        # linux kernel networking
        # 'pyroute2',

        # pip ref docs on these specs:
        # https://pip.pypa.io/en/stable/reference/requirement-specifiers/#examples
        # and pep:
        # https://peps.python.org/pep-0440/#version-specifiers

    ],
    tests_require=['pytest'],
    python_requires=">=3.11",
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
