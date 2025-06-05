import logging
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, Bot as TelegramBotInstance
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from outline_vpn.outline_vpn import OutlineVPN
from dotenv import load_dotenv
from sqlalchemy.future import select
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timedelta
import asyncio
import uuid
from decimal import Decimal
import json

# --- Импорты из вашего файла database.py ---
from database import User, OutlineKey, Payment, create_db_tables, get_async_session, AsyncSessionLocal

# Импорты для APScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Импорты для ЮKassa
from yookassa import Configuration as YooKassaConfiguration
from yookassa import Payment as YooKassaPaymentObject
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder

# Загружаем переменные окружения из .env файла
load_dotenv()

# --- НАСТРОЙКИ из .env файла ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv("API_URL")
CERT_SHA256 = os.getenv("CERT_SHA256")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
YOOKASSA_WEBHOOK_URL = os.getenv("YOOKASSA_WEBHOOK_URL")
BASE_PRICE_PER_MONTH = Decimal(os.getenv("BASE_PRICE_PER_MONTH", "160.00"))
# --- КОНЕЦ НАСТРОЕК ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

outline_client = None
if API_URL and CERT_SHA256:
    try:
        logger.info(f"Подключение к Outline API: URL={API_URL}")
        outline_client = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
        logger.info("Успешное подключение к Outline VPN API.")
    except Exception as e:
        logger.error(f"Ошибка Outline API: {e}", exc_info=True)
else:
    logger.warning("API_URL или CERT_SHA256 не найдены в .env.")

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    try:
        YooKassaConfiguration.account_id = YOOKASSA_SHOP_ID
        YooKassaConfiguration.secret_key = YOOKASSA_SECRET_KEY
        logger.info("Конфигурация ЮKassa установлена.")
    except Exception as e:
        logger.error(f"Ошибка конфигурации ЮKassa: {e}", exc_info=True)
else:
    logger.warning("YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY не найдены в .env.")

BUTTON_BUY_1_MONTH = "Купить на 1 месяц 💳"
BUTTON_BUY_6_MONTHS = "Купить на 6 месяцев 💳"
BUTTON_BUY_1_YEAR = "Купить на 1 год 💳"
yookassa_menu_keyboard = [[KeyboardButton(BUTTON_BUY_1_MONTH)], [KeyboardButton(BUTTON_BUY_6_MONTHS)], [KeyboardButton(BUTTON_BUY_1_YEAR)]]
REPLY_MARKUP_YOOKASSA_MENU = ReplyKeyboardMarkup(yookassa_menu_keyboard, resize_keyboard=True, one_time_keyboard=False)

# Глобальный экземпляр Application для доступа из вебхука
# Это не самый лучший способ, но для начала подойдет.
# В идеале, вебхук-сервер и бот должны быть более разделены или использовать общую шину сообщений.
application_instance_for_webhook: Application | None = None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_tg = update.effective_user
    logger.info(f"User {user_tg.first_name} ({user_tg.id}) started.")
    try:
        async for session in get_async_session():
            stmt = select(User).where(User.telegram_id == user_tg.id)
            db_user = (await session.execute(stmt)).scalar_one_or_none()
            if not db_user:
                db_user = User(telegram_id=user_tg.id, username=user_tg.username, first_name=user_tg.first_name, last_name=user_tg.last_name)
                session.add(db_user)
                try:
                    await session.commit()
                    logger.info(f"User {db_user.username} ({db_user.telegram_id}) added to DB.")
                except Exception as e_commit: # Более общая обработка
                    await session.rollback()
                    logger.error(f"Error saving user {user_tg.id} to DB (start): {e_commit}", exc_info=True)
    except Exception as e_db: # Более общая обработка
        logger.error(f"DB connection error in /start: {e_db}", exc_info=True)
        await update.message.reply_text("Проблема с базой данных. Попробуйте позже.")
        return
    await update.message.reply_html(f"Привет, {user_tg.mention_html()}! 👋\nВыберите срок подписки:", reply_markup=REPLY_MARKUP_YOOKASSA_MENU)

