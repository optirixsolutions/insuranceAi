"""Load a realistic, fully synthetic insurance book through the real ClaimOS API.

Nothing is inserted into the database directly: every record is created by the same
HTTP endpoints, validation, RLS and audit trail that real users go through. All
records are flagged ``is_synthetic``. No real person's data is used.

Usage:
    python scripts/seed_synthetic.py [--base-url http://127.0.0.1:8080] [--seed 7]

Generated sign-in details are written to .seed-credentials.json (git-ignored).
"""

from __future__ import annotations

import argparse
import json
import random
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import date, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CREDENTIALS_FILE = ROOT / ".seed-credentials.json"

FIRST = ["Amelia", "Rohan", "Grace", "Mateo", "Aisha", "Liam", "Sofia", "Kenji", "Hannah", "Omar", "Zara", "Lucas",
         "Ingrid", "Daniel", "Fatima", "Noah", "Elena", "Tariq"]
LAST = ["Hartley", "Mehta", "Okafor", "Fernandez", "Rahman", "Walsh", "Rossi", "Tanaka", "Becker", "Haddad",
        "Khan", "Moreau", "Larsen", "Kowalski", "Siddiqui", "Brennan", "Novak", "Qureshi"]
CITIES = [("Leeds", "GB"), ("Manchester", "GB"), ("Bristol", "GB"), ("Dublin", "IE"), ("Lahore", "PK"),
          ("Karachi", "PK"), ("Dubai", "AE"), ("Toronto", "CA")]
MOTOR_STORIES = {
    "collision": "Rear-ended while stationary at a junction; rear bumper, boot lid and parking sensors damaged.",
    "weather": "Hailstorm overnight left dents across the bonnet and roof, and cracked the rear windscreen.",
    "vandalism": "Vehicle keyed along both sides and wing mirror snapped while parked on the street.",
    "fire": "Engine bay fire after an electrical fault on the motorway; bonnet and wiring loom destroyed.",
    "glass": "Stone chip on the motorway spread into a crack across the driver's line of sight.",
    "theft": "Vehicle stolen from a residential driveway overnight; both keys still with the policyholder.",
}
HEALTH_STORIES = {
    "inpatient": "Admitted for three nights with acute appendicitis; laparoscopic appendectomy performed.",
    "outpatient": "Specialist consultation and MRI scan for persistent knee pain after a sports injury.",
    "emergency": "Emergency department visit after a fall at home; wrist fracture set and cast applied.",
    "day_care": "Day-case cataract surgery on the left eye with follow-up review.",
}


