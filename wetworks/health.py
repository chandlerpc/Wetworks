"""Host health snapshot for briefs."""

import shutil
import time
from pathlib import Path

import psutil


def snapshot(data_dir: Path) -> dict:
    disk = shutil.disk_usage(data_dir if data_dir.exists() else "/")
    mem = psutil.virtual_memory()
    return {
        "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        "cpu_percent": psutil.cpu_percent(interval=0.5),
        "mem_used_gb": round(mem.used / 1e9, 1),
        "mem_total_gb": round(mem.total / 1e9, 1),
        "disk_free_gb": round(disk.free / 1e9, 1),
        "disk_total_gb": round(disk.total / 1e9, 1),
    }


def format_health(h: dict) -> str:
    return (
        f"Uptime {h['uptime_hours']}h · CPU {h['cpu_percent']}% · "
        f"RAM {h['mem_used_gb']}/{h['mem_total_gb']} GB · "
        f"Disk free {h['disk_free_gb']}/{h['disk_total_gb']} GB"
    )
