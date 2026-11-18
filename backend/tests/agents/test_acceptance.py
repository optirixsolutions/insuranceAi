"""Phase 6 acceptance gates (docs/architecture/03-agent-contracts.md §7) on the deterministic cores.

Each evaluation set runs real documents through the real pipeline; see ``evaluations/``. The
document set runs its 13 text-layer PDF variants here and all 60 variants (Tesseract OCR) inside
the backend container via ``python -m evaluations.run``.
"""

from __future__ import annotations

import pytest

from evaluations import (
    assessment_set,
    decision_set,
    document_set,
    fraud_set,
    injection_set,
    policy_set,
    validation_set,
)
from evaluations.harness import EvalResult

SETS = {
    "document": (document_set.evaluate, 13),
    "policy": (policy_set.evaluate, 40),
    "fraud": (fraud_set.evaluate, 50),
    "assessment": (assessment_set.evaluate, 80),
    "validation": (validation_set.evaluate, 40),
    "decision": (decision_set.evaluate, 60),
    "injection": (injection_set.evaluate, 25),
}


@pytest.mark.parametrize("name", list(SETS))
async def test_acceptance_gate(name: str) -> None:
    evaluate, minimum = SETS[name]
    result: EvalResult = await evaluate()
    assert result.cases >= minimum, f"{name}: only {result.cases} cases"
    failed = [m.as_dict() for m in result.metrics if not m.passed]
    assert not failed, f"{name} gate failed: {failed}; first failures: {result.failures[:5]}"
