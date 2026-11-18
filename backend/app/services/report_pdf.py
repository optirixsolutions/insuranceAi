"""PDF rendering of a claim report (ReportLab; see ADR-020 for why not WeasyPrint).

The PDF is a faithful rendering of the JSON report: the same sections, the same evidence labels
(``E1`` ...) next to each statement, and an evidence appendix listing what every label points at.
"""

from __future__ import annotations

import io
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

STYLES = getSampleStyleSheet()
BODY = ParagraphStyle("body", parent=STYLES["BodyText"], fontSize=8.5, leading=11)
SMALL = ParagraphStyle("small", parent=BODY, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#444444"))
H1 = ParagraphStyle("h1", parent=STYLES["Heading1"], fontSize=15, spaceAfter=4)
H2 = ParagraphStyle("h2", parent=STYLES["Heading2"], fontSize=11, spaceBefore=8, spaceAfter=3)
GRID = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#bbbbbb")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
    ]
)


def _p(value: Any, style: ParagraphStyle = BODY) -> Paragraph:
    return Paragraph(escape("" if value is None else str(value)), style)


def _refs(refs: list[str] | None) -> str:
    return f"[{', '.join(refs)}]" if refs else ""


def _table(head: list[str], rows: list[list[Any]], widths: list[float] | None = None) -> Table:
    data = [[_p(h, SMALL) for h in head]] + [[_p(c, SMALL) for c in r] for r in rows]
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(GRID)
    return table


def _facts(pairs: list[tuple[str, Any]]) -> Table:
    table = Table([[_p(k, SMALL), _p(v)] for k, v in pairs], colWidths=[45 * mm, 125 * mm])
    table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return table


