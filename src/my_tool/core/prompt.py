"""Prompt 组装。模板可来自数据库（管理后台可编辑），也可回退到内置默认。

    三件事必须分清，否则"改个提示词就把系统改坏"是迟早的事：

      ① 可编辑部分（system prompt 主体、user 模板）—— 存在 prompt_templates 表，
         管理员在后台改，改完立即生效；
      ② 强制部分（CITATION_RULES）—— 代码里固化，后台改不到。
         因为它是**系统契约**而非话术：护栏按 [1][2] 角标核对引用，
         模型不标角标，护栏就只能判"引用缺失"，答案会全部降级为 partial；
      ③ 版本号 —— 每次保存产生新版本，写进 chat_messages.prompt_version。
         答案出问题时必须能回答"当时用的是哪版模板"。
"""

from dataclasses import dataclass
from typing import Sequence

from .entities import RetrievalHit


# =============================================================================
# 【模块 10 · Prompt 模板】目标文件：src/my_tool/core/prompt.py
# ★核心模块：模板的**唯一**组装点。改 prompt 只改这里与数据库，别在别处拼字符串。
# =============================================================================

# 内置默认。只在两种情况下用到：数据库里没有模板（首次启动）、以及后台"恢复默认"。
DEFAULT_SYSTEM_PROMPT = """你是公司薪酬核对制度助手，只根据提供的制度条文回答，不确定时明确说"制度中未找到"，回答末尾附引用来源，不得编造报销标准。"""

DEFAULT_USER_TEMPLATE = """【资料】
{context}

【问题】
{question}

请依据上面的资料作答。"""

# 系统强制追加在 system prompt 之后，后台不可编辑。理由见模块头部注释 ②。
CITATION_RULES = """
【回答格式（系统强制，不随模板调整）】
1. 每一个结论句后必须标注来源角标，格式为 [1]、[2]，角标对应【资料】的编号；不要写成 (1) 或 ①。
2. 金额、比例、基数、天数、条号必须从【资料】逐字照抄，禁止换算、合并、四舍五入或自行推算。
3. 【资料】没有给出的数字或结论，直接说明"制度中未找到"，不要用常识或其他制度补足。"""

# 内置模板的版本标签。数据库模板用 "tpl.v{版本号}"，两者不会混淆。
BUILTIN_VERSION = "builtin.v1"

# 兼容旧引用（manifest / 旧代码里还在用这个名字）
PROMPT_VERSION = BUILTIN_VERSION

# user 模板必须包含的占位符。缺了就是 KeyError，事故现场会出现在员工页而不是后台
REQUIRED_PLACEHOLDERS = ("{context}", "{question}")


@dataclass(frozen=True)
class PromptTemplate:
    """一份模板快照。frozen 是为了让"取到的这一份"在整个请求里不会变。"""

    version: int
    name: str
    system_prompt: str
    user_template: str

    @property
    def label(self) -> str:
        """写进 chat_messages.prompt_version 的标签。"""
        return f"tpl.v{self.version}" if self.version > 0 else BUILTIN_VERSION


BUILTIN_TEMPLATE = PromptTemplate(
    version=0,
    name="内置默认",
    system_prompt=DEFAULT_SYSTEM_PROMPT,
    user_template=DEFAULT_USER_TEMPLATE,
)


def build_prompt(question: str, hits: Sequence[RetrievalHit],
                 template: PromptTemplate | None = None
                 ) -> tuple[list[dict[str, str]], str, str]:
    """
    组装 messages，返回 (messages, context, prompt_version)。

    · context 同时返回，因为护栏要拿同一份文本做数字溯源复核 ——
      如果护栏自己再拼一遍上下文，两边稍微不一致就会出现"明明有依据却判未溯源"。
    · prompt_version 也返回：调用方要把它写进 chat_messages，用于事后追溯。

    资料格式刻意带上「编号 + 文档 + 条款 + chunk_id」：
      编号给模型引用；条款号给人核对；chunk_id 给系统回溯到 L0 原文。
    """
    tpl = template or BUILTIN_TEMPLATE

    blocks: list[str] = []
    for i, h in enumerate(hits, start=1):
        c = h.chunk
        header = f"[{i}] 《{c.doc_title}》{c.clause_range or c.heading_path}（{c.chunk_id}）"
        blocks.append(f"{header}\n{c.content}")
    context = "\n\n".join(blocks) if blocks else "（无可用资料）"

    system_text = (tpl.system_prompt.strip() + "\n" + CITATION_RULES).strip()
    # 模板里写错的占位符不该让整个问答炸掉：只替换已知的两个，
    # 其余花括号原样留着（可能是有意的字面量）。
    user_text = (tpl.user_template
                 .replace("{context}", context)
                 .replace("{question}", question))

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": user_text},
    ]
    return messages, context, tpl.label


def validate_template(system_prompt: str, user_template: str) -> str:
    """
    保存前校验，返回缺失的占位符描述（空字符串表示通过）。

    为什么必须在保存时校验而不是运行时兜底：
      缺 {context} 的后果是模型拿到了问题但没有任何资料，它会直接开始编 ——
      而这发生在员工页，不是后台，管理员根本不知道自己改坏了什么。
    """
    missing = [p for p in REQUIRED_PLACEHOLDERS if p not in (user_template or "")]
    if missing:
        return "、".join(missing)
    if len((system_prompt or "").strip()) < 20:
        return "system prompt 过短（至少 20 字）"
    if len(system_prompt) > 8000 or len(user_template) > 4000:
        return "模板过长"
    return ""
