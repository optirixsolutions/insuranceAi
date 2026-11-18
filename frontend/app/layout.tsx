import { GeistMono } from "geist/font/mono";
import { GeistSans } from "geist/font/sans";
import type { Metadata } from "next";
import type { ReactNode } from "react";

import { Providers } from "./providers";

import "./globals.css";

// Every request is rendered dynamically so Next.js can apply the per-request CSP nonce (see proxy.ts).
export const dynamic = "force-dynamic";

export const metadata: Metadata = {
  title: { default: "ClaimOS", template: "%s · ClaimOS" },
  description: "Evidence-driven insurance claims automation",
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${GeistSans.variable} ${GeistMono.variable}`}>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
