import type { Metadata } from "next";

import { RuleSetsAdmin } from "@/features/admin/rule-sets-admin";

export const metadata: Metadata = { title: "Products & rules" };

export default function RulesPage() {
  return <RuleSetsAdmin />;
}
