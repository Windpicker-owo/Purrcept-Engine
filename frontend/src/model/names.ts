/**
 * 把能力路径、插件类别与输入阶段翻译成用户可读的名称。
 * 只做命名，不推断状态；未知名称保持原文可见，不猜测成已知设备。
 */
import type { Device, Snapshot } from "../contracts";

/** 插件类别到用户名称；类别来自宿主快照，不由页面猜测。 */
export const DEVICE_NAMES: Readonly<Record<string, string>> = {
  qq: "QQ", voice_input: "收音", audio: "语音输出", computer: "电脑观察", live2d: "Live2D 身体",
};
/** 能力命名空间到用户名称；与插件类别分开，因为同一能力可能不属于设备。 */
const NAMESPACES: Readonly<Record<string, string>> = {
  ...DEVICE_NAMES,
  browser: "浏览器", notebook: "笔记", time: "时钟", clock: "时钟", skills: "技能", activity: "活动",
  activities: "活动", qzone: "QQ 空间", phone: "手机", view_image: "看图", rest: "休息", leave: "结束休息",
};
const STAGES: Readonly<Record<string, string>> = {
  attention: "正在判断要不要回应", deferred: "稍后再看", foreground: "等待回应", processing: "正在处理",
  processed: "已处理", failed: "处理失败",
};

/** 插件的显示名；未知类别显示实例 ID，便于在配置中对应。 */
export function deviceName(device: Device): string {
  return DEVICE_NAMES[device.kind ?? ""] ?? device.instance_id;
}
/** 能力路径（例如 me.qq.send_message）的所属对象名称。 */
export function capabilityName(path: string, snapshot: Snapshot | null): string {
  const parts = path.split(".");
  const namespace = parts[0] === "me" ? parts[1] : parts[0];
  if (namespace === undefined || namespace === "") return path;
  const device = snapshot?.devices.find((item) => item.instance_id === namespace);
  if (device !== undefined) return deviceName(device);
  return NAMESPACES[namespace] ?? namespace;
}
/** 一项行动涉及的对象名称，按首次出现顺序去重。 */
export function actionSubjects(paths: readonly string[], snapshot: Snapshot | null): string[] {
  const names: string[] = [];
  for (const path of paths) {
    const name = capabilityName(path, snapshot);
    if (!names.includes(name)) names.push(name);
  }
  return names;
}
/** 输入处理阶段的用户名称。 */
export function stageName(stage: string): string { return STAGES[stage] ?? stage; }
