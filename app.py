#!/usr/bin/env python3
from __future__ import annotations

import os
import time
import json
import socket
import signal
import random
import threading
import traceback
from typing import Any, Dict, Optional, List, Tuple
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError

import requests

try:
    import psutil  # type: ignore
except Exception:
    psutil = None


# ---------------- config ----------------

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://10.11.12.54:8080").rstrip("/")
AGENT_NAME = os.getenv("AGENT_NAME") or socket.gethostname()

HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "8"))
LEASE_TIMEOUT_MS = int(os.getenv("LEASE_TIMEOUT_MS", "2500"))
IDLE_SLEEP_SEC = float(os.getenv("IDLE_SLEEP_SEC", "0.05"))

TASK_EXEC_TIMEOUT_SEC = float(os.getenv("TASK_EXEC_TIMEOUT_SEC", "60"))

# Ops advertised to controller
TASKS_RAW = os.getenv("TASKS", "echo")
TASKS = [t.strip() for t in TASKS_RAW.split(",") if t.strip()]

# Workers
CPU_MIN_WORKERS = int(os.getenv("CPU_MIN_WORKERS", "1"))
CPU_MAX_WORKERS = int(os.getenv("CPU_MAX_WORKERS", "0"))  # 0 => auto
RESERVED_CORES = int(os.getenv("RESERVED_CORES", "2"))

# Optional labels (json or k=v,k2=v2)
AGENT_LABELS_RAW = os.getenv("AGENT_LABELS", "")
AGENT_LABELS: Dict[str, Any] = {}
if AGENT_LABELS_RAW.strip():
    try:
        AGENT_LABELS = json.loads(AGENT_LABELS_RAW)
    except Exception:
        for part in AGENT_LABELS_RAW.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            AGENT_LABELS[k.strip()] = v.strip()


# ---------------- runtime state ----------------

stop_event = threading.Event()
_session = requests.Session()

_hits = 0
_misses = 0
_inflight = 0
_lock = threading.Lock()

_worker_threads: Dict[int, threading.Thread] = {}


# ---------------- ops loading ----------------
# If you have ops_loader like your other agents, use it. Otherwise fall back to echo-only.

OPS: Dict[str, Any] = {}

def op_echo(payload: Any) -> Any:
    return {"ok": True, "echo": payload}

try:
    from ops_loader import load_ops  # type: ignore
    OPS = load_ops(TASKS)
except Exception:
    OPS = {"echo": op_echo}


# ---------------- helpers ----------------

def log(msg: str) -> None:
    print(msg, flush=True)

def _collect_metrics() -> Dict[str, Any]:
    if psutil is None:
        return {}
    try:
        return {
            "cpu_util": float(psutil.cpu_percent(interval=None)) / 100.0,
            "ram_mb": float(psutil.virtual_memory().used) / (1024 * 1024),
        }
    except Exception:
        return {}

def _usable_cores() -> int:
    if psutil is None:
        return 1
    try:
        n = psutil.cpu_count(logical=True) or 1
    except Exception:
        n = 1
    return max(1, n - max(0, RESERVED_CORES))

def _default_max_workers() -> int:
    if CPU_MAX_WORKERS and CPU_MAX_WORKERS > 0:
        return max(CPU_MIN_WORKERS, CPU_MAX_WORKERS)
    return max(CPU_MIN_WORKERS, _usable_cores())

def _post_json(path: str, payload: Dict[str, Any]) -> Tuple[int, Any]:
    url = f"{CONTROLLER_URL}{path}"
    try:
        r = _session.post(url, json=payload, timeout=HTTP_TIMEOUT_SEC)
    except Exception as e:
        return 0, {"error": str(e), "url": url}

    if r.status_code == 204:
        return 204, None

    try:
        body = r.json()
    except Exception:
        body = r.text

    return r.status_code, body

