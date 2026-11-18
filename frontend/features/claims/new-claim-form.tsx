"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, Select, fieldA11y } from "@/components/ui/form";
import { fieldErrors } from "@/features/auth/schema";
import { useCustomers, usePolicies } from "@/features/policies/api";
import { ApiError } from "@/lib/api/client";

import { useCreateClaim, useLossTypes } from "./api";
import { humanize } from "./format";

// Local calendar date (en-CA formats as YYYY-MM-DD).
const today = () => new Date().toLocaleDateString("en-CA");

const claimFormSchema = z.object({
  loss_type: z.string().min(1, "Choose the type of loss"),
  incident_date: z
    .string()
    .regex(/^\d{4}-\d{2}-\d{2}$/, "Enter the incident date")
    .refine((d) => d <= today(), "The incident date cannot be in the future"),
  incident_time: z.string().optional(),
  amount: z.string().regex(/^\d+(\.\d+)?$/, "Enter an amount such as 5200.00"),
  city: z.string().max(100).optional(),
  country: z
    .string()
    .trim()
    .toUpperCase()
    .regex(/^([A-Z]{2})?$/, "Use a 2-letter country code")
    .optional(),
  location_description: z.string().max(300).optional(),
  description: z.string().trim().min(10, "Describe what happened (at least 10 characters)").max(5000),
});

export function NewClaimForm() {
  const router = useRouter();
  const [customerQuery, setCustomerQuery] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [policyId, setPolicyId] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  // One key per form instance: a double submit or network retry replays instead of duplicating.
  const [idempotencyKey] = useState(() => crypto.randomUUID());

  const customers = useCustomers(customerQuery);
  const policies = usePolicies({ customerId }, Boolean(customerId));
  const lossTypes = useLossTypes();
  const create = useCreateClaim();
  const policy = useMemo(() => policies.data?.items.find((p) => p.id === policyId), [policies.data, policyId]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(event.currentTarget).entries());
    const parsed = claimFormSchema.safeParse(values);
    const next = parsed.success ? {} : fieldErrors(parsed.error);
    if (!customerId) next.customer = "Choose the customer";
    if (!policy) next.policy = "Choose the policy";
    setErrors(next);
    if (!parsed.success || !policy || Object.keys(next).length) return;
    const v = parsed.data;
    const location = { city: v.city || undefined, country: v.country || undefined, description: v.location_description || undefined };
    const result = await create
      .mutateAsync({
        idempotencyKey,
        body: {
          customer_id: customerId,
          policy_id: policy.id,
          loss_type: v.loss_type,
          incident_date: v.incident_date,
          incident_time: v.incident_time || null,
          incident_location: Object.values(location).some(Boolean) ? location : null,
          description: v.description,
          claimed_amount: { amount: v.amount, currency: policy.currency },
          is_synthetic: policy.is_synthetic,
        },
      })
      .catch(() => null);
    if (result) router.push(`/claims/${result.id}?created=1`);
  }

  const types = policy ? (lossTypes.data?.lines[policy.product_line] ?? []) : [];
  const apiError = create.error instanceof ApiError ? create.error.userMessage : create.error ? "The claim could not be submitted." : null;

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-6">
      {apiError ? <Alert>{apiError}</Alert> : null}
      <Card>
        <CardHeader>
          <div>
            <CardTitle>Customer and policy</CardTitle>
            <CardDescription>The claim is checked against the policy version in force on the incident date.</CardDescription>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-3">
            <Field id="customer-search" label="Find customer" hint="Search by name, email or reference.">
              <Input id="customer-search" type="search" value={customerQuery} onChange={(e) => setCustomerQuery(e.target.value)} />
            </Field>
            <Field id="customer" label="Customer" error={errors.customer}>
              <Select {...fieldA11y("customer", errors.customer)} value={customerId}
                onChange={(e) => { setCustomerId(e.target.value); setPolicyId(""); }}>
                <option value="">{customers.isPending ? "Loading…" : "Select a customer"}</option>
                {customers.data?.items.map((c) => (
                  <option key={c.id} value={c.id}>{c.full_name}{c.external_ref ? ` (${c.external_ref})` : ""}</option>
                ))}
              </Select>
            </Field>
          </div>
          <Field id="policy" label="Policy" error={errors.policy}>
            <Select {...fieldA11y("policy", errors.policy)} value={policyId} disabled={!customerId} onChange={(e) => setPolicyId(e.target.value)}>
              <option value="">{customerId ? "Select a policy" : "Choose a customer first"}</option>
              {policies.data?.items.map((p) => (
                <option key={p.id} value={p.id}>{p.policy_number} · {p.product_name} ({p.status})</option>
              ))}
            </Select>
          </Field>
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle>Incident</CardTitle></CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <Field id="loss_type" label="Type of loss" error={errors.loss_type}>
            <Select {...fieldA11y("loss_type", errors.loss_type)} disabled={!policy} defaultValue="">
              <option value="">{policy ? "Select" : "Choose a policy first"}</option>
              {types.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}
            </Select>
          </Field>
          <Field id="amount" label={`Claimed amount${policy ? ` (${policy.currency})` : ""}`} error={errors.amount}>
            <Input {...fieldA11y("amount", errors.amount)} inputMode="decimal" placeholder="0.00" />
          </Field>
          <Field id="incident_date" label="Incident date" error={errors.incident_date}>
            <Input {...fieldA11y("incident_date", errors.incident_date)} type="date" max={today()} />
          </Field>
          <Field id="incident_time" label="Incident time (optional)">
            <Input {...fieldA11y("incident_time")} type="time" />
          </Field>
          <Field id="city" label="City (optional)">
            <Input {...fieldA11y("city")} maxLength={100} />
          </Field>
          <Field id="country" label="Country code (optional)" error={errors.country}>
            <Input {...fieldA11y("country", errors.country)} maxLength={2} placeholder="GB" />
          </Field>
          <div className="sm:col-span-2">
            <Field id="location_description" label="Where exactly (optional)">
              <Input {...fieldA11y("location_description")} maxLength={300} />
            </Field>
          </div>
          <div className="sm:col-span-2">
            <Field id="description" label="What happened" error={errors.description}>
              <textarea {...fieldA11y("description", errors.description)} rows={5}
                className="w-full rounded-md border border-input bg-surface px-3 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/20 aria-[invalid=true]:border-danger" />
            </Field>
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end gap-2">
        <Button type="button" variant="secondary" onClick={() => router.back()}>Cancel</Button>
        <Button type="submit" disabled={create.isPending}>{create.isPending ? "Submitting…" : "Submit claim"}</Button>
      </div>
    </form>
  );
}
