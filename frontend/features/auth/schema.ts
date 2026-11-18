import { z } from "zod";

export const meSchema = z.object({
  user: z.object({ id: z.string(), email: z.string(), display_name: z.string() }),
  organization: z.object({ id: z.string(), name: z.string(), slug: z.string() }),
  roles: z.array(z.string()),
  permissions: z.array(z.string()),
  session_expires_at: z.string().nullable().optional(),
});
export type Me = z.infer<typeof meSchema>;

export const PASSWORD_MIN = 12;

export const emailField = z.string().trim().toLowerCase().email("Enter a valid email address").max(254);
export const passwordField = z
  .string()
  .min(PASSWORD_MIN, `Use at least ${PASSWORD_MIN} characters`)
  .max(128, "Use at most 128 characters")
  .refine((v) => v.trim() === v, "Remove leading or trailing spaces");

export const loginFormSchema = z.object({
  email: emailField,
  password: z.string().min(1, "Enter your password"),
});

export const registerFormSchema = z.object({
  organization_name: z.string().trim().min(2, "Enter your organisation name").max(200),
  display_name: z.string().trim().min(1, "Enter your name").max(120),
  email: emailField,
  password: passwordField,
});

export const setPasswordFormSchema = z
  .object({ password: passwordField, confirm: z.string() })
  .refine((v) => v.password === v.confirm, { path: ["confirm"], message: "Passwords do not match" });

export const ROLE_LABELS: Record<string, string> = {
  org_admin: "Organization Administrator",
  claims_manager: "Claims Manager",
  claims_adjuster: "Claims Adjuster",
  fraud_investigator: "Fraud Investigator",
  compliance_officer: "Compliance Officer",
  reviewer: "Reviewer",
  auditor: "Read-only Auditor",
};

/** Turn zod issues into a field -> first message map for form display. */
export function fieldErrors(error: z.ZodError): Record<string, string> {
  const out: Record<string, string> = {};
  for (const issue of error.issues) {
    const key = String(issue.path[0] ?? "form");
    out[key] ??= issue.message;
  }
  return out;
}
