import json
import logging
from datetime import datetime
from astrbot.api.star import Context

logger = logging.getLogger("astrbot")

async def parse_todo(context: Context, provider_id: str, text: str) -> list:
    """
    Parse natural language text into todo items using LLM.

    Args:
        context: The AstrBot context instance.
        provider_id: The ID of the LLM provider to use.
        text: The user input text containing todo items.

    Returns:
        List of parsed todo items (dicts), or None if parsing fails.
    """
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

    try:
        response = await context.llm_generate(
            provider_id=provider_id,
            prompt=prompt
        )
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None

    if not response:
        logger.warning("LLM returned empty response")
        return None

    # Clean up the response to ensure valid JSON
    cleaned_response = response.strip()
    if cleaned_response.startswith("```"):
        # Remove markdown code block markers
        lines = cleaned_response.splitlines()
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned_response = "\n".join(lines).strip()

    try:
        todos = json.loads(cleaned_response)
        if not isinstance(todos, list):
            logger.warning(f"LLM did not return a list: {cleaned_response}")
            return None
        return todos
    except json.JSONDecodeError as e:
        logger.error(f"JSON parsing failed: {e}. Response: {response}")
        return None
