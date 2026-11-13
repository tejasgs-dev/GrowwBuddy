import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Api } from "../api";
import App from "../App";
import { loadClientId } from "../clientId";
import { DISCLAIMER } from "../defaults";
import type { Reply } from "../types";
import { CLIENT, META, deferred, fakeApi, reply, thread } from "./fakeApi";

function renderApp(api: Api, client = CLIENT) {
  const user = userEvent.setup();
  const utils = render(<App api={api} client={client} />);
  return { user, ...utils };
}

const input = () => screen.getByLabelText("Ask a factual question") as HTMLTextAreaElement;
const sendBtn = () => screen.getByRole("button", { name: /^(Send|Sending)$/ });

describe("disclaimer (EV-5-01, blocking)", () => {
  it("is shown from /api/meta", async () => {
    const { api } = fakeApi();
    renderApp(api);
    await waitFor(() => expect(api.meta).toHaveBeenCalled());
    expect(screen.getByRole("note", { name: "Disclaimer" })).toHaveTextContent(DISCLAIMER);
  });

  it("is shown from the built-in default when /api/meta fails", async () => {
    const { api } = fakeApi();
    api.meta.mockRejectedValue(new ApiError("network", null));
    renderApp(api);
    await waitFor(() => expect(api.meta).toHaveBeenCalled());
    expect(screen.getByRole("note", { name: "Disclaimer" })).toHaveTextContent(
      "Facts-only. No investment advice.",
    );
    // Welcome and examples still render from the defaults.
    expect(screen.getByRole("heading", { name: "Welcome to GrowwBuddy" })).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Try asking" })).getAllByRole("button")).toHaveLength(3);
  });

  it("keeps the built-in disclaimer if the server sends a blank one", async () => {
    const { api } = fakeApi();
    api.meta.mockResolvedValue({ ...META, disclaimer: " " });
    renderApp(api);
    await waitFor(() => expect(api.meta).toHaveBeenCalled());
    expect(screen.getByRole("note", { name: "Disclaimer" })).toHaveTextContent(DISCLAIMER);
  });
});

describe("welcome and example chips (EV-5-02)", () => {
  it("shows the welcome text, scheme list and exactly three examples", async () => {
    const { api } = fakeApi();
    renderApp(api);
    expect(await screen.findByText(META.welcome)).toBeInTheDocument();
    const chips = within(screen.getByRole("list", { name: "Try asking" })).getAllByRole("button");
    expect(chips.map((c) => c.textContent)).toEqual(META.example_questions);
    expect(within(screen.getByRole("list", { name: "Schemes covered" })).getAllByRole("listitem")).toHaveLength(5);
    expect(screen.getByText("Last updated from sources: 11 Nov 2026")).toBeInTheDocument();
  });

  it("clicking a chip sends it once, even when clicked three times quickly", async () => {
    const { api } = fakeApi();
    const { user } = renderApp(api);
    const chip = await screen.findByRole("button", { name: META.example_questions[0] });
    await user.tripleClick(chip);
    await screen.findByText(`Reply to: ${META.example_questions[0]}`);
    expect(api.createThread).toHaveBeenCalledTimes(1);
    expect(api.send).toHaveBeenCalledTimes(1);
    expect(api.send.mock.calls[0][2]).toBe(META.example_questions[0]);
  });
});

