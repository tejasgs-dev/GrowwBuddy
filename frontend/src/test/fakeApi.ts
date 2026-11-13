// In-memory stand-in for the GrowwBuddy API, with hooks to script replies and failures.

import { vi } from "vitest";
import { ApiError, type Api } from "../api";
import type { ClientId } from "../clientId";
import type { Meta, Reply, ResponseType, StoredMessage, Thread } from "../types";

export const CLIENT: ClientId = { id: "0b6f6d3e-6f5c-4a43-9d3a-2f6c1d1e9a01", persistent: true };
export const MID_URL = "https://groww.in/mutual-funds/hdfc-mid-cap-fund-direct-growth";

export const META: Meta = {
  welcome: "Hi! I answer factual questions about five HDFC Mutual Fund schemes on Groww.",
  example_questions: [
    "What is the expense ratio of HDFC Mid Cap Fund?",
    "What is the exit load of HDFC Small Cap Fund?",
    "What is the minimum SIP amount for HDFC Large Cap Fund?",
  ],
  disclaimer: "Facts-only. No investment advice.",
  input_hint: "Please don't share PAN, Aadhaar, account numbers, OTPs, email or phone numbers.",
  last_updated: "2026-11-11",
  footer: "Last updated from sources: 11 Nov 2026",
  schemes: [
    { key: "hdfc-mid-cap", name: "HDFC Mid Cap Fund", url: MID_URL },
    { key: "hdfc-small-cap", name: "HDFC Small Cap Fund", url: MID_URL },
    { key: "hdfc-large-cap", name: "HDFC Large Cap Fund", url: MID_URL },
    { key: "hdfc-silver-etf-fof", name: "HDFC Silver ETF FoF", url: MID_URL },
    { key: "hdfc-multi-cap", name: "HDFC Multi Cap Fund", url: MID_URL },
  ],
};

export function reply(type: ResponseType, text: string, extra: Partial<Reply> = {}): Omit<Reply, "thread_id" | "message_id" | "thread_title"> & Partial<Reply> {
  const linked = !["pii_rejected", "busy", "greeting", "clarification", "unavailable"].includes(type);
  return {
    type,
    text,
    citation: linked ? { url: MID_URL, title: "HDFC Mid Cap Fund Direct Growth – Groww" } : null,
    last_updated: linked ? "2026-11-10" : null,
    footer: linked ? "Last updated from sources: 10 Nov 2026" : null,
    ...extra,
  };
}

type Responder = (text: string) => ReturnType<typeof reply>;

export function fakeApi(opts: { respond?: Responder; threads?: Thread[] } = {}) {
  let n = 0;
  const now = "2026-11-13T10:00:00Z";
  const threads: Thread[] = [...(opts.threads ?? [])];
  const store: Record<string, StoredMessage[]> = {};
  for (const t of threads) store[t.thread_id] ??= [];
  const respond: Responder = opts.respond ?? ((text) => reply("answer", `Reply to: ${text}`));

  const api = {
    meta: vi.fn(async () => META),
    listThreads: vi.fn(async (_c: string) => [...threads]),
    createThread: vi.fn(async (_c: string) => {
      const t: Thread = { thread_id: `thr_${++n}`, title: "New chat", created_at: now, updated_at: now };
      threads.unshift(t);
      store[t.thread_id] = [];
      return t;
    }),
    messages: vi.fn(async (tid: string, _c: string) => {
      if (!store[tid]) throw new ApiError("not_found", 404);
      return [...store[tid]];
    }),
    send: vi.fn(async (tid: string, _c: string, text: string, _cmid: string): Promise<Reply> => {
      const t = threads.find((x) => x.thread_id === tid);
      if (!t) throw new ApiError("not_found", 404);
      const r = respond(text);
      const stored = r.type !== "pii_rejected" && r.type !== "busy";
      if (stored && t.title === "New chat" && (r.type === "answer" || r.type === "not_found")) {
        t.title = text.slice(0, 60);
      }
      const mid = stored ? `msg_${++n}` : null;
      if (stored) {
        store[tid].push(
          { message_id: `msg_${++n}`, role: "user", text, type: null, citation: null, last_updated: null, footer: null, created_at: now },
          { message_id: mid!, role: "assistant", text: r.text, type: r.type, citation: r.citation, last_updated: r.last_updated, footer: r.footer, created_at: now },
        );
      }
      return { message_id: mid, thread_id: tid, thread_title: t.title, ...r } as Reply;
    }),
    deleteThread: vi.fn(async (tid: string, _c: string) => {
      const i = threads.findIndex((x) => x.thread_id === tid);
      if (i < 0) throw new ApiError("not_found", 404);
      threads.splice(i, 1);
      delete store[tid];
    }),
  } satisfies Api;

  return { api, threads, store };
}

export function thread(id: string, title: string): Thread {
  return { thread_id: id, title, created_at: "2026-11-12T10:00:00Z", updated_at: "2026-11-12T10:00:00Z" };
}

/** A promise you resolve by hand, for slow replies. */
export function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
