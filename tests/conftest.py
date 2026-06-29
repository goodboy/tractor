"""
Top level of the testing suites!

"""
from __future__ import annotations
import sys
import subprocess
import os
import signal
import platform
import time
from pathlib import Path
from typing import Literal

import pytest
import tractor
from tractor._testing import (
    examples_dir as examples_dir,
    tractor_test as tractor_test,
    expect_ctxc as expect_ctxc,
)

pytest_plugins: list[str] = [
    'pytester',
    # NOTE, now loaded in `pytest-ini` section of `pyproject.toml`
    # 'tractor._testing.pytest',
]

_ci_env: bool = os.environ.get('CI', False)
_non_linux: bool = platform.system() != 'Linux'

# Sending signal.SIGINT on subprocess fails on windows. Use CTRL_* alternatives
if platform.system() == 'Windows':
    _KILL_SIGNAL = signal.CTRL_BREAK_EVENT
    _INT_SIGNAL = signal.CTRL_C_EVENT
    _INT_RETURN_CODE = 3221225786
else:
    _KILL_SIGNAL = signal.SIGKILL
    _INT_SIGNAL = signal.SIGINT
    _INT_RETURN_CODE = 1 if sys.version_info < (3, 8) else -signal.SIGINT.value


no_windows = pytest.mark.skipif(
    platform.system() == "Windows",
    reason="Test is unsupported on windows",
)
no_macos = pytest.mark.skipif(
    platform.system() == "Darwin",
    reason="Test is unsupported on MacOS",
)


def get_cpu_state(
    icpu: int = 0,
    setting: Literal[
        'scaling_governor',
        '*_pstate_max_freq',
        'scaling_max_freq',
        # 'scaling_cur_freq',
    ] = '*_pstate_max_freq',
) -> tuple[
    Path,
    str|int,
]|None:
    '''
    Attempt to read the (first) CPU's setting according
    to the set `setting` from under the file-sys,

    /sys/devices/system/cpu/cpu0/cpufreq/{setting}

    Useful to determine latency headroom for various perf affected
    test suites.

    '''
    try:
        # Read governor for core 0 (usually same for all)
        setting_path: Path = list(
            Path(f'/sys/devices/system/cpu/cpu{icpu}/cpufreq/')
            .glob(f'{setting}')
        )[0]  # <- XXX must be single match!
        with open(
            setting_path,
            'r',
        ) as f:
            return (
                setting_path,
                f.read().strip(),
            )
    except (FileNotFoundError, IndexError):
        return None


def cpu_scaling_factor() -> float:
    '''
    Return a latency-headroom multiplier (>= 1.0) reflecting how
    much to inflate time-limits when CPU-freq scaling is active on
    linux.

    When no local scaling info is available (non-linux, missing
    sysfs) the base factor is 1.0; a flat CI bump is then applied
    on top (see below).

    '''
    factor: float = 1.
    if not _non_linux:
        mx = get_cpu_state()
        cur = get_cpu_state(setting='scaling_max_freq')
        if (
            mx is not None
            and
            cur is not None
        ):
            _mx_pth, max_freq = mx
            _cur_pth, cur_freq = cur
            cpu_scaled: float = int(cur_freq) / int(max_freq)
            if cpu_scaled != 1.:
                factor = 1. / (
                    cpu_scaled * 2  # <- bc likely "dual threaded"
                )

    # XXX, GH Actions (and most shared) CI runners are slow + noisy
    # and — unlike a throttled local box — do NOT expose CPU-freq
    # scaling via sysfs, so the probe above reads 1.0 and adds no
    # headroom. Apply a flat CI bump so every timing-test deadline
    # /assert that keys off this factor gets headroom on CI HW
    # (compounds with any local-throttle factor).
    #
    # macOS runners are noticeably slower + noisier than the linux
    # ones for our multi-actor cancel-cascade tests, so give them
    # extra headroom (3x vs 2x).
    if _ci_env:
        factor *= 3 if _non_linux else 2

    return factor


# session-cached sustained-load throttle multiplier — measured
# once (lazily) on the first `cpu_perf_headroom()` call. `None`
# = not-yet-measured.
_sustained_headroom: float|None = None


