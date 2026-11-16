/**
 * 首页以主体舞台、快捷工作台入口、行动工作区和关注侧栏建立清晰阅读顺序。
 */
import { useRef, type ReactElement } from "react";
import { MotionConfig } from "motion/react";
import { ActionPath, Button, Icon, Mascot, STATES } from "@purrcept/cel";
import type { ActionPathNode } from "@purrcept/cel";
import type { PanelState } from "../client";
import type { Action, Snapshot } from "../contracts";
import { runningAction } from "../model/agent";
import type { AgentView } from "../model/agent";
import type { AttentionItem } from "../model/attention";
import { summarizePath } from "../model/home-path";
import { capabilityName, deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { actionTitle, timelineItems } from "../model/timeline";
import { deviceMark, mark } from "../semantics";
import { duration, EmptyNote, ItemRow, relative, Section, StateMark } from "../ui/common";

function pathNodes(action: Action, snapshot: Snapshot): ActionPathNode[] {
  return [
    { id: "self", label: "她", icon: <Icon name="person" />, state: "completed" },
    ...summarizePath(action).visible.map((call) => ({
      id: call.call_id,
      label: capabilityName(call.path, snapshot),
      icon: <Icon name="ring" />,
      state: mark(call.state).state,
      detail: `第 ${action.calls.indexOf(call) + 1} 项 · ${mark(call.state).label}`,
    })),
  ];
}

export function Home({
  state,
  agent,
  attention,
  go,
}: {
  state: PanelState;
  agent: AgentView;
  attention: readonly AttentionItem[];
  go: (route: Route) => void;
}): ReactElement {
  const pathRef = useRef<HTMLDivElement>(null);
  const attentionRef = useRef<HTMLElement>(null);
  const snapshot = state.snapshot;
  const primaryAttention = attention.find((item) => item.needsUser) ?? attention[0];
  const relatedAction =
    primaryAttention?.route.page === "timeline"
      ? snapshot?.recent_actions.find((action) => action.action_id === primaryAttention.route.id)
      : undefined;
  const latest =
    agent.focus === "attention" ? relatedAction : snapshot ? runningAction(snapshot) ?? snapshot.recent_actions[0] : undefined;
  const pathSummary = latest ? summarizePath(latest) : null;
  const gaze = agent.focus === "path" ? pathRef : agent.focus === "attention" ? attentionRef : undefined;
  const running = snapshot?.activities.filter((activity) => !["ended", "completed"].includes(activity.state)) ?? [];
  const recent = timelineItems(snapshot, state.entries, "all").slice(0, 5);
  const now = Date.now();

  return (
    <>
      <section className="home-stage" aria-label="主体状态">
        <div className="home-overview">
          <div className="home-stage-block pc-field" data-family={STATES[agent.state].family} aria-hidden="true" />
          <div className="home-copy">
            <div className="home-kicker">
              <span className="eyebrow">此刻 · PURRCEPT ENGINE</span>
              <StateMark state={agent.state} label={stateWord(agent.state)} />
            </div>
            <h1 aria-live="polite">{agent.headline}</h1>
            <p className="home-detail">{agent.detail}</p>
            <div className="actions">
              <Button onClick={() => go({ page: "chat", id: null })}>发起对话测试</Button>
              <Button variant="secondary" onClick={() => go({ page: "system", id: null })}>
                配置与人设
              </Button>
              <Button variant="secondary" onClick={() => go({ page: "memory", id: null })}>
                浏览记忆手账
              </Button>
              {agent.focus === "attention" && primaryAttention ? (
                <Button variant="ghost" onClick={() => go(attention.length === 1 ? primaryAttention.route : { page: "attention", id: null })}>
                  {attention.length === 1 ? primaryAttention.next : "查看待处理事项"}
                </Button>
              ) : null}
            </div>
          </div>
          <div className="home-mascot" aria-hidden="true">
            <Mascot state={agent.state} gazeTarget={gaze} />
          </div>
        </div>
        {snapshot !== null && latest !== undefined ? (
          <div ref={pathRef} className="home-path">
            <div className="home-path-heading">
              <span className="eyebrow">
                {agent.focus === "attention" ? "需要核对的行动" : latest.state === "running" ? "当前行动" : "最近一次行动"}
              </span>
              <Button variant="ghost" onClick={() => go({ page: "timeline", id: latest.action_id })}>
                查看这次行动
              </Button>
            </div>
            <MotionConfig reducedMotion="always">
              <ActionPath label={actionTitle(latest, snapshot)} nodes={pathNodes(latest, snapshot)} />
            </MotionConfig>
            {pathSummary && pathSummary.omitted > 0 ? (
              <p className="path-summary-note">
                另有 {pathSummary.omitted} 项操作未展示
                {pathSummary.hiddenAttention > 0 ? `，其中 ${pathSummary.hiddenAttention} 项失败或结果未知` : ""}。请查看完整行动。
              </p>
            ) : null}
          </div>
        ) : null}
      </section>

      <div className="home-sections">
        <aside ref={attentionRef} className="home-attention" aria-label="需要关注">
          <Section
            id="home-attention"
            title="需要你关注"
            more={attention.length > 0 ? () => go({ page: "attention", id: null }) : undefined}
            aside={<span className="section-count">{snapshot ? `${attention.length} 项` : "等待状态"}</span>}
          >
            {snapshot === null ? (
              <EmptyNote icon="inbox">取得本机状态后，这里会显示需要关注的事。</EmptyNote>
            ) : attention.length === 0 ? (
              <EmptyNote>没有需要处理的事。</EmptyNote>
            ) : (
              <div className="attention-summary">
                {attention.slice(0, 3).map((item) => (
                  <div key={item.id} className="attention-summary-item">
                    <StateMark state={item.state} label={item.title} />
                    <p className="item-subtitle">{item.impact}</p>
                    <Button variant="ghost" onClick={() => go(item.route)}>
                      {item.next}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </Section>
        </aside>
        <div className="home-recent">
          <Section
            id="home-recent"
            title="最近发生"
            more={() => go({ page: "timeline", id: null })}
            aside={<span className="section-count">最近 {recent.length} 条</span>}
          >
            {recent.length === 0 ? (
              <EmptyNote icon="timeline">还没有行动记录。她开始做事后会出现在这里。</EmptyNote>
            ) : (
              <div className="item-list">
                {recent.map((item) => (
                  <ItemRow
                    key={item.id}
                    icon={item.kind === "action" ? "timeline" : "settings"}
                    title={item.title}
                    subtitle={`${relative(item.at, now)} · ${item.kind === "action" ? `${item.action.calls.length} 项操作` : "我的操作"}`}
                    trailing={item.status ? <StateMark {...item.status} /> : undefined}
                    onClick={() => go({ page: "timeline", id: item.id })}
                  />
                ))}
              </div>
            )}
          </Section>
        </div>
        <div className="home-activities">
          <Section
            id="home-activities"
            title="持续目标"
            more={() => go({ page: "activities", id: null })}
            aside={<span className="section-count">{running.length} 项</span>}
          >
            {running.length === 0 ? (
              <EmptyNote icon="activity">没有持续推进的目标。她开始一项活动时会出现在这里。</EmptyNote>
            ) : (
              <div className="item-list">
                {running.slice(0, 3).map((activity) => (
                  <ItemRow
                    key={activity.activity_id}
                    icon="activity"
                    title={activity.goal}
                    subtitle={`还剩 ${duration(activity.remaining_seconds)}`}
                    trailing={<StateMark {...mark(activity.state)} />}
                    onClick={() => go({ page: "activities", id: activity.activity_id })}
                  />
                ))}
              </div>
            )}
          </Section>
        </div>
        <div className="home-devices">
          <Section
            id="home-devices"
            title="连接、声线与身体"
            more={() => go({ page: "devices", id: null })}
            aside={snapshot ? <span className="section-count">{snapshot.devices.length} 个设备</span> : undefined}
          >
            {!snapshot ? (
              <EmptyNote icon="device">连接本机服务后，这里会显示设备状态。</EmptyNote>
            ) : snapshot.devices.length === 0 ? (
              <div className="attention-summary">
                <EmptyNote icon="device">尚未启用外部设备，可直接进入调音室或前往设置启用插件。</EmptyNote>
                <div className="actions">
                  <Button variant="secondary" onClick={() => go({ page: "devices", id: "studio" })}>
                    打开声线调音室
                  </Button>
                </div>
              </div>
            ) : (
              <div className="device-summary">
                {snapshot.devices.slice(0, 3).map((device) => (
                  <button
                    key={device.instance_id}
                    className="device-summary-row pc-focusable"
                    onClick={() => go({ page: "devices", id: device.instance_id })}
                  >
                    <span>{deviceName(device)}</span>
                    <StateMark {...deviceMark(device)} />
                    <Icon name="chevron" />
                  </button>
                ))}
              </div>
            )}
          </Section>
        </div>
      </div>
    </>
  );
}

function stateWord(state: AgentView["state"]): string {
  switch (state) {
    case "idle":
      return "空闲";
    case "perceiving":
    case "reasoning":
    case "acting":
      return "运行中";
    case "waiting-external":
      return "等待中";
    case "waiting-user":
      return "等你确认";
    case "warning":
      return "需要注意";
    case "paused":
      return "已暂停";
    case "completed":
      return "已完成";
    case "failed":
      return "失败";
  }
}
