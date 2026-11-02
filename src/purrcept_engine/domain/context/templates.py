"""上下文材料的 Core 模板，集中文字布局而不拥有业务数据或序列化。

调用方先完成 JSON/YAML 编码、围栏长度计算和媒体位置选择，再绑定模板值。
模板不读取资源、不解释正文中的占位符；原生内容块仍由相邻材料模块组装。
"""

from purrcept_core.models import PromptTemplate, RenderPolicy

# 请求局部尾部与实际用量锚点共用此身份，不能按用户正文中的相似标题定位。
ENGINE_STATE_REMINDER_KEY = "engine.committed_state"

# Core 默认 optional 会把纯空白视为空。模型原文、配置字段和序列化正文必须
# 保留精确字符，使用 Core 的显式策略直接转为文字，不隐式 strip 或二次插值。
VERBATIM = RenderPolicy(str)

MATERIAL_PROMPT = PromptTemplate(
    "context.material",
    "## {title}\n{fields}",
    policies={"title": VERBATIM, "fields": VERBATIM},
)
FENCED_TEXT_PROMPT = PromptTemplate(
    "context.fenced_text",
    "{fence}\n{text}\n{fence}",
    policies={"fence": VERBATIM, "text": VERBATIM},
)
ATTENTION_CONTEXT_PROMPT = PromptTemplate(
    "context.attention",
    "## 注意判断的有限工作背景（不含完整主脑历史）\n{body}",
    policies={"body": VERBATIM},
)
INPUT_BATCH_PROMPT = PromptTemplate(
    "context.input_batch",
    "以下为本批已接管的最新刺激（按来源分组）：",
)
INPUT_SOURCE_PROMPT = PromptTemplate(
    "context.input_source",
    "===来自 {source} 的最新刺激===",
    policies={"source": VERBATIM},
)
INPUT_EVIDENCE_PROMPT = PromptTemplate(
    "context.input_evidence",
    "输入证据：{input_id}\n"
    "来源发生时间 occurred_at：{occurred_at}\n"
    "宿主接收时间 received_at：{received_at}\n"
    "时间含日期与时区；来源时间由提供方报告，跨来源分组及历史呈现位置不代表实际先后。",
    policies={"input_id": VERBATIM, "occurred_at": VERBATIM, "received_at": VERBATIM},
)
MEDIA_REFERENCE_PROMPT = PromptTemplate(
    "context.media_reference",
    "{reason}\n{reference}",
    policies={"reason": VERBATIM, "reference": VERBATIM},
)
EXPIRED_AUDIO_PROMPT = PromptTemplate(
    "context.expired_audio",
    "历史语音输入已过期",
)
EXPIRED_IMAGE_PROMPT = PromptTemplate(
    "context.expired_image",
    "历史图片已过期",
)
MISSING_MATERIALS_PROMPT = PromptTemplate(
    "context.missing_materials",
    "材料缺口：\n{items}",
    policies={"items": VERBATIM},
)
DYNAMIC_STATE_PROMPT = PromptTemplate(
    "context.committed_state",
    "{state}",
    policies={"state": VERBATIM},
)
SUMMARY_MESSAGE_PROMPT = PromptTemplate(
    "context.summary.message",
    "内部历史摘要；不是新的外部证据。\n旧轮次 {start_turn}–{end_turn}：\n{summary}",
    policies={"summary": VERBATIM},
)
SUMMARY_PROMPT = PromptTemplate(
    "context.summary.prompt",
    "【系统内部上下文压缩指令】\n"
    "这是一次由宿主发起的内部上下文容量维护请求，不是新的外部用户发言、环境刺激或行动触发。"
    "严禁调用任何工具（禁止调用 play_action、recall、read_evidence、plan_wakeup、resolve_plan、"
    "respond_supervision 等），也不要输出寒暄、内心独白或角色扮演台词。\n"
    "请直接基于当前上下文中的上述对话与交互历史，进行专业、高密度、无损关键事实的提炼，用于替换"
    '这段已完成的旧历史。只输出标准 JSON 对象 {{"summary":"摘要正文"}}，严禁包含任何 Markdown '
    "格式、代码围栏或额外说明文字。\n\n"
    "请务必在摘要正文中完整涵盖并明确梳理以下五个核心维度：\n"
    "1. 观点、偏好与关系边界：提炼交互对象（用户或群友）明确表达的观点态度、兴趣喜好、"
    "生活作息与饮食习惯、雷区禁忌、称呼方式及沟通偏好；保留主体自身的立场表态，以及双方"
    "在互动中形成的相处默契、关系亲疏演变与社交边界共识；坚决剔除无实质信息量的客套寒暄、"
    "过冲情绪与重复口癖。\n"
    "2. 关键事件与时空因果脉络：按真实发生时间（结合 occurred_at、received_at 与行动时间线）梳理"
    "具体事件、核心话题的来龙去脉、重要决定与生活轨迹（如正在进行或刚结束的日常活动、浏览内容、"
    "文件处理、设备与睡眠状态等）；保留关键的触发前提、因果转折与否定限制条件，确保前后语境不失真。\n"
    "3. 未完成的目标、承诺与待办事项：严格保留主体对他人或自己许下的所有尚未闭环的承诺、口头约定、"
    "待继续推进的目标、定时唤醒计划（plan_wakeup）以及未完结的活动（activity）；清晰标注每项事宜的"
    "当前进展（已完成步骤、待确认事项、阻塞原因或后续触发条件）。\n"
    "4. 人工审核反馈与反思监管意见：重点提取人工行为审查（source=human_feedback 的 rating=good/bad "
    "及 comment 留言）提出的关键批评、肯定与改进指导，以及后台行为评审下达的监管纠偏要求"
    "（supervision）；明确记录被要求改正的问题（如用力过猛、机械套话、分寸失当或越界回复）及"
    "已被认可的得体做法，确保后续行动继续遵守这些约束。\n"
    "5. 言行事实分离与证据缺口保留：严格区分外部输入原话、主体内部思考、工具调用的实际回执"
    "（confirmed / failed / unknown）与未执行状态；未调用对外能力绝不能记为已回复或已发生，"
    "unknown 状态不得臆断为已成功；保留关键人物身份、会话通道、文件路径及尚未展开的证据引用"
    "（evidence_ref / stored_ref），对未展开原文或已过期历史媒体保持客观未决，"
    "严禁脑补未见的事实细节。",
)
