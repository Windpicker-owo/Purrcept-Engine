/**
 * 设备、声线与身体工作区（去卡片化主从分层架构）：
 * - 01 主动通信与感官机架：仅将带快捷控制或实时通道摘要的设备（安卓手机、QQ、电脑观察、现场收音、语音输出、Live2D）渲染为紧凑操控台；
 * - 02 内置感知与宿主插槽：将无按钮的底层器官（如图片读取、音频读取、clock、console、browser、bed）收拢为跟手胶囊芯片矩阵，彻底消灭大面积空白白盒。
 */
import { useEffect, useState, type ReactElement } from "react";
import { Button, Field, Icon, Switch, isLive } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import { isSnapshotDelayed } from "../client";
import type { Device, DeviceControl, Json, ObjectValue } from "../contracts";
import { targetReason } from "../model/controls";
import { ALLOWED_CONTROLS, computerReason, deviceNote, deviceSummary, deviceTechnical } from "../model/devices";
import { deviceName } from "../model/names";
import type { Route } from "../model/routes";
import { deviceMark } from "../semantics";
import { EmptyState, FilterPill, PageHeading, StateMark, TechDetails, time, Unavailable } from "../ui/common";
import { PhoneConnection } from "./PhoneConnection";
import { Live2DStudioSection, VoiceStudioSection } from "./StudioPanel";

type DevicesTab = "connections" | "studio" | "live2d";

const ACTIVE_DECK_KINDS = new Set(["qq", "voice_input", "audio", "computer", "live2d"]);

function isActiveDeck(device: Device): boolean {
  if (device.kind === "phone") return false; // 由 PhoneConnection 专属机架承载，避免重复卡片
  if (device.kind !== null && ACTIVE_DECK_KINDS.has(device.kind)) return true;
  const allowed = ALLOWED_CONTROLS[device.kind ?? ""] ?? [];
  return device.controls.some((c) => allowed.includes(c.key)) || deviceSummary(device).length > 0;
}

function deckAccent(kind: string | null): "sky" | "green" | "violet" {
  if (kind === "qq" || kind === "voice_input") return "green";
  if (kind === "computer" || kind === "live2d") return "violet";
  return "sky";
}

function deckCode(device: Device): string {
  switch (device.kind) {
    case "qq":
      return "DECK // SOCIAL LINK";
    case "voice_input":
      return "DECK // VOICE MIC";
    case "audio":
      return "DECK // AUDIO OUT";
    case "computer":
      return "DECK // DESKTOP EYE";
    case "live2d":
      return "DECK // AVATAR BODY";
    default:
      return `DECK // ${(device.kind ?? device.instance_id).toUpperCase()}`;
  }
}

function QuickControl({
  client,
  state,
  device,
  control,
  args = {},
  reason,
}: {
  client: PanelClient;
  state: PanelState;
  device: Device;
  control: DeviceControl;
  args?: ObjectValue;
  reason?: string;
}): ReactElement {
  const disabledReason =
    targetReason(state, device.instance_id, control.key) ?? (device.revision === null ? "插件没有提供控制版本。" : reason);
  return (
    <Button
      variant="secondary"
      disabledReason={disabledReason}
      onClick={() => {
        if (device.revision !== null) {
          void client.submit(device.instance_id, control.key, device.revision, args, `${deviceName(device)} · ${control.title}`);
        }
      }}
    >
      {control.title}
    </Button>
  );
}

interface InputDevice {
  index: number;
  name: string;
}

function inputDevices(value: Json | undefined): InputDevice[] {
  if (!Array.isArray(value)) return [];
  const devices: InputDevice[] = [];
  for (const item of value) {
    if (
      typeof item === "object" &&
      item !== null &&
      !Array.isArray(item) &&
      typeof item.index === "number" &&
      Number.isSafeInteger(item.index) &&
      item.index >= 0 &&
      typeof item.name === "string"
    ) {
      devices.push({ index: item.index, name: item.name });
    }
  }
  return devices;
}

