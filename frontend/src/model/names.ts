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

/**
 * 将已装配设备映射为用户可识别名称。
 *
 * @param device 含实例身份与插件类别的设备声明。
 * @returns 已知类别的人话名称；未知类别保留实例身份。
 */
export function deviceName(device: Device): string {
  return DEVICE_NAMES[device.kind ?? ""] ?? device.instance_id;
}
/**
 * 从完整能力路径确定操作所属对象的名称。
 *
 * @param path 实际调用记录中的完整能力路径。
 * @param snapshot 用于识别可配置插件实例的当前快照；null 仍可解析已知命名空间。
 * @returns 当前实例名称、已知命名空间名称或未识别的原始命名空间。
 */
export function capabilityName(path: string, snapshot: Snapshot | null): string {
  const parts = path.split(".");
  const namespace = parts[0] === "me" ? parts[1] : parts[0];
  if (namespace === undefined || namespace === "") return path;
  const device = snapshot?.devices.find((item) => item.instance_id === namespace);
  if (device !== undefined) return deviceName(device);
  return NAMESPACES[namespace] ?? namespace;
}
/**
 * 按调用顺序汇总一次行动涉及的对象。
 *
 * @param paths 行动实际调用路径，顺序与真实回执一致。
 * @param snapshot 解析可配置实例名称所需的当前快照。
 * @returns 按首次出现顺序去重的对象名称列表。
 */
export function actionSubjects(paths: readonly string[], snapshot: Snapshot | null): string[] {
  const names: string[] = [];
  for (const path of paths) {
    const name = capabilityName(path, snapshot);
    if (!names.includes(name)) names.push(name);
  }
  return names;
}
/**
 * 将输入处置阶段转换为用户文案。
 *
 * @param stage 宿主实际提供的输入处理阶段。
 * @returns 已知阶段名称；未知阶段保留原值。
 */
export function stageName(stage: string): string { return STAGES[stage] ?? stage; }
