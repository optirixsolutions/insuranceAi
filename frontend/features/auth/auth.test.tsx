import { fireEvent, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiRequest, noContent, readCsrfToken } from "@/lib/api/client";
import { mockFetchJson, renderWithQuery } from "@/tests/helpers";

import { LoginForm, RegisterForm } from "./auth-forms";
import { registerFormSchema } from "./schema";
import { safeNextPath } from "./use-me";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  useSearchParams: () => new URLSearchParams("next=/settings/users"),
  usePathname: () => "/login",
}));

describe("safeNextPath", () => {
  it.each([
    [null, "/"],
    ["/settings", "/settings"],
    ["https://evil.example", "/"],
    ["//evil.example", "/"],
    ["/\\evil.example", "/"],
    ["settings", "/"],
  ])("maps %s to %s", (input, expected) => {
    expect(safeNextPath(input)).toBe(expected);
  });
});

describe("CSRF handling", () => {
  beforeEach(() => {
    // jsdom refuses __Host- cookies over http; the client also accepts the non-prefixed dev name.
    document.cookie = "claimos_csrf=csrf-123; path=/";
  });

  it("reads the CSRF cookie", () => {
    expect(readCsrfToken("a=1; __Host-claimos_csrf=tok%2Fen; b=2")).toBe("tok/en");
    expect(readCsrfToken("a=1")).toBeUndefined();
  });

  it("sends the token on unsafe requests only", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(null, { status: 204 }));
    await apiRequest("POST", "/auth/logout", noContent);
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-123");
  });
});

describe("registerFormSchema", () => {
  it("enforces the password policy and normalises email", () => {
    const short = registerFormSchema.safeParse({ organization_name: "Acme", display_name: "A", email: "a@b.co", password: "short" });
    expect(short.success).toBe(false);
    const ok = registerFormSchema.parse({
      organization_name: "Acme",
      display_name: "A",
      email: " Admin@Example.COM ",
      password: "a long enough passphrase",
    });
    expect(ok.email).toBe("admin@example.com");
  });
});

describe("LoginForm", () => {
  it("validates before calling the API", () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    renderWithQuery(<LoginForm />);
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByText("Enter a valid email address")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the API error and redirects to the safe next path on success", async () => {
    mockFetchJson({ type: "t", title: "Authentication required", status: 401, code: "UNAUTHENTICATED", detail: "Invalid email or password" }, 401);
    renderWithQuery(<LoginForm />);
    fireEvent.change(screen.getByLabelText("Work email"), { target: { value: "a@b.co" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong-password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");

    mockFetchJson({
      user: { id: "u", email: "a@b.co", display_name: "A" },
      organization: { id: "o", name: "Org", slug: "org" },
      roles: ["org_admin"],
      permissions: ["users.manage"],
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith("/settings/users"));
  });
});

describe("RegisterForm", () => {
  it("renders accessible fields", () => {
    renderWithQuery(<RegisterForm />);
    for (const label of ["Organisation name", "Your name", "Work email", "Password"]) {
      expect(screen.getByLabelText(label)).toBeInTheDocument();
    }
  });
});
