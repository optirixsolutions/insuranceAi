import type { Metadata } from "next";
import { Suspense } from "react";

import { LoginForm } from "@/features/auth/auth-forms";

export const metadata: Metadata = { title: "Sign in" };

export default function LoginPage() {
  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
      <p className="mb-6 mt-1 text-sm text-muted-foreground">Use your ClaimOS work account.</p>
      <Suspense>
        <LoginForm />
      </Suspense>
    </>
  );
}
