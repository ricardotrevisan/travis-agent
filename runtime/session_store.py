"""
In-memory conversation history store, per sender.

Env vars (all optional):
  SESSION_MAX_USERS            — max number of senders kept in memory (LRU eviction). Default: 500
  SESSION_MAX_STORED_TURNS     — turns stored per sender (each turn = 1 user + 1 assistant msg). Default: 30
  SESSION_RECENT_TURNS_FOR_PROMPT — turns injected into LLM prompts. Default: 10
"""
import os
import threading
from collections import OrderedDict

_MAX_USERS = int(os.getenv("SESSION_MAX_USERS", "500"))
_MAX_STORED_TURNS = int(os.getenv("SESSION_MAX_STORED_TURNS", "30"))
_RECENT_TURNS_FOR_PROMPT = int(os.getenv("SESSION_RECENT_TURNS_FOR_PROMPT", "10"))

# Each value is a list of {"role": "user"|"assistant", "content": str}
# OrderedDict gives us LRU eviction by moving accessed keys to the end.
_store: OrderedDict[str, list[dict]] = OrderedDict()
_lock = threading.Lock()


def get_history(sender: str) -> list[dict]:
    """Return the last SESSION_RECENT_TURNS_FOR_PROMPT messages for this sender."""
    with _lock:
        msgs = _store.get(sender, [])
        _store.move_to_end(sender, last=True) if sender in _store else None
    return list(msgs[-(  _RECENT_TURNS_FOR_PROMPT * 2):])


def append_turn(sender: str, user_text: str, assistant_text: str) -> None:
    """Append a completed turn (user + assistant) to the sender's history."""
    with _lock:
        if sender not in _store:
            if len(_store) >= _MAX_USERS:
                _store.popitem(last=False)  # evict LRU
            _store[sender] = []
        _store.move_to_end(sender, last=True)
        msgs = _store[sender]
        msgs.append({"role": "user", "content": user_text})
        msgs.append({"role": "assistant", "content": assistant_text})
        # Trim to max stored turns (each turn = 2 messages)
        max_msgs = _MAX_STORED_TURNS * 2
        if len(msgs) > max_msgs:
            del msgs[: len(msgs) - max_msgs]


def clear_history(sender: str) -> None:
    with _lock:
        _store.pop(sender, None)
