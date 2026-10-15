/**
 * 设备：每个已装配插件是一张对象卡，显示人话状态、关键事实与允许的快捷控制。
 * 打开或刷新页面只查询；开始、停止与恢复都由明确点击发起，结果以宿主回执为准。
 * 全部原始字段在 Inspector 的技术细节中，卡片不平铺实现标识。
 */
import { useEffect, useState } from "react";
import type { ReactElement } from "react";
import { Button, isLive } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import type { Device, DeviceControl, Json, ObjectValue } from "../contracts";
import { deviceMark } from "../semantics";
import { targetReason } from "../model/controls";
import { ALLOWED_CONTROLS, computerReason, deviceNote, deviceSummary } from "../model/devices";
import { deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { EmptyState, PageHeading, StateMark, time, Unavailable } from "../ui/common";

/** 控制只来自已声明快捷键；页面不按模型能力目录生成任意按钮。 */
function QuickControl({ client, state, device, control, args = {}, reason }: {
  client: PanelClient; state: PanelState; device: Device; control: DeviceControl; args?: ObjectValue; reason?: string;
}): ReactElement {
  const disabledReason = targetReason(state, device.instance_id, control.key) ?? (device.revision === null ? "插件没有提供控制版本。" : reason);
  return <Button variant="secondary" disabledReason={disabledReason} onClick={() => { if (device.revision !== null) void client.submit(device.instance_id, control.key, device.revision, args, `${deviceName(device)} · ${control.title}`); }}>{control.title}</Button>;
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
    <div className="input-selection">
      <label>麦克风<select value={input} onChange={(event) => setInput(event.target.value)} disabled={devices.length === 0}><option value="">选择麦克风</option>{devices.map((item) => <option key={item.index} value={item.index}>{item.name}</option>)}</select></label>
      <label>收听方式<select value={mode} onChange={(event) => setMode(event.target.value)} disabled={modes.length === 0}><option value="">选择收听方式</option>{modes.map((item) => <option key={item} value={item}>{item === "asr" ? "识别成文字" : "直接听原声"}</option>)}</select></label>
    </div>
    {devices.length === 0 ? <Unavailable>{device.state.devices_loaded === false ? "还没有读取麦克风列表。先点“刷新设备列表”，再选择麦克风。" : "没有找到可用的麦克风。"}</Unavailable> : null}
    {modes.length === 0 ? <Unavailable>当前配置没有可用的收听方式。</Unavailable> : null}
    <div className="actions">{device.controls.filter((control) => ALLOWED_CONTROLS.voice_input?.includes(control.key)).map((control) => {
      const isStart = control.key === "start" || control.key === "start_input";
      const supportedFields = control.fields.every((field) => field === "device" || field === "mode");
      const args: ObjectValue = {};
      if (control.fields.includes("device") && selectedDevice) args.device = selectedDevice.index;
      if (control.fields.includes("mode") && selectedMode) args.mode = selectedMode;
      const reason = !supportedFields || (isStart && !["device", "mode"].every((field) => control.fields.includes(field))) ? "插件没有声明麦克风或收听方式参数。" : isStart
        ? device.state.state !== "stopped" && device.state.state !== "failed" && device.state.state !== "paused" ? "已经在收音，请先停止。"
          : selectedDevice === undefined || selectedMode === undefined ? "先选择麦克风和收听方式。"
            : selectedMode === "asr" && device.state.asr_ready !== true ? "语音识别还没有配置可用的凭据。"
              : selectedMode === "audio" && device.state.audio_ready !== true ? "直接听原声的模型能力或媒体计量尚未就绪。" : undefined
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
        if (current) setError("读取画面失败；下面的旧画面不代表当前桌面。");
      }
    };
    void load();
    return () => { current = false; abort.abort(); };
    // frame 是已读缓存；新的快照触发条件查询，不能由 setFrame 递归请求。
  }, [device.instance_id, device.has_preview, online, refreshAt]);
  useEffect(() => () => { if (frame) URL.revokeObjectURL(frame.url); }, [frame]);
  return <section className="preview"><h3>最近的画面</h3>
    {frame ? <><img src={frame.url} alt="电脑最近一次截取的画面" /><p className="muted">截取于 {time(frame.captured)}{!online || error ? " · 旧画面，当前状态未确认" : ""}</p></> : <Unavailable>{empty ? "还没有截取过画面。" : "暂无可显示的画面。"}</Unavailable>}
    {error ? <StateMark state="warning" label={error} /> : null}
  </section>;
}

/**
 * 展示已装配设备、预览与明确允许的快捷控制。
 *
 * client/state 共用页面观察；selected 与 go 定位设备。按钮点击才发起业务。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Devices({ client, state, selected, go }: { client: PanelClient; state: PanelState; selected: string | null; go: (route: Route) => void }): ReactElement {
  const devices = state.snapshot?.devices ?? [];
  const liveId = state.online ? devices.find((device) => isLive(deviceMark(device).state))?.instance_id : undefined;
  return <>
    <PageHeading title="设备">她的连接与感官。打开或刷新这个页面不会启动任何设备。</PageHeading>
    {devices.length === 0 ? <EmptyState icon="device" title="还没有启用设备">在配置文件的 plugins 中启用 QQ、收音、语音输出等插件，重启后会出现在这里。</EmptyState>
      : <div className="object-grid">{devices.map((device) => {
        const status = state.online ? deviceMark(device) : { state: "warning" as const, label: `连接中断 · 断开前${deviceMark(device).label}` };
        const note = deviceNote(device);
        return <article className="device-object" key={device.instance_id} aria-current={selected === device.instance_id ? "true" : undefined}>
          <div className="object-header"><h2>{deviceName(device)}</h2><StateMark {...status} live={device.instance_id === liveId} /></div>
          {deviceSummary(device).map((line) => <p key={line}>{line}</p>)}
          {device.kind === "voice_input" ? <VoiceControls client={client} state={state} device={device} />
            : <div className="actions">{device.controls.filter((control) => ALLOWED_CONTROLS[device.kind ?? ""]?.includes(control.key)).map((control) => <QuickControl key={control.key} client={client} state={state} device={device} control={control}
              reason={control.fields.length > 0 ? "这个操作需要页面暂不支持的参数。" : computerReason(device, control.key)} />)}</div>}
          {device.kind === "computer" && device.has_preview ? <CachedPreview device={device} online={state.online} refreshAt={state.receivedAt} /> : null}
          {note !== null ? <p className="muted">{note}</p> : null}
          <div className="actions"><Button variant="ghost" onClick={() => go({ page: "devices", id: device.instance_id })}>查看详情</Button></div>
        </article>;
      })}</div>}
  </>;
}
