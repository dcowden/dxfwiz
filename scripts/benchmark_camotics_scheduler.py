from __future__ import annotations

import csv
import ctypes
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "tests" / "output" / "toolpaths"
JSON_OUTPUT = OUTPUT_DIR / "camotics_scheduler_benchmark.json"
CSV_OUTPUT = OUTPUT_DIR / "camotics_scheduler_benchmark.csv"
TEST_TARGET = (
    "tests/toolpaths/test_reference_operations.py::"
    "test_reference_operations_render_gcode_gallery_and_match_expected_metrics"
)


@dataclass(frozen=True)
class CpuTimes:
    idle: int
    kernel: int
    user: int


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for threads in (4, 6, 8):
        for jobs in (2, 4, 6, 8, 10, 12):
            print(f"running CAMotics benchmark: threads={threads} jobs={jobs}", flush=True)
            result = run_case(threads=threads, jobs=jobs)
            results.append(result)
            write_outputs(results)
            status = "ok" if result["returncode"] == 0 else f"failed rc={result['returncode']}"
            print(
                f"  {status}: wall={result['wall_seconds']:.2f}s "
                f"cpu_avg={result['cpu_avg_percent']:.1f}% "
                f"cpu_peak={result['cpu_peak_percent']:.1f}%",
                flush=True,
            )
            if result["returncode"] != 0:
                print(result["stdout_tail"], flush=True)
                print(result["stderr_tail"], flush=True)
                return result["returncode"]
    print(f"wrote {JSON_OUTPUT}")
    print(f"wrote {CSV_OUTPUT}")
    return 0


def run_case(*, threads: int, jobs: int) -> dict:
    env = os.environ.copy()
    env["DXFWIZ_CAMOTICS_THREADS"] = str(threads)
    env["DXFWIZ_CAMOTICS_JOBS"] = str(jobs)
    env.setdefault("DXFWIZ_CAMOTICS_TIMEOUT_SECONDS", "600")
    command = [sys.executable, "-m", "pytest", TEST_TARGET, "-q"]
    cpu_samples: list[float] = []
    previous_cpu = get_system_cpu_times()
    started = time.perf_counter()
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    while process.poll() is None:
        time.sleep(0.5)
        current_cpu = get_system_cpu_times()
        sample = cpu_percent(previous_cpu, current_cpu)
        previous_cpu = current_cpu
        if sample is not None:
            cpu_samples.append(sample)
    _, stderr = process.communicate()
    elapsed = time.perf_counter() - started
    return {
        "threads": threads,
        "jobs": jobs,
        "returncode": process.returncode,
        "wall_seconds": elapsed,
        "cpu_avg_percent": sum(cpu_samples) / len(cpu_samples) if cpu_samples else 0.0,
        "cpu_peak_percent": max(cpu_samples) if cpu_samples else 0.0,
        "cpu_samples": len(cpu_samples),
        "stdout_tail": "",
        "stderr_tail": tail(stderr),
    }


def get_system_cpu_times() -> CpuTimes:
    idle = FILETIME()
    kernel = FILETIME()
    user = FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        raise ctypes.WinError()
    return CpuTimes(filetime_to_int(idle), filetime_to_int(kernel), filetime_to_int(user))


def filetime_to_int(value: FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


def cpu_percent(previous: CpuTimes, current: CpuTimes) -> float | None:
    idle_delta = current.idle - previous.idle
    total_delta = (current.kernel - previous.kernel) + (current.user - previous.user)
    if total_delta <= 0:
        return None
    busy = max(0.0, min(1.0, 1.0 - idle_delta / total_delta))
    return busy * 100.0


def write_outputs(results: list[dict]) -> None:
    JSON_OUTPUT.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    with CSV_OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "threads",
                "jobs",
                "returncode",
                "wall_seconds",
                "cpu_avg_percent",
                "cpu_peak_percent",
                "cpu_samples",
            ],
        )
        writer.writeheader()
        for result in results:
            writer.writerow({key: result[key] for key in writer.fieldnames})


def tail(text: str, lines: int = 40) -> str:
    return "\n".join(text.splitlines()[-lines:])


if __name__ == "__main__":
    raise SystemExit(main())
