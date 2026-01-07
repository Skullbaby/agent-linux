# app.py
# MYZEL CPU Agent (dynamic workers) — controller-aligned
#
# Controller contract:
#   - Lease task:  GET /api/task?agent=NAME&wait_ms=MS   (also /task)
#   - Register:    POST /api/agents/register            (also /agents/register)
#   - Heartbeat:   POST /api/agents/heartbeat           (also /agents/heartbeat)
#   - Result:      POST /api/result                     (also /result)
#
# Dynamic worker design:
#   - Start with 1 worker loop
#   - Grow worker count while there are bubbles (tasks available) and CPU headroom exists
#   - Shrink when idle / CPU is saturated
#
# CPU execution:
#   - ProcessPoolExecutor for CPU-bound ops (bypasses GIL)
#   - I/O-light ops can still run inline if they’re cheap, but default is via CPU pool
#
# Notes:
#   - This file intentionally does NOT include any “battery power” behavior.
#   - Designed to run cleanly on Linux + “forever stack” style service/runtime.

import os
import sys
import time
import json
import socket
import signal
import random
import threading
from typing import Any, Dict, Optional
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturesTimeoutError

import requests

try:
    import psutil
except Exception:
    psutil = None

from ops_loader import load_ops
from worker_sizing import build_worker_profile


# ---------------- config ----------------

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080").rstrip("/")
API_PREFIX_RAW = os.getenv("API_PREFIX", "/api").strip()
AGENT_NAME = os.getenv("AGENT_NAME") or socket.gethostname()

TASKS_RAW = os.getenv("TASKS", "echo")
TASKS = [t.strip() for t in TASKS_RAW.split(",") if t.strip()]

# Leave some cores for OS / background services.
RESERVED_CORES = int(os.getenv("RESERVED_CORES", "4"))

HEARTBEAT_SEC = float(os.getenv("HEARTBEAT_SEC", "3"))
WAIT_MS = int(os.getenv("WAIT_MS", "2000"))
LEASE_IDLE_SEC = float(os.getenv("LEASE_IDLE_SEC", "0.05"))

HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "6"))

# dynamic tuning
CPU_MIN_WORKERS = max(1, int(os.getenv("CPU_MIN_WORKERS", "1")))
CPU_PIPELINE_FACTOR = float(os.getenv("CPU_PIPELINE_FACTOR", "1.25"))  # inflight vs workers
TARGET_CPU_UTIL_PCT = float(os.getenv("TARGET_CPU_UTIL_PCT", "75"))
SCALE_TICK_SEC = float(os.getenv("SCALE_TICK_SEC", "2.0"))

# worker execution guardrails
TASK_EXEC_TIMEOUT_SEC = float(os.getenv("TASK_EXEC_TIMEOUT_SEC", "60"))

# labels
AGENT_LABELS_RAW = os.getenv("AGENT_LABELS", "")
AGENT_LABELS: Dict[str, Any] = {}
if AGENT_LABELS_RAW.strip():
    try:
        AGENT_LABELS = json.loads(AGENT_LABELS_RAW)
    except Exception:
        # allow simple k=v,k2=v2
        for part in AGENT_LABELS_RAW.split(","):
            part = part.strip()
            if not part or "=" not in part:
                continue
            k, v = part.split("=", 1)
            AGENT_LABELS[k.strip()] = v.strip()


# ---------------- logging ----------------

_LOG_LOCK = threading.Lock()
_last_log: Dict[str, float] = {}


def log(msg: str, key: str = "default", every: float = 1.0) -> None:
    now = time.time()
    with _LOG_LOCK:
        last = _last_log.get(key, 0.0)
        if now - last >= every:
            _last_log[key] = now
            print(msg, flush=True)


# ---------------- runtime state ----------------

stop_event = threading.Event()
OPS = load_ops(TASKS)

WORKER_PROFILE = build_worker_profile()
CPU_PROFILE = WORKER_PROFILE.get("cpu", {})
USABLE_CORES = int(CPU_PROFILE.get("usable_cores", 1))

# ---------------- CPU execution pool ----------------

# Use processes for true CPU parallelism (bypasses GIL)
_CPU_WORKERS = max(1, USABLE_CORES)
_CPU_POOL = ProcessPoolExecutor(max_workers=_CPU_WORKERS)

# Scaling state
_current_workers_lock = threading.Lock()
_current_workers = 1

# Inflight tracking (best-effort)
_hits = 0
_misses = 0
_inflight = 0
_worker_lock = threading.Lock()

