/**
 * 产品外壳：导航轨 + 开放主舞台 + Inspector（窄屏为 Sheet），hash 路由决定页面与选择。
 * 自动从服务端配置或对话通道读取当前 Bot 角色名（未配置时优雅回退），不硬编码特定角色名。
 */
import { useEffect, useState, useSyncExternalStore, type ReactElement } from "react";
import { breakpoints, Button, CelProvider, Feedback, Sheet } from "@purrcept/cel";
import type { PanelClient, PanelState } from "./client";
import { isSnapshotDelayed } from "./client";
import { agentView } from "./model/agent";
import type { AgentView } from "./model/agent";
import { attentionItems } from "./model/attention";
import type { AttentionItem } from "./model/attention";
import { createReviewWorkspace, type ReviewWorkspace } from "./model/review-workspace";
import { PAGES, parseRoute, routeHref, type Page, type Route } from "./model/routes";
import { controlId } from "./model/timeline";
import { Activities } from "./pages/Activities";
import { Attention } from "./pages/Attention";
import { Chat } from "./pages/Chat";
import { DeviceDetail, Devices } from "./pages/Devices";
import { Home } from "./pages/Home";
import { Memory } from "./pages/Memory";
import { Reviews } from "./pages/Reviews";
import { System } from "./pages/System";
import { Timeline } from "./pages/Timeline";
import { activityReason, mark } from "./semantics";
import { duration, StateMark, TechDetails, Unavailable } from "./ui/common";
import { ActionDetail, ControlDetail } from "./ui/details";
import { Navigation, useMediaQuery } from "./ui/Navigation";

function useRoute(): [Route, (route: Route) => void] {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const changed = (): void => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  return [
    route,
    (next) => {
      window.location.hash = routeHref(next);
    },
  ];
}

function Selection({
  route,
  state,
  client,
  go,
}: {
  route: Route;
  state: PanelState;
  client: PanelClient;
  go: (route: Route) => void;
}): ReactElement | null {
  const s = state.snapshot;
  const id = route.id;
  if (id === null) return null;
  if (route.page === "timeline") {
    const entry = state.entries.find((item) => controlId(item) === id);
    if (entry) return <ControlDetail entry={entry} client={client} state={state} open={(actionId) => go({ page: "timeline", id: actionId })} />;
    const action =
      s?.recent_actions.find((item) => item.action_id === id) ??
      state.entries.find((item) => item.result?.action?.action_id === id)?.result?.action ??
      undefined;
    return action ? (
      <>
        <Button variant="secondary" onClick={() => go({ page: "reviews", id })}>
          审查刺激与行动
        </Button>
        <ActionDetail action={action} snapshot={s} />
      </>
    ) : (
      <Unavailable>这条记录已经不在最近的列表中。</Unavailable>
    );
  }
  if (route.page === "devices") {
    if (id === "studio" || id === "live2d") return null;
    const device = s?.devices.find((item) => item.instance_id === id);
    if (!device) return <Unavailable>这个设备当前没有启用。</Unavailable>;
    return <DeviceDetail client={client} state={state} device={device} />;
  }
  if (route.page === "activities") {
    const activity = s?.activities.find((item) => item.activity_id === id);
    if (!activity) return <Unavailable>这项活动已经不在列表中。</Unavailable>;
    const resume = activityReason(activity, "resume");
    return (
      <div className="detail">
        <h2>{activity.goal}</h2>
        <StateMark {...mark(activity.state)} />
        <p>还剩 {duration(activity.remaining_seconds)}</p>
        {resume !== undefined && activity.state === "paused" ? <p className="muted">{resume}</p> : null}
        <TechDetails
          items={[
            ["活动", activity.activity_id],
            ["定义", activity.definition],
            ["状态值", activity.state],
            ["版本", activity.version],
            ["待核对", activity.needs_reconciliation],
            ["最近命令", activity.last_command?.command],
            ["命令状态", activity.last_command?.status],
          ]}
        />
      </div>
    );
  }
  return null;
}

