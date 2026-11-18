import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { mockFetchJson } from "@/tests/helpers";

import { ApiError, apiGet } from "./client";

const schema = z.object({ status: z.literal("ok") });

describe("apiGet", () => {
  it("returns validated data", async () => {
    const fetchMock = mockFetchJson({ status: "ok" });
    await expect(apiGet("/health/live", schema)).resolves.toEqual({ status: "ok" });
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/health/live", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("rejects responses that do not match the schema", async () => {
    mockFetchJson({ status: "maybe" });
    await expect(apiGet("/x", schema)).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
  });

  it("maps problem+json errors to ApiError", async () => {
    mockFetchJson(
      {
        type: "https://claimos.dev/problems/not-found",
        title: "Resource not found",
        status: 404,
        code: "NOT_FOUND",
        detail: "claim does not exist",
        request_id: "req-123",
      },
      404,
    );
    const error = await apiGet("/claims/1", schema).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 404, code: "NOT_FOUND", message: "claim does not exist", requestId: "req-123" });
  });

  it("parses accepted non-2xx statuses with the success schema", async () => {
    mockFetchJson({ status: "ok" }, 503);
    await expect(apiGet("/health/ready", schema, { acceptStatuses: [503] })).resolves.toEqual({ status: "ok" });
  });

  it("reports network failures", async () => {
    vi.spyOn(globalThis, "fetch").mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(apiGet("/x", schema)).rejects.toMatchObject({ status: 0, code: "NETWORK_ERROR" });
  });
});
