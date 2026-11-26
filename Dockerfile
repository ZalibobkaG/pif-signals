FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Сначала зависимости: слой кэшируется, пока не меняется requirements.txt
COPY requirements.txt .
RUN pip install -r requirements.txt \
    # candlelite (зависимость okx) при первом импорте пишет SETTINGS.config в site-packages:
    # создаём его на этапе сборки, пока мы ещё root
    && python -c "import candlelite"

COPY . .

# Работаем не от root; logs/ и tg_sessions/ монтируются снаружи
ARG UID=1000
RUN useradd --uid "${UID}" --create-home bot \
    && mkdir -p logs tg_sessions \
    && chown -R bot:bot logs tg_sessions
USER bot

CMD ["python", "main.py"]
