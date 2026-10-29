/**
 * 产品外壳：导航轨 + 主舞台 + Inspector（窄屏为 Sheet），hash 路由决定页面与选择。
 * 单页挂载只建立轮询；所有页面共用同一 PanelClient 与状态所有者。
 * 主体状态、待处理事项只在这里推导一次，再分发给各页面、顶栏和标签页标题。
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import type { ReactElement } from "react";
import { motion } from "motion/react";
import { Button, CelProvider, Feedback, Icon, Sheet, transitions } from "@purrcept/cel";
import type { PanelClient, PanelState } from "./client";
import { agentView } from "./model/agent";
import type { AgentView } from "./model/agent";
import { attentionItems } from "./model/attention";
import type { AttentionItem } from "./model/attention";
import { deviceSummary, deviceTechnical } from "./model/devices";
import { deviceName } from "./model/names";
import { PAGES, parseRoute, routeHref } from "./model/routes";
import type { Page, Route } from "./model/routes";
import { controlId } from "./model/timeline";
import { Activities } from "./pages/Activities";
import { Attention } from "./pages/Attention";
import { Devices } from "./pages/Devices";
import { Home } from "./pages/Home";
import { System } from "./pages/System";
import { Timeline } from "./pages/Timeline";
import { Reviews, ReviewDetail } from "./pages/Reviews";
import { activityReason, deviceMark, mark } from "./semantics";
import { BrandMark, duration, StateMark, TechDetails, Unavailable } from "./ui/common";
import { ActionDetail, ControlDetail } from "./ui/details";

const NARROW = "(max-width: 72rem)";

/** hash 是选择的唯一来源；浏览器后退与页面内导航都经同一监听更新 React。 */
function useRoute(): [Route, (route: Route) => void] {
  const [route, setRoute] = useState(() => parseRoute(window.location.hash));
  useEffect(() => {
    const changed = (): void => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  // 写 hash 产生浏览器历史，后退即返回上一层级。
  return [route, (next) => { window.location.hash = routeHref(next); }];
}

/** Inspector 内容由路由选择决定；对象已不在快照中时如实说明，而不是保留旧内容。 */
function Selection({ route, state, client, go }: { route: Route; state: PanelState; client: PanelClient; go: (route: Route) => void }): ReactElement | null {
  const s = state.snapshot;
  const id = route.id;
  if (id === null) return null;
  if (route.page === "reviews") return <ReviewDetail key={`${state.snapshot?.instance_id}:${id}`} actionId={id} state={state} client={client} />;
  if (route.page === "timeline") {
    const entry = state.entries.find((item) => controlId(item) === id);
    if (entry) return <ControlDetail entry={entry} client={client} state={state} open={(actionId) => go({ page: "timeline", id: actionId })} />;
    const action = s?.recent_actions.find((item) => item.action_id === id) ?? state.entries.find((item) => item.result?.action?.action_id === id)?.result?.action ?? undefined;
    return action ? <><Button variant="secondary" onClick={() => go({ page: "reviews", id })}>审查刺激与行动</Button><ActionDetail action={action} snapshot={s} /></> : <Unavailable>这条记录已经不在最近的列表中。</Unavailable>;
  }
  if (route.page === "devices") {
    const device = s?.devices.find((item) => item.instance_id === id);
    if (!device) return <Unavailable>这个设备当前没有启用。</Unavailable>;
    return <div className="detail">
      <h2>{deviceName(device)}</h2><StateMark {...deviceMark(device)} />
      {deviceSummary(device).map((line) => <p key={line}>{line}</p>)}
      <TechDetails items={deviceTechnical(device)} />
    </div>;
  }
  if (route.page === "activities") {
    const activity = s?.activities.find((item) => item.activity_id === id);
    if (!activity) return <Unavailable>这项活动已经不在列表中。</Unavailable>;
    const resume = activityReason(activity, "resume");
    return <div className="detail">
      <h2>{activity.goal}</h2><StateMark {...mark(activity.state)} />
      <p>还剩 {duration(activity.remaining_seconds)}</p>
      {resume !== undefined && activity.state === "paused" ? <p className="muted">{resume}</p> : null}
      <TechDetails items={[["活动", activity.activity_id], ["定义", activity.definition], ["状态值", activity.state], ["版本", activity.version], ["运行代次", activity.run_generation], ["计时依据", activity.budget_basis], ["末段计时不完整", activity.timing_incomplete], ["待核对", activity.needs_reconciliation], ["最近命令", activity.last_command_id], ["命令", activity.last_command?.command], ["命令状态", activity.last_command?.status], ["实际状态", activity.last_command?.actual_state], ["采用确认", activity.last_command?.applied]]} />
    </div>;
  }
  return null;
}

function PageBody({ page, state, client, agent, attention, route, go }: { page: Page; state: PanelState; client: PanelClient; agent: AgentView; attention: readonly AttentionItem[]; route: Route; go: (route: Route) => void }): ReactElement {
  switch (page) {
    case "home": return <Home state={state} agent={agent} attention={attention} go={go} />;
    case "attention": return <Attention items={attention} go={go} />;
    case "timeline": return <Timeline state={state} selected={route.id} go={go} />;
    case "reviews": return <Reviews client={client} state={state} selected={route.id} go={go} />;
    case "devices": return <Devices client={client} state={state} selected={route.id} go={go} />;
    case "activities": return <Activities client={client} state={state} selected={route.id} go={go} />;
    case "system": return <System state={state} client={client} go={go} />;
  }
}

/**
 * 装配单一面板观察者、路由与各页面。
 *
 * client 是本次页面唯一观察所有者；卸载只停止轮询，不撤销宿主业务。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function App({ client }: { client: PanelClient }): ReactElement {
  const state = useSyncExternalStore(client.subscribe, client.getState);
  const [route, go] = useRoute();
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW).matches);
  useEffect(() => {
    void client.refresh();
    // 标签页不可见时不轮询；回到前台立即刷新一次。
    const interval = window.setInterval(() => { if (document.visibilityState === "visible") void client.refresh(); }, 2000);
    const visible = (): void => { if (document.visibilityState === "visible") void client.refresh(); };
    const media = window.matchMedia(NARROW);
    const changed = (): void => setNarrow(media.matches);
    document.addEventListener("visibilitychange", visible);
    media.addEventListener("change", changed);
    return () => { window.clearInterval(interval); document.removeEventListener("visibilitychange", visible); media.removeEventListener("change", changed); };
  }, [client]);

  const attention = attentionItems(state.snapshot, state.entries);
  // 首次查询尚未失败时只表达连接中，不能先闪出断线警告再立刻移除。
  const connecting = state.snapshot === null && state.error === null;
  const currentAgent = agentView(state.snapshot, state.online || connecting, attention);
  const agent = state.snapshot === null && state.startup ? { state: state.startup.phase === "failed" ? "failed" as const : "waiting-external" as const, headline: state.startup.phase === "failed" ? "启动失败" : "Engine 正在启动", detail: state.startup.phase === "failed" ? "请查看诊断日志定位失败阶段。" : ({ sqlite:"正在打开本机数据。", models:"正在准备模型请求服务。", execution:"正在准备行动服务。", subject_restore:"正在恢复主体状态。", subject_start:"正在启用主体工作。" } as Record<string,string>)[state.startup.startup_stage ?? ""] ?? (state.startup.startup_stage?.startsWith("plugin_") ? "正在准备输入与设备插件。" : "配置与设备准备完成后即可接收新的工作。"), focus: null } : currentAgent;
  useEffect(() => { document.title = `${agent.headline} · Purrcept`; }, [agent.headline]);
  const current = PAGES.find((item) => item.id === route.page);
  const age = state.receivedAt === null ? null : Math.floor((Date.now() - state.receivedAt) / 1000);
  const freshness = age !== null ? `更新于 ${age} 秒前` : connecting ? "正在取得状态" : "尚未取得状态";
  const inspectable = ["timeline", "reviews", "devices", "activities"].includes(route.page);
  const hasSelection = route.id !== null && inspectable;
  const close = (): void => go({ page: route.page, id: null });

  return (
    <CelProvider>
      <div className="panel-shell" data-inspector={!narrow && inspectable}>
        <button className="skip-link pc-focusable" onClick={() => document.getElementById("main")?.focus()}>跳到主要内容</button>
        <nav className="navigation" aria-label="主导航">
          <a className="wordmark pc-focusable" href="#/home" aria-label="Purrcept 首页"><BrandMark /></a>
          {PAGES.map((item) => (
            <a key={item.id} className="nav-item pc-focusable" href={routeHref({ page: item.id, id: null })}
              title={`${item.label} · ${item.description}`} aria-current={route.page === item.id ? "page" : undefined}>
              {/* 当前项指示块是同一个对象，以 move 弹簧从旧位置滑到新位置；文字颜色同帧切换。 */}
              {route.page === item.id ? <motion.span layoutId="nav-current" className="nav-indicator" transition={transitions.move} aria-hidden="true" /> : null}
              <Icon name={item.icon} /><span>{item.label}</span>
              {item.id === "attention" && attention.length > 0 ? <span className="nav-count" aria-label={`${attention.length} 项待处理`}>{attention.length}</span> : null}
            </a>
          ))}
        </nav>

        <div className="main-column">
          <header className="topbar">
            <div className="location"><span className="product-name">Purrcept</span><span className="location-divider" aria-hidden="true">/</span><span>{current?.label}</span></div>
            <div className="topbar-status">
              {route.page !== "home" ? <StateMark state={agent.state} label={agent.headline} /> : null}
              <span className="topbar-age">{freshness}{!state.online && state.snapshot !== null ? " · 已过期" : ""}</span>
              {!state.online && !connecting ? <Button variant="ghost" onClick={() => void client.refresh()}>立即重试</Button> : null}
            </div>
          </header>
          {!state.online && !connecting ? (
            <Feedback state="warning" scope="system" urgency="passive" persistence="until-resolved"
              title={state.snapshot ? "与本机服务的连接中断" : "正在等待本机服务"}>
              {state.snapshot ? "页面显示的是断开前的状态，操作已暂停。" : "确认 Engine 正在运行。"}她已经接手的工作不会因页面断开而取消。{state.error ? ` ${state.error}` : ""}
            </Feedback>
          ) : null}

          <main id="main" className="main-stage" tabIndex={-1}>
            {/* 同级切换立即替换内容，不保留退场页，避免列宽变化时双页叠字和白闪。
                key 只重置各页的局部筛选；宿主观察和整个外壳保持同一生命周期。 */}
            <div key={route.page} className="page pc-enter" data-measure={route.page === "attention" || route.page === "system" ? "reading" : "wide"}>
              {state.snapshot === null && route.page !== "home"
                ? <section><h1>{current?.label}</h1><p className="page-intro">正在等待本机服务的状态。页面不会启动她或打开设备。</p></section>
                : <PageBody page={route.page} state={state} client={client} agent={agent} attention={attention} route={route} go={go} />}
            </div>
            {state.storageError ? (
              <Feedback state="warning" scope="region" urgency="passive" persistence="retained" title="操作记录没有保存到浏览器">{state.storageError}</Feedback>
            ) : null}
            {state.controlError ? (
              <Feedback state="warning" scope="region" urgency="passive" persistence="until-resolved" title="操作等待名额已满">{state.controlError}</Feedback>
            ) : null}
          </main>
        </div>

        {!narrow ? (inspectable ? <aside className="inspector" aria-label="详情">
            {hasSelection ? <>
              <div className="inspector-heading"><span className="eyebrow">详情</span><Button variant="ghost" onClick={close}>关闭</Button></div>
              <div key={route.id} className="pc-enter"><Selection route={route} state={state} client={client} go={go} /></div>
            </> : <div className="inspector-empty">
              <span className="item-icon" aria-hidden="true"><Icon name={current?.icon ?? "home"} /></span>
              <h2>查看{route.page === "reviews" ? "刺激与行动" : route.page === "timeline" ? "行动结果" : route.page === "devices" ? "设备状态" : "活动进展"}</h2>
              <p className="muted">{route.page === "reviews" ? "选择一次行动，阅读完整材料并留下评价。" : route.page === "timeline" ? "选择一条记录，查看每项操作的实际结果。" : route.page === "devices" ? "选择“查看详情”，查看设备的全部状态。" : "选择“查看详情”，查看活动的完整记录。"}</p>
            </div>}
          </aside> : null
        ) : (
          <Sheet open={hasSelection} onOpenChange={(open) => { if (!open) close(); }} title={current?.label ?? "详情"} description="详情">
            <Selection route={route} state={state} client={client} go={go} />
          </Sheet>
        )}
      </div>
    </CelProvider>
  );
}
