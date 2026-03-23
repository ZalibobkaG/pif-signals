import asyncio
import gzip
import os
import copy
from datetime import datetime, timezone
from pprint import pformat

from loguru import logger


def _write_trades_backup_sync(backups_dir: str, snapshot: dict) -> str:
    """
    Синхронная запись снапшота trades_dict в .txt.gz.
    Формат файла — текстовый дамп словаря, упакованный gzip.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"trades_dict_{ts}.txt.gz"
    path = os.path.join(backups_dir, filename)
    payload = pformat(snapshot, width=140, sort_dicts=False)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(payload)
    return path


def _prune_old_backups_sync(backups_dir: str, keep_last: int = 100) -> int:
    """
    Оставляет только keep_last самых новых .txt.gz бекапов.
    Возвращает число удаленных файлов.
    """
    entries = []
    for name in os.listdir(backups_dir):
        if not name.endswith(".txt.gz"):
            continue
        full_path = os.path.join(backups_dir, name)
        if os.path.isfile(full_path):
            entries.append(full_path)

    if len(entries) <= keep_last:
        return 0

    entries.sort(key=lambda p: os.path.getmtime(p))
    to_delete = entries[: len(entries) - keep_last]
    removed = 0
    for p in to_delete:
        try:
            os.remove(p)
            removed += 1
        except Exception:
            pass
    return removed


async def backup_trades_dict_loop(trades: dict, logs_dir: str, interval_sec: int, keep_last: int = 100):
    """
    Периодически сохраняет бекап trades_dict в logs/backups.
    Работает отдельным async-таском.
    """
    backups_dir = os.path.join(logs_dir, "backups")
    os.makedirs(backups_dir, exist_ok=True)
    logger.info(f"Backup loop started: every {interval_sec}s -> {backups_dir}")
    while True:
        try:
            # Сделаем копию, чтобы во время форматирования trades_dict не менялся.
            snapshot = copy.deepcopy(trades)
            backup_path = await asyncio.to_thread(_write_trades_backup_sync, backups_dir, snapshot)
            removed = await asyncio.to_thread(_prune_old_backups_sync, backups_dir, keep_last)
            logger.debug(f"trades_dict backup saved: {backup_path}")
            if removed:
                logger.debug(f"trades_dict backup prune: removed {removed} old files")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"trades_dict backup failed: {e}")
        await asyncio.sleep(interval_sec)

