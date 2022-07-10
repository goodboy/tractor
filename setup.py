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
    version='0.1.0a5.dev',  # alpha zone
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
        'tractor.testing',
    ],
    install_requires=[

        # trio related
        'trio>0.8',
        'async_generator',
        'trio_typing',

        # tooling
        'tricycle',
        'trio_typing',

        # tooling
        'colorlog',
        'wrapt',

        # pip ref docs on these specs:
        # https://pip.pypa.io/en/stable/reference/requirement-specifiers/#examples
        # and pep:
        # https://peps.python.org/pep-0440/#version-specifiers

        'pdbpp <= 0.10.1; python_version < "3.10"',

        # 3.10 has an outstanding unreleased issue and `pdbpp` itself
        # pins to patched forks of its own dependencies as well.
        "pdbpp @ git+https://github.com/pdbpp/pdbpp@master#egg=pdbpp ; python_version >= '3.10'",  # noqa: E501

        # windows deps workaround for ``pdbpp``
        # https://github.com/pdbpp/pdbpp/issues/498
        # https://github.com/pdbpp/fancycompleter/issues/37
        'pyreadline3 ; platform_system == "Windows"',

        # serialization
        'msgspec >= "0.4.0"'

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
        "Programming Language :: Python :: 3.9",
        "Intended Audience :: Science/Research",
        "Intended Audience :: Developers",
        "Topic :: System :: Distributed Computing",
    ],
)
