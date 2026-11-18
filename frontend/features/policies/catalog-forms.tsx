"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Field, Input, Select, fieldA11y } from "@/components/ui/form";
import { ApiError } from "@/lib/api/client";

import { useCreateCustomer, useCreatePolicy, useCreateProduct, useCustomers, useProducts } from "./api";

const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong.");
const values = (e: FormEvent<HTMLFormElement>) =>
  Object.fromEntries([...new FormData(e.currentTarget).entries()].map(([k, v]) => [k, String(v).trim()]));

export function ProductDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateProduct();
  return (
    <Dialog open={open} onClose={() => { create.reset(); onClose(); }} title="New insurance product"
      description="Required documents default to the standard list for the line of business.">
      <form className="space-y-3" onSubmit={(e) => {
        e.preventDefault();
        const v = values(e);
        create.mutate({ ...v, code: v.code?.toUpperCase(), currency: v.currency?.toUpperCase() }, { onSuccess: onClose });
      }}>
        {create.error ? <Alert>{errorText(create.error)}</Alert> : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="code" label="Code" hint="e.g. MOTOR_OD"><Input {...fieldA11y("code")} required pattern="[A-Za-z0-9_\-]{2,40}" /></Field>
          <Field id="currency" label="Currency"><Input {...fieldA11y("currency")} required maxLength={3} defaultValue="USD" /></Field>
        </div>
        <Field id="name" label="Name"><Input {...fieldA11y("name")} required /></Field>
        <Field id="line" label="Line of business">
          <Select {...fieldA11y("line")} defaultValue="motor">
            <option value="motor">Motor</option><option value="health">Health</option>
            <option value="property">Property</option><option value="travel">Travel</option>
          </Select>
        </Field>
        <div className="flex justify-end gap-2"><Button type="submit" disabled={create.isPending}>Create product</Button></div>
      </form>
    </Dialog>
  );
}

export function CustomerDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateCustomer();
  return (
    <Dialog open={open} onClose={() => { create.reset(); onClose(); }} title="New customer">
      <form className="space-y-3" onSubmit={(e) => {
        e.preventDefault();
        const v = values(e);
        create.mutate({
          full_name: v.full_name, customer_type: v.customer_type, external_ref: v.external_ref || null,
          email: v.email || null, phone: v.phone || null, date_of_birth: v.date_of_birth || null,
          address: v.city || v.country ? { city: v.city || null, country: v.country?.toUpperCase() || null } : null,
          is_synthetic: v.is_synthetic === "on",
        }, { onSuccess: onClose });
      }}>
        {create.error ? <Alert>{errorText(create.error)}</Alert> : null}
        <Field id="full_name" label="Full name"><Input {...fieldA11y("full_name")} required /></Field>
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="customer_type" label="Type">
            <Select {...fieldA11y("customer_type")}><option value="individual">Individual</option><option value="business">Business</option></Select>
          </Field>
          <Field id="external_ref" label="External reference"><Input {...fieldA11y("external_ref")} /></Field>
          <Field id="email" label="Email"><Input {...fieldA11y("email")} type="email" /></Field>
          <Field id="phone" label="Phone"><Input {...fieldA11y("phone")} type="tel" /></Field>
          <Field id="date_of_birth" label="Date of birth"><Input {...fieldA11y("date_of_birth")} type="date" /></Field>
          <Field id="city" label="City"><Input {...fieldA11y("city")} /></Field>
          <Field id="country" label="Country code"><Input {...fieldA11y("country")} maxLength={2} /></Field>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" name="is_synthetic" defaultChecked className="accent-[var(--color-primary)]" />
          Synthetic test record
        </label>
        <div className="flex justify-end"><Button type="submit" disabled={create.isPending}>Create customer</Button></div>
      </form>
    </Dialog>
  );
}

