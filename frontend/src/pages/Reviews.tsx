/**
 * 人工审查从持久行动历史分页读取，选择只加载原文，不运行模型或重做行动。
 * 评价使用现有稳定控制身份；材料版本由宿主再次核验，原始记录保持不变。
 */
import { useEffect, useState } from "react";
import type { ReactElement } from "react";
import { Button } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import type { Action, Review } from "../contracts";
import type { Route } from "../model/routes";
import { actionTitle } from "../model/timeline";
import { capabilityName } from "../model/names";
import { awaiting } from "../client";
import { mark } from "../semantics";
import { EmptyNote, PageHeading, StateMark, TechDetails, time, Unavailable } from "../ui/common";

function failure(error: unknown): string { return error instanceof Error ? error.message : "无法读取审查材料，请重试。"; }

/** 用原始时间换算本地显示，损坏时间保留原文，不猜测为当前时刻。 */
function stimulusTime(value: string): string {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? time(timestamp / 1000) : value;
}

/** 分页浏览权威历史，切换宿主或游标时丢弃迟到的旧查询结果。 */
export function Reviews({ client, state, selected, go }: { client: PanelClient; state: PanelState; selected: string | null; go: (route: Route) => void }): ReactElement {
  const [cursor, setCursor] = useState<string | null>(null);
  const [page, setPage] = useState<{ items: Action[]; next_cursor: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    setPage(null); setError(null);
    void client.reviews(cursor).then((value) => { if (active) setPage(value); }, (reason: unknown) => { if (active) setError(failure(reason)); });
    return () => { active = false; };
  }, [client, cursor, refresh, state.snapshot?.instance_id]);
  return <>
    <PageHeading title="行为审查">查看当时的刺激与实际行动，告诉她哪里做得好、哪里需要改进。评价会交给主脑和独立反思。</PageHeading>
    <div className="actions"><Button variant="secondary" onClick={() => { setCursor(null); setRefresh((v) => v + 1); }}>刷新最新记录</Button></div>
    {error ? <p role="alert">{error}</p> : page === null ? <p role="status">正在读取行动历史…</p> : page.items.length === 0 ? <EmptyNote icon="timeline">还没有可审查的行动。</EmptyNote> : <div className="item-list">
      {page.items.map((action) => <button key={action.action_id} className="item-row pc-focusable" data-interactive="true" aria-current={selected === action.action_id ? "true" : undefined} onClick={() => go({ page: "reviews", id: action.action_id })}>
        <span className="item-text"><strong>{actionTitle(action, state.snapshot)}</strong><span className="item-subtitle">{time(action.created_at)} · {action.calls.length} 项操作</span></span>
        <StateMark {...mark(action.state)} />
      </button>)}
    </div>}
    {page?.next_cursor ? <Button variant="secondary" onClick={() => setCursor(page.next_cursor)}>查看更早的行动</Button> : null}
    <p className="page-note">这里包含持久保存的行动历史。未关联主脑轮次的手动行动会明确标注。</p>
  </>;
}