_session = requests.Session()

# Determine API prefix (try /api then fallback)
API_PREFIX = API_PREFIX_RAW if API_PREFIX_RAW.startswith("/") else f"/{API_PREFIX_RAW}"


def _url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return f"{CONTROLLER_URL}{path}"


def _api(path: str) -> str:
    # path should start with /...
    if not path.startswith("/"):
        path = "/" + path
    return _url(f"{API_PREFIX}{path}")


def _probe_prefix() -> None:
    global API_PREFIX
    # Try /api first, then no prefix
    candidates = [API_PREFIX, ""]
    for pref in candidates:
        try_url = f"{CONTROLLER_URL}{pref}/healthz" if pref else f"{CONTROLLER_URL}/healthz"
        try:
            r = _session.get(try_url, timeout=HTTP_TIMEOUT)
            if r.status_code < 500:
                API_PREFIX = pref
                log(f"[agent] API prefix set to: '{API_PREFIX or '(none)'}'", "prefix", every=0.0)
                return
        except Exception:
            continue
    # If nothing worked, keep current and let registration attempts show the error.
    log("[agent] WARNING: could not probe API prefix; using configured API_PREFIX.", "prefix_warn", every=0.0)


def _post_json(url: str, payload: Dict[str, Any]) -> requests.Response:
    return _session.post(url, json=payload, timeout=HTTP_TIMEOUT)


