import type { Metadata } from "next";
import { Suspense } from "react";

import { AcceptInviteForm } from "@/features/auth/auth-forms";

export const metadata: Metadata = { title: "Set your password" };

export default function AcceptInvitePage() {
  return (
    <>
      <h1 className="text-xl font-semibold tracking-tight">Set your password</h1>
      <p className="mb-6 mt-1 text-sm text-muted-foreground">
        Finish setting up your ClaimOS account. This link works once.
      </p>
      <Suspense>
        <AcceptInviteForm />
      </Suspense>
    </>
  );
}
