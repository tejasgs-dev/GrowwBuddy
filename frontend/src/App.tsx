import { useCallback, useEffect, useRef, useState } from "react";
import { api as defaultApi, type Api } from "./api";
import { loadClientId, type ClientId } from "./clientId";
import { Composer } from "./components/Composer";
import { Header } from "./components/Header";
import { Icon } from "./components/Icon";
import { MessageList } from "./components/MessageList";
import { ThreadList } from "./components/ThreadList";
import { Welcome } from "./components/Welcome";
import type { Thread } from "./types";
import { useChat } from "./useChat";

interface Props {
  api?: Api;
  client?: ClientId;
}

export default function App({ api = defaultApi, client: givenClient }: Props) {
  const [client] = useState<ClientId>(() => givenClient ?? loadClientId());
  const chat = useChat(api, client);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const input = useRef<HTMLTextAreaElement>(null);

  const focusInput = () => input.current?.focus();

  // Escape closes the drawer on narrow screens.
  useEffect(() => {
    if (!drawerOpen) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setDrawerOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const onSelect = (id: string) => {
    chat.selectThread(id);
    setDrawerOpen(false);
    focusInput();
  };

  const onNew = () => {
    chat.newChat();
    setDrawerOpen(false);
    focusInput();
  };

  const onDelete = useCallback(
    (t: Thread) => {
      if (window.confirm(`Delete "${t.title}"? This removes its messages too.`)) {
        void chat.deleteThread(t.thread_id);
      }
    },
    [chat],
  );

  const showWelcome =
    chat.activeId === null || (chat.historyStatus === "ready" && chat.items.length === 0);

  return (
    <div className={`app${drawerOpen ? " drawer-open" : ""}`}>
      <a className="skip-link" href="#composer-input">
        Skip to message box
      </a>
      <Header
        disclaimer={chat.meta.disclaimer}
        drawerOpen={drawerOpen}
        onToggleDrawer={() => setDrawerOpen((o) => !o)}
        onHome={onNew}
      />
      <div className="body">
        <ThreadList
          threads={chat.threads}
          status={chat.threadsStatus}
          activeId={chat.activeId}
          schemes={chat.meta.schemes}
          persistent={client.persistent}
          open={drawerOpen}
          onNew={onNew}
          onSelect={onSelect}
          onDelete={onDelete}
        />
        {drawerOpen && (
          <div className="scrim" aria-hidden="true" onClick={() => setDrawerOpen(false)} />
        )}

        <main className="main" id="main">
          <div className="scroll">
            <div className="column">
              {chat.notice && (
                <div className="notice" role="status">
                  <Icon name="info" size={16} />
                  <span>{chat.notice}</span>
                  <button
                    type="button"
                    className="icon-btn"
                    aria-label="Dismiss"
                    onClick={chat.dismissNotice}
                  >
                    <Icon name="close" size={14} />
                  </button>
                </div>
              )}
              {chat.activeId !== null && chat.historyStatus === "loading" && chat.items.length === 0 && (
                <p className="loading-history" role="status">
                  Loading conversation…
                </p>
              )}
              {chat.activeId !== null && chat.historyStatus === "error" && (
                <div className="notice" role="alert">
                  <Icon name="warning" size={16} />
                  <span>Couldn't load this conversation.</span>
                  <button type="button" className="btn btn-small" onClick={chat.reloadHistory}>
                    <Icon name="retry" size={14} />
                    Retry
                  </button>
                </div>
              )}
              {showWelcome && chat.historyStatus !== "error" ? (
                <Welcome
                  meta={chat.meta}
                  pending={chat.pending}
                  onAsk={(q) => void chat.send(q)}
                />
              ) : (
                <MessageList items={chat.items} pending={chat.pending} />
              )}
              {showWelcome && chat.pending && <MessageList items={[]} pending />}
            </div>
          </div>
          <div className="dock">
            <div className="column">
              <Composer
                ref={input}
                value={chat.draft}
                onChange={chat.setDraft}
                onSend={(t) => void chat.send(t)}
                pending={chat.pending}
                hint={chat.meta.input_hint}
                error={chat.error}
                onRetry={chat.retry}
                onDismissError={chat.dismissError}
              />
            </div>
          </div>
        </main>
      </div>
    </div>
  );
}
