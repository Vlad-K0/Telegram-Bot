import logging
from flask import Flask, request, abort
import os
import json
import asyncio # Для запуска асинхронных функций из Flask

# --- Настройки ---
# Предполагается, что этот скрипт имеет доступ к тому же .env файлу, что и бот
from dotenv import load_dotenv
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# --- Конец настроек ---

# --- Импорт логики обработки из вашего основного файла бота ---
# Это самый сложный момент, если webhook_listener.py и my_telegram_bot.py - разные файлы/процессы.
# Для этого примера я предполагаю, что мы можем импортировать process_yookassa_notification
# и что у нас есть способ получить/создать экземпляр telegram.Bot.

# Попытка импортировать обработчик и модели
# В реальном проекте это может потребовать реструктуризации вашего кода,
# чтобы избежать циклических импортов и сделать 'process_yookassa_notification'
# и 'AsyncSessionLocal' легко импортируемыми.
# Возможно, 'process_yookassa_notification' и логику БД лучше вынести в отдельные модули.

# Заглушка: Предположим, что у нас есть доступ к функции process_yookassa_notification
# и мы можем создать экземпляр бота для отправки сообщений.
# В идеале, вы должны передать экземпляр Application или Bot в этот Flask-сервер при его запуске,
# или Flask-сервер должен сам инициализировать telegram.Bot.

# Глобальный экземпляр Application или Bot из my_telegram_bot.py
# Это не будет работать напрямую, если это разные процессы, без IPC или общей памяти.
# Для упрощения этого примера, мы сделаем так, чтобы Flask сам создавал Bot-инстанс,
# а process_yookassa_notification будет адаптирована для работы с этим.

# В my_telegram_bot.py у нас была application_instance_for_webhook
# Мы не можем напрямую получить к ней доступ отсюда, если это другой процесс.

# Вместо этого, Flask создаст свой экземпляр Bot для отправки сообщений
# и process_yookassa_notification будет вызываться с этим экземпляром.

from telegram import Bot as TelegramBotInstance # Импортируем класс Bot

# --- Копируем или импортируем необходимые части из my_telegram_bot.py и database.py ---
# Это не очень хороший подход (дублирование или сложные импорты), но для демонстрации:

# --- Начало блока, который нужно адаптировать/импортировать из ваших файлов ---
# Импорты, необходимые для process_yookassa_notification
from sqlalchemy.future import select
from datetime import datetime, timedelta
from outline_vpn.outline_vpn import OutlineVPN # Нужен outline_client
import uuid # Для имен ключей

# Предполагаем, что database.py доступен для импорта
# или вы копируете определения AsyncSessionLocal, User, Payment, OutlineKey сюда.
# Чтобы избежать этого, лучше вынести их в общий модуль.
try:
    from database import User, OutlineKey, Payment, AsyncSessionLocal
    logger_db_init = logging.getLogger('webhook_db_init') # Отдельный логгер
    logger_db_init.info("Модели БД успешно импортированы в webhook_listener.")
except ImportError as e:
    logging.error(f"Не удалось импортировать модели БД в webhook_listener: {e}")
    # Если модели не загружены, дальнейшая работа с БД невозможна
    User, OutlineKey, Payment, AsyncSessionLocal = None, None, None, None


# Инициализация Outline клиента (нужна для process_yookassa_notification)
API_URL = os.getenv("API_URL")
CERT_SHA256 = os.getenv("CERT_SHA256")
outline_client_webhook = None
if API_URL and CERT_SHA256:
    try:
        outline_client_webhook = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
        logging.info("Webhook: Outline VPN client initialized.")
    except Exception as e:
        logging.error(f"Webhook: Error initializing Outline client: {e}")
else:
    logging.warning("Webhook: API_URL or CERT_SHA256 for Outline not found in .env.")