function VoiceControls({ client, state, device }: { client: PanelClient; state: PanelState; device: Device }): ReactElement {
  const devices = inputDevices(device.state.devices);
  const modes = Array.isArray(device.state.available_modes)
    ? device.state.available_modes.filter((item): item is string => item === "asr" || item === "audio")
    : [];
  const [input, setInput] = useState("");
  const [mode, setMode] = useState("");
  const selectedDevice = devices.find((item) => String(item.index) === input);
  const selectedMode = modes.includes(mode) ? mode : undefined;
  const vadControl = device.controls.find((control) => control.key === "set_vad_interrupt");
  const vadEnabled = device.state.vad_interrupt_enabled;
  const vadReason =
    targetReason(state, device.instance_id, "set_vad_interrupt") ??
    (device.revision === null
      ? "插件没有提供控制版本。"
      : typeof vadEnabled !== "boolean"
        ? "自动停播状态未提供。"
        : vadControl?.fields.length !== 1 || vadControl.fields[0] !== "enabled"
          ? "插件没有声明自动停播开关参数。"
          : undefined);

  return (
    <>
      {vadControl ? (
        <div className="setting-row">
          <div>
            <h3>VAD 自动停播</h3>
            <p className="muted">检测到持续人声时停止当前及已有候播的语音。关闭后仍正常收音和分段。</p>
            <p className="muted">本次运行有效，重启后使用配置默认值。</p>
          </div>
          <Switch
            label="VAD 自动停播"
            checked={typeof vadEnabled === "boolean" ? vadEnabled : null}
            disabledReason={vadReason}
            onCheckedChange={(enabled) => {
              if (device.revision !== null) {
                void client.submit(
                  device.instance_id,
                  "set_vad_interrupt",
                  device.revision,
                  { enabled },
                  enabled ? "开启 VAD 自动停播" : "关闭 VAD 自动停播",
                );
              }
            }}
          />
        </div>
      ) : null}
      <div className="input-selection">
        <Field label="麦克风">
          {(control) => (
            <select {...control} value={input} onChange={(event) => setInput(event.target.value)} disabled={devices.length === 0}>
              <option value="">选择麦克风</option>
              {devices.map((item) => (
                <option key={item.index} value={item.index}>
                  {item.name}
                </option>
              ))}
            </select>
          )}
        </Field>
        <Field label="收听方式">
          {(control) => (
            <select {...control} value={mode} onChange={(event) => setMode(event.target.value)} disabled={modes.length === 0}>
              <option value="">选择收听方式</option>
              {modes.map((item) => (
                <option key={item} value={item}>
                  {item === "asr" ? "识别成文字" : "直接听原声"}
                </option>
              ))}
            </select>
          )}
        </Field>
      </div>
      {devices.length === 0 ? (
        <Unavailable>
          {device.state.devices_loaded === false
            ? "还没有读取麦克风列表。先点“刷新设备列表”，再选择麦克风。"
            : "没有找到可用的麦克风。"}
        </Unavailable>
      ) : null}
      {modes.length === 0 ? <Unavailable>当前配置没有可用的收听方式。</Unavailable> : null}
      <div className="actions">
        {device.controls
          .filter((control) => ALLOWED_CONTROLS.voice_input?.includes(control.key))
          .map((control) => {
            const isStart = control.key === "start" || control.key === "start_input";
            const supportedFields = control.fields.every((field) => field === "device" || field === "mode");
            const args: ObjectValue = {};
            if (control.fields.includes("device") && selectedDevice) args.device = selectedDevice.index;
            if (control.fields.includes("mode") && selectedMode) args.mode = selectedMode;
            const reason =
              !supportedFields || (isStart && !["device", "mode"].every((field) => control.fields.includes(field)))
                ? "插件没有声明麦克风或收听方式参数。"
                : isStart
                  ? device.state.state !== "stopped" && device.state.state !== "failed" && device.state.state !== "paused"
                    ? "已经在收音，请先停止。"
                    : selectedDevice === undefined || selectedMode === undefined
                      ? "先选择麦克风和收听方式。"
                      : selectedMode === "asr" && device.state.asr_ready !== true
                        ? "语音识别还没有配置可用的凭据。"
                        : selectedMode === "audio" && device.state.audio_ready !== true
                          ? "直接听原声的模型能力或媒体计量尚未就绪。"
                          : undefined
                  : undefined;
            return <QuickControl key={control.key} client={client} state={state} device={device} control={control} args={args} reason={reason} />;
          })}
      </div>
    </>
  );
}

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
        const response = await fetch(`/api/preview/${encodeURIComponent(device.instance_id)}`, {
          cache: "no-store",
          headers,
          signal: abort.signal,
        });
        if (!current) return;
        if (response.status === 304) {
          setError(null);
          return;
        }
        if (response.status === 204) {
          setEmpty(true);
          setError(null);
          setFrame(null);
          return;
        }
        if (!response.ok || !response.headers.get("Content-Type")?.startsWith("image/")) throw new Error("preview");
        const blob = await response.blob();
        if (!current) return;
        const raw = response.headers.get("X-Captured-At");
        const captured = raw !== null && Number.isFinite(Number(raw)) && Number(raw) >= 0 ? Number(raw) : null;
        setFrame({ url: URL.createObjectURL(blob), captured, etag: response.headers.get("ETag") });
        setEmpty(false);
        setError(null);
      } catch {
        if (current) setError("读取画面失败；下面的旧画面不代表当前桌面。");
      }
    };
    void load();
    return () => {
      current = false;
      abort.abort();
    };
  }, [device.instance_id, device.has_preview, online, refreshAt]);

  useEffect(
    () => () => {
      if (frame) URL.revokeObjectURL(frame.url);
    },
    [frame],
  );

  return (
    <section className="preview">
      <h3>最近的画面</h3>
      {frame ? (
        <>
          <img src={frame.url} alt="电脑最近一次截取的画面" />
          <p className="muted">
            截取于 {time(frame.captured)}
            {!online || error ? " · 旧画面，当前状态未确认" : ""}
          </p>
        </>
      ) : (
        <Unavailable>{empty ? "还没有截取过画面。" : "暂无可显示的画面。"}</Unavailable>
      )}
      {error ? <StateMark state="warning" label={error} /> : null}
    </section>
  );
}

