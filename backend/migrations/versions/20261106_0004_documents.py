"""Claim documents, pages, extractions, fields and corrections.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = pg.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
ORG_SETTING = "NULLIF(current_setting('app.org_id', true), '')::uuid"
TABLES = (
    "claim_documents",
    "document_pages",
    "document_extractions",
    "extracted_fields",
    "field_corrections",
)


def _id() -> sa.Column[object]:
    return sa.Column("id", UUID, primary_key=True, server_default=sa.text("gen_random_uuid()"))


def _doc_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["organization_id", "document_id"],
        ["claim_documents.organization_id", "claim_documents.id"],
        ondelete="CASCADE",
        name=f"fk_{table}_document",
    )


def _app_role(sql: str) -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'claimos_app') THEN "
        f"EXECUTE {sql!r}; END IF; END $$"
    )


def upgrade() -> None:
    op.create_table(
        "claim_documents",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("claim_id", UUID, nullable=False),
        sa.Column("version_no", sa.Integer, nullable=False, server_default="1"),
        sa.Column("supersedes_id", UUID),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("declared_mime", sa.String(100), nullable=False),
        sa.Column("detected_mime", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("perceptual_hash", sa.String(16)),
        sa.Column("text_simhash", sa.String(16)),
        sa.Column("storage_bucket", sa.String(100), nullable=False),
        sa.Column("storage_key", sa.String(400), nullable=False),
        sa.Column("scan_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("processing_status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("document_type", sa.String(40)),
        sa.Column("classification_confidence", sa.Float),
        sa.Column("classification_source", sa.String(20)),
        sa.Column("page_count", sa.Integer),
        sa.Column("quality_score", sa.Float),
        sa.Column("review_reasons", pg.ARRAY(sa.String(60)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("duplicates", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("error_detail", sa.String(300)),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("processed_at", TS),
        sa.Column("uploaded_by", UUID),
        sa.Column("is_synthetic", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("deleted_at", TS),
        sa.Column("row_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "id", name="uq_claim_documents_org_id"),
        sa.ForeignKeyConstraint(
            ["organization_id", "claim_id"],
            ["claims.organization_id", "claims.id"],
            ondelete="CASCADE",
            name="fk_claim_documents_claim",
        ),
        sa.CheckConstraint(
            "scan_status IN ('pending', 'clean', 'infected', 'skipped_dev', 'error')", name="scan"
        ),
        sa.CheckConstraint(
            "processing_status IN ('queued', 'processing', 'processed', 'needs_review', 'failed', 'rejected')",
            name="processing",
        ),
        sa.CheckConstraint("size_bytes > 0", name="size"),
    )
    op.create_index("ix_claim_documents_org", "claim_documents", ["organization_id"])
    op.create_index("ix_claim_documents_claim", "claim_documents", ["claim_id"])
    op.create_index("ix_claim_documents_org_sha", "claim_documents", ["organization_id", "sha256"])
    op.create_index("ix_claim_documents_review", "claim_documents", ["organization_id", "processing_status"])

    op.create_table(
        "document_pages",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("page_number", sa.Integer, nullable=False),
        sa.Column("text_source", sa.String(20), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("ocr_confidence", sa.Float),
        sa.Column("quality_score", sa.Float, nullable=False),
        sa.Column("quality_flags", pg.ARRAY(sa.String(40)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("width", sa.Integer),
        sa.Column("height", sa.Integer),
        sa.Column("rotation_applied", sa.Integer, nullable=False, server_default="0"),
        sa.Column("image_storage_key", sa.String(400)),
        sa.Column("words", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "page_number", name="uq_document_pages_page"),
        _doc_fk("document_pages"),
        sa.CheckConstraint(
            "text_source IN ('text_layer', 'tesseract', 'paddleocr', 'docx', 'none')", name="source"
        ),
    )
    op.create_index("ix_document_pages_document", "document_pages", ["document_id"])
    op.execute("CREATE INDEX ix_document_pages_text_trgm ON document_pages USING gin (text gin_trgm_ops)")

    op.create_table(
        "document_extractions",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("method", sa.String(40), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt", sa.Integer, nullable=False),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        _doc_fk("document_extractions"),
    )
    op.create_index("ix_document_extractions_document", "document_extractions", ["document_id"])

    op.create_table(
        "extracted_fields",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("extraction_id", UUID),
        sa.Column("field_name", sa.String(60), nullable=False),
        sa.Column("value_text", sa.Text),
        sa.Column("value_normalized", pg.JSONB),
        sa.Column("value_type", sa.String(20), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("page_number", sa.Integer),
        sa.Column("evidence_excerpt", sa.String(500)),
        sa.Column("bbox", pg.JSONB),
        sa.Column("grounding_status", sa.String(20), nullable=False),
        sa.Column("validation_status", sa.String(20), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("is_current", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("verified_by", UUID),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        _doc_fk("extracted_fields"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence"),
        sa.CheckConstraint(
            "grounding_status IN ('grounded', 'ungrounded', 'not_applicable')", name="grounding"
        ),
        sa.CheckConstraint(
            "validation_status IN ('verified', 'unverified', 'low_confidence', 'conflict', 'corrected', 'rejected')",
            name="validation",
        ),
        sa.CheckConstraint("source IN ('rules', 'llm', 'manual')", name="source"),
    )
    op.create_index("ix_extracted_fields_document", "extracted_fields", ["document_id", "field_name"])

    op.create_table(
        "field_corrections",
        _id(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("previous_field_id", UUID, nullable=False),
        sa.Column("new_field_id", UUID, nullable=False),
        sa.Column("field_name", sa.String(60), nullable=False),
        sa.Column("previous_value", sa.Text),
        sa.Column("new_value", sa.Text, nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("corrected_by", UUID, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        _doc_fk("field_corrections"),
    )
    op.create_index("ix_field_corrections_document", "field_corrections", ["document_id"])
    op.execute(
        "CREATE TRIGGER trg_field_corrections_append_only BEFORE UPDATE OR DELETE ON field_corrections "
        "FOR EACH ROW EXECUTE FUNCTION claimos_forbid_mutation()"
    )
    op.execute(
        "CREATE TRIGGER trg_claim_documents_updated_at BEFORE UPDATE ON claim_documents "
        "FOR EACH ROW EXECUTE FUNCTION claimos_set_updated_at()"
    )

    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tenant_isolation ON {table} USING (organization_id = {ORG_SETTING}) "
            f"WITH CHECK (organization_id = {ORG_SETTING})"
        )
    _app_role("REVOKE UPDATE, DELETE, TRUNCATE ON field_corrections FROM claimos_app")


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
