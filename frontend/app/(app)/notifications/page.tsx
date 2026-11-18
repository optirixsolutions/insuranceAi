import type { Metadata } from "next";

import { NotificationsList } from "@/features/communications/notifications";

export const metadata: Metadata = { title: "Notifications" };

export default function NotificationsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Notifications</h1>
      <NotificationsList />
    </div>
  );
}
