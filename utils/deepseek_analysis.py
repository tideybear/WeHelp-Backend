# -*- coding: utf-8 -*-
"""DeepSeek 对话封装。

对外暴露：
- `chat()`：传入 messages 即可获得模型回复。
- `fetch_webpage()`：借助 DeepSeek 的 /v1/tools/fetch 抓取网页正文。
- `search_web()`：借助 DeepSeek 的 /v1/tools/search 做联网搜索。
"""
import json
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Union

from openai import OpenAI
import requests

logger = logging.getLogger(__name__)

# 调试打印开关：置为 True 时打印每次调用的入参与返回
DEBUG_PRINT = True

# 调用计数器，用于在打印中标明第几轮调用
_call_counter = 0


def _debug_print(title: str, content: Any) -> None:
    """打印调试信息（标题 + 内容）。"""
    if not DEBUG_PRINT:
        return
    print('=' * 60)
    print(f'[{title}]')
    print('-' * 60)
    print(content)
    print('=' * 60)


def _next_call_label() -> str:
    """返回本次调用的轮次标签（第 1 次 / 第 2 次 ...）。"""
    global _call_counter
    _call_counter += 1
    return f'第 {_call_counter} 次调用'

# 默认配置
DEFAULT_API_KEY = 'sk-13d51c0d2de04517a2887b05bc24886c'
DEFAULT_BASE_URL = 'https://api.deepseek.com'
DEFAULT_MODEL = 'deepseek-flash'
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.7

# 网页抓取相关配置
FETCH_PATH = '/v1/tools/fetch'
FETCH_TIMEOUT = 60

# 联网搜索相关配置
SEARCH_PATH = '/v1/tools/search'
SEARCH_TIMEOUT = 60

# 网页搜索相关配置
SEARCH_PATH = '/v1/tools/search'
SEARCH_TIMEOUT = 60
DEFAULT_SEARCH_COUNT = 5

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
    label = _next_call_label()
    _debug_print(f'传入的 message（{label}）', json.dumps(payload, ensure_ascii=False, indent=2, default=str))
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

    content = _extract_content(response)
    _debug_print(f'DeepSeek 返回结果（{label}）', content)
    return content


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


def fetch_webpage(
    url: str,
    timeout: int = FETCH_TIMEOUT,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, Any]:
    """借助 DeepSeek 的 /v1/tools/fetch 抓取网页，返回网页内容。

    Args:
        url: 要抓取的网页地址
        timeout: 请求超时秒数，默认 60
        base_url: 接口根地址，默认取 DEEPSEEK_BASE_URL
        api_key: API Key，默认取 DEEPSEEK_API_KEY

    Returns:
        抓取到的网页信息字典，通常包含 title、content 等字段；
        接口未返回内容时返回空字典。

    Raises:
        ValueError: url 为空
        requests.HTTPError: 接口返回非 2xx
    """
    if not url or not str(url).strip():
        raise ValueError('url 不能为空')

    url = str(url).strip()
    root = (base_url or os.environ.get('DEEPSEEK_BASE_URL', DEFAULT_BASE_URL)).rstrip('/')
    key = api_key or os.environ.get('DEEPSEEK_API_KEY', DEFAULT_API_KEY)
    endpoint = f'{root}{FETCH_PATH}'

    logger.debug('抓取网页: url=%s, endpoint=%s', url, endpoint)
    try:
        response = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json={'url': url},
            timeout=timeout,
        )
        response.raise_for_status()
        page = response.json()
    except requests.HTTPError as exc:
        logger.error('抓取网页失败(HTTP): url=%s, error=%s', url, exc)
        raise
    except (requests.RequestException, ValueError) as exc:
        logger.error('抓取网页失败: url=%s, error=%s', url, exc)
        raise

    if not isinstance(page, dict):
        logger.warning('抓取网页返回格式异常: %s', type(page))
        return {}

    _debug_print(f'抓取网页结果: {url}', json.dumps(page, ensure_ascii=False, indent=2, default=str))
    logger.info('抓取网页成功: url=%s, title=%s', url, page.get('title'))
    return page


def search_web(
    query: str,
    count: int = 5,
    need_content: bool = True,
    timeout: int = SEARCH_TIMEOUT,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """借助 DeepSeek 的 /v1/tools/search 做联网搜索，返回搜索结果列表。

    Args:
        query: 搜索关键词
        count: 返回结果数量，默认 5
        need_content: 是否返回网页正文内容，默认 True
        timeout: 请求超时秒数，默认 60
        base_url: 接口根地址，默认取 DEEPSEEK_BASE_URL
        api_key: API Key，默认取 DEEPSEEK_API_KEY

    Returns:
        搜索结果列表，每项通常包含 title、url、summary 等字段；
        接口无结果时返回空列表。

    Raises:
        ValueError: query 为空
        requests.HTTPError: 接口返回非 2xx
    """
    if not query or not str(query).strip():
        raise ValueError('query 不能为空')

    query = str(query).strip()
    root = (base_url or os.environ.get('DEEPSEEK_BASE_URL', DEFAULT_BASE_URL)).rstrip('/')
    key = api_key or os.environ.get('DEEPSEEK_API_KEY', DEFAULT_API_KEY)
    endpoint = f'{root}{SEARCH_PATH}'

    payload = {
        'query': query,
        'count': count,
        'need_content': need_content,
    }

    logger.debug('联网搜索: query=%s, endpoint=%s', query, endpoint)
    try:
        response = requests.post(
            endpoint,
            headers={
                'Authorization': f'Bearer {key}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
    except requests.HTTPError as exc:
        logger.error('联网搜索失败(HTTP): query=%s, error=%s', query, exc)
        raise
    except (requests.RequestException, ValueError) as exc:
        logger.error('联网搜索失败: query=%s, error=%s', query, exc)
        raise

    results = data.get('results') if isinstance(data, dict) else None
    if not isinstance(results, list):
        logger.warning('联网搜索返回格式异常: %s', type(data))
        return []

    _debug_print(f'联网搜索结果: {query}', json.dumps(results, ensure_ascii=False, indent=2, default=str))
    logger.info('联网搜索成功: query=%s, 共 %s 条结果', query, len(results))
    return results
