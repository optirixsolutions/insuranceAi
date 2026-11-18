import {
  Activity,
  Bot,
  ClipboardCheck,
  FileSearch,
  FilePlus2,
  FileText,
  Files,
  Gauge,
  LayoutDashboard,
  Plug,
  ScrollText,
  Settings,
  ShieldAlert,
  ShieldCheck,
  UserCheck,
  type LucideIcon,
} from "lucide-react";

/** The implementation phase the frontend currently ships. Areas from later phases are shown as unavailable. */
export const CURRENT_PHASE = 12;

export type NavItem = {
  label: string;
  href: string;
  icon: LucideIcon;
  /** Roadmap phase that delivers this area (docs/architecture/06-roadmap-and-dependencies.md). */
  phase: number;
  /** Hidden from users without this permission (the API enforces it regardless). */
  permission?: string;
};

export type NavGroup = { label: string; items: NavItem[] };

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Operations",
    items: [
      { label: "Overview", href: "/", icon: LayoutDashboard, phase: 1 },
      { label: "Claims", href: "/claims", icon: Files, phase: 3, permission: "claims.read" },
      { label: "New Claim", href: "/claims/new", icon: FilePlus2, phase: 3, permission: "claims.write" },
      { label: "AI Processing", href: "/processing", icon: Activity, phase: 7 },
      { label: "Human Review", href: "/reviews", icon: UserCheck, phase: 9, permission: "reviews.decide" },
    ],
  },
  {
    label: "Analysis",
    items: [
      { label: "Document Intelligence", href: "/documents", icon: FileSearch, phase: 4, permission: "documents.read" },
      { label: "Fraud Investigation", href: "/fraud", icon: ShieldAlert, phase: 9, permission: "fraud.investigate" }, // high-risk review queue
      { label: "Policy Management", href: "/policies", icon: ShieldCheck, phase: 3, permission: "claims.read" },
      { label: "Assessments", href: "/assessments", icon: ClipboardCheck, phase: 10, permission: "claims.read" },
      { label: "Reports", href: "/reports", icon: FileText, phase: 11, permission: "claims.read" },
      { label: "Analytics", href: "/analytics", icon: Gauge, phase: 10, permission: "claims.read" },
    ],
  },
  {
    label: "Administration",
    items: [
      { label: "Audit Logs", href: "/audit", icon: ScrollText, phase: 2, permission: "audit.read" },
      { label: "AI Agents", href: "/agents", icon: Bot, phase: 5, permission: "ai.manage" },
      { label: "Integrations", href: "/integrations", icon: Plug, phase: 11, permission: "org.manage" },
      { label: "Settings", href: "/settings", icon: Settings, phase: 2 },
    ],
  },
];

export function isAvailable(item: NavItem, currentPhase: number = CURRENT_PHASE): boolean {
  return item.phase <= currentPhase;
}

export function isPermitted(item: NavItem, permissions: readonly string[]): boolean {
  return !item.permission || permissions.includes(item.permission);
}

function matches(href: string, pathname: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

/** Active only for the most specific matching item (so /claims/new is not also "Claims"). */
export function isActive(item: NavItem, pathname: string): boolean {
  if (!matches(item.href, pathname)) return false;
  const all = NAV_GROUPS.flatMap((g) => g.items);
  return !all.some((other) => other.href.length > item.href.length && matches(other.href, pathname));
}
