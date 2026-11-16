/** 专用配对边界只接受可绘制 QR 格子，不能把错误响应或 HTML 当成成功。 */
import { expect, test } from "vitest";
import { phonePairing, phoneSetup } from "../src/model/phone";

test("有效邀请与已配对状态分别呈现，拒绝 HTML 和非有限期限", () => {
  const ready = { status: "ready", url: "https://192.0.2.10:8766", certificate_sha256: "a".repeat(64), expires_at: 12345, qr_size: 29, qr_path: "M4 4h1v1h-1z" };
  expect(phonePairing(ready).status).toBe("ready");
  expect(phonePairing({ status: "paired", name: "测试手机", device_id: "old-device" })).toEqual({ status: "paired", name: "测试手机", device_id: "old-device" });
  expect(() => phonePairing({ ...ready, qr_path: "<svg onload=alert(1)>" })).toThrow();
  expect(() => phonePairing({ ...ready, expires_at: Infinity })).toThrow();
  expect(() => phonePairing({ status: "success" })).toThrow();
  expect(() => phonePairing({ status: "paired", name: "旧手机" })).toThrow();
});

test("嵌入式配置不可用与可编辑的完整设置分别校验", () => {
  expect(phoneSetup({ available: false, message: "未提供配置文件" }).available).toBe(false);
  expect(phoneSetup({ available: true, config_name: "purrcept.toml", address: "192.0.2.10", port: 8766, addresses: ["192.0.2.10"] }).port).toBe(8766);
  expect(() => phoneSetup({ available: true })).toThrow();
  expect(() => phoneSetup({ available: true, config_name: "purrcept.toml", address: "", port: 8766, addresses: [null] })).toThrow();
});
