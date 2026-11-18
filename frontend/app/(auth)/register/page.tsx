import type { Metadata } from "next";
import { Suspense } from "react";

import { RegisterForm } from "@/features/auth/auth-forms";

export const metadata: Metadata = { title: "Create organisation" };

export default function RegisterPage() {
  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Create your organisation</h1>
      <p className="mb-6 mt-1 text-sm text-muted-foreground">
        You become its administrator and can invite your team afterwards.
      </p>
      <Suspense>
        <RegisterForm />
      </Suspense>
    </>
  );
}
