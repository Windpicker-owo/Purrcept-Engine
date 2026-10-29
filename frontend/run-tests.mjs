/** Windows 沙箱的工作进程共用明确的 Engine 临时目录，不读写 Design 工具链。 */
import { spawn } from "node:child_process";
import { mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const temporary = fileURLToPath(new URL("./.test-tmp", import.meta.url));
await mkdir(temporary, { recursive: true });
const runner = fileURLToPath(new URL("./node_modules/vitest/vitest.mjs", import.meta.url));
const child = spawn(process.execPath, [runner, "run", "--config", "vitest.config.mjs"], {
  cwd: fileURLToPath(new URL(".", import.meta.url)),
  env: { ...process.env, TEMP: temporary, TMP: temporary },
  stdio: "inherit",
});
child.on("error", (error) => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
child.on("exit", (code) => { process.exitCode = code ?? 1; });
