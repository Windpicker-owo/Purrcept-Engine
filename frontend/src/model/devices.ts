/**
 * 设备展示注册表：每种插件声明人话摘要、允许的快捷控制与完整技术事实。
 * 摘要只翻译已提供的字段，缺失时不出现；全部原始字段保留在技术细节中。
 * 快捷控制范围来自 contract §7，不按模型能力目录生成任意按钮。
 */
import type { Device, Json } from "../contracts";

/** 每类插件允许在面板上直接发起的控制。 */
export const ALLOWED_CONTROLS: Readonly<Record<string, readonly string[]>> = {
  qq: ["reconnect"], voice_input: ["devices", "start", "stop"], audio: ["stop_output"],
  computer: ["connect", "disconnect", "observe_start", "observe_pause"], live2d: [],
};

function time(value: number): string { return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false }); }
function count(value: Json | undefined): number | undefined {
  if (typeof value === "number") return value;
  if (Array.isArray(value)) return value.length;
  if (typeof value === "object" && value !== null) return Object.keys(value).length;
  return undefined;
}

/**
 * 从已提供字段构造设备简要事实。
 *
 * @param device 宿主实际装配的设备及其已校验状态。
 * @returns 当前可确认的摘要行，缺失字段不补造为成功或离线。
 */
export function deviceSummary(device: Device): string[] {
  const s = device.state;
  const lines: string[] = [];
  switch (device.kind) {
    case "qq": {
      if (s.mode === "reverse") lines.push("等待 QQ 端主动连入");
      else if (s.mode !== undefined) lines.push("由 Engine 主动连接 QQ 端");
      const pending = count(s.pending_calls);
      if (pending !== undefined && pending > 0) lines.push(`${pending} 个请求正在等 QQ 回应`);
      if (typeof s.reason === "string" && s.reason !== "") lines.push(s.reason);
      break;
    }
    case "voice_input": {
      const inputs = Array.isArray(s.devices) ? s.devices : [];
      const selected = inputs.find((item) => typeof item === "object" && item !== null && !Array.isArray(item) && item.index === s.device);
      if (selected && typeof selected === "object" && !Array.isArray(selected) && typeof selected.name === "string") lines.push(`收音设备：${selected.name}`);
      if (s.mode === "asr") lines.push("把听到的话识别成文字");
      else if (s.mode === "audio") lines.push("把原声直接交给她");
      if (typeof s.partial === "string" && s.partial !== "") lines.push(`正在识别：“${s.partial}”`);
      const latest = s.latest_input;
      if (typeof latest === "object" && latest !== null && !Array.isArray(latest)) {
        const stages: Readonly<Record<string, string>> = {
          attention: "最近一句已收到，等待判断", deferred: "最近一句已收到，暂缓处理",
          foreground: "最近一句正在等待处理", processing: "正在处理最近一句",
          processed: "最近一句已处理；是否发声以语音输出状态为准",
          failed: "最近一句处理失败，不能视为已经回应",
        };
        lines.push(typeof latest.processing_stage === "string" ? stages[latest.processing_stage] ?? "最近一句的处理结果尚未取得" : "最近一句的处理结果尚未取得");
      }
      if (s.audio_ready === false) lines.push("直接听原声尚未就绪，请选择“识别成文字”");
      break;
    }
    case "audio": {
      const active = s.active;
      if (typeof active === "object" && active !== null && !Array.isArray(active)) {
        lines.push(active.started === true ? "播放器已开始输出" : "正在准备语音，尚未开始播放");
      } else if (s.last === null) lines.push("本次运行尚无语音输出结果");
      const queued = count(s.queued);
      if (queued !== undefined && queued > 0) lines.push(`还有 ${queued} 段等待播放`);
      break;
    }
    case "computer": {
      if (s.connected === true) lines.push(s.observing === true ? "正在持续观察屏幕" : "已连接，未在观察");
      if (typeof s.foreground === "string" && s.foreground !== "") lines.push(`当前窗口：${s.foreground}`);
      if (typeof s.captured_at === "number") lines.push(`最近画面：${time(s.captured_at)}`);
      break;
    }
    case "live2d": {
      if (s.adopted === true) lines.push("身体正在跟随语音与表情");
      break;
    }
    default: break;
  }
  if (typeof s.error === "string" && s.error !== "") lines.push(`故障：${s.error}`);
  return lines;
}

