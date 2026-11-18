"""Policy version selection and validity on the incident date. Pure functions with traces."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True, slots=True)
class VersionTerms:
    id: str
    version_no: int
    status: str  # draft | published | superseded
    effective_from: date
    effective_to: date
    waiting_period_days: int


def select_version(versions: list[VersionTerms], incident_date: date) -> tuple[VersionTerms | None, str]:
    """The published (or superseded-but-then-current) version in force on the incident date.

    Drafts never count. If several versions cover the date (an endorsement re-issued the
    schedule), the highest version number wins: it is the latest statement of the terms.
    """
    issued = [v for v in versions if v.status in ("published", "superseded")]
    covering = [v for v in issued if v.effective_from <= incident_date <= v.effective_to]
    if covering:
        chosen = max(covering, key=lambda v: v.version_no)
        others = len(covering) - 1
        reason = (
            f"Version {chosen.version_no} is in force on {incident_date.isoformat()} "
            f"({chosen.effective_from.isoformat()} to {chosen.effective_to.isoformat()})"
        )
        if others:
            reason += f"; it supersedes {others} earlier version(s) covering the same date"
        return chosen, reason
    if not issued:
        return None, "The policy has no published version"
    before = [v for v in issued if v.effective_to < incident_date]
    after = [v for v in issued if v.effective_from > incident_date]
    if before and not after:
        last = max(before, key=lambda v: v.effective_to)
        return None, f"Cover ended on {last.effective_to.isoformat()}, before the incident"
    if after and not before:
        first = min(after, key=lambda v: v.effective_from)
        return None, f"Cover starts on {first.effective_from.isoformat()}, after the incident"
    return None, f"No version covers {incident_date.isoformat()} (gap between periods of cover)"


def is_active(policy_status: str, version: VersionTerms | None, on: date) -> bool:
    """Active only with a version in force; lapsed/cancelled are checked by the version end date,
    which the administration system moves when cover stops, so the policy status alone is not proof."""
    if version is None:
        return False
    if policy_status == "cancelled":
        return False
    return version.effective_from <= on <= version.effective_to


def inception(versions: list[VersionTerms]) -> date | None:
    issued = [v.effective_from for v in versions if v.status in ("published", "superseded")]
    return min(issued) if issued else None


def waiting_period_ok(version: VersionTerms, versions: list[VersionTerms], on: date) -> bool:
    """Waiting periods run from first inception (continuous cover), not from each renewal."""
    if version.waiting_period_days <= 0:
        return True
    start = inception(versions) or version.effective_from
    return on >= start + timedelta(days=version.waiting_period_days)
