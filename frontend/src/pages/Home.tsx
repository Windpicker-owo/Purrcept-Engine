/**
 * 首页以主体舞台、行动工作区和关注侧栏建立固定阅读顺序。
 * 舞台持有当前状态与关联行动；正文优先待处理，其后是记录、目标和连接，桌面按区域排布。
 * 全部内容来自同一宿主快照，入口只导航；吉祥物拥有首页唯一的持续运动。
 */
import { useRef } from "react";
import type { ReactElement } from "react";
import { MotionConfig } from "motion/react";
import { ActionPath, Button, Icon, Mascot, STATES } from "@purrcept/cel";
import type { ActionPathNode } from "@purrcept/cel";
import type { PanelState } from "../client";
import type { Action, Snapshot } from "../contracts";
import { deviceMark, mark } from "../semantics";
import { summarizePath } from "../model/home-path";
import { runningAction } from "../model/agent";
import type { AgentView } from "../model/agent";
import type { AttentionItem } from "../model/attention";
import { capabilityName, deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { actionTitle, timelineItems } from "../model/timeline";
import { duration, EmptyNote, ItemRow, relative, Section, StateMark } from "../ui/common";

/** 摘要中的每个节点保留调用身份和原位置，相同设备上的两次调用不能合并。 */
function pathNodes(action: Action, snapshot: Snapshot): ActionPathNode[] {
  return [
    { id: "self", label: "她", icon: <Icon name="person" />, state: "completed" },
    ...summarizePath(action).visible.map((call) => ({
      id: call.call_id, label: capabilityName(call.path, snapshot), icon: <Icon name="ring" />,
      state: mark(call.state).state, detail: `第 ${action.calls.indexOf(call) + 1} 项 · ${mark(call.state).label}`,
    })),
  ];
}

/**
 * 组织当前主体状态、近期行动与关注事项。
 *
 * state、agent、attention 必须来自同一共享观察事实，go 只定位完整页面。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Home({ state, agent, attention, go }: { state: PanelState; agent: AgentView; attention: readonly AttentionItem[]; go: (route: Route) => void }): ReactElement {
  const pathRef = useRef<HTMLDivElement>(null);
  const attentionRef = useRef<HTMLElement>(null);
  const snapshot = state.snapshot;
  // 当前行动可能比最新完成记录更早，路径必须与吉祥物描述的工作保持一致。
  const primaryAttention = attention.find((item) => item.needsUser) ?? attention[0];
  const relatedAction = primaryAttention?.route.page === "timeline"
    ? snapshot?.recent_actions.find((action) => action.action_id === primaryAttention.route.id) : undefined;
  // 待处理状态只关联它自己的行动，不把另一条已完成记录接在异常提示下面。
  const latest = agent.focus === "attention" ? relatedAction : snapshot ? runningAction(snapshot) ?? snapshot.recent_actions[0] : undefined;
  const pathSummary = latest ? summarizePath(latest) : null;
  const gaze = agent.focus === "path" ? pathRef : agent.focus === "attention" ? attentionRef : undefined;
  const running = snapshot?.activities.filter((activity) => !["ended", "completed"].includes(activity.state)) ?? [];
  const recent = timelineItems(snapshot, state.entries, "all").slice(0, 5);
  const now = Date.now();

  return <>
    <section className="home-stage" aria-label="主体状态">
      <div className="home-overview">
        <div className="home-stage-block pc-field" data-family={STATES[agent.state].family} aria-hidden="true" />
        <div className="home-copy">
          <div className="home-kicker"><span className="eyebrow">此刻 · PURRCEPT ENGINE</span><StateMark state={agent.state} label={stateWord(agent.state)} /></div>
          <h1 aria-live="polite">{agent.headline}</h1>
          <p className="home-detail">{agent.detail}</p>
          {agent.focus === "attention" && primaryAttention ? <Button onClick={() => go(attention.length === 1 ? primaryAttention.route : { page: "attention", id: null })}>
            {attention.length === 1 ? primaryAttention.next : "查看待处理事项"}
          </Button> : null}
        </div>
        <div className="home-mascot" aria-hidden="true"><Mascot state={agent.state} gazeTarget={gaze} /></div>
      </div>
      {snapshot !== null && latest !== undefined ? <div ref={pathRef} className="home-path">
        <div className="home-path-heading"><span className="eyebrow">{agent.focus === "attention" ? "需要核对的行动" : latest.state === "running" ? "当前行动" : "最近一次行动"}</span><Button variant="ghost" onClick={() => go({ page: "timeline", id: latest.action_id })}>查看这次行动</Button></div>
        {/* 路径仅呈现结果和方向，不与吉祥物争夺持续运动。 */}
        <MotionConfig reducedMotion="always"><ActionPath label={actionTitle(latest, snapshot)} nodes={pathNodes(latest, snapshot)} /></MotionConfig>
        {pathSummary && pathSummary.omitted > 0 ? <p className="path-summary-note">另有 {pathSummary.omitted} 项操作未展示{pathSummary.hiddenAttention > 0 ? `，其中 ${pathSummary.hiddenAttention} 项失败或结果未知` : ""}。请查看完整行动。</p> : null}
      </div> : null}
    </section>

    <div className="home-sections">
      <aside ref={attentionRef} className="home-attention" aria-label="需要关注"><Section id="home-attention" title="需要你关注" more={attention.length > 0 ? () => go({ page: "attention", id: null }) : undefined}
          aside={<span className="section-count">{snapshot ? `${attention.length} 项` : "等待状态"}</span>}>
          {snapshot === null ? <EmptyNote icon="inbox">取得本机状态后，这里会显示需要关注的事。</EmptyNote>
            : attention.length === 0 ? <EmptyNote>没有需要处理的事。</EmptyNote>
              : <div className="attention-summary">{attention.slice(0, 3).map((item) => <div key={item.id} className="attention-summary-item">
                <StateMark state={item.state} label={item.title} />
                <p className="item-subtitle">{item.impact}</p>
                <Button variant="ghost" onClick={() => go(item.route)}>{item.next}</Button>
              </div>)}</div>}
        </Section></aside>
      <div className="home-recent"><Section id="home-recent" title="最近发生" more={() => go({ page: "timeline", id: null })}
          aside={<span className="section-count">最近 {recent.length} 条</span>}>
          {recent.length === 0 ? <EmptyNote icon="timeline">还没有行动记录。她开始做事后会出现在这里。</EmptyNote> : <div className="item-list">
            {recent.map((item) => <ItemRow key={item.id} icon={item.kind === "action" ? "timeline" : "settings"} title={item.title}
              subtitle={`${relative(item.at, now)} · ${item.kind === "action" ? `${item.action.calls.length} 项操作` : "我的操作"}`}
              trailing={item.status ? <StateMark {...item.status} /> : undefined}
              onClick={() => go({ page: "timeline", id: item.id })} />)}
          </div>}
        </Section></div>
      <div className="home-activities"><Section id="home-activities" title="持续目标" more={() => go({ page: "activities", id: null })}
          aside={<span className="section-count">{running.length} 项</span>}>
          {running.length === 0 ? <EmptyNote icon="activity">没有持续推进的目标。她开始一项活动时会出现在这里。</EmptyNote>
            : <div className="item-list">{running.slice(0, 3).map((activity) => <ItemRow key={activity.activity_id} icon="activity" title={activity.goal}
              subtitle={`还剩 ${duration(activity.remaining_seconds)}`} trailing={<StateMark {...mark(activity.state)} />}
              onClick={() => go({ page: "activities", id: activity.activity_id })} />)}</div>}
        </Section></div>
      <div className="home-devices"><Section id="home-devices" title="连接与感官" more={() => go({ page: "devices", id: null })}
          aside={snapshot ? <span className="section-count">{snapshot.devices.length} 个</span> : undefined}>
          {!snapshot ? <EmptyNote icon="device">连接本机服务后，这里会显示设备状态。</EmptyNote>
            : snapshot.devices.length === 0 ? <EmptyNote icon="device">还没有启用设备，前往设备页了解如何连接。</EmptyNote>
              : <div className="device-summary">{snapshot.devices.slice(0, 3).map((device) => <button key={device.instance_id} className="device-summary-row pc-focusable"
                onClick={() => go({ page: "devices", id: device.instance_id })}>
                <span>{deviceName(device)}</span><StateMark {...deviceMark(device)} /><Icon name="chevron" />
              </button>)}</div>}
        </Section></div>
    </div>
  </>;
}

/** Design 统一状态词，用于首页状态点旁的短标签。 */
function stateWord(state: AgentView["state"]): string {
  switch (state) {
    case "idle": return "空闲";
    case "perceiving": case "reasoning": case "acting": return "运行中";
    case "waiting-external": return "等待中";
    case "waiting-user": return "等你确认";
    case "warning": return "需要注意";
    case "paused": return "已暂停";
    case "completed": return "已完成";
    case "failed": return "失败";
  }
}
