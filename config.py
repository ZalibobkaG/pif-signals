"""Конфигурация приложения: читается из config.yaml (путь можно переопределить через CONFIG_PATH)."""
import os

import yaml

_DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
CONFIG_PATH = os.environ.get("CONFIG_PATH", _DEFAULT_PATH)


def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_cfg = _load(CONFIG_PATH)
_trading = _cfg.get("trading") or {}
_backup = _cfg.get("backup") or {}
_ai = _cfg.get("ai") or {}

# Баланс для расчёта риска (USDT).
BALANCE = float(_trading.get("balance", 200))

# Период сохранения бекапа trades_dict (сек).
BACKUP_INTERVAL_SECONDS = int(_backup.get("interval_seconds", 1000))

# Максимальное количество backup-файлов в logs/backups.
BACKUP_MAX_FILES = int(_backup.get("max_files", 100))

# LLM: OpenAI-совместимый API.
AI_BASE_URL = _ai.get("base_url", "https://api.deepseek.com")
AI_MODEL = _ai.get("model", "deepseek-chat")
