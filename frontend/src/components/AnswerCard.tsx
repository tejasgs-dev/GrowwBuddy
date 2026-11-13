import { isAllowedLink } from "../defaults";
import type { ChatItem, ResponseType } from "../types";
import { Icon, type IconName } from "./Icon";

type Tone = "plain" | "amber" | "red" | "slate" | "blue";

interface Variant {
  tone: Tone;
  label?: string;
  icon?: IconName;
}

// Distinct styling per reply type (task 5.6, EV-5-03).
export const VARIANTS: Record<ResponseType, Variant> = {
  answer: { tone: "plain" },
  greeting: { tone: "plain" },
  clarification: { tone: "plain" },
  refusal: { tone: "amber", label: "Facts-only restriction", icon: "info" },
  performance: { tone: "amber", label: "Performance data restricted", icon: "chart" },
  not_found: { tone: "slate", label: "Not found in sources", icon: "help" },
  pii_rejected: { tone: "red", label: "Privacy protection", icon: "shield" },
  busy: { tone: "blue", label: "Busy right now", icon: "hourglass" },
  unavailable: { tone: "slate", label: "Temporarily unavailable", icon: "warning" },
};

// Replies that never carry a link or footer (backend pipeline/types.py NO_LINK_TYPES).
const NO_LINK: ReadonlySet<ResponseType> = new Set([
  "pii_rejected",
  "greeting",
  "clarification",
  "busy",
  "unavailable",
]);

type AssistantItem = Extract<ChatItem, { kind: "assistant" }>;

/** One assistant reply. All text is rendered as plain text, never HTML (EC-UI-01). The
 * single source link is shown only for https://groww.in links (EC-UI-03). */
export function AnswerCard({ item }: { item: AssistantItem }) {
  const v = VARIANTS[item.type] ?? VARIANTS.answer;
  const linkable = !NO_LINK.has(item.type);
  const url = linkable && isAllowedLink(item.citation?.url) ? item.citation!.url : null;
  const footer = linkable ? item.footer : null;

  return (
    <article className={`card card--${v.tone}`} data-type={item.type}>
      {v.label && (
        <p className="card-label">
          {v.icon && <Icon name={v.icon} size={15} />}
          <span>{v.label}</span>
        </p>
      )}
      <p className="card-text">{item.text}</p>
      {url && (
        <a className="card-link" href={url} target="_blank" rel="noopener noreferrer">
          <span>Source: {item.citation!.title || "Groww scheme page"}</span>
          <Icon name="external" size={14} />
          <span className="sr-only"> (opens in a new tab)</span>
        </a>
      )}
      {footer && <p className="card-footer">{footer}</p>}
    </article>
  );
}
