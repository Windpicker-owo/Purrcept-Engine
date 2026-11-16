/** Phone 专用响应只在配对页内存中使用，不进入普通快照或控制身份存储。 */
import { ContractError } from "../contracts";

export interface PhoneSetup {
  available: boolean; config_name?: string; address?: string; port?: number; addresses?: string[]; message?: string;
}
export interface PhonePairing {
  status: "ready" | "closed" | "expired" | "paired" | "unavailable";
  name?: string; device_id?: string; url?: string; certificate_sha256?: string; expires_at?: number; qr_size?: number; qr_path?: string;
}

/** 边界校验不接受 HTML；二维码只允许后端生成的有界整数格路径。 */
export function phonePairing(value: unknown): PhonePairing {
  if (typeof value !== "object" || value === null || !("status" in value)) throw new ContractError("phone.pairing");
  const p = value as Record<string, unknown>;
  if (!["ready", "closed", "expired", "paired", "unavailable"].includes(String(p.status))) throw new ContractError("phone.status");
  if (p.status === "ready" && (typeof p.url !== "string" || !p.url.startsWith("https://")
    || typeof p.certificate_sha256 !== "string" || !/^[a-f0-9]{64}$/i.test(p.certificate_sha256)
    || typeof p.expires_at !== "number" || !Number.isFinite(p.expires_at)
    || typeof p.qr_size !== "number" || !Number.isInteger(p.qr_size) || p.qr_size < 21 || p.qr_size > 200
    || typeof p.qr_path !== "string" || p.qr_path.length > 400000 || !/^(M\d{1,3} \d{1,3}h1v1h-1z)+$/.test(p.qr_path))) throw new ContractError("phone.qr");
  if (p.name !== undefined && typeof p.name !== "string") throw new ContractError("phone.name");
  if (p.status === "paired" && (typeof p.device_id !== "string" || p.device_id.length < 1 || p.device_id.length > 128)) throw new ContractError("phone.device_id");
  return p as unknown as PhonePairing;
}

/** 设置响应只含公开连接字段，缺失字段不能被猜成已保存。 */
export function phoneSetup(value: unknown): PhoneSetup {
  if (typeof value !== "object" || value === null || !("available" in value) || typeof value.available !== "boolean") throw new ContractError("phone.setup");
  const p = value as Record<string, unknown>;
  if (p.available && (typeof p.config_name !== "string" || typeof p.address !== "string"
    || typeof p.port !== "number" || !Number.isInteger(p.port) || !Array.isArray(p.addresses)
    || !p.addresses.every((item) => typeof item === "string"))) throw new ContractError("phone.settings");
  return p as unknown as PhoneSetup;
}