async def initiate_yookassa_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, months: int, duration_days: int):
    user_tg = update.effective_user
    if not user_tg:
        logger.warning("No effective_user in initiate_yookassa_payment.")
        await update.message.reply_text("Не удалось определить пользователя. Попробуйте /start.")
        return

    logger.info(f"User {user_tg.id} chose {months} months subscription.")
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        await update.message.reply_text("Сервис оплаты временно недоступен.")
        return

    payment_amount = BASE_PRICE_PER_MONTH * months
    tariff_identifier = f"{months}m_vpn"
    description = f"Outline VPN, {months} мес. ({payment_amount} RUB)"
    
    try:
        async for session in get_async_session():
            stmt_user = select(User).where(User.telegram_id == user_tg.id)
            db_user = (await session.execute(stmt_user)).scalar_one_or_none()
            if not db_user:
                logger.error(f"User {user_tg.id} not found in DB for payment (should exist after /start).")
                await update.message.reply_text("Ошибка: пользователь не найден. Нажмите /start.")
                return

            idempotency_key = str(uuid.uuid4())
            order_id_internal = f"order_{db_user.id}_{tariff_identifier}_{uuid.uuid4().hex[:6]}"
            return_url = f"https://t.me/{context.bot.username}?order_id={order_id_internal}&status=return"
            yookassa_metadata = {"internal_user_db_id": str(db_user.id), "telegram_user_id": str(user_tg.id), "duration_days": str(duration_days)}

            builder = PaymentRequestBuilder()
            builder.set_amount({"value": str(payment_amount), "currency": "RUB"}) \
                .set_capture(True) \
                .set_confirmation({"type": "redirect", "return_url": return_url}) \
                .set_description(description) \
                .set_metadata(yookassa_metadata)
            payment_request = builder.build()
            loop = asyncio.get_event_loop()
            yookassa_payment_obj = await loop.run_in_executor(None, lambda: YooKassaPaymentObject.create(payment_request, idempotency_key))

            if yookassa_payment_obj and yookassa_payment_obj.confirmation and yookassa_payment_obj.confirmation.confirmation_url:
                new_db_payment = Payment(yookassa_payment_id=yookassa_payment_obj.id, user_id=db_user.id, amount=payment_amount, currency="RUB", status=yookassa_payment_obj.status, description=description, additional_data=json.dumps(yookassa_metadata))
                session.add(new_db_payment)
                await session.commit()
                logger.info(f"Created Yookassa payment ID: {yookassa_payment_obj.id} for user {db_user.id}, status: {yookassa_payment_obj.status}.")
                await update.message.reply_text(f"Для оплаты {months} мес. ({payment_amount} RUB) перейдите:\n{yookassa_payment_obj.confirmation.confirmation_url}\n\nКлюч будет выдан после оплаты.")
            else:
                logger.error(f"Yookassa payment creation failed for user {db_user.id}. Response: {yookassa_payment_obj}")
                await update.message.reply_text("Не удалось создать ссылку на оплату. Попробуйте позже.")
    except Exception as e:
        logger.error(f"Error in initiate_yookassa_payment for user {user_tg.id}: {e}", exc_info=True)
        await update.message.reply_text("Ошибка при создании платежа.")

