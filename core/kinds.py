"""记忆条目的类型：用正则猜，不调模型（M7）。

类型只在三个地方有用：

* `plan` / `anniversary` 有到期时间，会走提醒；
* `relation` / `intimacy` 是"暖"的记忆，生气的时候先别拿出来；
* 其余（`preference` / `fact`）只影响展示和排序。

用规则而不是 LLM，是因为这层只需要"大致不出错"：猜错了最坏也只是
生气时少说一句、或者一条计划没进提醒列表（用户仍能用 `/kahn memory add`
手动补 `due_ts`）。多一次模型调用换这点准确度不划算。
"""

from __future__ import annotations

import re

PLAN = "plan"
PREFERENCE = "preference"
RELATION = "relation"
ANNIVERSARY = "anniversary"
INTIMACY = "intimacy"
FACT = "fact"

ALL_KINDS = (PLAN, PREFERENCE, RELATION, ANNIVERSARY, INTIMACY, FACT)

# 生气时先不拿出来说的类型（F19 / emotion-design §5）。冷冰冰的事实照常。
WARM_KINDS = frozenset({RELATION, INTIMACY})

# 顺序有意义：先认"日子"，再认"人"，最后才认"打算/口味"。
# 例："我妈下周生日" 应该落 anniversary 而不是 plan 或 relation。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (ANNIVERSARY, re.compile(r"生日|纪念日|周年|忌日|结婚|在一起")),
    (INTIMACY, re.compile(r"想你|爱你|喜欢你|抱抱|亲亲|贴贴|宝贝|老婆|老公|媳妇")),
    (
        RELATION,
        re.compile(
            r"我(妈|爸|哥|姐|弟|妹|爷爷|奶奶|外婆|外公|朋友|同学|同事|老板|室友|师父)"
            r"|家里人|女朋友|男朋友|对象|孩子"
        ),
    ),
    (
        PLAN,
        re.compile(
            r"下周|下个月|明天|后天|周一|周二|周三|周四|周五|周六|周日|周天"
            r"|打算|计划|约好|答应|准备(去|要|做)|要(去|来|做|交|参加)"
            r"|考试|体检|出差|面试|开会|交房租|还款|发工资|放假|开学"
        ),
    ),
    (PREFERENCE, re.compile(r"喜欢|讨厌|爱吃|不爱吃|不吃|过敏|口味|习惯|受不了|最怕")),
)


def infer_kind(text: str) -> str:
    """按内容猜一个 kind；猜不出来就是 fact。"""
    value = (text or "").strip()
    if not value:
        return FACT
    for kind, pattern in _RULES:
        if pattern.search(value):
            return kind
    return FACT


def is_warm(kind: str) -> bool:
    """是不是"暖"的那一类（生气时会被暂时藏起来）。"""
    return (kind or "") in WARM_KINDS
