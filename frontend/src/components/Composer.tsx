import { forwardRef, useMemo, type FormEvent, type KeyboardEvent } from "react";
import { checkPii, piiWarning } from "../pii";
import { ERROR_TEXT, MAX_MESSAGE_CHARS, type SendError } from "../useChat";
import { Icon } from "./Icon";

interface Props {
  value: string;
  onChange: (text: string) => void;
  onSend: (text: string) => void;
  pending: boolean;
  hint: string;
  error: SendError | null;
  onRetry: () => void;
  onDismissError: () => void;
}

/** Message box (task 5.7) with the client-side PII warning (task 5.8): a courtesy only, so
 * the user can still edit or send (EC-PII-16). Enter sends; Shift+Enter adds a line. */
export const Composer = forwardRef<HTMLTextAreaElement, Props>(function Composer(
  { value, onChange, onSend, pending, hint, error, onRetry, onDismissError },
  ref,
) {
  const warning = useMemo(() => piiWarning(checkPii(value)), [value]);
  const canSend = !pending && value.trim().length > 0;
  const remaining = MAX_MESSAGE_CHARS - value.length;

  const submit = (e?: FormEvent) => {
    e?.preventDefault();
    if (canSend) onSend(value);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  };

  const describedBy = ["composer-hint", warning ? "composer-warning" : null]
    .filter(Boolean)
    .join(" ");

  return (
    <div className="composer-wrap">
      {error && (
        <div className="send-error" role="alert">
          <Icon name="warning" size={16} />
          <span className="send-error-text">{ERROR_TEXT[error.kind]}</span>
          <button type="button" className="btn btn-small" onClick={onRetry} disabled={pending}>
            <Icon name="retry" size={14} />
            Retry
          </button>
          <button
            type="button"
            className="icon-btn"
            aria-label="Dismiss error"
            onClick={onDismissError}
          >
            <Icon name="close" size={14} />
          </button>
        </div>
      )}
      <form className={`composer${warning ? " has-warning" : ""}`} onSubmit={submit}>
        <span className="composer-icon">
          <Icon name="search" size={20} />
        </span>
        <label htmlFor="composer-input" className="sr-only">
          Ask a factual question
        </label>
        <textarea
          id="composer-input"
          ref={ref}
          rows={1}
          value={value}
          maxLength={MAX_MESSAGE_CHARS}
          placeholder="Ask a factual question…"
          aria-describedby={describedBy}
          autoComplete="off"
          onChange={(e) => onChange(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <button
          type="submit"
          className="send-btn"
          aria-label={pending ? "Sending" : "Send"}
          disabled={!canSend}
        >
          <Icon name="send" size={18} />
        </button>
      </form>
      <div aria-live="polite">
        {warning && (
          <p className="composer-warning" id="composer-warning">
            <Icon name="warning" size={14} />
            {warning}
          </p>
        )}
      </div>
      <div className="composer-meta">
        <p className="composer-hint" id="composer-hint">
          <Icon name="lock" size={13} />
          {hint}
        </p>
        {remaining <= 100 && (
          <span className="composer-count" aria-live="polite">
            {remaining} characters left
          </span>
        )}
      </div>
    </div>
  );
});
