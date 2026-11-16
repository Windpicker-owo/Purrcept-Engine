/** 手机连接由 Engine WebUI 持有：只读查询不打开窗口，明确点击才生成邀请或保存配置。 */
import { useEffect, useRef, useState } from "react";
import type { ReactElement } from "react";
import { Button, Dialog, Field, Panel } from "@purrcept/cel";
import type { PanelClient } from "../client";
import type { PhonePairing, PhoneSetup } from "../model/phone";

/** 设置只保存到下次启动；短时二维码随断线、过期或组件卸载撤下，不存浏览器。 */
export function PhoneConnection({ client, online, target }: { client: PanelClient; online: boolean; target: string | undefined }): ReactElement {
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
    if (online) void client.phoneSettings().then((value) => {
      if (disposed) return;
      setSettings(value); setAddress(value.address || value.addresses?.[0] || ""); setPort(String(value.port ?? 8766));
    }).catch((reason: unknown) => { if (!disposed) setError(reason instanceof Error ? reason.message : "手机设置读取失败。"); });
    return () => { disposed = true; };
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
        if (!disposed && epoch === pairEpoch.current) { setPairing(value); setPairError(""); }
      } catch (reason: unknown) {
        if (!disposed && epoch === pairEpoch.current) { setPairing(null); setPairError(reason instanceof Error ? reason.message : "配对状态读取失败。"); }
      } finally {
        if (!disposed) timer = setTimeout(() => { void read(); }, 1000);
      }
    }
    void read();
    return () => { disposed = true; clearTimeout(timer); };
  }, [client, online, target]);

  async function operate(operation: "open" | "replace" | "preview" | "save"): Promise<void> {
    setBusy(true); setError("");
    if (operation === "open" || operation === "replace") pairEpoch.current++;
    try {
      if (operation === "open" && target !== undefined) setPairing(await client.phonePairing(target, true));
      else if (operation === "replace" && target !== undefined && replacement !== null) {
        const result = await client.replacePhonePairing(target, replacement.id);
        if (result.status !== "ready") throw new Error("尚未取得新配对二维码，请重新读取 Engine 状态。");
        setPairing(result); setReplacement(null);
        setNotice("旧配对已撤销，请用新安装的 App 扫描二维码。Engine 中的历史记录保留。");
      }
      else if (operation === "preview") {
        setPreview(null);
        const result = await client.phoneConfigure({ action: "preview", address, port: Number(port) });
        if (typeof result.preview_id !== "string" || typeof result.url !== "string" || typeof result.create_identity !== "boolean") throw new Error("没有取得有效预览，请重试。");
        setPreview({ id: result.preview_id, url: result.url, create: result.create_identity });
      } else if (operation === "save" && preview !== null) {
        const result = await client.phoneConfigure({ action: "save", preview_id: preview.id });
        if (result.saved !== true || result.restart_required !== true) throw new Error("尚未确认保存，请重新读取设置。");
        setPreview(null);
        setNotice("手机设置已保存，原配置已备份。请正常关闭并重新打开 Engine，再回到这里显示配对二维码。");
      }
    } catch (reason: unknown) { setError(reason instanceof Error ? reason.message : "操作未完成，请重试。"); }
    finally { if (operation === "open" || operation === "replace") pairEpoch.current++; setBusy(false); }
  }

  const ready = online && pairing?.status === "ready" && (pairing.expires_at ?? 0) * 1000 > Date.now();
  const reason = !online ? "等待重新连接 Engine。" : busy ? "正在处理。" : undefined;
  return <Panel id="phone-connection" title="连接安卓手机">
    <div className="phone-connection">
      <p>在手机打开 Purrcept Go，选择扫码配对。主体始终在这台电脑运行，聊天无需无障碍或设备操控权限。</p>
      {!online ? <p role="status">连接中断，配对二维码已收起。连接恢复后会重新读取。</p> : target === undefined
        ? <p>Phone 尚未在当前 Engine 启用。展开下方设置，保存后正常重启 Engine。</p>
        : pairing?.status === "paired" ? <>
          <p role="status">电脑已保存配对：{pairing.name ?? "安卓手机"}。</p>
          <p className="muted">重装 App 或更换手机后，请点击重新配对，再扫描新二维码。</p>
          <div className="actions"><Button variant="secondary" disabledReason={reason} onClick={() => { if (pairing.device_id !== undefined) { setError(""); setReplacement({ id: pairing.device_id, name: pairing.name ?? "安卓手机" }); } }}>重新配对</Button></div>
        </>
          : <>
            {ready && pairing !== null ? <div className="phone-qr-layout">
              <svg className="phone-qr" viewBox={`0 0 ${pairing.qr_size} ${pairing.qr_size}`} role="img" aria-label="手机配对二维码" shapeRendering="crispEdges">
                <rect width="100%" height="100%" fill="white" /><path d={pairing.qr_path} fill="black" />
              </svg>
              <div className="phone-pair-instructions"><h3>用手机扫描这个二维码</h3><p>让手机与电脑连接同一个 Wi-Fi，扫码后核对电脑，再点击配对并连接。</p>
                <p className="phone-address">{pairing.url}</p><p className="muted">有效至 {new Date((pairing.expires_at ?? 0) * 1000).toLocaleTimeString("zh-CN")}。二维码只在本机显示，请勿分享。</p>
                <details><summary>核对电脑证书</summary><p className="phone-fingerprint">{pairing.certificate_sha256?.toUpperCase()}</p></details></div>
            </div> : <p role="status">{pairing === null ? "正在读取 Phone 配对状态…" : pairing.status === "unavailable" ? "Phone 监听器尚未就绪，请查看系统页的启动或故障信息。" : "当前没有有效二维码。点击下方按钮开启新的两分钟配对窗口，无需重启 Engine。"}</p>}
            <div className="actions"><Button disabledReason={reason} onClick={() => { void operate("open"); }}>{ready ? "刷新配对二维码" : "显示配对二维码"}</Button></div>
          </>}
      <div className="actions"><Button variant="ghost" onClick={() => setEditing(!editing)}>{editing ? "收起手机设置" : "设置手机连接"}</Button></div>
      {editing ? <div className="phone-settings">
        {settings?.available ? <>
          <p className="muted">保存到当前 Engine 使用的 {settings.config_name}。更改会在下次正常启动后生效。</p>
          <Field label="手机可访问的电脑地址" description="通常填写电脑 Wi-Fi 的 IPv4 地址，不包含协议和端口。">{(control) => <input {...control} list="phone-addresses" value={address} onChange={(event) => { setAddress(event.target.value); setPreview(null); }} />}</Field>
          <datalist id="phone-addresses">{settings.addresses?.map((item) => <option key={item} value={item} />)}</datalist>
          <Field label="连接端口" description="保持当前建议端口，避免与 WebUI 使用同一个端口。">{(control) => <input {...control} type="number" min="1" max="65535" value={port} onChange={(event) => { setPort(event.target.value); setPreview(null); }} />}</Field>
          <div className="actions"><Button variant="secondary" disabledReason={reason} onClick={() => { void operate("preview"); }}>查看将要保存的改动</Button></div>
          {preview !== null ? <div className="phone-review"><p>连接地址：{preview.url}</p><p>{preview.create ? "创建本机 TLS 证书" : "保留已有证书身份"}，启用 Phone 并监听电脑网卡上的此端口。模型设置和其他插件保留，原配置自动备份。</p>
            <Button disabledReason={reason} onClick={() => { void operate("save"); }}>保存手机设置</Button></div> : null}
        </> : <p>{settings?.message ?? "正在读取当前配置…"}</p>}
      </div> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {(error || pairError) && replacement === null ? <p role="alert">{error || pairError}</p> : null}
      <Dialog open={replacement !== null} onOpenChange={(open) => { if (!open && !busy) setReplacement(null); }} title="重新配对手机？"
        description={`将撤销「${replacement?.name ?? "旧手机"}」的连接凭据并断开已有连接。Engine 中的历史记录会保留；已执行的手机操作不会撤销。确认后用新安装的 App 扫描新二维码。`}
        actions={<><Button variant="secondary" disabledReason={busy ? "正在处理。" : undefined} onClick={() => setReplacement(null)}>取消</Button><Button disabledReason={reason} onClick={() => { void operate("replace"); }}>撤销旧配对并显示二维码</Button></>}>
        {error ? <p role="alert">{error}</p> : null}
      </Dialog>
    </div>
  </Panel>;
}