describe("threads", () => {
  it("creates a thread on the first send and titles it from the reply (EC-UI-08)", async () => {
    const { api } = fakeApi();
    const { user } = renderApp(api);
    await user.type(input(), "What is the exit load of HDFC Small Cap Fund?{Enter}");
    expect(await screen.findByText("Reply to: What is the exit load of HDFC Small Cap Fund?")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Recent chats" });
    expect(within(list).getByRole("button", { name: "What is the exit load of HDFC Small Cap Fund?" })).toHaveAttribute("aria-current", "true");
    expect(input().value).toBe("");
  });

  it("switches threads and shows each thread's own history", async () => {
    const { api, store } = fakeApi({ threads: [thread("thr_a", "Mid cap TER"), thread("thr_b", "Small cap exit load")] });
    store.thr_a.push({ message_id: "m1", role: "user", text: "TER of mid cap?", type: null, citation: null, last_updated: null, footer: null, created_at: "" });
    store.thr_b.push({ message_id: "m2", role: "user", text: "Exit load of small cap?", type: null, citation: null, last_updated: null, footer: null, created_at: "" });
    const { user } = renderApp(api);

    await user.click(await screen.findByRole("button", { name: "Mid cap TER" }));
    expect(await screen.findByText("TER of mid cap?")).toBeInTheDocument();
    expect(screen.queryByText("Exit load of small cap?")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Small cap exit load" }));
    expect(await screen.findByText("Exit load of small cap?")).toBeInTheDocument();
    expect(screen.queryByText("TER of mid cap?")).not.toBeInTheDocument();
  });

  it("restores the open thread after a reload (EV-5-11)", async () => {
    const { api, store } = fakeApi({ threads: [thread("thr_a", "First"), thread("thr_b", "Second")] });
    store.thr_b.push({ message_id: "m2", role: "user", text: "second thread question", type: null, citation: null, last_updated: null, footer: null, created_at: "" });
    const first = renderApp(api);
    await first.user.click(await screen.findByRole("button", { name: "Second" }));
    await screen.findByText("second thread question");
    first.unmount();

    renderApp(api);
    expect(await screen.findByText("second thread question")).toBeInTheDocument();
  });

  it("deleting the open thread moves to a neighbour, then to the welcome screen (EC-UI-11)", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const { api } = fakeApi({ threads: [thread("thr_a", "Alpha"), thread("thr_b", "Beta")] });
    const { user } = renderApp(api);

    await user.click(await screen.findByRole("button", { name: "Alpha" }));
    await user.click(screen.getByRole("button", { name: "Delete chat: Alpha" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Alpha" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Beta" })).toHaveAttribute("aria-current", "true");

    await user.click(screen.getByRole("button", { name: "Delete chat: Beta" }));
    expect(await screen.findByRole("heading", { name: "Welcome to GrowwBuddy" })).toBeInTheDocument();
    expect(api.deleteThread).toHaveBeenCalledTimes(2);
  });

  it("the GrowwBuddy logo goes back to the welcome screen, keeping the chats", async () => {
    const { api, store } = fakeApi({ threads: [thread("thr_a", "Alpha")] });
    store.thr_a.push({ message_id: "m1", role: "user", text: "alpha question", type: null, citation: null, last_updated: null, footer: null, created_at: "" });
    const { user } = renderApp(api);
    await user.click(await screen.findByRole("button", { name: "Alpha" }));
    await screen.findByText("alpha question");

    const home = screen.getByRole("link", { name: "GrowwBuddy home" });
    expect(home).toHaveAttribute("href", "/");
    await user.click(home);
    expect(await screen.findByRole("heading", { name: "Welcome to GrowwBuddy" })).toBeInTheDocument();
    expect(screen.queryByText("alpha question")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alpha" })).not.toHaveAttribute("aria-current");
    expect(input()).toHaveFocus();

    // The chat is still there to go back to.
    await user.click(screen.getByRole("button", { name: "Alpha" }));
    expect(await screen.findByText("alpha question")).toBeInTheDocument();
  });

  it("does nothing when the delete is cancelled", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    const { api } = fakeApi({ threads: [thread("thr_a", "Alpha")] });
    const { user } = renderApp(api);
    await user.click(await screen.findByRole("button", { name: "Delete chat: Alpha" }));
    expect(api.deleteThread).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Alpha" })).toBeInTheDocument();
  });
});

describe("reply types in the stream", () => {
  it("withholds the text of a PII-rejected message and shows the privacy card", async () => {
    const { api } = fakeApi({ respond: () => reply("pii_rejected", "For your security, please don't share personal details.") });
    const { user } = renderApp(api);
    await user.type(input(), "my pan is ABCDE1234F{Enter}");
    expect(await screen.findByText("Privacy protection")).toBeInTheDocument();
    expect(screen.queryByText(/ABCDE1234F/)).not.toBeInTheDocument();
    expect(screen.getByText(/Message not sent/)).toBeInTheDocument();
  });

  it("user messages containing HTML are shown as text (EV-5-04)", async () => {
    const { api } = fakeApi();
    const { user, container } = renderApp(api);
    const html = '<img src=x onerror="window.__p=1">';
    await user.click(input());
    await user.paste(html);
    await user.keyboard("{Enter}");
    await screen.findByText(`Reply to: ${html}`);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getAllByText(html).length).toBeGreaterThan(0);
  });
});

