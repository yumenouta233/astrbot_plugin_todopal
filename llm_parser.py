import json
import logging
import re
from datetime import datetime
from astrbot.api.star import Context

logger = logging.getLogger("astrbot")

async def parse_todo(context: Context, provider_id: str, text: str) -> list:
    """
    Parse natural language text into todo items using LLM.
    Legacy function kept for backward compatibility if needed, 
    but mainly used by analyze_intent for 'add' action.
    """
    # ... logic similar to before, but we might want to consolidate ...
    # For now, let's keep it as is or reuse the logic inside analyze_intent
    # Actually, analyze_intent can call this if needed, or we just implement logic there.
    # Let's keep it independent for now.
    if not text.strip():
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    
    prompt = f"""
你是一个待办事项解析助手。今天是 {today_str}。
请将用户的输入拆分成待办事项，并返回 JSON 格式的列表。

要求：
1. 返回格式必须是 JSON 数组。
2. 每个元素包含 "date" (YYYY-MM-DD), "time" (HH:MM, 如果没有具体时间则为 null), "content" (事项内容)。
3. 如果用户没有指定日期，根据语境推断，或者默认为今天。
4. 不要返回任何解释性文字，只返回 JSON。

用户输入：
{text}

JSON 输出：
"""

    return await _call_llm_and_parse_json(context, provider_id, prompt)

async def analyze_intent(context: Context, provider_id: str, text: str, current_todos: list) -> dict:
    """
    Analyze user intent using LLM.
    Returns a dict with:
    - type: 'add' | 'done' | 'fix' | 'check' | 'cancel' | 'unknown'
    - payload: varies by type
      - add: list of todo items (dicts)
      - done: list of indices (1-based) or content strings
      - fix: {'index': int, 'content': str}
      - check: None
    """
    if not text.strip():
        return None

    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # Simplify current todos for prompt
    todos_summary = ""
    if current_todos:
        todos_summary = "当前待办事项:\n"
        for i, todo in enumerate(current_todos, 1):
            status = "已完成" if todo.get('status') == 'done' else "未完成"
            todos_summary += f"{i}. {todo.get('content')} ({status})\n"
    else:
        todos_summary = "当前没有待办事项。"

    prompt = f"""
你是一个智能待办事项助手。今天是 {today_str}。
{todos_summary}

用户输入: "{text}"

请分析用户的意图，并返回 JSON 格式的结果。
意图类型 (type) 只能是以下之一：
- "add": 添加新的待办事项。
- "done": 完成待办事项。
- "fix": 修改待办事项的内容。
- "check": 查看待办事项。
- "cancel": 取消操作或无意义输入。

返回格式示例：

1. 添加事项 ("买牛奶"):
{{
    "type": "add",
    "payload": [
        {{ "date": "{today_str}", "time": null, "content": "买牛奶" }}
    ]
}}

2. 完成事项 ("完成第1项" 或 "买牛奶做完了"):
{{
    "type": "done",
    "payload": "1"  // 如果是数字，直接返回数字字符串；如果是内容匹配，返回内容字符串
}}
// 注意：如果是多个，用空格或逗号分隔，如 "1 2"

3. 修改事项 ("把第2条改成开会"):
{{
    "type": "fix",
    "payload": "2 开会" // 格式必须是 "序号 新内容"
}}

4. 查看事项 ("看看还有什么" 或 "列出清单"):
{{
    "type": "check",
    "payload": null
}}

要求：
1. 必须返回合法的 JSON 对象。
2. 不要返回 markdown 格式，只返回 JSON 字符串。
"""

    result = await _call_llm_and_parse_json(context, provider_id, prompt, expect_list=False)
    return result

async def _call_llm_and_parse_json(context: Context, provider_id: str, prompt: str, expect_list: bool = True):
    try:
        response = await context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt
        )
        if hasattr(response, "completion_text"):
             response = response.completion_text
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None

    if not response:
        logger.warning("LLM returned empty response")
        return None

    # Clean up JSON
    match = re.search(r"(\{.*\}|\[.*\])", response, re.DOTALL)
    if match:
        cleaned_response = match.group(0)
    else:
        cleaned_response = response.strip()
        if cleaned_response.startswith("```"):
            lines = cleaned_response.splitlines()
            if lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            cleaned_response = "\n".join(lines).strip()

    try:
        data = json.loads(cleaned_response)
        if expect_list and not isinstance(data, list):
             # Fallback: maybe it returned a dict wrapper?
             if isinstance(data, dict) and "todos" in data:
                 return data["todos"]
             logger.warning(f"LLM did not return a list: {cleaned_response}")
             return None
        return data
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing failed: {e}. Response: {response}")
        return None