function PageBody({
  page,
  state,
  client,
  agent,
  attention,
  route,
  go,
  reviewWorkspace,
  botName,
  onBotNameChange,
}: {
  page: Page;
  state: PanelState;
  client: PanelClient;
  agent: AgentView;
  attention: readonly AttentionItem[];
  route: Route;
  go: (route: Route) => void;
  reviewWorkspace: ReviewWorkspace;
  botName: string;
  onBotNameChange: (name: string) => void;
}): ReactElement {
  switch (page) {
    case "home":
      return <Home client={client} state={state} agent={agent} attention={attention} botName={botName} go={go} />;
    case "chat":
      return <Chat client={client} state={state} botName={botName} onBotNameChange={onBotNameChange} go={go} />;
    case "memory":
      return <Memory client={client} selected={route.id} botName={botName} go={go} />;
    case "attention":
      return <Attention items={attention} go={go} />;
    case "timeline":
      return <Timeline state={state} selected={route.id} go={go} />;
    case "reviews":
      return <Reviews client={client} state={state} selected={route.id} go={go} workspace={reviewWorkspace} />;
    case "devices":
      return <Devices client={client} state={state} selected={route.id} go={go} />;
    case "activities":
      return <Activities client={client} state={state} selected={route.id} go={go} />;
    case "system":
      return <System state={state} client={client} onBotNameChange={onBotNameChange} go={go} />;
  }
}

const OFFLINE_CAPABLE_PAGES: readonly Page[] = ["home", "system", "memory", "devices"];