def render(report: dict[str, Any]) -> bytes:
    meta, claim, policy = report["report"], report["claim"], report["policy"]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=f"Claim report {claim['reference']} v{meta['version_no']}",
        author="ClaimOS",
        subject="Claim decision report",
    )
    story: list[Any] = [
        _p(f"Claim report {claim['reference']}", H1),
        _p(
            f"Version {meta['version_no']} · template {meta['template_version']} · "
            f"generated {meta['generated_at']}" + (" · SYNTHETIC DATA" if meta.get("synthetic") else ""),
            SMALL,
        ),
        Spacer(1, 4 * mm),
        _p("Claim", H2),
        _facts(
            [
                ("Status", claim["status"]),
                ("Product / loss type", f"{claim['product']} / {claim['loss_type']}"),
                ("Customer", claim["customer"]),
                ("Incident date / reported", f"{claim['incident_date']} / {claim['reported_at']}"),
                ("Claimed", f"{claim['claimed_amount']} {claim['currency']}"),
                ("Description", claim["description"]),
            ]
        ),
        _p("Policy", H2),
        _facts(
            [
                ("Policy / version", f"{policy['policy_number']} v{policy['version_no']}"),
                ("Effective", f"{policy['effective_from']} to {policy['effective_to']}"),
                ("Sum insured / deductible", f"{policy['sum_insured']} / {policy['deductible']}"),
            ]
        ),
    ]
    rec = report["recommendation"]
    story += [
        _p("Recommendation", H2),
        _p(f"{rec['recommendation'] or 'none'} (rule {rec['matrix_rule'] or '-'}) {_refs(rec['evidence'])}"),
        _p(rec.get("explanation") or "", SMALL),
    ]
    for reason in rec.get("mandatory_review_reasons", []):
        story.append(_p(f"• {reason}", SMALL))
    story.append(_p("Decisions", H2))
    if report["decisions"]:
        story.append(
            _table(
                [
                    "Decision",
                    "Mode",
                    "Amount",
                    "Decided by",
                    "Second approver",
                    "Reason",
                    "Override",
                    "Evidence",
                ],
                [
                    [
                        d["decision"],
                        d["mode"],
                        f"{d['approved_amount'] or '-'} {d['currency']}",
                        d["decided_by"],
                        d["second_approver"] or "-",
                        f"{d['reason_code'] + ': ' if d['reason_code'] else ''}{d['reason'] or ''}",
                        d["override_reason"] if d["override"] else "no",
                        _refs(d["evidence"]),
                    ]
                    for d in report["decisions"]
                ],
            )
        )
    else:
        story.append(_p("No decision recorded yet.", SMALL))
    cov = report["coverage"]
    story += [
        _p("Coverage", H2),
        _p(
            f"Policy active on incident date: {cov['policy_active_on_incident_date']} · "
            f"holder match: {cov['holder_match']}",
            SMALL,
        ),
    ]
    if cov["coverages"]:
        story.append(
            _table(
                ["Coverage", "Status", "Reasons", "Clauses"],
                [
                    [c["code"], c["status"], "; ".join(c["reasons"]), _refs(c["evidence"])]
                    for c in cov["coverages"]
                ],
            )
        )
    for x in cov["exclusions_triggered"]:
        story.append(_p(f"Exclusion {x['code']}: {x['reason']} {_refs(x['evidence'])}"))
    fraud = report["fraud"]
    story += [
        _p("Fraud analysis", H2),
        _p(f"Risk {fraud['risk_score']}/100 ({fraud['risk_level']}), action {fraud['recommended_action']}"),
    ]
    if fraud["indicators"]:
        story.append(
            _table(
                ["Indicator", "Severity", "Description", "Points", "Evidence"],
                [
                    [i["code"], i["severity"], i["description"], i["contribution"], _refs(i["evidence"])]
                    for i in fraud["indicators"]
                ],
            )
        )
    a = report["assessment"]
    story += [
        _p("Financial assessment", H2),
        _p(f"Status {a['status']} · calculator {a['calculator']} · rule set {a['rule_set_version']}", SMALL),
    ]
    if a["line_items"]:
        story.append(
            _table(
                ["#", "Item", "Category", "Claimed", "Dep. %", "Eligible", "Status", "Evidence"],
                [
                    [
                        li["line"],
                        li["description"],
                        li["category"],
                        li["claimed"],
                        li["depreciation_pct"],
                        li["eligible"],
                        li["status"],
                        _refs(li["evidence"]),
                    ]
                    for li in a["line_items"]
                ],
            )
        )
    totals = [
        (k.replace("_", " ").capitalize(), f"{v} {a['currency'] or ''}")
        for k, v in a["totals"].items()
        if v is not None
    ]
    if totals:
        story.append(_facts(totals))
    if a["calculation"]:
        story.append(
            _table(["Step", "Result", "Rule"], [[s["op"], s["output"], s["rule"]] for s in a["calculation"]])
        )
    if report["validation"]:
        story += [
            _p("Cross-document validation", H2),
            _table(
                ["Check", "Severity", "Finding", "Evidence"],
                [
                    [f["check"], f["severity"], f["message"], _refs(f["evidence"])]
                    for f in report["validation"]
                ],
            ),
        ]
    if report["fields"]:
        story += [
            _p("Extracted fields", H2),
            _table(
                ["Field", "Value", "Document", "Page", "Source", "Status", "Evidence"],
                [
                    [
                        f["field"],
                        f["value"],
                        f["document"],
                        f["page"],
                        f["source"],
                        f["status"],
                        _refs(f["evidence"]),
                    ]
                    for f in report["fields"]
                ],
            ),
        ]
    story += [
        _p("Documents", H2),
        _table(
            ["File", "Type", "Pages", "SHA-256"],
            [[d["filename"], d["type"], d["pages"], d["sha256"]] for d in report["documents"]],
        )
        if report["documents"]
        else _p("No documents.", SMALL),
        _p("Status history", H2),
        _table(
            ["From", "To", "Reason", "At"],
            [[h["from"], h["to"], h["reason"], h["at"]] for h in report["status_history"]],
        )
        if report["status_history"]
        else _p("No status changes.", SMALL),
        _p("Agent runs", H2),
        _table(
            ["Agent", "Status", "Model", "Rule set", "Run id"],
            [
                [r["agent"], r["status"], r["model"] or "deterministic", r["rule_set_version"], r["run_id"]]
                for r in report["agent_runs"]
            ],
        )
        if report["agent_runs"]
        else _p("No agent runs.", SMALL),
        _p("Evidence index", H2),
        _table(
            ["Ref", "Kind", "Points at", "Page", "Excerpt"],
            [
                [e["ref"], e["kind"], e["label"] or e["id"], e["page"] or "", e["excerpt"] or ""]
                for e in report["evidence"]
            ],
            widths=[12 * mm, 16 * mm, 45 * mm, 10 * mm, 91 * mm],
        )
        if report["evidence"]
        else _p("No evidence cited.", SMALL),
    ]
    doc.build(story)
    return buffer.getvalue()
