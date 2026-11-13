// Thin typed client for the GrowwBuddy API. Same-origin: Vite proxies /api in development
// and FastAPI serves the UI in production.

import type { Meta, Reply, StoredMessage, Thread } from "./types";

export type ApiErrorKind = "network" | "rate_limited" | "not_found" | "too_large" | "http";

export class ApiError extends Error {
  constructor(
    readonly kind: ApiErrorKind,
    readonly status: number | null,
  ) {
    super(`api error: ${kind}${status ? ` (${status})` : ""}`);
    this.name = "ApiError";
  }
}

const BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new ApiError("network", null);
  }
  if (!res.ok) {
    const kind: ApiErrorKind =
      res.status === 429
        ? "rate_limited"
        : res.status === 404
          ? "not_found"
          : res.status === 413
            ? "too_large"
            : "http";
    throw new ApiError(kind, res.status);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const q = (clientId: string) => `client_id=${encodeURIComponent(clientId)}`;
const tid = (threadId: string) => encodeURIComponent(threadId);

export const api = {
  meta: () => request<Meta>("/api/meta"),

  listThreads: async (clientId: string) =>
    (await request<{ threads: Thread[] }>(`/api/threads?${q(clientId)}`)).threads,

  createThread: (clientId: string) =>
    request<Thread>("/api/threads", {
      method: "POST",
      body: JSON.stringify({ client_id: clientId }),
    }),

  messages: async (threadId: string, clientId: string) =>
    (
      await request<{ thread: Thread; messages: StoredMessage[] }>(
        `/api/threads/${tid(threadId)}/messages?${q(clientId)}`,
      )
    ).messages,

  send: (threadId: string, clientId: string, text: string, clientMessageId: string) =>
    request<Reply>(`/api/threads/${tid(threadId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ client_id: clientId, text, client_message_id: clientMessageId }),
    }),

  deleteThread: (threadId: string, clientId: string) =>
    request<void>(`/api/threads/${tid(threadId)}?${q(clientId)}`, { method: "DELETE" }),
};

export type Api = typeof api;
