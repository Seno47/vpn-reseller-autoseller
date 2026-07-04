from __future__ import annotations

import argparse
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Sample:
    timestamp: float
    rss_mb: float
    cpu_seconds: float


def process_stats_windows(pid: int) -> tuple[float, float]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            f"$root = {pid}; "
            "$ids = @($root); "
            "$queue = @($root); "
            "while ($queue.Count -gt 0) { "
            "  $current = $queue[0]; "
            "  if ($queue.Count -gt 1) { $queue = $queue[1..($queue.Count - 1)] } else { $queue = @() } "
            "  $children = Get-CimInstance Win32_Process -Filter \"ParentProcessId=$current\"; "
            "  foreach ($child in $children) { "
            "    if ($ids -notcontains [int]$child.ProcessId) { "
            "      $ids += [int]$child.ProcessId; "
            "      $queue += [int]$child.ProcessId; "
            "    } "
            "  } "
            "} "
            "$workingSet = 0; "
            "$cpu = 0; "
            "foreach ($id in $ids) { "
            "  try { "
            "    $p = Get-Process -Id $id -ErrorAction Stop; "
            "    $workingSet += $p.WorkingSet64; "
            "    if ($null -ne $p.CPU) { $cpu += [double]$p.CPU; } "
            "  } catch {} "
            "} "
            'Write-Output "$workingSet,$cpu"'
        ),
    ]
    output = subprocess.check_output(command, text=True).strip()
    working_set, cpu_seconds = output.split(",", 1)
    return int(working_set) / 1024 / 1024, float(cpu_seconds or 0)


def linux_descendant_pids(root_pid: int) -> set[int]:
    children_by_parent: dict[int, list[int]] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            stat = stat_path.read_text(encoding="utf-8").split()
            pid = int(stat[0])
            parent_pid = int(stat[3])
        except (OSError, ValueError, IndexError):
            continue
        children_by_parent.setdefault(parent_pid, []).append(pid)

    result = {root_pid}
    queue = [root_pid]
    while queue:
        current = queue.pop(0)
        for child_pid in children_by_parent.get(current, []):
            if child_pid not in result:
                result.add(child_pid)
                queue.append(child_pid)
    return result


def process_stats_linux(pid: int) -> tuple[float, float]:
    ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    page_size = os.sysconf("SC_PAGE_SIZE")
    total_rss_pages = 0
    total_ticks = 0
    for current_pid in linux_descendant_pids(pid):
        try:
            stat = Path(f"/proc/{current_pid}/stat").read_text(encoding="utf-8").split()
            statm = Path(f"/proc/{current_pid}/statm").read_text(encoding="utf-8").split()
        except OSError:
            continue
        total_ticks += int(stat[13]) + int(stat[14])
        total_rss_pages += int(statm[1])
    return total_rss_pages * page_size / 1024 / 1024, total_ticks / ticks


def process_stats(pid: int) -> tuple[float, float]:
    if platform.system().lower() == "windows":
        return process_stats_windows(pid)
    return process_stats_linux(pid)


def wait_for_health(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Application did not become healthy: {last_error}")


def cpu_percent(samples: list[Sample]) -> list[float]:
    values: list[float] = []
    cores = os.cpu_count() or 1
    for previous, current in zip(samples, samples[1:]):
        elapsed = current.timestamp - previous.timestamp
        if elapsed <= 0:
            continue
        values.append(max(0.0, (current.cpu_seconds - previous.cpu_seconds) / elapsed / cores * 100))
    return values


def print_summary(samples: list[Sample], *, duration: int, port: int) -> None:
    rss_values = [sample.rss_mb for sample in samples]
    cpu_values = cpu_percent(samples)
    print()
    print("XyraNet Reseller Autoseller resource measurement")
    print(f"OS: {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Python: {platform.python_version()}")
    print(f"Port: {port}")
    print(f"Duration: {duration}s")
    print(f"Samples: {len(samples)}")
    print()
    print("Memory RSS:")
    print(f"  min: {min(rss_values):.1f} MB")
    print(f"  avg: {statistics.mean(rss_values):.1f} MB")
    print(f"  max: {max(rss_values):.1f} MB")
    print()
    print("CPU:")
    if cpu_values:
        print(f"  avg: {statistics.mean(cpu_values):.2f}% of total machine CPU")
        print(f"  max: {max(cpu_values):.2f}% of total machine CPU")
    else:
        print("  not enough samples")


def build_env(port: int, database_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "APP_HOST": "127.0.0.1",
            "APP_PORT": str(port),
            "APP_BASE_URL": f"http://127.0.0.1:{port}",
            "DATABASE_PATH": str(database_path),
            "ENABLE_TELEGRAM": "false",
            "TELEGRAM_BOT_TOKEN": "",
            "ADMIN_IDS": "123456789",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "benchmark-password",
            "ADMIN_TOKEN": "benchmark-admin-token-1234567890",
            "XYRANET_API_KEY": "",
            "DIGISELLER_SELLER_ID": "",
            "DIGISELLER_API_KEY": "",
            "GGSEL_SELLER_ID": "",
            "GGSEL_API_KEY": "",
            "LOG_LEVEL": "WARNING",
        }
    )
    return env


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure idle RAM and CPU usage for the autoseller app.")
    parser.add_argument("--duration", type=int, default=60, help="Measurement duration in seconds.")
    parser.add_argument("--warmup", type=int, default=5, help="Seconds to wait after /health is ready.")
    parser.add_argument("--port", type=int, default=18095, help="Temporary local port for the measurement run.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    run_py = repo_root / "run.py"
    if not run_py.exists():
        raise SystemExit("run.py not found. Run this script from the project repository.")

    with tempfile.TemporaryDirectory(prefix="xyranet-autoseller-measure-") as tmp:
        database_path = Path(tmp) / "reseller-measure.sqlite3"
        env = build_env(args.port, database_path)
        process = subprocess.Popen(
            [sys.executable, str(run_py)],
            cwd=repo_root,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_for_health(f"http://127.0.0.1:{args.port}/health", timeout=30)
            time.sleep(args.warmup)
            samples: list[Sample] = []
            end_at = time.monotonic() + args.duration
            while time.monotonic() <= end_at:
                if process.poll() is not None:
                    raise RuntimeError(f"Application exited early with code {process.returncode}")
                rss_mb, cpu_seconds = process_stats(process.pid)
                samples.append(Sample(timestamp=time.monotonic(), rss_mb=rss_mb, cpu_seconds=cpu_seconds))
                time.sleep(1)
            if samples:
                print_summary(samples, duration=args.duration, port=args.port)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
