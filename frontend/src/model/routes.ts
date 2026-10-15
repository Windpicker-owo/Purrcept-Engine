/**
 * 页面路由：hash 形式的深链，浏览器前进/后退即页面层级的返回。
 * 路由只决定看什么，不触发查询以外的任何宿主操作。
 */
import type { IconName } from "@purrcept/cel";

export type Page = "home" | "attention" | "timeline" | "devices" | "activities" | "system";
/** 已知页面和可选对象身份，仅定位观察内容，不附带业务执行资格。 */
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

/**
 * 将本机 hash 解析为页面与可选选择身份。
 *
 * @param hash 包含或省略 # 前缀的浏览器 location.hash。
 * @returns 已知页面及解码后的选择身份；未知页面回首页，坏编码仅清除选择。
 */
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
/**
 * 为当前页面和选择生成本机深链。
 *
 * @param route 已知页面及可选对象身份；null 身份只定位页面。
 * @returns 对象身份经 URI 编码的 hash 链接，不触发宿主操作。
 */
export function routeHref(route: Route): string {
  return route.id === null ? `#/${route.page}` : `#/${route.page}/${encodeURIComponent(route.id)}`;
}
