from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from datetime import datetime
import re
import uuid

import json
import os
from pathlib import Path
import asyncio
import inspect
from datetime import datetime, timedelta
from astrbot.api.message_components import Plain

try:
    from .llm_parser import parse_todo, analyze_intent
    from .storage import TodoStorage
    from .matcher import TodoMatcher
except ImportError:
    from llm_parser import parse_todo, analyze_intent
    from storage import TodoStorage
    from matcher import TodoMatcher

@register("todopal", "TodoPal", "TodoPal Plugin", "1.6.0")
class TodoPalPlugin(Star):
    """
    TodoPal plugin for AstrBot to manage todo items.
    """

    def __init__(self, context: Context, config: dict = None):
        """
        Initialize the TodoPal plugin.

        Args:
            context: The AstrBot context.
        """
        super().__init__(context)
        self.storage = TodoStorage()
        # In-memory session state: {unified_msg_origin: {'state': str, 'todos': list, 'pending_date': str}}
        self.sessions = {}
        
        self.config = config or {}
        
        # We don't need triggers.json anymore if we have self.config
        # But we provide a fallback default
        self.triggers = self.config.get("custom_triggers", ["记", "待办", "任务", "做完", "完成", "修改", "改一下", "看看", "清单", "列表", "check", "add", "fix", "done"])
        
        # Start cron loop for proactive messaging
        self._cron_task = asyncio.create_task(self._cron_loop())
        self._last_rollover_date = ""
        self._last_summary_sent = {}

    @staticmethod
    def _normalize_hhmm(value: str, default: str) -> str:
        try:
            parts = str(value).split(":")
            if len(parts) != 2:
                return default
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                return default
            return f"{hour:02d}:{minute:02d}"
        except Exception:
            return default

    async def _get_provider_id_from_origin(self, origin: str):
        if not origin:
            return None
        try:
            return await self.context.get_current_chat_provider_id(umo=origin)
        except TypeError:
            return await self.context.get_current_chat_provider_id(origin)
        except Exception:
            try:
                return await self.context.get_current_chat_provider_id(origin)
            except Exception:
                return None

    @staticmethod
    def _persona_text_from_data(persona_data):
        if not persona_data:
            return ""
        if isinstance(persona_data, str):
            return persona_data.strip()
        if isinstance(persona_data, dict):
            for key in ("prompt", "system_prompt", "content", "description", "text"):
                value = persona_data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            name = persona_data.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
            return ""
        for key in ("prompt", "system_prompt", "content", "description", "text", "name"):
            value = getattr(persona_data, key, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def _get_persona_instruction(self, persona: str, custom_prompt: str) -> str:
        if custom_prompt:
            return f"人格设定：\n{custom_prompt}\n"
        if not persona:
            return ""
        persona_text = ""
        get_persona = getattr(self.context, "get_persona", None)
        if callable(get_persona):
            try:
                persona_obj = get_persona(persona)
                if asyncio.iscoroutine(persona_obj):
                    persona_obj = await persona_obj
                persona_text = self._persona_text_from_data(persona_obj)
            except Exception as e:
                logger.debug(f"Get persona failed for {persona}: {e}")
        if persona_text:
            return f"人格设定：\n{persona_text}\n"
        logger.debug(f"Persona details not found for {persona}, fallback to id prompt")
        return f"人格设定ID：{persona}\n"

    @staticmethod
    def _normalize_date_str(date_text: str):
        if not date_text:
            return None
        txt = str(date_text).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(txt, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        matched = re.match(r"^(\d{1,2})月(\d{1,2})日$", txt)
        if matched:
            now = datetime.now()
            month = int(matched.group(1))
            day = int(matched.group(2))
            try:
                return datetime(now.year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                return None
        return None

    def _resolve_check_date(self, query_text: str = "", payload=None):
        payload_date = None
        if isinstance(payload, dict):
            payload_date = self._normalize_date_str(payload.get("date"))
        elif isinstance(payload, str):
            payload_date = self._normalize_date_str(payload)
        if payload_date:
            return payload_date
        text = (query_text or "").strip()
        now = datetime.now()
        if "后天" in text:
            return (now + timedelta(days=2)).strftime("%Y-%m-%d")
        if "明天" in text:
            return (now + timedelta(days=1)).strftime("%Y-%m-%d")
        if "今天" in text:
            return now.strftime("%Y-%m-%d")
        explicit = re.search(r"(\d{4}[-/]\d{1,2}[-/]\d{1,2})", text)
        if explicit:
            parsed = self._normalize_date_str(explicit.group(1))
            if parsed:
                return parsed
        md = re.search(r"(\d{1,2})月(\d{1,2})日", text)
        if md:
            parsed = self._normalize_date_str(f"{md.group(1)}月{md.group(2)}日")
            if parsed:
                return parsed
        return now.strftime("%Y-%m-%d")

    async def _reply_with_persona_prefix(self, event, lead_text: str, body_text: str):
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        merged_text = f"{lead_text}\n\n{body_text}" if body_text else lead_text
        if not persona and not custom_prompt:
            return event.plain_result(merged_text)
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
        if not provider_id:
            logger.debug("Persona prefix fallback: provider_id not found")
            return event.plain_result(merged_text)
        prompt = f"""
你是一个助手。请根据以下人格设定，生成一句简短开场白，语气自然，不超过30字。
{persona_instruction}
开场白意图：{lead_text}

只输出一句开场白，不要输出列表，不要加引号。
"""
        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            if resp and hasattr(resp, "completion_text") and resp.completion_text:
                prefix = resp.completion_text.strip().splitlines()[0].strip()
                if prefix:
                    return event.plain_result(f"{prefix}\n\n{body_text}" if body_text else prefix)
            logger.debug("Persona prefix fallback: empty llm response")
        except Exception as e:
            logger.error(f"Persona prefix failed: {e}")
        return event.plain_result(merged_text)

    @staticmethod
    def _extract_completion_text(response) -> str:
        if response is None:
            return ""
        if isinstance(response, str):
            return response.strip()
        text = getattr(response, "completion_text", None)
        if isinstance(text, str) and text.strip():
            return text.strip()
        content = getattr(response, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        return ""

    @staticmethod
    def _sanitize_intro_text(text: str) -> str:
        if not text:
            return ""
        one_line = re.sub(r"\s+", " ", str(text)).strip()
        one_line = one_line.replace("\n", " ").strip()
        if not one_line:
            return ""
        one_line = re.sub(r"^[\-\d\.\)\s]+", "", one_line).strip()
        if one_line.endswith(("?", "？")):
            one_line = one_line[:-1].rstrip() + "。"
        if len(one_line) > 60:
            one_line = one_line[:60].rstrip("，,;；。.!！？?") + "。"
        return one_line

    async def _iter_llm_stream_chunks(self, provider_id: str, prompt: str):
        method = getattr(self.context, "llm_generate", None)
        if not callable(method):
            return
        result = method(chat_provider_id=provider_id, prompt=prompt, stream=True)
        if inspect.isawaitable(result):
            result = await result
        if result is None:
            return
        if hasattr(result, "__aiter__"):
            async for chunk in result:
                text = self._extract_completion_text(chunk)
                if text:
                    yield text
            return
        if hasattr(result, "__iter__") and not isinstance(result, (str, bytes, dict)):
            for chunk in result:
                text = self._extract_completion_text(chunk)
                if text:
                    yield text
            return
        text = self._extract_completion_text(result)
        if text:
            yield text

    async def _generate_check_intro_segments(self, event: AstrMessageEvent, title: str, todos: list):
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
        if not provider_id:
            return []

        prompt = f"""
你是一个待办助手。请先给用户一句自然回应，再由系统发送清单详情。
{persona_instruction}
场景：用户请求查看待办事项。
清单标题：{title}
待办数量：{len(todos)}

要求：
1. 只输出一句短句，不超过30字。
2. 不要提问，不要反问，不要输出列表。
3. 语气自然、贴合人格设定。
"""
        stream_text = ""
        try:
            async for piece in self._iter_llm_stream_chunks(provider_id, prompt):
                if piece:
                    stripped_piece = piece.strip()
                    if not stripped_piece:
                        continue
                    if stream_text and stripped_piece.startswith(stream_text):
                        stream_text = stripped_piece
                    elif stripped_piece.startswith(stream_text):
                        stream_text = stripped_piece
                    else:
                        stream_text += stripped_piece
            if stream_text:
                merged = self._sanitize_intro_text(stream_text)
                if merged:
                    segments = []
                    buf = ""
                    for ch in merged:
                        buf += ch
                        if ch in "，。！？；,.!?;" or len(buf) >= 18:
                            part = buf.strip()
                            if part:
                                segments.append(part)
                            buf = ""
                    tail = buf.strip()
                    if tail:
                        segments.append(tail)
                    if segments:
                        return segments
        except Exception as e:
            logger.debug(f"Stream intro failed, fallback to non-stream: {e}")

        try:
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            final_text = self._sanitize_intro_text(self._extract_completion_text(resp))
            if final_text:
                return [final_text]
        except Exception as e:
            logger.debug(f"Non-stream intro failed: {e}")
        return []

    async def _cron_loop(self):
        """Background task to handle reminders and summaries."""
        logger.info("TodoPal cron loop started.")
        last_reminders = {}
        
        while True:
            try:
                now = datetime.now()
                current_time_str = now.strftime("%H:%M")
                today_str = now.strftime("%Y-%m-%d")
                summary_time = self._normalize_hhmm(self.config.get("summary_time", "23:00"), "23:00")
                reminder_start = self._normalize_hhmm(self.config.get("reminder_start", "09:00"), "09:00")
                reminder_end = self._normalize_hhmm(self.config.get("reminder_end", "22:00"), "22:00")
                
                if self.config.get("auto_rollover", True):
                    if today_str != self._last_rollover_date:
                        self._do_rollover(today_str)
                        self._last_rollover_date = today_str

                users = self.storage.get_all_users()
                for u in users:
                    platform = u['platform']
                    user_id = u['user_id']
                    origin = u['origin']
                    cached_provider_id = u.get("provider_id")
                    user_key = f"{platform}_{user_id}"
                    
                    if self.config.get("summary_enable", True):
                        if current_time_str >= summary_time and self._last_summary_sent.get(user_key) != today_str:
                            sent = await self._send_proactive_summary(platform, user_id, origin, today_str, cached_provider_id)
                            if sent:
                                self._last_summary_sent[user_key] = today_str
                    
                    if self.config.get("reminder_enable", False):
                        if reminder_start <= current_time_str <= reminder_end:
                            last_time = last_reminders.get(user_key)
                            interval_hours = self.config.get("reminder_interval", 2)
                            if not last_time or (now - last_time).total_seconds() >= interval_hours * 3600:
                                sent = await self._send_proactive_reminder(platform, user_id, origin, today_str, cached_provider_id)
                                if sent:
                                    last_reminders[user_key] = now
                                    
            except Exception as e:
                logger.error(f"TodoPal cron loop error: {e}")
            
            await asyncio.sleep(60)

    def _do_rollover(self, today_str: str):
        yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        users = self.storage.get_all_users()
        for u in users:
            rolled = self.storage.rollover_pending_todos(u['platform'], u['user_id'], yesterday_str, today_str)
            if rolled > 0:
                logger.info(f"Rolled over {rolled} items for {u['user_id']}")

    async def _send_proactive_summary(self, platform, user_id, origin, today_str, cached_provider_id=None) -> bool:
        todos = self.storage.load_todos(platform, user_id, today_str)
        if not todos:
            return False
            
        completed = [t for t in todos if t.get('status') == 'done']
        pending = [t for t in todos if t.get('status') == 'pending']
        
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        
        prompt = f"""
{persona_instruction}
用户今天的待办事项总结：
- 共计 {len(todos)} 项
- 已完成 {len(completed)} 项
- 未完成 {len(pending)} 项
详情：
{[t.get('content') for t in todos]}

请生成一段总结性的话语，主动发给用户，语气要自然。不要返回JSON，直接返回要说的话。
"""
        logger.debug(f"Proactive summary prompt: {prompt}")
        provider_id = cached_provider_id or await self._get_provider_id_from_origin(origin)
        if not provider_id:
            return False
        
        resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        if resp and hasattr(resp, 'completion_text'):
            msg = resp.completion_text
            await self.context.send_message(origin, msg)
            return True
        return False

    async def _send_proactive_reminder(self, platform, user_id, origin, today_str, cached_provider_id=None) -> bool:
        todos = self.storage.load_todos(platform, user_id, today_str)
        pending = [t for t in todos if t.get('status') == 'pending']
        
        if not pending:
            return False # Nothing to remind
            
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)
        
        prompt = f"""
{persona_instruction}
用户还有 {len(pending)} 项待办未完成，分别是：
{[t.get('content') for t in pending]}

请生成一段简短的话语，主动提醒用户去完成任务，语气要自然。不要返回JSON，直接返回要说的话。
"""
        logger.debug(f"Proactive reminder prompt: {prompt}")
        provider_id = cached_provider_id or await self._get_provider_id_from_origin(origin)
        if not provider_id:
            return False
        
        resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
        if resp and hasattr(resp, 'completion_text'):
            msg = resp.completion_text
            await self.context.send_message(origin, msg)
            return True
        return False

    async def _reply_with_persona(self, event, plain_text: str):
        """Helper to reply with persona if configured, otherwise plain text."""
        persona = self.config.get("bot_persona", "")
        custom_prompt = self.config.get("bot_persona_prompt", "")
        
        if not persona and not custom_prompt:
            return event.plain_result(plain_text)

        persona_instruction = await self._get_persona_instruction(persona, custom_prompt)

        prompt = f"""
你是一个助手。请根据以下人格设定，将括号里的系统提示转化为符合人设的自然回复。
{persona_instruction}
系统提示：({plain_text})

请直接输出回复内容，不要加引号。
"""
        logger.debug(f"Reply persona prompt: {prompt}")
        try:
            provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
            if not provider_id:
                logger.debug("Persona fallback: provider_id not found")
                return event.plain_result(plain_text)
            resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
            if resp and hasattr(resp, 'completion_text') and resp.completion_text:
                return event.plain_result(resp.completion_text)
            logger.debug("Persona fallback: empty llm response")
        except Exception as e:
            logger.error(f"Persona reply failed: {e}")
        
        return event.plain_result(plain_text)

    def _format_preview(self, todos: list, include_confirm_prompt: bool = True) -> str:
        """
        Format todos for user confirmation or display.
        """
        grouped = {}
        for todo in todos:
            date = todo.get("date", "Unknown")
            if date not in grouped:
                grouped[date] = []
            grouped[date].append(todo)
        
        result_lines = []
        for date, items in grouped.items():
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                weekday_map = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
                weekday = weekday_map[dt.weekday()]
                date_header = f"{dt.year}年{dt.month}月{dt.day}日 星期{weekday}"
            except ValueError:
                date_header = date
            
            result_lines.append(date_header)
            result_lines.append("")
            
            for i, item in enumerate(items, 1):
                time = item.get("time")
                content = item.get("content", "")
                status = item.get("status", "pending")
                
                prefix = f"{time} " if time else ""
                check_mark = "✅ " if status == "done" else ""
                
                result_lines.append(f"{i}. {check_mark}{prefix}{content}")
            result_lines.append("")
        
        if include_confirm_prompt:
            result_lines.append("如果正确，请回复“确认”。")
        return "\n".join(result_lines)

    @filter.regex(r".*")
    async def todo_parse(self, event: AstrMessageEvent):
        """
        Parse todo items from user input.
        Supports:
        1. Explicit commands: 'todo', 'add', 'done', 'fix', 'check'
        2. Natural language with keywords (defined in triggers.json)
        """
        message_str = event.message_str.strip()
        if not message_str:
            return

        # 1. Check for explicit command prefixes
        explicit_match = re.match(r"^(todo|add|done|fix|check)\s*(.*)", message_str, re.IGNORECASE)
        
        if explicit_match:
            command_prefix = explicit_match.group(1).lower()
            todo_content = explicit_match.group(2).strip()
            # If explicit command, force smart mode for 'todo' or execute others directly
        else:
            # 2. Check for keywords in triggers
            # If no keyword matches, ignore this message
            if not any(keyword in message_str for keyword in self.triggers):
                return
            
            # If matched, treat as 'todo' command (smart agent)
            command_prefix = 'todo'
            todo_content = message_str

        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        provider_id_for_user = await self._get_provider_id_from_origin(event.unified_msg_origin)
        self.storage.register_user(platform, user_id, event.unified_msg_origin, provider_id_for_user)

        if command_prefix == 'check':
            yield await self._handle_check_command(event, platform, user_id, todo_content, None)
            return

        if not todo_content:
            yield await self._reply_with_persona(event, f"请输入{command_prefix}的具体内容。")
            return

        # --- Handle 'done' command ---
        if command_prefix == 'done':
            # done command handles its own persona reply internally if we refactor it, 
            # OR we refactor _handle_done_command to return string and we reply here.
            # Currently _handle_done_command yields plain_result directly.
            # We should refactor the helper functions to return (success, message) or just message string.
            # But that's a big refactor. 
            # Alternative: modifying helper functions to use _reply_with_persona.
            async for result in self._handle_done_command(event, platform, user_id, todo_content):
                 yield result
            return

        # --- Handle 'fix' command ---
        if command_prefix == 'fix':
            async for result in self._handle_fix_command(event, platform, user_id, todo_content):
                yield result
            return

        provider_id = provider_id_for_user

        if not provider_id:
            yield event.plain_result("未配置 LLM Provider。")
            return

        # Smart handling for 'todo' command
        if command_prefix == 'todo':
            today = datetime.now().strftime("%Y-%m-%d")
            current_todos = self.storage.load_todos(platform, user_id, today)
            
            # Analyze intent
            intent_result = await analyze_intent(self.context, provider_id, todo_content, current_todos)
            
            if not intent_result or not intent_result.get('type'):
                yield await self._reply_with_persona(event, "抱歉，我没太理解您的意思，请换个说法试试。")
                return
            
            intent_type = intent_result['type']
            payload = intent_result.get('payload')
            
            if intent_type == 'check':
                async for result in self._handle_check_command(event, platform, user_id, todo_content, payload):
                    yield result
                return
                
            elif intent_type == 'done':
                if not payload:
                     yield await self._reply_with_persona(event, "需要指定完成哪一项哦。")
                     return
                async for result in self._handle_done_command(event, platform, user_id, str(payload)):
                    yield result
                return
                
            elif intent_type == 'fix':
                if not payload:
                    yield await self._reply_with_persona(event, "需要指定修改哪一项及新内容哦。")
                    return
                async for result in self._handle_fix_command(event, platform, user_id, str(payload)):
                    yield result
                return
                
            elif intent_type == 'add':
                # Proceed to existing logic for adding todos
                # Payload should be the list of todos
                if isinstance(payload, list):
                    todos = payload
                else:
                    # Fallback to old parser if payload is weird
                    todos = await parse_todo(self.context, provider_id, todo_content)
            
            elif intent_type == 'cancel':
                yield await self._reply_with_persona(event, "好的，什么都不做。")
                return
                
            else:
                yield await self._reply_with_persona(event, "抱歉，我没太理解您的意思。")
                return

        else:
            # 'add' command: always append
            todos = await parse_todo(self.context, provider_id, todo_content)

        if todos is None:
            yield await self._reply_with_persona(event, "暂时没有稳定识别这条待办，请换一种更明确的表达方式。")
            return
        if not todos:
            yield await self._reply_with_persona(event, "未能识别到任何待办事项。")
            return

        # For 'todo' (smart mode), we default to append unless we want to support overwrite logic explicitly.
        # Given the "Smart Agent" change, 'todo' implies natural language interaction which usually means "add to list".
        # However, to preserve "Overwrite" capability, we might need a specific trigger.
        # For now, let's make 'todo' (via intent 'add') default to 'append' to be safe, 
        # BUT if we want to support the old "Overwrite" behavior, we might need to ask.
        # Let's stick to 'append' for smart 'add' to avoid data loss.
        # If user really wants to overwrite, they might need to clear first (not supported yet) or we add a "clear" intent later.
        # Wait, the previous logic was: 'todo' = overwrite, 'add' = append.
        # Now 'todo' = smart agent. 'add' = append.
        # So we effectively removed "Overwrite" command unless we re-introduce it.
        # Let's change action_type to 'append' for both, unless we add logic.
        
        action_type = 'append' 
        
        # Store session state
        self.sessions[event.unified_msg_origin] = {
            'state': 'WAITING_CONFIRM',
            'action_type': action_type,
            'todos': todos,
            'source_text': todo_content,
            'platform': platform,
            'user_id': user_id
        }

        preview = self._format_preview(todos, include_confirm_prompt=True)
        todo_count = len(todos)
        date_count = len({t.get("date") for t in todos if t.get("date")})
        if date_count > 1:
            lead_text = f"我先帮你整理了 {todo_count} 项待办，覆盖 {date_count} 天，确认后就保存。"
        else:
            lead_text = f"我先帮你整理了 {todo_count} 项待办，确认后就保存。"
        
        if command_prefix == 'todo_old_overwrite_mode_disabled':
            # Check if data exists for today to warn user
            today = datetime.now().strftime("%Y-%m-%d")
            existing = self.storage.load_todos(platform, user_id, today)
            warning = ""
            if existing:
                warning = f"\n⚠️ **注意：这将覆盖您今天已有的 {len(existing)} 条待办！**"
            
            yield await self._reply_with_persona(event, f"【新建/覆盖模式】{warning}\n{preview}")
        else:
            yield await self._reply_with_persona_prefix(event, lead_text, preview)

    @filter.regex(r"^(确认|取消)$")
    async def handle_confirmation(self, event: AstrMessageEvent):
        """
        Handle confirmation or choice selection.
        """
        session = self.sessions.get(event.unified_msg_origin)
        if not session:
            # Not in a session, ignore or let other plugins handle
            return 

        action = event.message_str.strip()
        state = session['state']
        todos = session['todos']
        platform = session['platform']
        user_id = session['user_id']
        source_text = session.get('source_text', '')

        if action == "取消":
            del self.sessions[event.unified_msg_origin]
            yield await self._reply_with_persona(event, "已取消。")
            return

        if state == 'WAITING_CONFIRM':
            if action == "确认":
                mode = session.get('action_type', 'append')
                self._save_todos(platform, user_id, todos, source_text, mode=mode)
                del self.sessions[event.unified_msg_origin]
                yield await self._reply_with_persona(event, "已保存待办事项。")
            else:
                yield event.plain_result("请回复“确认”或“取消”。")

    def _save_todos(self, platform, user_id, todos, source_text, mode='append'):
        # Group by date first
        grouped = {}
        for todo in todos:
            date = todo.get("date")
            if not date: continue
            if date not in grouped: grouped[date] = []
            grouped[date].append(todo)
        
        for date, items in grouped.items():
            # Enrich items
            for item in items:
                item['id'] = f"{date.replace('-', '')}-{uuid.uuid4().hex[:6]}"
                item['status'] = 'pending'
                item['created_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                item['updated_at'] = item['created_at']
                item['done_at'] = None
                item['source_text'] = source_text
            
            if mode == 'overwrite':
                self.storage.save_todos(platform, user_id, date, items)
            else:
                self.storage.append_todos(platform, user_id, date, items)

    async def _handle_done_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle marking todos as done using the 'done' prefix.
        Matches: "done 1, 2", "done 买菜"
        """
        today = datetime.now().strftime("%Y-%m-%d")
        
        todos = self.storage.load_todos(platform, user_id, today)
        if not todos:
            yield await self._reply_with_persona(event, f"今天没有待办事项哦。")
            return

        matched_indices = TodoMatcher.match_todos(todos, content)
        
        if not matched_indices:
            yield await self._reply_with_persona(event, "找不到对应的待办事项，请检查描述或序号是否准确。")
            return

        updated_items = []
        for idx in matched_indices:
            if todos[idx]['status'] != 'done':
                self.storage.update_todo_status(platform, user_id, today, idx, 'done')
                updated_items.append(todos[idx]['content'])
        
        if not updated_items:
            yield await self._reply_with_persona(event, "所选的待办事项已经是完成状态啦。")
            return

        # Reload to get the fresh state and format it
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        
        yield await self._reply_with_persona(event, f"太棒了！已更新状态：\n\n{preview}")

    async def _handle_check_command(self, event: AstrMessageEvent, platform: str, user_id: str, query_text: str = "", payload=None):
        target_date = self._resolve_check_date(query_text, payload)
        todos = self.storage.load_todos(platform, user_id, target_date)
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        after_tomorrow = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        if target_date == today:
            title = "今日待办清单："
            empty_text = "今天还没有待办事项哦。"
        elif target_date == tomorrow:
            title = "明日待办清单："
            empty_text = "明天还没有待办事项哦。"
        elif target_date == after_tomorrow:
            title = "后日待办清单："
            empty_text = "后天还没有待办事项哦。"
        else:
            title = f"{target_date} 待办清单："
            empty_text = f"{target_date} 还没有待办事项哦。"
        if not todos:
            yield await self._reply_with_persona(event, empty_text)
            return
        preview = self._format_preview(todos, include_confirm_prompt=False)
        intro_segments = await self._generate_check_intro_segments(event, title, todos)
        if intro_segments:
            for seg in intro_segments:
                yield event.plain_result(seg)
        else:
            fallback_intro = await self._reply_with_persona(event, title.replace("：", ""))
            yield fallback_intro
        yield event.plain_result(f"{title}\n\n{preview}")

    async def _handle_fix_command(self, event: AstrMessageEvent, platform: str, user_id: str, content: str):
        """
        Handle 'fix' command to modify a specific todo item content.
        Format: fix 3 改成光电数据集会议
        """
        today = datetime.now().strftime("%Y-%m-%d")
        todos = self.storage.load_todos(platform, user_id, today)
        
        if not todos:
            yield await self._reply_with_persona(event, "今天没有待办事项，无法修改。")
            return

        # Parse index and content
        match = re.match(r"^(\d+)\s*(.*)", content)
        if not match:
            yield await self._reply_with_persona(event, "格式错误。请使用：fix 序号 新内容\n例如：fix 3 改成光电数据集会议")
            return
            
        idx = int(match.group(1)) - 1
        raw_new_content = match.group(2).strip()
        
        if not (0 <= idx < len(todos)):
            yield await self._reply_with_persona(event, f"找不到第 {idx+1} 条待办。")
            return
            
        if not raw_new_content:
            yield await self._reply_with_persona(event, "请输入新的待办内容。")
            return

        # Simple cleanup: remove common prefixes like "改成", "变为"
        cleaned_content = re.sub(r"^(改成|变为|变成|是|为|:)\s*", "", raw_new_content).strip()
        
        updated_item = self.storage.update_todo_content(platform, user_id, today, idx, cleaned_content)
        
        if updated_item:
            # Show the updated list
            fresh_todos = self.storage.load_todos(platform, user_id, today)
            preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
            yield await self._reply_with_persona(event, f"已修改第 {idx+1} 条：\n\n{preview}")
        else:
            yield await self._reply_with_persona(event, "修改失败，请重试。")
