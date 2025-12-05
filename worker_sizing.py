"""
worker_sizing.py

Worker profile helper for agent-lite.

For endpoints (laptops / desktops / terminals) we keep things very small:
- tier: "ultra-lite" by default
- 1 usable core, 1 worker by default
- strict limits on payload size and token count

All values can be overridden via environment variables.
"""

import os
import math
from typing import Dict, Any

try:
    import psutil
except ImportError:
    psutil = None


def _detect_total_cores() -> int:
    """
    Detect total logical CPU cores on this machine.
    Falls back to 1 if detection fails.
    """
    # Prefer psutil if available
    if psutil is not None and hasattr(psutil, "cpu_count"):
        try:
            cores = psutil.cpu_count(logical=True)
            if cores:
                return int(cores)
        except Exception:
            pass

    cores = os.cpu_count()
    return int(cores) if cores else 1


def _detect_cpu() -> Dict[str, Any]:
    """
    CPU sizing for agent-lite.

    We keep things intentionally conservative:
    - usable_cores: default 1 (LITE_USABLE_CORES can override)
    - max_cpu_workers: default 1 (LITE_MAX_CPU_WORKERS can override, but never
      more than usable_cores)
    """
    total_cores = _detect_total_cores()

    # How many cores the agent is allowed to treat as usable.
    usable_cores_env = os.getenv("LITE_USABLE_CORES", "1")
    try:
        usable_cores = int(usable_cores_env)
    except ValueError:
        usable_cores = 1

    if usable_cores < 1:
        usable_cores = 1
    if usable_cores > total_cores:
        usable_cores = total_cores

    # Reserve the rest for the OS / user
    reserved_cores = max(0, total_cores - usable_cores)

    # Max workers for this agent; default 1, never > usable_cores
    max_cpu_workers_env = os.getenv("LITE_MAX_CPU_WORKERS", str(usable_cores))
    try:
        max_cpu_workers = int(max_cpu_workers_env)
    except ValueError:
        max_cpu_workers = usable_cores

    if max_cpu_workers < 1:
        max_cpu_workers = 1
    if max_cpu_workers > usable_cores:
        max_cpu_workers = usable_cores

    min_cpu_workers = 1

    return {
        "total_cores": int(total_cores),
        "reserved_cores": int(reserved_cores),
        "usable_cores": int(usable_cores),
        "min_cpu_workers": int(min_cpu_workers),
        "max_cpu_workers": int(max_cpu_workers),
    }


def build_worker_profile() -> Dict[str, Any]:
    """
    Build the worker_profile structure advertised to the controller.

    Shape keeps backward compatibility with the existing controller / UI:

      {
        "tier": "ultra-lite",
        "cpu": {...},
        "gpu": {...},
        "workers": {
          "max_total_workers": int,
          "current_workers": 0
        },
        "limits": {
          "max_payload_bytes": int,
          "max_tokens": int
        }
      }
    """
    cpu_info = _detect_cpu()

    # Agent-lite: always report no GPU
    gpu_info = {
        "gpu_present": False,
        "gpu_count": 0,
        "vram_gb": None,
        "devices": [],
        "max_gpu_workers": 0,
    }

    # Total worker limit: use CPU max workers as upper bound
    max_total_workers = cpu_info.get("max_cpu_workers", 1)
    if isinstance(max_total_workers, float):
        max_total_workers = int(math.floor(max_total_workers))

    if max_total_workers < 1:
        max_total_workers = 1

    # Hard limits for what this endpoint will accept
    max_payload_bytes_env = os.getenv("LITE_MAX_PAYLOAD_BYTES", "4096")
    max_tokens_env = os.getenv("LITE_MAX_TOKENS", "1024")

    try:
        max_payload_bytes = int(max_payload_bytes_env)
    except ValueError:
        max_payload_bytes = 4096

    try:
        max_tokens = int(max_tokens_env)
    except ValueError:
        max_tokens = 1024

    tier = os.getenv("LITE_TIER", "ultra-lite")

    return {
        "tier": tier,
        "cpu": cpu_info,
        "gpu": gpu_info,
        "workers": {
            "max_total_workers": int(max_total_workers),
            "current_workers": 0,
        },
        "limits": {
            "max_payload_bytes": max_payload_bytes,
            "max_tokens": max_tokens,
        },
    }
