/** 前端边界测试使用 Design 已冻结的工具链，运行时不借用真实设备。 */
import { fileURLToPath } from "node:url";
export default {
  root: fileURLToPath(new URL(".", import.meta.url)),
  test: { include: ["tests/**/*.test.ts"], environment: "node" },
};