/** 展示完整刺激和回执，评分与执行结果分开；断线和受理未知期间不创建新评价。 */
export function ReviewDetail({ client, state, actionId }: { client: PanelClient; state: PanelState; actionId: string }): ReactElement {
  const [data, setData] = useState<Review | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [rating, setRating] = useState<"good" | "bad" | null>(null);
  const [comment, setComment] = useState("");
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(true);
  const instance = state.snapshot?.instance_id;
  const entry = state.entries.find((item) => item.request.instance_id === instance && item.request.target === `action:${actionId}` && item.request.operation === "review");
  const pending = entry !== undefined && awaiting(entry);
  const resultState = entry?.result?.state;
  useEffect(() => {
    let active = true;
    // 保留已读材料和表单，刷新不能先卸载输入框或让已保存评价闪回空白页。
    setLoading(true); setError(null);
    void client.review(actionId).then((value) => { if (active) setData(value); }, (reason: unknown) => { if (active) setError(failure(reason)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [client, actionId, instance, refresh, resultState]);
  const disabled = !state.online ? "本机服务暂时不在线。" : state.snapshot?.phase !== "running" || !state.snapshot.authority_available ? "宿主当前不能接管评价。"
    : data === null || loading ? "请先取得完整审查材料。" : error !== null ? "请先刷新审查材料。" : data.action.state === "running" ? "请等待行动结束，再刷新材料。" : pending ? "请先确认上一条评价是否保存。" : rating === null ? "请选择好或坏。" : undefined;
  async function submit(): Promise<void> {
    if (disabled !== undefined || data === null || rating === null || sending) return;
    setSending(true);
    try {
      await client.submit(`action:${actionId}`, "review", data.revision, { rating, comment }, `行动评价：${rating === "good" ? "好" : "坏"}`);
      setRefresh((value) => value + 1);
    } finally { setSending(false); }
  }
  return <div className="detail review-detail">
    <h2>审查这次行动</h2>
    <Button variant="ghost" onClick={() => setRefresh((v) => v + 1)}>刷新完整材料</Button>
    {error ? <p role="alert">{error}</p> : data === null ? <p role="status">正在读取完整刺激和行动…</p> : <>
      <StateMark {...mark(data.action.state)} />
      <section><h3>当时收到的刺激</h3>
        {data.turn_id === null ? <p className="muted">这次行动未关联主脑轮次；以下仅展示明确关联的输入。</p> : null}
        {data.stimuli.length === 0 ? <Unavailable>没有关联的输入刺激，可能来自手动控制或嵌入调用。</Unavailable> : data.stimuli.map((s) => <article className="call-item" key={s.input_id}>
          <p><strong>{s.display_name ?? s.adapter_id}</strong></p><p className="muted">发生：{stimulusTime(s.occurred_at)} · 接收：{stimulusTime(s.received_at)}</p>
          <pre>{s.text || "（无文字正文）"}</pre>
          {s.media.map((m) => {
            const url = `/api/reviews/${encodeURIComponent(actionId)}/media/${encodeURIComponent(m.media_id)}`;
            return <div key={m.media_id} className="review-media">
              {["image/png", "image/jpeg", "image/gif", "image/webp"].includes(m.media_type) ? <img src={url} alt={m.name ?? "原始刺激图片"} loading="lazy" />
                : m.media_type.startsWith("audio/") ? <audio src={url} controls preload="none" aria-label={m.name ?? "原始刺激音频"} /> : null}
              <a href={url} download>下载原始媒体：{m.name ?? m.media_type}</a>
            </div>;
          })}
        </article>)}
      </section>
      <section><h3>实际行动与完整回执</h3>
        {data.action.calls.map((call) => <article className="call-item" key={call.call_id}>
          <p><strong>{capabilityName(call.path, state.snapshot)}</strong></p><StateMark {...mark(call.state)} />
          <p className="muted">调用参数</p><pre>{call.arguments_json}</pre>
          {call.result_json !== null ? <><p className="muted">实际结果</p><pre>{call.result_json}</pre></> : null}
          {call.error !== null ? <p>{call.error}</p> : null}
        </article>)}
        {data.action.error !== null ? <p>{data.action.error}</p> : null}
        <details><summary>行动源码与内部结果</summary><pre>{data.action.source}</pre>{data.action.output ? <pre>{data.action.output}</pre> : null}{data.action.result_json !== null ? <pre>{data.action.result_json}</pre> : null}</details>
      </section>
      <form className="review-form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
        <h3>你的评价</h3><fieldset className="toolbar" disabled={sending || pending}><legend>这次做得怎么样？</legend>
          {(["good", "bad"] as const).map((value) => <label className="filter" key={value} data-selected={rating === value}><input type="radio" name={`rating-${actionId}`} value={value} checked={rating === value} onChange={() => setRating(value)} />{value === "good" ? "好" : "坏"}</label>)}
        </fieldset>
        <label className="search-field">留言（可选）<textarea rows={5} maxLength={4000} disabled={sending || pending} value={comment} onChange={(event) => setComment(event.target.value)} placeholder="为什么这样做不好？怎么做会更好？也可以说明哪些表现值得保持。" /></label>
        <Button type="submit" variant="secondary" loading={sending} disabledReason={disabled}>提交评价</Button>
      </form>
      {entry ? <div role="status">
        {entry.result?.error ?? entry.error ?? (data.feedback.some((f) => f.request_id === entry.request.request_id) ? "评价已保存，已加入主脑与反思的可靠待办。" : "正在确认评价是否保存。")}
        {entry.unresolved ? <div className="actions"><Button variant="ghost" disabledReason={!state.online ? "本机服务暂时不在线。" : undefined} onClick={() => void client.observe(entry.request.request_id)}>查询原评价</Button><Button variant="ghost" loading={entry.sending} disabledReason={!state.online ? "本机服务暂时不在线。" : undefined} onClick={() => void client.retry(entry.request.request_id)}>原样重传</Button></div> : null}
      </div> : null}
      <section><h3>评价记录（最近 32 条）</h3>{data.feedback.length === 0 ? <p className="muted">还没有人工评价。</p> : data.feedback.map((f) => <article key={f.evidence_id} className="call-item"><p><strong>{f.rating === "good" ? "好" : "坏"}</strong> · {time(f.created_at)}</p><p className="review-comment">{f.comment || "未留言"}</p>{f.revision !== data.revision ? <p className="muted">这条评价针对之前的回执版本。</p> : null}</article>)}</section>
      <TechDetails items={[["行动", actionId], ["轮次", data.turn_id], ["审查版本", data.revision]]} />
    </>}
  </div>;
}
