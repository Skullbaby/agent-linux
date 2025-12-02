import os
import time
import socket
import signal
import threading
from typing import Optional, List, Dict, Any

import requests

# Optional metrics
try:
    import psutil
except ImportError:
    psutil = None

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from worker_sizing import build_worker_profile

# ---------------- config ----------------

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")
AGENT_NAME = os.getenv("AGENT_NAME", socket.gethostname())
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
TASK_WAIT_MS = int(os.getenv("TASK_WAIT_MS", "2000"))
HTTP_TIMEOUT_SEC = float(os.getenv("HTTP_TIMEOUT_SEC", "6"))
AGENT_LABELS_RAW = os.getenv("AGENT_LABELS", "")

MODEL_NAME = os.getenv(
    "CLASSIFY_MODEL",
    "assemblyai/distilbert-base-uncased-sst2",
)

_running = True

# ---------------- Local metrics tracking ----------------

_metrics_lock = threading.Lock()
_tasks_completed = 0
_tasks_failed = 0
_task_durations: List[float] = []
_max_duration_samples = 100  # Rolling window size

# ---------------- worker profile / labels ----------------

WORKER_PROFILE = build_worker_profile()

BASE_LABELS: Dict[str, Any] = {}

# Parse AGENT_LABELS="key=value,key2=value2"
if AGENT_LABELS_RAW.strip():
    for item in AGENT_LABELS_RAW.split(","):
        if not item.strip():
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            BASE_LABELS[k.strip()] = v.strip()
        else:
            BASE_LABELS[item.strip()] = True

# Always include worker_profile in labels for the controller
BASE_LABELS["worker_profile"] = WORKER_PROFILE

CAPABILITIES: Dict[str, Any] = {
    "ops": ["map_tokenize", "map_classify"]
}

# ---------------- model / op registry ----------------
# Agent-lite: CPU only, no GPU detection

_model_lock = threading.Lock()
_classifier_tokenizer: Optional[AutoTokenizer] = None
_classifier_model: Optional[AutoModelForSequenceClassification] = None
_classifier_device = "cpu"  # Agent-lite: CPU only


def _load_classifier_if_needed():
    global _classifier_tokenizer, _classifier_model
    with _model_lock:
        if _classifier_tokenizer is not None and _classifier_model is not None:
            return

        print(f"[agent] loading classifier model: {MODEL_NAME} on {_classifier_device}")
        _classifier_tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        _classifier_model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME)
        _classifier_model.to(_classifier_device)
        _classifier_model.eval()


def op_map_tokenize(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Very simple "tokenization" op.

    Payload:
      { "text": "some text" }

    Result:
      { "ok": true, "tokens": [...], "length": int }
    """
    text = str(payload.get("text", ""))
    tokens = text.split()
    return {
        "ok": True,
        "tokens": tokens,
        "length": len(tokens),
    }


def op_map_classify(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sentiment classification using HF model.

    Payload:
      { "text": "some text" }

    Result includes:
      - label
      - score
      - raw logits (optional)
    """
    text = str(payload.get("text", ""))

    if not text.strip():
        return {
            "ok": True,
            "label": "NEUTRAL",
            "score": 0.0,
            "detail": "empty text",
        }

    _load_classifier_if_needed()
    assert _classifier_tokenizer is not None
    assert _classifier_model is not None

    inputs = _classifier_tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=256,
    ).to(_classifier_device)

    with torch.no_grad():
        outputs = _classifier_model(**inputs)
        logits = outputs.logits[0]
        probs = torch.softmax(logits, dim=-1)
        score, idx = torch.max(probs, dim=-1)
        label = _classifier_model.config.id2label[int(idx)]

    return {
        "ok": True,
        "label": label,
        "score": float(score),
        "logits": [float(x) for x in logits.tolist()],
    }


OPS = {
    "map_tokenize": op_map_tokenize,
    "map_classify": op_map_classify,
}

# ---------------- metrics helpers ----------------


