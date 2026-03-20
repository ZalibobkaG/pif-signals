"""Конфигурация приложения."""

# Баланс для расчёта риска (USDT). Из .env TRADING_BALANCE или 100 по умолчанию.
BALANCE = float(200)

# Период сохранения бекапа trades_dict (сек).
BACKUP_INTERVAL_SECONDS = 100

# Максимальное количество backup-файлов в logs/backups.
BACKUP_MAX_FILES = 100
