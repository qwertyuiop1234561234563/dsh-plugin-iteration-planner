"""Agent function calling 编排循环。

流程：构造消息 → 调用 LLM → 若有 ``tool_calls`` 则并行调用内部算法引擎并回填结果 →
再次调用 LLM，直至模型给出最终文本或达到最大步数上限。

设计要点：
- 所有算法结果均来自工具（引擎）返回值，模型不得臆造；
- 单次工具失败不会中断整轮编排，而是把错误作为工具结果回填，让模型自行纠偏；
- 同一条 assistant 消息中的多个 ``tool_calls`` **并行执行**（``asyncio.gather``，保持原序回填）；
- 上下文预算超限时自动裁剪**最旧的用户回合**，保证当前请求与系统提示始终完整；
- 会话历史经 :class:`SessionStore` 持久化（默认内存 + TTL，可切 Redis），同会话串行化；
- **写操作两阶段**：写类工具（``plan_iteration`` / ``replan_all`` / ``task_add`` /
  ``task_update`` / ``task_delete``）不在编排循环内直接执行，而是登记为
  :class:`PendingAction` 返回前端；用户确认后经 :meth:`AgentRunner.confirm_actions`
  统一落库（只读工具仍在循环内即时执行，保证问答与排查可正常进行）。
- **检索增强（RAG + 长期记忆）**：每轮自动召回知识库片段与相关长期记忆并作为独立
  system 区块注入（增强而非必需，检索失败自动降级）；同时向模型暴露 ``kb_search`` /
  ``kb_list_documents`` / ``memory_search`` 只读工具，支持 agentic 检索；
  对话结束后**后台**提取本轮长期记忆（不阻塞响应）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, AsyncIterator

from ..constants import (
    SSE_DELTA,
    SSE_FINAL,
    SSE_META,
    SSE_PENDING,
    SSE_RESULT,
    SSE_TOOL_RESULT,
    SSE_TOOL_START,
)
from ..errors import ApiError, bad_request, llm_unavailable, not_found
from ..kb.pipeline import KnowledgeBaseService
from ..metrics import METRICS
from ..models import AgentChatResponse, AgentRunResult, KbHit, MemoryItem, PendingAction, ToolCallRecord
from ..sessions import InMemorySessionStore, SessionStore
from .llm import LlmClient, strip_protocol_leak
from .memory import MemoryService
from .prompts import ITERATION_PLANNER_SYSTEM_PROMPT, build_chat_instruction, build_plan_instruction
from .sanitize import unwrap_arguments
from .schemas import TOOL_RESULT_PENDING_CONFIRMATION, TOOL_SPECS, WRITE_TOOLS, describe_action
from .tools import ToolDispatcher

logger = logging.getLogger("iteration_planner.agent")

#: 回填给模型的单条工具结果上限（字符），避免超大计划撑爆上下文
_MAX_TOOL_RESULT_CHARS = 60_000

#: 工具名 → 必填参数（用于执行前的结构校验）
_REQUIRED: dict[str, list[str]] = {
    spec["function"]["name"]: list(spec["function"]["parameters"].get("required", [])) for spec in TOOL_SPECS
}
_TOOL_NAMES: frozenset[str] = frozenset(_REQUIRED)

#: 全部已声明的工具名（含因服务缺失而未暴露者），用于识别「模型试图调用但没写成 tool_calls」
_ALL_TOOL_NAMES: frozenset[str] = frozenset(spec["function"]["name"] for spec in TOOL_SPECS)

#: 匹配正文里 JSON 形式的 ``"name": "<工具名>"``（模型把调用写成文本时的常见形状）
_TOOL_ATTEMPT_RE = re.compile(r'"name"\s*:\s*"([A-Za-z_][A-Za-z0-9_]*)"')

#: 模型把工具调用写成不可解析正文时回填的纠偏提示（对齐主流框架的输出格式重试）
_TOOL_RETRY_HINT = (
    "你上一条回复不是有效的工具调用：它把对 {tool} 的调用写成了普通文本，系统无法解析。"
    "请**直接以结构化 tool_calls 重新发出该调用**，不要在正文里书写 JSON 或代码块。"
)

#: 写操作登记为待确认动作后回填给模型的 tool 结果（同时承担「别漏掉其余改动」的提示职责）
_PENDING_TOOL_RESULT = (
    "已登记为待确认动作（**尚未写库**，用户确认后才会执行）：{label}。"
    "若用户本轮还提出了**其它**改动（同一任务的另一个字段、其它任务、增删任务等），"
    "请继续调用相应工具把它们**全部**登记完，不要遗漏也不要重复登记同一条；"
    "全部登记完成后，用一句话告诉用户本轮共待确认哪些变更。"
)

#: 识别「模型宣称自己已经完成了写操作」的措辞（用于拦截无任何写操作却谎报完成）
_WRITE_CLAIM_RE = re.compile(
    r"已(?:成功)?(?:修改|更改|变更|更新|调整|设置|删除|新增|添加|登记|提交|执行|改为|改好"
    r"|规划|创建|生成|发起|完成|处理)"
    r"|操作后(?:的)?(?:任务|计划|结果|详情|数据)"
)

#: 谎报完成时回填给模型的纠偏提示：必须真的调用写工具，而不是宣称已做
_FALSE_COMPLETION_HINT = (
    "你上一条回复宣称已经完成了写操作，但系统**没有收到任何写工具调用**（既没有登记待确认动作，"
    "也没有真正执行），因此用户看到的「已完成」「操作后」结果是虚假的。"
    "请立即**真正调用对应的写工具**（task_update / task_delete / task_add / plan_iteration 等）；"
    "若缺少必要信息（例如不知道要操作哪个计划或哪个任务），请先调用 get_plan / list_plans 取得，"
    "仍无法确定时再向用户说明需要补充什么信息——但**不得宣称已经完成**。"
)

#: 重试后仍谎报完成时的兜底文案（用确定性提示替换，避免用户依据虚假结论行事）
_FALSE_COMPLETION_NOTICE = (
    "⚠️ 本轮**没有任何变更被登记或执行**（未产生待确认动作），因此上一步提到的修改结果并不真实。\n\n"
    "请明确告知要操作的计划与任务（例如「把计划 XXX 中『数据库设计』的工时改为 12 小时」），"
    "或先切换到目标计划页面再发起请求。"
)

#: 「已登记待确认、但尚未落库」时的确定性文案。
#:
#: 实测缺陷：模型在把 ``plan_iteration`` 登记为待确认动作后，照样回复「已成功规划迭代…计划ID为
#: [planId]」——用户会以为计划已经建好，而实际上什么都没落库。这类声明的危害与「什么都没做却
#: 说做了」等同，因此必须与 :data:`_FALSE_COMPLETION_NOTICE` 一样被服务端覆盖。
#: 措辞刻意避开 :data:`_WRITE_CLAIM_RE` 的「已+动词」形态，保证本函数对同一文本重复施加也稳定。
_PENDING_CLAIM_NOTICE = (
    "⚠️ 本轮变更**尚未落库**：改动只被登记为「待确认动作」，需要你确认后才会真正执行，"
    "当前计划数据**没有任何变化**。"
)

#: 模型可能拿**占位符**冒充计划 ID（实测：``计划ID为[planId]``）
_PLAN_ID_PLACEHOLDER_RE = re.compile(r"[\[{<【(]\s*plan_?id\s*[\]}>】)]", re.IGNORECASE)

#: 模型已**如实披露**「改动只是待确认、尚未落库」的语料特征。命中即视为正确表述，不再覆盖。
#:
#: 刻意收紧到「待确认 / 尚未落库 / 需要您确认 / 确认后」这类明确措辞：不能把任何含「请确认」的
#: 句子都当作披露——「已成功创建计划，请确认」这种句尾带确认的**虚假完成声明**照样必须被拦下。
_PENDING_DISCLOSURE_RE = re.compile(r"待确认|尚未落库|需(?:要)?(?:您|你)?确认|确认后")

#: 计划 ID 的真实形态：UUID（引擎由 ``uuid4`` 生成）或 32 位十六进制串。
#: 用于识别「文本里出现了 ID，但本轮根本没有产生任何计划」的**伪造 ID**。
_PLAN_ID_SHAPED_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
    r"|\b[0-9a-fA-F]{32}\b"
)

#: 本轮未产生任何计划却提到 ID 时，替换 ID 片段所用的占位文本 + 追加的确定性说明
_PLAN_ID_ABSENT_TOKEN = "（无）"
_PLAN_ID_ABSENT_NOTICE = (
    "\n\n（系统说明：本轮**没有产生任何计划**，上一步答复里出现的计划 ID 并不存在，"
    "不能用于后续操作或分享。）"
)


def _has_landed_write(records: list[ToolCallRecord]) -> bool:
    """本轮是否存在**真正落库**的写操作。

    不能只看 ``ok``：登记为待确认动作的写工具同样返回 ``ok=True``（调用确实被成功受理），
    但数据**尚未写入**。因此以 :data:`TOOL_RESULT_PENDING_CONFIRMATION` 把「仅登记」剔除，
    剩下的才算落库。
    """
    for record in records:
        if not (record.ok and record.name in WRITE_TOOLS):
            continue
        result = record.result if isinstance(record.result, dict) else {}
        if result.get("status") == TOOL_RESULT_PENDING_CONFIRMATION:
            continue
        return True
    return False


def _enforce_authoritative_reply(
    text: str, *, records: list[ToolCallRecord], plan_id: str | None, pending: list[PendingAction]
) -> str:
    """服务端对模型答复的**最终事实校验与重写**（输出护栏）。

    立场（对齐 OpenAI Agents SDK 的 ``outputGuardrails`` 与 CRITIC 的「结论须有外部凭据」）：
    模型自述是否可信，不由模型自己判断，而由服务端拿**工具的真实执行结果**核对。凡与真实
    执行状态冲突的表述一律以服务端事实为准——因为用户会依据答复采取行动，虚假的「已完成」
    比一句「未完成」危险得多。

    两处必须在服务端兜底：

    1. **完成声明**：命中 :data:`_WRITE_CLAIM_RE` 但本轮没有任何**真正落库**的写操作时，
       整体替换为确定性文案（有 ``pending`` → 「尚未落库」；无 ``pending`` → 「没有任何变更」）。
       唯一例外是模型已**如实披露**了待确认状态（命中 :data:`_PENDING_DISCLOSURE_RE`）——
       那种表述本身正确，覆盖它只会丢掉模型给出的细节。
    2. **计划 ID**：占位符（``[planId]``）与凭空捏造的 ID 都不得下发。本轮确实产出计划时
       用真实 ID 替换占位符；未产出计划时删除 ID 片段并明确说明。

    顺序不可颠倒：完成声明会**整体替换**文本（连带其中的 ID），先替换再处理 ID 才能保证
    最终文本里既没有虚假声明、也没有虚假 ID。
    """
    # ---- 1. 完成声明事实校验（整体替换，故放在前面）----
    if _WRITE_CLAIM_RE.search(text) and not _has_landed_write(records):
        if pending and _PENDING_DISCLOSURE_RE.search(text):
            # 模型已如实说明「只是待确认、还没落库」：表述正确，保留原文
            pass
        elif pending:
            METRICS.inc_counter("agent_false_completion_total", {"action": "pending_notice"})
            logger.warning("模型在仅登记待确认动作（尚未落库）时宣称已完成变更，已替换为确定性提示")
            return _PENDING_CLAIM_NOTICE
        else:
            METRICS.inc_counter("agent_false_completion_total", {"action": "notice"})
            logger.warning("模型在无任何写操作的情况下宣称已完成变更，已替换为确定性提示")
            return _FALSE_COMPLETION_NOTICE

    # ---- 2. 计划 ID 事实校验 ----
    if plan_id:
        text = _PLAN_ID_PLACEHOLDER_RE.sub(plan_id, text)
    elif _PLAN_ID_PLACEHOLDER_RE.search(text) or _PLAN_ID_SHAPED_RE.search(text):
        text = _PLAN_ID_PLACEHOLDER_RE.sub(_PLAN_ID_ABSENT_TOKEN, text)
        text = _PLAN_ID_SHAPED_RE.sub(_PLAN_ID_ABSENT_TOKEN, text)
        text = f"{text}{_PLAN_ID_ABSENT_NOTICE}"
        METRICS.inc_counter("agent_reply_fabricated_plan_id_total")
        logger.warning("模型答复中出现并不存在的计划 ID，已由服务端剔除并说明")
    return text


#: 「当前计划状态」类问题的特征词：这类问题的答案只能是工具实时结果，不能来自长期记忆。
_STATE_QUESTION_RE = re.compile(
    r"工期|工时|关键路径|燃尽|风险|人力|负荷|质量指标|任务清单|进度|延期"
    r"|(?:这个|该|当前|这份|本)(?:计划|迭代|项目)|计划(?:里|中|内)"
)

#: 疑问句式特征。**必须与状态特征词同时命中**才算「状态提问」——否则「把…工时改为 12 小时」
#: 这类**写指令**（含「工时」但并非提问）会被误判，导致纠偏提示把模型带偏去回答上一个问题。
_QUESTION_SHAPE_RE = re.compile(r"[?？]|吗|呢|什么|多少|哪些|哪几|是否|有没有|几(?:天|个|条|项|人)|怎么")

#: 写操作意图的特征词：用户明确要求改动时，本轮必须产出待确认写动作
_WRITE_INTENT_RE = re.compile(r"改为|改成|修改|变更|更新|新增|添加|加到|插入|删除|移除|设置|调整|拆分|指派|分配|补一个|加一个")


def _is_state_question(text: str) -> bool:
    """判断用户输入是否为「当前计划状态」的提问（状态特征词 + 疑问句式同时成立）。"""
    return _STATE_QUESTION_RE.search(text) is not None and _QUESTION_SHAPE_RE.search(text) is not None


def _has_write_intent(text: str) -> bool:
    """判断用户输入是否明确要求改动（写操作）。"""
    return _WRITE_INTENT_RE.search(text) is not None


#: 模型「信息不足 → 反问澄清」的语料特征：命中即视为**合法收敛**（写指令并未被静默忽略），
#: 不再触发写意图纠偏——否则模型如实说「请提供计划 ID」也会被反复逼着重试。
_CLARIFY_RE = re.compile(
    r"[?？]|请(?:再|先)?(?:提供|告知|说明|明确|确认|指明|补充|选择|指定|告诉我|给出)"
    r"|需要(?:您|你)(?:提供|补充|确认|指定)|无法确定|不清楚|不确定|哪一(?:个|种)"
)


def _is_clarification_request(text: str) -> bool:
    """判断模型回复是否为「向用户追问缺失信息」而非作答（含反问与显式索取）。"""
    return _CLARIFY_RE.search(text) is not None


#: 答复中是否出现**度量值**（数字 + 量词/单位）。不能只用裸数字判断——任务 ID 形如 ``t1``
#: 也含数字，会把「关键路径是 t1 → t2」这类正常回答误判为「给出了当前计划的实测值」。
_HAS_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:天|小时|分钟|万元|人|人次|个|项|条|次|%|h|H)")

#: 无工具调用却给出状态数字时回填的纠偏提示（防止拿长期记忆/history 冒充当前实测值）
_STATE_GROUNDING_HINT = (
    "你刚才回答了当前计划的状态，但本轮**没有调用任何工具**。长期记忆与历史对话里的数字"
    "可能来自其它计划或更早时间点，不能作为当前计划的实测值。请**先调用工具**"
    "（get_plan 取任务与工期／schedule_calc 取关键路径／risk_scan 取风险等；若不知道 planId，"
    "先 list_plans），再依据工具返回结果重新作答。本轮只是**查询状态**，不要创建新计划或做任何写操作。"
)

#: 用户明确要求改动，但本轮**没有登记到任何写动作**、也没有追问澄清时回填的纠偏提示
#: （防止写指令被当成状态查询作答，从而被静默空转——用户的修改请求完全不会生效）。
_WRITE_INTENT_HINT = (
    "本轮用户明确要求**改动**计划内容，但系统**没有登记到任何待确认的写动作**"
    "（既没有成功调用写工具，也没有向用户追问缺失信息），因此用户的修改请求当前不会生效。"
    "请**立即调用对应的写工具**（task_update / task_delete / task_add / plan_iteration 等）"
    "把改动登记为待确认动作；若缺少必要信息（如不知道要操作哪个计划或哪个任务），"
    "请先调用 get_plan / list_plans 取得，仍无法确定时再明确向用户说明需要补充什么信息。"
)


#: 送进模型的用户消息里，真实用户输入被包在 ``<<<USER_INPUT ... USER_INPUT>>>`` 边界内，
#: 边界之外的文字是**脚手架**（可信上下文 + list_plans 指引）。做「写意图 / 状态提问」这类
#: 语料判据时必须只看边界内的正文——否则脚手架里的「新增 / 修改 / 删除任务」字样会把
#: 「关键路径怎么算？」这种纯提问误判成写指令，进而触发无意义的纠偏重试。
_UNTRUSTED_BODY_RE = re.compile(r"<<<\s*USER_INPUT\s*(.*?)\s*USER_INPUT\s*>>>", re.DOTALL)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    """取最后一条 user 消息里的**用户原始输入**（已剥离边界脚手架）。"""
    for message in reversed(messages):
        if message.get("role") == "user":
            text = _content_to_text(message.get("content"))
            match = _UNTRUSTED_BODY_RE.search(text)
            return match.group(1).strip() if match else text
    return ""


#: 依赖「知识库 / 长期记忆」服务才可用的工具 → 所需服务名（服务缺失时不出现在工具清单中，
#: 避免模型调用一个必然失败的工具而浪费步数）
_SERVICE_BOUND_TOOLS: dict[str, str] = {
    "kb_search": "knowledge",
    "kb_list_documents": "knowledge",
    "memory_search": "memory",
}

#: 以「既有任务」为目标的写类工具：登记待确认动作前必须校验 taskId 真实存在（见
#: :meth:`AgentRunner._resolve_task_target`），避免模型臆造的 ID 进入确认卡片后才以 404 失败。
_TASK_TARGET_WRITE_TOOLS: frozenset[str] = frozenset({"task_update", "task_delete"})

#: 以「需求文本」为关键入参的写类工具：``requirement`` 会作为需求原文被引擎持久化
#: （并可能经「情景对比」以基线 ``rawRequirement`` 复算扩散到新计划），因此登记前必须
#: 拒绝被模型回填成问句的值（见 :func:`_reject_question_as_requirement`）。
_REQUIREMENT_WRITE_TOOLS: frozenset[str] = frozenset({"plan_iteration", "replan_all"})

#: 回填给模型的「需求文本非法」纠偏提示
_REQUIREMENT_REJECTION = (
    "{tool} 的 requirement 参数必须是**本次要规划 / 重规划的需求原文（陈述句）**，"
    "而当前填入的值看起来是一个**状态提问**（如「当前计划的迭代工期是多少天？」）——"
    "这既不是需求，也不该被持久化为需求原文，因此该调用已被系统拒绝。"
    "请注意区分：\n"
    "- 只是要**改某个已有任务**（责任人 / 工时 / 状态等）→ 请改用 task_update / task_delete，"
    "不要用 {tool} 重做整个计划；\n"
    "- 确实需要**按新需求重新规划** → 请先向用户确认新的需求描述，再以该需求原文调用 {tool}。"
)


def _reject_question_as_requirement(name: str, arguments: dict[str, Any]) -> str | None:
    """校验「需求文本」类写工具的 ``requirement``：是状态提问则返回纠偏提示。

    实测缺陷：会话上一轮问了「当前计划的迭代工期是多少天？是否存在高风险项？」，下一轮
    用户要求改任务时，模型把**上一轮的问题原文**当作 ``replan_all.requirement`` 提交——
    这类值一旦确认落库，就会被引擎当作需求原文保存，并可能经情景对比复算污染后续计划。
    """
    requirement = arguments.get("requirement")
    if not isinstance(requirement, str) or not requirement.strip():
        return None  # 缺参由必填校验统一处理
    if _is_state_question(requirement):
        METRICS.inc_counter("agent_requirement_rejected_total", {"tool": name})
        logger.warning("拒绝把状态提问当作需求原文提交给 %s：%s", name, requirement[:80])
        return _REQUIREMENT_REJECTION.format(tool=name)
    return None


#: 纠偏提示里最多回填的任务条数（防止超大计划把提示撑成另一个「计划快照」）
_MAX_TASK_PREVIEW = 30


def _task_preview(tasks: list[dict[str, Any]]) -> str:
    """把计划任务压缩为「名称(ID)」清单，供模型纠正 taskId 时对照。"""
    preview = [f"{task.get('taskName') or '(未命名)'}({task.get('taskId')})" for task in tasks[:_MAX_TASK_PREVIEW]]
    if len(tasks) > _MAX_TASK_PREVIEW:
        preview.append(f"…等共 {len(tasks)} 个任务")
    return "、".join(preview) or "（该计划当前没有任务）"


#: 模型偶发把工具调用写成正文（纯文本 JSON / ```json 代码块）而非结构化 ``tool_calls``；
#: 这里的字段名兼容主流 Agent 框架（LangChain / Anthropic 风格）以便还原真实调用。
_TEXT_TOOL_NAME_KEYS: tuple[str, ...] = ("name", "tool", "tool_name")
_TEXT_TOOL_ARG_KEYS: tuple[str, ...] = ("arguments", "parameters", "input", "args")
#: 命中这些键说明是协议信封 / 非工具对象，直接排除
_TEXT_TOOL_DENY_KEYS: frozenset[str] = frozenset({"finish_reason", "delta", "choices"})


def _text_tool_call(obj: Any) -> tuple[str, dict[str, Any]] | None:
    """把一个 JSON 对象识别为工具调用；仅当工具名属于当前可用工具时才认可。"""
    if not isinstance(obj, dict) or _TEXT_TOOL_DENY_KEYS & obj.keys():
        return None
    function = obj.get("function")
    if isinstance(function, dict):
        name, args = function.get("name"), function.get("arguments")
    else:
        name = next((obj[key] for key in _TEXT_TOOL_NAME_KEYS if isinstance(obj.get(key), str)), None)
        args = next((obj[key] for key in _TEXT_TOOL_ARG_KEYS if obj.get(key) is not None), None)
    if not isinstance(name, str) or name not in _TOOL_NAMES:
        return None
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    return name, args


def _close_truncated_json(raw: str) -> str | None:
    """给被截断的 JSON 片段补上缺失的闭合符，返回可解析字符串（仍不合法时返回 ``None``）。

    轻量模型把工具调用写成正文时，常在 JSON 中途停笔（如 ``{"planId": "abc`` 缺少引号与
    两层大括号）。这里统计未闭合的结构与字符串并补齐，使这类「半截调用」也能被还原。
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in raw:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None  # 括号不配对：不是单纯截断，放弃修复
            stack.pop()
    if not stack and not in_string:
        return None  # 结构已闭合，说明原本就不是合法 JSON
    suffix = '"' if in_string else ""
    for char in reversed(stack):
        suffix += "}" if char == "{" else "]"
    return raw + suffix


def _text_tool_calls(content: str) -> list[dict[str, Any]]:
    """从助手正文中还原「被写成文本的工具调用」（防御性兜底）。

    轻量模型有时不产出结构化 ``tool_calls``，而是把调用写成正文里的 JSON（含
    ```json 代码块），甚至在中途被截断。若按纯文本原样返回，用户的写/查请求会被静默
    吞掉；这里把可识别的调用（含截断片段修复后）还原为标准 ``tool_calls`` 继续编排。
    仅在工具名属于 :data:`_TOOL_NAMES` 时才认可，避免把普通说明文字误判为调用。
    """
    if not content or "{" not in content:
        return []
    decoder = json.JSONDecoder()
    recovered: list[dict[str, Any]] = []
    cursor = 0
    while True:
        start = content.find("{", cursor)
        if start < 0:
            break
        cursor = start + 1
        try:
            obj, _ = decoder.raw_decode(content, start)
        except ValueError:
            # 截断片段：尝试补齐闭合符后再解析
            closed = _close_truncated_json(content[start:])
            if closed is None:
                continue
            try:
                obj = json.loads(closed)
            except ValueError:
                continue
        parsed = _text_tool_call(obj)
        if parsed is None:
            continue
        name, args = parsed
        recovered.append(
            {
                "id": f"call_text_{len(recovered)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        )
    return recovered


def _mentions_tool(text: str, name: str) -> bool:
    """判断文本是否「独立」提及某工具名（前后不是 ASCII 标识符字符，避免子串误命中）。

    注意：不能用 ``str.isalnum()`` 判定边界——中文字符在 Python 里也算 alnum，会把
    「使用 get_plan 工具」这类正常提及误判为子串，因此只按 ASCII 标识符字符划界。
    """
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text) is not None


def _unparsed_tool_attempt(content: str) -> str | None:
    """识别「模型试图调用工具、但输出无法还原成 tool_calls」的正文，返回其目标工具名。

    两种情况会命中：

    1. 正文里出现 ``"name": "<已知工具>"``（参数被截断或结构不完整，无法还原）；
    2. 正文只写了参数对象（无 ``name`` 键）且尾部 JSON 明显被截断，同时正文恰好提到唯一一个工具名。

    这类答复绝不能当作最终结果直接返回（用户会看到一段「我要调用 XX」却什么都没发生），
    而应回填纠偏提示让模型改用结构化 ``tool_calls`` 重新输出。
    """
    if "{" not in content:
        return None
    for match in _TOOL_ATTEMPT_RE.finditer(content):
        name = match.group(1)
        if name in _ALL_TOOL_NAMES:
            return name
    brace = content.rfind("{")
    if brace < 0 or _close_truncated_json(content[brace:]) is None:
        return None
    mentioned = [name for name in sorted(_ALL_TOOL_NAMES) if _mentions_tool(content, name)]
    return mentioned[0] if len(mentioned) == 1 else None


def _content_to_text(content: Any) -> str:
    """把 assistant content（str / list / None）归一化为文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content)


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """解析 function call 的 arguments（可能是 JSON 字符串或已是对象）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ApiError(422, "LLM_FORMAT_ERROR", f"工具入参 JSON 解析失败：{exc}") from exc
        if not isinstance(parsed, dict):
            raise ApiError(422, "LLM_FORMAT_ERROR", "工具入参必须是 JSON 对象")
        return parsed
    return {}


def _estimate_chars(messages: list[dict[str, Any]]) -> int:
    """估算消息列表的字符规模（约 4 字符 ≈ 1 token）。"""
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += len(json.dumps(tool_calls, ensure_ascii=False, default=str))
    return total


class AgentRunner:
    """无状态编排器：由 HTTP 层注入 LLM 客户端、工具调度器与会话存储。"""

    def __init__(
        self,
        llm: LlmClient | None,
        dispatcher: ToolDispatcher,
        *,
        max_steps: int = 8,
        temperature: float = 0.2,
        store: SessionStore | None = None,
        max_context_chars: int = 96_000,
        knowledge: KnowledgeBaseService | None = None,
        memory: MemoryService | None = None,
        kb_auto_inject: bool = True,
        kb_inject_top_k: int = 3,
        memory_top_k: int = 4,
    ) -> None:
        self._llm = llm
        self._dispatcher = dispatcher
        self._max_steps = max_steps
        self._temperature = temperature
        self._store: SessionStore = store or InMemorySessionStore()
        self._max_context_chars = max_context_chars
        self._knowledge = knowledge
        self._memory = memory
        self._kb_auto_inject = kb_auto_inject
        self._kb_inject_top_k = kb_inject_top_k
        self._memory_top_k = memory_top_k
        # 仅暴露「当前真正可用」的工具：检索类工具在对应服务缺失时会被移除
        available = {"knowledge": knowledge, "memory": memory}
        self._tool_specs = [
            spec
            for spec in TOOL_SPECS
            if available.get(_SERVICE_BOUND_TOOLS.get(spec["function"]["name"], ""), True) is not None
        ]

    @property
    def tool_specs(self) -> list[dict[str, Any]]:
        """当前实际可调用的工具定义（检索类工具在服务缺失时已被剔除）。"""
        return self._tool_specs

    @property
    def tool_names(self) -> list[str]:
        """当前实际可调用的工具名（供 ``/agent/health`` 与前端提示）。"""
        return [spec["function"]["name"] for spec in self._tool_specs]

    @property
    def available(self) -> bool:
        """LLM 是否已配置（未配置时 Agent 端点给出明确错误提示）。"""
        return self._llm is not None

    # ------------------------------- 对外入口 -------------------------------

    async def run_plan(
        self,
        requirement: str,
        *,
        iteration_deadline: str | None = None,
        iteration_start_date: str | None = None,
        dev_member_count: int | None = None,
    ) -> AgentRunResult:
        """自然语言需求 → 完整计划（端到端一次编排）。

        同时按需求文本召回知识库片段（若启用）注入上下文：历史迭代文档 / 规范 / 约束
        往往直接决定拆解粒度与依赖关系，比让模型凭空推断更可靠。
        """
        instruction = build_plan_instruction(
            requirement,
            iteration_deadline=iteration_deadline,
            iteration_start_date=iteration_start_date,
            dev_member_count=dev_member_count,
        )
        kb_context, references = await self._retrieve_knowledge(requirement)
        messages = self._base_messages(kb_context) + [{"role": "user", "content": instruction}]
        result = await self._loop(messages)
        result.references = [KbHit(**hit) for hit in references]
        return result

    async def run_chat(
        self,
        session_id: str,
        message: str,
        *,
        defer_writes: bool = True,
        plan_id: str | None = None,
    ) -> AgentRunResult:
        """会话式对话：携带历史上下文继续编排（同会话串行化）。

        ``defer_writes`` 为真时，本轮识别到的写操作不会立即执行，而是随结果返回
        待确认动作；同时**覆盖式**刷新该会话的待确认槽位（新一轮对话会作废上一轮
        未确认的动作，避免陈旧提案被误执行）。

        ``plan_id`` 为前端上报的「当前计划」，以可信上下文注入本轮指令。

        每轮还会按当前输入召回**长期记忆**与**知识库片段**并注入独立 system 区块，
        对话结束后在后台提取本轮记忆（不阻塞响应）。
        """
        async with self._store.lock(session_id):
            history = await self._store.get(session_id) or []
            instruction = build_chat_instruction(message, plan_id)
            memory_context, memories = await self._retrieve_memories(message)
            kb_context, references = await self._retrieve_knowledge(message)
            messages = (
                self._base_messages(memory_context, kb_context) + history + [{"role": "user", "content": instruction}]
            )
            result = await self._loop(messages, defer_writes=defer_writes)
            result.references = [KbHit(**hit) for hit in references]
            result.memories = [MemoryItem(**item) for item in memories]

            # 仅保留「用户 / 最终助手」两条，工具中间态不入历史，控制上下文体积
            # 历史存原始（已消毒）文本，边界标记仅用于本回合送入模型的即时输入
            updated = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.content},
            ]
            await self._store.set(session_id, updated)
            if defer_writes:
                await self._store.set_pending(session_id, [item.model_dump() for item in result.pendingActions])
            # 后台提取长期记忆：LLM 调用有延迟，不占用本轮响应时间
            if self._memory is not None:
                self._memory.schedule_extract(session_id, message, result.content)
            return result

    async def run_chat_stream(
        self,
        session_id: str,
        message: str,
        *,
        defer_writes: bool = True,
        plan_id: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """流式版 :meth:`run_chat`：以 SSE 事件序列逐步产出（打字机式输出）。

        事件顺序：``meta``（会话 ID + 检索增强上下文）→ 若干 ``delta`` / ``tool_start`` /
        ``tool_result`` / ``pending`` → ``final``（完整结果）。事件名常量见
        ``app.constants.SSE_*``。

        与 :meth:`run_chat` **完全共享**同一套编排、会话持久化与后台记忆提取逻辑，
        仅把「一次性返回」改为「边编排边下发」，因此两条链路的业务语义严格一致。
        """
        async with self._store.lock(session_id):
            history = await self._store.get(session_id) or []
            instruction = build_chat_instruction(message, plan_id)
            memory_context, memories = await self._retrieve_memories(message)
            kb_context, references = await self._retrieve_knowledge(message)
            messages = (
                self._base_messages(memory_context, kb_context) + history + [{"role": "user", "content": instruction}]
            )
            # 首帧即携带检索增强上下文：前端可提前渲染「参考依据 / 召回记忆」卡片
            yield {"event": SSE_META, "data": {"sessionId": session_id, "references": references, "memories": memories}}

            result: AgentRunResult | None = None
            async for event in self._loop_stream(messages, defer_writes=defer_writes):
                if event["event"] == SSE_RESULT:
                    result = event["data"]
                    continue
                yield event
            if result is None:  # pragma: no cover - 编排必然产出结果或抛出异常
                raise ApiError(500, "INTERNAL_ERROR", "Agent 编排未产出结果")

            result.references = [KbHit(**hit) for hit in references]
            result.memories = [MemoryItem(**item) for item in memories]

            # 仅保留「用户 / 最终助手」两条，工具中间态不入历史，控制上下文体积
            updated = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": result.content},
            ]
            await self._store.set(session_id, updated)
            if defer_writes:
                await self._store.set_pending(session_id, [item.model_dump() for item in result.pendingActions])
            if self._memory is not None:
                self._memory.schedule_extract(session_id, message, result.content)
            response = AgentChatResponse(sessionId=session_id, **result.model_dump())
            yield {"event": SSE_FINAL, "data": response.model_dump()}

    async def confirm_actions(
        self, session_id: str, action_ids: list[str] | None = None, *, reject: bool = False
    ) -> tuple[list[ToolCallRecord], list[PendingAction], str | None]:
        """执行（或驳回）待确认写操作（两阶段写操作的第二阶段）。

        :param action_ids: 要执行的 ``actionId``；``None`` 表示确认全部
        :param reject: 驳回模式——作废全部待确认动作、不执行任何写操作（会话上下文保留）
        :return: ``(执行记录, 仍待确认的动作, 产生的计划 ID)``

        逐个**串行**执行（每次写操作都会触发引擎全量重规划，串行可避免写冲突）；
        单个动作失败不影响其余动作；未确认的动作保留在会话中，可再次确认。
        """
        async with self._store.lock(session_id):
            stored = await self._store.get_pending(session_id) or []
            pending = [PendingAction.model_validate(item) for item in stored if isinstance(item, dict)]

            if reject:
                # 驳回是幂等操作：即使当前没有待确认动作也返回成功语义
                await self._store.set_pending(session_id, [])
                METRICS.inc_counter("agent_confirm_total", {"outcome": "rejected"})
                logger.info("用户驳回待确认写操作：会话 %s，作废 %d 项", session_id, len(pending))
                return [], [], None

            if not pending:
                raise not_found("没有待确认的操作（可能已执行、已取消或会话已过期），请重新发起请求")

            wanted = set(action_ids) if action_ids is not None else None
            if wanted is not None:
                unknown = sorted(wanted - {item.actionId for item in pending})
                if unknown:
                    raise bad_request(f"待确认动作不存在：{'、'.join(unknown[:5])}")
            selected = [item for item in pending if wanted is None or item.actionId in wanted]
            remaining = [item for item in pending if wanted is not None and item.actionId not in wanted]

            records: list[ToolCallRecord] = []
            plan_id: str | None = None
            for item in selected:
                record, produced = await self._execute_action(item)
                records.append(record)
                # 任务级写操作的结果信封里 planId 嵌套在 plan 下，回落到动作自身携带的 planId
                plan_id = produced or item.planId or plan_id
                if not record.ok:
                    logger.warning("待确认动作执行失败：%s · %s", item.tool, record.error)

            await self._store.set_pending(session_id, [item.model_dump() for item in remaining])
            await self._append_execution_history(session_id, selected, records)
            METRICS.inc_counter("agent_confirm_total", {"outcome": "executed" if records else "empty"})
            return records, remaining, plan_id

    async def reset_session(self, session_id: str) -> bool:
        """清空指定会话（含待确认动作），返回是否存在。"""
        return await self._store.pop(session_id)

    async def complete(self, messages: list[dict[str, Any]], *, temperature: float | None = None) -> str:
        """单轮纯文本补全（不携带工具，供风险建议等「一次性生成」场景使用）。

        与 :meth:`_loop` 不同，这里不做 function calling 编排：只调用一次模型并把
        assistant 文本归一化返回；未配置 LLM 时抛出 :func:`llm_unavailable` 由调用方降级。
        """
        if self._llm is None:
            raise llm_unavailable()
        message = await self._llm.chat(
            messages,
            temperature=self._temperature if temperature is None else temperature,
        )
        return _content_to_text(message.get("content"))

    # ------------------------------- 内部实现 -------------------------------

    @staticmethod
    def _base_messages(*blocks: str | None) -> list[dict[str, Any]]:
        """系统提示 + 检索增强区块（长期记忆 / 知识库片段）。

        区块作为**独立 system 消息**注入而非拼接进主提示：主提示保持稳定（利于缓存命中），
        检索结果随每轮变化，且顺序固定（记忆在前、知识库在后），便于排查上下文内容。
        """
        messages: list[dict[str, Any]] = [{"role": "system", "content": ITERATION_PLANNER_SYSTEM_PROMPT}]
        for block in blocks:
            if block:
                messages.append({"role": "system", "content": block})
        return messages

    async def _retrieve_knowledge(self, query: str) -> tuple[str | None, list[dict[str, Any]]]:
        """召回知识库上下文与命中片段（未启用 / 失败时返回空）。"""
        if self._knowledge is None or not self._kb_auto_inject:
            return None, []
        return await self._knowledge.context_and_hits(query, top_k=self._kb_inject_top_k)

    async def _retrieve_memories(self, query: str) -> tuple[str | None, list[dict[str, Any]]]:
        """召回长期记忆上下文与记忆列表（未启用 / 失败时返回空）。"""
        if self._memory is None:
            return None, []
        return await self._memory.context_and_memories(query, top_k=self._memory_top_k)

    async def _loop(self, messages: list[dict[str, Any]], *, defer_writes: bool = False) -> AgentRunResult:
        """非流式编排：消费 :meth:`_loop_stream` 并取回终态结果。

        保持「单一实现」：流式与非流式共用同一套循环，避免两条链路语义漂移。
        """
        async for event in self._loop_stream(messages, defer_writes=defer_writes):
            if event["event"] == SSE_RESULT:
                return event["data"]
        raise ApiError(500, "INTERNAL_ERROR", "Agent 编排未产出结果")  # pragma: no cover

    async def _loop_stream(
        self, messages: list[dict[str, Any]], *, defer_writes: bool = False
    ) -> AsyncIterator[dict[str, Any]]:
        """编排循环的**流式实现**：边调用模型/工具边产出事件。

        产出 ``delta`` / ``tool_start`` / ``tool_result`` / ``pending`` 事件（可直接下发给
        前端），并以 ``SSE_RESULT`` 事件收尾（内部使用，携带 :class:`AgentRunResult`，
        由调用方转换为 ``final`` 或直接返回）。

        模型未实现 ``chat_stream`` 时自动回退为一次性调用：仅退化为「无逐字效果」，
        功能与语义不变。
        """
        if self._llm is None:
            raise llm_unavailable()

        streamer = getattr(self._llm, "chat_stream", None)
        records: list[ToolCallRecord] = []
        pending: list[PendingAction] = []
        plan_id: str | None = None
        #: 本轮**真实用户输入**：只在循环开始前取一次。纠偏提示是追加进 ``messages`` 的
        #: ``role=user`` 消息，若每步都重新取「最后一条 user 消息」，这些提示本身会成为下一轮
        #: 的判据语料（例如「需要补充什么信息」会被当成状态提问），导致纠偏连环误触发。
        current_user_text = _last_user_text(messages)
        false_claim_fixes = 0
        state_grounding_fixes = 0
        write_intent_fixes = 0

        for step in range(1, self._max_steps + 1):
            self._enforce_budget(messages)
            message: dict[str, Any] | None = None
            if streamer is None:
                # 模型未实现流式能力：一次性调用，并把全文作为单帧下发（功能不变，仅少了逐字效果）
                message = await self._llm.chat(messages, tools=self._tool_specs, temperature=self._temperature)
                text = strip_protocol_leak(_content_to_text(message.get("content")))
                if message.get("content") is not None:
                    message = {**message, "content": text}
                if text:
                    yield {"event": SSE_DELTA, "data": {"text": text}}
            else:
                async for chunk in streamer(messages, tools=self._tool_specs, temperature=self._temperature):
                    if chunk.get("type") == "content":
                        yield {"event": SSE_DELTA, "data": {"text": chunk["text"]}}
                    elif chunk.get("type") == "message":
                        message = chunk.get("message")
                if message is None:
                    raise ApiError(422, "LLM_FORMAT_ERROR", "LLM 流式响应未返回完整消息")

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                # 兜底：模型把工具调用写成了正文（纯文本 JSON / 代码块）而非结构化 tool_calls，
                # 若按文本原样返回，用户的请求会被静默吞掉；这里还原为真实调用继续编排。
                recovered = _text_tool_calls(strip_protocol_leak(_content_to_text(message.get("content"))))
                if recovered:
                    tool_calls = recovered
                    message = {"role": "assistant", "content": "", "tool_calls": recovered}
                    METRICS.inc_counter("agent_text_tool_recovered_total")
                    logger.warning(
                        "模型将工具调用写成正文，已兜底还原 %d 个调用：%s",
                        len(recovered),
                        "、".join(call["function"]["name"] for call in recovered),
                    )
            messages.append(self._canonical_assistant(message))

            if not tool_calls:
                # 无工具调用时正文即为最终答复：剥离协议字节（不依赖具体 LlmClient 实现）
                final_text = strip_protocol_leak(_content_to_text(message.get("content"))).strip()
                attempt = _unparsed_tool_attempt(final_text)
                if attempt is not None and step < self._max_steps:
                    # 正文是一次无法解析的工具调用尝试：不作为最终答复，纠偏后重试
                    # （对齐主流 Agent 框架的「输出解析失败 → 回填格式纠偏」策略）
                    METRICS.inc_counter("agent_text_tool_retry_total", {"tool": attempt})
                    logger.warning(
                        "模型把对 %s 的调用写成不可解析的正文，回填纠偏提示后重试（第 %d 步）", attempt, step
                    )
                    messages.append({"role": "user", "content": _TOOL_RETRY_HINT.format(tool=attempt)})
                    continue
                METRICS.observe("agent_steps", step, buckets=(1, 2, 3, 4, 5, 6, 8), help="Agent 收敛步数")
                if not pending and not any(record.ok and record.name in WRITE_TOOLS for record in records):
                    # 一致性校验（一）：本轮没有任何写操作，模型却宣称「已修改/操作后…」——这是
                    # **虚假完成**，用户会据此以为变更已生效。先纠偏重试，仍无效则用确定性提示覆盖。
                    if _WRITE_CLAIM_RE.search(final_text):
                        false_claim_fixes += 1
                        if false_claim_fixes == 1 and step < self._max_steps:
                            METRICS.inc_counter("agent_false_completion_total", {"action": "retry"})
                            logger.warning("模型在无写操作的情况下宣称已完成变更，回填纠偏提示后重试（第 %d 步）", step)
                            messages.append({"role": "user", "content": _FALSE_COMPLETION_HINT})
                            continue
                        METRICS.inc_counter("agent_false_completion_total", {"action": "notice"})
                        logger.warning("模型重复谎报写入完成，已用确定性提示替换本轮结论")
                        final_text = _FALSE_COMPLETION_NOTICE
                if not records and not state_grounding_fixes:
                    # 一致性校验（二）：用户问的是**当前计划状态**，模型却一次工具都没调用就给出数字
                    # ——这些数字只能来自长期记忆或历史对话，可能属于别的计划/更早时间点。纠偏一次。
                    if _is_state_question(current_user_text) and _HAS_FIGURE_RE.search(final_text):
                        state_grounding_fixes += 1
                        if step < self._max_steps:
                            METRICS.inc_counter("agent_ungrounded_state_answer_total", {"action": "retry"})
                            logger.warning("模型未调用工具即回答当前计划状态，回填纠偏提示后重试（第 %d 步）", step)
                            messages.append({"role": "user", "content": _STATE_GROUNDING_HINT})
                            continue
                        METRICS.inc_counter("agent_ungrounded_state_answer_total", {"action": "unresolved"})
                        logger.warning("模型重复在无工具调用下回答状态问题，本轮结论未获得工具支撑")
                if (
                    not pending
                    # 只看「是否**尝试过**写工具」而不看成败：写工具报错时模型已通过 tool 消息得知失败，
                    # 此时如实回复「修改失败：任务不存在」是诚实收敛，不应再被判为空转并覆盖掉错误说明。
                    and not any(record.name in WRITE_TOOLS for record in records)
                    and _has_write_intent(current_user_text)
                    # 用户消息本身是提问（如「人员负荷需要调整吗？」）时不强制写操作——那是询问而非指令
                    and not _is_state_question(current_user_text)
                    # 模型反问澄清（如「请提供计划 ID」）是信息不足时的合法收敛，不应判为空转
                    and not _is_clarification_request(final_text)
                ):
                    # 一致性校验（三）：用户本轮明确要求改动，模型却既没登记任何写动作、也没反问澄清，
                    # 而是把写指令当作状态查询作答——修改请求被静默空转（用户以为已受理，实际什么都没发生）。
                    write_intent_fixes += 1
                    if write_intent_fixes == 1 and step < self._max_steps:
                        METRICS.inc_counter("agent_write_intent_noop_total", {"action": "retry"})
                        logger.warning("用户要求改动但本轮无任何写动作且未澄清，回填纠偏提示后重试（第 %d 步）", step)
                        messages.append({"role": "user", "content": _WRITE_INTENT_HINT})
                        continue
                    METRICS.inc_counter("agent_write_intent_noop_total", {"action": "unresolved"})
                    logger.warning("模型重复忽略用户的写指令，本轮未登记任何待确认写动作")
                    final_text = _FALSE_COMPLETION_NOTICE

                # 输出护栏：模型答复的最终事实以**服务端校验结果**为准（对齐 OpenAI Agents SDK 的
                # ``outputGuardrails`` 与 CRITIC 的「结论须有外部凭据」）。此处必须在 ``pending``
                # 分支**之外**执行——早期实现只在「无任何写操作」的分支里校验，于是「登记为待确认后
                # 照样宣称已完成、并给出并不存在的 planId」这更常见、更危险的谎报被整条漏过。
                final_text = _enforce_authoritative_reply(
                    final_text, records=records, plan_id=plan_id, pending=pending
                )

                if pending:
                    # 本回合登记过写操作：以服务端确定性摘要收尾，保证展示清单与真实登记项一致。
                    # 注意只增量下发**摘要**：模型叙述若在生成过程中已逐字下发过，这里再带上就会重复。
                    pending = _dedupe_pending(pending)
                    summary = _describe_pending(pending)
                    final_text = f"{final_text}\n\n{summary}" if final_text else summary
                    METRICS.inc_counter("agent_runs_total", {"outcome": "pending_confirmation"})
                    yield {"event": SSE_DELTA, "data": {"text": f"\n\n{summary}"}}
                    yield {"event": SSE_PENDING, "data": {"actions": [item.model_dump() for item in pending]}}
                else:
                    METRICS.inc_counter("agent_runs_total", {"outcome": "completed"})
                    if not final_text:
                        # 既无工具调用也无文本：明确告知而不是返回空白，避免用户以为请求被吞
                        METRICS.inc_counter("agent_runs_total", {"outcome": "empty_reply"})
                        logger.warning(
                            "Agent 收敛但未产出任何文本与工具调用（model=%s）", getattr(self._llm, "name", "?")
                        )
                        final_text = "模型本轮未产出可展示的内容，请重试或换一种说法描述你的需求。"
                yield {
                    "event": SSE_RESULT,
                    "data": AgentRunResult(
                        content=final_text,
                        planId=plan_id,
                        toolCalls=records,
                        steps=step,
                        pendingActions=pending,
                    ),
                }
                return

            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                yield {"event": SSE_TOOL_START, "data": {"name": name}}

            # 同一条消息内的多个工具调用并行执行，结果按原序回填（OpenAI 协议要求顺序一致）
            outcomes = await asyncio.gather(
                *(self._run_tool_call(index, call, defer_writes) for index, call in enumerate(tool_calls))
            )
            for record, tool_message, produced_plan_id, pending_action in outcomes:
                records.append(record)
                plan_id = produced_plan_id or plan_id
                if tool_message is not None:
                    messages.append(tool_message)
                if pending_action is not None:
                    pending.append(pending_action)
                # result 可能很大（整份计划），此处只下发轻量字段，完整明细由 final 事件补齐
                yield {
                    "event": SSE_TOOL_RESULT,
                    "data": {
                        "name": record.name,
                        "arguments": record.arguments,
                        "ok": record.ok,
                        "error": record.error,
                    },
                }

            if pending:
                # 已登记写操作：**不结束本回合**。用户可能一次提出多处改动，必须让模型有机会
                # 把其余改动一并登记（早期实现在此处直接 return，导致「改责任人 + 改工时」这类
                # 复合请求只登记第一条、其余被静默丢弃）。收敛判定统一交给「模型不再请求工具」。
                pending = _dedupe_pending(pending)

        if pending:
            # 未收敛但已登记变更：给出确认卡片比直接报错更有用（用户至少能核对并执行已识别部分）
            METRICS.inc_counter("agent_runs_total", {"outcome": "pending_confirmation"})
            summary = _describe_pending(pending)
            yield {"event": SSE_PENDING, "data": {"actions": [item.model_dump() for item in pending]}}
            yield {
                "event": SSE_RESULT,
                "data": AgentRunResult(
                    content=summary,
                    planId=plan_id,
                    toolCalls=records,
                    steps=self._max_steps,
                    pendingActions=pending,
                ),
            }
            return
        METRICS.inc_counter("agent_runs_total", {"outcome": "max_steps"})
        raise ApiError(
            422,
            "AGENT_MAX_STEPS",
            f"Agent 在 {self._max_steps} 步内未收敛，请细化需求描述或减少需变更的任务数量",
        )

    def _enforce_budget(self, messages: list[dict[str, Any]]) -> None:
        """上下文超预算时，从最旧的「用户回合」开始裁剪（system 与当前输入始终保留）。"""
        if self._max_context_chars <= 0:
            return
        trimmed = False
        while _estimate_chars(messages) > self._max_context_chars:
            user_indices = [i for i, message in enumerate(messages) if message.get("role") == "user"]
            if len(user_indices) <= 1:
                break  # 仅剩当前用户输入，无法继续裁剪
            start, end = user_indices[0], user_indices[1]
            del messages[start:end]
            trimmed = True
        if trimmed:
            METRICS.inc_counter("agent_context_trimmed_total")

    @staticmethod
    def _canonical_assistant(message: dict[str, Any]) -> dict[str, Any]:
        """规范 assistant 消息，保证 tool_calls 结构完整（OpenAI 协议要求 id 必填）。"""
        canonical: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
        tool_calls = message.get("tool_calls")
        if tool_calls:
            canonical["tool_calls"] = tool_calls
        return canonical

    async def _run_tool_call(
        self, index: int, call: Any, defer_writes: bool = False
    ) -> tuple[ToolCallRecord, dict[str, Any] | None, str | None, PendingAction | None]:
        """校验 + 执行单个工具调用（任何异常都以错误结果回填，绝不中断整轮编排）。

        并行执行（``asyncio.gather``）下，单个子任务抛出非预期异常会导致整批失败并中断
        Agent 循环，因此这里做**兜底捕获**：把任何意外错误降级为一条错误工具结果，
        交由模型自行纠偏。

        返回 ``(记录, 回填消息, 产生的 planId, 待确认动作)``：当 ``defer_writes`` 为真且
        命中写类工具时，不执行而是返回待确认动作（回填消息为 ``None``）。
        """
        call_id = f"call_{index}"
        name = ""
        arguments: dict[str, Any] = {}
        try:
            if not isinstance(call, dict):
                raise ApiError(422, "LLM_FORMAT_ERROR", "工具调用结构非法（应为对象）")
            call_id = str(call.get("id") or f"call_{index}")
            function = call.get("function")
            if not isinstance(function, dict):
                raise ApiError(422, "LLM_FORMAT_ERROR", "工具调用缺少 function 定义")
            name = str(function.get("name") or "")
            return await self._dispatch_validated(call_id, name, function.get("arguments"), defer_writes)
        except ApiError as exc:
            record, tool_message = self._error_result(call_id, name, arguments, exc.code, exc.message)
            return record, tool_message, None, None
        except Exception as exc:  # noqa: BLE001 - 兜底：任何意外错误都不应中断编排
            logger.exception("工具调用出现未预期异常：%s", name or "(未知)")
            record, tool_message = self._error_result(
                call_id, name, arguments, "TOOL_EXECUTION_ERROR", f"工具执行异常：{exc}"
            )
            return record, tool_message, None, None

    async def _dispatch_validated(
        self, call_id: str, name: str, raw_arguments: Any, defer_writes: bool
    ) -> tuple[ToolCallRecord, dict[str, Any] | None, str | None, PendingAction | None]:
        """校验工具名与入参后执行（或登记为待确认写操作）。"""
        arguments: dict[str, Any] = {}
        if name not in _TOOL_NAMES:
            record, tool_message = self._error_result(
                call_id, name, arguments, "VALIDATION_ERROR", f"未知工具：{name or '(空)'}"
            )
            return record, tool_message, None, None
        arguments = _parse_arguments(raw_arguments)
        # 入参是模型生成的自由文本：可能把「送入模型时加的不可信边界」整段回填进来，
        # 在落库 / 展示 / 审计之前统一还原，避免脚手架文本变成业务数据。
        arguments = unwrap_arguments(arguments)
        missing = [key for key in _REQUIRED[name] if arguments.get(key) in (None, "")]
        if missing:
            message = f"工具 {name} 缺少必填参数：{', '.join(missing)}"
            record, tool_message = self._error_result(call_id, name, arguments, "VALIDATION_ERROR", message)
            return record, tool_message, None, None
        if defer_writes and name in WRITE_TOOLS:
            return await self._register_pending(call_id, name, arguments)
        record, tool_message, plan_id = await self._execute(call_id, name, arguments)
        return record, tool_message, plan_id, None

    async def _register_pending(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolCallRecord, dict[str, Any] | None, str | None, PendingAction | None]:
        """把写类工具登记为待确认动作（不落库，但先校验目标任务真实存在）。

        为什么登记前要先查计划：``taskId`` 由模型生成，而模型对「第一个任务」这类指代只能靠
        猜测，极易编造出一个不存在的 ID 或误用 ``task_add`` 新建同名任务。若放任其进入确认卡片，
        用户看到的是「修改任务 1b29d7f4… 的 责任人 → pm-01」——既无法核对改的是哪个任务，
        确认后还会以 404 失败。因此在登记阶段就查一次计划：目标不存在则把「计划现有任务」
        回填给模型令其自纠；目标存在则把任务名写进卡片，让确认环节真正可核对。
        """
        label = describe_action(name, arguments)
        if name in _REQUIREMENT_WRITE_TOOLS:
            rejected = _reject_question_as_requirement(name, arguments)
            if rejected is not None:
                record, tool_message = self._error_result(call_id, name, arguments, "VALIDATION_ERROR", rejected)
                return record, tool_message, None, None
        if name in _TASK_TARGET_WRITE_TOOLS:
            task_name, error = await self._resolve_task_target(arguments)
            if error is not None:
                record, tool_message = self._error_result(call_id, name, arguments, "NOT_FOUND", error)
                return record, tool_message, None, None
            if task_name:
                label = describe_action(name, arguments, task_name=task_name)
        plan_ref = arguments.get("planId")
        action = PendingAction(
            actionId=f"act_{uuid.uuid4().hex[:12]}",
            tool=name,
            label=label,
            arguments=arguments,
            planId=plan_ref if isinstance(plan_ref, str) and plan_ref else None,
        )
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name, "ok": "deferred"})
        record = ToolCallRecord(
            name=name,
            arguments=arguments,
            ok=True,
            result={"status": "PENDING_CONFIRMATION", "actionId": action.actionId},
        )
        # 回填一条**规范的 tool 结果**：OpenAI 协议要求每个 tool_call 都有对应的 tool 消息，
        # 缺了它下一轮请求就是非法历史（部分网关会直接报错）；同时借这条消息提醒模型
        # 「把本轮其余改动也一并登记」，避免复合请求只登记第一条。
        tool_message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _PENDING_TOOL_RESULT.format(label=label),
        }
        return record, tool_message, None, action

    async def _resolve_task_target(self, arguments: dict[str, Any]) -> tuple[str | None, str | None]:
        """校验 ``taskId`` 是否属于该计划，并返回任务名。

        返回 ``(任务名 | None, 错误消息 | None)``：
        - 找到任务 → ``(名称, None)``（名称为空串时仍视为找到）；
        - 计划不存在 / 任务不在计划中 → ``(None, 可回填给模型的纠偏提示)``；
        - 引擎暂时不可达 → ``(None, None)``：**不阻断**用户流程，退化为无名称的登记，
          真实错误会在确认执行时暴露（此处仅计数与告警，符合「降级不阻断主链路」约定）。
        """
        plan_id = arguments.get("planId")
        task_id = arguments.get("taskId")
        if not isinstance(plan_id, str) or not plan_id or not isinstance(task_id, str) or not task_id:
            return None, None  # 缺参由 `_REQUIRED` 校验先行拦截，这里无需重复报错
        try:
            tasks = await self._dispatcher.fetch_tasks(plan_id)
        except ApiError as exc:
            if exc.code == "NOT_FOUND" or exc.status_code == 404:
                return None, f"计划 {plan_id} 不存在，请先调用 list_plans 确认正确的 planId。"
            logger.warning("登记待确认写操作前查询计划 %s 失败，降级为无校验登记：%s", plan_id, exc.message)
            METRICS.inc_counter(
                "agent_pending_resolve_degraded_total",
                {"reason": "engine_error"},
                help="待确认写操作登记前的任务校验降级次数（引擎不可达时不阻断流程）",
            )
            return None, None
        for task in tasks:
            if str(task.get("taskId")) == task_id:
                return str(task.get("taskName") or ""), None
        return None, (
            f"任务 {task_id} 不在计划 {plan_id} 中，未登记该操作。"
            f"计划现有任务（名称/ID）：{_task_preview(tasks)}。"
            "请先调用 get_plan 取得真实 taskId 后重试；若确实需要新建任务，请改用 task_add。"
        )

    @staticmethod
    def _error_result(
        call_id: str, name: str, arguments: dict[str, Any], code: str, message: str
    ) -> tuple[ToolCallRecord, dict[str, Any]]:
        """构造一条错误工具结果（回填给模型，供其纠偏）。"""
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name or "unknown", "ok": "false"})
        record = ToolCallRecord(name=name, arguments=arguments, ok=False, error=message)
        payload = json.dumps({"error": code, "message": message}, ensure_ascii=False)
        return record, {"role": "tool", "tool_call_id": call_id, "content": payload}

    async def _execute(
        self, call_id: str, name: str, arguments: dict[str, Any]
    ) -> tuple[ToolCallRecord, dict[str, Any], str | None]:
        """执行工具并把结果 / 错误回填给模型。"""
        result = await self._dispatcher.dispatch(name, arguments)
        METRICS.inc_counter("agent_tool_calls_total", {"tool": name, "ok": "true"})
        record = ToolCallRecord(name=name, arguments=arguments, ok=True, result=result)
        plan_id = result.get("planId") if isinstance(result, dict) else None
        payload = _dump(result)
        return record, {"role": "tool", "tool_call_id": call_id, "content": payload}, plan_id

    async def _execute_action(self, action: PendingAction) -> tuple[ToolCallRecord, str | None]:
        """执行单个已确认的写操作（失败降级为错误记录，不抛出）。"""
        try:
            result = await self._dispatcher.dispatch(action.tool, action.arguments)
        except ApiError as exc:
            METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "false"})
            return ToolCallRecord(name=action.tool, arguments=action.arguments, ok=False, error=exc.message), None
        except Exception as exc:  # noqa: BLE001 - 兜底：单个动作失败不应影响其余动作
            logger.exception("待确认动作执行异常：%s", action.tool)
            METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "false"})
            return (
                ToolCallRecord(name=action.tool, arguments=action.arguments, ok=False, error=f"工具执行异常：{exc}"),
                None,
            )
        METRICS.inc_counter("agent_tool_calls_total", {"tool": action.tool, "ok": "true"})
        plan_id = result.get("planId") if isinstance(result, dict) else None
        return ToolCallRecord(name=action.tool, arguments=action.arguments, ok=True, result=result), plan_id

    async def _append_execution_history(
        self, session_id: str, selected: list[PendingAction], records: list[ToolCallRecord]
    ) -> None:
        """把确认执行的结果写入会话历史（供后续追问引用，历史只存可读摘要）。"""
        if not records:
            return
        lines: list[str] = []
        for item, record in zip(selected, records):
            outcome = "已执行" if record.ok else f"失败（{record.error or '未知原因'}）"
            lines.append(f"- {item.label}：{outcome}")
        summary = "用户已确认并执行以下写操作：\n" + "\n".join(lines)
        history = await self._store.get(session_id) or []
        await self._store.set(session_id, history + [{"role": "assistant", "content": summary}])


def _dedupe_pending(pending: list[PendingAction]) -> list[PendingAction]:
    """同一轮内模型可能重复提交同一写操作，按「工具 + 入参」去重（保留首次出现）。"""
    seen: set[str] = set()
    unique: list[PendingAction] = []
    for item in pending:
        key = f"{item.tool}|{json.dumps(item.arguments, ensure_ascii=False, sort_keys=True, default=str)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _describe_pending(pending: list[PendingAction]) -> str:
    """确定性生成「待确认」摘要文本（不依赖模型输出，保证降级场景下也可读）。"""
    lines = [f"{index}. {item.label}" for index, item in enumerate(pending, start=1)]
    return (
        f"本次拟执行以下 {len(pending)} 项写操作，**需您确认后才会真正落库**：\n"
        + "\n".join(lines)
        + "\n\n点击确认后我将立即执行，并由算法引擎完成全量重规划。"
    )


def _dump(value: Any) -> str:
    """把工具结果序列化为回填文本并按上限截断。"""
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _MAX_TOOL_RESULT_CHARS:
        return text[:_MAX_TOOL_RESULT_CHARS] + "\n…（结果过长已截断）"
    return text


__all__ = ["AgentRunner"]