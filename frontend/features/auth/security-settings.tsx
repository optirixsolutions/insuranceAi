"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Alert, Field, Input, fieldA11y } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { apiGet, apiRequest, noContent } from "@/lib/api/client";

import { ROLE_LABELS } from "./schema";
import { hasPermission, useMe } from "./use-me";

const statusSchema = z.object({
  enabled: z.boolean(),
  confirmed_at: z.string().nullable(),
  recovery_codes_left: z.number().int(),
  required: z.boolean(),
});
const setupSchema = z.object({ secret: z.string(), otpauth_uri: z.string() });
const recoverySchema = z.object({ recovery_codes: z.array(z.string()) });
const policySchema = z.object({ required_roles: z.array(z.string()) });

const MFA_KEY = ["auth", "mfa"] as const;
const POLICY_KEY = ["organization", "mfa-policy"] as const;

function formValue(event: FormEvent<HTMLFormElement>, name: string) {
  return String(new FormData(event.currentTarget).get(name) ?? "").trim();
}

function RecoveryCodes({ codes, onDone }: { codes: string[]; onDone: () => void }) {
  return (
    <div className="space-y-3">
      <Alert tone="success">Two-factor authentication is on.</Alert>
      <p className="text-sm">
        Save these recovery codes somewhere safe. Each one signs you in once if you lose your phone. They are shown only now.
      </p>
      <ul aria-label="Recovery codes" className="grid grid-cols-2 gap-2 rounded-md border border-border p-3 font-mono text-sm sm:grid-cols-5">
        {codes.map((c) => (
          <li key={c}>{c}</li>
        ))}
      </ul>
      <Button type="button" onClick={onDone}>
        I have saved them
      </Button>
    </div>
  );
}

function Enrol({ onEnabled }: { onEnabled: (codes: string[]) => void }) {
  const setup = useMutation({ mutationFn: () => apiRequest("POST", "/auth/mfa/setup", setupSchema) });
  const confirm = useMutation({
    mutationFn: (code: string) => apiRequest("POST", "/auth/mfa/confirm", recoverySchema, { body: { code } }),
    onSuccess: (data) => onEnabled(data.recovery_codes),
  });
  const [error, setError] = useState<string | undefined>();

  if (!setup.data) {
    return (
      <div className="space-y-3">
        {setup.error ? <Alert>{setup.error.message}</Alert> : null}
        <p className="text-sm text-muted-foreground">
          Use an authenticator app (for example Google Authenticator, Microsoft Authenticator or 1Password) that shows a new
          code every 30 seconds.
        </p>
        <Button type="button" onClick={() => setup.mutate()} disabled={setup.isPending}>
          {setup.isPending ? "Preparing…" : "Set up two-factor authentication"}
        </Button>
      </div>
    );
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const code = formValue(event, "code").replace(/\s/g, "");
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the 6-digit code from your app");
      return;
    }
    setError(undefined);
    confirm.mutate(code);
  }

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-4">
      {confirm.error ? <Alert>{confirm.error.message}</Alert> : null}
      <ol className="list-decimal space-y-2 pl-5 text-sm">
        <li>
          In your authenticator app, add an account and enter this setup key:
          <code data-testid="mfa-secret" className="mt-1 block break-all rounded bg-surface-muted px-2 py-1 font-mono text-sm">
            {setup.data.secret}
          </code>
          <span className="mt-1 block text-xs text-muted-foreground">
            Or, on this device, open the{" "}
            <a href={setup.data.otpauth_uri} className="text-primary hover:underline">
              setup link
            </a>
            . Never share the key.
          </span>
        </li>
        <li>Enter the 6-digit code the app shows.</li>
      </ol>
      <Field id="code" label="Code from the app" error={error}>
        <Input {...fieldA11y("code", error)} inputMode="numeric" autoComplete="one-time-code" maxLength={7} required />
      </Field>
      <Button type="submit" disabled={confirm.isPending}>
        {confirm.isPending ? "Checking…" : "Turn on"}
      </Button>
    </form>
  );
}

