// Shapes of the GrowwBuddy HTTP API (backend/growwbuddy/api/schemas.py, architecture §6.6).

export type ResponseType =
  | "answer"
  | "refusal"
  | "performance"
  | "not_found"
  | "pii_rejected"
  | "greeting"
  | "clarification"
  | "busy"
  | "unavailable";

export interface Citation {
  url: string;
  title: string;
}

export interface Thread {
  thread_id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

/** Reply to one sent message. `message_id` is null for unstored replies (pii_rejected, busy). */
export interface Reply {
  message_id: string | null;
  thread_id: string;
  type: ResponseType;
  text: string;
  citation: Citation | null;
  last_updated: string | null;
  footer: string | null;
  thread_title: string;
}

export interface StoredMessage {
  message_id: string;
  role: "user" | "assistant";
  text: string;
  type: ResponseType | null;
  citation: Citation | null;
  last_updated: string | null;
  footer: string | null;
  created_at: string;
}

export interface Scheme {
  key: string;
  name: string;
  url: string;
}

export interface Meta {
  welcome: string;
  example_questions: string[];
  disclaimer: string;
  input_hint: string;
  last_updated: string | null;
  footer: string | null;
  schemes: Scheme[];
}

/** One row in the chat stream. */
export type ChatItem =
  | { kind: "user"; id: string; text: string; withheld?: boolean }
  | {
      kind: "assistant";
      id: string;
      type: ResponseType;
      text: string;
      citation: Citation | null;
      footer: string | null;
    };
