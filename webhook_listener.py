import logging
import os
import json
import asyncio

from flask import Flask, request
from dotenv import load_dotenv
from sqlalchemy.future import select
from sqlalchemy import and_
from datetime import datetime, timedelta
from outline_vpn.outline_vpn import OutlineVPN
import uuid
from telegram import Bot as TelegramBotInstance

# --- 1. ЗАГРУЗКА НАСТРОЕК ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv("API_URL")
CERT_SHA256 = os.getenv("CERT_SHA256")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


# --- 2. ИМПОРТ МОДЕЛЕЙ БАЗЫ ДАННЫХ ---
try:
    from database import User, OutlineKey, Payment, AsyncSessionLocal
    log.info("Модели БД успешно импортированы в webhook_listener.")
except ImportError as e:
    log.error(f"Не удалось импортировать модели БД: {e}")
    User, OutlineKey, Payment, AsyncSessionLocal = None, None, None, None


# --- 3. ИНИЦИАЛИЗАЦИЯ КЛИЕНТА OUTLINE ---
outline_client_webhook = None
if API_URL and CERT_SHA256:
    try:
        outline_client_webhook = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
        log.info("Webhook: Клиент Outline VPN инициализирован.")
    except Exception as e:
        log.error(f"Webhook: Ошибка при инициализации клиента Outline: {e}")
else:
    log.warning("Webhook: API_URL или CERT_SHA256 для Outline не найдены в .env.")


# --- 4. ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ ПЛАТЕЖА ---
async def process_yookassa_notification_standalone(notification_data: dict, outline_client: OutlineVPN | None):
    """
    Обрабатывает входящие веб-уведомления от ЮKassa.
    """
    logger_webhook_process = logging.getLogger('yookassa_process')
    bot_instance = None
    if BOT_TOKEN:
        bot_instance = TelegramBotInstance(token=BOT_TOKEN)

    event = notification_data.get("event")
    payment_object = notification_data.get("object")

    if not (event and payment_object and payment_object.get("id")):
        logger_webhook_process.error("Invalid Yookassa notification data.")
        return

    yookassa_payment_id = payment_object.get("id")

    if event == "payment.succeeded" and payment_object.get("status") == "succeeded":
        logger_webhook_process.info(f"Payment {yookassa_payment_id} SUCCEEDED.")
        
        session = AsyncSessionLocal()
        try:
            stmt = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
            db_payment = (await session.execute(stmt)).scalar_one_or_none()

            if not db_payment or db_payment.status == "succeeded":
                logger_webhook_process.warning(f"Payment {yookassa_payment_id} not found or already processed.")
                return

            additional_data = json.loads(db_payment.additional_data or '{}')
            action = additional_data.get("action", "create") # По умолчанию - создание нового ключа
            duration_days = int(additional_data.get("duration_days", 30))
            telegram_user_id = int(additional_data.get("telegram_user_id"))
            
            db_payment.status = "succeeded"
            db_payment.updated_at = datetime.utcnow()
            
            # --- ЛОГИКА ПРОДЛЕНИЯ КЛЮЧА ---
            if action == "extend":
                key_id = additional_data.get("key_to_extend_id")
                key_to_extend = await session.get(OutlineKey, key_id)
                
                if key_to_extend:
                    # Продлеваем от текущей даты окончания
                    new_expiry_date = key_to_extend.expires_at + timedelta(days=duration_days)
                    key_to_extend.expires_at = new_expiry_date
                    await session.commit()
                    
                    expires_str = new_expiry_date.strftime('%d.%m.%Y %H:%M')
                    msg_text = f"✅ Ваша подписка успешно продлена!\n\nНовая дата окончания: {expires_str} UTC"
                    logger_webhook_process.info(f"Key ID {key_id} extended for user {telegram_user_id}.")
                    if bot_instance:
                        await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text)
                    return # Завершаем обработку

            # --- ЛОГИКА СОЗДАНИЯ НОВОГО КЛЮЧА (ЕСЛИ НЕ ПРОДЛЕНИЕ) ---
            user_db_id = int(additional_data.get("internal_user_db_id"))
            new_key_obj = await asyncio.to_thread(outline_client.create_key)
            
            new_db_key = OutlineKey(
                outline_id_on_server=str(new_key_obj.key_id),
                access_url=new_key_obj.access_url,
                name=f"tg_user_{user_db_id}",
                user_id=user_db_id,
                payment_id=db_payment.id,
                expires_at=datetime.utcnow() + timedelta(days=duration_days)
            )
            session.add(new_db_key)
            await session.commit()
            
            expires_str = new_db_key.expires_at.strftime('%d.%m.%Y %H:%M')
            msg_text = f"✅ Оплата прошла успешно!\n\n🔑 Ваш новый ключ Outline:\n{new_db_key.access_url}\n\nДействителен до: {expires_str} UTC"
            logger_webhook_process.info(f"New key {new_db_key.id} created for user {telegram_user_id}.")
            if bot_instance:
                await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text)

        except Exception as e:
            logger_webhook_process.error(f"Error processing payment {yookassa_payment_id}: {e}", exc_info=True)
            await session.rollback()
        finally:
            await session.close()


# --- 5. FLASK ПРИЛОЖЕНИЕ ---
flask_app = Flask(__name__)

@flask_app.route('/yookassa_webhook', methods=['POST'])
def yookassa_webhook_route():
    json_data = request.get_json()
    log.info(f"Webhook received data: {json_data}")

    try:
        # Запускаем нашу асинхронную логику
        asyncio.run(process_yookassa_notification_standalone(json_data, outline_client_webhook))
    except Exception as e:
        log.error(f"Critical error in webhook processing: {e}", exc_info=True)
        return "Internal Server Error", 500

    return "OK", 200

if __name__ == '__main__':
    flask_app.run(host='0.0.0.0', port=5001)