def _measure_sustained_headroom(
    secs: float = 0.9,
    # a healthy all-core sustained clock holds AT/ABOVE this
    # fraction of the package single-core max ceiling (boost sags
    # under full multi-core load even un-throttled, but not far);
    # at/above it we assume no throttle and return 1.0.
    throttle_gate: float = 0.6,
    max_headroom: float = 4.,
) -> float:
    '''
    One-shot all-core burn returning a latency multiplier
    (>= 1.0) that reflects *sustained-load* CPU throttle.

    Catches the firmware/EC power-cap clamp (AMD PPT/STAPM &
    friends) that pins achieved `scaling_cur_freq` to a fraction
    of the ceiling under multi-core load while EVERY static knob
    (`governor`, `scaling_max_freq`, `EPP`, `platform_profile`)
    still reads "full performance". That cap is INVISIBLE to
    `cpu_scaling_factor()` and is the gremlin behind mass `trio`
    deadline-miss failures on byte-identical code — see
    `scripts/cpu-perf-check`.

    Best-effort: returns 1.0 on non-linux / missing sysfs / any
    error so it can never break a test run.

    '''
    import glob
    import multiprocessing as mp

    def _read_mhz(path: str) -> int|None:
        try:
            with open(path) as f:
                return int(f.read()) // 1000
        except OSError:
            return None

    try:
        maxs: list[int] = [
            v for f in glob.glob(
                '/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_max_freq'
            )
            if (v := _read_mhz(f)) is not None
        ]
        pkg_max: int = max(maxs) if maxs else 0
        if not pkg_max:
            return 1.

        def _burn(stop: float) -> None:
            # fixed-width (64-bit-masked) arithmetic keeps this a
            # steady ALU load; an unmasked `x` grows ~x**2/iter into
            # a huge bigint, degenerating into noisy alloc/mul-bound
            # work (and needless memory) across N procs.
            x: int = 1
            while time.perf_counter() < stop:
                x = (x + (x * x ^ 0x5)) & 0xFFFF_FFFF_FFFF_FFFF

        # explicit `fork` ctx so we're immune to whatever global
        # mp start-method tractor/the suite may have set (`spawn`
        # would re-exec + re-import 24x — slow and pointless here).
        ctx = mp.get_context('fork')
        ncpu: int = os.cpu_count() or 1
        stop: float = time.perf_counter() + secs
        procs = [
            ctx.Process(target=_burn, args=(stop,), daemon=True)
            for _ in range(ncpu)
        ]
        for p in procs:
            p.start()

        # skip the ~0.4s boost window so we sample the steady
        # state AFTER any power-cap has engaged.
        samples: list[int] = []
        time.sleep(0.4)
        while time.perf_counter() < stop - 0.1:
            curs: list[int] = [
                v for f in glob.glob(
                    '/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq'
                )
                if (v := _read_mhz(f)) is not None
            ]
            if curs:
                samples.append(sum(curs) // len(curs))
            time.sleep(0.15)
        for p in procs:
            p.join()

        if not samples:
            return 1.
        frac: float = (sum(samples) // len(samples)) / pkg_max
        # below the gate we read it as a power-cap throttle. The
        # spawn/IPC/fork-bound work these budgets guard slows ~1:1
        # with the achieved-vs-max freq ratio, so compensate by the
        # FULL inverse fraction (a boost-discounted factor
        # under-shoots and still trips the marginal cases).
        if frac >= throttle_gate:
            return 1.
        # a 0/parked-core freq read would `ZeroDivisionError` the
        # inverse below — silently swallowed by the outer `except`
        # into a 1.0 (no-throttle), defeating the probe on exactly
        # the broken box it should flag; read 0 as max throttle.
        if frac <= 0:
            return max_headroom
        return min(max_headroom, 1. / frac)

    except Exception:
        return 1.


def cpu_perf_headroom() -> float:
    '''
    Latency-headroom multiplier (>= 1.0) covering BOTH cpu-perf
    throttle classes — multiply a test's deadline by it, e.g.
    `timeout *= cpu_perf_headroom()`:

      - static cpu-freq scaling — via `cpu_scaling_factor()`
        (governor/policy lowered the `scaling_max_freq` ceiling).

      - sustained-load power-cap throttle — via
        `_measure_sustained_headroom()` (firmware/EC PPT/STAPM
        clamps achieved freq under load while every static knob
        reads "performance"; INVISIBLE to the static check). This
        is the gremlin behind mass `trio` deadline-miss failures
        on unchanged code — see
        `ai/conc-anal/trio_033_cancel_cascade_slowdown_depth3_issue.md`.

    The sustained probe runs ONCE per session (cached); the cost
    is a ~0.9s all-core burn on first call only.

    '''
    global _sustained_headroom
    static: float = cpu_scaling_factor()
    if _non_linux:
        return static
    if _sustained_headroom is None:
        _sustained_headroom = _measure_sustained_headroom()
    return max(static, _sustained_headroom)


# NOTE, the `--ll`/`--tl` CLI flags + the `loglevel`, `test_log`
# and `testing_pkg_name` fixtures have been factored into the
# `tractor._testing.pytest` plugin (loaded via the `-p` entry in
# `pyproject.toml`'s `[tool.pytest.ini_options]`) so downstream
# consuming projects (eg. `modden`) inherit them for free. The
# plugin's `testing_pkg_name` fixture defaults to `'tractor'`, so
# this suite keeps treating `--ll` as the runtime loglevel.


@pytest.fixture(scope='session')
def ci_env() -> bool:
    '''
    Detect CI environment.

    '''
    return _ci_env


def sig_prog(
    proc: subprocess.Popen,
    sig: int,
    canc_timeout: float = 0.2,
    tries: int = 3,
) -> int:
    '''
    Kill the actor-process with `sig`.

    Prefer to kill with the provided signal and
    failing a `canc_timeout`, send a `SIKILL`-like
    to ensure termination.

    '''
    for i in range(tries):
        proc.send_signal(sig)
        if proc.poll() is None:
            print(
                f'WARNING, proc still alive after,\n'
                f'canc_timeout={canc_timeout!r}\n'
                f'sig={sig!r}\n'
                f'\n'
                f'{proc.args!r}\n'
            )
            time.sleep(canc_timeout)
    else:
        # TODO: why sometimes does SIGINT not work on teardown?
        # seems to happen only when trace logging enabled?
        if proc.poll() is None:
            print(
                f'XXX WARNING KILLING PROG WITH SIGINT XXX\n'
                f'canc_timeout={canc_timeout!r}\n'
                f'{proc.args!r}\n'
            )
            proc.send_signal(_KILL_SIGNAL)

    ret: int = proc.wait()
    assert ret


# NOTE, the `daemon` fixture (+ its `_wait_for_daemon_ready`
# helper + the post-yield teardown drain logic) has been
# moved to `tests/discovery/conftest.py` since 100% of its
# consumers are discovery-protocol tests now living under
# that subdir. See:
# - `tests/discovery/test_multi_program.py`
# - `tests/discovery/test_registrar.py`
# - `tests/discovery/test_tpt_bind_addrs.py`


# @pytest.fixture(autouse=True)
# def shared_last_failed(pytestconfig):
#     val = pytestconfig.cache.get("example/value", None)
#     breakpoint()
#     if val is None:
#         pytestconfig.cache.set("example/value", val)
#     return val


# TODO: a way to let test scripts (like from `examples/`)
# guarantee they won't `registry_addrs` collide!
# -[ ] maybe use some kinda standard `def main()` arg-spec that
#     we can introspect from a fixture that is called from the test
#     body?
# -[ ] test and figure out typing for below prototype! Bp
#
# @pytest.fixture
# def set_script_runtime_args(
#     reg_addr: tuple,
# ) -> Callable[[...], None]:

#     def import_n_partial_in_args_n_triorun(
#         script: Path,  # under examples?
#         **runtime_args,
#     ) -> Callable[[], Any]:  # a `partial`-ed equiv of `trio.run()`

#         # NOTE, below is taken from
#         # `.test_advanced_faults.test_ipc_channel_break_during_stream`
#         mod: ModuleType = import_path(
#             examples_dir() / 'advanced_faults'
#             / 'ipc_failure_during_stream.py',
#             root=examples_dir(),
#             consider_namespace_packages=False,
#         )
#         return partial(
#             trio.run,
#             partial(
#                 mod.main,
#                 **runtime_args,
#             )
#         )
#     return import_n_partial_in_args_n_triorun