def _record_task_result(duration_ms: float, ok: bool) -> None:
    """
    Record the result of a task execution for local metrics tracking.
    Thread-safe.
    """
    global _tasks_completed, _tasks_failed, _task_durations

    with _metrics_lock:
        if ok:
            _tasks_completed += 1
        else:
            _tasks_failed += 1

        _task_durations.append(duration_ms)
        # Keep only the last N samples for rolling average
        if len(_task_durations) > _max_duration_samples:
            _task_durations.pop(0)


def _collect_metrics() -> Dict[str, Any]:
    """
    Collect lightweight agent metrics for autonomic decisions.
    Includes both system metrics (CPU, RAM) and task performance metrics.
    """
    metrics: Dict[str, Any] = {}

    # System metrics via psutil
    if psutil is not None:
        try:
            metrics["cpu_util"] = psutil.cpu_percent(interval=0.0) / 100.0
        except Exception:
            pass

        try:
            vm = psutil.virtual_memory()
            metrics["ram_mb"] = int(vm.used / (1024 * 1024))
        except Exception:
            pass

    # Task performance metrics
    with _metrics_lock:
        metrics["tasks_completed"] = _tasks_completed
        metrics["tasks_failed"] = _tasks_failed

        if _task_durations:
            avg_ms = sum(_task_durations) / len(_task_durations)
            metrics["avg_task_ms"] = avg_ms

    return metrics


# ---------------- HTTP helpers ----------------


def _post_json(path: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"{CONTROLLER_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=HTTP_TIMEOUT_SEC)
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None
    except Exception as e:
        print(f"[agent] POST {url} failed: {e}")
        return None


def _get_json(path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = f"{CONTROLLER_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT_SEC)
        if resp.status_code == 204:
            return None
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None
    except Exception as e:
        print(f"[agent] GET {url} failed: {e}")
        return None


# ---------------- register / heartbeat ----------------


def register_agent() -> None:
    payload: Dict[str, Any] = {
        "agent": AGENT_NAME,
        "labels": BASE_LABELS,
        "capabilities": CAPABILITIES,
        "worker_profile": WORKER_PROFILE,
    }
    # send metrics as a dedicated sub-dict
    payload["metrics"] = _collect_metrics()
    print(f"[agent] registering with controller as {AGENT_NAME}")
    _post_json("/agents/register", payload)


def heartbeat_loop() -> None:
    while _running:
        payload: Dict[str, Any] = {
            "agent": AGENT_NAME,
            "labels": BASE_LABELS,
            "capabilities": CAPABILITIES,
            "worker_profile": WORKER_PROFILE,
        }
        # send metrics as a dedicated sub-dict
        payload["metrics"] = _collect_metrics()
        _post_json("/agents/heartbeat", payload)
        time.sleep(HEARTBEAT_SEC)


# ---------------- task execution ----------------


def _execute_op(op: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fn = OPS.get(op)
    if fn is None:
        return {
            "ok": False,
            "error": f"Unknown op '{op}'",
        }

    try:
        return fn(payload)
    except Exception as e:
        return {
            "ok": False,
            "error": f"Exception in op '{op}': {e}",
        }


def worker_loop() -> None:
    global _running
    print(f"[agent] worker loop starting for {AGENT_NAME}")
    while _running:
        # Ask for a task
        task = _get_json("/task", {"agent": AGENT_NAME, "wait_ms": TASK_WAIT_MS})
        if not task:
            # No task right now
            continue

        job_id = task.get("id")
        op = task.get("op")
        payload = task.get("payload") or {}

        start_ts = time.time()
        result_data = _execute_op(op, payload)
        duration_ms = (time.time() - start_ts) * 1000.0

        ok = bool(result_data.get("ok", True))
        error_str = result_data.get("error")

        # Record metrics locally
        _record_task_result(duration_ms, ok)

        result_payload: Dict[str, Any] = {
            "id": job_id,
            "agent": AGENT_NAME,
            "op": op,
            "ok": ok,
            "result": result_data if ok else None,
            "error": error_str if not ok else None,
            "duration_ms": duration_ms,
        }

        _post_json("/result", result_payload)


# ---------------- signal handling ----------------


def _stop(*_args, **_kwargs):
    global _running
    print("[agent] stop signal received, shutting down...")
    _running = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


# ---------------- main ----------------


def main():
    register_agent()

    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    worker_loop()


if __name__ == "__main__":
    main()
