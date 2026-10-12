/**
 * 本机观察台的产品视图：概况、有限设备控制、活动与真实回执。
 * 展示只来自 PanelClient；点击先形成明确意图，不乐观改写设备或活动状态。
 * Inspector 与窄屏 Sheet 共用同一事实内容，首页吉祥物不承担业务结果证明。
 */
import { useEffect, useState, useSyncExternalStore } from "react";
import type { ReactElement, ReactNode } from "react";
import { MotionConfig } from "motion/react";
import { Button, CelProvider, EFFECT_OUTCOMES, Feedback, Icon, Mascot, Sheet, StatusMark } from "@purrcept/cel";
import type { AgentState, IconName } from "@purrcept/cel";
import { awaiting, PanelClient } from "./client";
import type { Entry, PanelState } from "./client";
import type { Action, Device, DeviceControl, Json, ObjectValue, Session, Snapshot } from "./contracts";
import { activityReason, controlMark, deviceMark, mark } from "./semantics";
import type { Mark } from "./semantics";

type Page = "overview" | "devices" | "activities" | "receipts";
const PAGES: { id: Page; label: string; icon: IconName; description: string }[] = [
  { id: "overview", label: "概况", icon: "ring", description: "主体、预算与当前积压" },
  { id: "devices", label: "设备", icon: "person", description: "连接、收音与缓存观察" },
  { id: "activities", label: "活动", icon: "pause", description: "目标、运行额度与控制确认" },
  { id: "receipts", label: "回执", icon: "check", description: "真实调用与本页控制记录" },
];
const DEVICE_NAMES: Readonly<Record<string, string>> = { qq: "QQ 连接", voice_input: "收音", audio: "语音输出", computer: "电脑观察", live2d: "Live2D 身体" };
const ALLOWED_CONTROLS: Readonly<Record<string, readonly string[]>> = {
  qq: ["reconnect"], voice_input: ["devices", "start", "stop"], audio: ["stop_output"],
  computer: ["connect", "disconnect", "observe_start", "observe_pause"], live2d: [],
};

/** 状态保留 Cel 的形状与颜色；唯一持续运动留给首页 Mascot。 */
function StaticMark(props: Mark): ReactElement {
  return <MotionConfig reducedMotion="always"><StatusMark state={props.state} label={props.label} /></MotionConfig>;
}
function time(value: number | null): string {
  return value === null ? "未提供" : new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}
function duration(value: number): string { return `${Math.floor(value / 60)} 分 ${Math.floor(value % 60)} 秒`; }
function fact(value: Json | undefined): string {
  if (value === undefined || value === null) return "未提供";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value).slice(0, 512);
  return String(value);
}
function Facts({ items }: { items: [string, Json | undefined][] }): ReactElement {
  return <dl className="facts">{items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{fact(value)}</dd></div>)}</dl>;
}
function Unavailable({ children }: { children: ReactNode }): ReactElement { return <p className="muted">{children}</p>; }

/** 全局状态影响写入资格，局部业务结果仍留在对象自身。 */
function blocked(state: PanelState): string | undefined {
  if (!state.online) return "快照已过期，等待本机服务恢复后再操作。";
  if (!state.snapshot?.authority_available) return "权威存储不可用，不能接管新控制。";
  if (state.snapshot.phase !== "running") return "宿主尚未运行或正在关闭，不接管新控制。";
  return undefined;
}
function targetReason(state: PanelState, target: string, operation: string): string | undefined {
  // 生命周期停止必须能打断其它控制；这里只合并相同操作，不能锁住整个设备或活动。
  return blocked(state) ?? (state.entries.some((entry) => entry.request.instance_id === state.snapshot?.instance_id && entry.request.target === target && entry.request.operation === operation && awaiting(entry)) ? "同一操作尚未确认；请先查询或核对原回执。" : undefined);
}

