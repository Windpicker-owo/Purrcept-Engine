/** 产品静态入口只装配页面观察者；没有设备启动或模型调用的生命周期。 */
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { PanelClient } from "./client";
import "./styles.css";

const element = document.getElementById("root");
if (element === null) throw new Error("面板入口缺失。");
// 浏览器可拒绝 sessionStorage 的 getter；让客户端边界显示保存故障，不能因此白屏。
const storage = {
  getItem: (key: string): string | null => window.sessionStorage.getItem(key),
  setItem: (key: string, value: string): void => window.sessionStorage.setItem(key, value),
};
const client = new PanelClient((path, options) => fetch(path, options), storage, () => crypto.randomUUID());
createRoot(element).render(<App client={client} />);
