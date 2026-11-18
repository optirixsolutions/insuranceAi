import type { BadgeTone } from "@/components/ui/badge";

import type { MoneyValue } from "./api";

export const STATUS_LABELS: Record<string, string> = {
  SUBMITTED: "Submitted",
  DOCUMENT_PROCESSING: "Document processing",
  DOCUMENT_REVIEW: "Document review",
  POLICY_VERIFICATION: "Policy verification",
  FRAUD_ANALYSIS: "Fraud analysis",
  CLAIM_ASSESSMENT: "Assessment",
  VALIDATION: "Validation",
  DECISION_PENDING: "Decision pending",
  HUMAN_REVIEW: "Human review",
  AWAITING_INFORMATION: "Awaiting information",
  APPROVED: "Approved",
  REJECTED: "Rejected",
  COMPLETED: "Completed",
  FAILED: "Failed",
};

export function statusTone(status: string): BadgeTone {
  if (status === "APPROVED" || status === "COMPLETED") return "success";
  if (status === "REJECTED" || status === "FAILED") return "danger";
  if (status === "AWAITING_INFORMATION" || status === "HUMAN_REVIEW" || status === "DOCUMENT_REVIEW") return "warning";
  if (status === "SUBMITTED") return "neutral";
  return "info";
}

export const PRIORITY_TONE: Record<string, BadgeTone> = { urgent: "danger", high: "warning", normal: "neutral", low: "neutral" };

export const SEVERITY_TONE: Record<string, BadgeTone> = { error: "danger", warning: "warning", info: "info" };

export function humanize(value: string): string {
  return value.replaceAll("_", " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Format a decimal string without ever converting to a float for arithmetic. */
export function formatMoney(money: MoneyValue): string {
  const [whole = "0", fraction] = money.amount.split(".");
  const grouped = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(BigInt(whole));
  return `${money.currency} ${fraction ? `${grouped}.${fraction}` : grouped}`;
}

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const dateTimeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

/** Calendar dates (YYYY-MM-DD) are formatted without a timezone shift. */
export function formatDate(value: string): string {
  const [y, m, d] = value.split("-").map(Number);
  return dateFormat.format(new Date(y ?? 1970, (m ?? 1) - 1, d ?? 1));
}

export function formatDateTime(value: string): string {
  return dateTimeFormat.format(new Date(value));
}