export function App({ client }: { client: PanelClient }): ReactElement {
  const state = useSyncExternalStore(client.subscribe, client.getState);
  const [route, go] = useRoute();
  const narrow = useMediaQuery(breakpoints.inspector);
  const [reviewWorkspace] = useState(createReviewWorkspace);
  const [botName, setBotName] = useState("");

  useEffect(() => {
    void client.refresh();
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void client.refresh();
    }, 2000);
    const visible = (): void => {
      if (document.visibilityState === "visible") void client.refresh();
    };
    document.addEventListener("visibilitychange", visible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [client]);

  // 从服务端配置或对话接口动态解析当前 Bot 名称；未提供该接口的合成测试宿主静默回退。
  useEffect(() => {
    let active = true;
    const syncPersonaName = async (): Promise<void> => {
      try {
        const cfg = await client.loadConfig();
        if (!active) return;
        if (cfg.available && cfg.subject.character.name.trim()) {
          setBotName(cfg.subject.character.name.trim());
          return;
        }
      } catch {
        // 离线或最小测试替身未挂载 /api/config 时继续尝试 /api/chat 或保持中性回退
      }
      if (!state.online) return;
      try {
        const feed = await client.chatFeed(1);
        if (!active) return;
        const candidate = feed.character_name.trim();
        if (candidate && candidate !== "主体") {
          setBotName(candidate);
        }
      } catch {
        // 保持空字符串，由各页面优雅回退至“她”
      }
    };
    void syncPersonaName();
    return () => {
      active = false;
    };
  }, [client, state.online, state.snapshot?.instance_id]);

  const attention = attentionItems(state.snapshot, state.entries);
  const connecting = state.snapshot === null && state.error === null;
  const currentAgent = agentView(state.snapshot, state.online || connecting, attention);
  const snapshotDelayed = isSnapshotDelayed(state);
  let agent: AgentView = currentAgent;
  if (snapshotDelayed) {
    agent = {
      state: state.observedHealth?.can_process ? "warning" : "failed",
      headline: state.observedHealth?.can_process ? "状态更新延迟" : "主体已停止处理",
      detail: "本机服务仍可响应，等待最新状态。",
      focus: null,
    };
  } else if (state.snapshot === null && state.startup) {
    const failed = state.startup.phase === "failed";
    const stages: Record<string, string> = {
      sqlite: "正在打开本机数据。",
      models: "正在准备模型请求服务。",
      execution: "正在准备行动服务。",
      subject_restore: "正在恢复主体状态。",
      subject_start: "正在启用主体工作。",
    };
    const stage = state.startup.startup_stage ?? "";
    const detail =
      stages[stage] ??
      (stage.startsWith("plugin_") ? "正在准备输入与设备插件。" : "配置与设备准备完成后即可接收新的工作。");
    agent = {
      state: failed ? "failed" : "waiting-external",
      headline: failed ? "启动失败" : "Engine 正在启动",
      detail: failed ? "请查看终端日志或前往「设置」检查配置。" : detail,
      focus: null,
    };
  }

  useEffect(() => {
    document.title = botName ? `${agent.headline} · ${botName} · Purrcept` : `${agent.headline} · Purrcept`;
  }, [agent.headline, botName]);

  const current = PAGES.find((item) => item.id === route.page);
  const age = state.receivedAt === null ? null : Math.floor((Date.now() - state.receivedAt) / 1000);
  const freshness = age !== null ? `更新于 ${age} 秒前` : connecting ? "正在取得状态" : "尚未取得状态";
  const isDeviceStudioSubroute = route.page === "devices" && (route.id === "studio" || route.id === "live2d");
  const inspectable = ["timeline", "devices", "activities"].includes(route.page) && !isDeviceStudioSubroute;
  const hasSelection = route.id !== null && inspectable;
  const close = (): void => go({ page: route.page, id: null });

  return (
    <CelProvider>
      <div className="panel-shell" data-inspector={!narrow && hasSelection}>
        <button className="skip-link pc-focusable" onClick={() => document.getElementById("main")?.focus()}>
          跳到主要内容
        </button>
        <Navigation page={route.page} attentionCount={attention.length} />

        <div className="main-column">
          <header className="topbar">
            <div className="location">
              <span className="product-name">Purrcept</span>
              {botName ? (
                <>
                  <span className="location-divider" aria-hidden="true">
                    ·
                  </span>
                  <span className="location-persona">{botName}</span>
                </>
              ) : null}
              <span className="location-divider" aria-hidden="true">
                /
              </span>
              <span>{current?.label}</span>
            </div>
            <div className="topbar-status">
              {route.page !== "home" ? <StateMark state={agent.state} label={agent.headline} /> : null}
              <span className="topbar-age">
                {freshness}
                {!state.online && state.snapshot !== null ? " · 已过期" : ""}
              </span>
              {!state.online && !connecting ? (
                <Button variant="ghost" onClick={() => void client.refresh()}>
                  立即重试
                </Button>
              ) : null}
            </div>
          </header>
          {!state.online && !connecting ? (
            <Feedback
              state="warning"
              scope="system"
              urgency="passive"
              persistence="until-resolved"
              title={snapshotDelayed ? "本机服务可连接，状态更新延迟" : state.snapshot ? "与本机服务的连接中断" : "正在等待本机服务"}
            >
              {snapshotDelayed
                ? "正在重试读取状态；取得最新状态前控制操作暂停。"
                : state.snapshot
                  ? "页面显示的是断开前的状态，控制操作已暂停。"
                  : "正在初始化或等待 Engine 响应；你仍可在「设置」中检查与保存配置文件。"}
              {state.error ? ` ${state.error}` : ""}
            </Feedback>
          ) : null}

          <main id="main" className="main-stage" tabIndex={-1}>
            <div
              key={route.page}
              className="page pc-enter"
              data-measure={route.page === "attention" ? "reading" : "wide"}
            >
              {state.snapshot === null && !OFFLINE_CAPABLE_PAGES.includes(route.page) ? (
                <section>
                  <h1>{current?.label}</h1>
                  <p className="page-intro">正在等待本机服务完成启动。你也可以先前往「设置」检查配置或「记忆」浏览手账。</p>
                </section>
              ) : (
                <PageBody
                  page={route.page}
                  state={state}
                  client={client}
                  agent={agent}
                  attention={attention}
                  route={route}
                  go={go}
                  reviewWorkspace={reviewWorkspace}
                  botName={botName}
                  onBotNameChange={setBotName}
                />
              )}
            </div>
            {state.storageError ? (
              <Feedback state="warning" scope="region" urgency="passive" persistence="retained" title="操作记录没有保存到浏览器">
                {state.storageError}
              </Feedback>
            ) : null}
            {state.controlError ? (
              <Feedback state="warning" scope="region" urgency="passive" persistence="until-resolved" title="操作等待名额已满">
                {state.controlError}
              </Feedback>
            ) : null}
          </main>
        </div>

        {!narrow ? (
          hasSelection ? (
            <aside className="inspector" aria-label="详情">
              <div className="inspector-heading">
                <span className="eyebrow">详情</span>
                <Button variant="ghost" onClick={close}>
                  关闭
                </Button>
              </div>
              <div key={route.id} className="pc-enter">
                <Selection route={route} state={state} client={client} go={go} />
              </div>
            </aside>
          ) : null
        ) : (
          <Sheet open={hasSelection} onOpenChange={(open) => { if (!open) close(); }} title={current?.label ?? "详情"} description="详情">
            <Selection route={route} state={state} client={client} go={go} />
          </Sheet>
        )}
      </div>
    </CelProvider>
  );
}
