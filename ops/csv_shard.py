# ops/csv_shard.py
import csv
import os
from typing import Any, Dict, List, Optional


def _read_csv_shard(source_uri: str, start_row: int, shard_size: int) -> List[Dict[str, Any]]:
    """
    Read a slice of rows from a CSV after the header.
    start_row = 0 means first data row.
    """
    rows: List[Dict[str, Any]] = []
    stop_row = start_row + shard_size

    with open(source_uri, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader):
            if idx < start_row:
                continue
            if idx >= stop_row:
                break
            rows.append(row)

    return rows


def op_read_csv_shard(task_or_payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generic CSV shard op.

    Accepts either:
      A) payload dict directly
      B) full task dict containing "payload": { ... }

    Payload fields:
      - dataset_id: str (optional)
      - source_uri: str (required)
      - start_row: int (optional, default 0)
      - shard_size: int (optional, default 100)
      - mode: "rows" | "count" (optional, default "rows")
    """
    if task_or_payload is None:
        return {"ok": False, "error": "read_csv_shard: missing payload"}

    if not isinstance(task_or_payload, dict):
        return {"ok": False, "error": "read_csv_shard: payload must be a dict"}

    payload = task_or_payload.get("payload") if "payload" in task_or_payload else task_or_payload
    if payload is None or not isinstance(payload, dict):
        return {"ok": False, "error": "read_csv_shard: payload must be a dict"}

    dataset_id = payload.get("dataset_id", "unknown_dataset")
    source_uri = payload.get("source_uri")
    if not source_uri or not isinstance(source_uri, str):
        return {"ok": False, "error": "read_csv_shard: payload.source_uri (string) is required"}

    try:
        start_row = int(payload.get("start_row", 0))
        shard_size = int(payload.get("shard_size", 100))
    except Exception:
        return {"ok": False, "error": "read_csv_shard: start_row and shard_size must be integers"}

    if start_row < 0:
        return {"ok": False, "error": "read_csv_shard: start_row must be >= 0"}
    if shard_size <= 0:
        return {"ok": False, "error": "read_csv_shard: shard_size must be > 0"}

    mode = payload.get("mode", "rows")
    if mode not in ("rows", "count"):
        return {"ok": False, "error": "read_csv_shard: mode must be 'rows' or 'count'"}

    if not os.path.exists(source_uri):
        return {"ok": False, "error": f"read_csv_shard: file not found: {source_uri}"}

    try:
        rows = _read_csv_shard(source_uri, start_row, shard_size)
    except Exception as e:
        return {"ok": False, "error": f"read_csv_shard: failed reading csv: {type(e).__name__}: {e}"}

    end_row = start_row + len(rows)

    if mode == "count":
        return {
            "ok": True,
            "dataset_id": dataset_id,
            "mode": "count",
            "start_row": start_row,
            "end_row": end_row,
            "row_count": len(rows),
        }

    return {
        "ok": True,
        "dataset_id": dataset_id,
        "mode": "rows",
        "start_row": start_row,
        "end_row": end_row,
        "row_count": len(rows),
        "rows": rows,
    }
