#!/usr/bin/env python
#
# tractor: a trionic actor model built on `multiprocessing` and `trio`
#
# Copyright (C) 2018-2020  Tyler Goodlet

# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
from setuptools import setup

with open('docs/README.rst', encoding='utf-8') as f:
    readme = f.read()


setup(
    name="tractor",
    version='0.1.0a0',  # first ever alpha
    description='structured concurrrent "actors"',
    long_description=readme,
    license='GPLv3',
    author='Tyler Goodlet',
    maintainer='Tyler Goodlet',
    maintainer_email='jgbt@protonmail.com',
    url='https://github.com/goodboy/tractor',
    platforms=['linux', 'windows'],
    packages=[
        'tractor',
        'tractor.testing',
    ],
    install_requires=[
        'trio>0.8',
        'msgpack',
        'async_generator',
        'colorlog',
        'wrapt',
        'trio_typing',
        'pdbpp',
    ],
    tests_require=['pytest'],
    python_requires=">=3.7",
    keywords=[
        'trio',
        "async",
        "concurrency",
        "actor model",
        "distributed",
        'multiprocessing'
    ],
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Operating System :: POSIX :: Linux",
        "Operating System :: Microsoft :: Windows",
        "Framework :: Trio",
        "License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Intended Audience :: Science/Research",
        "Intended Audience :: Developers",
        "Topic :: System :: Distributed Computing",
    ],
)
