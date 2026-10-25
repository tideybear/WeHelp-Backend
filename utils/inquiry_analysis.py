# -*- coding: utf-8 -*-
"""问诊分析框架（骨架）。"""
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.deepseek_analysis import chat  # noqa: E402

# TODO: 在这里补充前提逻辑 / 系统提示词
SYSTEM_PROMPT = '你是一个优秀的python工程师和数据库工程师'


def load_table_dict() -> str:
    """读取表结构文件（utils/table_dict）内容。"""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'table_dict')
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


def build_messages(
    question: str,
    context: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """组装发给模型的消息列表。"""
    messages: List[Dict[str, Any]] = []
    if SYSTEM_PROMPT:
        messages.append({'role': 'system', 'content': SYSTEM_PROMPT})

    content = question
    if context:
        content = f'{question}\n\n以下是需要参考的上下文数据：\n{context}'
    messages.append({'role': 'user', 'content': content})
    return messages


def analyze(
    messages: List[Dict[str, Any]],
    **kwargs: Any,
) -> str:
    """调用 DeepSeek 完成问诊分析，返回模型回复文本。"""
    return chat(messages, **kwargs)


if __name__ == '__main__':
    #问答拼接步骤：
    question1 = """帮我分析一下这行数据，那你先记住我的数据库表结构数据主表，我将数据给到你，你分析每行数据，并生成每行数据分析后的可执行insert语句给到我，事实求是，我不需要听分析或者推断内容，不确定数据打？即可，我要写入数据库中"""
    #第一步，获取数据库表结构数据，表结构记录在table_dict文件中
    table_dict = load_table_dict()

    #第二步，把表结构拼接到要发出的 messages 中
    messages = build_messages(question1, context=table_dict)

    #第三步，发送消息给模型，并获取回复
    question2 = """测试数据：日期	业务员	姓名	邮箱	公司	询盘内容	是否回复	是否成交	有回复未成交原因
2025/2/15	黄燕智	Chris Huntley	christopherbarrie.huntley@ge.com		"Good evening,
Based on the information supplied on your website, I cannot determine whether Shenler supply relays into the aerospace industry and if so, what QMS certification is held to support industry requirements.

If possible, I would appreciate further information around this requests.

Regards,
Chris"	否	否	没有航空相关认证"""
    messages = build_messages(messages, context=question2)


    result = analyze(messages)
    print(result)