/** 当前预算来自会话单调时钟快照，不在浏览器自行倒计时或补充额度。 */
function SessionView({ title, session }: { title: string; session: Session | null }): ReactElement {
  const state: Mark = session === null ? { state: "warning", label: "主体状态未提供" }
    : session.closing ? { state: "waiting-external", label: "正在收尾" }
    : session.busy ? { state: "reasoning", label: "正在推进会话" } : { state: "idle", label: "当前无会话运行" };
  return <section className="session-region"><h3>{title}</h3><StaticMark {...state} />
    {session === null ? null : <>
      <p className="muted">生成代次 {session.generation} · 预算按整轮实际请求计量。</p>
      {session.budgets.length === 0 ? <Unavailable>当前没有运行中的预算。</Unavailable> : session.budgets.map((budget, index) => <p key={index}>
        已用 {budget.used_requests} / {budget.max_requests} 次模型请求 · 剩余 {duration(budget.remaining_seconds)}<br />
        <span className="muted">快照时刻的剩余值；等待模型与设备也计入运行时间。</span>
      </p>)}
    </>}
  </section>;
}

function Overview({ snapshot: s, online, select }: { snapshot: Snapshot; online: boolean; select: (action: Action) => void }): ReactElement {
  const count = s.pending_inputs.reduce((sum, item) => sum + item.count, 0);
  const bytes = s.pending_inputs.reduce((sum, item) => sum + item.logical_bytes, 0);
  const hostMark = online ? mark(s.phase) : { state: "warning" as const, label: "宿主旧快照已过期" };
  const mascotState: AgentState = !online ? "warning" : !s.authority_available ? "failed" : s.phase !== "running" ? hostMark.state
    : s.foreground === null ? "warning" : s.foreground.busy || s.reflection?.busy ? "reasoning" : "idle";
  return <>
    <section className="home-stage">
      <div className="home-copy"><p className="eyebrow">PURRCEPT / 本机观察台</p><h1>持续行动，<br />此刻有据可查。</h1>
        <StaticMark {...hostMark} /><p>页面读取当前宿主的事实。关闭浏览器后，<br />已接管的工作与设备寿命仍由宿主拥有。</p>
        <p className="muted">{s.authority_available ? "权威存储可用" : "权威存储不可用"} · 已装配 {s.devices.length} 个插件</p>
      </div><div className="home-mascot" aria-hidden="true"><Mascot state={mascotState} /></div>
    </section>
    <section><h2>主体与反思</h2><div className="sessions"><SessionView title="前台主体" session={s.foreground} /><SessionView title="独立反思" session={s.reflection} /></div></section>
    <section><h2>积压与阻塞</h2><p className="lead">{count} 项待处理输入 <span className="muted">/ 上限 {s.pending_limits.count} 项</span></p>
      <p>逻辑体积 {bytes.toLocaleString()} / {s.pending_limits.logical_bytes.toLocaleString()} 字节</p>
      {count >= s.pending_limits.count || bytes >= s.pending_limits.logical_bytes ? <Feedback state="warning" scope="region" urgency="passive" persistence="until-resolved" title="输入容量已达到边界">新输入可能被明确拒绝或等待容量恢复；已有事实不被挤掉。</Feedback> : null}
      {s.pending_inputs.length === 0 ? <Unavailable>没有待处理输入。</Unavailable> : <div className="flat-list">{s.pending_inputs.map((item) => <div className="flat-row" key={item.stage}><span>{item.stage}</span><span>{item.count} 项 · {item.logical_bytes.toLocaleString()} 字节</span></div>)}</div>}
      {s.pending_resources.length > 0 ? <Feedback state="warning" scope="region" urgency="passive" persistence="until-resolved" title="仍有资源等待收回">{s.pending_resources.join("、")}</Feedback> : null}
      {s.knowledge === null ? null : <Facts items={[["语义索引启用", s.knowledge.semantic_enabled], ["索引故障", s.knowledge.index_failure], ["索引待收回资源", s.knowledge.pending_resources]]} />}
    </section>
    <section><h2>近期真实回执</h2><ActionList actions={s.recent_actions.slice(0, 3)} select={select} /></section>
  </>;
}

