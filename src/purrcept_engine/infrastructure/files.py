"""有界 HTTP 文件下载；仅返回宿主内的字节，不决定保存目录或解析文件内容。

客户端由使用者创建并关闭。每次下载有整体截止时间，逐跳验证重定向，按实际
解码后的字节计量；传输失败不重试，也不会把未完整接收的数据交给发布方。
"""

import asyncio
from urllib.parse import urljoin

import httpx


class DownloadError(Exception):
    """下载未取得完整文件；消息不包含可能带授权信息的下载地址。"""


def validate_download_url(value: str) -> str:
    """核验 HTTP(S) 来源，允许 OneBot 提供的本机或局域网地址。

    Args:
        value: 平台实际提供的完整下载链接。

    Raises:
        DownloadError: 链接无效、带用户凭据或使用非 HTTP(S) 协议。
    """
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as error:
        raise DownloadError("下载链接无效。") from error
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.fragment:
        raise DownloadError("下载链接须为不含用户凭据和片段的 HTTP(S) 地址。")
    return str(url)


async def download_file(
    client: httpx.AsyncClient, url: str, *, max_bytes: int, timeout_seconds: float
) -> bytes:
    """在整体时限和实际字节上限内取得完整文件。

    Args:
        client: 所属插件持有并负责关闭的客户端，不使用隐式重试。
        url: 已取得的 HTTP(S) 地址；每次重定向也执行相同核验。
        max_bytes: 文件解码后允许占用的最大字节数。
        timeout_seconds: 包括重定向和流式接收的总秒数。

    Returns:
        完整文件字节；调用者随后才可发布到磁盘。

    Raises:
        DownloadError: 地址、响应、大小或传输时间不满足约束。

    Note:
        取消原样传播并关闭当前响应。局域网地址是合法 OneBot 部署方式，
        这里不禁止私网，但禁止环境代理、任意协议和无限重定向。
    """
    current = validate_download_url(url)
    try:
        async with asyncio.timeout(timeout_seconds):
            for redirect in range(4):
                async with client.stream("GET", current) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect == 3:
                            raise DownloadError("文件下载重定向缺少目标或超过三次。")
                        current = validate_download_url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise DownloadError(f"文件下载返回 HTTP {response.status_code}。")
                    declared = response.headers.get("content-length")
                    length: int | None = None
                    if declared is not None:
                        if not declared.isascii() or not declared.isdecimal():
                            raise DownloadError("文件下载返回无效长度。")
                        length = int(declared)
                        if length > max_bytes:
                            raise DownloadError("文件超过附件字节上限。")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > max_bytes:
                            raise DownloadError("文件超过附件字节上限。")
                        data.extend(chunk)
                    if (
                        length is not None
                        and not response.headers.get("content-encoding")
                        and len(data) != length
                    ):
                        raise DownloadError("文件下载不完整，实际长度与响应声明不符。")
                    return bytes(data)
    except (httpx.HTTPError, TimeoutError) as error:
        raise DownloadError("文件下载失败或超时，未保存文件。") from error
    raise DownloadError("文件下载未取得完整响应。")