async def handle_buy_1_month(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await initiate_yookassa_payment(update, context, 1, 30)
async def handle_buy_6_months(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await initiate_yookassa_payment(update, context, 6, 180)
async def handle_buy_1_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: await initiate_yookassa_payment(update, context, 12, 365)

async def process_yookassa_notification(notification_data: dict, bot_instance: TelegramBotInstance | None):
    logger.info(f"Webhook: Received Yookassa notification: {notification_data}")
    event = notification_data.get("event")
    payment_object = notification_data.get("object")

    if not (event and payment_object and payment_object.get("id")):
        logger.error("Webhook: Invalid Yookassa notification data.")
        return

    yookassa_payment_id = payment_object.get("id")
    status_from_yookassa = payment_object.get("status")

    if event == "payment.succeeded" and status_from_yookassa == "succeeded":
        logger.info(f"Webhook: Payment {yookassa_payment_id} SUCCEEDED.")
        session = AsyncSessionLocal()
        try:
            stmt_payment = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            db_payment = (await session.execute(stmt_payment)).scalar_one_or_none()

            if not db_payment:
                logger.error(f"Webhook: Payment {yookassa_payment_id} not found in DB.")
                return
            if db_payment.status == "succeeded":
                logger.warning(f"Webhook: Payment {yookassa_payment_id} already processed.")
                return

            additional_data = json.loads(db_payment.additional_data or '{}')
            internal_user_db_id = int(additional_data.get("internal_user_db_id"))
            duration_days = int(additional_data.get("duration_days", 30))
            telegram_user_id = int(additional_data.get("telegram_user_id"))

            db_payment.status = "succeeded"
            db_payment.updated_at = datetime.utcnow()
            
            db_user = await session.get(User, internal_user_db_id)
            if not db_user:
                logger.error(f"Webhook: User DB ID {internal_user_db_id} not found for payment {db_payment.id}.")
                await session.rollback(); return

            if not outline_client:
                logger.error(f"Webhook: Outline_client not init for payment {db_payment.id}.")
                await session.rollback()
                if bot_instance: await bot_instance.send_message(telegram_user_id, "Оплата прошла, но проблема с VPN сервисом. Свяжитесь с поддержкой.")
                return
            
            key_name = f"tg_user_{db_user.id}_paid_{uuid.uuid4().hex[:4]}"
            loop = asyncio.get_event_loop()
            new_key_obj = await loop.run_in_executor(None, outline_client.create_key)

            if not new_key_obj:
                logger.error(f"Webhook: Failed to create Outline key for payment {db_payment.id}.")
                await session.rollback()
                if bot_instance: await bot_instance.send_message(telegram_user_id, "Оплата прошла, но не удалось создать VPN ключ. Свяжитесь с поддержкой.")
                return

            new_db_key = OutlineKey(
                outline_id_on_server=str(new_key_obj.key_id), access_url=new_key_obj.access_url,
                name=getattr(new_key_obj, 'name', key_name), user_id=db_user.id,
                payment_id=db_payment.id, expires_at=datetime.utcnow() + timedelta(days=duration_days)
            )
            session.add(new_db_key)
            session.add(db_payment) # Ensure payment status update is also part of the transaction
            await session.commit()
            logger.info(f"Webhook: Payment {yookassa_payment_id} processed. OutlineKey {new_db_key.id} created for user {telegram_user_id}.")

            if bot_instance:
                expires_str = new_db_key.expires_at.strftime('%Y-%m-%d %H:%M')
                # Экранирование для MarkdownV2 (очень базовое)
                escaped_url = new_key_obj.access_url.replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("]", "\\]").replace("(", "\\(").replace(")", "\\)").replace("~", "\\~").replace("`", "\\`").replace(">", "\\>").replace("#", "\\#").replace("+", "\\+").replace("-", "\\-").replace("=", "\\=").replace("|", "\\|").replace("{", "\\{").replace("}", "\\}").replace(".", "\\.").replace("!", "\\!")
                msg_text = f"✅ Оплата прошла успешно!\n\n🔑 Ваш ключ Outline:\n`{escaped_url}`\n\nДействителен до: {expires_str} UTC"
                await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text, parse_mode='MarkdownV2')
        except Exception as e:
            logger.error(f"Webhook: Error processing payment.succeeded {yookassa_payment_id}: {e}", exc_info=True)
            await session.rollback()
        finally:
            await session.close()

    elif event == "payment.canceled":
        logger.info(f"Webhook: Payment {yookassa_payment_id} CANCELED.")
        session = AsyncSessionLocal()
        try:
            stmt_payment = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            db_payment = (await session.execute(stmt_payment)).scalar_one_or_none()
            if db_payment and db_payment.status != "canceled":
                db_payment.status = "canceled"; db_payment.updated_at = datetime.utcnow()
                session.add(db_payment); await session.commit()
                logger.info(f"Webhook: Payment {yookassa_payment_id} status updated to canceled in DB.")
        except Exception as e:
            logger.error(f"Webhook: Error updating canceled payment {yookassa_payment_id}: {e}", exc_info=True)
            await session.rollback()
        finally:
            await session.close()
    else:
        logger.info(f"Webhook: Received Yookassa event '{event}' for payment {yookassa_payment_id}. Not processed.")

async def check_and_deactivate_expired_keys():
    logger.info("APScheduler: Checking expired keys...")
    session = AsyncSessionLocal()
    try:
        stmt = select(OutlineKey).where(OutlineKey.is_active == True, OutlineKey.expires_at <= datetime.utcnow())
        expired_keys = (await session.execute(stmt)).scalars().all()
        if not expired_keys: logger.info("APScheduler: No expired keys found."); return
        logger.info(f"APScheduler: Found {len(expired_keys)} expired keys.")
        loop = asyncio.get_event_loop()
        for key in expired_keys:
            if not outline_client: logger.error("APScheduler: Outline_client not init."); continue
            try:
                deleted = await loop.run_in_executor(None, outline_client.delete_key, key.outline_id_on_server)
                if deleted: key.is_active = False; session.add(key); logger.info(f"APScheduler: Key {key.outline_id_on_server} deleted and marked inactive.")
                else: logger.warning(f"APScheduler: Key {key.outline_id_on_server} not deleted by API. Marking inactive."); key.is_active = False; session.add(key)
            except Exception as e_del: logger.error(f"APScheduler: Error deleting key {key.outline_id_on_server}: {e_del}")
        await session.commit()
    except Exception as e_main: logger.error(f"APScheduler: Global error: {e_main}", exc_info=True); await session.rollback()
    finally: await session.close(); logger.info("APScheduler: Check finished.")

scheduler = AsyncIOScheduler(timezone="UTC")

async def custom_application_setup(app_param: Application) -> None: # Переименовал параметр для ясности
    global application_instance_for_webhook # Указываем, что мы хотим изменить глобальную переменную
    application_instance_for_webhook = app_param # Сохраняем экземпляр

    logger.info("DB tables check/creation...")
    await create_db_tables()
    scheduler.add_job(check_and_deactivate_expired_keys, 'interval', minutes=2, id="key_deactivation_job")
    try:
        scheduler.start()
        logger.info("APScheduler started. Check every 2 minutes.")
    except Exception as e_scheduler:
        logger.error(f"Error starting APScheduler: {e_scheduler}", exc_info=True)
    logger.info("Application setup complete.")

def main() -> None:
    if not BOT_TOKEN: logger.critical("CRITICAL: BOT_TOKEN not found!"); return
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY): logger.warning("Yookassa credentials not set. Payments will be unavailable.")

    builder = Application.builder().token(BOT_TOKEN)
    builder.post_init(custom_application_setup) # custom_application_setup получит Application как аргумент
    application = builder.build()
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_BUY_1_MONTH}$"), handle_buy_1_month))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_BUY_6_MONTHS}$"), handle_buy_6_months))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_BUY_1_YEAR}$"), handle_buy_1_year))
    
    logger.info("Bot starting...")
    try:
        application.run_polling()
    finally:
        if scheduler.running: scheduler.shutdown(); logger.info("APScheduler stopped.")
    logger.info("Bot stopped.")

