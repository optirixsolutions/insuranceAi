"use client";

import { Bell } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Alert } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDateTime } from "@/features/claims/format";

import { useInbox, useMarkRead, type AppNotification } from "./api";

function Item({ n, onOpen }: { n: AppNotification; onOpen: (n: AppNotification) => void }) {
  const content = (
    <>
      <p className={`text-sm ${n.read_at ? "" : "font-semibold"}`}>{n.title}</p>
      <p className="text-xs text-muted-foreground">{n.body}</p>
      <p className="mt-0.5 text-[11px] text-muted-foreground">{formatDateTime(n.created_at)}</p>
    </>
  );
  return (
    <li className="border-b border-border last:border-0" data-testid="notification">
      {n.link ? (
        <Link href={n.link} onClick={() => onOpen(n)} className="block px-3 py-2 hover:bg-surface-muted">{content}</Link>
      ) : (
        <button type="button" onClick={() => onOpen(n)} className="block w-full px-3 py-2 text-left hover:bg-surface-muted">{content}</button>
      )}
    </li>
  );
}

export function NotificationBell() {
  const inbox = useInbox();
  const mark = useMarkRead();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);
  const unread = inbox.data?.unread ?? 0;
  const openItem = (n: AppNotification) => { if (!n.read_at) mark.mutate(n.id); setOpen(false); };
  return (
    <div className="relative" ref={ref}>
      <Button variant="ghost" size="icon" aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`} aria-expanded={open}
        onClick={() => setOpen(!open)} data-testid="notification-bell">
        <Bell aria-hidden />
        {unread ? (
          <span className="absolute right-1 top-1 min-w-4 rounded-full bg-danger px-1 text-[10px] font-semibold leading-4 text-white" data-testid="unread-count">
            {unread > 99 ? "99+" : unread}
          </span>
        ) : null}
      </Button>
      {open ? (
        <div className="absolute right-0 z-50 mt-2 w-80 rounded-md border border-border bg-surface shadow-lg" role="dialog" aria-label="Notifications">
          <div className="flex items-center justify-between border-b border-border px-3 py-2">
            <span className="text-sm font-medium">Notifications</span>
            {unread ? <button type="button" className="text-xs text-primary hover:underline" onClick={() => mark.mutate(undefined)}>Mark all read</button> : null}
          </div>
          {inbox.data?.items.length ? (
            <ul className="max-h-96 overflow-y-auto">{inbox.data.items.slice(0, 8).map((n) => <Item key={n.id} n={n} onOpen={openItem} />)}</ul>
          ) : <p className="px-3 py-4 text-sm text-muted-foreground">Nothing yet.</p>}
          <Link href="/notifications" onClick={() => setOpen(false)} className="block border-t border-border px-3 py-2 text-center text-sm text-primary hover:underline">All notifications</Link>
        </div>
      ) : null}
    </div>
  );
}

export function NotificationsList() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const inbox = useInbox(unreadOnly);
  const mark = useMarkRead();
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} /> Unread only
        </label>
        <Button size="sm" variant="secondary" onClick={() => mark.mutate(undefined)} disabled={!inbox.data?.unread}>Mark all read</Button>
      </div>
      {inbox.isPending ? <Skeleton className="h-48" /> : inbox.error ? <Alert>Could not load notifications.</Alert> : (
        <Card>
          <CardContent className="p-0">
            {inbox.data.items.length ? (
              <ul data-testid="notifications-list">{inbox.data.items.map((n) => <Item key={n.id} n={n} onOpen={(x) => !x.read_at && mark.mutate(x.id)} />)}</ul>
            ) : <p className="p-4 text-sm text-muted-foreground">No notifications.</p>}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