describe("client-side PII warning (EV-5-06)", () => {
  it("warns on a PAN but still lets the user send", async () => {
    const { api } = fakeApi();
    const { user } = renderApp(api);
    await user.type(input(), "my PAN is ABCDE1234F");
    expect(screen.getByText(/Possible personal details detected \(PAN\)/)).toBeInTheDocument();
    expect(input()).toHaveAccessibleDescription(expect.stringContaining("Possible personal details"));
    expect(sendBtn()).toBeEnabled();
    await user.click(sendBtn());
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  });

  it("doesn't warn on a large rupee amount, matching the server", async () => {
    const { api } = fakeApi();
    const { user } = renderApp(api);
    await user.type(input(), "Is ₹1000000000 a large AUM?");
    expect(screen.queryByText(/Possible personal details/)).not.toBeInTheDocument();
  });
});

describe("client_id fallback (EV-5-08)", () => {
  it("uses an in-memory ID when localStorage throws, and says so", async () => {
    vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const c = loadClientId();
    expect(c.persistent).toBe(false);
    expect(c.id).toMatch(/^[0-9a-f-]{36}$/);

    const { api } = fakeApi();
    const { user } = renderApp(api, c);
    expect(screen.getByText(/chats will disappear when you reload/)).toBeInTheDocument();
    await user.type(input(), "hello{Enter}");
    await screen.findByText("Reply to: hello");
    expect(api.createThread).toHaveBeenCalledWith(c.id);
  });

  it("keeps the same ID across loads when storage works", () => {
    const a = loadClientId();
    const b = loadClientId();
    expect(a.persistent).toBe(true);
    expect(b.id).toBe(a.id);
  });
});

