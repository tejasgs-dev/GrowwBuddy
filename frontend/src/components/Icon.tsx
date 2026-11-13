// Small inline stroke icons (no icon font to download). Always decorative: aria-hidden.

const PATHS = {
  plus: ["M12 5v14", "M5 12h14"],
  trash: ["M4 7h16", "M9 7V4h6v3", "M18 7l-1 13H7L6 7", "M10 11v6", "M14 11v6"],
  warning: ["M12 3.5 2.5 20h19L12 3.5Z", "M12 10v4.5", "M12 17.5h.01"],
  info: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 11v5", "M12 7.5h.01"],
  shield: ["M12 3 19 6v5.5c0 4.5-3 7.8-7 9.5-4-1.7-7-5-7-9.5V6l7-3Z", "M9.5 12l2 2 3.5-3.5"],
  chart: ["M3 17l6-6 4 4 8-8", "M15 7h6v6"],
  search: ["M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z", "M20 20l-4-4"],
  send: ["M12 19V5", "M6 11l6-6 6 6"],
  external: ["M14 4h6v6", "M20 4 11 13", "M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"],
  menu: ["M4 7h16", "M4 12h16", "M4 17h16"],
  close: ["M6 6l12 12", "M18 6 6 18"],
  database: [
    "M12 8c4.4 0 8-1.3 8-3s-3.6-3-8-3-8 1.3-8 3 3.6 3 8 3Z",
    "M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5",
    "M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  ],
  verified: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M8.5 12.5l2.5 2.5 4.5-5"],
  percent: [
    "M19 5 5 19",
    "M6.5 9a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
    "M17.5 20a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z",
  ],
  exit: ["M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4", "M9 8l-4 4 4 4", "M5 12h11"],
  calendar: ["M5 5h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2Z", "M3 10h18", "M8 3v4", "M16 3v4"],
  clock: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M12 7v5l3 2"],
  lock: ["M6 11h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1v-8a1 1 0 0 1 1-1Z", "M8 11V7a4 4 0 0 1 8 0v4"],
  help: [
    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z",
    "M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .9-1 1.6v.6",
    "M12 17h.01",
  ],
  hourglass: ["M6 3h12", "M6 21h12", "M7 3v3l5 6 5-6V3", "M7 21v-3l5-6 5 6v3"],
  chat: ["M4 5h16v11H10l-5 4v-4H4V5Z", "M8.5 10.5l2.3 2.3 4.7-4.6"],
  arrow: ["M5 12h14", "M13 6l6 6-6 6"],
  retry: ["M20 11a8 8 0 1 0-2.4 5.7", "M20 4v7h-7"],
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 18, className }: { name: IconName; size?: number; className?: string }) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}

export function Logo({ size = 32 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path
        d="M8 9.5h16v10H14l-4.5 3.5v-3.5H8z"
        fill="none"
        stroke="var(--on-accent)"
        strokeWidth="2.2"
        strokeLinejoin="round"
      />
      <path
        d="M12.5 14.5l2.3 2.3 4.7-4.6"
        fill="none"
        stroke="var(--on-accent)"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
