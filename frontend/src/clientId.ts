// Anonymous browser ID (task 5.2). A random UUID, never tied to personal data. If storage is
// blocked (private browsing), fall back to an in-memory ID and tell the user (EC-THR-13).

const KEY = "growwbuddy.client_id";
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function uuid(): string {
  const c = globalThis.crypto;
  if (typeof c?.randomUUID === "function") return c.randomUUID();
  // randomUUID needs a secure context; plain-http LAN access still has getRandomValues.
  const b = new Uint8Array(16);
  c.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export interface ClientId {
  id: string;
  /** False when localStorage is unavailable: threads won't survive a reload. */
  persistent: boolean;
}

export function loadClientId(): ClientId {
  try {
    const store = window.localStorage;
    const saved = store.getItem(KEY);
    if (saved && UUID_RE.test(saved)) return { id: saved, persistent: true };
    const id = uuid();
    store.setItem(KEY, id);
    // Some browsers accept setItem and drop it; read back to be sure.
    return { id, persistent: store.getItem(KEY) === id };
  } catch {
    return { id: uuid(), persistent: false };
  }
}

/** Small per-browser preferences (the open thread). Never required for correctness. */
export const prefs = {
  get(key: string): string | null {
    try {
      return window.localStorage.getItem(`growwbuddy.${key}`);
    } catch {
      return null;
    }
  },
  set(key: string, value: string | null): void {
    try {
      if (value === null) window.localStorage.removeItem(`growwbuddy.${key}`);
      else window.localStorage.setItem(`growwbuddy.${key}`, value);
    } catch {
      /* storage blocked: nothing to remember */
    }
  },
};