describe("loading and errors (EV-5-09)", () => {
  it("shows a loading state and disables Send only in the pending thread", async () => {
    const slow = deferred<Reply>();
    const { api } = fakeApi({ threads: [thread("thr_a", "Alpha"), thread("thr_b", "Beta")] });
    const realSend = api.send.getMockImplementation()!;
    api.send.mockImplementation(async (tid, c, text, id) => (tid === "thr_a" ? slow.promise : realSend(tid, c, text, id)));
    const { user } = renderApp(api);

    await user.click(await screen.findByRole("button", { name: "Alpha" }));
    await user.type(input(), "slow question{Enter}");
    expect(await screen.findByRole("status")).toHaveTextContent("Checking the sources");
    await user.type(input(), "second");
    expect(sendBtn()).toBeDisabled();

    // Another thread is not blocked.
    await user.click(screen.getByRole("button", { name: "Beta" }));
    await user.type(input(), "fast question{Enter}");
    expect(await screen.findByText("Reply to: fast question")).toBeInTheDocument();

    await act(async () => slow.resolve({ ...reply("answer", "slow answer"), message_id: "m9", thread_id: "thr_a", thread_title: "Alpha" } as Reply));
    await user.click(screen.getByRole("button", { name: "Alpha" }));
    expect(await screen.findByText("slow answer")).toBeInTheDocument();
  });

  it("network error keeps the text, and Retry resends with the same message ID", async () => {
    const { api } = fakeApi();
    const realSend = api.send.getMockImplementation()!;
    api.send.mockRejectedValueOnce(new ApiError("network", null));
    const { user } = renderApp(api);

    await user.type(input(), "exit load of mid cap?{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn't reach GrowwBuddy");
    expect(input().value).toBe("exit load of mid cap?");
    expect(screen.queryByText("exit load of mid cap?", { selector: ".bubble" })).not.toBeInTheDocument();

    api.send.mockImplementation(realSend);
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Reply to: exit load of mid cap?")).toBeInTheDocument();
    expect(api.send).toHaveBeenCalledTimes(2);
    expect(api.send.mock.calls[1][3]).toBe(api.send.mock.calls[0][3]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("an edited message after an error gets a new message ID", async () => {
    const { api } = fakeApi();
    const realSend = api.send.getMockImplementation()!;
    api.send.mockRejectedValueOnce(new ApiError("network", null));
    const { user } = renderApp(api);
    await user.type(input(), "first try{Enter}");
    await screen.findByRole("alert");
    api.send.mockImplementation(realSend);
    await user.clear(input());
    await user.type(input(), "edited");
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("Reply to: edited");
    expect(api.send.mock.calls[1][3]).not.toBe(api.send.mock.calls[0][3]);
  });

  it("HTTP 429 shows the friendly rate-limit message (EC-UI-05)", async () => {
    const { api } = fakeApi();
    api.send.mockRejectedValueOnce(new ApiError("rate_limited", 429));
    const { user } = renderApp(api);
    await user.type(input(), "TER of large cap?{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "You're sending messages too quickly, please wait a moment.",
    );
    expect(input().value).toBe("TER of large cap?");
  });

  it("a busy reply shows the busy card and keeps the text to resend", async () => {
    const { api } = fakeApi({ respond: () => reply("busy", "GrowwBuddy is busy right now. Please try again shortly.") });
    const { user, container } = renderApp(api);
    await user.type(input(), "benchmark of multi cap?{Enter}");
    expect(await screen.findByText("Busy right now")).toBeInTheDocument();
    expect(input().value).toBe("benchmark of multi cap?");
    const card = container.querySelector('article[data-type="busy"]')!;
    expect(card.querySelector("a")).toBeNull();
    expect(screen.queryByText("benchmark of multi cap?", { selector: ".bubble" })).not.toBeInTheDocument();
  });
});

describe("keyboard and accessibility (EV-5-12, EV-5-13)", () => {
  it("sends with Enter and adds a new line with Shift+Enter", async () => {
    const { api } = fakeApi();
    const { user } = renderApp(api);
    await user.type(input(), "line one{Shift>}{Enter}{/Shift}line two");
    expect(input().value).toBe("line one\nline two");
    expect(api.send).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  });

  it("example chips and threads are reachable with Tab and work with Enter", async () => {
    const { api } = fakeApi({ threads: [thread("thr_a", "Alpha")] });
    const { user } = renderApp(api);
    await screen.findByRole("button", { name: "Alpha" });
    const chip = screen.getByRole("button", { name: META.example_questions[1] });
    for (let i = 0; i < 20 && document.activeElement !== chip; i++) await user.tab();
    expect(chip).toHaveFocus();
    await user.keyboard("{Enter}");
    await waitFor(() => expect(api.send).toHaveBeenCalledTimes(1));
  });

  it("has no serious or critical axe violations on the welcome and chat screens", async () => {
    const { api } = fakeApi({
      respond: (t) => (t.includes("invest") ? reply("refusal", "I can only share facts.") : reply("answer", "The TER is 0.74%.")),
    });
    const { user, container } = renderApp(api);
    await screen.findByText(META.welcome);
    const check = async () => {
      const r = await axe.run(container, { rules: { "color-contrast": { enabled: false } } });
      return r.violations.filter((v) => v.impact === "serious" || v.impact === "critical").map((v) => v.id);
    };
    expect(await check()).toEqual([]);

    await user.type(input(), "TER of mid cap?{Enter}");
    await screen.findByText("The TER is 0.74%.");
    await user.type(input(), "should I invest?{Enter}");
    await screen.findByText("I can only share facts.");
    expect(await check()).toEqual([]);
  });
});