/** New policy with an initial version and a single coverage backed by one clause. */
export function PolicyDialog({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const create = useCreatePolicy();
  const products = useProducts();
  const [customerQuery, setCustomerQuery] = useState("");
  const customers = useCustomers(customerQuery);
  return (
    <Dialog open={open} onClose={() => { create.reset(); onClose(); }} title="New policy"
      description="Creates the policy with its first version. More detailed terms can be loaded with JSON import.">
      <form className="space-y-3" onSubmit={(e) => {
        e.preventDefault();
        const v = values(e);
        create.mutate({
          policy_number: v.policy_number, customer_id: v.customer_id, product_id: v.product_id,
          is_synthetic: v.is_synthetic === "on", publish_initial_version: v.publish === "on",
          initial_version: {
            effective_from: v.effective_from, effective_to: v.effective_to, sum_insured: v.sum_insured,
            deductible_amount: v.deductible_amount || "0", waiting_period_days: Number(v.waiting_period_days || 0),
            clauses: [{ clause_ref: "1.1", section: "coverage", title: v.coverage_title || "Cover", text: v.clause_text }],
            coverages: [{ coverage_code: v.coverage_code, perils: (v.perils ?? "").split(",").map((p) => p.trim()).filter(Boolean),
              limit_amount: v.sum_insured, clause_ref: "1.1" }],
          },
        }, { onSuccess: (policy) => { onClose(); onCreated(policy.id); } });
      }}>
        {create.error ? <Alert>{errorText(create.error)}</Alert> : null}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field id="policy_number" label="Policy number"><Input {...fieldA11y("policy_number")} required /></Field>
          <Field id="product_id" label="Product">
            <Select {...fieldA11y("product_id")} required>
              {products.data?.map((p) => <option key={p.id} value={p.id}>{p.name} ({p.currency})</option>)}
            </Select>
          </Field>
          <Field id="customer-q" label="Find customer"><Input id="customer-q" value={customerQuery} onChange={(e) => setCustomerQuery(e.target.value)} /></Field>
          <Field id="customer_id" label="Customer">
            <Select {...fieldA11y("customer_id")} required>
              {customers.data?.items.map((c) => <option key={c.id} value={c.id}>{c.full_name}</option>)}
            </Select>
          </Field>
          <Field id="effective_from" label="Effective from"><Input {...fieldA11y("effective_from")} type="date" required /></Field>
          <Field id="effective_to" label="Effective to (exclusive)"><Input {...fieldA11y("effective_to")} type="date" required /></Field>
          <Field id="sum_insured" label="Sum insured"><Input {...fieldA11y("sum_insured")} required inputMode="decimal" /></Field>
          <Field id="deductible_amount" label="Deductible"><Input {...fieldA11y("deductible_amount")} inputMode="decimal" defaultValue="0" /></Field>
          <Field id="waiting_period_days" label="Waiting period (days)"><Input {...fieldA11y("waiting_period_days")} type="number" min={0} defaultValue={0} /></Field>
          <Field id="coverage_code" label="Coverage code"><Input {...fieldA11y("coverage_code")} required defaultValue="own_damage" /></Field>
        </div>
        <Field id="perils" label="Perils covered" hint="Comma-separated, e.g. collision, fire, theft"><Input {...fieldA11y("perils", undefined, "x")} required /></Field>
        <Field id="coverage_title" label="Clause title"><Input {...fieldA11y("coverage_title")} defaultValue="Cover" /></Field>
        <Field id="clause_text" label="Clause wording">
          <textarea {...fieldA11y("clause_text")} required rows={3} className="w-full rounded-md border border-input bg-surface px-3 py-2 text-sm" />
        </Field>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="checkbox" name="publish" defaultChecked className="accent-[var(--color-primary)]" />Publish immediately</label>
          <label className="flex items-center gap-2"><input type="checkbox" name="is_synthetic" defaultChecked className="accent-[var(--color-primary)]" />Synthetic test record</label>
        </div>
        <div className="flex justify-end"><Button type="submit" disabled={create.isPending}>Create policy</Button></div>
      </form>
    </Dialog>
  );
}
