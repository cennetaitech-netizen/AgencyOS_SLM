import { useCallback, useEffect, useRef, useState } from "react";
import type { ChatApiClient } from "../api/client";
import type { ActionProposal, ActionView, ChatError, Conversation, MessageEnd, UsageAlert, Wallet } from "../types";

export interface ActionCardModel {
  proposal: ActionProposal;
  view?: ActionView;
  busy?: boolean;
  error?: string;
}

export interface UserItem {
  kind: "user";
  id: string;
  text: string;
}

export interface AssistantItem {
  kind: "assistant";
  id: string;
  messageId?: string;
  text: string;
  streaming: boolean;
  toolLabel: string | null;
  end?: MessageEnd;
  error?: ChatError;
  actions: ActionCardModel[];
  rating?: "up" | "down";
}

export type ChatItem = UserItem | AssistantItem;

/** Error codes that stop the user from sending until something changes (tokens, limits, billing). */
export const BLOCKING_CODES = new Set([
  "insufficient_tokens",
  "daily_limit_reached",
  "usage_cap_reached",
  "billing_suspended",
  "chat_disabled",
]);

let seq = 0;
const localId = () => `local-${Date.now()}-${seq++}`;

export function useChat(client: ChatApiClient) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [items, setItems] = useState<ChatItem[]>([]);
  const [wallet, setWallet] = useState<Wallet | null>(null);
  const [sending, setSending] = useState(false);
  const [blocked, setBlocked] = useState<ChatError | null>(null);
  const [alerts, setAlerts] = useState<UsageAlert[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const refreshConversations = useCallback(
    () => client.listConversations().then(setConversations).catch(() => undefined),
    [client],
  );
  const refreshWallet = useCallback(
    () =>
      client
        .getWallet()
        .then((w) => {
          setWallet(w);
          return w;
        })
        .catch(() => null),
    [client],
  );

  useEffect(() => {
    void refreshConversations();
    void refreshWallet();
  }, [refreshConversations, refreshWallet]);

  const patchAssistant = (id: string, fn: (a: AssistantItem) => AssistantItem) =>
    setItems((prev) => prev.map((it) => (it.kind === "assistant" && it.id === id ? fn(it) : it)));

  const patchAction = (itemId: string, actionId: string, fn: (c: ActionCardModel) => ActionCardModel) =>
    patchAssistant(itemId, (a) => ({
      ...a,
      actions: a.actions.map((c) => (c.proposal.action_id === actionId ? fn(c) : c)),
    }));

  const selectConversation = useCallback(
    async (id: string) => {
      abortRef.current?.abort();
      setActiveId(id);
      const msgs = await client.getMessages(id);
      setItems(
        msgs
          .filter((m) => m.content)
          .map((m): ChatItem =>
            m.role === "user"
              ? { kind: "user", id: m.id, text: m.content }
              : {
                  kind: "assistant",
                  id: m.id,
                  messageId: m.id,
                  text: m.content,
                  streaming: false,
                  toolLabel: null,
                  actions: [],
                  end:
                    m.tokens_charged != null
                      ? ({ tokens_charged: m.tokens_charged, message_id: m.id } as MessageEnd)
                      : undefined,
                },
          ),
      );
    },
    [client],
  );

  const newConversation = useCallback(() => {
    abortRef.current?.abort();
    setActiveId(null);
    setItems([]);
  }, []);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;
      setSending(true);
      let convId = activeId;
      const assistantId = localId();
      try {
        if (!convId) {
          convId = await client.createConversation();
          setActiveId(convId);
        }
        setItems((prev) => [
          ...prev,
          { kind: "user", id: localId(), text: trimmed },
          { kind: "assistant", id: assistantId, text: "", streaming: true, toolLabel: null, actions: [] },
        ]);
        const controller = new AbortController();
        abortRef.current = controller;
        for await (const ev of client.sendMessage(convId, trimmed, controller.signal)) {
          switch (ev.event) {
            case "message.start":
              patchAssistant(assistantId, (a) => ({ ...a, messageId: ev.data.message_id }));
              break;
            case "tool.start":
              patchAssistant(assistantId, (a) => ({ ...a, toolLabel: ev.data.label }));
              break;
            case "tool.end":
              patchAssistant(assistantId, (a) => ({ ...a, toolLabel: null }));
              break;
            case "token":
              patchAssistant(assistantId, (a) => ({ ...a, text: a.text + ev.data.text, toolLabel: null }));
              break;
            case "action.proposed":
              patchAssistant(assistantId, (a) => ({ ...a, actions: [...a.actions, { proposal: ev.data }] }));
              break;
            case "message.end":
              patchAssistant(assistantId, (a) => ({ ...a, end: ev.data, streaming: false }));
              setWallet((w) => (w ? { ...w, spendable: ev.data.balance_after, low_balance: ev.data.low_balance } : w));
              if (ev.data.alerts?.length) setAlerts(ev.data.alerts);
              break;
            case "error":
              patchAssistant(assistantId, (a) => ({ ...a, error: ev.data, streaming: false, toolLabel: null }));
              if (BLOCKING_CODES.has(ev.data.code)) setBlocked(ev.data);
              break;
          }
        }
      } catch (err) {
        if ((err as Error).name !== "AbortError") {
          patchAssistant(assistantId, (a) => ({
            ...a,
            streaming: false,
            error: { code: "network", message: "Connection lost. You were not charged for unsent text.", charged: false },
          }));
        }
      } finally {
        patchAssistant(assistantId, (a) => ({ ...a, streaming: false, toolLabel: null }));
        abortRef.current = null;
        setSending(false);
        void refreshConversations();
        void refreshWallet();
      }
    },
    [activeId, client, refreshConversations, refreshWallet, sending],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const rate = useCallback(
    async (itemId: string, messageId: string, rating: "up" | "down", reason?: string, comment?: string) => {
      await client.sendFeedback(messageId, rating, reason, comment);
      patchAssistant(itemId, (a) => ({ ...a, rating }));
    },
    [client],
  );

  const runAction = useCallback(
    async (itemId: string, actionId: string, op: "confirm" | "cancel" | "undo") => {
      patchAction(itemId, actionId, (c) => ({ ...c, busy: true, error: undefined }));
      try {
        if (op === "undo") {
          const proposal = await client.undoAction(actionId);
          patchAction(itemId, actionId, (c) => ({ ...c, busy: false }));
          patchAssistant(itemId, (a) => ({ ...a, actions: [...a.actions, { proposal }] }));
          return;
        }
        const view = op === "confirm" ? await client.confirmAction(actionId) : await client.cancelAction(actionId);
        patchAction(itemId, actionId, (c) => ({ ...c, busy: false, view }));
      } catch (err) {
        patchAction(itemId, actionId, (c) => ({ ...c, busy: false, error: (err as Error).message }));
      }
    },
    [client],
  );

  const rename = useCallback(
    async (id: string, title: string) => {
      await client.renameConversation(id, title);
      await refreshConversations();
    },
    [client, refreshConversations],
  );

  const remove = useCallback(
    async (id: string) => {
      await client.deleteConversation(id);
      if (id === activeId) newConversation();
      await refreshConversations();
    },
    [activeId, client, newConversation, refreshConversations],
  );

  const clearBlocked = useCallback(async () => {
    const w = await refreshWallet();
    if (blocked?.code !== "insufficient_tokens" || (w && w.spendable > 0)) setBlocked(null);
  }, [blocked, refreshWallet]);

  return {
    conversations,
    activeId,
    items,
    wallet,
    sending,
    blocked,
    alerts,
    dismissAlerts: () => setAlerts([]),
    send,
    stop,
    rate,
    runAction,
    selectConversation,
    newConversation,
    rename,
    remove,
    clearBlocked,
    refreshWallet,
  };
}
