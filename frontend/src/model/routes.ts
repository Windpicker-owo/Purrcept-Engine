/**
 * 页面路由：hash 形式的深链，浏览器前进/后退即页面层级的返回。
 * 涵盖真后台工作台（对话、记忆、配置与人设、设备与调音）以及行动审查与记录。
 */
import type { IconName } from "@purrcept/cel";

export type Page =
  | "home"
  | "chat"
  | "memory"
  | "devices"
  | "activities"
  | "timeline"
  | "reviews"
  | "attention"
  | "system";

export interface Route {
  page: Page;
  id: string | null;
}

export interface PageMeta {
  readonly id: Page;
  readonly label: string;
  readonly icon: IconName;
  readonly description: string;
  readonly rail?: boolean;
}

export const PAGES: readonly PageMeta[] = [
  { id: "home", label: "首页", icon: "home", description: "运行概览与快捷入口", rail: true },
  { id: "chat", label: "对话", icon: "person", description: "直接在网页与她聊天调试", rail: true },
  { id: "memory", label: "记忆", icon: "review", description: "浏览与编辑随笔、长期记忆与技能", rail: true },
  { id: "devices", label: "设备", icon: "device", description: "连接、声线调音室与身体表演", rail: true },
  { id: "activities", label: "活动", icon: "activity", description: "持续进行的目标", rail: true },
  { id: "timeline", label: "时间线", icon: "timeline", description: "近期行动与操作回执", rail: true },
  { id: "reviews", label: "行为审查", icon: "inbox", description: "查看当轮刺激与代码并留下评价", rail: true },
  { id: "attention", label: "待处理", icon: "triangle", description: "需要你留意或核对的事项", rail: false },
  { id: "system", label: "设置", icon: "settings", description: "人设、模型、插件配置与系统健康", rail: true },
];

export function parseRoute(hash: string): Route {
  const [page, ...rest] = hash.replace(/^#\/?/, "").split("/");
  const known = PAGES.find((item) => item.id === page);
  if (known === undefined) return { page: "home", id: null };
  const raw = rest.join("/");
  if (raw === "") return { page: known.id, id: null };
  try {
    return { page: known.id, id: decodeURIComponent(raw) };
  } catch (error: unknown) {
    if (!(error instanceof URIError)) throw error;
    return { page: known.id, id: null };
  }
}

export function routeHref(route: Route): string {
  return route.id === null ? `#/${route.page}` : `#/${route.page}/${encodeURIComponent(route.id)}`;
}
