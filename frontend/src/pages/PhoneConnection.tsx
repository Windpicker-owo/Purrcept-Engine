/** 手机连接机架卡片：紧凑融入主动通信机架网格，仅在展开二维码或地址设置时自动拓宽。 */
import { useEffect, useRef, useState, type ReactElement } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Button, Dialog, Field, transitions } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type { PhonePairing, PhoneSetup } from "../model/phone";
import { StateMark } from "../ui/common";

export function PhoneConnection({
  client,
  online,
  target,
}: {
  client: PanelClient;
  online: boolean;
  target: string | undefined;
}): ReactElement {
  const [pairing, setPairing] = useState<PhonePairing | null>(null);
  const [settings, setSettings] = useState<PhoneSetup | null>(null);
  const [address, setAddress] = useState("");
  const [port, setPort] = useState("8766");
  const [editing, setEditing] = useState(false);
  const [preview, setPreview] = useState<{ id: string; url: string; create: boolean } | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [pairError, setPairError] = useState("");
  const pairEpoch = useRef(0);
  const [busy, setBusy] = useState(false);
  const [replacement, setReplacement] = useState<{ id: string; name: string } | null>(null);

  useEffect(() => {
    let disposed = false;
    if (online) {
      void client
        .phoneSettings()
        .then((value) => {
          if (disposed) return;
          setSettings(value);
          setAddress(value.address || value.addresses?.[0] || "");
          setPort(String(value.port ?? 8766));
        })
        .catch((reason: unknown) => {
          if (!disposed) setError(reason instanceof Error ? reason.message : "手机设置读取失败。");
        });
    }
    return () => {
      disposed = true;
    };
  }, [client, online]);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setPairing(null);
    async function read(): Promise<void> {
      if (!online || target === undefined) return;
      const epoch = pairEpoch.current;
      try {
        const value = await client.phonePairing(target);
        if (!disposed && epoch === pairEpoch.current) {
          setPairing(value);
          setPairError("");
        }
      } catch (reason: unknown) {
        if (!disposed && epoch === pairEpoch.current) {
          setPairing(null);
          setPairError(reason instanceof Error ? reason.message : "配对状态读取失败。");
        }
      } finally {
        if (!disposed) {
          timer = setTimeout(() => {
            void read();
          }, 1000);
        }
      }
    }
    void read();
    return () => {
      disposed = true;
      clearTimeout(timer);
    };
  }, [client, online, target]);

  async function operate(operation: "open" | "replace" | "preview" | "save"): Promise<void> {
    setBusy(true);
    setError("");
    if (operation === "open" || operation === "replace") pairEpoch.current++;
    try {
      if (operation === "open" && target !== undefined) {
        setPairing(await client.phonePairing(target, true));
      } else if (operation === "replace" && target !== undefined && replacement !== null) {
        const result = await client.replacePhonePairing(target, replacement.id);
        if (result.status !== "ready") throw new Error("尚未取得新配对二维码，请重新读取 Engine 状态。");
        setPairing(result);
        setReplacement(null);
        setNotice("旧配对已撤销，请用新安装的 App 扫描二维码。Engine 中的历史记录保留。");
      } else if (operation === "preview") {
        setPreview(null);
        const result = await client.phoneConfigure({ action: "preview", address, port: Number(port) });
        if (
          typeof result.preview_id !== "string" ||
          typeof result.url !== "string" ||
          typeof result.create_identity !== "boolean"
        ) {
          throw new Error("没有取得有效预览，请重试。");
        }
        setPreview({ id: result.preview_id, url: result.url, create: result.create_identity });
      } else if (operation === "save" && preview !== null) {
        const result = await client.phoneConfigure({ action: "save", preview_id: preview.id });
        if (result.saved !== true || result.restart_required !== true) {
          throw new Error("尚未确认保存，请重新读取设置。");
        }
        setPreview(null);
        setNotice("手机设置已保存，原配置已备份。请正常关闭并重新打开 Engine，再回到这里显示配对二维码。");
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "操作未完成，请重试。");
    } finally {
      if (operation === "open" || operation === "replace") pairEpoch.current++;
      setBusy(false);
    }
  }

  const ready = online && pairing?.status === "ready" && (pairing.expires_at ?? 0) * 1000 > Date.now();
  const reason = !online ? "等待重新连接 Engine。" : busy ? "正在处理。" : undefined;
  const badge = !online
    ? { state: "warning" as const, label: "连接中断" }
    : target === undefined
      ? { state: "paused" as const, label: "未启用" }
      : pairing?.status === "paired"
        ? { state: "completed" as const, label: "已配对" }
        : ready
          ? { state: "acting" as const, label: "扫码窗口开启" }
          : { state: "waiting-external" as const, label: "待配对" };

  return (
    <article
      id="phone-connection"
      className="channel-deck phone-deck"
      data-accent="sky"
      data-expanded={ready || editing ? "true" : undefined}
      aria-labelledby="phone-deck-title"
    >
      <div className="deck-body">
        <div className="deck-top">
          <div>
            <span className="deck-code">DECK // PURRCEPT GO</span>
            <h2 id="phone-deck-title" className="deck-title">
              连接安卓手机
            </h2>
          </div>
          <StateMark {...badge} />
        </div>

        <p className="deck-desc">
          在手机打开 Purrcept Go 扫码配对；主体始终在这台电脑运行，聊天无需无障碍权限。
        </p>

        {!online ? (
          <p role="status" className="muted">
            连接中断，配对二维码已收起。连接恢复后会重新读取。
          </p>
        ) : target === undefined ? (
          <p className="muted">Phone 尚未在当前 Engine 启用。展开下方设置保存后正常重启即可。</p>
        ) : pairing?.status === "paired" ? (
          <p role="status" className="deck-highlight">
            已绑定设备：<strong>{pairing.name ?? "安卓手机"}</strong>（换机或重装 App 可点击下方重新配对）
          </p>
        ) : ready && pairing !== null ? (
          <div className="phone-qr-layout">
            <svg
              className="phone-qr"
              viewBox={`0 0 ${pairing.qr_size} ${pairing.qr_size}`}
              role="img"
              aria-label="手机配对二维码"
              shapeRendering="crispEdges"
            >
              <rect width="100%" height="100%" fill="white" />
              <path d={pairing.qr_path} fill="black" />
            </svg>
            <div className="phone-pair-instructions">
              <h3>用手机扫描这个二维码</h3>
              <p>让手机与电脑连接同一个 Wi-Fi，扫码核对后点击配对并连接。</p>
              <p className="phone-address">{pairing.url}</p>
              <p className="muted">
                有效至 {new Date((pairing.expires_at ?? 0) * 1000).toLocaleTimeString("zh-CN")}。二维码只在本机显示，请勿分享。
              </p>
              <details>
                <summary>核对电脑证书</summary>
                <p className="phone-fingerprint">{pairing.certificate_sha256?.toUpperCase()}</p>
              </details>
            </div>
          </div>
        ) : (
          <p role="status" className="muted">
            {pairing === null
              ? "正在读取 Phone 配对状态…"
              : pairing.status === "unavailable"
                ? "Phone 监听器尚未就绪，请查看系统页的启动或故障信息。"
                : "点击「显示配对二维码」开启两分钟扫码窗口，无需重启 Engine。"}
          </p>
        )}

        <AnimatePresence initial={false}>
          {editing ? (
            <motion.div
              key="phone-settings-panel"
              className="phone-settings"
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={transitions.snap}
            >
              {settings?.available ? (
                <>
                  <p className="muted">保存到当前使用的 {settings.config_name}，下次正常启动后生效。</p>
                  <div className="config-grid">
                    <Field label="手机可访问的电脑地址" description="通常填写电脑 Wi-Fi 的 IPv4 地址，不含协议和端口。">
                      {(control) => (
                        <input
                          {...control}
                          list="phone-addresses"
                          value={address}
                          onChange={(event) => {
                            setAddress(event.target.value);
                            setPreview(null);
                          }}
                        />
                      )}
                    </Field>
                    <Field label="连接端口" description="避免与 WebUI 使用同一端口。">
                      {(control) => (
                        <input
                          {...control}
                          type="number"
                          min="1"
                          max="65535"
                          value={port}
                          onChange={(event) => {
                            setPort(event.target.value);
                            setPreview(null);
                          }}
                        />
                      )}
                    </Field>
                  </div>
                  <datalist id="phone-addresses">
                    {settings.addresses?.map((item) => <option key={item} value={item} />)}
                  </datalist>
                  <div className="actions">
                    <Button variant="secondary" disabledReason={reason} onClick={() => void operate("preview")}>
                      查看将要保存的改动
                    </Button>
                  </div>
                  {preview !== null ? (
                    <div className="phone-review">
                      <p>连接地址：{preview.url}</p>
                      <p>
                        {preview.create ? "创建本机 TLS 证书" : "保留已有证书身份"}，启用 Phone 并监听此端口；原配置自动备份。
                      </p>
                      <Button disabledReason={reason} onClick={() => void operate("save")}>
                        保存手机设置
                      </Button>
                    </div>
                  ) : null}
                </>
              ) : (
                <p className="muted">{settings?.message ?? "正在读取当前配置…"}</p>
              )}
            </motion.div>
          ) : null}
        </AnimatePresence>

        {notice ? <p role="status">{notice}</p> : null}
        {(error || pairError) && replacement === null ? <p role="alert">{error || pairError}</p> : null}
      </div>

      <div className="deck-actions">
        {online && target !== undefined ? (
          pairing?.status === "paired" ? (
            <Button
              variant="secondary"
              disabledReason={reason}
              onClick={() => {
                if (pairing.device_id !== undefined) {
                  setError("");
                  setReplacement({ id: pairing.device_id, name: pairing.name ?? "安卓手机" });
                }
              }}
            >
              重新配对
            </Button>
          ) : (
            <Button disabledReason={reason} onClick={() => void operate("open")}>
              {ready ? "刷新配对二维码" : "显示配对二维码"}
            </Button>
          )
        ) : null}
        <Button variant="ghost" onClick={() => setEditing(!editing)}>
          {editing ? "收起手机设置" : "设置手机连接"}
        </Button>
      </div>

      <Dialog
        open={replacement !== null}
        onOpenChange={(open) => {
          if (!open && !busy) setReplacement(null);
        }}
        title="重新配对手机？"
        description={`将撤销「${replacement?.name ?? "旧手机"}」的连接凭据并断开已有连接。Engine 中的历史记录会保留；已执行的手机操作不会撤销。确认后用新安装的 App 扫描新二维码。`}
        actions={
          <>
            <Button variant="secondary" disabledReason={busy ? "正在处理。" : undefined} onClick={() => setReplacement(null)}>
              取消
            </Button>
            <Button disabledReason={reason} onClick={() => void operate("replace")}>
              撤销旧配对并显示二维码
            </Button>
          </>
        }
      >
        {error ? <p role="alert">{error}</p> : null}
      </Dialog>
    </article>
  );
}
