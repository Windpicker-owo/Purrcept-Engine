/**
 * 首页（开放式学园桌案 × 前景探头状态横幅 × 实体活页手账构图）：
 * - 破除 2x2 同构大白盒堆叠：主舞台、时间刻度流与在场连接条直接生长在晴空画布上；
 * - 吉祥物位于舞台后景，下半部自然收拢在不透明前景横幅（.home-path）背后，呈现趴在窗台/横幅沿上“探出头”张望的层次感；
 * - 横幅背景随主体状态切换：安静待命/完成/小憩时呈现静态清透底色，执行/思考/倾听/待拍板时呈现差异化动态色彩流光与呼吸潮汐；
 * - 所有角色称呼均从服务端动态配置传入，未配置时优雅回退为中性文案。
 */
import { useEffect, useRef, useState, type ReactElement } from "react";
import { AnimatePresence, motion, MotionConfig } from "motion/react";
import { ActionPath, Button, Icon, Mascot, STATES, transitions } from "@purrcept/cel";
import type { ActionPathNode } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import type { Action, Snapshot } from "../contracts";
import type { NotebookFileSummary } from "../model/admin";
import { runningAction } from "../model/agent";
import type { AgentView } from "../model/agent";
import type { AttentionItem } from "../model/attention";
import { summarizePath } from "../model/home-path";
import { capabilityName, deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { actionTitle, timelineItems } from "../model/timeline";
import { deviceMark, mark } from "../semantics";
import { duration, EmptyNote, relative, StateMark } from "../ui/common";

type DeskTab = "journal" | "memory" | "skills";

const DESK_TABS: readonly { key: DeskTab; ear: string; kicker: string; emptyHint: string }[] = [
  {
    key: "journal",
    ear: "随笔",
    kicker: "DIARY // 亲笔随笔",
    emptyHint: "还没有写下日常随笔。和她聊完天后，随手记下的生活片段会夹在这里。",
  },
  {
    key: "memory",
    ear: "记忆",
    kicker: "MEMORY // 长期记忆",
    emptyHint: "还没有沉淀专题记忆。你偏好的习惯、重要约定与长期上下文会收纳在这一栏。",
  },
  {
    key: "skills",
    ear: "技能",
    kicker: "SKILLS // 挂载便签",
    emptyHint: "还没有挂载自定义技能卡。可在手账页新建带 YAML 描述头的操作便签。",
  },
];

function stripMarkdownPreview(raw: string): string {
  let body = raw.replace(/\r\n/g, "\n");
  if (body.startsWith("---\n")) {
    const end = body.indexOf("\n---\n", 4);
    if (end !== -1) body = body.slice(end + 5);
  }
  const lines = body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.length > 0 && !line.startsWith("#") && !line.startsWith("```"))
    .map((line) => line.replace(/^>\s?/, "").replace(/^[-*+]\s+/, ""));
  const joined = lines.join(" ").trim();
  if (!joined) return "这页手账已创建，点击下方入口即可展开完整纸面排版或继续添笔。";
  return joined.length > 120 ? `${joined.slice(0, 120)}…` : joined;
}

