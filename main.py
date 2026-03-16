from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    from .llm_parser import parse_todo
except ImportError:
    from llm_parser import parse_todo


@register("todopal", "TodoPal", "TodoPal Plugin", "1.0.0")
class TodoPalPlugin(Star):
    """
    TodoPal plugin for AstrBot to manage todo items.
    """

    def __init__(self, context: Context):
        """
        Initialize the TodoPal plugin.

        Args:
            context: The AstrBot context.
        """
        super().__init__(context)

    @filter.command("todo_parse")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input command /todo_parse.

        Args:
            event: The message event triggered by the command.
        """
        message_str = event.message_str
        if not message_str:
            yield event.plain_result("请输入待办事项内容。")
            return

        # Get the current LLM provider ID
        try:
            provider_id = self.context.get_current_chat_provider_id(event.unified_msg_origin)
        except Exception as e:
            logger.error(f"Failed to get provider ID: {e}")
            yield event.plain_result("无法获取当前的 LLM Provider ID，请检查配置。")
            return

        if not provider_id:
            yield event.plain_result("未配置 LLM Provider。")
            return

        # Call the parser logic
        todos = await parse_todo(self.context, provider_id, message_str)

        if todos is None:
            # As per requirement: "如果解析失败，返回： 暂时没有稳定识别这条待办，请换一种更明确的表达方式。"
            yield event.plain_result("暂时没有稳定识别这条待办，请换一种更明确的表达方式。")
            return

        if not todos:
            yield event.plain_result("未能识别到任何待办事项。")
            return

        # Format output
        result_lines = ["我识别到以下待办：", ""]
        for i, todo in enumerate(todos, 1):
            date = todo.get("date", "Unknown Date")
            time = todo.get("time")
            content = todo.get("content", "Unknown Content")

            time_str = f" {time}" if time else " 全天"
            result_lines.append(f"{i}. {date}{time_str} {content}")

        result_lines.append("")
        result_lines.append("如果正确，请回复“确认”。")

        yield event.plain_result("\n".join(result_lines))
