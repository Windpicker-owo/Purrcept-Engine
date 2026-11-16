/**
 * 导航按布局能力在桌面轨道和手机五项标签栏之间切换。
 * 更多入口复用 Cel Sheet；路由仍由 App 持有。
 */
import { useEffect, useState, type ReactElement } from "react";
import { motion } from "motion/react";
import { breakpoints, Icon, Sheet, transitions } from "@purrcept/cel";
import { PAGES, routeHref, type Page } from "../model/routes";
import { BrandMark } from "./common";

const MOBILE_PAGES: readonly Page[] = ["home", "chat", "memory", "devices"];

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

export function Navigation({ page, attentionCount }: { page: Page; attentionCount: number }): ReactElement {
  const compact = useMediaQuery(breakpoints.navigation);
  const [moreOpen, setMoreOpen] = useState(false);
  useEffect(() => setMoreOpen(false), [page, compact]);
  const railPages = PAGES.filter((item) => item.rail !== false);
  const items = compact ? railPages.filter((item) => MOBILE_PAGES.includes(item.id)) : railPages;
  const secondary = !MOBILE_PAGES.includes(page);

  return (
    <>
      <nav className="navigation" aria-label="主导航">
        <a className="wordmark pc-focusable" href="#/home" aria-label="Purrcept 首页">
          <BrandMark />
        </a>
        {items.map((item) => {
          const active = page === item.id || (item.id === "home" && page === "attention");
          return (
            <a
              key={item.id}
              className="nav-item pc-focusable"
              href={routeHref({ page: item.id, id: null })}
              title={`${item.label} · ${item.description}`}
              aria-current={active ? "page" : undefined}
            >
              {active ? <motion.span layoutId="nav-current" className="nav-indicator" transition={transitions.move} aria-hidden="true" /> : null}
              <Icon name={item.icon} />
              <span>{compact && item.id === "timeline" ? "记录" : item.label}</span>
              {item.id === "home" && attentionCount > 0 ? (
                <span className="nav-count" aria-label={`${attentionCount} 项待处理`}>
                  {attentionCount}
                </span>
              ) : null}
            </a>
          );
        })}
        {compact ? (
          <button
            type="button"
            className="nav-item pc-focusable"
            aria-label="更多页面"
            aria-expanded={moreOpen}
            aria-haspopup="dialog"
            data-current={secondary}
            onClick={() => setMoreOpen(true)}
          >
            {secondary ? <motion.span layoutId="nav-current" className="nav-indicator" transition={transitions.move} aria-hidden="true" /> : null}
            <Icon name="more" />
            <span>更多</span>
          </button>
        ) : null}
      </nav>
      <Sheet open={moreOpen} onOpenChange={setMoreOpen} title="更多页面" description="配置与人设、行动审查、时间线与持续目标。">
        <nav className="more-pages" aria-label="更多页面">
          {PAGES.filter((item) => !MOBILE_PAGES.includes(item.id)).map((item) => (
            <a
              key={item.id}
              className="more-page pc-focusable"
              href={routeHref({ page: item.id, id: null })}
              onClick={() => setMoreOpen(false)}
            >
              <Icon name={item.icon} />
              <span>
                <strong>{item.label}</strong>
                <span>{item.description}</span>
              </span>
              <Icon name="chevron" />
            </a>
          ))}
        </nav>
      </Sheet>
    </>
  );
}