export function Devices({
  client,
  state,
  selected,
  go,
}: {
  client: PanelClient;
  state: PanelState;
  selected: string | null;
  go: (route: Route) => void;
}): ReactElement {
  const activeTab: DevicesTab = selected === "studio" ? "studio" : selected === "live2d" ? "live2d" : "connections";
  const devices = state.snapshot?.devices ?? [];
  const liveId = state.online ? devices.find((device) => isLive(deviceMark(device).state))?.instance_id : undefined;

  const activeDecks = devices.filter(isActiveDeck);
  const passiveOrgans = devices.filter((d) => d.kind !== "phone" && !isActiveDeck(d));

  const switchTab = (next: DevicesTab): void => {
    go({ page: "devices", id: next === "connections" ? null : next });
  };

  const formatDeviceStatus = (device: Device) =>
    state.online
      ? deviceMark(device)
      : {
          state: "warning" as const,
          label: `${isSnapshotDelayed(state) ? "状态已过期 · 最近" : "连接中断 · 断开前"}${deviceMark(device).label}`,
        };

  return (
    <>
      <PageHeading eyebrow="BODY & LINK // 外部通信与内置感官" title="设备、声线与身体">
        主动交互通道独立为操控机架，无按钮的底层宿主插件收拢为机能芯片插槽；点选任意机架或芯片即可在侧栏核对实时回执。
      </PageHeading>

      <div className="timeline-tools">
        <fieldset className="toolbar">
          <legend className="visually-hidden">选择设备工作区</legend>
          {(
            [
              ["connections", "设备连接"],
              ["studio", "声线调音室"],
              ["live2d", "Live2D 表演"],
            ] as const
          ).map(([key, label]) => (
            <FilterPill
              key={key}
              group="devices-tab"
              value={key}
              label={label}
              checked={activeTab === key}
              onChange={() => switchTab(key)}
            />
          ))}
        </fieldset>
      </div>

      {activeTab === "studio" ? (
        <VoiceStudioSection client={client} />
      ) : activeTab === "live2d" ? (
        <Live2DStudioSection client={client} go={go} />
      ) : (
        <div className="devices-desk">
          <section className="open-section" aria-labelledby="active-decks-heading">
            <header className="editorial-header">
              <div className="editorial-title-group">
                <span className="big-num" aria-hidden="true">
                  01
                </span>
                <div>
                  <span className="eyebrow">ACTIVE CHANNELS // 可交互通道</span>
                  <h2 id="active-decks-heading">主动通信与外部身体机架</h2>
                </div>
              </div>
              <span className="eyebrow">{activeDecks.length + 1} 台操控机架</span>
            </header>

            <div className="object-grid deck-grid">
              <PhoneConnection
                client={client}
                online={state.online}
                target={devices.find((device) => device.kind === "phone")?.instance_id}
              />

              {activeDecks.map((device) => {
                const status = formatDeviceStatus(device);
                const note = deviceNote(device);
                const summaryLines = deviceSummary(device);
                const quickControls =
                  device.kind !== "voice_input"
                    ? device.controls.filter((control) => ALLOWED_CONTROLS[device.kind ?? ""]?.includes(control.key))
                    : [];

                return (
                  <article
                    className="channel-deck device-object"
                    key={device.instance_id}
                    data-accent={deckAccent(device.kind)}
                    aria-current={selected === device.instance_id ? "true" : undefined}
                  >
                    <div className="deck-body">
                      <div className="deck-top">
                        <div>
                          <span className="deck-code">{deckCode(device)}</span>
                          <h2 className="deck-title">{deviceName(device)}</h2>
                        </div>
                        <StateMark {...status} live={device.instance_id === liveId} />
                      </div>

                      {summaryLines.map((line) => (
                        <p key={line} className="deck-desc">
                          {line}
                        </p>
                      ))}

                      {quickControls.length > 0 || device.kind === "audio" || device.kind === "live2d" ? (
                        <div className="actions">
                          {quickControls.map((control) => (
                            <QuickControl
                              key={control.key}
                              client={client}
                              state={state}
                              device={device}
                              control={control}
                              reason={
                                control.fields.length > 0
                                  ? "这个操作需要页面暂不支持的参数。"
                                  : computerReason(device, control.key)
                              }
                            />
                          ))}
                          {device.kind === "audio" ? (
                            <Button variant="secondary" onClick={() => switchTab("studio")}>
                              打开调音室
                            </Button>
                          ) : null}
                          {device.kind === "live2d" ? (
                            <Button variant="secondary" onClick={() => switchTab("live2d")}>
                              调节姿态与口型
                            </Button>
                          ) : null}
                        </div>
                      ) : null}

                      {note !== null ? <p className="muted device-note">{note}</p> : null}
                    </div>

                    <div className="deck-actions">
                      <Button variant="ghost" onClick={() => go({ page: "devices", id: device.instance_id })}>
                        {device.kind === "voice_input" ? "设置收音" : "查看详情"}
                      </Button>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          {devices.length === 0 ? (
            <EmptyState
              icon="device"
              title="还没有启用外部设备插件"
              action={
                <Button onClick={() => go({ page: "system", id: null })}>
                  前往设置在 plugins 中启用设备
                </Button>
              }
            >
              你可以在设置页一键添加 QQ、收音麦克风、语音输出、电脑观察或 Live2D 插件，重启后将在此展示连接状态。
            </EmptyState>
          ) : passiveOrgans.length > 0 ? (
            <section className="open-section organs-rack" aria-labelledby="passive-organs-heading">
              <header className="editorial-header">
                <div className="editorial-title-group">
                  <span className="big-num" aria-hidden="true">
                    02
                  </span>
                  <div>
                    <span className="eyebrow">SYSTEM ORGANS // 内置感知与宿主插槽</span>
                    <h2 id="passive-organs-heading">底层能力芯片槽</h2>
                  </div>
                </div>
                <span className="eyebrow">点击任意芯片展开技术回执（{passiveOrgans.length} 枚）</span>
              </header>

              <div className="chip-grid object-grid-chips">
                {passiveOrgans.map((device) => {
                  const status = formatDeviceStatus(device);
                  const isSelected = selected === device.instance_id;
                  return (
                    <button
                      key={device.instance_id}
                      type="button"
                      className="organ-chip pc-focusable"
                      aria-current={isSelected ? "true" : undefined}
                      onClick={() => go({ page: "devices", id: device.instance_id })}
                    >
                      <StateMark {...status} live={device.instance_id === liveId} />
                      <span className="organ-chip-name">{deviceName(device)}</span>
                      <Icon name="chevron" />
                    </button>
                  );
                })}
              </div>
            </section>
          ) : null}
        </div>
      )}
    </>
  );
}

export function DeviceDetail({ client, state, device }: { client: PanelClient; state: PanelState; device: Device }): ReactElement {
  return (
    <div className="detail device-detail">
      <h2>{deviceName(device)}</h2>
      <StateMark {...(state.online ? deviceMark(device) : { state: "warning", label: "状态已过期，操作暂停" })} />
      {deviceSummary(device).map((line) => (
        <p key={line}>{line}</p>
      ))}
      {device.kind === "voice_input" ? <VoiceControls client={client} state={state} device={device} /> : null}
      {device.kind === "computer" && device.has_preview ? (
        <CachedPreview device={device} online={state.online} refreshAt={state.receivedAt} />
      ) : null}
      <TechDetails items={deviceTechnical(device)} />
    </div>
  );
}
