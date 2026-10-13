/**
 * 活动：持续目标的状态、剩余额度、最近进展与暂停/恢复/结束。
 * 暂停与结束先撤销新步骤资格，再等待实际收尾；恢复沿用原余额，不追加额度。
 * 中断后待核对的活动不能用普通恢复绕过主体核对。
 */
import type { ReactElement } from "react";
import { Button, Feedback, isLive } from "@purrcept/cel";
import type { PanelClient, PanelState } from "../client";
import { activityReason, mark } from "../semantics";
import { targetReason } from "../model/controls";
import type { Route } from "../model/routes";
import { duration, EmptyState, PageHeading, StateMark, Unavailable } from "../ui/common";

const OPERATIONS = { pause: ["暂停", "暂停活动"], resume: ["恢复", "恢复活动"], end: ["结束", "结束活动"] } as const;

export function Activities({ client, state, selected, go }: { client: PanelClient; state: PanelState; selected: string | null; go: (route: Route) => void }): ReactElement {
  const activities = state.snapshot?.activities ?? [];
  const liveId = activities.find((activity) => isLive(mark(activity.state).state))?.activity_id;
  return <>
    <PageHeading title="活动">她在持续推进的目标。暂停和结束会等她把手头的步骤收好；恢复不会追加运行额度。</PageHeading>
    {activities.length === 0 ? <EmptyState icon="activity" title="没有活动">她开始一项持续的目标时会出现在这里。</EmptyState>
      : <div className="object-grid">{activities.map((activity) => <article className="activity-object" key={activity.activity_id} aria-current={selected === activity.activity_id ? "true" : undefined}>
        <div className="object-header"><h2>{activity.goal}</h2><StateMark {...mark(activity.state)} live={activity.activity_id === liveId} /></div>
        <p className="lead">还剩 {duration(activity.remaining_seconds)}</p>
        {activity.timing_incomplete ? <p className="muted">上次异常中断，最后一段时间没有计入。</p> : null}
        {activity.needs_reconciliation ? <Feedback state="waiting-external" scope="object" urgency="passive" persistence="until-resolved" title="中断后待核对">她会先核对上次的实际结果，再决定是否继续。</Feedback> : null}
        {activity.progress_preview ? <><h3>最近进展</h3><p className="excerpt">{activity.progress_preview.text}{activity.progress_preview.truncated ? `\n…（仅预览，原文 ${activity.progress_preview.original_chars} 字）` : ""}</p></> : <Unavailable>还没有记录进展。</Unavailable>}
        {activity.last_command && activity.last_command.status !== "confirmed" ? <StateMark {...mark(activity.last_command.status)} /> : null}
        {activity.last_command?.error ? <p>{activity.last_command.error.text}{activity.last_command.error.truncated ? " …（已截断）" : ""}</p> : null}
        <div className="actions">
          {(["pause", "resume", "end"] as const).map((operation) => <Button variant="secondary" key={operation}
            disabledReason={targetReason(state, `activity:${activity.activity_id}`, operation) ?? activityReason(activity, operation)}
            onClick={() => void client.submit(`activity:${activity.activity_id}`, operation, String(activity.version), {}, `${OPERATIONS[operation][1]}：${activity.goal}`)}>{OPERATIONS[operation][0]}</Button>)}
          <Button variant="ghost" onClick={() => go({ page: "activities", id: activity.activity_id })}>查看详情</Button>
        </div>
      </article>)}</div>}
  </>;
}
