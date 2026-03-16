"""
История привязки монета → root_id из ответов AI.
Используется при sync внешних сетапов Bybit: ищем по монете самый новый root_id (message_id сетапа).
"""
import json
import os
import time

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(_BASE_DIR, "logs")
COIN_ROOT_FILE = os.path.join(LOGS_DIR, "coin_root_history.json")
_MAX_ENTRIES_PER_COIN = 100


def _load() -> dict:
    """Возвращает { "COIN": [ {"root_id": int, "ts": float}, ... ], ... }."""
    if not os.path.exists(COIN_ROOT_FILE):
        return {}
    try:
        with open(COIN_ROOT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    os.makedirs(LOGS_DIR, exist_ok=True)
    with open(COIN_ROOT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_coin_root(coin: str, root_id: int) -> None:
    """Добавить запись: по этой монете был ответ AI с данным root_id (самый новый по ts)."""
    if not coin or root_id is None:
        return
    coin = coin.upper().strip()
    data = _load()
    entries = data.setdefault(coin, [])
    entries.append({"root_id": int(root_id), "ts": time.time()})
    entries.sort(key=lambda x: x["ts"], reverse=True)
    data[coin] = entries[:_MAX_ENTRIES_PER_COIN]
    _save(data)


def get_latest_root_for_coin(coin: str):
    """Самый новый root_id по монете (максимальный ts). Если записей нет — None."""
    if not coin:
        return None
    coin = coin.upper().strip()
    data = _load()
    entries = data.get(coin) or []
    if not entries:
        return None
    return max(entries, key=lambda x: x["ts"])["root_id"]
