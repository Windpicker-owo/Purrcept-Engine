/**
 * 导航按布局能力在桌面七项轨道和手机五项标签栏之间切换。
 * 更多入口复用 Cel Sheet；路由仍由 App 持有，导航不发起业务控制。
 */
import { useEffect, useState, type ReactElement } from "react";
import { motion } from "motion/react";
import { breakpoints, Icon, Sheet, transitions } from "@purrcept/cel";
import { PAGES, routeHref, type Page } from "../model/routes";
import { BrandMark } from "./common";

const MOBILE_PAGES: readonly Page[] = ["home", "attention", "timeline", "devices"];

/** 监听同源媒体查询，卸载时归还监听器；不改变任何业务或路由状态。 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const media = window.matchMedia(query);
    const changed = (): void => setMatches(media.matches);
    changed();
    media.addEventListener("change", changed);
    return () => media.removeEventListener("change", changed);
  }, [query]);
  return matches;
}

/** 当前项与待处理数量来自共享事实；手机低频目的地通过具名列表进入。 */
export function Navigation({ page, attentionCount }: { page: Page; attentionCount: number }): ReactElement {
  const compact = useMediaQuery(breakpoints.navigation);
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => setMoreOpen(false), [page, compact]);
  const items = compact ? PAGES.filter((item) => MOBILE_PAGES.includes(item.id)) : PAGES;
  const secondary = !MOBILE_PAGES.includes(page);
  return <>
    <nav className="navigation" aria-label="主导航">
      <a className="wordmark pc-focusable" href="#/home" aria-label="Purrcept 首页"><BrandMark /></a>
      {items.map((item) => <a key={item.id} className="nav-item pc-focusable" href={routeHref({ page: item.id, id: null })}
        title={`${item.label} · ${item.description}`} aria-current={page === item.id ? "page" : undefined}>
        {page === item.id ? <motion.span layoutId="nav-current" className="nav-indicator" transition={transitions.move} aria-hidden="true" /> : null}
        <Icon name={item.icon} /><span>{compact && item.id === "timeline" ? "记录" : item.label}</span>
        {item.id === "attention" && attentionCount > 0 ? <span className="nav-count" aria-label={`${attentionCount} 项待处理`}>{attentionCount}</span> : null}
      </a>)}
      {compact ? <button type="button" className="nav-item pc-focusable" aria-label="更多页面" aria-expanded={moreOpen}
        aria-haspopup="dialog" data-current={secondary} onClick={() => setMoreOpen(true)}>
        {secondary ? <motion.span layoutId="nav-current" className="nav-indicator" transition={transitions.move} aria-hidden="true" /> : null}
        <Icon name="more" /><span>更多</span>
      </button> : null}
    </nav>
    <Sheet open={moreOpen} onOpenChange={setMoreOpen} title="更多页面" description="行为审查、持续活动与系统工具。">
      <nav className="more-pages" aria-label="更多页面">{PAGES.filter((item) => !MOBILE_PAGES.includes(item.id)).map((item) =>
        <a key={item.id} className="more-page pc-focusable" href={routeHref({ page: item.id, id: null })} onClick={() => setMoreOpen(false)}>
          <Icon name={item.icon} /><span><strong>{item.label}</strong><span>{item.description}</span></span><Icon name="chevron" />
        </a>)}
      </nav>
    </Sheet>
  </>;
}
