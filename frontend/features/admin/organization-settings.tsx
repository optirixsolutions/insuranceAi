"use client";

import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, fieldA11y } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { hasPermission, useMe } from "@/features/auth/use-me";
import { ApiError } from "@/lib/api/client";

import { useOrganization, useUpdateOrganization, type Organization } from "./api";

function OrganizationForm({ org, canEdit }: { org: Organization; canEdit: boolean }) {
  const update = useUpdateOrganization();
  const [saved, setSaved] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaved(false);
    const data = new FormData(event.currentTarget);
    const name = String(data.get("name") ?? "").trim();
    const currency = String(data.get("default_currency") ?? "").trim().toUpperCase();
    const timezone = String(data.get("default_timezone") ?? "").trim();
    const jurisdiction = String(data.get("jurisdiction_code") ?? "").trim();
    const next: Record<string, string> = {};
    if (name.length < 2) next.name = "Enter at least 2 characters";
    if (!/^[A-Z]{3}$/.test(currency)) next.default_currency = "Use a 3-letter ISO 4217 code, e.g. USD";
    if (!timezone) next.default_timezone = "Enter a time zone, e.g. Europe/London";
    setErrors(next);
    if (Object.keys(next).length) return;
    update.mutate(
      {
        version: org.row_version,
        changes: { name, default_currency: currency, default_timezone: timezone, jurisdiction_code: jurisdiction || null },
      },
      { onSuccess: () => setSaved(true) },
    );
  }

  const conflict = update.error instanceof ApiError && update.error.status === 412;
  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4">
      {update.error ? (
        <Alert>{conflict ? "Someone else changed these settings. Reload the page to see the latest values." : update.error.message}</Alert>
      ) : null}
      {saved ? <Alert tone="success">Organisation settings saved.</Alert> : null}
      <fieldset disabled={!canEdit || update.isPending} className="grid gap-4 sm:grid-cols-2">
        <Field id="name" label="Organisation name" error={errors.name}>
          <Input {...fieldA11y("name", errors.name)} defaultValue={org.name} />
        </Field>
        <Field id="slug" label="Identifier">
          <Input id="slug" value={org.slug} readOnly className="font-mono text-xs" />
        </Field>
        <Field id="default_currency" label="Default currency" error={errors.default_currency}>
          <Input {...fieldA11y("default_currency", errors.default_currency)} defaultValue={org.default_currency} maxLength={3} />
        </Field>
        <Field id="default_timezone" label="Default time zone" error={errors.default_timezone}>
          <Input {...fieldA11y("default_timezone", errors.default_timezone)} defaultValue={org.default_timezone} />
        </Field>
        <Field id="jurisdiction_code" label="Jurisdiction code" hint="Selects the compliance profile from Phase 6 onwards.">
          <Input {...fieldA11y("jurisdiction_code", undefined, "x")} defaultValue={org.jurisdiction_code ?? ""} maxLength={16} />
        </Field>
        <Field id="ai_approval" label="AI processing of real data">
          <Input id="ai_approval" readOnly value={org.ai_data_processing_approved ? "Approved" : "Not approved: synthetic data only"} />
        </Field>
      </fieldset>
      {canEdit ? (
        <div className="flex justify-end">
          <Button type="submit" disabled={update.isPending}>{update.isPending ? "Saving…" : "Save changes"}</Button>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">Only organisation administrators can change these settings.</p>
      )}
    </form>
  );
}

export function OrganizationSettings() {
  const org = useOrganization();
  const canEdit = hasPermission(useMe().data, "org.manage");
  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Organisation profile</CardTitle>
          <CardDescription>Defaults applied to claims and reports in this organisation.</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {org.data ? (
          <OrganizationForm key={org.data.row_version} org={org.data} canEdit={canEdit} />
        ) : org.error ? (
          <Alert>{org.error.message}</Alert>
        ) : (
          <Skeleton className="h-48" />
        )}
      </CardContent>
    </Card>
  );
}
