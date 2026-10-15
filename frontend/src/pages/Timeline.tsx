/**
 * 时间线：她的行动与我的操作按时间倒序排列，过滤工具带切换范围。
 * 数据表承担扫描节奏：表头与数据之间是唯一的分界线，行本身只靠高度与悬停底色区分；
 * 选择一行在 Inspector（窄屏为 Sheet）中展开详情，路由保留选择。
 */
import { useState } from "react";
import type { ReactElement } from "react";
import { Button, Icon, isLive } from "@purrcept/cel";
import type { PanelState } from "../client";
import type { Route } from "../model/routes";
import { FILTERS, timelineItems } from "../model/timeline";
import type { TimelineFilter } from "../model/timeline";
import { EmptyNote, PageHeading, relative, StateMark, time } from "../ui/common";

/**
 * 按筛选条件展示当前行动与本页控制时间线。
 *
 * state 提供原事实，selected 定位详情，go 更新深链；筛选不重新执行操作。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Timeline({ state, selected, go }: { state: PanelState; selected: string | null; go: (route: Route) => void }): ReactElement {
  const [filter, setFilter] = useState<TimelineFilter>("all");
  const [query, setQuery] = useState("");
  const filtered = timelineItems(state.snapshot, state.entries, filter);
  const term = query.trim().toLocaleLowerCase();
  const items = filtered.filter((item) => `${item.title} ${item.status?.label ?? ""}`.toLocaleLowerCase().includes(term));
  const liveId = items.find((item) => item.status !== null && isLive(item.status.state))?.id;
  const now = Date.now();
  return <>
    <PageHeading title="时间线">她最近做了什么，以及你在这个页面发起的操作。结果未知的操作不会自动重做。</PageHeading>
    <div className="timeline-tools">
      <fieldset className="toolbar"><legend className="visually-hidden">筛选记录</legend>
        {FILTERS.map((item) => <label key={item.id} className="filter" data-selected={filter === item.id}>
          <input type="radio" name="timeline-filter" value={item.id} checked={filter === item.id} onChange={() => setFilter(item.id)} />{item.label}
        </label>)}
      </fieldset>
      <label className="search-field"><span>搜索记录</span><input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="按内容或结果搜索" /></label>
    </div>
    <div className="result-summary"><span role="status">{items.length} 条记录{term ? ` · 搜索“${query.trim()}”` : ""}</span>
      {filter !== "all" || term ? <Button variant="ghost" onClick={() => { setFilter("all"); setQuery(""); }}>清除筛选</Button> : null}
    </div>
    {items.length === 0 ? <EmptyNote icon="timeline">{filter === "all" && !term ? "还没有记录。她开始做事或你发起操作后会出现在这里。" : "没有符合条件的记录。试试其他关键词，或清除筛选。"}</EmptyNote>
      : <div className="data-table">
        <div className="data-head" aria-hidden="true"><span>内容</span><span>时间</span><span>结果</span></div>
        {items.map((item) => <button key={item.id} className="data-row pc-focusable" aria-current={selected === item.id ? "true" : undefined}
          onClick={() => go({ page: "timeline", id: item.id })}>
          <span className="data-main">
            <span className="item-icon" aria-hidden="true"><Icon name={item.kind === "action" ? "timeline" : "settings"} /></span>
            <span className="item-text"><span className="item-title">{item.title}</span><span className="item-subtitle">{item.kind === "action" ? `${item.action.calls.length} 项操作` : "我的操作"}</span></span>
          </span>
          <span className="data-time" title={time(item.at)}>{relative(item.at, now)}</span>
          <span className="data-status">
            {item.status ? <StateMark {...item.status} live={item.id === liveId} /> : <StateMark state="waiting-external" label="等待回应" />}
            {item.needsCheck ? <StateMark state="warning" label="需要核对" /> : null}
          </span>
        </button>)}
      </div>}
    <p className="page-note">“我的操作”目前只保存在这个浏览器标签页中。</p>
  </>;
}