function pathNodes(action: Action, snapshot: Snapshot, selfLabel: string): ActionPathNode[] {
  return [
    { id: "self", label: selfLabel, icon: <Icon name="person" />, state: "completed" },
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
  client,
  state,
  agent,
  attention,
  botName,
  go,
}: {
  client: PanelClient;
  state: PanelState;
  agent: AgentView;
  attention: readonly AttentionItem[];
  botName: string;
  go: (route: Route) => void;
}): ReactElement {
  const pathRef = useRef<HTMLDivElement>(null);
  const ticketRef = useRef<HTMLDivElement>(null);
  const [deskTab, setDeskTab] = useState<DeskTab>("journal");
  const [noteIndex, setNoteIndex] = useState(0);
  const [notes, setNotes] = useState<readonly NotebookFileSummary[]>([]);
  const [noteInstance, setNoteInstance] = useState<string>("");
  const [excerpt, setExcerpt] = useState<string>("");

  const snapshot = state.snapshot;
  const selfName = botName.trim() || "她";
  const primaryAttention = attention.find((item) => item.needsUser) ?? attention[0];
  const relatedAction =
    primaryAttention?.route.page === "timeline"
      ? snapshot?.recent_actions.find((action) => action.action_id === primaryAttention.route.id)
      : undefined;
  const latest =
    agent.focus === "attention" ? relatedAction : snapshot ? runningAction(snapshot) ?? snapshot.recent_actions[0] : undefined;
  const pathSummary = latest ? summarizePath(latest) : null;
  const gaze = agent.focus === "path" ? pathRef : agent.focus === "attention" ? ticketRef : undefined;
  const running = snapshot?.activities.filter((activity) => !["ended", "completed"].includes(activity.state)) ?? [];
  const recent = timelineItems(snapshot, state.entries, "all").slice(0, 5);
  const now = Date.now();
  const dynamicBanner = isDynamicBannerState(agent.state);

  // 静默读取真实手账列表供桌角活页本预览；合成测试环境无此接口时安全跳过。
  useEffect(() => {
    let active = true;
    const loadDeskNotes = async (): Promise<void> => {
      try {
        const res = await client.listNotebooks();
        if (!active || !res.available || res.instances.length === 0) return;
        const inst = res.instances[0]!;
        setNoteInstance(inst.instance_id);
        setNotes(inst.files);
      } catch {
        // 未启用或离线时不打扰主舞台
      }
    };
    void loadDeskNotes();
    return () => {
      active = false;
    };
  }, [client, snapshot?.instance_id]);

  const tabNotes = notes.filter((n) => n.category === deskTab);
  const currentNote = tabNotes.length > 0 ? tabNotes[noteIndex % tabNotes.length] : undefined;

  useEffect(() => {
    if (!currentNote || !noteInstance) {
      setExcerpt("");
      return;
    }
    let active = true;
    void client
      .readNote(noteInstance, currentNote.path)
      .then((doc) => {
        if (active) setExcerpt(stripMarkdownPreview(doc.content));
      })
      .catch(() => {
        if (active) setExcerpt(currentNote.description || "");
      });
    return () => {
      active = false;
    };
  }, [client, noteInstance, currentNote?.path]);

  const activeTabMeta = DESK_TABS.find((item) => item.key === deskTab) ?? DESK_TABS[0]!;

  return (
    <>
      <section className="home-stage" aria-label="主体状态" data-state={agent.state}>
        <div className="home-stage-backdrop" aria-hidden="true">
          <span className="home-watermark">01 // COMPANION DESK</span>
          <div className="home-stage-dots" />
        </div>

        <div className="home-overview">
          <div className="home-copy">
            <div className="home-kicker">
              <span className="pc-eyebrow">STAGE // 01</span>
              {botName.trim() ? <span className="companion-pill">{botName.trim()} · 在场</span> : null}
              <StateMark state={agent.state} label={stateWord(agent.state)} />
              {snapshot !== null ? (
                attention.length === 0 ? (
                  <span className="peace-charm" role="status">
                    0 项待办 · 顺畅待命
                  </span>
                ) : (
                  <button
                    type="button"
                    className="peace-charm is-alert pc-focusable"
                    onClick={() => go(attention.length === 1 && primaryAttention ? primaryAttention.route : { page: "attention", id: null })}
                  >
                    {attention.length} 项待你拿主意 →
                  </button>
                )
              ) : null}
            </div>

            <h1 aria-live="polite">{agent.headline}</h1>
            <p className="home-detail">{agent.detail}</p>

            <AnimatePresence initial={false}>
              {attention.length > 0 && primaryAttention ? (
                <motion.div
                  ref={ticketRef}
                  key={primaryAttention.id}
                  className="attention-ticket"
                  aria-label="需要关注"
                  initial={{ opacity: 0, y: -6, scale: 0.98 }}
                  animate={{ opacity: 1, y: 0, scale: 1 }}
                  exit={{ opacity: 0, y: -4, scale: 0.98 }}
                  transition={transitions.snap}
                >
                  <div className="ticket-body">
                    <StateMark state={primaryAttention.state} label={primaryAttention.title} />
                    <p className="item-subtitle">{primaryAttention.impact}</p>
                  </div>
                  <div className="ticket-actions">
                    <Button variant="secondary" onClick={() => go(primaryAttention.route)}>
                      {primaryAttention.next}
                    </Button>
                    {attention.length > 1 ? (
                      <Button variant="ghost" onClick={() => go({ page: "attention", id: null })}>
                        全部 {attention.length} 项
                      </Button>
                    ) : null}
                  </div>
                </motion.div>
              ) : null}
            </AnimatePresence>

            <div className="actions">
              <Button onClick={() => go({ page: "chat", id: null })}>
                {botName.trim() ? `找${botName.trim()}搭话` : "发起陪伴对话"}
              </Button>
              <Button variant="secondary" onClick={() => go({ page: "memory", id: null })}>
                翻开记忆手账
              </Button>
              <Button variant="secondary" onClick={() => go({ page: "system", id: null })}>
                人设与引擎配置
              </Button>
            </div>
          </div>

          <div className="home-mascot-col" data-state={agent.state}>
            <span className="mascot-mood-tag" aria-hidden="true">
              {moodCaption(agent.state)}
            </span>
            <div className="home-mascot" data-state={agent.state} aria-hidden="true">
              <Mascot state={agent.state} gazeTarget={gaze} />
            </div>
          </div>
        </div>

        <div
          ref={pathRef}
          className="home-path"
          data-state={agent.state}
          data-family={STATES[agent.state].family}
          data-dynamic={dynamicBanner ? "true" : "false"}
        >
          <div className="home-banner-fx" aria-hidden="true">
            <span className="banner-wash" />
            <span className="banner-flow-a" />
            <span className="banner-flow-b" />
            <span className="banner-dots" />
            <span className="banner-top-gleam" />
          </div>

          {snapshot !== null && latest !== undefined ? (
            <>
              <div className="home-path-heading">
                <div className="home-path-badges">
                  <span className="pc-eyebrow">
                    {agent.focus === "attention"
                      ? "CHECK // 需要核对的行动"
                      : latest.state === "running"
                        ? "LIVE // 当前行动"
                        : "RECENT // 最近一次行动"}
                  </span>
                  <span className="banner-rhythm-tag">{bannerRhythmLabel(agent.state)}</span>
                </div>
                <Button variant="ghost" onClick={() => go({ page: "timeline", id: latest.action_id })}>
                  查看这次行动
                </Button>
              </div>
              <div className="home-path-track">
                <MotionConfig reducedMotion="always">
                  <ActionPath label={actionTitle(latest, snapshot)} nodes={pathNodes(latest, snapshot, selfName)} />
                </MotionConfig>
              </div>
              {pathSummary && pathSummary.omitted > 0 ? (
                <p className="path-summary-note">
                  另有 {pathSummary.omitted} 项操作未展示
                  {pathSummary.hiddenAttention > 0 ? `，其中 ${pathSummary.hiddenAttention} 项失败或结果未知` : ""}。请查看完整行动。
                </p>
              ) : null}
            </>
          ) : (
            <>
              <div className="home-path-heading">
                <div className="home-path-badges">
                  <span className="pc-eyebrow">{bannerRhythmLabel(agent.state)}</span>
                </div>
                <span className="banner-standby-title">{stateWord(agent.state)}</span>
              </div>
              <div className="home-path-standby">
                <StateMark state={agent.state} label={agent.headline} />
                <span className="item-subtitle">
                  {snapshot === null
                    ? "连上本机服务后，这里会实时铺开她的行动节点与回执轨迹。"
                    : `当前尚无近期行动节点；有新消息或任务触发时，${selfName}的行动轨迹会在这里展开。`}
                </span>
              </div>
            </>
          )}
        </div>
      </section>

      <div className="desk-split">
        {/* 左翼：直接印在晴空画布上的开放时间轴流 + 紧凑设备状态灯排 */}
        <div className="editorial-column">
          <section className="open-section" aria-labelledby="home-recent-heading">
            <header className="editorial-header">
              <div className="editorial-title-group">
                <span className="big-num" aria-hidden="true">
                  02
                </span>
                <div>
                  <span className="eyebrow">TIMELINE // 刚刚发生的足迹</span>
                  <h2 id="home-recent-heading">最近动态</h2>
                </div>
              </div>
              <Button variant="ghost" onClick={() => go({ page: "timeline", id: null })}>
                完整时间线
              </Button>
            </header>

            {recent.length === 0 ? (
              <EmptyNote icon="timeline">
                还没有新的行动记录，和{selfName}聊两句或触发一次任务就会在这里留下刻度。
              </EmptyNote>
            ) : (
              <div className="spine-timeline">
                {recent.map((item) => {
                  const isRunning = item.status?.state === "acting" || item.status?.state === "reasoning";
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className="spine-item pc-focusable"
                      data-running={isRunning ? "true" : undefined}
                      onClick={() => go({ page: "timeline", id: item.id })}
                    >
                      <span className="spine-dot" aria-hidden="true" />
                      <span className="spine-main">
                        <span className="spine-row-top">
                          <span className="spine-time">{relative(item.at, now)}</span>
                          <span className="spine-name">{item.title}</span>
                        </span>
                        <span className="item-subtitle">
                          {item.kind === "action" ? `${item.action.calls.length} 项操作回执` : "本页发起的控制操作"}
                        </span>
                      </span>
                      {item.status ? (
                        <span className="spine-trailing">
                          <StateMark {...item.status} />
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            )}
          </section>

          {running.length > 0 ? (
            <section className="inline-strip" aria-labelledby="home-goals-heading">
              <div className="strip-head">
                <span className="eyebrow" id="home-goals-heading">
                  GOALS // 正在持续推进的目标（{running.length}）
                </span>
                <Button variant="ghost" onClick={() => go({ page: "activities", id: null })}>
                  查看活动
                </Button>
              </div>
              <div className="pill-cluster">
                {running.slice(0, 3).map((activity) => (
                  <button
                    key={activity.activity_id}
                    type="button"
                    className="status-pill pc-focusable"
                    onClick={() => go({ page: "activities", id: activity.activity_id })}
                  >
                    <StateMark {...mark(activity.state)} />
                    <span className="status-pill-name">{activity.goal}</span>
                    <span className="item-subtitle">余 {duration(activity.remaining_seconds)}</span>
                  </button>
                ))}
              </div>
            </section>
          ) : null}

          <section className="inline-strip" aria-labelledby="home-links-heading">
            <div className="strip-head">
              <span className="eyebrow" id="home-links-heading">
                VOICE &amp; LINK // 在场感官与连接插槽
              </span>
              <Button variant="ghost" onClick={() => go({ page: "devices", id: null })}>
                {snapshot ? `全部 ${snapshot.devices.length} 个插槽` : "设备与声线"}
              </Button>
            </div>
            {!snapshot ? (
              <p className="item-subtitle">连接本机服务后，这里会亮起在线通道与声线状态灯牌。</p>
            ) : snapshot.devices.length === 0 ? (
              <div className="pill-cluster">
                <Button variant="secondary" onClick={() => go({ page: "devices", id: "studio" })}>
                  打开声线调音室
                </Button>
                <Button variant="ghost" onClick={() => go({ page: "system", id: null })}>
                  前往设置挂载插件
                </Button>
              </div>
            ) : (
              <div className="pill-cluster">
                {snapshot.devices.slice(0, 6).map((device) => (
                  <button
                    key={device.instance_id}
                    type="button"
                    className="status-pill pc-focusable"
                    onClick={() => go({ page: "devices", id: device.instance_id })}
                  >
                    <StateMark state={deviceMark(device).state} label={deviceName(device)} />
                    <span className="item-subtitle">{deviceMark(device).label}</span>
                  </button>
                ))}
                {snapshot.devices.length > 6 ? (
                  <button
                    type="button"
                    className="status-pill pc-focusable"
                    onClick={() => go({ page: "devices", id: null })}
                  >
                    <span className="status-pill-name">+{snapshot.devices.length - 6} 项器官</span>
                  </button>
                ) : null}
              </div>
            )}
          </section>
        </div>

        {/* 右翼：全屏唯一的厚重实体——装订环 + 侧边彩色索引耳活页手账册 */}
        <div className="notebook-artifact-wrap">
          <article className="notebook-artifact" aria-labelledby="desk-notebook-title">
            <div className="notebook-washi-strip" aria-hidden="true" />
            <div className="binder-rings" aria-hidden="true">
              <span className="ring-loop" />
              <span className="ring-loop" />
              <span className="ring-loop" />
              <span className="ring-loop" />
              <span className="ring-loop" />
            </div>

            <div className="side-index-tabs" role="tablist" aria-label="手账分类切换">
              {DESK_TABS.map((tab) => {
                const active = deskTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    type="button"
                    role="tab"
                    aria-selected={active}
                    className="index-ear pc-focusable"
                    data-tab={tab.key}
                    data-active={active ? "true" : undefined}
                    onClick={() => {
                      setDeskTab(tab.key);
                      setNoteIndex(0);
                    }}
                  >
                    {tab.ear}
                  </button>
                );
              })}
            </div>

            <header className="nb-cover-head">
              <div>
                <span className="nb-kicker">{activeTabMeta.kicker}</span>
                <h2 id="desk-notebook-title">
                  {botName.trim() ? `${botName.trim()}的手账桌角` : "记忆手账桌角"}
                </h2>
              </div>
              <span className="notebook-count">
                {notes.length > 0 ? `共 ${notes.length} 篇` : "方格手账"}
              </span>
            </header>

            <AnimatePresence mode="wait" initial={false}>
              <motion.div
                key={`${deskTab}-${currentNote?.path ?? "empty"}`}
                className="nb-cover-body"
                initial={{ opacity: 0, y: 5 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -4 }}
                transition={transitions.snap}
              >
                {currentNote ? (
                  <>
                    <div className="nb-note-meta">
                      <span className="notebook-entry-path">{currentNote.path}</span>
                      <span className="notebook-entry-time">{relative(currentNote.updated_at, now)}</span>
                    </div>
                    <h3 className="nb-note-title">{currentNote.title}</h3>
                    <p className="nb-note-excerpt">
                      {excerpt || currentNote.description || "正在展开这页手账摘录…"}
                    </p>
                  </>
                ) : (
                  <p className="nb-note-excerpt">{activeTabMeta.emptyHint}</p>
                )}
              </motion.div>
            </AnimatePresence>

            <footer className="nb-cover-foot">
              {tabNotes.length > 1 ? (
                <Button variant="ghost" onClick={() => setNoteIndex((n) => n + 1)}>
                  换一篇摘录 ↻
                </Button>
              ) : (
                <span className="notebook-meta-stamp">
                  {running.length > 0 ? `${running.length} 项心愿进行中` : "点击索引耳切换分类"}
                </span>
              )}
              <Button
                variant="secondary"
                onClick={() => go({ page: "memory", id: currentNote?.path ?? null })}
              >
                {currentNote ? "展开这页手账" : "打开手账本"}
              </Button>
            </footer>
          </article>
        </div>
      </div>
    </>
  );
}

function isDynamicBannerState(state: AgentView["state"]): boolean {
  return (
    state === "acting" ||
    state === "reasoning" ||
    state === "perceiving" ||
    state === "waiting-user" ||
    state === "waiting-external" ||
    state === "warning"
  );
}

function bannerRhythmLabel(state: AgentView["state"]): string {
  switch (state) {
    case "idle":
      return "STATIC // 静候待命";
    case "completed":
      return "SETTLED // 行动收束";
    case "paused":
      return "REST // 小憩静置";
    case "acting":
      return "STREAM // 疾风执行流";
    case "reasoning":
      return "THOUGHT // 思绪与记忆潮汐";
    case "perceiving":
      return "SIGNAL // 声波感知涟漪";
    case "waiting-user":
      return "AWAIT // 暖光待你拍板";
    case "waiting-external":
      return "SYNC // 晴空守候回音";
    case "warning":
      return "NOTICE // 状态波动提示";
    case "failed":
      return "ALERT // 异常阻断稳态";
  }
}

function moodCaption(state: AgentView["state"]): string {
  switch (state) {
    case "idle":
      return "悠闲待命中 ♪";
    case "perceiving":
      return "耳朵竖起倾听中…";
    case "reasoning":
      return "认真组织思路中…";
    case "acting":
      return "专注执行动作中！";
    case "waiting-user":
      return "等你拿个主意～";
    case "waiting-external":
      return "安静等待回音…";
    case "warning":
      return "发现一处小状况";
    case "paused":
      return "暂时小憩片刻";
    case "completed":
      return "刚刚顺利办妥！";
    case "failed":
      return "遇到一点阻碍";
  }
}

function stateWord(state: AgentView["state"]): string {
  switch (state) {
    case "idle":
      return "悠闲待命";
    case "perceiving":
    case "reasoning":
    case "acting":
      return "专注行动中";
    case "waiting-external":
      return "等待回音";
    case "waiting-user":
      return "等你拍板";
    case "warning":
      return "需要留意";
    case "paused":
      return "暂时小憩";
    case "completed":
      return "刚刚完成";
    case "failed":
      return "遇到阻碍";
  }
}