# Адаптированная функция process_yookassa_notification
# Она будет использовать свой outline_client_webhook и свой bot_instance
async def process_yookassa_notification_standalone(notification_data: dict, bot_instance: TelegramBotInstance | None):
    # Эта функция почти идентична той, что в my_telegram_bot.py,
    # но использует outline_client_webhook и bot_instance, переданные ей.
    # Убедитесь, что здесь используется AsyncSessionLocal, импортированный выше.

    logger_webhook_process = logging.getLogger('yookassa_process') # Отдельный логгер
    logger_webhook_process.info(f"Webhook Standalone: Processing Yookassa notification: {notification_data}")
    
    event = notification_data.get("event")
    payment_object = notification_data.get("object")

    if not (event and payment_object and payment_object.get("id")):
        logger_webhook_process.error("Webhook Standalone: Invalid Yookassa notification data.")
        return

    yookassa_payment_id = payment_object.get("id")
    status_from_yookassa = payment_object.get("status")

    if event == "payment.succeeded" and status_from_yookassa == "succeeded":
        logger_webhook_process.info(f"Webhook Standalone: Payment {yookassa_payment_id} SUCCEEDED.")
        
        if not AsyncSessionLocal: # Проверка, что сессия БД доступна
            logger_webhook_process.error("Webhook Standalone: AsyncSessionLocal is not available. Cannot process payment.")
            return

        session = AsyncSessionLocal()
        try:
            stmt_payment = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            db_payment = (await session.execute(stmt_payment)).scalar_one_or_none()

            if not db_payment:
                logger_webhook_process.error(f"Webhook Standalone: Payment {yookassa_payment_id} not found in DB.")
                return
            if db_payment.status == "succeeded":
                logger_webhook_process.warning(f"Webhook Standalone: Payment {yookassa_payment_id} already processed.")
                return

            additional_data = json.loads(db_payment.additional_data or '{}')
            internal_user_db_id = int(additional_data.get("internal_user_db_id"))
            duration_days = int(additional_data.get("duration_days", 30))
            telegram_user_id = int(additional_data.get("telegram_user_id"))

            db_payment.status = "succeeded"
            db_payment.updated_at = datetime.utcnow()
            
            db_user = await session.get(User, internal_user_db_id)
            if not db_user:
                logger_webhook_process.error(f"Webhook Standalone: User DB ID {internal_user_db_id} not found for payment {db_payment.id}.")
                await session.rollback(); return

            if not outline_client_webhook: # Используем локальный outline_client_webhook
                logger_webhook_process.error(f"Webhook Standalone: Outline_client not init for payment {db_payment.id}.")
                await session.rollback()
                if bot_instance: await bot_instance.send_message(telegram_user_id, "Оплата прошла, но проблема с VPN сервисом. Свяжитесь с поддержкой.")
                return
            
            key_name = f"tg_user_{db_user.id}_paid_{uuid.uuid4().hex[:4]}"
            # loop = asyncio.get_event_loop() # Не нужно получать loop так в Flask с asyncio.run
            new_key_obj = await asyncio.to_thread(outline_client_webhook.create_key) # Используем asyncio.to_thread для синхронного вызова

            if not new_key_obj:
                logger_webhook_process.error(f"Webhook Standalone: Failed to create Outline key for payment {db_payment.id}.")
                await session.rollback()
                if bot_instance: await bot_instance.send_message(telegram_user_id, "Оплата прошла, но не удалось создать VPN ключ. Свяжитесь с поддержкой.")
                return

            new_db_key = OutlineKey(
                outline_id_on_server=str(new_key_obj.key_id), access_url=new_key_obj.access_url,
                name=getattr(new_key_obj, 'name', key_name), user_id=db_user.id,
                payment_id=db_payment.id, expires_at=datetime.utcnow() + timedelta(days=duration_days)
            )
            session.add(new_db_key)
            session.add(db_payment)
            await session.commit()
            logger_webhook_process.info(f"Webhook Standalone: Payment {yookassa_payment_id} processed. OutlineKey {new_db_key.id} created for user {telegram_user_id}.")

            if bot_instance:
                expires_str = new_db_key.expires_at.strftime('%Y-%m-%d %H:%M')
                escaped_url = new_key_obj.access_url.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)").replace("~", "\\~").replace("`", "\\`").replace(">", "\\>").replace("#", "\\#").replace("+", "\\+").replace("-", "\\-").replace("=", "\\=").replace("|", "\\|").replace("{", "\\{").replace("}", "\\}").replace(".", "\\.").replace("!", "\\!")
                msg_text = f"✅ Оплата прошла успешно!\n\n🔑 Ваш ключ Outline:\n`{escaped_url}`\n\nДействителен до: {expires_str} UTC"
                await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text, parse_mode='MarkdownV2')
        except Exception as e:
            logger_webhook_process.error(f"Webhook Standalone: Error processing payment.succeeded {yookassa_payment_id}: {e}", exc_info=True)
            await session.rollback()
        finally:
            await session.close()

    elif event == "payment.canceled":
        logger_webhook_process.info(f"Webhook Standalone: Payment {yookassa_payment_id} CANCELED.")
        if not AsyncSessionLocal: return
        session = AsyncSessionLocal()
        try:
            stmt_payment = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            db_payment = (await session.execute(stmt_payment)).scalar_one_or_none()
            if db_payment and db_payment.status != "canceled":
                db_payment.status = "canceled"; db_payment.updated_at = datetime.utcnow()
                session.add(db_payment); await session.commit()
                logger_webhook_process.info(f"Webhook Standalone: Payment {yookassa_payment_id} status updated to canceled in DB.")
        except Exception as e:
            logger_webhook_process.error(f"Webhook Standalone: Error updating canceled payment {yookassa_payment_id}: {e}", exc_info=True)
            await session.rollback()
        finally:
            await session.close()
    else:
        logger_webhook_process.info(f"Webhook Standalone: Received Yookassa event '{event}' for payment {yookassa_payment_id}. Not processed.")
