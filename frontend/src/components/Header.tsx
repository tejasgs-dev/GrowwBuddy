import { Icon, Logo } from "./Icon";

interface Props {
  disclaimer: string;
  drawerOpen: boolean;
  onToggleDrawer: () => void;
  onHome: () => void;
}

/** App bar with the always-visible disclaimer (task 5.3). On phones the disclaimer drops to
 * its own full-width strip under the bar, so it is never hidden (EC-UI-09). */
export function Header({ disclaimer, drawerOpen, onToggleDrawer, onHome }: Props) {
  return (
    <header className="topbar">
      <div className="topbar-row">
        <button
          type="button"
          className="icon-btn menu-btn"
          aria-label={drawerOpen ? "Close chat list" : "Open chat list"}
          aria-expanded={drawerOpen}
          aria-controls="thread-drawer"
          onClick={onToggleDrawer}
        >
          <Icon name={drawerOpen ? "close" : "menu"} size={20} />
        </button>
        <div className="brand">
          {/* Home: back to the welcome screen. A real link, so it also works with
              middle-click or a new tab; a plain click is handled in the app. */}
          <a
            className="brand-home"
            href="/"
            aria-label="GrowwBuddy home"
            onClick={(e) => {
              if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
              e.preventDefault();
              onHome();
            }}
          >
            <Logo size={30} />
            <span className="brand-name">GrowwBuddy</span>
          </a>
          <span className="brand-tag">HDFC MF reference</span>
        </div>
        <p className="disclaimer" role="note" aria-label="Disclaimer">
          <Icon name="warning" size={16} />
          <span>{disclaimer}</span>
        </p>
      </div>
    </header>
  );
}
