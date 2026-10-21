"""用户亲自填写的本机隐藏凭据窗口；只保存，不读取已有秘密或连接平台。"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from .credentials import (
    CredentialError,
    OpenLiveCredentials,
    WindowsCredentialStore,
    credential_target,
)

APP_ID = 1783128932975


def main() -> None:
    """打开仅本机可见的输入窗口；保存由用户点击，取消不触碰保险库。

    Note:
        没有HTTP端口、浏览器存储、命令行秘密或网络操作。
        保存会替换此项目条目；不会更改Engine配置或重启服务。
    """
    window = tk.Tk()
    window.title("杰瑞喵 · B站官方接入凭据")
    window.resizable(False, False)
    frame = ttk.Frame(window, padding=20)
    frame.grid()
    ttk.Label(
        frame,
        text=f"项目：杰瑞喵  {APP_ID}\n房间：1850493825\n仅保存至当前Windows用户的凭据管理器。",
        justify="left",
    ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
    entries: list[ttk.Entry] = []
    for row, label in enumerate(("AccessKey ID", "AccessKey Secret", "主播身份码"), start=1):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 12), pady=5)
        entry = ttk.Entry(frame, show="●", width=46)
        entry.grid(row=row, column=1, pady=5)
        entries.append(entry)
    ttk.Label(
        frame,
        text="保存会替换此项目的本地条目；不登录、不连接平台、不启动直播。\n不要把任何字段发到聊天或截图中。",
        justify="left",
    ).grid(row=4, column=0, columnspan=2, sticky="w", pady=12)
    status = ttk.Label(frame, text="等待你亲自填写。")
    status.grid(row=5, column=0, columnspan=2, sticky="w")

    def save() -> None:
        try:
            value = OpenLiveCredentials(*(entry.get().strip() for entry in entries))
            WindowsCredentialStore().write(APP_ID, value)
        except CredentialError as error:
            status.configure(text=f"未保存：{error}")
            return
        for entry in entries:
            entry.delete(0, tk.END)
        status.configure(text="已保存。尚未连接B站，请回到聊天告知“已保存”。")
        save_button.configure(state="disabled")
        messagebox.showinfo(
            "保存完成",
            f"保存位置：Windows凭据管理器\n{credential_target(APP_ID)}\n\n未启用官方接入、未更改当前运行服务。",
            parent=window,
        )

    save_button = ttk.Button(frame, text="保存凭据", command=save)
    save_button.grid(row=6, column=0, pady=(14, 0))
    ttk.Button(frame, text="关闭", command=window.destroy).grid(
        row=6, column=1, sticky="e", pady=(14, 0)
    )
    entries[0].focus_set()
    window.mainloop()


if __name__ == "__main__":
    main()