def _get_json(url: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    r = _session.get(url, params=params, timeout=HTTP_TIMEOUT)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def register() -> None:
    payload = {
        "agent": AGENT_NAME,
        "tasks": TASKS,
        "worker_profile": WORKER_PROFILE,
        "labels": AGENT_LABELS,
        "ts": time.time(),
    }
    # /agents/register (or /api/agents/register)
    url = _api("/agents/register") if API_PREFIX else _url("/agents/register")
    r = _post_json(url, payload)
    r.raise_for_status()
    log(f"[agent] registered as {AGENT_NAME} tasks={TASKS}", "register", every=0.0)


def heartbeat_loop() -> None:
    url = _api("/agents/heartbeat") if API_PREFIX else _url("/agents/heartbeat")
    while not stop_event.is_set():
        try:
            payload = {"agent": AGENT_NAME, "ts": time.time()}
            _post_json(url, payload)
        except Exception as e:
            log(f"[agent] heartbeat error: {e}", "hb_err", every=3.0)
        stop_event.wait(HEARTBEAT_SEC)


def lease_task() -> Optional[Dict[str, Any]]:
    # /task?agent=...&wait_ms=...
    url = _api("/task") if API_PREFIX else _url("/task")
    params = {"agent": AGENT_NAME, "wait_ms": WAIT_MS}
    try:
        task = _get_json(url, params)
        return task
    except requests.HTTPError as e:
        log(f"[agent] lease HTTP error: {e}", "lease_http", every=2.0)
    except Exception as e:
        log(f"[agent] lease error: {e}", "lease_err", every=2.0)
    return None


def post_result(job_id: str, ok: bool, result: Any = None, error: str = "", meta: Optional[Dict[str, Any]] = None) -> None:
    url = _api("/result") if API_PREFIX else _url("/result")
    payload: Dict[str, Any] = {
        "agent": AGENT_NAME,
        "job_id": job_id,
        "ok": ok,
        "result": result,
        "error": error,
        "ts": time.time(),
    }
    if meta:
        payload["meta"] = meta
    try:
        r = _post_json(url, payload)
        r.raise_for_status()
    except Exception as e:
        log(f"[agent] post_result error job_id={job_id}: {e}", "post_err", every=2.0)


def _run_op(op_name: str, payload: Any) -> Any:
    fn = OPS.get(op_name)
    if not fn:
        raise RuntimeError(f"unknown op: {op_name}")
    return fn(payload)


def execute_task(task: Dict[str, Any]) -> None:
    global _inflight
    job_id = str(task.get("job_id") or task.get("id") or "")
    op = str(task.get("op") or "")
    payload = task.get("payload")

    if not job_id:
        log("[agent] malformed task missing job_id", "malformed", every=1.0)
        return
    if not op:
        post_result(job_id, False, result=None, error="malformed task: missing op")
        return

    t0 = time.time()
    with _worker_lock:
        _inflight += 1

    try:
        # Default: run in CPU pool (safe for CPU bound).
        future = _CPU_POOL.submit(_run_op, op, payload)
        out = future.result(timeout=TASK_EXEC_TIMEOUT_SEC)
        dt = (time.time() - t0) * 1000.0
        post_result(job_id, True, result=out, error="", meta={"op": op, "ms": dt})
    except FuturesTimeoutError:
        dt = (time.time() - t0) * 1000.0
        post_result(job_id, False, result=None, error=f"timeout after {TASK_EXEC_TIMEOUT_SEC}s", meta={"op": op, "ms": dt})
    except Exception as e:
        dt = (time.time() - t0) * 1000.0
        post_result(job_id, False, result=None, error=str(e), meta={"op": op, "ms": dt})
    finally:
        with _worker_lock:
            _inflight = max(0, _inflight - 1)


def worker_loop(worker_id: int) -> None:
    global _hits, _misses
    log(f"[agent] worker-{worker_id} start", f"wstart{worker_id}", every=0.0)

    while not stop_event.is_set():
        task = lease_task()
        if task:
            _hits += 1
            execute_task(task)
            continue

        _misses += 1
        # Idle wait with a touch of jitter to avoid herd behavior.
        idle = LEASE_IDLE_SEC * (0.5 + random.random())
        stop_event.wait(idle)

    log(f"[agent] worker-{worker_id} stop", f"wstop{worker_id}", every=0.0)


def _cpu_util() -> float:
    if psutil is None:
        return 0.0
    try:
        return float(psutil.cpu_percent(interval=None))
    except Exception:
        return 0.0


def scale_loop() -> None:
    global _current_workers

    while not stop_event.is_set():
        cpu = _cpu_util()
        with _worker_lock:
            inflight = _inflight
        # Target inflight workers based on usable cores and pipeline factor
        desired = max(CPU_MIN_WORKERS, int(max(1, USABLE_CORES) * CPU_PIPELINE_FACTOR))
        # If CPU is too hot, reduce pressure
        if cpu >= TARGET_CPU_UTIL_PCT:
            desired = max(CPU_MIN_WORKERS, min(desired, max(1, USABLE_CORES)))

        # If we're missing a lot, reduce (idle)
        misses = _misses
        hits = _hits

        with _current_workers_lock:
            current = _current_workers

        # Simple heuristics:
        # - If we’re getting tasks (hits) and CPU has headroom, grow up to desired
        # - If idle (misses >> hits), shrink
        grow = hits > 0 and cpu < TARGET_CPU_UTIL_PCT
        idle = misses > max(25, hits * 5)

        target = current
        if grow and current < desired:
            target = min(desired, current + 1)
        elif idle and current > CPU_MIN_WORKERS:
            target = max(CPU_MIN_WORKERS, current - 1)

        if target != current:
            log(f"[agent] scale workers {current} -> {target} (cpu={cpu:.1f} inflight={inflight} hits={hits} misses={misses})",
                "scale", every=0.0)
            set_worker_count(target)

        stop_event.wait(SCALE_TICK_SEC)


_worker_threads: Dict[int, threading.Thread] = {}
_worker_stop_flags: Dict[int, threading.Event] = {}


def set_worker_count(n: int) -> None:
    global _current_workers

    n = max(CPU_MIN_WORKERS, int(n))

    with _current_workers_lock:
        current = _current_workers
        _current_workers = n

    # Start new workers
    for wid in range(current + 1, n + 1):
        t = threading.Thread(target=worker_loop, args=(wid,), daemon=True)
        _worker_threads[wid] = t
        t.start()

    # Note: We do not hard-kill threads; they exit via stop_event.
    # Shrinking just means we stop spawning additional workers and let current idle.
    # (If you want “true” shrink, do per-thread stop flags; for now we keep it simple.)


def shutdown(signum: int, frame: Any) -> None:
    log(f"[agent] shutdown signal {signum}", "shutdown", every=0.0)
    stop_event.set()


def main() -> int:
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    _probe_prefix()

    # Register (retry loop)
    while not stop_event.is_set():
        try:
            register()
            break
        except Exception as e:
            log(f"[agent] register error: {e}", "reg_err", every=2.0)
            stop_event.wait(2.0)

    if stop_event.is_set():
        return 1

    # Heartbeat
    hb = threading.Thread(target=heartbeat_loop, daemon=True)
    hb.start()

    # Start initial workers
    set_worker_count(1)

    # Scaling manager
    scaler = threading.Thread(target=scale_loop, daemon=True)
    scaler.start()

    # Keep main alive
    while not stop_event.is_set():
        stop_event.wait(0.5)

    # Shutdown pool
    try:
        _CPU_POOL.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
