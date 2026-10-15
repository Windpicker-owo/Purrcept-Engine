/**
 * 待处理：全部 until-resolved 事项的唯一汇总，每项说明发生了什么、影响和下一步。
 * 事项从快照推导，条件消失即退出；页面不提供“全部忽略”之类会掩盖事实的操作。
 */
import type { ReactElement } from "react";
import { Button, Feedback } from "@purrcept/cel";
import type { AttentionItem } from "../model/attention";
import type { Route } from "../model/routes";
import { EmptyState, PageHeading } from "../ui/common";

/**
 * 展示当前仍有事实依据的待处理事项。
 *
 * items 是共享推导结果，go 用于进入对应详情，不确认或删除权威待办。
 *
 * @returns 仅呈现当前输入事实的 React 元素。
 */
export function Attention({ items, go }: { items: readonly AttentionItem[]; go: (route: Route) => void }): ReactElement {
  return <>
    <PageHeading title="待处理">这里汇总需要你看一下的事。问题解决后会自动消失。</PageHeading>
    {items.length === 0 ? <EmptyState title="一切正常" action={<Button variant="secondary" onClick={() => go({ page: "home", id: null })}>回到首页</Button>}>
      设备故障、结果未知的操作和中断的活动会出现在这里。
    </EmptyState> : <div className="attention-list">
      {items.map((item) => <Feedback key={item.id} state={item.state} scope="object" urgency="actionable" persistence="until-resolved"
        title={item.title} actions={<Button variant="secondary" onClick={() => go(item.route)}>{item.next}</Button>}>
        {item.impact}
      </Feedback>)}
    </div>}
  </>;
}
