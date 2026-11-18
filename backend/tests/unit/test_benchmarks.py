"""Benchmark harness: percentile definition (same as PostgreSQL percentile_cont), aggregation, profiles."""

from __future__ import annotations

from typing import Any

import pytest

from benchmarks.api_load import label
from benchmarks.claims_bench import aggregate, documents_for
from benchmarks.stats import meets, percentile, summary


def test_percentile_matches_percentile_cont() -> None:
    # SELECT percentile_cont(f) WITHIN GROUP (ORDER BY v) FROM unnest('{10,20,30,40}'::float8[]) v
    values = [40.0, 10.0, 30.0, 20.0]
    assert percentile(values, 0.5) == 25.0
    assert percentile(values, 0.9) == pytest.approx(37.0)
    assert percentile(values, 0.95) == pytest.approx(38.5)
    assert percentile(values, 0.0) == 10.0 and percentile(values, 1.0) == 40.0
    assert percentile([7.0], 0.9) == 7.0
    assert percentile([], 0.5) is None
    with pytest.raises(ValueError):
        percentile(values, 1.5)


def test_summary_and_target_check() -> None:
    s = summary([1.0, 2.0, 3.0, 4.0, 5.0])
    assert s["n"] == 5 and s["p50"] == 3.0 and s["min"] == 1.0 and s["max"] == 5.0 and s["mean"] == 3.0
    assert summary([])["p95"] is None
    assert meets([100.0, 150.0, 200.0], 0.5, 180.0) is True
    assert meets([100.0, 250.0, 260.0], 0.5, 180.0) is False
    assert meets([], 0.5, 180.0) is None


def _record(index: int, process: float) -> dict[str, Any]:
    return {
        "index": index,
        "ok": True,
        "process_s": process,
        "documents_s": 10.0,
        "end_to_end_s": process + 10,
        "upload_s": 0.5,
        "sse_lag_ms": [20.0, 40.0],
        "node_s": {"INTAKE": 0.2, "DOCUMENT_PROCESSING": 1.0},
        "stop_event": "interrupt",
        "llm_calls": 0,
        "agents": [{"cost_usd": 0.0}],
    }


def test_aggregate_excludes_warmup_and_counts_failures() -> None:
    records = [
        _record(0, 999.0),  # warm-up: excluded
        _record(1, 100.0),
        _record(2, 200.0),
        {"index": 3, "ok": False, "error": "TimeoutError"},
    ]
    out = aggregate(records, warmup=1)
    assert out["claims"] == 3 and out["succeeded"] == 2
    assert out["failure_rate"] == pytest.approx(1 / 3)
    assert out["process_s"]["p50"] == 150.0 and out["process_s"]["max"] == 200.0
    assert out["nodes_s"]["INTAKE"]["n"] == 2 and out["sse_lag_ms"]["n"] == 4
    assert out["stop_events"] == {"interrupt": 2}
    assert out["target"] == {"p50_le_180s": True, "p90_le_240s": True}


def test_profiles_match_the_reference_workload() -> None:
    simple = documents_for("simple", seed=1)
    assert len(simple) == 4  # <= 5 documents
    born_digital = [f for _, f in simple if f[2] == "application/pdf"]
    assert len(born_digital) / len(simple) >= 0.5
    assert {f[2] for _, f in documents_for("poor", seed=1)} >= {"image/jpeg", "image/png"}
    with pytest.raises(ValueError):
        documents_for("huge", seed=1)


def test_endpoint_labels_hide_ids() -> None:
    assert label("/claims/0b6f9c1e-0000-4000-8000-000000000001/documents") == "/claims/{id}/documents"
    assert label("/claims?page_size=25") == "/claims"