/**
 * 给出当前设备类别的操作边界说明。
 *
 * @param device 宿主实际装配的设备及其已校验状态。
 * @returns 已知类别的一句使用提示；无对应说明时为 null。
 */
export function deviceNote(device: Device): string | null {
  switch (device.kind) {
    case "qq": return "重新连接不会重发旧消息或结果未知的发送。";
    case "audio": return "停止语音输出会同时取消正在说和排队等待的语音；之后的新语音仍会正常播放。";
    case "voice_input": return "开启收音只表示麦克风正在采集；收到、处理和语音回复是不同阶段。";
    case "live2d": return "这里只显示身体状态，不提供控制。";
    case "computer": return "查看画面只读取最近一次的截图，不会额外截屏。";
    default: return null;
  }
}

/**
 * 保留设备完整技术事实供 Inspector 核对。
 *
 * @param device 宿主实际装配的设备及其已校验状态。
 * @returns 具名关键字段及未消费的原始 JSON 字段，不改写权威状态。
 */
export function deviceTechnical(device: Device): [string, Json | undefined][] {
  const s = device.state;
  const known: [string, Json | undefined][] = (() => {
    switch (device.kind) {
      case "qq": return [["连接方式", s.mode], ["恢复尝试", s.attempts], ["连接代次", s.generation], ["原因", s.reason], ["等待真实响应", s.pending_calls], ["迟到结果待核对", s.pending_late_results], ["未接管缺口", s.gap_counts]];
      case "voice_input": return [["输入模式", s.mode], ["输入设备索引", s.device], ["收音会话", s.session_id], ["当前片段", s.utterance_id], ["识别状态", s.recognition_state], ["识别中的文字", s.partial], ["最近发言处理", s.latest_input], ["待交付片段", s.pending_utterances], ["输入电平", s.level], ["故障", s.error]];
      case "audio": return [["当前播放", s.active], ["候播数量", count(s.queued)], ["候播上限", s.max_pending], ["最近真实结果", s.last], ["故障", s.error]];
      case "computer": return [["连接", s.connected], ["连续观察", s.observing], ["观察代次", s.generation], ["当前窗口", s.foreground], ["等待主体处理的输入", s.input_id], ["最新候选画面", s.candidate], ["采集时间", typeof s.captured_at === "number" ? time(s.captured_at) : undefined], ["故障", s.error]];
      case "live2d": return [["播放关联", s.playback_id], ["姿态意图", s.intent_id], ["已经采用", s.adopted], ["播放进度（秒）", s.elapsed_seconds], ["故障", s.error]];
      default: return [["状态", s.state], ["原因", s.reason], ["故障", s.error]];
    }
  })();
  return [["实例", device.instance_id], ["类别", device.kind], ["控制版本", device.revision], ["状态值", s.state], ...known];
}

/**
 * 检查当前电脑观察控制是否满足页面条件。
 *
 * @param device 实际设备状态，只有 computer 类型采用此规则。
 * @param operation 明确的连接、断开、开始观察或暂停操作。
 * @returns 拒绝操作的原因；undefined 表示本规则不阻止，宿主仍会核验。
 */
export function computerReason(device: Device, operation: string): string | undefined {
  if (device.kind !== "computer") return undefined;
  if (typeof device.state.connected !== "boolean" || typeof device.state.observing !== "boolean") return "电脑连接或观察状态未提供。";
  if (operation === "connect" && device.state.connected) return "电脑已经连接。";
  if (operation !== "connect" && !device.state.connected) return "请先连接电脑。";
  if (operation === "observe_start" && device.state.observing) return "已经在观察。";
  if (operation === "observe_pause" && !device.state.observing) return "观察已经暂停。";
  return undefined;
}
