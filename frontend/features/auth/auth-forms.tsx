"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Alert, Field, Input, fieldA11y } from "@/components/ui/form";
import { ApiError, apiRequest } from "@/lib/api/client";

import {
  fieldErrors,
  loginFormSchema,
  meSchema,
  PASSWORD_MIN,
  registerFormSchema,
  setPasswordFormSchema,
} from "./schema";
import { ME_QUERY_KEY, safeNextPath } from "./use-me";

type Errors = Record<string, string>;

function formValues(event: FormEvent<HTMLFormElement>) {
  return Object.fromEntries(new FormData(event.currentTarget).entries());
}

function useSubmit<S extends z.ZodTypeAny>(schema: S, send: (values: z.infer<S>) => Promise<unknown>) {
  const [errors, setErrors] = useState<Errors>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const parsed = schema.safeParse(formValues(event));
    if (!parsed.success) {
      setErrors(fieldErrors(parsed.error));
      return;
    }
    setErrors({});
    setPending(true);
    try {
      await send(parsed.data);
    } catch (error) {
      setFormError(error instanceof ApiError ? error.userMessage : "Something went wrong. Try again.");
      setPending(false);
    }
  }
  return { errors, formError, pending, onSubmit };
}

function useSignedIn() {
  const queryClient = useQueryClient();
  const router = useRouter();
  const next = safeNextPath(useSearchParams().get("next"));
  return (me: unknown) => {
    queryClient.setQueryData(ME_QUERY_KEY, me);
    router.replace(next);
  };
}

const loginResultSchema = z.union([meSchema, z.object({ mfa_required: z.literal(true), challenge: z.string() })]);
const mfaCodeSchema = z.object({
  code: z
    .string()
    .trim()
    .min(6, "Enter the 6-digit code or a recovery code")
    .max(20, "Enter the 6-digit code or a recovery code"),
});

export function LoginForm() {
  const signedIn = useSignedIn();
  const [challenge, setChallenge] = useState<string | null>(null);
  const { errors, formError, pending, onSubmit } = useSubmit(loginFormSchema, async (values) => {
    const result = await apiRequest("POST", "/auth/login", loginResultSchema, { body: values });
    if ("mfa_required" in result) setChallenge(result.challenge);
    else signedIn(result);
  });
  if (challenge) return <MfaCodeForm challenge={challenge} onRestart={() => setChallenge(null)} />;
  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {formError ? <Alert>{formError}</Alert> : null}
      <Field id="email" label="Work email" error={errors.email}>
        <Input {...fieldA11y("email", errors.email)} type="email" autoComplete="username" required autoFocus />
      </Field>
      <Field id="password" label="Password" error={errors.password}>
        <Input {...fieldA11y("password", errors.password)} type="password" autoComplete="current-password" required />
      </Field>
      <Button type="submit" className="w-full" disabled={pending}>
        {pending ? "Signing in…" : "Sign in"}
      </Button>
      <p className="text-center text-sm text-muted-foreground">
        New organisation?{" "}
        <Link href="/register" className="font-medium text-primary hover:underline">
          Create an account
        </Link>
      </p>
    </form>
  );
}

/** Second sign-in step for accounts with two-factor authentication. */
function MfaCodeForm({ challenge, onRestart }: { challenge: string; onRestart: () => void }) {
  const signedIn = useSignedIn();
  const { errors, formError, pending, onSubmit } = useSubmit(mfaCodeSchema, async (values) =>
    signedIn(await apiRequest("POST", "/auth/mfa/verify", meSchema, { body: { challenge, code: values.code } })),
  );
  const hint = "From your authenticator app. You can also use one of your recovery codes.";
  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {formError ? <Alert>{formError}</Alert> : null}
      <Field id="code" label="Authentication code" error={errors.code} hint={hint}>
        <Input
          {...fieldA11y("code", errors.code, hint)}
          inputMode="numeric"
          autoComplete="one-time-code"
          required
          autoFocus
          maxLength={20}
        />
      </Field>
      <Button type="submit" className="w-full" disabled={pending}>
        {pending ? "Checking…" : "Verify"}
      </Button>
      <p className="text-center text-sm text-muted-foreground">
        <button type="button" onClick={onRestart} className="font-medium text-primary hover:underline">
          Use a different account
        </button>
      </p>
    </form>
  );
}

export function RegisterForm() {
  const signedIn = useSignedIn();
  const { errors, formError, pending, onSubmit } = useSubmit(registerFormSchema, async (values) =>
    signedIn(await apiRequest("POST", "/auth/register", meSchema, { body: values })),
  );
  const hint = `At least ${PASSWORD_MIN} characters. A passphrase works well.`;
  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {formError ? <Alert>{formError}</Alert> : null}
      <Field id="organization_name" label="Organisation name" error={errors.organization_name}>
        <Input {...fieldA11y("organization_name", errors.organization_name)} autoComplete="organization" required autoFocus />
      </Field>
      <Field id="display_name" label="Your name" error={errors.display_name}>
        <Input {...fieldA11y("display_name", errors.display_name)} autoComplete="name" required />
      </Field>
      <Field id="email" label="Work email" error={errors.email}>
        <Input {...fieldA11y("email", errors.email)} type="email" autoComplete="username" required />
      </Field>
      <Field id="password" label="Password" error={errors.password} hint={hint}>
        <Input {...fieldA11y("password", errors.password, hint)} type="password" autoComplete="new-password" required />
      </Field>
      <Button type="submit" className="w-full" disabled={pending}>
        {pending ? "Creating organisation…" : "Create organisation"}
      </Button>
      <p className="text-center text-sm text-muted-foreground">
        Already have an account?{" "}
        <Link href="/login" className="font-medium text-primary hover:underline">
          Sign in
        </Link>
      </p>
    </form>
  );
}

/** Reads the one-time token from the URL fragment (never sent to the server in the URL). */
export function AcceptInviteForm() {
  const signedIn = useSignedIn();
  const [token, setToken] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    const value = new URLSearchParams(window.location.hash.slice(1)).get("token");
    // Remove the token from the address bar and history once read.
    window.history.replaceState(null, "", window.location.pathname);
    // eslint-disable-next-line react-hooks/set-state-in-effect -- the fragment is only available in the browser
    setToken(value);
  }, []);
  const { errors, formError, pending, onSubmit } = useSubmit(setPasswordFormSchema, async (values) =>
    signedIn(await apiRequest("POST", "/auth/invitations/accept", meSchema, { body: { token, password: values.password } })),
  );

  if (token === undefined) return null;
  if (!token) return <Alert>This link is incomplete. Open the full link you were sent, or ask an administrator for a new one.</Alert>;
  const hint = `At least ${PASSWORD_MIN} characters.`;
  return (
    <form method="post" onSubmit={onSubmit} noValidate className="space-y-4">
      {formError ? <Alert>{formError}</Alert> : null}
      <Field id="password" label="New password" error={errors.password} hint={hint}>
        <Input {...fieldA11y("password", errors.password, hint)} type="password" autoComplete="new-password" required autoFocus />
      </Field>
      <Field id="confirm" label="Confirm password" error={errors.confirm}>
        <Input {...fieldA11y("confirm", errors.confirm)} type="password" autoComplete="new-password" required />
      </Field>
      <Button type="submit" className="w-full" disabled={pending}>
        {pending ? "Saving…" : "Set password and sign in"}
      </Button>
    </form>
  );
}
