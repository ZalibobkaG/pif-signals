import asyncio
import json
import os
import sys

from loguru import logger
from pathlib import Path
from typing import Dict, Any

# Корень проекта в path, чтобы импорт utils работал при запуске из любой директории
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from dotenv import load_dotenv

from utils.work_with_ai import WorkWithAI

_BASE_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
LOGS_DIR = os.path.join(_BASE_DIR, "logs")
HISTORY_FILE = os.path.join(LOGS_DIR, "messages_history.json")
OUT_FILE = os.path.join(LOGS_DIR, "messages_history_ai.json")


def _load_messages_history(limit: int = 30) -> Dict[int, Any]:
    """Читает первые `limit` root-сообщений из messages_history.json."""
    if not os.path.exists(HISTORY_FILE):
        raise FileNotFoundError(f"messages_history.json not found at {HISTORY_FILE}")

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Ключи в файле — строки, конвертим в int и сортируем
    root_ids = sorted(int(k) for k in data.keys())
    root_ids = root_ids[:limit]

    result: Dict[int, Any] = {}
    for root_id in root_ids:
        raw = data.get(str(root_id))
        if isinstance(raw, dict):
            # Приводим id сообщений к int
            result[root_id] = {int(k2): v2 for k2, v2 in raw.items()}
        else:
            result[root_id] = raw
    return result


async def _process_root(
    ai: WorkWithAI,
    root_id: int,
    root_data: Any,
    out: Dict[str, Dict[str, Any]],
    ) -> None:
    """
    Обрабатывает один root:
    - сначала root сообщение;
    - если есть реплаи/дополнения — по очереди, накапливая тред, как в боевом режиме.

    Каждый ответ AI сохраняется к конкретному msg_id.
    """
    # В выходном JSON всегда храним по строковым ключам
    if isinstance(root_data, str):
        full_text = root_data
        logger.info(f"Replaying root {root_id}")
        ai_result = await ai.get_response(full_text)
        # Явно сохраняем message_id исходного сообщения
        if isinstance(ai_result, dict):
            ai_result.setdefault("message_id", root_id)

        out.setdefault(str(root_id), {})
        out[str(root_id)][str(root_id)] = {
            "text": root_data,
            "full_text": full_text,
            "ai": ai_result,
        }
        return

    # dict: {msg_id -> text}
    # Сортируем сообщения по id и идём по очереди: root, затем реплаи/дополнения
    msgs: Dict[int, str] = root_data
    ordered_ids = sorted(msgs.keys())

    parts: list[str] = []
    for msg_id in ordered_ids:
        text = msgs[msg_id]
        parts.append(text)
        full_text = "\n\n---\n\n".join(parts)

        logger.info(f"Replaying root {root_id}, msg {msg_id}")
        ai_result = await ai.get_response(full_text)
        if isinstance(ai_result, dict):
            ai_result.setdefault("message_id", msg_id)

        out.setdefault(str(root_id), {})
        out[str(root_id)][str(msg_id)] = {
            "text": text,
            "full_text": full_text,
            "ai": ai_result,
        }


async def main(limit: int = 30) -> None:
    """
    Прогоняет первые `limit` root-сообщений из messages_history.json через AI.

    - Для каждого root: сначала отправляется root-сообщение.
    - Если у него есть реплаи/ссылки-дополнения — они отправляются по очереди,
      причём в промпт попадает накопленный тред (root + все предыдущие).
    - Ответ AI для каждого msg_id сохраняется в logs/messages_history_ai.json.
    """
    load_dotenv()

    os.makedirs(LOGS_DIR, exist_ok=True)
    logger.add(os.path.join(LOGS_DIR, "replay_history_with_ai.log"), encoding="utf-8")

    history = _load_messages_history(limit=limit)

    # WorkWithAI ожидает очередь, но здесь мы используем только get_response
    dummy_queue: asyncio.Queue = asyncio.Queue()
    ai = WorkWithAI(messages_queue=dummy_queue)

    out: Dict[str, Dict[str, Any]] = {}

    for root_id, root_data in history.items():
        try:
            await _process_root(ai, root_id, root_data, out)
        except Exception as e:
            logger.exception(f"Error processing root {root_id}: {e}")

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    logger.info(f"Saved AI results for {len(out)} roots to {OUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())