if __name__ == "__main__":
    main()

# --- Код для Flask веб-сервера (webhook_listener.py) должен быть в ОТДЕЛЬНОМ файле ---
# Пример того, как Flask мог бы вызвать process_yookassa_notification:
#
# from flask import Flask, request, abort
# import asyncio
# # Нужно будет импортировать process_yookassa_notification и application_instance_for_webhook
# # из my_telegram_bot.py. Это требует аккуратной организации импортов,
# # чтобы избежать циклических зависимостей и обеспечить доступ к 'application_instance_for_webhook'.
# # Возможно, 'application_instance_for_webhook' лучше передавать как параметр при запуске Flask.
#
# flask_app = Flask(__name__)
#
# # Предположим, что у Flask есть доступ к application_instance_for_webhook,
# # что является сложной задачей, если это разные процессы.
# # Лучше: Flask получает данные и передает их в очередь (Redis) или делает API вызов к боту,
# # или бот и вебхук-сервер разделяют какую-то общую логику/сервисы.
#
# @flask_app.route('/yookassa_webhook', methods=['POST'])
# def handle_yookassa_webhook():
#     data = request.get_json()
#     if not data:
#         abort(400)
#     logger.info(f"Flask received webhook: {data}") # Логгирование из Flask
#
#     # Получаем текущий event loop, если Flask работает в асинхронном режиме с asyncio,
#     # или создаем новый для выполнения корутины.
#     # Это упрощение. В реальности интеграция asyncio с Flask требует ASGI-сервера (Hypercorn, Uvicorn).
#     try:
#         # Это ОЧЕНЬ УПРОЩЕННЫЙ вызов. Нужен доступ к application_instance_for_webhook.bot
#         # И process_yookassa_notification должен быть доступен для импорта.
#         if application_instance_for_webhook and application_instance_for_webhook.bot:
#             asyncio.run(process_yookassa_notification(data, application_instance_for_webhook.bot))
#         else:
#             logger.error("Flask: application_instance_for_webhook.bot is not available!")
#     except Exception as e:
#         logger.error(f"Flask: Error calling process_yookassa_notification: {e}")
#
#     return '', 200
#
# # if __name__ == '__main__':
# # flask_app.run(host='0.0.0.0', port=5001) # Порт, который вы открываете через ngrok
