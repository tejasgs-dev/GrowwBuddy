import type { Meta } from "../types";
import { Icon, type IconName } from "./Icon";

interface Props {
  meta: Meta;
  pending: boolean;
  onAsk: (question: string) => void;
}

function iconFor(q: string): IconName {
  const s = q.toLowerCase();
  if (s.includes("expense") || s.includes("ter")) return "percent";
  if (s.includes("exit load")) return "exit";
  if (s.includes("sip") || s.includes("minimum")) return "calendar";
  return "chat";
}

/** Empty-thread view: welcome text and three example questions (task 5.5). Clicking an
 * example sends it; clicks are ignored while a send is pending (EC-UI-07). */
export function Welcome({ meta, pending, onAsk }: Props) {
  const examples = meta.example_questions.slice(0, 3);
  return (
    <section className="welcome" aria-labelledby="welcome-title">
      <div className="kb">
        <p className="kb-label">
          <Icon name="database" size={14} />
          Groww scheme pages · {meta.schemes.length} schemes indexed
        </p>
        <ul className="scheme-chips" aria-label="Schemes covered">
          {meta.schemes.map((s) => (
            <li key={s.key} className="chip">
              <span className="chip-dot" aria-hidden="true" />
              {s.name}
            </li>
          ))}
        </ul>
      </div>

      <div className="welcome-card">
        <div className="welcome-head">
          <span className="welcome-icon">
            <Icon name="chat" size={22} />
          </span>
          <h1 id="welcome-title">Welcome to GrowwBuddy</h1>
        </div>
        <p className="welcome-text">{meta.welcome}</p>
        {meta.footer && (
          <p className="pill">
            <Icon name="clock" size={14} />
            {meta.footer}
          </p>
        )}
      </div>

      <div className="examples-head">
        <h2 className="section-label" id="examples-label">
          Try asking
        </h2>
        <span className="examples-note">Answers cite one Groww page</span>
      </div>
      <ul className="examples" aria-labelledby="examples-label">
        {examples.map((q) => (
          <li key={q}>
            <button
              type="button"
              className="example"
              onClick={() => onAsk(q)}
              disabled={pending}
            >
              <span className="example-icon">
                <Icon name={iconFor(q)} size={18} />
              </span>
              <span className="example-text">{q}</span>
              <span className="example-go">
                <Icon name="arrow" size={16} />
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
