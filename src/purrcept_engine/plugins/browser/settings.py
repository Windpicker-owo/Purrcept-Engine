"""浏览器冻结配置，路径以主配置目录为基准，声明不启动浏览器。

computer_id明确指父电脑插件实例；可见路径由Catalog父链生成，不重复配置
完整模型路径。站点名单约束文档导航完整性，不建立人员权限或审批。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*$")]
Positive = Annotated[int, Field(gt=0, strict=True)]
Finite = Annotated[float, Field(allow_inf_nan=False)]


class SearchEngine(StrEnum):
    """旧内置搜索结果解析的两种真实格式，不自动换搜索服务。"""

    BING = "bing"
    DUCKDUCKGO = "duckduckgo"


class BrowserSettings(BaseModel):
    """一次浏览器会话的明确选择；缺依赖/浏览器程序不自动安装。"""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    computer_id: Identifier = "computer"
    browser_id: Identifier = "browser"
    label: str = Field(default="浏览器", min_length=1, pattern=r"\S")
    user_data_dir: Path = Field(default=Path(".purrcept/browser-profile"), strict=False)
    screenshot_dir: Path | None = Field(default=None, strict=False)
    channel: str = "chrome"
    executable_path: Path | None = Field(default=None, strict=False)
    headless: bool = True
    cdp_url: str = ""
    search_engine: SearchEngine = Field(default=SearchEngine.BING, strict=False)
    search_url: str = ""
    allowed_hosts: tuple[str, ...] = Field(default=(), strict=False)
    blocked_hosts: tuple[str, ...] = Field(default=(), strict=False)
    navigation_timeout_seconds: Finite = Field(default=45, gt=0)
    action_timeout_seconds: Finite = Field(default=30, gt=0)
    max_chars: Positive = 8000
    max_search_results: Positive = 8
    screenshot_max_files: Positive = 20
    screenshot_quality: Positive = Field(default=60, le=100)
    typing_delay_per_char_seconds: Finite = Field(default=0.04, ge=0)
    typing_delay_max_seconds: Finite = Field(default=1.2, ge=0)

    @field_validator("allowed_hosts", "blocked_hosts")
    @classmethod
    def known_host_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """配置边界拒空主机，规范大小写，不把正文内容解释成许可。"""
        if any(not host.strip() for host in value):
            raise ValueError("主机名单不能包含空项。")
        return tuple(host.strip().lower() for host in value)

    def typing_pause_seconds(self, text: str) -> float:
        """沿用每字0.04秒、总1.2秒的输入停顿，0关闭停顿。"""
        return min(len(text) * self.typing_delay_per_char_seconds, self.typing_delay_max_seconds)

    def resolve_paths(self, base_dir: Path) -> BrowserSettings:
        """纯路径装配，不查文件、不展开另一个配置来源。"""
        replacements: dict[str, object] = {}
        for name in ("user_data_dir", "screenshot_dir", "executable_path"):
            value: object = getattr(self, name)
            if isinstance(value, Path) and not value.is_absolute():
                replacements[name] = base_dir / value
        return self.model_copy(update=replacements)