class Api:
    """Minimal browser-like client: keeps the session, echoes the CSRF token, validates responses."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/") + "/api/v1"
        self.cookies: dict[str, str] = {}

    def call(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
             expect: tuple[int, ...] = (200, 201, 204)) -> Any:
        # Respect the edge rate limiter (Nginx answers 429): back off and retry the same request.
        for attempt in range(8):
            status, payload = self._send(method, path, body, headers)
            if status != 429:
                break
            time.sleep(0.5 * (attempt + 1))
        if status not in expect:
            raise RuntimeError(f"{method} {path} -> {status}: {payload}")
        return payload

    def _send(self, method: str, path: str, body: Any, headers: dict[str, str] | None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.cookies:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()))
        csrf = self.cookies.get("__Host-claimos_csrf") or self.cookies.get("claimos_csrf")
        if method != "GET" and csrf:
            request.add_header("X-CSRF-Token", csrf)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed http(s) base URL
                status, raw, set_cookies = response.status, response.read(), response.headers.get_all("Set-Cookie")
        except urllib.error.HTTPError as exc:
            status, raw, set_cookies = exc.code, exc.read(), exc.headers.get_all("Set-Cookie")
        for header in set_cookies or []:
            for name, morsel in SimpleCookie(header).items():
                self.cookies[name] = morsel.value
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode(errors="replace")[:200]
        return status, payload


def password() -> str:
    return "seed-" + secrets.token_urlsafe(18)


def version(start: date, sum_insured: str, deductible: str, line: str, waiting: int = 0) -> dict[str, Any]:
    if line == "motor":
        clauses = [
            {"clause_ref": "2.1", "section": "coverage", "title": "Accidental damage",
             "text": "We will pay for accidental damage to the insured vehicle, including fire and theft, "
                     "up to the market value shown in the schedule, less the policy excess."},
            {"clause_ref": "4.2", "section": "exclusion", "title": "Unlicensed driver",
             "text": "We will not pay for loss or damage while the vehicle is driven by a person who does not "
                     "hold a valid licence."},
            {"clause_ref": "4.5", "section": "exclusion", "title": "Wear and tear",
             "text": "We will not pay for wear and tear, mechanical or electrical breakdown."},
        ]
        coverages = [{"coverage_code": "own_damage", "clause_ref": "2.1", "limit_amount": sum_insured,
                      "perils": ["collision", "fire", "theft", "weather", "vandalism", "glass"]}]
        exclusions = [
            {"exclusion_code": "unlicensed_driver", "applies_to": ["own_damage"], "clause_ref": "4.2",
             "description": "Vehicle driven by someone without a valid licence"},
            {"exclusion_code": "wear_and_tear", "applies_to": ["own_damage"], "clause_ref": "4.5",
             "description": "Wear and tear, mechanical or electrical breakdown"},
        ]
    else:
        clauses = [
            {"clause_ref": "3.1", "section": "coverage", "title": "Hospital treatment",
             "text": "We will pay eligible hospital and specialist charges for medically necessary treatment, "
                     "subject to the annual limit and the waiting period."},
            {"clause_ref": "6.1", "section": "exclusion", "title": "Pre-existing conditions",
             "text": "We will not pay for treatment of conditions that existed before the policy start date."},
        ]
        coverages = [{"coverage_code": "medical_expenses", "clause_ref": "3.1", "limit_amount": sum_insured,
                      "perils": ["inpatient", "outpatient", "emergency", "day_care"], "co_pay_pct": "10.00"}]
        exclusions = [{"exclusion_code": "pre_existing", "applies_to": ["medical_expenses"], "clause_ref": "6.1",
                       "description": "Conditions existing before the start date"}]
    return {"effective_from": start.isoformat(), "effective_to": (start + timedelta(days=365)).isoformat(),
            "sum_insured": sum_insured, "deductible_amount": deductible, "waiting_period_days": waiting,
            "annual_aggregate_limit": sum_insured if line == "health" else None,
            "clauses": clauses, "coverages": coverages, "exclusions": exclusions}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--seed", type=int, default=7, help="random seed for reproducible data")
    args = parser.parse_args()
    rng = random.Random(args.seed)
    today = date.today()
    tag = uuid.uuid4().hex[:6]
    credentials: dict[str, dict[str, str]] = {}

    admin = Api(args.base_url)
    admin_email, admin_pw = f"admin.{tag}@northwind-mutual.example", password()
    admin.call("POST", "/auth/register", {"organization_name": "Northwind Mutual (synthetic)",
                                          "display_name": "Morgan Ellis", "email": admin_email,
                                          "password": admin_pw})
    credentials["org_admin"] = {"email": admin_email, "password": admin_pw}
    print(f"Registered organisation; admin {admin_email}")

    staff: dict[str, dict[str, Any]] = {}
    for key, name, roles in [("manager", "Priya Shah", ["claims_manager"]),
                             ("adjuster1", "Callum Reid", ["claims_adjuster"]),
                             ("adjuster2", "Nadia Farouk", ["claims_adjuster"]),
                             ("reviewer", "Jonas Weber", ["reviewer"]),
                             ("investigator", "Leila Haidari", ["fraud_investigator"]),
                             ("auditor", "Samuel Price", ["auditor"])]:
        email = f"{key}.{tag}@northwind-mutual.example"
        invited = admin.call("POST", "/users", {"email": email, "display_name": name, "roles": roles})
        token = invited["access_link"]["url"].split("#token=", 1)[1]
        client, pw = Api(args.base_url), password()
        me = client.call("POST", "/auth/invitations/accept", {"token": token, "password": pw})
        staff[key] = {"id": me["user"]["id"], "api": client}
        credentials[key] = {"email": email, "password": pw}
    print(f"Invited and activated {len(staff)} staff accounts")

    motor = admin.call("POST", "/products", {"code": "MOTOR_OD", "name": "Private Motor – Own Damage",
                                             "line": "motor", "currency": "USD"})
    health = admin.call("POST", "/products", {"code": "HEALTH_IND", "name": "Individual Health Plan",
                                              "line": "health", "currency": "USD"})

    policies: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for i in range(16):
        first, last = FIRST[i % len(FIRST)], LAST[(i * 7) % len(LAST)]
        city, country = CITIES[i % len(CITIES)]
        customer = admin.call("POST", "/customers", {
            "full_name": f"{first} {last}", "external_ref": f"NWM-C-{1000 + i}", "is_synthetic": True,
            "email": f"{first.lower()}.{last.lower()}@example.test",
            "date_of_birth": (date(1960, 1, 1) + timedelta(days=rng.randint(0, 14000))).isoformat(),
            "address": {"city": city, "country": country}})
        line = "motor" if i % 3 else "health"
        product = motor if line == "motor" else health
        start = today - timedelta(days=rng.randint(20, 330))
        sum_insured = f"{rng.choice([12000, 18000, 25000, 32000, 45000])}.00" if line == "motor" else "50000.00"
        policy = admin.call("POST", "/policies", {
            "policy_number": f"NWM-{line[0].upper()}-{24000 + i}", "customer_id": customer["id"],
            "product_id": product["id"], "is_synthetic": True, "publish_initial_version": True,
            "initial_version": version(start, sum_insured, "500.00" if line == "motor" else "250.00", line,
                                       waiting=0 if line == "motor" else 30)})
        policies.append((customer, policy, line))
    print(f"Created {len(policies)} customers and policies with published versions")

    claims: list[dict[str, Any]] = []
    for customer, policy, line in policies:
        stories = MOTOR_STORIES if line == "motor" else HEALTH_STORIES
        start = date.fromisoformat(policy["versions"][0]["effective_from"])
        for _ in range(rng.choice([1, 1, 2, 2, 3])):
            loss_type = rng.choice(list(stories))
            incident = start + timedelta(days=rng.randint(1, max(2, (today - start).days - 1)))
            amount = rng.choice([180, 420, 950, 1850, 3200, 5400, 8700, 14500, 26000])
            city, country = CITIES[rng.randrange(len(CITIES))]
            claim = admin.call("POST", "/claims", {
                "customer_id": customer["id"], "policy_id": policy["id"], "loss_type": loss_type,
                "incident_date": min(incident, today).isoformat(), "description": stories[loss_type],
                "incident_location": {"city": city, "country": country},
                "claimed_amount": {"amount": f"{amount}.{rng.choice(['00', '50', '75'])}", "currency": "USD"},
                "is_synthetic": True}, headers={"Idempotency-Key": uuid.uuid4().hex})
            claims.append(claim)
    # A deliberate near-duplicate and a claim dated before cover, to exercise intake findings.
    customer, policy, line = policies[1]
    original = next(c for c in claims if c["policy_id"] == policy["id"])
    for incident in (original["incident_date"],
                     (date.fromisoformat(policy["versions"][0]["effective_from"]) - timedelta(days=10)).isoformat()):
        claims.append(admin.call("POST", "/claims", {
            "customer_id": customer["id"], "policy_id": policy["id"], "loss_type": "collision",
            "incident_date": incident, "description": MOTOR_STORIES["collision"],
            "claimed_amount": {"amount": "2400.00", "currency": "USD"}, "is_synthetic": True},
            headers={"Idempotency-Key": uuid.uuid4().hex}))
    print(f"Filed {len(claims)} claims")

    manager = staff["manager"]["api"]
    for index, claim in enumerate(claims):
        if index % 5 == 4:
            continue  # leave some claims unassigned for the triage queue
        adjuster = staff["adjuster1" if index % 2 else "adjuster2"]
        manager.call("PUT", f"/claims/{claim['id']}/assignments", {"role": "adjuster", "user_id": adjuster["id"]})
        adjuster["api"].call("POST", f"/claims/{claim['id']}/notes", {
            "body": rng.choice(["Called the policyholder to confirm the incident details.",
                                "Requested photos and the repair estimate by email.",
                                "Customer will send the hospital discharge summary this week."])})
        if index % 4 == 0:
            detail = adjuster["api"].call("GET", f"/claims/{claim['id']}")
            adjuster["api"].call("POST", f"/claims/{claim['id']}/status",
                                 {"status": "AWAITING_INFORMATION", "reason": "Waiting for mandatory documents"},
                                 headers={"If-Match": f'"{detail["row_version"]}"'})
    manager.call("PUT", f"/claims/{claims[-2]['id']}/assignments",
                 {"role": "investigator", "user_id": staff["investigator"]["id"]})
    manager.call("POST", f"/claims/{claims[-2]['id']}/notes",
                 {"body": "Same policy and dates as an earlier claim; referred for a duplicate check.",
                  "visibility": "restricted"})
    print("Assigned claims, added notes and moved some to awaiting information")

    CREDENTIALS_FILE.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
    verify = admin.call("GET", "/audit-logs/verify")
    print(f"Audit chain valid: {verify['valid']} ({verify['events_checked']} events)")
    print(f"Sign-in details for every seeded account were written to {CREDENTIALS_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
