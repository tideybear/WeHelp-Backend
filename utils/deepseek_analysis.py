# -*- coding: utf-8 -*-
"""DeepSeek 对话封装。

对外暴露一个 `chat()` 函数，传入 messages 即可获得模型回复。
"""
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Union

from openai import OpenAI

logger = logging.getLogger(__name__)

# 默认配置
DEFAULT_API_KEY = 'sk-13d51c0d2de04517a2887b05bc24886c'
DEFAULT_BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-flash'
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7

# 复用同一个 client，避免每次调用都新建连接
client = OpenAI(
    api_key=os.environ.get('DEEPSEEK_API_KEY', DEFAULT_API_KEY),
    base_url=os.environ.get('DEEPSEEK_BASE_URL', DEFAULT_BASE_URL),
)


def _normalize_messages(
    messages: Union[str, Dict[str, Any], Iterable[Union[str, Dict[str, Any]]]],
) -> List[Dict[str, Any]]:
    """把多种入参形态统一成 OpenAI 的 messages 列表。

    支持：
    - str：作为一条 user 消息
    - dict：单条消息
    - List[dict]：多条消息
    - List[str]：多条 user 消息
    """
    if messages is None:
        raise ValueError('messages 不能为空')

    if isinstance(messages, str):
        return [{'role': 'user', 'content': messages}]

    if isinstance(messages, dict):
        return [messages]

    if isinstance(messages, (list, tuple)):
        normalized: List[Dict[str, Any]] = []
        for item in messages:
            if isinstance(item, str):
                normalized.append({'role': 'user', 'content': item})
            elif isinstance(item, dict):
                normalized.append(item)
            else:
                raise ValueError(f'messages 中的元素类型不支持: {type(item)!r}')
        if not normalized:
            raise ValueError('messages 不能为空')
        return normalized

    raise ValueError(f'不支持的 messages 类型: {type(messages)!r}')


def chat(
    messages: Union[str, Dict[str, Any], Iterable[Union[str, Dict[str, Any]]]],
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = DEFAULT_TEMPERATURE,
    stream: bool = False,
    top_p: Optional[float] = None,
    thinking: Optional[Union[str, Dict[str, Any]]] = None,
    **kwargs: Any,
) -> Union[str, Iterable[str]]:
    """调用 DeepSeek 对话接口。

    Args:
        messages: 消息内容，支持字符串 / 单条 dict / 消息列表
        model: 模型名，默认 'deepseek-flash'
        max_tokens: 最大生成 token 数（推理模型需要留足空间给思维链）
        temperature: 采样温度
        stream: 是否流式返回
        top_p: 核采样，可选
        thinking: 思考模式控制。传入 'disabled' 关闭思维链（适合表格摘要、
                 结构化提取等任务），'enabled' 开启，None 使用服务端默认
        **kwargs: 透传给 OpenAI client 的其它参数

    Returns:
        非流式返回回复文本 str；流式返回文本片段迭代器
    """
    payload = _normalize_messages(messages)

    params: Dict[str, Any] = {
        'model': model,
        'messages': payload,
        'max_tokens': max_tokens,
        'temperature': temperature,
        'stream': stream,
    }
    if top_p is not None:
        params['top_p'] = top_p

    if thinking is not None:
        if isinstance(thinking, str):
            thinking = {'type': thinking}
        params['extra_body'] = {'thinking': thinking}

    params.update(kwargs)

    logger.debug('调用 DeepSeek: model=%s, messages=%s 条', model, len(payload))
    try:
        response = client.chat.completions.create(**params)
    except Exception as exc:
        logger.error('DeepSeek 调用失败: %s', exc, exc_info=True)
        raise

    if stream:
        def _iter():
            for chunk in response:
                delta = chunk.choices[0].delta
                content = getattr(delta, 'content', None)
                if content:
                    yield content

        return _iter()

    return _extract_content(response)


def _extract_content(response: Any) -> str:
    """从响应中提取正文。

    推理模型（如 deepseek-flash）会先产出 reasoning_content 思维链，
    若 max_tokens 不足以覆盖思维链，正文 content 会是空字符串。
    此时回退到思维链内容，避免调用方拿到空结果。
    """
    choice = response.choices[0]
    message = choice.message
    content = getattr(message, 'content', None)
    if content:
        return content

    reasoning = getattr(message, 'reasoning_content', None)
    finish_reason = getattr(choice, 'finish_reason', None)
    if reasoning:
        logger.warning(
            '模型未返回正文（finish_reason=%s），回退返回思维链内容', finish_reason
        )
        return reasoning
    return ''