// All chat state: meta, threads, per-thread messages, drafts, pending sends and errors.
//
// State is keyed per thread, so a slow reply only disables Send in its own thread
// (EC-UI-06), and a failed send keeps its text in that thread's box (EC-UI-04).
// `NEW` is the key for the welcome screen before a thread exists; the thread is created on
// the first send (EC-UI-08).

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, type Api, type ApiErrorKind } from "./api";
import { prefs, uuid, type ClientId } from "./clientId";
import { DEFAULT_META, mergeMeta } from "./defaults";
import type { ChatItem, Meta, Reply, StoredMessage, Thread } from "./types";

export const NEW = "__new__";
export const MAX_MESSAGE_CHARS = 1000; // backend pipeline/orchestrator.py (EC-INT-27)

export type SendErrorKind = Exclude<ApiErrorKind, "not_found">;

export interface SendError {
  kind: SendErrorKind;
  /** The text that failed and its ID, so Retry is deduplicated by the server (EC-THR-07). */
  text: string;
  clientMessageId: string;
}

export const ERROR_TEXT: Record<SendErrorKind, string> = {
  network: "Couldn't reach GrowwBuddy. Check your connection and try again.",
  rate_limited: "You're sending messages too quickly, please wait a moment.",
  too_large: "That message is too long. Please ask one short question.",
  http: "Something went wrong on our side. Please try again.",
};

type Status = "loading" | "ready" | "error";

function fromStored(m: StoredMessage): ChatItem {
  return m.role === "user"
    ? { kind: "user", id: m.message_id, text: m.text }
    : {
        kind: "assistant",
        id: m.message_id,
        type: m.type ?? "answer",
        text: m.text,
        citation: m.citation,
        footer: m.footer,
      };
}

function fromReply(r: Reply, localId: string): ChatItem {
  return {
    kind: "assistant",
    id: r.message_id ?? localId,
    type: r.type,
    text: r.text,
    citation: r.citation,
    footer: r.footer,
  };
}

const errorKind = (e: unknown): ApiErrorKind => (e instanceof ApiError ? e.kind : "http");

