import { useEffect, useRef } from "react";
import type { ChatItem } from "../types";
import { AnswerCard } from "./AnswerCard";

interface Props {
  items: ChatItem[];
  pending: boolean;
}

/** The conversation. User text is plain text too (EC-UI-02). */
export function MessageList({ items, pending }: Props) {
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [items.length, pending]);

  return (
    <div className="stream" role="log" aria-live="polite" aria-label="Conversation">
      <ol className="messages">
        {items.map((item) =>
          item.kind === "user" ? (
            <li key={item.id} className="msg msg--user">
              {item.withheld ? (
                <p className="bubble bubble--withheld">
                  Message not sent: it looked like it contained personal details.
                </p>
              ) : (
                <p className="bubble">{item.text}</p>
              )}
            </li>
          ) : (
            <li key={item.id} className="msg msg--bot">
              <AnswerCard item={item} />
            </li>
          ),
        )}
      </ol>
      {pending && (
        <div className="typing" role="status">
          <span className="typing-label">Checking the sources</span>
          <span className="typing-dots" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
        </div>
      )}
      <div ref={end} />
    </div>
  );
}
