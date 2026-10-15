"""执行与接管的冻结资源额度，不依赖宿主配置或装配。

app将TOML中的execution节校验为本模块值，执行服务和存储借用其中额度。
字段是已授予的上限，Worker和模型不能修改；实际开始时的剩余期限由执行
服务计算。这里只校验纯数值和协议信封关系，不打开进程、连接或设备。
"""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveInt = Annotated[int, Field(gt=0)]
PositiveSeconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ExecutionSettings(BaseModel):
    """拥有脚本、IPC、普通派发及输入接管额度，模型不能提高这些上限。"""

    model_config = ConfigDict(
        frozen=True,
        strict=True,
        extra="forbid",
        hide_input_in_errors=True,
        revalidate_instances="always",
    )
    action_seconds: PositiveSeconds = 180.0
    worker_cpu_seconds: PositiveSeconds = 15.0
    worker_memory_bytes: PositiveInt = 512 * 1024 * 1024
    max_calls: PositiveInt = 64
    source_bytes: PositiveInt = 64 * 1024
    output_bytes: PositiveInt = 64 * 1024
    result_bytes: PositiveInt = 1024 * 1024
    call_arguments_bytes: PositiveInt = 1024 * 1024
    call_result_bytes: PositiveInt = 1024 * 1024
    frame_bytes: PositiveInt = 2 * 1024 * 1024
    max_workers: PositiveInt = 4
    max_provider_calls: PositiveInt = 16
    max_pending_inputs: PositiveInt = 1024
    pending_input_bytes: PositiveInt = 256 * 1024 * 1024

    @model_validator(mode="after")
    def _frame_carries_payload(self) -> Self:
        """帧还需容纳协议信封，不能把有效负载上限直接当成帧上限。"""
        largest_payload = max(self.result_bytes, self.call_arguments_bytes, self.call_result_bytes)
        if self.frame_bytes <= largest_payload:
            raise ValueError("协议帧必须大于最大单条 JSON 负载并为信封留出空间")
        return self
