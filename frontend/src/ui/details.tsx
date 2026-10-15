/**
 * Inspector 详情：行动的逐项结果与本页控制的送达情况。
 * 顺序遵循 Design：身份与状态 → 关键结果 → 可执行操作 → 技术细节（折叠）。
 * “行动已结束”不等于每项操作都生效：逐项按已经发生 / 结果未知 / 没有发生分组。
 */
import type { ReactElement } from "react";
import { Button, EFFECT_OUTCOMES } from "@purrcept/cel";
import type { PanelClient, PanelState, Entry } from "../client";
import type { Action, Call, Snapshot } from "../contracts";
import { controlMark, mark } from "../semantics";
import { capabilityName } from "../model/names";
import { actionTitle } from "../model/timeline";
import { StateMark, TechDetails, time, Unavailable } from "./common";

const GROUPS: readonly { key: string; title: string; family: string; match: (call: Call) => boolean }[] = [
  { key: "confirmed", title: EFFECT_OUTCOMES.committed.label, family: EFFECT_OUTCOMES.committed.family, match: (call) => call.state === "confirmed" },
  { key: "unknown", title: EFFECT_OUTCOMES.unknown.label, family: EFFECT_OUTCOMES.unknown.family, match: (call) => call.state === "unknown" },
  { key: "failed", title: "失败（已发生的部分不会回滚）", family: "failed", match: (call) => call.state === "failed" },
  { key: "pending", title: "进行中", family: "active", match: (call) => !["confirmed", "unknown", "not_started", "failed"].includes(call.state) },
  { key: "not_started", title: EFFECT_OUTCOMES.skipped.label, family: EFFECT_OUTCOMES.skipped.family, match: (call) => call.state === "not_started" },
];

/** 能力路径的末段是操作名；完整路径留在技术细节。 */
function operation(path: string): string { return path.split(".").at(-1) ?? path; }

export function ActionDetail({ action, snapshot }: { action: Action; snapshot: Snapshot | null }): ReactElement {
  return <div className="detail">
    <h2>{actionTitle(action, snapshot)}</h2>
    <StateMark {...mark(action.state)} />
    <p className="muted">开始于 {time(action.created_at)}{action.finished_at !== null ? ` · 结束于 ${time(action.finished_at)}` : ""}</p>
    {action.error ? <p>{action.error.text}{action.error.truncated ? " …（已截断）" : ""}</p> : null}
    {action.calls.length === 0 ? <Unavailable>这次行动没有调用任何外部操作。</Unavailable> : GROUPS.map((group) => {
      const calls = action.calls.filter(group.match);
      return calls.length === 0 ? null : <section key={group.key} className="call-group">
        <h3 className="pc-tint" data-family={group.family}>{group.title} · {calls.length}</h3>
        {calls.map((call) => <div className="call-item" key={call.call_id}>
          <p><strong>{capabilityName(call.path, snapshot)}</strong> <span className="identity">{operation(call.path)}</span></p>
          <StateMark {...mark(call.state)} />
          {call.error ? <p>{call.error.text}{call.error.truncated ? " …（已截断）" : ""}</p> : null}
          {call.result ? <pre>{call.result.text}{call.result.truncated ? `\n…（仅预览，原文 ${call.result.original_chars} 字）` : ""}</pre> : null}
          <TechDetails items={[["调用", call.call_id], ["能力路径", call.path], ["状态值", call.state], ["回执版本", call.receipt_revision], ["开始", time(call.started_at)], ["结束", time(call.finished_at)], ["错误类型", call.error_type], ["媒体", call.media_count]]} />
        </div>)}
      </section>;
    })}
    {action.calls.some((call) => call.state === "unknown") ? <p className="muted">结果未知的操作不会自动重做；请到对应应用中确认实际情况。</p> : null}
    <TechDetails items={[["行动", action.action_id], ["状态值", action.state], ["调用数", action.calls.length]]}>
      <p className="muted">“已经发生”来自对应插件的真实回执，不额外保证外部效果一直保持。</p>
    </TechDetails>
  </div>;
}

/** 本页控制：先说明是否送达，再给出关联行动或活动命令的真实结果。 */
export function ControlDetail({ entry, client, state, open }: { entry: Entry; client: PanelClient; state: PanelState; open: (actionId: string) => void }): ReactElement {
  const r = entry.result;
  const actual = r?.action;
  const expired = state.snapshot !== null && entry.request.instance_id !== state.snapshot.instance_id;
  const status = entry.sending ? { state: "waiting-external" as const, label: "正在发送" } : r ? controlMark(r)
    : entry.unavailable ? { state: "warning" as const, label: "历史回执不可用" }
    : entry.rejected ? { state: "failed" as const, label: "操作未被接受" } : { state: "warning" as const, label: "不确定是否已送达" };
  return <div className="detail">
    <h2>{entry.title}</h2>
    <StateMark {...status} />
    {expired ? <Unavailable>这条操作属于 Engine 重启之前，不能再重新发送。</Unavailable> : null}
    {entry.error ? <p>{entry.error}</p> : null}
    {r?.error ? <p>{r.error}</p> : null}
    {actual ? <><StateMark {...mark(actual.state)} /><Button variant="ghost" onClick={() => open(actual.action_id)}>查看逐项结果</Button></>
      : r?.action_id ? <Unavailable>已经交给她执行，还没有取得结果。</Unavailable> : null}
    {r?.activity_command ? <>
      <StateMark {...mark(r.activity_command.status)} />
      {r.activity_command.status !== "confirmed" ? <Unavailable>已收到不代表已经停稳，页面会继续查询同一条命令。</Unavailable> : null}
      {r.activity_command.error ? <p>{r.activity_command.error}</p> : null}
    </> : r?.command_id ? <Unavailable>活动命令已收到，还没有取得确认。</Unavailable> : null}
    <div className="actions">
      <Button variant="secondary" disabledReason={!state.online ? "本机服务暂时不在线。" : undefined} loading={entry.sending} onClick={() => void client.observe(entry.request.request_id)}>查询最新结果</Button>
      {entry.unresolved ? <Button variant="secondary" loading={entry.sending} disabledReason={!state.online ? "本机服务暂时不在线。" : expired ? "Engine 已重启，不能重新发送旧操作。" : undefined} onClick={() => void client.retry(entry.request.request_id)}>重新发送同一请求</Button> : null}
    </div>
    {entry.unresolved ? <p className="muted">重新发送使用原来的请求身份，宿主会去重，不会执行两次。</p> : null}
    <TechDetails items={[["请求", entry.request.request_id], ["宿主实例", entry.request.instance_id], ["目标", entry.request.target], ["操作", entry.request.operation], ["期望版本", entry.request.expected_revision], ["参数", entry.request.arguments], ["接管状态", r?.state], ["错误码", r?.error_code], ["关联行动", r?.action_id], ["关联命令", r?.command_id], ["实际状态", r?.activity_command?.actual_state], ["采用确认", r?.activity_command?.applied]]} />
  </div>;
}
