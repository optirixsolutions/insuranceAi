"use client";

import { FileJson, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { humanize } from "@/features/claims/format";
import { ApiError } from "@/lib/api/client";
import { cn } from "@/lib/utils";

import { useCustomers, useImportPolicies, usePolicies, useProducts } from "./api";
import { CustomerDialog, PolicyDialog, ProductDialog } from "./catalog-forms";

const SECTIONS = ["Policies", "Customers", "Products", "Import"] as const;
type Section = (typeof SECTIONS)[number];

function Table({ head, children }: { head: string[]; children: React.ReactNode }) {
  return (
    <Card className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-sm">
        <thead>
          <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
            {head.map((h) => <th key={h} scope="col" className="px-4 py-2.5 font-medium">{h}</th>)}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </Card>
  );
}

function PoliciesSection({ canManage }: { canManage: boolean }) {
  const router = useRouter();
  const [q, setQ] = useState("");
  const policies = usePolicies({ q });
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="sm:w-80"><Field id="policy-q" label="Search"><Input id="policy-q" type="search" placeholder="Policy number or customer" value={q} onChange={(e) => setQ(e.target.value)} /></Field></div>
        {canManage ? <Button onClick={() => setOpen(true)}><Plus aria-hidden />New policy</Button> : null}
      </div>
      <Table head={["Policy", "Customer", "Product", "Status"]}>
        {policies.isPending ? <tr><td colSpan={4} className="p-4"><Skeleton className="h-20" /></td></tr>
          : policies.data?.items.length === 0 ? <tr><td colSpan={4} className="p-6 text-center text-muted-foreground">No policies yet.</td></tr>
          : policies.data?.items.map((p) => (
            <tr key={p.id} className="border-b border-border last:border-0">
              <td className="px-4 py-3"><Link href={`/policies/${p.id}`} className="font-mono text-primary hover:underline">{p.policy_number}</Link>{p.is_synthetic ? <Badge className="ml-2">synthetic</Badge> : null}</td>
              <td className="px-4 py-3">{p.customer_name}</td>
              <td className="px-4 py-3">{p.product_name} <span className="text-xs text-muted-foreground">({p.currency})</span></td>
              <td className="px-4 py-3"><Badge tone={p.status === "active" ? "success" : "neutral"}>{p.status}</Badge></td>
            </tr>
          ))}
      </Table>
      <PolicyDialog open={open} onClose={() => setOpen(false)} onCreated={(id) => router.push(`/policies/${id}`)} />
    </div>
  );
}

function CustomersSection({ canManage }: { canManage: boolean }) {
  const [q, setQ] = useState("");
  const customers = useCustomers(q);
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="sm:w-80"><Field id="customer-q" label="Search"><Input id="customer-q" type="search" value={q} onChange={(e) => setQ(e.target.value)} /></Field></div>
        {canManage ? <Button onClick={() => setOpen(true)}><Plus aria-hidden />New customer</Button> : null}
      </div>
      <Table head={["Name", "Reference", "Contact", "Type"]}>
        {customers.data?.items.map((c) => (
          <tr key={c.id} className="border-b border-border last:border-0">
            <td className="px-4 py-3 font-medium">{c.full_name}{c.is_synthetic ? <Badge className="ml-2">synthetic</Badge> : null}</td>
            <td className="px-4 py-3 font-mono text-xs">{c.external_ref ?? "—"}</td>
            <td className="px-4 py-3 text-muted-foreground">{c.email ?? c.phone ?? "—"}</td>
            <td className="px-4 py-3">{humanize(c.customer_type)}</td>
          </tr>
        ))}
      </Table>
      <CustomerDialog open={open} onClose={() => setOpen(false)} />
    </div>
  );
}

function ProductsSection({ canManage }: { canManage: boolean }) {
  const products = useProducts();
  const [open, setOpen] = useState(false);
  return (
    <div className="space-y-4">
      {canManage ? <div className="flex justify-end"><Button onClick={() => setOpen(true)}><Plus aria-hidden />New product</Button></div> : null}
      <Table head={["Product", "Line", "Currency", "Required documents"]}>
        {products.data?.map((p) => (
          <tr key={p.id} className="border-b border-border last:border-0 align-top">
            <td className="px-4 py-3"><p className="font-medium">{p.name}</p><p className="font-mono text-xs text-muted-foreground">{p.code}</p></td>
            <td className="px-4 py-3">{humanize(p.line)}</td>
            <td className="px-4 py-3">{p.currency}</td>
            <td className="px-4 py-3 text-xs">{p.document_requirements.filter((r) => r.mandatory).map((r) => humanize(r.document_type)).join(", ")}</td>
          </tr>
        ))}
      </Table>
      <ProductDialog open={open} onClose={() => setOpen(false)} />
    </div>
  );
}

function ImportSection() {
  const importer = useImportPolicies();
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);

  function run(dryRun: boolean) {
    setParseError(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setParseError("The file is not valid JSON.");
      return;
    }
    const body = Array.isArray(parsed) ? { policies: parsed } : (parsed as Record<string, unknown>);
    importer.mutate({ ...body, dry_run: dryRun });
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Import policies (JSON)</CardTitle>
          <CardDescription>All-or-nothing: validate first with a dry run, then import. Nothing is saved if any policy is invalid.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <input type="file" accept="application/json,.json" aria-label="Choose JSON file" className="text-sm"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) void f.text().then(setText); }} />
        <textarea aria-label="Import JSON" rows={10} value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
          className="w-full rounded-md border border-input bg-surface px-3 py-2 font-mono text-xs" placeholder='{"policies": [ … ]}' />
        {parseError ? <Alert>{parseError}</Alert> : null}
        {importer.error ? <Alert>{importer.error instanceof ApiError ? importer.error.userMessage : "Import failed."}</Alert> : null}
        {importer.data ? (
          <Alert tone="success">
            {importer.data.dry_run ? "Dry run passed: " : "Imported: "}
            {importer.data.policies_created} policies, {importer.data.customers_created} new customers, {importer.data.versions_published} published versions.
          </Alert>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button variant="secondary" disabled={!text || importer.isPending} onClick={() => run(true)}><FileJson aria-hidden />Validate (dry run)</Button>
          <Button disabled={!text || importer.isPending} onClick={() => run(false)}>Import</Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function PolicyManagement() {
  const me = useMe().data;
  const canPolicies = hasPermission(me, "policies.manage");
  const canProducts = hasPermission(me, "products.manage");
  const sections = SECTIONS.filter((s) => s !== "Import" || canPolicies);
  const [section, setSection] = useState<Section>("Policies");
  return (
    <div className="space-y-6">
      <nav aria-label="Policy management sections" className="flex gap-1 border-b border-border">
        {sections.map((s) => (
          <button key={s} type="button" aria-current={section === s ? "page" : undefined} onClick={() => setSection(s)}
            className={cn("-mb-px border-b-2 px-3 py-2 text-sm font-medium",
              section === s ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")}>
            {s}
          </button>
        ))}
      </nav>
      {section === "Policies" ? <PoliciesSection canManage={canPolicies} /> : null}
      {section === "Customers" ? <CustomersSection canManage={canPolicies} /> : null}
      {section === "Products" ? <ProductsSection canManage={canProducts} /> : null}
      {section === "Import" ? <ImportSection /> : null}
    </div>
  );
}
