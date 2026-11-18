import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { mockFetchJson, renderWithQuery } from "@/tests/helpers";

import { EnvironmentBanner } from "./environment-banner";
import type { Readiness } from "./schema";
import { SystemHealthPanel } from "./system-health-panel";

function readiness(overrides: Partial<Readiness> = {}): Readiness {
  return {
    status: "degraded",
    version: "0.1.0",
    environment: "development",
    checked_at: "2026-11-04T09:41:01Z",
    components: [
      { name: "database", status: "ok", required: true, latency_ms: 3.2, detail: null },
      { name: "migrations", status: "ok", required: true, latency_ms: 4.1, detail: "revision 0001 (head)" },
      { name: "workers", status: "degraded", required: false, latency_ms: 1000.4, detail: "no workers responded" },
    ],
    development_adapters: ["unencrypted_object_storage"],
    ...overrides,
  };
}

describe("SystemHealthPanel", () => {
  it("renders each component from the API response", async () => {
    mockFetchJson(readiness());
    renderWithQuery(<SystemHealthPanel />);

    const db = await screen.findByTestId("component-database");
    expect(within(db).getByText("PostgreSQL")).toBeInTheDocument();
    expect(within(db).getByText("Operational")).toBeInTheDocument();

    const workers = screen.getByTestId("component-workers");
    expect(within(workers).getByText("Degraded")).toBeInTheDocument();
    expect(within(workers).getByText("optional")).toBeInTheDocument();
    expect(within(workers).getByText("no workers responded")).toBeInTheDocument();
    expect(screen.getByTestId("overall-status")).toHaveTextContent("Degraded");
  });

  it("shows a down report delivered with HTTP 503", async () => {
    mockFetchJson(
      readiness({
        status: "down",
        components: [{ name: "database", status: "down", required: true, latency_ms: 2000, detail: "timed out after 2s" }],
      }),
      503,
    );
    renderWithQuery(<SystemHealthPanel />);
    expect(await screen.findByTestId("overall-status")).toHaveTextContent("Unavailable");
    expect(screen.getByText("timed out after 2s")).toBeInTheDocument();
  });

  it("shows an error with the request id when the API is unreachable", async () => {
    mockFetchJson(
      { type: "https://claimos.dev/problems/internal", title: "Internal server error", status: 500, code: "INTERNAL", request_id: "abc123" },
      500,
    );
    renderWithQuery(<SystemHealthPanel />);
    // useReadiness retries once (≈1 s back-off) before surfacing the error.
    const alert = await screen.findByRole("alert", {}, { timeout: 4000 });
    expect(alert).toHaveTextContent("Health information is unavailable.");
    expect(alert).toHaveTextContent("request abc123");
  });
});

describe("EnvironmentBanner", () => {
  it("labels non-production environments and active development adapters", async () => {
    mockFetchJson(readiness());
    renderWithQuery(<EnvironmentBanner />);
    const banner = await screen.findByTestId("environment-banner");
    expect(banner).toHaveTextContent("development");
    expect(banner).toHaveTextContent("Object storage without server-side encryption");
  });

  it("is hidden in a clean production environment", async () => {
    const fetchMock = mockFetchJson(readiness({ environment: "production", development_adapters: [] }));
    const { container } = renderWithQuery(<EnvironmentBanner />);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
