import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AnswerCard } from "../components/AnswerCard";
import type { ChatItem, ResponseType } from "../types";
import { MID_URL } from "./fakeApi";

type Item = Extract<ChatItem, { kind: "assistant" }>;

const item = (type: ResponseType, over: Partial<Item> = {}): Item => ({
  kind: "assistant",
  id: "msg_1",
  type,
  text: "The expense ratio of HDFC Mid Cap Fund is 0.74%.",
  citation: { url: MID_URL, title: "HDFC Mid Cap Fund Direct Growth – Groww" },
  footer: "Last updated from sources: 10 Nov 2026",
  ...over,
});

describe("AnswerCard (EV-5-03)", () => {
  it.each(["answer", "refusal", "performance", "not_found"] as const)(
    "%s shows exactly one safe link and the footer",
    (type) => {
      const { container } = render(<AnswerCard item={item(type)} />);
      const links = container.querySelectorAll("a");
      expect(links).toHaveLength(1);
      expect(links[0]).toHaveAttribute("href", MID_URL);
      expect(links[0]).toHaveAttribute("target", "_blank");
      expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
      expect(screen.getByText("Last updated from sources: 10 Nov 2026")).toBeInTheDocument();
    },
  );

  it.each(["pii_rejected", "busy", "greeting", "clarification", "unavailable"] as const)(
    "%s never shows a link or footer",
    (type) => {
      const { container } = render(<AnswerCard item={item(type)} />);
      expect(container.querySelectorAll("a")).toHaveLength(0);
      expect(screen.queryByText(/Last updated from sources/)).not.toBeInTheDocument();
    },
  );

  it("styles refusal, performance, not_found, pii_rejected and busy distinctly", () => {
    const labels: Record<string, string> = {
      refusal: "Facts-only restriction",
      performance: "Performance data restricted",
      not_found: "Not found in sources",
      pii_rejected: "Privacy protection",
      busy: "Busy right now",
    };
    const classes = new Set<string>();
    for (const [type, label] of Object.entries(labels)) {
      const { container, unmount } = render(<AnswerCard item={item(type as ResponseType)} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      const card = container.querySelector("article")!;
      expect(card).toHaveAttribute("data-type", type);
      classes.add(`${card.className}|${label}`);
      unmount();
    }
    expect(classes.size).toBe(5);
    const { container } = render(<AnswerCard item={item("answer")} />);
    expect(container.querySelector(".card-label")).toBeNull();
  });
});

describe("plain-text rendering (EV-5-04, blocking)", () => {
  it("shows HTML and Markdown in answer text literally", () => {
    const evil = '<script>window.__pwned = 1</script><img src=x onerror="window.__pwned=2"> **bold**';
    const { container } = render(<AnswerCard item={item("answer", { text: evil })} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("strong, b")).toBeNull();
    expect(screen.getByText(evil)).toBeInTheDocument();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("shows HTML in the citation title literally", () => {
    const { container } = render(
      <AnswerCard item={item("answer", { citation: { url: MID_URL, title: "<b>x</b>" } })} />,
    );
    expect(container.querySelector("b")).toBeNull();
    expect(screen.getByText("Source: <b>x</b>")).toBeInTheDocument();
  });
});

describe("link domain check (EV-5-05)", () => {
  it.each([
    "http://groww.in/mutual-funds/x",
    "https://evil.example/groww.in",
    "https://groww.in.evil.example/",
    "https://notgroww.in/",
    "javascript:alert(1)",
    "https://user:pw@groww.in/",
    "not a url",
    "",
  ])("renders no link for %s", (url) => {
    const { container } = render(
      <AnswerCard item={item("answer", { citation: { url, title: "Source" } })} />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
    // The footer still shows: the answer is unchanged, only the bad link is dropped.
    expect(screen.getByText(/Last updated from sources/)).toBeInTheDocument();
  });

  it.each(["https://groww.in/mutual-funds/x", "https://www.groww.in/x"])("links %s", (url) => {
    const { container } = render(
      <AnswerCard item={item("answer", { citation: { url, title: "Source" } })} />,
    );
    expect(container.querySelectorAll("a")).toHaveLength(1);
  });
});
