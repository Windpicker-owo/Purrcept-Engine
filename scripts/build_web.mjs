/**
 * 将产品源码与真正的 Cel 参考组件编译进 Engine 的有限静态资源。
 * Design 只作为开发期源码/Token/冻结工具链来源；不会改写它或让安装包依赖兄弟目录。
 * 固定资源名与 app/web.py 白名单对应，单个脚本包避免打开任意静态目录。
 */
import { copyFile, readFile, writeFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const engine = fileURLToPath(new URL("../", import.meta.url));
const design = path.resolve(engine, "../purrcept_design");
const dependencies = path.join(design, "react/node_modules");
const { build } = await import(pathToFileURL(path.join(dependencies, "vite/dist/node/index.js")).href);
const output = path.join(engine, "src/purrcept_engine/app/static");

await build({
  configFile: false,
  root: path.join(engine, "frontend"),
  base: "/",
  resolve: {
    alias: [
      { find: "@purrcept/cel", replacement: path.join(design, "react/src/index.ts") },
      { find: /^motion(\/.*)?$/, replacement: path.join(dependencies, "motion") + "$1" },
      { find: /^react-dom(\/.*)?$/, replacement: path.join(dependencies, "react-dom") + "$1" },
      { find: /^react(\/.*)?$/, replacement: path.join(dependencies, "react") + "$1" },
    ],
  },
  build: {
    outDir: output,
    emptyOutDir: true,
    cssCodeSplit: false,
    rolldownOptions: {
      // 这是完整浏览器客户端，依赖的 React Server Component 标记不影响其语义。
      // 只过滤这一已知提示；其它构建诊断仍正常输出并阻止真实错误被掩盖。
      onLog(level, log, defaultHandler) {
        if (log.code === "MODULE_LEVEL_DIRECTIVE" && log.message.includes("use client")) return;
        defaultHandler(level, log);
      },
      output: {
        entryFileNames: "panel.js",
        codeSplitting: false,
        assetFileNames: "panel.css",
      },
    },
  },
});

// 产物中的 Token 保持与 Design 完全同源；源码样式不定义第二套 Token。
await copyFile(path.join(design, "dist/purrcept.css"), path.join(output, "purrcept.css"));
const indexPath = path.join(output, "index.html");
const html = await readFile(indexPath, "utf8");
await writeFile(indexPath, html.replaceAll('"/panel.', '"/assets/panel.'));

// 构建器保留依赖中的CSS模板空白；当前产物的纯空白行只在该模板中，
// 移除行上的水平空白不改变CSS规则，也让静态产物通过仓库diff检查。
const scriptPath = path.join(output, "panel.js");
const script = await readFile(scriptPath, "utf8");
await writeFile(scriptPath, script.replace(/^[\t ]+$/gm, ""));