def _extract_task(task: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    job_id = task.get("id") or task.get("job_id")
    op = task.get("op")
    payload = task.get("payload") or {}

    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError(f"task missing job id: {task!r}")
    if not isinstance(op, str) or not op:
        raise RuntimeError(f"task missing op: {task!r}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"task payload not dict: {task!r}")

    return job_id, op, payload


# ---------------- v1 leasing/results ----------------

def lease_once(max_tasks: int) -> Optional[Tuple[str, Dict[str, Any]]]:
    payload: Dict[str, Any] = {
        "agent": AGENT_NAME,
        "capabilities": {"ops": TASKS},
        "max_tasks": max_tasks,
        "timeout_ms": LEASE_TIMEOUT_MS,
        "labels": AGENT_LABELS,
        "worker_profile": {
            "tier": "cpu",
            "cpu": {"usable_cores": _usable_cores(), "min_cpu_workers": CPU_MIN_WORKERS, "max_cpu_workers": _default_max_workers()},
            "workers": {"max_total_workers": _default_max_workers()},
        },
        "metrics": _collect_metrics(),
    }

    code, body = _post_json("/v1/leases", payload)
    if code == 204:
        return None
    if code == 0:
        raise RuntimeError(f"lease failed: {body}")
    if code >= 400:
        raise RuntimeError(f"lease HTTP {code}: {body}")

    if not isinstance(body, dict):
        raise RuntimeError(f"lease body not dict: {body!r}")

    lease_id = body.get("lease_id")
    tasks = body.get("tasks")

    if not isinstance(lease_id, str) or not lease_id:
        raise RuntimeError(f"lease missing lease_id: {body!r}")
    if not isinstance(tasks, list) or not tasks:
        return None

    task = tasks[0]
    if not isinstance(task, dict):
        raise RuntimeError(f"task not dict: {task!r}")

    return lease_id, task


def post_result(lease_id: str, job_id: str, ok: bool, result: Any = None, error: Any = None) -> None:
    payload: Dict[str, Any] = {
        "lease_id": lease_id,
        "job_id": job_id,
        "status": "succeeded" if ok else "failed",
        "result": result if ok else None,
        "error": None if ok else error,
    }
    code, body = _post_json("/v1/results", payload)
    if code == 0:
        raise RuntimeError(f"result failed: {body}")
    if code >= 400:
        raise RuntimeError(f"result HTTP {code}: {body}")


# ---------------- execution ----------------

CPU_POOL = ProcessPoolExecutor(max_workers=_default_max_workers())

def _run_op(op: str, payload: Dict[str, Any]) -> Any:
    fn = OPS.get(op)
    if fn is None:
        raise RuntimeError(f"unknown op: {op}")
    return fn(payload)

def execute_one(lease_id: str, task: Dict[str, Any]) -> None:
    global _inflight
    job_id, op, payload = _extract_task(task)

    t0 = time.time()
    with _lock:
        _inflight += 1

    try:
        future = CPU_POOL.submit(_run_op, op, payload)
        out = future.result(timeout=TASK_EXEC_TIMEOUT_SEC)
        dt = (time.time() - t0) * 1000.0
        post_result(lease_id, job_id, True, result={"out": out, "ms": dt, "op": op})
    except FuturesTimeoutError:
        dt = (time.time() - t0) * 1000.0
        post_result(lease_id, job_id, False, error={"error": "timeout", "ms": dt, "op": op})
    except Exception as e:
        dt = (time.time() - t0) * 1000.0
        post_result(
            lease_id,
            job_id,
            False,
            error={"type": type(e).__name__, "message": str(e), "ms": dt, "op": op, "trace": traceback.format_exc(limit=10)},
        )
    finally:
        with _lock:
            _inflight = max(0, _inflight - 1)


def worker_loop(wid: int) -> None:
    global _hits, _misses
    log(f"[agent-linux-v1] worker-{wid} start")
    while not stop_event.is_set():
        try:
            leased = lease_once(max_tasks=1)
        except Exception as e:
            log(f"[agent-linux-v1] lease error: {e}")
            stop_event.wait(1.0)
            continue

        if not leased:
            with _lock:
                _misses += 1
            stop_event.wait(IDLE_SLEEP_SEC * (0.5 + random.random()))
            continue

        lease_id, task = leased
        with _lock:
            _hits += 1
        try:
            execute_one(lease_id, task)
        except Exception as e:
            log(f"[agent-linux-v1] execute error: {e}")

    log(f"[agent-linux-v1] worker-{wid} stop")


def set_workers(n: int) -> None:
    n = max(CPU_MIN_WORKERS, int(n))
    for wid in range(1, n + 1):
        if wid in _worker_threads:
            continue
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        _worker_threads[wid] = t
        t.start()


def shutdown(signum: int, _frame: Any) -> None:
    log(f"[agent-linux-v1] shutdown signal {signum}")
    stop_event.set()


def main() -> int:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if not TASKS:
        log("[agent-linux-v1] TASKS empty; exiting")
        return 2

    n = _default_max_workers()
    log(f"[agent-linux-v1] starting name={AGENT_NAME} controller={CONTROLLER_URL} ops={TASKS} workers={n}")
    set_workers(n)

    while not stop_event.is_set():
        stop_event.wait(0.5)

    try:
        CPU_POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
