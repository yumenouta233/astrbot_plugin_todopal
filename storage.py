import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Union

logger = logging.getLogger("astrbot")

class TodoStorage:
    """
    Handles local JSON storage for TodoPal plugin.
    
    Directory structure:
    data/plugin_data/todopal/{platform}/{user_id}/{year}/{month}/{date}.json
    """

    def __init__(self, base_path: str = "data/plugin_data/todopal"):
        """
        Initialize TodoStorage.

        Args:
            base_path: Base directory for storage.
        """
        self.base_path = Path(base_path)
        self.users_file = self.base_path / "users.json"
        self._ensure_users_file()

    def _ensure_users_file(self):
        if not self.users_file.exists():
            self.ensure_directory(self.users_file)
            with open(self.users_file, 'w', encoding='utf-8') as f:
                json.dump({}, f)

    def register_user(self, platform: str, user_id: str, origin: str):
        """Register or update a user's unified message origin for proactive messaging."""
        try:
            with open(self.users_file, 'r', encoding='utf-8') as f:
                users = json.load(f)
            
            key = f"{platform}_{user_id}"
            users[key] = {
                "platform": platform,
                "user_id": user_id,
                "origin": origin,
                "last_active": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            with open(self.users_file, 'w', encoding='utf-8') as f:
                json.dump(users, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to register user {user_id}: {e}")

    def get_all_users(self) -> List[Dict]:
        """Get all registered users."""
        try:
            with open(self.users_file, 'r', encoding='utf-8') as f:
                users = json.load(f)
            return list(users.values())
        except Exception as e:
            logger.error(f"Failed to read users: {e}")
            return []

    def _get_file_path(self, platform: str, user_id: str, date_str: str) -> Path:
        """
        Construct the file path for a specific user and date.

        Args:
            platform: Platform identifier (e.g., 'qq').
            user_id: User identifier.
            date_str: Date string in 'YYYY-MM-DD' format.

        Returns:
            Path object pointing to the JSON file.
        """
        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            year = str(date_obj.year)
            month = f"{date_obj.month:02d}"
            
            # Sanitize platform and user_id to be safe for filenames
            safe_platform = "".join(c for c in platform if c.isalnum() or c in ('_', '-'))
            safe_user_id = "".join(c for c in user_id if c.isalnum() or c in ('_', '-'))
            
            return self.base_path / safe_platform / safe_user_id / year / month / f"{date_str}.json"
        except ValueError:
            # Fallback if date parsing fails, though date_str should come from verified source
            return self.base_path / "unknown" / f"{date_str}.json"

    def ensure_directory(self, file_path: Path):
        """
        Ensure the directory for the file exists.

        Args:
            file_path: Path to the file.
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)

    def load_todos(self, platform: str, user_id: str, date_str: str) -> List[Dict]:
        """
        Load todos for a specific date.

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.

        Returns:
            List of todo items (dicts). Returns empty list if file doesn't exist.
        """
        file_path = self._get_file_path(platform, user_id, date_str)
        if not file_path.exists():
            return []
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                logger.warning(f"Data in {file_path} is not a list. Returning empty list.")
                return []
        except (json.JSONDecodeError, OSError) as e:
            logger.error(f"Failed to load todos from {file_path}: {e}")
            return []

    def rollover_pending_todos(self, platform: str, user_id: str, from_date_str: str, to_date_str: str) -> int:
        """
        Move pending todos from one date to another. Returns number of rolled over items.
        """
        from_todos = self.load_todos(platform, user_id, from_date_str)
        if not from_todos:
            return 0
            
        pending_items = [t for t in from_todos if t.get('status') != 'done']
        if not pending_items:
            return 0
            
        # Append to target date
        self.append_todos(platform, user_id, to_date_str, pending_items)
        
        # Mark as rolled over in old file (optional, but good for data consistency)
        for t in from_todos:
            if t.get('status') != 'done':
                t['status'] = 'rolled_over'
                t['updated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.save_todos(platform, user_id, from_date_str, from_todos)
        
        return len(pending_items)

    def save_todos(self, platform: str, user_id: str, date_str: str, todos: List[Dict]):
        """
        Save todos for a specific date (overwrite existing).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.
            todos: List of todo items to save.
        """
        file_path = self._get_file_path(platform, user_id, date_str)
        self.ensure_directory(file_path)
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(todos, f, ensure_ascii=False, indent=2)
            logger.info(f"Saved {len(todos)} todos to {file_path}")
        except OSError as e:
            logger.error(f"Failed to save todos to {file_path}: {e}")

    def append_todos(self, platform: str, user_id: str, date_str: str, new_todos: List[Dict]) -> List[Dict]:
        """
        Append new todos to existing ones for a specific date.

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string 'YYYY-MM-DD'.
            new_todos: List of new todo items.

        Returns:
            The updated list of all todos.
        """
        current_todos = self.load_todos(platform, user_id, date_str)
        
        # Simple append. In a real app, you might check for duplicates.
        # Ensure IDs are unique? For simplicity, we assume generated IDs are unique enough
        # or we re-generate IDs if needed. Here we just append.
        
        updated_todos = current_todos + new_todos
        self.save_todos(platform, user_id, date_str, updated_todos)
        return updated_todos

    def update_todo_status(self, platform: str, user_id: str, date_str: str, todo_index: int, status: str) -> Optional[Dict]:
        """
        Update the status of a specific todo item by index (0-based).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string.
            todo_index: Index of the todo item in the list.
            status: New status (e.g., 'done').

        Returns:
            The updated todo item if successful, None otherwise.
        """
        todos = self.load_todos(platform, user_id, date_str)
        if 0 <= todo_index < len(todos):
            todos[todo_index]['status'] = status
            todos[todo_index]['updated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if status == 'done':
                todos[todo_index]['done_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            self.save_todos(platform, user_id, date_str, todos)
            return todos[todo_index]
        return None

    def update_todo_content(self, platform: str, user_id: str, date_str: str, todo_index: int, new_content: str) -> Optional[Dict]:
        """
        Update the content of a specific todo item by index (0-based).

        Args:
            platform: Platform identifier.
            user_id: User identifier.
            date_str: Date string.
            todo_index: Index of the todo item in the list.
            new_content: New content string.

        Returns:
            The updated todo item if successful, None otherwise.
        """
        todos = self.load_todos(platform, user_id, date_str)
        if 0 <= todo_index < len(todos):
            todos[todo_index]['content'] = new_content
            todos[todo_index]['updated_at'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # Preserve source_text or update it? Maybe append modification note?
            # For simplicity, we keep source_text as original or update it to reflect modification.
            # Let's keep original source_text but maybe add a note if we had a field for it.
            # Here we just update content.
            
            self.save_todos(platform, user_id, date_str, todos)
            return todos[todo_index]
        return None
