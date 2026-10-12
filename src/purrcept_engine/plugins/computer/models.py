"""电脑采集的冻结边界与有限变化检测，不读取设备或推动主体。

驱动交付完整JPEG及当次窗口身份。ComputerRuntime只持有上一采样、上次接管
的分块基准和一个候选；原生字节正式接管后归Store。Pillow只在变化检测时
加载，电脑未启用不要求安装图像依赖。
"""

from __future__ import annotations

import io
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from purrcept_engine.errors import EngineError

Positive = Annotated[int, Field(gt=0, strict=True)]
Finite = Annotated[float, Field(allow_inf_nan=False)]
Target = Literal["foreground_monitor", "monitor", "window"]


class ComputerError(EngineError):
    """连接/目标业务资格拒绝；设备或内部故障不能改写为成功截图。"""


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ObservationPolicy(_Record):
    """持久观察偏好；enabled只在显式连接后生效，不恢复设备连接。"""

    enabled: bool = False
    target: Target = "foreground_monitor"
    monitor_id: str = ""
    threshold: Finite = Field(default=8, gt=0, le=255)
    changed_blocks: Positive = Field(default=2, le=144)
    min_interval: Finite = Field(default=0.25, ge=0.25, le=2)
    max_interval: Finite = Field(default=2, ge=0.25, le=2)
    analysis_interval: Finite = Field(default=10, ge=10, le=3600)
    microphone_id: str = "microphone"
    proactive_voice: bool = False
    voice_interval: Finite = Field(default=60, ge=60, le=3600)

    @model_validator(mode="after")
    def valid_target_and_intervals(self) -> ObservationPolicy:
        """拒绝未指定显示器或反向采样区间，不静默选另一块屏幕。"""
        if self.target == "monitor" and not self.monitor_id.strip():
            raise ValueError("monitor目标必须给出monitor_id。")
        if self.min_interval > self.max_interval:
            raise ValueError("最小采样间隔不能超过最大采样间隔。")
        return self


class ForegroundInfo(_Record):
    """实际窗口与显示器物理像素边界，负坐标允许，面积必须为正。"""

    window_id: str = Field(min_length=1)
    title: str
    process_id: Positive
    executable: str
    bounds: tuple[int, int, int, int] = Field(strict=False)
    monitor_id: str = Field(min_length=1)
    monitor_bounds: tuple[int, int, int, int] = Field(strict=False)

    @model_validator(mode="after")
    def positive_area(self) -> ForegroundInfo:
        """真实驱动与IPC都必须满足矩形约束。"""
        for bounds in (self.bounds, self.monitor_bounds):
            if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                raise ValueError("窗口和显示器必须有正面积。")
        return self


class ComputerFrame(_Record):
    """本次完整编码像素；失败不能用旧帧代替，时间为UTC Unix秒。"""

    data: bytes = Field(min_length=1, max_length=8 * 1024 * 1024)
    captured_at: Finite = Field(gt=0)
    foreground: ForegroundInfo
    target: str = Field(min_length=1)
    original_size: tuple[Positive, Positive] = Field(strict=False)
    delivered_size: tuple[Positive, Positive] = Field(strict=False)
    capture_ms: Finite = Field(default=0, ge=0)
    encode_ms: Finite = Field(default=0, ge=0)

    @property
    def identity(self) -> tuple[object, ...]:
        """窗口、显示器、目标或尺寸变化不能沿用旧变化基准。"""
        return (
            self.foreground.window_id,
            self.foreground.monitor_id,
            self.target,
            self.original_size,
            self.foreground.monitor_bounds,
        )


class ChangeDetector:
    """16×9亮度双基准，上一采样检测突变，上次接管检测缓慢累计变化。"""

    def __init__(self) -> None:
        """每次明确观察代次重建基准，不保存无限帧。"""
        self.previous: tuple[float, ...] | None = None
        self.submitted: tuple[float, ...] | None = None
        self.identity: tuple[object, ...] | None = None

    def sample(
        self, frame: ComputerFrame, policy: ObservationPolicy
    ) -> tuple[bool, tuple[float, ...]]:
        """仅计算变化；接管成功后才由committed推进累计基准。"""
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(frame.data)) as source:
            gray = source.convert("L")
            gray.thumbnail((320, 180))
            width, height = gray.size
            # 小窗口的边缘网格重复同一像素，避免零面积裁剪；不拉伸长宽比。
            blocks = tuple(
                float(
                    ImageStat.Stat(
                        gray.crop(
                            (
                                min(width - 1, x * width // 16),
                                min(height - 1, y * height // 9),
                                max(min(width - 1, x * width // 16) + 1, (x + 1) * width // 16),
                                max(min(height - 1, y * height // 9) + 1, (y + 1) * height // 9),
                            )
                        )
                    ).mean[0]
                )
                for y in range(9)
                for x in range(16)
            )
        changed = self.identity != frame.identity or any(
            baseline is None
            or sum(abs(a - b) > policy.threshold for a, b in zip(blocks, baseline, strict=True))
            >= policy.changed_blocks
            for baseline in (self.previous, self.submitted)
        )
        self.previous, self.identity = blocks, frame.identity
        return changed, blocks

    def committed(self, blocks: tuple[float, ...]) -> None:
        """丢弃候选不影响最后正式接管基准。"""
        self.submitted = blocks
