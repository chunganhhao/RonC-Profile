"""ASCII-only, value-free launch diagnostics for the runner's stdout log."""

from contextlib import contextmanager
from time import perf_counter


def report_timing(stage: str, started: float, status: str = "ok") -> None:
    print(f"LAUNCH_TIMING {stage} {perf_counter() - started:.3f}s {status}", flush=True)


@contextmanager
def launch_stage(stage: str):
    started = perf_counter()
    status = "failed"
    try:
        yield
        status = "ok"
    finally:
        report_timing(stage, started, status)