/** 控制只来自已声明快捷键；页面不按模型能力目录生成任意按钮。 */
function QuickControl({ client, state, device, control, args = {}, reason }: {
  client: PanelClient; state: PanelState; device: Device; control: DeviceControl;
  args?: ObjectValue; reason?: string;
}): ReactElement {
  const disabledReason = targetReason(state, device.instance_id, control.key) ?? (device.revision === null ? "插件没有控制版本。" : reason);
  return <Button variant="secondary" disabledReason={disabledReason} onClick={() => { if (device.revision !== null) void client.submit(device.instance_id, control.key, device.revision, args, control.title); }}>{control.title}</Button>;
}
interface InputDevice { index: number; name: string }
/** 缓存设备与模式必须是可解释的具体元数据，缺失时不补系统默认设备。 */
function inputDevices(value: Json | undefined): InputDevice[] {
  if (!Array.isArray(value)) return [];
  const devices: InputDevice[] = [];
  for (const item of value) {
    if (typeof item === "object" && item !== null && !Array.isArray(item) && typeof item.index === "number" && Number.isSafeInteger(item.index) && item.index >= 0 && typeof item.name === "string") devices.push({ index: item.index, name: item.name });
  }
  return devices;
}
function VoiceControls({ client, state, device }: { client: PanelClient; state: PanelState; device: Device }): ReactElement {
  const devices = inputDevices(device.state.devices);
  const modes = Array.isArray(device.state.available_modes) ? device.state.available_modes.filter((item): item is string => item === "asr" || item === "audio") : [];
  const [input, setInput] = useState("");
  const [mode, setMode] = useState("");
  const selectedDevice = devices.find((item) => String(item.index) === input);
  const selectedMode = modes.includes(mode) ? mode : undefined;
  return <>
    <div className="input-selection"><label>输入设备<select value={input} onChange={(event) => setInput(event.target.value)} disabled={devices.length === 0}><option value="">选择输入设备</option>{devices.map((item) => <option key={item.index} value={item.index}>{item.name} · {item.index}</option>)}</select></label>
      <label>输入模式<select value={mode} onChange={(event) => setMode(event.target.value)} disabled={modes.length === 0}><option value="">选择输入模式</option>{modes.map((item) => <option key={item} value={item}>{item === "asr" ? "ASR 识别为文字" : "原生音频"}</option>)}</select></label>
    </div>
    {devices.length === 0 ? <Unavailable>{device.state.devices_loaded === false ? "尚未枚举输入设备。先明确刷新设备列表，再选择收音设备。" : "插件没有可选输入设备，开始收音不可用。"}</Unavailable> : null}
    {modes.length === 0 ? <Unavailable>插件尚未提供可用输入模式，开始收音不可用。</Unavailable> : null}
    <div className="actions">{device.controls.filter((control) => ALLOWED_CONTROLS.voice_input?.includes(control.key)).map((control) => {
      const isStart = control.key === "start" || control.key === "start_input";
      const supportedFields = control.fields.every((field) => field === "device" || field === "mode");
      const args: ObjectValue = {};
      if (control.fields.includes("device") && selectedDevice) args.device = selectedDevice.index;
      if (control.fields.includes("mode") && selectedMode) args.mode = selectedMode;
      const reason = !supportedFields || (isStart && !["device", "mode"].every((field) => control.fields.includes(field))) ? "收音快捷操作缺少明确的设备/模式参数声明。" : isStart
        ? device.state.state !== "stopped" && device.state.state !== "failed" && device.state.state !== "paused" ? "已有收音会话，请先停止。"
          : selectedDevice === undefined || selectedMode === undefined ? "先选择插件提供的设备与输入模式。"
            : selectedMode === "asr" && device.state.asr_ready !== true ? "ASR 尚未配置可用的显式识别凭据。"
              : selectedMode === "audio" && device.state.audio_ready !== true ? "生成模型尚未声明原生音频能力。" : undefined
        : undefined;
      return <QuickControl key={control.key} client={client} state={state} device={device} control={control} args={args} reason={reason} />;
    })}</div>
  </>;
}

