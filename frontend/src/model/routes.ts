/**
 * 页面路由：hash 形式的深链，浏览器前进/后退即页面层级的返回。
 * 路由只决定看什么，不触发查询以外的任何宿主操作。
 */
import type { IconName } from "@purrcept/cel";

export type Page = "home" | "attention" | "timeline" | "devices" | "activities" | "system";
export interface Route { page: Page; id: string | null }

/** 导航按主体概览、行动记录、连接与系统排列，同级页面立即切换。 */
export const PAGES: readonly { id: Page; label: string; icon: IconName; description: string }[] = [
  { id: "home", label: "首页", icon: "home", description: "她现在在做什么" },
  { id: "attention", label: "待处理", icon: "inbox", description: "需要你看一下的事" },
  { id: "timeline", label: "时间线", icon: "timeline", description: "最近发生了什么" },
  { id: "devices", label: "设备", icon: "device", description: "连接与感官" },
  { id: "activities", label: "活动", icon: "activity", description: "持续进行的目标" },
  { id: "system", label: "系统", icon: "settings", description: "运行状况与工具" },
];

/** 无法识别的 hash 回到首页，不抛错也不保留伪造的选中对象。 */
export function parseRoute(hash: string): Route {
  const [page, ...rest] = hash.replace(/^#\/?/, "").split("/");
  const known = PAGES.find((item) => item.id === page);
  if (known === undefined) return { page: "home", id: null };
  const raw = rest.join("/");
  if (raw === "") return { page: known.id, id: null };
  try {
    return { page: known.id, id: decodeURIComponent(raw) };
  } catch (error: unknown) {
    // 畸形百分号编码只能来自手工输入的地址；保留页面，丢弃无法解释的选择。
    if (!(error instanceof URIError)) throw error;
    return { page: known.id, id: null };
  }
}
/** 生成可分享的本机深链。 */
export function routeHref(route: Route): string {
  return route.id === null ? `#/${route.page}` : `#/${route.page}/${encodeURIComponent(route.id)}`;
}
