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

    def _event_scope(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"
        return platform, user_id

    def _resolve_date_input(self, date_text: str = "") -> str:
        if not date_text:
            return datetime.now().strftime("%Y-%m-%d")
        parsed = self._normalize_date_str(date_text)
        if parsed:
            return parsed
        return self._resolve_check_date(str(date_text), None)

    def _simple_items(self, todos: list):
        items = []
        for idx, todo in enumerate(todos, 1):
            items.append({
                "index": idx,
                "date": todo.get("date"),
                "time": todo.get("time"),
                "content": todo.get("content"),
                "status": todo.get("status", "pending")
            })
        return items

    @staticmethod
    def _service_message(action: str, ok: bool, error: str = "", **kwargs) -> str:
        if action == "add":
            if ok:
                return f"已识别 {kwargs.get('count', 0)} 项待办，确认后保存。"
            if error == "EMPTY_CONTENT":
                return "请输入待办内容。"
            if error == "NO_PROVIDER":
                return "未配置 LLM Provider。"
            return "未能识别到任何待办事项。"
        if action == "done":
            if ok:
                count = kwargs.get("updated_count", 0)
                if count == 0:
                    return "所选的待办事项已经是完成状态啦。"
                return f"已完成 {count} 项待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项哦。"
            if error == "NOT_FOUND":
                return "找不到对应的待办事项，请检查描述或序号是否准确。"
            return "处理失败，请重试。"
        if action == "fix":
            if ok:
                return f"已修改第 {kwargs.get('index', 0)} 条待办。"
            if error == "EMPTY_LIST":
                return "今天没有待办事项，无法修改。"
            if error == "INDEX_OUT_OF_RANGE":
                return f"找不到第 {kwargs.get('index', 0)} 条待办。"
            if error == "EMPTY_CONTENT":
                return "请输入新的待办内容。"
            return "修改失败，请重试。"
        if action == "check":
            return f"共 {kwargs.get('count', 0)} 项待办。"
        return "处理完成。"

    async def _service_check(self, platform: str, user_id: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        return {
            "ok": True,
            "action": "check",
            "date": target_date,
            "count": len(todos),
            "items": self._simple_items(todos),
            "message": self._service_message("check", True, count=len(todos))
        }

    async def _service_add(self, event: AstrMessageEvent, platform: str, user_id: str, content: str, date_text: str = "", time_text: str = "", persist: bool = True, parsed_todos: list = None):
        source_text = (content or "").strip()
        if not source_text:
            return {"ok": False, "action": "add", "error": "EMPTY_CONTENT", "message": self._service_message("add", False, "EMPTY_CONTENT")}
        todos = list(parsed_todos) if isinstance(parsed_todos, list) else []
        if not todos:
            target_date = self._resolve_date_input(date_text) if date_text else ""
            if target_date:
                todos = [{
                    "date": target_date,
                    "time": (time_text.strip() if time_text else None),
                    "content": source_text
                }]
            else:
                provider_id = await self._get_provider_id_from_origin(event.unified_msg_origin)
                if not provider_id:
                    return {"ok": False, "action": "add", "error": "NO_PROVIDER", "message": self._service_message("add", False, "NO_PROVIDER")}
                todos = await parse_todo(self.context, provider_id, source_text)
                if not todos:
                    return {"ok": False, "action": "add", "error": "PARSE_FAILED", "message": self._service_message("add", False, "PARSE_FAILED")}
        if persist:
            self._save_todos(platform, user_id, todos, source_text, mode='append')
        grouped = {}
        for todo in todos:
            dt = todo.get("date") or datetime.now().strftime("%Y-%m-%d")
            grouped.setdefault(dt, 0)
            grouped[dt] += 1
        return {
            "ok": True,
            "action": "add",
            "added_count": len(todos),
            "dates": grouped,
            "items": todos,
            "message": self._service_message("add", True, count=len(todos))
        }

    async def _service_done(self, platform: str, user_id: str, selector: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "done", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("done", False, "EMPTY_LIST")}
        matched_indices = TodoMatcher.match_todos(todos, selector or "")
        if not matched_indices:
            return {"ok": False, "action": "done", "date": target_date, "error": "NOT_FOUND", "message": self._service_message("done", False, "NOT_FOUND")}
        updated = []
        for idx in matched_indices:
            if todos[idx].get("status") != "done":
                self.storage.update_todo_status(platform, user_id, target_date, idx, "done")
                updated.append(idx + 1)
        return {
            "ok": True,
            "action": "done",
            "date": target_date,
            "updated_indices": updated,
            "updated_count": len(updated),
            "message": self._service_message("done", True, updated_count=len(updated))
        }

    async def _service_fix(self, platform: str, user_id: str, index: int, content: str, date_text: str = ""):
        target_date = self._resolve_date_input(date_text)
        todos = self.storage.load_todos(platform, user_id, target_date)
        if not todos:
            return {"ok": False, "action": "fix", "date": target_date, "error": "EMPTY_LIST", "message": self._service_message("fix", False, "EMPTY_LIST", index=index)}
        if index < 1 or index > len(todos):
            return {"ok": False, "action": "fix", "date": target_date, "error": "INDEX_OUT_OF_RANGE", "message": self._service_message("fix", False, "INDEX_OUT_OF_RANGE", index=index)}
        cleaned_content = re.sub(r"^(改成|变为|变成|是|为|:)\s*", "", (content or "").strip()).strip()
        if not cleaned_content:
            return {"ok": False, "action": "fix", "date": target_date, "error": "EMPTY_CONTENT", "message": self._service_message("fix", False, "EMPTY_CONTENT", index=index)}
        updated = self.storage.update_todo_content(platform, user_id, target_date, index - 1, cleaned_content)
        if not updated:
            return {"ok": False, "action": "fix", "date": target_date, "error": "UPDATE_FAILED", "message": self._service_message("fix", False, "UPDATE_FAILED", index=index)}
        return {
            "ok": True,
            "action": "fix",
            "date": target_date,
            "index": index,
            "item": {
                "index": index,
                "date": updated.get("date"),
                "time": updated.get("time"),
                "content": updated.get("content"),
                "status": updated.get("status", "pending")
            },
            "message": self._service_message("fix", True, index=index)
        }

    @filter.llm_tool(name="todo_check")
    async def todo_tool_check(self, event: AstrMessageEvent, date: str = ""):
        '''查询待办清单。

        Args:
            date(string): 日期，可为空，支持今天/明天/后天/YYYY-MM-DD/M月D日
        '''
        platform, user_id = self._event_scope(event)
        result = await self._service_check(platform, user_id, date)
        yield event.plain_result(json.dumps(result, ensure_ascii=False))

    @filter.llm_tool(name="todo_add")
    async def todo_tool_add(self, event: AstrMessageEvent, content: str, date: str = "", time: str = ""):
        '''新增待办事项。

        Args:
            content(string): 待办原始内容
            date(string): 可选日期，支持YYYY-MM-DD或自然日期表达
            time(string): 可选时间，格式建议HH:MM
        '''
        platform, user_id = self._event_scope(event)
        result = await self._service_add(event, platform, user_id, content, date, time)
        yield event.plain_result(json.dumps(result, ensure_ascii=False))

    @filter.llm_tool(name="todo_done")
    async def todo_tool_done(self, event: AstrMessageEvent, selector: str, date: str = ""):
        '''标记待办完成。

        Args:
            selector(string): 序号、序号列表或内容关键词
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        result = await self._service_done(platform, user_id, selector, date)
        yield event.plain_result(json.dumps(result, ensure_ascii=False))

    @filter.llm_tool(name="todo_fix")
    async def todo_tool_fix(self, event: AstrMessageEvent, index: int, content: str, date: str = ""):
        '''修改指定待办内容。

        Args:
            index(number): 待办序号，从1开始
            content(string): 新的待办内容
            date(string): 可选日期，不传默认今天
        '''
        platform, user_id = self._event_scope(event)
        result = await self._service_fix(platform, user_id, index, content, date)
        yield event.plain_result(json.dumps(result, ensure_ascii=False))

    @filter.regex(r"^(todo|add|done|fix|check)\s*.*")
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

        explicit_match = re.match(r"^(todo|add|done|fix|check)\s*(.*)", message_str, re.IGNORECASE)
        if not explicit_match:
            return
        command_prefix = explicit_match.group(1).lower()
        todo_content = explicit_match.group(2).strip()

        user_id = event.get_sender_id()
        try:
            platform = event.unified_msg_origin.split(":")[0]
        except (AttributeError, IndexError):
            platform = "unknown"

        provider_id_for_user = await self._get_provider_id_from_origin(event.unified_msg_origin)
        self.storage.register_user(platform, user_id, event.unified_msg_origin, provider_id_for_user)

        if command_prefix == 'check':
            async for result in self._handle_check_command(event, platform, user_id, todo_content, None):
                yield result
            return

        if not todo_content:
            yield await self._reply_with_persona(event, f"请输入{command_prefix}的具体内容。")
            return

        if command_prefix == 'done':
            async for result in self._handle_done_command(event, platform, user_id, todo_content):
                yield result
            return

        if command_prefix == 'fix':
            async for result in self._handle_fix_command(event, platform, user_id, todo_content):
                yield result
            return

        provider_id = provider_id_for_user
        add_result = None
        if command_prefix == 'todo':
            if not provider_id:
                yield event.plain_result("未配置 LLM Provider。")
                return
            today = datetime.now().strftime("%Y-%m-%d")
            current_todos = self.storage.load_todos(platform, user_id, today)
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
                if isinstance(payload, list):
                    add_result = await self._service_add(event, platform, user_id, todo_content, persist=False, parsed_todos=payload)
                else:
                    add_result = await self._service_add(event, platform, user_id, todo_content, persist=False)
            elif intent_type == 'cancel':
                yield await self._reply_with_persona(event, "好的，什么都不做。")
                return
            else:
                yield await self._reply_with_persona(event, "抱歉，我没太理解您的意思。")
                return
        else:
            add_result = await self._service_add(event, platform, user_id, todo_content, persist=False)

        if not add_result or not add_result.get("ok"):
            fail_message = add_result.get("message") if isinstance(add_result, dict) else "未能识别到任何待办事项。"
            if fail_message == "未配置 LLM Provider。":
                yield event.plain_result(fail_message)
            else:
                yield await self._reply_with_persona(event, fail_message)
            return
        todos = add_result.get("items") or []
        action_type = 'append' 
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
        result = await self._service_done(platform, user_id, content, "")
        if not result.get("ok"):
            yield await self._reply_with_persona(event, result.get("message", "处理失败，请重试。"))
            return
        if result.get("updated_count", 0) == 0:
            yield await self._reply_with_persona(event, result.get("message", "所选的待办事项已经是完成状态啦。"))
            return

        today = datetime.now().strftime("%Y-%m-%d")
        fresh_todos = self.storage.load_todos(platform, user_id, today)
        preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
        yield await self._reply_with_persona(event, f"{result.get('message', '已更新状态。')}\n\n{preview}")

    async def _handle_check_command(self, event: AstrMessageEvent, platform: str, user_id: str, query_text: str = "", payload=None):
        target_date = self._resolve_check_date(query_text, payload)
        service_result = await self._service_check(platform, user_id, target_date)
        todos = []
        for item in service_result.get("items", []):
            todos.append({
                "date": item.get("date"),
                "time": item.get("time"),
                "content": item.get("content"),
                "status": item.get("status", "pending")
            })
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
        match = re.match(r"^(\d+)\s*(.*)", content)
        if not match:
            yield await self._reply_with_persona(event, "格式错误。请使用：fix 序号 新内容\n例如：fix 3 改成光电数据集会议")
            return
            
        idx = int(match.group(1))
        raw_new_content = match.group(2).strip()
        if not raw_new_content:
            yield await self._reply_with_persona(event, "请输入新的待办内容。")
            return

        result = await self._service_fix(platform, user_id, idx, raw_new_content, "")
        if not result.get("ok"):
            yield await self._reply_with_persona(event, result.get("message", "修改失败，请重试。"))
            return
        if result.get("ok"):
            today = datetime.now().strftime("%Y-%m-%d")
            fresh_todos = self.storage.load_todos(platform, user_id, today)
            preview = self._format_preview(fresh_todos, include_confirm_prompt=False)
            yield await self._reply_with_persona(event, f"{result.get('message', f'已修改第 {idx} 条待办。')}\n\n{preview}")
        else:
            yield await self._reply_with_persona(event, "修改失败，请重试。")
