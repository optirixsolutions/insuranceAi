"""Restore-drill check, run inside the API container by scripts/restore_drill.sh (reads stdin as a script).

Re-verifies every organisation's audit hash chain in the restored scratch database with the application's
own verification code. Prints one line per organisation; exits 1 if any chain is broken. Read-only.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.tenancy import bind_tenant
from app.services.audit_service import verify_chain


async def main() -> int:
    settings = get_settings()
    source = settings.migration_database_url or settings.database_url
    url = make_url(source.get_secret_value()).set(database=os.environ["RESTORE_DRILL_DB"])
    engine = create_async_engine(url)
    broken = 0
    try:
        async with AsyncSession(engine) as s:
            orgs = (await s.execute(text("SELECT id, slug FROM organizations ORDER BY created_at"))).all()
        events = 0
        for org_id, slug in orgs:
            async with AsyncSession(engine) as s:
                await bind_tenant(s, org_id)
                result = await verify_chain(s)
            events += result.events_checked
            if not result.valid:
                broken += 1
                print(f"audit chain BROKEN  {slug}: seq {result.first_invalid_seq} ({result.reason})")
        print(f"audit chains: {len(orgs) - broken}/{len(orgs)} organisations valid, {events} events re-hashed")
    finally:
        await engine.dispose()
    return 1 if broken else 0


sys.exit(asyncio.run(main()))
