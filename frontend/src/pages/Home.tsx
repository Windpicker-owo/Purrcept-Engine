/**
 * 首页以主体舞台、行动工作区和关注侧栏建立固定阅读顺序。
 * 舞台持有当前状态与行动路径；左栏浏览记录和持续目标，右栏汇总待处理与连接。
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
import { runningAction } from "../model/agent";
import type { AgentView } from "../model/agent";
import type { AttentionItem } from "../model/attention";
import { capabilityName, deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { actionTitle, timelineItems } from "../model/timeline";
import { duration, EmptyNote, ItemRow, relative, Section, StateMark } from "../ui/common";

/** 行动路径按调用顺序合并同一对象；未完成状态优先于已完成状态。 */
function pathNodes(action: Action, snapshot: Snapshot): ActionPathNode[] {
  const nodes: ActionPathNode[] = [{ id: "self", label: "她", icon: <Icon name="person" />, state: "completed" }];
  for (const call of action.calls) {
    const label = capabilityName(call.path, snapshot);
    const state = mark(call.state).state;
    const existing = nodes.findIndex((node) => node.label === label && node.id !== "self");
    const node: ActionPathNode = { id: `call:${label}`, label, icon: <Icon name="ring" />, state, detail: mark(call.state).label };
    if (existing < 0) nodes.push(node);
    else if (nodes[existing]?.state === "completed") nodes[existing] = node;
  }
  return nodes.slice(0, 5);
}

/** 展示当前主体和两个功能区域；快照刷新不会重挂载舞台或重复触发入场动画。 */
export function Home({ state, agent, attention, go }: { state: PanelState; agent: AgentView; attention: readonly AttentionItem[]; go: (route: Route) => void }): ReactElement {
  const pathRef = useRef<HTMLDivElement>(null);
  const attentionRef = useRef<HTMLElement>(null);
  const snapshot = state.snapshot;
  // 当前行动可能比最新完成记录更早，路径必须与吉祥物描述的工作保持一致。
  const latest = snapshot ? runningAction(snapshot) ?? snapshot.recent_actions[0] : undefined;
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
          {agent.focus === "attention" ? <Button onClick={() => go({ page: "attention", id: null })}>查看待处理事项</Button> : null}
        </div>
        <div className="home-mascot" aria-hidden="true"><Mascot state={agent.state} gazeTarget={gaze} /></div>
      </div>
      {snapshot !== null && latest !== undefined ? <div ref={pathRef} className="home-path">
        <div className="home-path-heading"><span className="eyebrow">{latest.state === "running" ? "当前行动" : "最近一次行动"}</span><Button variant="ghost" onClick={() => go({ page: "timeline", id: latest.action_id })}>查看这次行动</Button></div>
        {/* 路径仅呈现结果和方向，不与吉祥物争夺持续运动。 */}
        <MotionConfig reducedMotion="always"><ActionPath label={actionTitle(latest, snapshot)} nodes={pathNodes(latest, snapshot)} /></MotionConfig>
      </div> : null}
    </section>

    <div className="home-sections">
      <div className="home-work">
        <Section id="home-recent" title="最近发生" more={() => go({ page: "timeline", id: null })}
          aside={<span className="section-count">最近 {recent.length} 条</span>}>
          {recent.length === 0 ? <EmptyNote icon="timeline">还没有行动记录。她开始做事后会出现在这里。</EmptyNote> : <div className="item-list">
            {recent.map((item) => <ItemRow key={item.id} icon={item.kind === "action" ? "timeline" : "settings"} title={item.title}
              subtitle={`${relative(item.at, now)} · ${item.kind === "action" ? `${item.action.calls.length} 项操作` : "我的操作"}`}
              trailing={item.status ? <StateMark {...item.status} /> : undefined}
              onClick={() => go({ page: "timeline", id: item.id })} />)}
          </div>}
        </Section>
        <Section id="home-activities" title="持续目标" more={() => go({ page: "activities", id: null })}
          aside={<span className="section-count">{running.length} 项</span>}>
          {running.length === 0 ? <EmptyNote icon="activity">没有持续推进的目标。她开始一项活动时会出现在这里。</EmptyNote>
            : <div className="item-list">{running.slice(0, 3).map((activity) => <ItemRow key={activity.activity_id} icon="activity" title={activity.goal}
              subtitle={`还剩 ${duration(activity.remaining_seconds)}`} trailing={<StateMark {...mark(activity.state)} />}
              onClick={() => go({ page: "activities", id: activity.activity_id })} />)}</div>}
        </Section>
      </div>

      <aside ref={attentionRef} className="home-context" aria-label="关注与连接">
        <Section id="home-attention" title="需要你关注" more={attention.length > 0 ? () => go({ page: "attention", id: null }) : undefined}
          aside={<span className="section-count">{snapshot ? `${attention.length} 项` : "等待状态"}</span>}>
          {snapshot === null ? <EmptyNote icon="inbox">取得本机状态后，这里会显示需要关注的事。</EmptyNote>
            : attention.length === 0 ? <EmptyNote>没有需要处理的事。</EmptyNote>
              : <div className="attention-summary">{attention.slice(0, 3).map((item) => <div key={item.id} className="attention-summary-item">
                <StateMark state={item.state} label={item.title} />
                <p className="item-subtitle">{item.impact}</p>
                <Button variant="ghost" onClick={() => go(item.route)}>{item.next}</Button>
              </div>)}</div>}
        </Section>
        <Section id="home-devices" title="连接与感官" more={() => go({ page: "devices", id: null })}
          aside={snapshot ? <span className="section-count">{snapshot.devices.length} 个</span> : undefined}>
          {!snapshot ? <EmptyNote icon="device">连接本机服务后，这里会显示设备状态。</EmptyNote>
            : snapshot.devices.length === 0 ? <EmptyNote icon="device">还没有启用设备，前往设备页了解如何连接。</EmptyNote>
              : <div className="device-summary">{snapshot.devices.slice(0, 3).map((device) => <button key={device.instance_id} className="device-summary-row pc-focusable"
                onClick={() => go({ page: "devices", id: device.instance_id })}>
                <span>{deviceName(device)}</span><StateMark {...deviceMark(device)} /><Icon name="chevron" />
              </button>)}</div>}
        </Section>
      </aside>
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
