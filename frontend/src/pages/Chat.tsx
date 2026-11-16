/**
 * 对话调试台：直接在浏览器里向主体发送唤醒消息，实时查看她的回复与能力调用。
 */
import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent, type ReactElement } from "react";
import { Button, Feedback, Field } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import type { ChatActionItem, ChatFeed, ChatInputItem } from "../model/admin";
import { capabilityName, stageName } from "../model/names";
import type { Route } from "../model/routes";
import { mark } from "../semantics";
import { EmptyState, PageHeading, Section, StateMark, time } from "../ui/common";

type StreamEntry =
  | { readonly kind: "input"; readonly timeSec: number; readonly item: ChatInputItem }
  | { readonly kind: "action"; readonly timeSec: number; readonly item: ChatActionItem };

function parseIsoSeconds(iso: string): number {
  const parsed = Date.parse(iso);
  return Number.isFinite(parsed) ? parsed / 1000 : 0;
}

function adapterLabel(adapterId: string, conversationId: string): string {
  if (adapterId === "console" && conversationId === "web-admin") return "网页后台";
  if (adapterId === "console") return "本地终端";
  if (adapterId === "voice_input" || adapterId.includes("voice")) return "麦克风收音";
  if (adapterId.includes("qq")) return "QQ";
  if (adapterId.includes("phone")) return "手机端";
  return adapterId;
}

function replyChannelLabel(channel: string): string {
  switch (channel) {
    case "console":
      return "控制台回复";
    case "voice":
      return "现场语音";
    case "voice_file":
      return "合成语音文件";
    case "qq":
      return "QQ 消息";
    case "phone":
      return "手机消息";
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
  go,
}: {
  client: PanelClient;
  state: PanelState;
  go: (route: Route) => void;
}): ReactElement {
  const [feed, setFeed] = useState<ChatFeed | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [senderName, setSenderName] = useState("拾风");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const prevCountRef = useRef(0);

  const reload = async (): Promise<void> => {
    try {
      const next = await client.chatFeed(50);
      setFeed(next);
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

  const handleSend = async (event?: FormEvent): Promise<void> => {
    event?.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed || sending) return;
    setSending(true);
    setSendError(null);
    try {
      await client.sendChat(trimmed, senderName.trim() || "Web 管理员");
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

  const personaName = feed?.character_name || "她";
  const busy = feed?.foreground_busy ?? state.snapshot?.foreground?.busy ?? false;
  const canProcess = feed?.can_process ?? state.snapshot?.health.can_process ?? false;

  return (
    <>
      <PageHeading
        title={`与${personaName}对话`}
        aside={
          busy ? (
            <StateMark state="reasoning" label="正在思考回应…" live />
          ) : canProcess ? (
            <StateMark state="completed" label="在线待命" />
          ) : (
            <StateMark state="warning" label="等待引擎就绪" />
          )
        }
      >
        在这里发送的消息会以最高唤醒优先级交给主脑处理，并同步汇总控制台文字、现场语音与插件动作。
      </PageHeading>

      {feed && !feed.console_enabled ? (
        <Feedback
          state="warning"
          scope="region"
          urgency="actionable"
          persistence="retained"
          title="当前未开启 console 文字输出通道"
          actions={
            <Button variant="secondary" onClick={() => go({ page: "system", id: null })}>
              前往设置开启本地对话输出
            </Button>
          }
        >
          如果也没有启用语音或 QQ 插件，主脑可能没有直接向你回话的出口。建议在设置中开启「启用控制台与网页文字回复」。
        </Feedback>
      ) : null}

      {loadError ? (
        <Feedback state="failed" scope="region" urgency="passive" persistence="retained" title="无法刷新对话记录">
          {loadError}
        </Feedback>
      ) : null}

      <div className="chat-workspace">
        <Section
          id="chat-stream"
          title="实时对话与行动流水"
          aside={
            <Button variant="ghost" onClick={() => void reload()}>
              立即刷新
            </Button>
          }
        >
          {entries.length === 0 ? (
            <EmptyState icon="person" title="还没有对话记录">
              在下方输入框写一句话按回车发送，即可直接测试主脑的回应、工具调用与性格表现。
            </EmptyState>
          ) : (
            <div className="chat-stream">
              {entries.map((entry) => {
                if (entry.kind === "input") {
                  const inp = entry.item;
                  return (
                    <article key={`in-${inp.input_id}`} className="chat-bubble" data-role="user">
                      <header className="chat-meta">
                        <strong>{inp.display_name || "用户"}</strong>
                        <span className="section-count">{adapterLabel(inp.adapter_id, inp.conversation_id)}</span>
                        <span className="muted">{stageName(inp.stage)}</span>
                        <span className="muted">{time(entry.timeSec || null)}</span>
                      </header>
                      <p className="chat-text">{inp.text}</p>
                    </article>
                  );
                }

                const act = entry.item;
                const nonReplyCalls = act.calls.filter(
                  (c) => !act.replies.some((r) => r.call_id === c.call_id),
                );
                return (
                  <article key={`act-${act.action_id}`} className="chat-bubble" data-role="agent">
                    <header className="chat-meta">
                      <strong>{personaName}</strong>
                      <StateMark {...mark(act.state)} />
                      <span className="muted">{time(act.created_at)}</span>
                      <Button variant="ghost" onClick={() => go({ page: "reviews", id: act.action_id })}>
                        审查本轮
                      </Button>
                    </header>
                    {act.replies.map((rep) => (
                      <div key={rep.call_id} className="chat-reply-block">
                        <span className="eyebrow">{replyChannelLabel(rep.channel)}</span>
                        <p className="chat-text">
                          {rep.text}
                          {rep.truncated ? " …（已截断）" : ""}
                        </p>
                      </div>
                    ))}
                    {nonReplyCalls.length > 0 ? (
                      <div className="chat-calls-strip">
                        {nonReplyCalls.map((call) => (
                          <span key={call.call_id} className="section-count">
                            {capabilityName(call.path, state.snapshot)} · {call.path.split(".").at(-1)}（{mark(call.state).label}）
                          </span>
                        ))}
                      </div>
                    ) : null}
                    {act.error ? <p className="muted">执行异常：{act.error}</p> : null}
                  </article>
                );
              })}
              <div ref={bottomRef} />
            </div>
          )}
        </Section>

        <form className="section chat-composer" onSubmit={(e) => void handleSend(e)}>
          {sendError ? (
            <Feedback state="failed" scope="object" urgency="passive" persistence="retained" title="发送未成功">
              {sendError}
            </Feedback>
          ) : null}
          <div className="chat-composer-fields">
            <Field label="你的称呼" description="作为消息发送者名称呈现给主脑">
              {(control) => (
                <input
                  {...control}
                  type="text"
                  value={senderName}
                  maxLength={40}
                  onChange={(e) => setSenderName(e.target.value)}
                />
              )}
            </Field>
            <Field label="发送消息（Enter 发送，Shift + Enter 换行）">
              {(control) => (
                <textarea
                  {...control}
                  rows={3}
                  placeholder={`对${personaName}说点什么，例如：嗨，今天过得怎么样？帮我记一条随笔吧…`}
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={handleKeyDown}
                />
              )}
            </Field>
          </div>
          <div className="actions">
            <Button
              type="submit"
              loading={sending}
              disabledReason={
                !state.online && !canProcess
                  ? "正在等待 Engine 就绪。"
                  : !draft.trim()
                    ? "请输入要发送的消息内容。"
                    : undefined
              }
            >
              发送消息
            </Button>
          </div>
        </form>
      </div>
    </>
  );
}