# --- Конец блока адаптации/импорта ---


# Инициализация Flask-приложения
flask_app = Flask(__name__) # Переименовал, чтобы не конфликтовать с app из PTB

# Настройка логирования для Flask (если нужно отдельно от основного)
if not flask_app.debug:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    flask_app.logger.handlers = gunicorn_logger.handlers
    flask_app.logger.setLevel(gunicorn_logger.level)
else:
    logging.basicConfig(level=logging.INFO, format='Flask Webhook: %(asctime)s - %(levelname)s - %(message)s')


# Инициализируем экземпляр Telegram бота для отправки сообщений
# Убедитесь, что BOT_TOKEN загружен из .env
telegram_bot_instance = None
if BOT_TOKEN:
    telegram_bot_instance = TelegramBotInstance(token=BOT_TOKEN)
    logging.info("Webhook: Telegram Bot instance for sending messages initialized.")
else:
    logging.error("Webhook: BOT_TOKEN not found, unable to initialize Telegram Bot for sending messages.")


@flask_app.route('/yookassa_webhook', methods=['POST']) # Изменил URL на тот, что у вас в .env
def yookassa_webhook_route(): # Переименовал функцию, чтобы не конфликтовать
    json_data = request.get_json()
    flask_app.logger.info(f"Webhook received data: {json_data}")

    if not json_data:
        flask_app.logger.warning("Webhook: Empty JSON data received.")
        abort(400, description="Empty JSON data")

    # Запускаем асинхронную обработку в отдельном потоке/задаче,
    # чтобы не блокировать Flask и быстро вернуть ответ ЮKassa.
    # asyncio.create_task() или asyncio.run() здесь могут потребовать настройки event loop,
    # если Flask работает в синхронном режиме.
    # Проще всего для Flask - использовать asyncio.to_thread для асинхронной функции,
    # либо если ваш WSGI сервер поддерживает ASGI (как Hypercorn с Quart),
    # то можно делать полноценный async def.

    # Для простого Flask (WSGI) и вызова async функции:
    try:
        # Это вызовет process_yookassa_notification_standalone в текущем потоке,
        # но сама функция асинхронная. Для Flask лучше использовать
        # loop = asyncio.new_event_loop()
        # asyncio.set_event_loop(loop)
        # loop.run_until_complete(process_yookassa_notification_standalone(json_data, telegram_bot_instance))
        # Или, если у вас Flask >= 2.0, можно использовать app.ensure_sync
        # asyncio.run(process_yookassa_notification_standalone(json_data, telegram_bot_instance))
        # Это будет блокирующим вызовом для Flask-обработчика.

        # Чтобы сделать неблокирующим (Flask ответит 200 OK сразу, а обработка пойдет в фоне):
        # Нужно использовать фоновые задачи. Для Flask это не так просто, как для FastAPI.
        # Простейший вариант - запустить в новом потоке, но это не идеально для asyncio.

        # Для демонстрации, сделаем блокирующий вызов с новым event loop
        # (не рекомендуется для продакшена без тщательной настройки)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(process_yookassa_notification_standalone(json_data, telegram_bot_instance))
        loop.close()

    except Exception as e:
        flask_app.logger.error(f"Webhook: Error during async processing: {e}", exc_info=True)
        # Важно все равно вернуть 200 OK ЮKassa, если это не ошибка запроса,
        # а ошибка нашей внутренней обработки, чтобы ЮKassa не повторяла запросы.
        # Но если это ошибка формата запроса, то можно и 400.

    return '', 200

if __name__ == '__main__':
    # Убедитесь, что у вас установлен Flask: pip install Flask
    # Для запуска: python webhook_listener.py
    # И затем ngrok: ngrok http 5001 (или ваш порт)
    # И этот ngrok URL укажите в ЮKassa
    logging.info("Starting Flask webhook listener on port 5001...")
    flask_app.run(host='0.0.0.0', port=5001)

