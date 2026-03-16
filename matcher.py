import re
from typing import List, Dict, Optional, Tuple

class TodoMatcher:
    """
    Helper class to match user input against todo items.
    """

    @staticmethod
    def match_todo(todos: List[Dict], query: str) -> Tuple[Optional[int], Optional[Dict]]:
        """
        Find a todo item based on user query.
        
        Supported queries:
        1. "第X个" -> Matches index X-1.
        2. "XX做完了" -> Matches content containing XX.

        Args:
            todos: List of todo items.
            query: User input string (e.g., "第2个做完了", "买菜做完了").

        Returns:
            Tuple (index, todo_item) if match found, else (None, None).
        """
        if not todos:
            return None, None

        # 1. Try to match "第X个"
        # Regex for "第X个" or just "X" if it's explicitly about index (though query might be mixed)
        index_match = re.search(r"第(\d+)个", query)
        if index_match:
            try:
                idx = int(index_match.group(1)) - 1  # Convert 1-based to 0-based
                if 0 <= idx < len(todos):
                    return idx, todos[idx]
            except ValueError:
                pass

        # 2. Try to match by content
        # Remove "做完了", "完成了", "完成" from the end of the query to get the core content
        clean_query = re.sub(r"(做完了|完成了|完成|已完成)$", "", query).strip()
        
        if not clean_query:
            return None, None

        # Simple substring match
        candidates = []
        for i, todo in enumerate(todos):
            if clean_query in todo['content']:
                candidates.append((i, todo))
        
        if len(candidates) == 1:
            return candidates[0]
        
        # If multiple matches or no match, return None for now (or handle ambiguity later)
        return None, None