export function useChat(api: Api, client: ClientId) {
  const [meta, setMeta] = useState<Meta>(DEFAULT_META);
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadsStatus, setThreadsStatus] = useState<Status>("loading");
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Record<string, ChatItem[]>>({});
  const [history, setHistory] = useState<Record<string, Status>>({});
  const [pending, setPending] = useState<Record<string, boolean>>({});
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, SendError | null>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const pendingRef = useRef(new Set<string>());
  const activeRef = useRef<string | null>(null);

  const selectThread = useCallback((id: string | null) => {
    activeRef.current = id;
    setActiveId(id);
    prefs.set("active_thread", id);
  }, []);

  const markPending = (key: string, on: boolean) => {
    if (on) pendingRef.current.add(key);
    else pendingRef.current.delete(key);
    setPending((p) => ({ ...p, [key]: on }));
  };
  const setDraftFor = (key: string, text: string) => setDrafts((d) => ({ ...d, [key]: text }));
  const setErrorFor = (key: string, e: SendError | null) => setErrors((x) => ({ ...x, [key]: e }));
  const append = (tid: string, ...items: ChatItem[]) =>
    setMessages((m) => ({ ...m, [tid]: [...(m[tid] ?? []), ...items] }));
  const removeItem = (tid: string, id: string) =>
    setMessages((m) => ({ ...m, [tid]: (m[tid] ?? []).filter((i) => i.id !== id) }));

  const threadsRef = useRef<Thread[]>([]);
  threadsRef.current = threads;

  const forgetThread = useCallback(
    (tid: string) => {
      const ts = threadsRef.current;
      const idx = ts.findIndex((t) => t.thread_id === tid);
      const rest = ts.filter((t) => t.thread_id !== tid);
      // Deleting the open thread moves to its neighbour, else the welcome screen (EC-UI-11).
      if (activeRef.current === tid) {
        const next = rest[Math.min(Math.max(idx, 0), rest.length - 1)];
        selectThread(next ? next.thread_id : null);
      }
      setThreads((cur) => cur.filter((t) => t.thread_id !== tid));
      setMessages((m) => {
        const copy = { ...m };
        delete copy[tid];
        return copy;
      });
      setHistory((h) => {
        const copy = { ...h };
        delete copy[tid];
        return copy;
      });
    },
    [selectThread],
  );

  // ------------------------------------------------------------- initial load

  useEffect(() => {
    let live = true;
    api
      .meta()
      .then((m) => live && setMeta(mergeMeta(m)))
      .catch(() => {
        /* keep the built-in defaults, including the disclaimer (EC-UI-12) */
      });
    api
      .listThreads(client.id)
      .then((ts) => {
        if (!live) return;
        setThreads(ts);
        setThreadsStatus("ready");
        const saved = prefs.get("active_thread");
        if (saved && ts.some((t) => t.thread_id === saved) && activeRef.current === null) {
          selectThread(saved);
        }
      })
      .catch(() => live && setThreadsStatus("error"));
    return () => {
      live = false;
    };
  }, [api, client.id, selectThread]);

  // ------------------------------------------------------------- history per thread

  const loadHistory = useCallback(
    (tid: string) => {
      setHistory((h) => ({ ...h, [tid]: "loading" }));
      api
        .messages(tid, client.id)
        .then((ms) => {
          // Keep anything added locally while the request was in flight.
          setMessages((m) => ({ ...m, [tid]: [...ms.map(fromStored), ...(m[tid] ?? [])] }));
          setHistory((h) => ({ ...h, [tid]: "ready" }));
        })
        .catch((e: unknown) => {
          if (errorKind(e) === "not_found") {
            forgetThread(tid);
            setNotice("That chat no longer exists.");
          } else {
            setHistory((h) => ({ ...h, [tid]: "error" }));
          }
        });
    },
    [api, client.id, forgetThread],
  );

  useEffect(() => {
    if (activeId && history[activeId] === undefined && messages[activeId] === undefined) {
      loadHistory(activeId);
    }
  }, [activeId, history, messages, loadHistory]);

  // ------------------------------------------------------------- sending

  const send = useCallback(
    async (raw: string, clientMessageId: string = uuid()) => {
      const text = raw.trim();
      const startKey = activeRef.current ?? NEW;
      // Ignore a second send while one is pending in this thread (EC-UI-07, EC-THR-07).
      if (!text || pendingRef.current.has(startKey)) return;

      let tid = activeRef.current;
      let key = startKey;
      const localUserId = `local_u_${clientMessageId}`;
      markPending(key, true);
      setErrorFor(key, null);
      setDraftFor(key, "");

      try {
        if (!tid) {
          const t = await api.createThread(client.id);
          tid = t.thread_id;
          setThreads((ts) => [t, ...ts]);
          setMessages((m) => ({ ...m, [t.thread_id]: [] }));
          setHistory((h) => ({ ...h, [t.thread_id]: "ready" }));
          markPending(key, false);
          key = tid;
          markPending(key, true);
          if (activeRef.current === null) selectThread(tid);
        }
        const threadId = tid;
        append(threadId, { kind: "user", id: localUserId, text });

        const reply = await api.send(threadId, client.id, text, clientMessageId);
        const card = fromReply(reply, `local_a_${clientMessageId}`);

        if (reply.type === "busy") {
          // Not stored server-side: drop the bubble and put the text back to resend (5.9).
          removeItem(threadId, localUserId);
          append(threadId, card);
          setDrafts((d) => ({ ...d, [threadId]: d[threadId]?.trim() ? d[threadId] : text }));
        } else if (reply.type === "pii_rejected") {
          // Never stored; don't keep the personal details on screen either.
          setMessages((m) => ({
            ...m,
            [threadId]: [
              ...(m[threadId] ?? []).map((i) =>
                i.id === localUserId ? { ...i, text: "", withheld: true } : i,
              ),
              card,
            ],
          }));
        } else {
          append(threadId, card);
        }

        setThreads((ts) => {
          const cur = ts.find((t) => t.thread_id === threadId);
          if (!cur) return ts;
          const updated = { ...cur, title: reply.thread_title, updated_at: new Date().toISOString() };
          return [updated, ...ts.filter((t) => t.thread_id !== threadId)];
        });
      } catch (e) {
        const kind = errorKind(e);
        if (tid) removeItem(tid, localUserId);
        if (kind === "not_found" && tid) {
          forgetThread(tid);
          setNotice("That chat no longer exists. Your message is back in the box.");
          setDraftFor(NEW, text);
        } else {
          const k: SendErrorKind = kind === "not_found" ? "http" : kind;
          setDrafts((d) => ({ ...d, [key]: d[key]?.trim() ? d[key] : text }));
          setErrorFor(key, { kind: k, text, clientMessageId });
        }
      } finally {
        markPending(key, false);
      }
    },
    [api, client.id, selectThread, forgetThread],
  );

  const activeKey = activeId ?? NEW;

  const retry = useCallback(() => {
    const err = errors[activeKey];
    if (!err) return;
    const draft = (drafts[activeKey] ?? "").trim();
    const text = draft || err.text;
    // Same text -> same client_message_id, so a reply the server already made is reused.
    void send(text, text === err.text ? err.clientMessageId : uuid());
  }, [errors, drafts, activeKey, send]);

  const deleteThread = useCallback(
    async (tid: string) => {
      try {
        await api.deleteThread(tid, client.id);
      } catch (e) {
        if (errorKind(e) !== "not_found") {
          setNotice("Couldn't delete that chat. Please try again.");
          return;
        }
      }
      forgetThread(tid);
    },
    [api, client.id, forgetThread],
  );

  return {
    meta,
    threads,
    threadsStatus,
    activeId,
    items: activeId ? (messages[activeId] ?? []) : [],
    historyStatus: activeId ? (history[activeId] ?? "loading") : ("ready" as Status),
    reloadHistory: () => activeId && loadHistory(activeId),
    pending: !!pending[activeKey],
    draft: drafts[activeKey] ?? "",
    setDraft: (text: string) => setDraftFor(activeKey, text),
    error: errors[activeKey] ?? null,
    dismissError: () => setErrorFor(activeKey, null),
    notice,
    dismissNotice: () => setNotice(null),
    send,
    retry,
    selectThread,
    newChat: () => selectThread(null),
    deleteThread,
  };
}

export type Chat = ReturnType<typeof useChat>;
