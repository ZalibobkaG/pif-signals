import os
import asyncio
import json
from openai import OpenAI

from loguru import logger

SYSTEM_PROMPT = """
Ты получаешь сообщение из телеграм канала с торговыми сигналами от разных трейдеров. Тебе нужно обработать это сообщение и выдать
информацию из этого сообщения (или нескольких сообщений) в формате JSON. В канале бывают несколько видов сообщений: 
- Сигнал или дополнение к сигналу от трейдера
- Информация от модератора канала (пример: Привет друзья, админ на связи...). Часто эти сообщения присылаются с хештегом #admin. 
Вид сообщений от трейдеров (они почти всегда пишутся с такой частью текста: [trader #A] или [trader #B] и другие трейдеры):
1) начальный сигнал от трейдера (название монеты, направление long/short, цена входа (может быть двухфазный вход), stop loss, take profit (их может быть несколько), риск на сделку)
2) дополнения/изменения в сигнале (например передвинут stop loss в безубыток, закрыть остаток, убрать/отменить лимитный ордер, вход по текущим)
3) статистика (например тейк1 0.93RR, sl: -0,5)

Ты можешь использовать только допустимые категории действий исходя из смысла сообщения. Вот список допустимых категорий (они расположены по приоритету) и поводы их использовать: 
- place_new_order используется, когда трейдер выложил сетап с точками входа, названием монеты, направление long/short, цена входа (может быть двухфазный вход), stop loss, take profit (их может быть несколько), риск на сделку;
- enter_now используется, если трейдер говорит что нужно совершить вход по текущим ценам или он совершает вход по текущим;
- stop_breakeven используется, если трейдер говорит что нужно переставить стоп-лосс в безубыток или говорит можете перевести стоп-лосс в безубыток, если желаете/переживаете;
- cancel используется, если трейдер говорит что нужно отменить/убрать лимитку. редко говорят что ушла без нас - это тоже означает что нужно убрать лимитку;
- close_entire используется, если трейдер говорит что закрывает позицию (или ее остаток);
- info используется, если трейдер говорит ненужную информацию/статистику о сделке, например тейк1 0.93RR, sl: -0,5
- help используется, если ты не можешь определить 

Формат твоего ответного сообщения JSON:
{ 
'action': 'place_new_order', 
'trader': 'trader #A',
'coin': 'XRPUSDT',
'side': 'long',
'entry': [1.80, 1.74],
'sl': 1.5,
'tp': [2, 2.1, 2.5],
'risk': '1%'
}

Дополнения: у trader #D у нас всегда риск на сделку 0,5%. У других трейдеров, если не указан риск на сделку - пиши 1%. Может быть указано 0,3% деп (депозит/баланс) - это означает риск 0,3%
Используй только этот формат ответа. В твоем ответе всегда должно быть указано action, trader, coin исходя из контекста этого сетапа. 
Если другая информация не нужна (например при cancel нам не нужно риск, стоп, тейк) пиши None.
У нас может быть одна или несколько точек входа, но всегда нужно отправлять список в квадратных скобках.
При определении категории акцентируй внимание на последнем сообщении, смысл действия должен быть составлен по последнему сообщению.
"""


class WorkWithAI:
    def __init__(
        self,
        messages_queue: asyncio.Queue,
        api_key: str = None,
        base_url: str = "https://api.deepseek.com",
        ai_response_queue: asyncio.Queue = None,
    ):
        self.messages_queue = messages_queue
        self.api_key = api_key or os.environ.get("AI_API_KEY", "")
        self.client = OpenAI(api_key=self.api_key, base_url=base_url)
        self.ai_response_queue = ai_response_queue if ai_response_queue is not None else asyncio.Queue()

    def _sync_get_response(self, tg_message: str) -> dict:
        response = self.client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": tg_message},
            ],
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)

    async def get_response(self, tg_message: str) -> dict:
        return await asyncio.to_thread(self._sync_get_response, tg_message)

    async def listen(self):
        """Берёт из очереди полные треды и отдаёт в AI. Результаты (JSON) кладёт в ai_response_queue.

        В очередь от монитора прилетает dict вида {"text": full_text, "root_id": <id начального сообщения>}.
        В промпт уходит только text, а в ответ добавляем message_id=root_id.
        """
        while True:
            try:
                item = self.messages_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue

            # Поддерживаем и старый формат (когда в очереди лежала просто строка)
            if isinstance(item, dict) and "text" in item:
                full_text = item["text"]
                root_id = item.get("root_id")
            else:
                full_text = item
                root_id = None
            try:
                logger.info(f"FULL TEXT: {full_text}")
                result = await self.get_response(full_text)
                if isinstance(result, dict):
                    if root_id is not None:
                        result["message_id"] = root_id
                    # Исходный текст из канала (сообщение + реплаи) для отображения в ТГ
                    result["source_text"] = full_text

                await self.ai_response_queue.put(result)
                logger.info(f" 🤖 {result['action']} {result['trader']} {result['coin']} {result['side']} {result['entry']} {result['sl']} {result['tp']} {result['risk']}")
            except Exception as e:
                logger.error(f"AI error: {e}")