/** 缓存预览只由 GET 获取，不连接、不采集；失去新鲜快照即保留过期说明。 */
function CachedPreview({ device, online, refreshAt }: { device: Device; online: boolean; refreshAt: number | null }): ReactElement {
  const [frame, setFrame] = useState<{ url: string; captured: number | null; etag: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [empty, setEmpty] = useState(false);
  useEffect(() => {
    if (!online || !device.has_preview) return;
    let current = true;
    const abort = new AbortController();
    const load = async (): Promise<void> => {
      try {
        const headers: Record<string, string> = {};
        if (frame?.etag) headers["If-None-Match"] = frame.etag;
        const response = await fetch(`/api/preview/${encodeURIComponent(device.instance_id)}`, { cache: "no-store", headers, signal: abort.signal });
        if (!current) return;
        if (response.status === 304) { setError(null); return; }
        if (response.status === 204) { setEmpty(true); setError(null); setFrame(null); return; }
        if (!response.ok || !response.headers.get("Content-Type")?.startsWith("image/")) throw new Error("preview");
        const blob = await response.blob();
        if (!current) return;
        const raw = response.headers.get("X-Captured-At");
        const captured = raw !== null && Number.isFinite(Number(raw)) && Number(raw) >= 0 ? Number(raw) : null;
        setFrame({ url: URL.createObjectURL(blob), captured, etag: response.headers.get("ETag") });
        setEmpty(false); setError(null);
      } catch {
        if (current) setError("缓存预览读取失败；已有画面不能代表当前桌面。");
      }
    };
    void load();
    return () => { current = false; abort.abort(); };
    // frame 是已读缓存；新的快照触发条件查询，不能由 setFrame 递归请求。
  }, [device.instance_id, device.has_preview, online, refreshAt]);
  useEffect(() => () => { if (frame) URL.revokeObjectURL(frame.url); }, [frame]);
  return <section className="preview"><h3>最后取得的画面</h3>
    {frame ? <><img src={frame.url} alt="电脑插件最后取得的缓存画面" /><p className="muted">采集于 {time(frame.captured)}{!online || error ? " · 旧缓存，当前状态未确认" : " · 缓存画面"}</p></> : <Unavailable>{empty ? "没有缓存画面。" : "尚无可显示的缓存画面。"}</Unavailable>}
    <p className="muted">刷新预览只读取已有缓存，不触发截图。</p>
    {error ? <StaticMark state="warning" label={error} /> : null}
  </section>;
}

function deviceFacts(device: Device): [string, Json | undefined][] {
  const s = device.state;
  switch (device.kind) {
    case "qq": return [["连接方式", s.mode], ["恢复尝试", s.attempts], ["连接代次", s.generation], ["原因", s.reason], ["等待真实响应", s.pending_calls], ["迟到结果待核对", s.pending_late_results], ["未接管缺口", s.gap_counts]];
    case "voice_input": return [["输入模式", s.mode], ["输入设备索引", s.device], ["收音会话", s.session_id], ["当前片段", s.utterance_id], ["识别状态", s.recognition_state], ["识别中的文字", s.partial], ["待交付片段", s.pending_utterances], ["输入电平", s.level], ["故障", s.error]];
    case "audio": return [["当前播放", s.active], ["候播数量", Array.isArray(s.queued) ? s.queued.length : undefined], ["候播上限", s.max_pending], ["最近真实结果", s.last], ["故障", s.error]];
    case "computer": return [["连续观察", s.observing], ["观察代次", s.generation], ["当前窗口", s.foreground], ["等待主体处理的输入", s.input_id], ["最新候选画面", s.candidate], ["采集时间", typeof s.captured_at === "number" ? time(s.captured_at) : undefined], ["故障", s.error]];
    case "live2d": return [["播放关联", s.playback_id], ["姿态意图", s.intent_id], ["已经采用", s.adopted], ["播放进度（秒）", s.elapsed_seconds], ["故障", s.error]];
    default: return [["状态", s.state], ["原因", s.reason], ["故障", s.error]];
  }
}
function Devices({ client, state }: { client: PanelClient; state: PanelState }): ReactElement {
  const devices = state.snapshot?.devices ?? [];
  return <><h1>设备与连接</h1><p className="page-intro">设备状态来自已装配插件。打开或刷新页面只查询，开始、停止与恢复均由明确操作发起。</p>
    {devices.length === 0 ? <Unavailable>当前宿主没有已装配插件。</Unavailable> : devices.map((device) => <article className="device-object" key={device.instance_id}>
      <div className="object-header"><h2>{DEVICE_NAMES[device.kind ?? ""] ?? "已装配插件"}</h2><StaticMark {...(state.online ? deviceMark(device) : { state: "warning", label: `旧快照已过期 · 上次${deviceMark(device).label}` })} /></div>
      <p className="identity">{device.instance_id}</p><Facts items={deviceFacts(device)} />
      {device.kind === "qq" ? <p className="muted">{device.state.mode === "reverse" ? "反向模式由 Engine 监听，等待外部 QQ 端连入。恢复只检查或恢复监听。" : "主动模式按有限次数恢复连接。"}连接恢复不会重发旧消息或未知发送。</p> : null}
      {device.kind === "audio" ? <p className="muted">停止语音输出会撤销当前合成/播放及已经候播的现场语音。此后新接管的说话仍可发声。</p> : null}
      {device.kind === "voice_input" ? <VoiceControls client={client} state={state} device={device} /> : <div className="actions">{device.controls.filter((control) => ALLOWED_CONTROLS[device.kind ?? ""]?.includes(control.key)).map((control) => <QuickControl key={control.key} client={client} state={state} device={device} control={control}
        reason={control.fields.length > 0 ? "该快捷操作需要页面尚不支持的参数。" : computerReason(device, control.key)} />)}</div>}
      {device.controls.length === 0 || device.kind === "live2d" ? <Unavailable>{device.kind === "live2d" ? "本页只观察 Live2D 状态。" : "此插件没有声明面板快捷控制。"}</Unavailable> : null}
      {device.kind === "computer" && device.has_preview ? <CachedPreview device={device} online={state.online} refreshAt={state.receivedAt} /> : null}
    </article>)}
  </>;
}
/** 控制资格用明确连接/观察事实，缺失字段不会假设设备处于安全初始态。 */
function computerReason(device: Device, operation: string): string | undefined {
  if (device.kind !== "computer") return undefined;
  if (typeof device.state.connected !== "boolean" || typeof device.state.observing !== "boolean") return "电脑连接或观察状态未提供。";
  if (operation === "connect" && device.state.connected) return "电脑已经连接。";
  if (operation !== "connect" && !device.state.connected) return "请先明确连接电脑。";
  if (operation === "observe_start" && device.state.observing) return "连续观察已经运行。";
  if (operation === "observe_pause" && !device.state.observing) return "连续观察已暂停。";
  return undefined;
}

function Activities({ client, state }: { client: PanelClient; state: PanelState }): ReactElement {
  return <><h1>持续活动</h1><p className="page-intro">暂停与结束先撤销新步骤资格，再等待实际收尾。恢复沿用原余额，不追加额度。</p>
    {state.snapshot?.activities.length === 0 ? <Unavailable>当前没有活动记录。</Unavailable> : state.snapshot?.activities.map((activity) => <article className="activity-object" key={activity.activity_id}>
      <div className="object-header"><h2>{activity.goal}</h2><StaticMark {...mark(activity.state)} /></div>
      <p className="identity">{activity.definition} · {activity.activity_id}</p>
      <p className="lead">剩余 {duration(activity.remaining_seconds)}</p><p className="muted">{activity.budget_basis === "committed_checkpoint" ? "按最近成功提交的计时检查点。" : `计时依据：${activity.budget_basis}。`}{activity.timing_incomplete ? "异常中断的末段计时不完整。" : ""}</p>
      {activity.needs_reconciliation ? <Feedback state="warning" scope="object" urgency="passive" persistence="until-resolved" title="实际结果仍待主体核对">重启或中断后的活动不会自动恢复；普通恢复不能解除核对要求。</Feedback> : null}
      {activity.progress_preview ? <><h3>最近关键进展</h3><pre>{activity.progress_preview.text}{activity.progress_preview.truncated ? `\n…（仅预览，原文 ${activity.progress_preview.original_chars} 字）` : ""}</pre></> : null}
      {activity.last_command ? <section className="command-fact"><h3>最近控制确认</h3><StaticMark {...mark(activity.last_command.status)} /><Facts items={[["控制", activity.last_command.command], ["实际状态", activity.last_command.actual_state], ["采用确认", activity.last_command.applied]]} />{activity.last_command.error ? <p>{activity.last_command.error.text}{activity.last_command.error.truncated ? " …（已截断）" : ""}</p> : null}</section> : null}
      <div className="actions">{(["pause", "resume", "end"] as const).map((operation) => <Button variant="secondary" key={operation} disabledReason={targetReason(state, `activity:${activity.activity_id}`, operation) ?? activityReason(activity, operation)} onClick={() => void client.submit(`activity:${activity.activity_id}`, operation, String(activity.version), {}, { pause: "暂停活动", resume: "恢复活动", end: "结束活动" }[operation])}>{ { pause: "暂停", resume: "恢复", end: "结束" }[operation]}</Button>)}</div>
      <p className="muted">版本 {activity.version} · 运行代次 {activity.run_generation} · 最近命令 <span className="identity">{activity.last_command_id}</span></p>
    </article>)}
  </>;
}

function ActionList({ actions, select }: { actions: Action[]; select: (action: Action) => void }): ReactElement {
  return actions.length === 0 ? <Unavailable>还没有行动回执。</Unavailable> : <div className="flat-list">{actions.map((action) => <div className="receipt-row" key={action.action_id}>
    <button className="receipt-select pc-focusable" onClick={() => select(action)}><span className="identity">{action.action_id}</span><span>{action.calls.length} 次真实能力调用 · {time(action.created_at)}</span></button>
    <StaticMark {...mark(action.state)} />
    {action.calls.some((call) => call.state === "unknown") ? <StaticMark state="warning" label="包含结果未知的调用" /> : null}
  </div>)}</div>;
}

/** completed 不是全部副作用证明，逐调用 confirmed/unknown/未开始独立展示。 */
function ActionDetail({ action }: { action: Action }): ReactElement {
  const outcomes = [
    { key: "confirmed", title: `${EFFECT_OUTCOMES.committed.label}（由调用回执确认）`, family: EFFECT_OUTCOMES.committed.family },
    { key: "unknown", title: EFFECT_OUTCOMES.unknown.label, family: EFFECT_OUTCOMES.unknown.family },
    { key: "not_started", title: `${EFFECT_OUTCOMES.skipped.label}（未开始）`, family: EFFECT_OUTCOMES.skipped.family },
    { key: "failed", title: "失败（错误不等于全部回滚）", family: "failed" },
    { key: "pending", title: "等待实际回执", family: "waiting" },
  ];
  return <div className="action-detail"><p className="identity">{action.action_id}</p><StaticMark {...mark(action.state)} />
    <p className="muted">逐调用的确认是所属协议/Provider 的真实回执，不额外保证独立观察或外部效果永久维持。</p>
    {action.error ? <p>{action.error.text}{action.error.truncated ? " …（已截断）" : ""}</p> : null}
    {outcomes.map((group) => {
      const calls = action.calls.filter((call) => group.key === "pending" ? !["confirmed", "unknown", "not_started", "failed"].includes(call.state) : call.state === group.key);
      return calls.length === 0 ? null : <section key={group.key} className="call-group"><h3 className="pc-tint" data-family={group.family}>{group.title}</h3>{calls.map((call) => <div className="call-item" key={call.call_id}>
        <p className="identity">{call.path}</p><StaticMark {...mark(call.state)} /><p className="muted">回执版本 {call.receipt_revision} · 媒体 {call.media_count} 项</p>
        {call.error ? <p>{call.error_type}: {call.error.text}{call.error.truncated ? " …（已截断）" : ""}</p> : null}
        {call.result ? <pre>{call.result.text}{call.result.truncated ? `\n…（仅预览，原文 ${call.result.original_chars} 字）` : ""}</pre> : null}
      </div>)}</section>;
    })}
    {action.calls.length === 0 ? <Unavailable>这项行动没有逐次能力调用。</Unavailable> : null}
  </div>;
}

function ControlEntry({ entry, client, state, select }: { entry: Entry; client: PanelClient; state: PanelState; select: (action: Action) => void }): ReactElement {
  const r = entry.result;
  const actualAction = r?.action;
  const expired = state.snapshot !== null && entry.request.instance_id !== state.snapshot.instance_id;
  return <article className="control-entry"><h3>{entry.title}</h3><p className="identity">请求 {entry.request.request_id}</p>
    <StaticMark {...(entry.sending ? { state: "waiting-external", label: "正在等待接管回应" } : r ? controlMark(r) : entry.rejected ? { state: "failed", label: "请求被接入层拒绝" } : { state: "warning", label: "是否接管仍未知" })} />
    {expired ? <Unavailable>此请求属于旧宿主实例，不能向当前实例重传。</Unavailable> : null}
    {entry.error ? <p>{entry.error}</p> : null}
    {r?.error ? <p>{r.error} <span className="identity">{r.error_code}</span></p> : null}
    {actualAction ? <><StaticMark {...mark(actualAction.state)} /><Button variant="ghost" onClick={() => select(actualAction)}>查看逐调用真实回执</Button></> : r?.action_id ? <Unavailable>已关联行动 {r.action_id}，尚未取得真实回执。</Unavailable> : null}
    {r?.activity_command ? <><p>活动命令：<StaticMark {...mark(r.activity_command.status)} /></p><Facts items={[["实际状态", r.activity_command.actual_state], ["采用确认", r.activity_command.applied], ["错误", r.activity_command.error]]} />{r.activity_command.status !== "confirmed" ? <Unavailable>已受理不代表实际收尾已结束，继续查询同一命令。</Unavailable> : null}</> : r?.command_id ? <Unavailable>已关联活动命令 {r.command_id}，尚未取得驱动确认。</Unavailable> : null}
    <div className="actions"><Button variant="secondary" disabledReason={!state.online ? "本机服务未确认在线。" : undefined} loading={entry.sending} onClick={() => void client.observe(entry.request.request_id)}>查询原控制结果</Button>
      {entry.unresolved ? <Button variant="secondary" loading={entry.sending} disabledReason={!state.online ? "本机服务未确认在线。" : expired ? "不能跨宿主实例重传。" : undefined} onClick={() => void client.retry(entry.request.request_id)}>原 ID 重传</Button> : null}
    </div>
  </article>;
}

/** 单页挂载只建立轮询；四个导航视图共用同一客户端和状态所有者。 */
export function Panel({ client }: { client: PanelClient }): ReactElement {
  const state = useSyncExternalStore(client.subscribe, client.getState);
  const [page, setPage] = useState<Page>("overview");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedFallback, setSelectedFallback] = useState<Action | null>(null);
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 72rem)").matches);
  const [, tick] = useState(0);
  useEffect(() => {
    void client.refresh();
    const interval = window.setInterval(() => { tick((value) => value + 1); void client.refresh(); }, 2000);
    const media = window.matchMedia("(max-width: 72rem)");
    const changed = (): void => setNarrow(media.matches);
    media.addEventListener("change", changed);
    return () => { window.clearInterval(interval); media.removeEventListener("change", changed); };
  }, [client]);
  const selected = state.snapshot?.recent_actions.find((action) => action.action_id === selectedId)
    ?? state.entries.find((entry) => entry.result?.action?.action_id === selectedId)?.result?.action ?? selectedFallback;
  const select = (action: Action): void => { setSelectedId(action.action_id); setSelectedFallback(action); };
  const close = (): void => { setSelectedId(null); setSelectedFallback(null); };
  const currentPage = PAGES.find((item) => item.id === page);
  const age = state.receivedAt === null ? null : Math.floor((Date.now() - state.receivedAt) / 1000);
  const recentControl = state.entries[0];
  return (
    <CelProvider>
      <div className="panel-shell">
        <nav className="navigation" aria-label="观察台导航">
          <span className="wordmark" title="Purrcept">P.</span>
          {PAGES.map((item) => (
            <button key={item.id} className="nav-item pc-focusable" aria-label={item.label}
              title={`${item.label} · ${item.description}`} aria-current={page === item.id ? "page" : undefined}
              onClick={() => { setPage(item.id); close(); }}>
              <Icon name={item.icon} /><span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="main-column">
          <header className="topbar">
            <div><span className="eyebrow">PURRCEPT ENGINE</span><span className="location">{currentPage?.label}</span></div>
            <div className="topbar-status">
              <StaticMark state={state.online ? "completed" : "warning"}
                label={state.online ? "本机服务已回应" : state.snapshot ? "连接中断 · 旧快照已过期" : "尚未取得本机快照"} />
              <Button variant="ghost" onClick={() => void client.refresh()}>刷新状态</Button>
            </div>
          </header>
          {!state.online ? (
            <Feedback state="warning" scope="system" urgency="passive" persistence="until-resolved"
              title={state.snapshot ? "旧快照已过期，不能当作当前在线状态" : "等待本机服务快照"}>
              {state.error ?? "正在读取当前宿主。"} 已接管工作不会因页面断开被取消。
            </Feedback>
          ) : null}

          <main id="main" className="main-stage">
            {state.snapshot === null ? (
              <section><h1>本机观察台</h1><p>等待同源宿主提供状态。页面不会启动主体或打开设备。</p></section>
            ) : page === "overview" ? (
              <Overview snapshot={state.snapshot} online={state.online} select={select} />
            ) : page === "devices" ? (
              <Devices client={client} state={state} />
            ) : page === "activities" ? (
              <Activities client={client} state={state} />
            ) : (
              <>
                <h1>行动与控制回执</h1>
                <p className="page-intro">行动结束与实际效果分开呈现。未知结果保留原身份，不自动重做。</p>
                <section><h2>近期行动</h2><ActionList actions={state.snapshot.recent_actions} select={select} /></section>
                <section>
                  <h2>本标签页控制记录</h2>
                  {state.entries.length === 0 ? <Unavailable>本标签页还没有提交控制。</Unavailable>
                    : state.entries.map((entry) => <ControlEntry key={entry.request.request_id} entry={entry} client={client} state={state} select={select} />)}
                </section>
              </>
            )}
            {page !== "receipts" && recentControl ? (
              <section><h2>最近控制</h2><ControlEntry entry={recentControl} client={client} state={state} select={select} /></section>
            ) : null}
            {state.storageError ? (
              <Feedback state="warning" scope="region" urgency="passive" persistence="retained" title="页面控制记录保存异常">
                {state.storageError}
              </Feedback>
            ) : null}
            <footer className="page-footer">
              <p>{state.receivedAt === null ? "尚无快照时间" : `快照取得于 ${new Date(state.receivedAt).toLocaleTimeString("zh-CN")} · ${age} 秒前`}{!state.online ? " · 已过期" : ""}</p>
              {state.snapshot ? <p className="identity">宿主 {state.snapshot.instance_id} · 版本 {state.snapshot.host_version}</p> : null}
            </footer>
          </main>
        </div>

        {!narrow ? (
          <aside className="inspector">
            <div className="inspector-heading"><h2>事实详情</h2>{selected ? <Button variant="ghost" onClick={close}>关闭</Button> : null}</div>
            {selected ? <ActionDetail action={selected} /> : <p className="muted">选择一项行动，查看逐调用真实状态、有限结果预览与未确认部分。</p>}
            <p className="muted">面板不显示完整提示或行动代码，也不执行页面聊天。</p>
          </aside>
        ) : (
          <Sheet open={selected !== null} onOpenChange={(open) => { if (!open) close(); }} title="真实回执" description="逐调用结果与有限预览">
            {selected ? <ActionDetail action={selected} /> : null}
          </Sheet>
        )}
      </div>
    </CelProvider>
  );
}