function Disable({ required, onDone }: { required: boolean; onDone: () => void }) {
  const disable = useMutation({
    mutationFn: (body: { password: string; code: string }) => apiRequest("POST", "/auth/mfa/disable", noContent, { body }),
    onSuccess: onDone,
  });
  if (required) {
    return <p className="text-xs text-muted-foreground">Your organisation requires two-factor authentication for your role.</p>;
  }
  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    disable.mutate({ password: formValue(event, "mfa_password"), code: formValue(event, "mfa_code") });
  }
  return (
    <form onSubmit={onSubmit} noValidate className="space-y-3">
      {disable.error ? <Alert>{disable.error.message}</Alert> : null}
      <div className="grid gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <Field id="mfa_password" label="Password">
          <Input id="mfa_password" name="mfa_password" type="password" autoComplete="current-password" required />
        </Field>
        <Field id="mfa_code" label="Current code">
          <Input id="mfa_code" name="mfa_code" inputMode="numeric" autoComplete="one-time-code" maxLength={20} required />
        </Field>
        <Button type="submit" variant="secondary" disabled={disable.isPending}>
          {disable.isPending ? "Turning off…" : "Turn off"}
        </Button>
      </div>
    </form>
  );
}

function TwoFactorCard() {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: MFA_KEY, queryFn: () => apiGet("/auth/mfa", statusSchema) });
  const [codes, setCodes] = useState<string[] | null>(null);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: MFA_KEY });

  let body;
  if (codes) {
    body = (
      <RecoveryCodes
        codes={codes}
        onDone={() => {
          setCodes(null);
          // Everything else may have been refused until enrolment: refetch it all.
          void queryClient.invalidateQueries();
        }}
      />
    );
  } else if (status.error) {
    body = <Alert>{status.error.message}</Alert>;
  } else if (!status.data) {
    body = <Skeleton className="h-24" />;
  } else if (status.data.enabled) {
    const since = status.data.confirmed_at ? new Date(status.data.confirmed_at).toLocaleDateString() : "";
    body = (
      <div className="space-y-4">
        <Alert tone="success">
          On since {since}. Recovery codes left: {status.data.recovery_codes_left}.
        </Alert>
        <Disable required={status.data.required} onDone={refresh} />
      </div>
    );
  } else {
    body = (
      <div className="space-y-3">
        {status.data.required ? (
          <Alert>Your organisation requires two-factor authentication for your role. Set it up to continue working.</Alert>
        ) : null}
        <Enrol
          onEnabled={(c) => {
            setCodes(c);
            refresh();
          }}
        />
      </div>
    );
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Two-factor authentication</CardTitle>
          <CardDescription>Ask for a code from your phone as well as your password when you sign in.</CardDescription>
        </div>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}

function PolicyCard() {
  const queryClient = useQueryClient();
  const policy = useQuery({ queryKey: POLICY_KEY, queryFn: () => apiGet("/organization/mfa-policy", policySchema) });
  const save = useMutation({
    mutationFn: (roles: string[]) =>
      apiRequest("PUT", "/organization/mfa-policy", policySchema, { body: { required_roles: roles } }),
    onSuccess: (data) => queryClient.setQueryData(POLICY_KEY, data),
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate(new FormData(event.currentTarget).getAll("roles").map(String));
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>Organisation policy</CardTitle>
          <CardDescription>People with these roles must turn on two-factor authentication before they can use ClaimOS.</CardDescription>
        </div>
      </CardHeader>
      <CardContent>
        {policy.data ? (
          <form key={policy.data.required_roles.join(",")} onSubmit={onSubmit} className="space-y-4">
            {save.error ? <Alert>{save.error.message}</Alert> : null}
            {save.isSuccess ? <Alert tone="success">Policy saved.</Alert> : null}
            <fieldset className="grid gap-2 sm:grid-cols-2">
              <legend className="sr-only">Roles that require two-factor authentication</legend>
              {Object.entries(ROLE_LABELS).map(([key, label]) => (
                <label key={key} className="flex items-center gap-2 text-sm">
                  <input type="checkbox" name="roles" value={key} defaultChecked={policy.data.required_roles.includes(key)} />
                  {label}
                </label>
              ))}
            </fieldset>
            <Button type="submit" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save policy"}
            </Button>
          </form>
        ) : policy.error ? (
          <Alert>{policy.error.message}</Alert>
        ) : (
          <Skeleton className="h-24" />
        )}
      </CardContent>
    </Card>
  );
}

export function SecuritySettings() {
  const canManage = hasPermission(useMe().data, "org.manage");
  return (
    <div className="space-y-6">
      <TwoFactorCard />
      {canManage ? <PolicyCard /> : null}
    </div>
  );
}
