/**
 * 陪伴私聊空间（清透学园 MomoTalk 风格）：
 * 自动同步配置中的 Bot 名称（不硬编码特定角色或个人称呼），提供尖角气泡进场微动效与轻量输入栏。
 */
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactElement } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Button, ChatBubble, Feedback, Field, Icon, transitions } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import type { ChatActionItem, ChatFeed, ChatInputItem } from "../model/admin";
import { capabilityName, stageName } from "../model/names";
import type { Route } from "../model/routes";
import { mark } from "../semantics";
import { BrandMark, PageHeading, StateMark, time } from "../ui/common";

type StreamEntry =
  | { readonly kind: "input"; readonly timeSec: number; readonly item: ChatInputItem }
  | { readonly kind: "action"; readonly timeSec: number; readonly item: ChatActionItem };

const SENDER_STORAGE_KEY = "purrcept-chat-sender-v1";

function readInitialSender(): string {
  try {
    return window.sessionStorage.getItem(SENDER_STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function parseIsoSeconds(iso: string): number {
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

function adapterLabel(adapterId: string, conversationId: string): string {
  if (adapterId === "console" && conversationId === "web-admin") return "私聊窗口";
  if (adapterId === "console") return "本地终端";
  if (adapterId === "voice_input" || adapterId.includes("voice")) return "现场收音";
  if (adapterId.includes("qq")) return "QQ 频道";
  if (adapterId.includes("phone")) return "随身手机";
  return adapterId;
}

function replyChannelLabel(channel: string): string {
  switch (channel) {
    case "console":
      return "当面回应";
    case "voice":
      return "现场语音";
    case "qq":
      return "QQ 消息";
    case "phone":
      return "手机消息已入队";
    default:
      return channel;
  }
}

function buildStream(feed: ChatFeed): StreamEntry[] {
  const entries: StreamEntry[] = [
    ...feed.inputs.map((item): StreamEntry => ({
      kind: "input",
      timeSec: parseIsoSeconds(item.received_at || item.occurred_at),
      item,
    })),
    ...feed.actions
      .filter((act) => act.replies.length > 0 || act.calls.length > 0 || act.error !== null)
      .map((item): StreamEntry => ({
        kind: "action",
        timeSec: item.created_at,
        item,
      })),
  ];
  entries.sort((a, b) => a.timeSec - b.timeSec);
  return entries;
}

export function Chat({
  client,
  state,
  botName,
  onBotNameChange,
  go,
}: {
  client: PanelClient;
  state: PanelState;
  botName: string;
  onBotNameChange: (name: string) => void;
  go: (route: Route) => void;
}): ReactElement {
  const [feed, setFeed] = useState<ChatFeed | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [senderName, setSenderName] = useState(readInitialSender);
  const [editingSender, setEditingSender] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const prevCountRef = useRef(0);

  const reload = async (): Promise<void> => {
    try {
      const next = await client.chatFeed(50);
      setFeed(next);
      const liveCharName = next.character_name.trim();
      if (liveCharName && liveCharName !== "主体" && liveCharName !== botName) {
        onBotNameChange(liveCharName);
      }
      setLoadError(null);
    } catch (err: unknown) {
      setLoadError(err instanceof Error ? err.message : "读取对话流水失败。");
    }
  };

  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void reload();
    }, 1800);
    return () => window.clearInterval(timer);
  }, [client]);

  const entries = feed ? buildStream(feed) : [];
  useEffect(() => {
    if (entries.length > prevCountRef.current) {
      bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }
    prevCountRef.current = entries.length;
  }, [entries.length]);

  const updateSender = (val: string): void => {
    setSenderName(val);
    try {
      window.sessionStorage.setItem(SENDER_STORAGE_KEY, val);
    } catch {
      // 忽略隐私模式限制
    }
  };

  const handleSend = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await client.sendChat(trimmed, senderName.trim() || "伙伴");
      setDraft("");
      await reload();
      void client.refresh();
    } catch (err: unknown) {
      setSendError(err instanceof Error ? err.message : "发送消息失败。");
    } finally {
      setSending(false);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSend();
    }
  };

  const applyStarter = (text: string): void => {
    setDraft(text);
    textareaRef.current?.focus();
  };

  const resolvedBot =
    (feed?.character_name && feed.character_name !== "主体" ? feed.character_name.trim() : "") || botName.trim();
  const personaName = resolvedBot || "她";
  const busy = feed?.foreground_busy ?? state.snapshot?.foreground?.busy ?? false;
  const canProcess = feed?.can_process ?? state.snapshot?.health.can_process ?? false;
  const activeSender = senderName.trim() || "伙伴";

  const starters: readonly { label: string; text: string }[] = [
    { label: "打个招呼", text: resolvedBot ? `嗨${resolvedBot}，今天在忙些什么呀？` : "嗨，今天状态怎么样呀？" },
    { label: "记一条随笔", text: "帮我在 journal 手账里记下今天的心情和刚刚聊到的事吧！" },
    { label: "翻翻共同记忆", text: "翻翻你的笔记本，最近有没有记住关于我的新习惯？" },
  ];

  return (
    <>
      <PageHeading
        eyebrow="COMPANION CHAT // 即时对话空间"
        title={`与${personaName}私聊`}
        aside={
          busy ? (
            <StateMark state="reasoning" label="认真组织回应中…" live />
          ) : canProcess ? (
            <StateMark state="completed" label="在线听你说" />
          ) : (
            <StateMark state="warning" label="等待引擎就绪" />
          )
        }
      >
        在这里发出的每一句话都会第一时间唤醒{personaName}，并同步收拢文字回应、现场语音与手账工具动作。
      </PageHeading>

      {feed && !feed.console_enabled ? (
        <Feedback
          state="warning"
          scope="region"
          urgency="actionable"
          persistence="retained"
          title="当前未开启网页文字回复通道"
          actions={
            <Button variant="secondary" onClick={() => go({ page: "system", id: null })}>
              前往设置开启本地对话输出
            </Button>
          }
        >
          如果未同时开启语音或 QQ 插件，{personaName}可能没有直接回话的出口。建议在设置中勾选「启用控制台与网页文字回复」。
        </Feedback>
      ) : null}

      {loadError ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="暂时无法刷新聊天记录">
          {loadError}
        </Feedback>
      ) : null}

      <div className="chat-workspace">
        <section className="chat-room" aria-labelledby="chat-room-title">
          <header className="chat-room-header">
            <div className="chat-persona-card">
              <span className="chat-avatar-badge" aria-hidden="true">
                <BrandMark />
              </span>
              <div className="chat-persona-copy">
                <div className="chat-persona-row">
                  <h2 id="chat-room-title">{resolvedBot || "陪伴频道"}</h2>
                  <span className="companion-pill">{busy ? "正在思考措辞" : canProcess ? "频道畅通" : "离线待命"}</span>
                </div>
                <p className="item-subtitle">
                  {busy
                    ? "耳朵竖起思考中，正在认真组织下一步回应…"
                    : canProcess
                      ? "随时可以聊日常、记手账或交办小任务。"
                      : "本地宿主准备好后，这里会立刻恢复对话。"}
                </p>
              </div>
            </div>
            <div className="chat-room-tools">
              <Button variant="ghost" onClick={() => go({ page: "memory", id: null })}>
                翻阅手账
              </Button>
              <Button variant="ghost" onClick={() => void reload()}>
                刷新记录
              </Button>
            </div>
          </header>

          {entries.length === 0 ? (
            <div className="chat-welcome">
              <span className="pc-eyebrow">NEW TOPIC // 开启今日话题</span>
              <h3>还没有新的聊天泡泡，先和{personaName}打个招呼吧</h3>
              <p className="muted">点选下方任一话题卡即可填入输入框，或直接输入想说的话按 Enter 发送。</p>
              <div className="chat-starter-grid">
                {starters.map((starter) => (
                  <button
                    key={starter.label}
                    type="button"
                    className="chat-starter-card pc-focusable"
                    onClick={() => applyStarter(starter.text)}
                  >
                    <span className="chat-starter-tag">{starter.label}</span>
                    <span className="chat-starter-text">{starter.text}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="chat-stream" role="log" aria-live="polite">
              {entries.map((entry) => {
                if (entry.kind === "input") {
                  const inp = entry.item;
                  return (
                    <ChatBubble
                      key={`in-${inp.input_id}`}
                      speaker="user"
                      author={inp.display_name || activeSender}
                      badge={`${adapterLabel(inp.adapter_id, inp.conversation_id)} · ${stageName(inp.stage)}`}
                      timestamp={time(entry.timeSec || null)}
                    >
                      {inp.text}
                    </ChatBubble>
                  );
                }

                const act = entry.item;
                const nonReplyCalls = act.calls.filter((c) => !act.replies.some((r) => r.call_id === c.call_id));
                const primaryChannel = act.replies.length > 0 ? replyChannelLabel(act.replies[0]!.channel) : "执行了伴随动作";

                return (
                  <ChatBubble
                    key={`act-${act.action_id}`}
                    speaker="agent"
                    author={resolvedBot || "回应"}
                    badge={primaryChannel}
                    state={mark(act.state).state}
                    timestamp={time(act.created_at)}
                    actions={
                      <Button variant="ghost" onClick={() => go({ page: "reviews", id: act.action_id })}>
                        回看本轮
                      </Button>
                    }
                  >
                    <div className="chat-agent-stack">
                      {act.replies.map((rep, index) => (
                        <div key={rep.call_id} className="chat-reply-block">
                          {act.replies.length > 1 || index > 0 ? (
                            <span className="chat-channel-tag">{replyChannelLabel(rep.channel)}</span>
                          ) : null}
                          <p className="chat-text">
                            {rep.text}
                            {rep.truncated ? " …（已截断）" : ""}
                          </p>
                        </div>
                      ))}
                      {nonReplyCalls.length > 0 ? (
                        <div className="chat-calls-strip" aria-label="伴随能力调用">
                          {nonReplyCalls.map((call) => (
                            <span key={call.call_id} className="chat-call-chip">
                              <Icon name="ring" />
                              <span>
                                {capabilityName(call.path, state.snapshot)} · {call.path.split(".").at(-1)}（{mark(call.state).label}）
                              </span>
                            </span>
                          ))}
                        </div>
                      ) : null}
                      {act.error ? <p className="chat-error-note">执行遇到小状况：{act.error}</p> : null}
                    </div>
                  </ChatBubble>
                );
              })}
              <div ref={bottomRef} />
            </div>
          )}

          <form className="chat-composer" onSubmit={(e) => void handleSend(e)}>
            {sendError ? (
              <Feedback state="failed" scope="object" urgency="passive" persistence="retained" title="消息还没发出去">
                {sendError}
              </Feedback>
            ) : null}

            <div className="chat-composer-meta">
              <div className="chat-identity-bar">
                <span className="chat-sender-pill">发送身份：{activeSender}</span>
                <button
                  type="button"
                  className="chat-sender-toggle pc-focusable"
                  aria-expanded={editingSender}
                  onClick={() => setEditingSender((prev) => !prev)}
                >
                  {editingSender ? "收起称呼" : "设置你的称呼"}
                </button>
              </div>
              <span className="item-subtitle">Enter 发送 · Shift + Enter 换行</span>
            </div>

            <AnimatePresence initial={false}>
              {editingSender ? (
                <motion.div
                  key="sender-editor"
                  className="chat-sender-editor"
                  initial={{ opacity: 0, y: -4 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={transitions.snap}
                >
                  <Field label="你的称呼" description={`作为消息发送者名字呈现给${personaName}`}>
                    {(control) => (
                      <input
                        {...control}
                        type="text"
                        value={senderName}
                        maxLength={40}
                        placeholder="伙伴"
                        onChange={(e) => updateSender(e.target.value)}
                      />
                    )}
                  </Field>
                </motion.div>
              ) : null}
            </AnimatePresence>

            <div className="chat-composer-bar">
              <Field label={`写给${personaName}的话`}>
                {(control) => (
                  <textarea
                    {...control}
                    ref={textareaRef}
                    rows={2}
                    placeholder={`和${personaName}说点什么吧，例如：今天过得怎么样？帮我记一条随笔…`}
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={handleKeyDown}
                  />
                )}
              </Field>
              <Button
                type="submit"
                loading={sending}
                disabledReason={
                  !state.online && !canProcess
                    ? "正在等待 Engine 就绪。"
                    : !draft.trim()
                      ? "先写一句想说的话吧。"
                      : undefined
                }
              >
                发送消息
              </Button>
            </div>
          </form>
        </section>
      </div>
    </>
  );
}
