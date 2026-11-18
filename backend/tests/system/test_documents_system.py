"""Document intelligence end to end on the running stack: real uploads, AV, OCR, extraction."""

from __future__ import annotations

import io
from typing import Any

import pytest
from PIL import Image

from tests.fixtures.documents import (
    HOSPITAL_BILL,
    REPAIR_ESTIMATE,
    docx_file,
    docx_with_macro,
    photo_image,
    scan_image,
    text_pdf,
)
from tests.system.conftest import SystemClient, wait_for_documents

pytestmark = pytest.mark.system

CLAIM_FORM = [
    "MOTOR CLAIM FORM",
    "Claimant: Rohan Tanaka",
    "Policy Number: NWM-M-24001",
    "Date of accident: 2026-10-08",
    "Registration: LK21 ABC",
    "Declaration: the above is true.",
    "Signature: R. Tanaka",
]
EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def _pdf_of_image(png: bytes) -> bytes:
    buffer = io.BytesIO()
    Image.open(io.BytesIO(png)).convert("RGB").save(buffer, format="PDF", resolution=150)
    return buffer.getvalue()


UPLOADS: dict[str, tuple[bytes, str]] = {
    "estimate.pdf": (text_pdf(REPAIR_ESTIMATE), "application/pdf"),
    "hospital-bill-scan.png": (scan_image(HOSPITAL_BILL), "image/png"),
    "estimate-scanned.pdf": (_pdf_of_image(scan_image(REPAIR_ESTIMATE, seed=2)), "application/pdf"),
    "bill-blurred.png": (scan_image(HOSPITAL_BILL, degrade="blur"), "image/png"),
    "bill-noisy.png": (scan_image(HOSPITAL_BILL, degrade="noise"), "image/png"),
    "bill-faded.jpg": (scan_image(HOSPITAL_BILL, degrade="faded", fmt="JPEG"), "image/jpeg"),
    "bill-small.png": (scan_image(HOSPITAL_BILL, degrade="small"), "image/png"),
    "bill-rotated.png": (scan_image(HOSPITAL_BILL, rotate=90), "image/png"),
    "damage-photo.jpg": (photo_image(), "image/jpeg"),
    "claim-form.docx": (
        docx_file(CLAIM_FORM),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
}
REJECTED: dict[str, tuple[bytes, str, str]] = {
    "macro.docx": (
        docx_with_macro(CLAIM_FORM),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx_macros",
    ),
    "script.pdf": (
        text_pdf(["x" * 40], extra_catalog="/OpenAction << /S /JavaScript /JS (app.alert(1)) >> "),
        "application/pdf",
        "pdf_active_content",
    ),
    "fake.png": (text_pdf(["looks like a pdf"]), "image/png", "type_mismatch"),
    "tool.exe": (b"MZ\x90\x00\x03 executable bytes", "application/octet-stream", "unsupported_type"),
}


@pytest.fixture(scope="module")
def processed(admin: SystemClient, claim: dict[str, Any]) -> dict[str, dict[str, Any]]:
    files = [
        ("files", (name, data, mime))
        for name, (data, mime) in {**UPLOADS, **{k: (v[0], v[1]) for k, v in REJECTED.items()}}.items()
    ]
    r = admin.post(f"/claims/{claim['id']}/documents", files=files)
    assert r.status_code == 201, r.text
    results = {item["filename"]: item for item in r.json()}
    for name, (_, _, code) in REJECTED.items():
        assert results[name]["accepted"] is False and results[name]["error_code"] == code, results[name]
    assert all(results[name]["accepted"] for name in UPLOADS)
    docs = wait_for_documents(admin, claim["id"], len(UPLOADS))
    return {d["original_filename"]: d for d in docs}


def _fields(admin: SystemClient, doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["field_name"]: f for f in admin.get(f"/documents/{doc['id']}/fields").json()}


def test_text_layer_pdf(admin: SystemClient, processed: dict[str, dict[str, Any]]) -> None:
    doc = processed["estimate.pdf"]
    assert doc["document_type"] == "repair_estimate" and doc["scan_status"] in ("clean", "skipped_dev")
    pages = admin.get(f"/documents/{doc['id']}/pages").json()
    assert pages[0]["text_source"] == "text_layer" and pages[0]["has_image"]
    fields = _fields(admin, doc)
    assert fields["total_amount"]["value_normalized"] == {"amount": "1475.00", "currency": "GBP"}
    assert fields["policy_number"]["evidence_excerpt"] == "Policy Number: NWM-M-24001"
    assert fields["policy_number"]["bbox"] and fields["policy_number"]["grounding_status"] == "grounded"


def test_ocr_on_clean_scans(admin: SystemClient, processed: dict[str, dict[str, Any]]) -> None:
    bill = processed["hospital-bill-scan.png"]
    page = admin.get(f"/documents/{bill['id']}/pages").json()[0]
    assert page["text_source"] == "tesseract" and page["ocr_confidence"] > 0.75
    assert bill["document_type"] == "medical_bill"
    fields = _fields(admin, bill)
    assert fields["total_amount"]["value_normalized"] == {"amount": "6660.50", "currency": "USD"}
    assert fields["patient_name"]["value_text"] == "Amelia Hartley"
    assert fields["admission_date"]["value_normalized"] == "2026-09-14"
    scanned_pdf = admin.get(f"/documents/{processed['estimate-scanned.pdf']['id']}/pages").json()[0]
    assert scanned_pdf["text_source"] == "tesseract" and "Total due" in scanned_pdf["text"]


def test_degraded_scans_report_accuracy(
    admin: SystemClient, processed: dict[str, dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    """Measures field accuracy on degraded scans instead of assuming it. Hard floor: 75% correct."""
    expected = {
        "policy_number": "NWM-H-24000",
        "invoice_number": "INV-778812",
        "patient_name": "Amelia Hartley",
        "total_amount": {"amount": "6660.50", "currency": "USD"},
        "admission_date": "2026-09-14",
    }
    total = correct = 0
    report = []
    for name in (
        "bill-blurred.png",
        "bill-noisy.png",
        "bill-faded.jpg",
        "bill-small.png",
        "bill-rotated.png",
    ):
        doc = processed[name]
        fields = _fields(admin, doc)
        hits = sum(
            1
            for key, want in expected.items()
            if key in fields
            and (fields[key]["value_normalized"] == want or fields[key]["value_text"] == want)
        )
        total += len(expected)
        correct += hits
        report.append(
            f"{name}: {hits}/{len(expected)} fields, status={doc['processing_status']}, "
            f"quality={doc['quality_score']}, reasons={doc['review_reasons']}"
        )
    rotated = admin.get(f"/documents/{processed['bill-rotated.png']['id']}/pages").json()[0]
    report.append(f"rotation applied to rotated scan: {rotated['rotation_applied']}")
    with capsys.disabled():
        print("\nDegraded-scan extraction accuracy:", f"{correct}/{total}", *report, sep="\n  ")
    assert rotated["rotation_applied"] in (90, 270)
    assert correct / total >= 0.75


def test_photo_and_docx(admin: SystemClient, processed: dict[str, dict[str, Any]]) -> None:
    assert processed["damage-photo.jpg"]["document_type"] == "photo_evidence"
    form = processed["claim-form.docx"]
    assert form["document_type"] == "claim_form"
    fields = _fields(admin, form)
    assert fields["incident_date"]["value_normalized"] == "2026-10-08"
    assert fields["vehicle_registration"]["value_normalized"] == "LK21ABC"


def test_duplicates_and_reupload(
    admin: SystemClient, claim: dict[str, Any], processed: dict[str, dict[str, Any]]
) -> None:
    again = admin.post(
        f"/claims/{claim['id']}/documents",
        files=[("files", ("again.pdf", UPLOADS["estimate.pdf"][0], "application/pdf"))],
    ).json()[0]
    assert again["accepted"] is False and again["error_code"] == "already_uploaded"
    # The JPEG re-encoding of the clean bill is a near-duplicate of the PNG scan.
    jpeg = scan_image(HOSPITAL_BILL, fmt="JPEG")
    admin.post(f"/claims/{claim['id']}/documents", files=[("files", ("bill-copy.jpg", jpeg, "image/jpeg"))])
    docs = {d["original_filename"]: d for d in wait_for_documents(admin, claim["id"], len(UPLOADS) + 1)}
    copy = docs["bill-copy.jpg"]
    assert copy["processing_status"] == "needs_review" and "possible_duplicate" in copy["review_reasons"]
    assert any(d["method"] in ("image_similarity", "text_similarity") for d in copy["duplicates"])


def test_correction_is_audited_and_claim_tracks_received_documents(
    admin: SystemClient, claim: dict[str, Any], processed: dict[str, dict[str, Any]]
) -> None:
    doc = processed["estimate.pdf"]
    field = _fields(admin, doc)["policyholder_name"]
    r = admin.patch(
        f"/documents/{doc['id']}/fields/{field['id']}",
        json={"value": "Rohan K. Tanaka", "reason": "Middle initial on the policy schedule"},
    )
    assert (
        r.status_code == 200
        and r.json()["validation_status"] == "corrected"
        and r.json()["source"] == "manual"
    )
    assert _fields(admin, doc)["policyholder_name"]["value_text"] == "Rohan K. Tanaka"
    events = admin.get("/audit-logs", params={"action": "document.field_corrected"}).json()["items"]
    assert events and events[0]["entity_id"] == doc["id"] and "Rohan" not in str(events[0]["after"])
    verified = admin.post(f"/documents/{doc['id']}/fields/{_fields(admin, doc)['total_amount']['id']}/verify")
    assert verified.json()["validation_status"] == "verified"
    detail = admin.get(f"/claims/{claim['id']}").json()
    assert {"repair_estimate", "claim_form", "photo_evidence"} <= set(detail["documents_received"])
    assert admin.get("/audit-logs/verify").json()["valid"] is True


def test_previews_downloads_and_isolation(
    admin: SystemClient, base_url: str, processed: dict[str, dict[str, Any]]
) -> None:
    doc = processed["estimate.pdf"]
    image = admin.get(f"/documents/{doc['id']}/pages/1/image")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    original = admin.get(f"/documents/{doc['id']}/content")
    assert original.headers["content-disposition"].startswith("attachment;")
    assert original.content == UPLOADS["estimate.pdf"][0]
    outsider = SystemClient(base_url)
    import uuid as _uuid

    outsider.post(
        "/auth/register",
        json={
            "organization_name": "Other Org",
            "display_name": "O",
            "email": f"other-{_uuid.uuid4().hex[:8]}@example.test",
            "password": "another passphrase!",
        },
    )
    assert outsider.get(f"/documents/{doc['id']}").status_code == 404
    assert outsider.get(f"/documents/{doc['id']}/content").status_code == 404


def test_malware_is_rejected_by_clamav(admin: SystemClient, claim: dict[str, Any]) -> None:
    ready = admin.get("/health/ready").json()
    av = next((c for c in ready["components"] if c["name"] == "antivirus"), None)
    if av is None or av["status"] != "ok":
        pytest.skip(f"ClamAV not ready on this stack ({av['detail'] if av else 'not configured'})")
    # Malware hidden inside an Office document: a DOCX is a ZIP, and clamd unpacks archives.
    import zipfile

    source = io.BytesIO(docx_file(CLAIM_FORM))
    infected = io.BytesIO()
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(infected, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item))
        dst.writestr("word/media/eicar.com", EICAR)
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    r = admin.post(
        f"/claims/{claim['id']}/documents",
        files=[("files", ("claim-form-final.docx", infected.getvalue(), docx_mime))],
    )
    assert r.json()[0]["accepted"] is True, r.text  # structurally valid DOCX: only the scanner can catch it
    doc = r.json()[0]["document"]
    docs = {d["id"]: d for d in wait_for_documents(admin, claim["id"], len(UPLOADS) + 2)}
    assert docs[doc["id"]]["scan_status"] == "infected" and docs[doc["id"]]["processing_status"] == "rejected"
