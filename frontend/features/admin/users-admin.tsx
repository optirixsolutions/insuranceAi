"use client";

import { Copy, KeyRound, ShieldCheck, UserPlus } from "lucide-react";
import { useState, type FormEvent } from "react";

import { Badge, type BadgeTone } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog } from "@/components/ui/dialog";
import { Alert, Field, Input, Select, fieldA11y } from "@/components/ui/form";
import { Skeleton } from "@/components/ui/skeleton";
import { ROLE_LABELS, emailField } from "@/features/auth/schema";
import { useMe } from "@/features/auth/use-me";
import { ApiError } from "@/lib/api/client";

import {
  useInviteUser,
  useResetAccess,
  useRoles,
  useSetUserRoles,
  useSetUserStatus,
  useUsers,
  type AccessLink,
  type User,
} from "./api";

const STATUS_TONE: Record<User["status"], BadgeTone> = { active: "success", invited: "info", disabled: "neutral" };
const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
const errorText = (e: unknown) => (e instanceof ApiError ? e.userMessage : "Something went wrong. Try again.");

function RoleCheckboxes({ selected, onChange }: { selected: string[]; onChange: (roles: string[]) => void }) {
  const roles = useRoles();
  if (!roles.data) return <Skeleton className="h-24" />;
  return (
    <fieldset className="space-y-2">
      <legend className="mb-1.5 text-sm font-medium">Roles</legend>
      {roles.data.map((role) => (
        <label key={role.key} className="flex items-start gap-2.5 text-sm">
          <input
            type="checkbox"
            name="roles"
            value={role.key}
            className="mt-0.5 size-4 accent-[var(--color-primary)]"
            checked={selected.includes(role.key)}
            onChange={(e) => onChange(e.target.checked ? [...selected, role.key] : selected.filter((r) => r !== role.key))}
          />
          <span>
            <span className="font-medium">{role.name}</span>
            <span className="block text-xs text-muted-foreground">{role.description}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

function AccessLinkNotice({ link }: { link: AccessLink }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-3">
      <Alert tone="success">
        Share this one-time link with the user through a trusted channel. It expires{" "}
        {dateFormat.format(new Date(link.expires_at))} and will not be shown again.
      </Alert>
      <div className="flex gap-2">
        <Input readOnly value={link.url} aria-label="One-time access link" className="font-mono text-xs" onFocus={(e) => e.currentTarget.select()} />
        <Button
          type="button"
          variant="secondary"
          onClick={() => void navigator.clipboard.writeText(link.url).then(() => setCopied(true))}
        >
          <Copy aria-hidden />
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
    </div>
  );
}

function InviteDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const invite = useInviteUser();
  const [roles, setRoles] = useState<string[]>(["claims_adjuster"]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [link, setLink] = useState<AccessLink | null>(null);

  const close = () => {
    setLink(null);
    setErrors({});
    invite.reset();
    onClose();
  };

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const email = emailField.safeParse(data.get("email"));
    const name = String(data.get("display_name") ?? "").trim();
    const next: Record<string, string> = {};
    if (!email.success) next.email = email.error.issues[0]?.message ?? "Invalid email";
    if (!name) next.display_name = "Enter a name";
    if (roles.length === 0) next.roles = "Choose at least one role";
    setErrors(next);
    if (Object.keys(next).length || !email.success) return;
    const result = await invite.mutateAsync({ email: email.data, display_name: name, roles }).catch(() => null);
    if (result) setLink(result.access_link);
  }

  return (
    <Dialog open={open} onClose={close} title="Invite user" description="The user sets their own password from a one-time link.">
      {link ? (
        <div className="space-y-4">
          <AccessLinkNotice link={link} />
          <div className="flex justify-end">
            <Button onClick={close}>Done</Button>
          </div>
        </div>
      ) : (
        <form onSubmit={onSubmit} noValidate className="space-y-4">
          {invite.error ? <Alert>{errorText(invite.error)}</Alert> : null}
          <Field id="display_name" label="Name" error={errors.display_name}>
            <Input {...fieldA11y("display_name", errors.display_name)} required autoFocus />
          </Field>
          <Field id="email" label="Work email" error={errors.email}>
            <Input {...fieldA11y("email", errors.email)} type="email" required />
          </Field>
          <RoleCheckboxes selected={roles} onChange={setRoles} />
          {errors.roles ? <p className="text-xs text-danger">{errors.roles}</p> : null}
          <div className="flex justify-end gap-2">
            <Button type="button" variant="secondary" onClick={close}>Cancel</Button>
            <Button type="submit" disabled={invite.isPending}>{invite.isPending ? "Inviting…" : "Create invitation"}</Button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

function RolesDialog({ user, onClose }: { user: User | null; onClose: () => void }) {
  const setRoles = useSetUserRoles();
  const [roles, setSelected] = useState<string[]>(user?.roles ?? []);
  const close = () => {
    setRoles.reset();
    onClose();
  };
  return (
    <Dialog open={user !== null} onClose={close} title={`Roles for ${user?.display_name ?? ""}`}>
      <div className="space-y-4">
        {setRoles.error ? <Alert>{errorText(setRoles.error)}</Alert> : null}
        <RoleCheckboxes selected={roles} onChange={setSelected} />
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={close}>Cancel</Button>
          <Button
            disabled={setRoles.isPending || roles.length === 0}
            onClick={() => user && setRoles.mutate({ userId: user.id, roles }, { onSuccess: close })}
          >
            Save roles
          </Button>
        </div>
      </div>
    </Dialog>
  );
}

export function UsersAdmin() {
  const me = useMe().data;
  const [page, setPage] = useState(1);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const users = useUsers({ page, q, status });
  const [inviteOpen, setInviteOpen] = useState(false);
  const [editing, setEditing] = useState<User | null>(null);
  const [resetLink, setResetLink] = useState<AccessLink | null>(null);
  const setUserStatus = useSetUserStatus();
  const resetAccess = useResetAccess();
  const actionError = setUserStatus.error ?? resetAccess.error;
  const totalPages = users.data ? Math.max(1, Math.ceil(users.data.total / users.data.page_size)) : 1;

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="flex-1">
          <Field id="user-search" label="Search">
            <Input id="user-search" type="search" placeholder="Name or email" value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} />
          </Field>
        </div>
        <div className="sm:w-44">
          <Field id="user-status" label="Status">
            <Select id="user-status" value={status} onChange={(e) => { setStatus(e.target.value); setPage(1); }}>
              <option value="">All</option>
              <option value="active">Active</option>
              <option value="invited">Invited</option>
              <option value="disabled">Disabled</option>
            </Select>
          </Field>
        </div>
        <Button onClick={() => setInviteOpen(true)}>
          <UserPlus aria-hidden />
          Invite user
        </Button>
      </div>

      {actionError ? <Alert>{errorText(actionError)}</Alert> : null}
      {resetLink ? <AccessLinkNotice link={resetLink} /> : null}

      <Card className="overflow-x-auto">
        <table className="w-full min-w-[720px] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs uppercase tracking-wide text-muted-foreground">
              <th scope="col" className="px-4 py-2.5 font-medium">User</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Roles</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Status</th>
              <th scope="col" className="px-4 py-2.5 font-medium">Last sign-in</th>
              <th scope="col" className="px-4 py-2.5 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.isPending ? (
              <tr><td colSpan={5} className="p-4"><Skeleton className="h-20" /></td></tr>
            ) : users.error ? (
              <tr><td colSpan={5} className="p-4 text-danger" role="alert">{errorText(users.error)}</td></tr>
            ) : users.data.items.length === 0 ? (
              <tr><td colSpan={5} className="p-6 text-center text-muted-foreground">No users match these filters.</td></tr>
            ) : (
              users.data.items.map((user) => {
                const isSelf = user.id === me?.user.id;
                return (
                  <tr key={user.id} className="border-b border-border last:border-0" data-testid={`user-row-${user.email}`}>
                    <td className="px-4 py-3">
                      <p className="font-medium">{user.display_name}{isSelf ? <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span> : null}</p>
                      <p className="text-xs text-muted-foreground">{user.email}</p>
                    </td>
                    <td className="px-4 py-3 text-xs">{user.roles.map((r) => ROLE_LABELS[r] ?? r).join(", ")}</td>
                    <td className="px-4 py-3"><Badge tone={STATUS_TONE[user.status]}>{user.status}</Badge></td>
                    <td className="px-4 py-3 text-xs text-muted-foreground">{user.last_login_at ? dateFormat.format(new Date(user.last_login_at)) : "Never"}</td>
                    <td className="px-4 py-3">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="sm" disabled={isSelf} title={isSelf ? "You cannot change your own roles" : undefined} onClick={() => setEditing(user)}>
                          <ShieldCheck aria-hidden />Roles
                        </Button>
                        <Button variant="ghost" size="sm" disabled={isSelf || user.status === "disabled" || resetAccess.isPending}
                          onClick={() => resetAccess.mutate(user.id, { onSuccess: setResetLink })}>
                          <KeyRound aria-hidden />Reset access
                        </Button>
                        <Button variant="ghost" size="sm" disabled={isSelf || setUserStatus.isPending}
                          onClick={() => setUserStatus.mutate({ user, status: user.status === "disabled" ? "active" : "disabled" })}>
                          {user.status === "disabled" ? "Enable" : "Disable"}
                        </Button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </Card>

      <div className="flex items-center justify-between text-sm text-muted-foreground">
        <span>{users.data ? `${users.data.total} user${users.data.total === 1 ? "" : "s"}` : null}</span>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>Previous</Button>
          <span>Page {page} of {totalPages}</span>
          <Button variant="secondary" size="sm" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>Next</Button>
        </div>
      </div>

      <InviteDialog open={inviteOpen} onClose={() => setInviteOpen(false)} />
      <RolesDialog key={editing?.id ?? "none"} user={editing} onClose={() => setEditing(null)} />
    </div>
  );
}
