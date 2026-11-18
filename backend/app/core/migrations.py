"""Read the expected Alembic head so readiness can detect un-migrated databases."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


def expected_heads() -> frozenset[str]:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return frozenset(ScriptDirectory.from_config(config).get_heads())
