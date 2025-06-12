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
API_URL = os.getenv("API_URL") # Outline API URL
CERT_SHA256 = os.getenv("CERT_SHA256") # Outline Cert
AMNEZIA_API_URL_WH = os.getenv("AMNEZIA_API_URL") # Amnezia API URL for webhook listener

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


# --- 2. ИМПОРТ МОДЕЛЕЙ БАЗЫ ДАННЫХ ---
try:
    from database import User, VpnKey, Payment, AsyncSessionLocal # Renamed OutlineKey to VpnKey
    log.info("Модели БД успешно импортированы в webhook_listener.")
except ImportError as e:
    log.error(f"Не удалось импортировать модели БД: {e}")
    User, VpnKey, Payment, AsyncSessionLocal = None, None, None, None # Renamed OutlineKey to VpnKey

# Attempt to import Amnezia client functions from my_telegram_bot
# This might require refactoring in a larger application to avoid circular dependencies
try:
    from my_telegram_bot import create_amnezia_user, get_amnezia_config, PROTOCOL_CALLBACK_OUTLINE, PROTOCOL_CALLBACK_AMNEZIA
    log.info("Amnezia client functions and protocol constants imported successfully into webhook_listener.")
except ImportError as e:
    log.error(f"Could not import Amnezia client functions or protocol constants from my_telegram_bot: {e}")
    create_amnezia_user, get_amnezia_config = None, None
    PROTOCOL_CALLBACK_OUTLINE, PROTOCOL_CALLBACK_AMNEZIA = 'proto_outline', 'proto_amnezia' # Fallback defaults


# --- 3. ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ---
outline_client_webhook = None
if API_URL and CERT_SHA256: # For Outline
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
            action = additional_data.get("action", "create")
            duration_days = int(additional_data.get("duration_days", 30))
            telegram_user_id = int(additional_data.get("telegram_user_id")) # Must be present
            user_db_id = int(additional_data.get("internal_user_db_id")) # Must be present
            chosen_protocol = additional_data.get("chosen_protocol", PROTOCOL_CALLBACK_OUTLINE) # Default to Outline if not present

            db_payment.status = "succeeded"
            db_payment.updated_at = datetime.utcnow()
            
            key_extended_or_created = False

            if action == "extend":
                # Prioritize the specific key_to_extend_db_id from metadata
                key_id_from_metadata = additional_data.get("key_to_extend_db_id")
                if not key_id_from_metadata: # Fallback to old field if new one isn't there
                    key_id_from_metadata = additional_data.get("key_to_extend_id")
                
                if key_id_from_metadata:
                    key_to_extend = await session.get(VpnKey, int(key_id_from_metadata)) # Ensure it's int
                    if key_to_extend and key_to_extend.user_id == user_db_id and key_to_extend.protocol == chosen_protocol: # Security check for user_id and protocol match
                        new_expiry_date = (key_to_extend.expires_at if key_to_extend.expires_at > datetime.utcnow() else datetime.utcnow()) + timedelta(days=duration_days)
                        key_to_extend.expires_at = new_expiry_date
                        key_to_extend.is_active = True # Ensure it's active
                        db_payment.outline_key_association = key_to_extend # Associate payment with this key
                        await session.commit()

                        expires_str = new_expiry_date.strftime('%d.%m.%Y %H:%M')
                        protocol_name_msg = "Outline VPN" if chosen_protocol == PROTOCOL_CALLBACK_OUTLINE else "Amnezia VPN"
                        msg_text = f"✅ Ваша подписка {protocol_name_msg} успешно продлена!\n\nНовая дата окончания: {expires_str} UTC"
                        logger_webhook_process.info(f"Key ID {key_id_to_extend} ({chosen_protocol}) extended for user {telegram_user_id}.")
                        if bot_instance: await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text)
                        key_extended_or_created = True
                    else:
                        logger_webhook_process.warning(f"Extend action: Key ID {key_id_to_extend} not found or protocol mismatch for user {telegram_user_id}.")
                        action = "create" # Fallback to creating a new key if extension target is invalid
                else:
                    logger_webhook_process.warning(f"Extend action: key_to_extend_id missing in metadata for user {telegram_user_id}.")
                    action = "create" # Fallback

            if action == "create" and not key_extended_or_created:
                expires_at = datetime.utcnow() + timedelta(days=duration_days)
                new_db_key = None
                msg_text = ""

                if chosen_protocol == PROTOCOL_CALLBACK_OUTLINE:
                    if not outline_client_webhook:
                        logger_webhook_process.error("Outline client not configured in webhook for creating key.")
                        raise Exception("Outline client not available") # Will be caught below

                    outline_key_obj = await asyncio.to_thread(outline_client_webhook.create_key)
                    new_db_key = VpnKey(
                        key_uuid_on_server=str(outline_key_obj.key_id),
                        access_url=outline_key_obj.access_url,
                        name=f"tg_user_{user_db_id}_paid_outline",
                        protocol=PROTOCOL_CALLBACK_OUTLINE,
                        user_id=user_db_id,
                        payment_id=db_payment.id,
                        expires_at=expires_at,
                        is_trial=False, # Paid key
                        is_active=True
                    )
                    session.add(new_db_key)
                    await session.commit() # Commit to get ID for association
                    db_payment.outline_key_association = new_db_key # Associate payment
                    await session.commit()

                    expires_str = expires_at.strftime('%d.%m.%Y %H:%M')
                    msg_text = f"✅ Оплата прошла успешно!\n\n🔑 Ваш новый ключ Outline VPN:\n`{new_db_key.access_url}`\n\nДействителен до: {expires_str} UTC"
                    logger_webhook_process.info(f"New Outline key {new_db_key.id} created for user {telegram_user_id}.")

                elif chosen_protocol == PROTOCOL_CALLBACK_AMNEZIA:
                    if not (create_amnezia_user and get_amnezia_config and AMNEZIA_API_URL_WH): # Check if functions were imported and URL is set
                        logger_webhook_process.error("Amnezia client functions or AMNEZIA_API_URL not available in webhook.")
                        raise Exception("Amnezia service not available")

                    # Check if user exists in Amnezia, create if not.
                    # The create_amnezia_user function in the bot currently doesn't distinguish between create/get,
                    # assuming it's idempotent or API handles "already exists" gracefully.
                    amnezia_user_data = await create_amnezia_user(telegram_user_id) # Uses AMNEZIA_API_URL from bot's env
                    if not amnezia_user_data:
                        raise Exception(f"Failed to create/verify Amnezia user {telegram_user_id}")

                    config_text = await get_amnezia_config(telegram_user_id)
                    if not config_text:
                        raise Exception(f"Failed to get Amnezia config for user {telegram_user_id}")

                    # Check for existing Amnezia key to update, or create new
                    existing_amnezia_key = (await session.execute(
                        select(VpnKey).where(
                            VpnKey.user_id == user_db_id,
                            VpnKey.protocol == PROTOCOL_CALLBACK_AMNEZIA,
                            VpnKey.is_active == True # Consider if we should update an inactive key
                        )
                    )).scalars().first()

                    if existing_amnezia_key:
                        new_db_key = existing_amnezia_key
                        new_db_key.expires_at = (new_db_key.expires_at if new_db_key.expires_at > datetime.utcnow() else datetime.utcnow()) + timedelta(days=duration_days)
                        new_db_key.payment_id = db_payment.id # Update payment association
                        new_db_key.is_active = True
                        logger_webhook_process.info(f"Updating existing Amnezia key {new_db_key.id} for user {telegram_user_id}.")
                    else:
                        new_db_key = VpnKey(
                            key_uuid_on_server=str(telegram_user_id), # Using telegram_id as unique server ID
                            access_url=config_text,
                            name=f"tg_user_{user_db_id}_paid_amnezia",
                            protocol=PROTOCOL_CALLBACK_AMNEZIA,
                            user_id=user_db_id,
                            payment_id=db_payment.id,
                            expires_at=expires_at,
                            is_trial=False, # Paid key
                            is_active=True
                        )
                        session.add(new_db_key)

                    await session.commit() # Commit to get ID for association if new
                    db_payment.outline_key_association = new_db_key # Associate payment
                    await session.commit()

                    expires_str = expires_at.strftime('%d.%m.%Y %H:%M')
                    # Sending config as a file for Amnezia
                    from io import BytesIO
                    config_filename = f"amnezia_vpn_config_tg_{telegram_user_id}.conf"
                    config_bytes = config_text.encode('utf-8')
                    config_file_like = BytesIO(config_bytes)
                    config_file_like.name = config_filename
                    
                    if bot_instance:
                        await bot_instance.send_document(
                            chat_id=telegram_user_id,
                            document=config_file_like,
                            filename=config_filename,
                            caption=(
                                f"✅ Оплата прошла успешно!\n\n"
                                f"🔑 Ваш новый/обновленный ключ Amnezia VPN (WireGuard) готов!\n"
                                f"Конфигурационный файл прикреплен выше.\n\n"
                                f"Он будет действителен до: *{expires_str} UTC*."
                            ),
                            parse_mode='Markdown'
                        )
                    logger_webhook_process.info(f"New/Updated Amnezia key {new_db_key.id} for user {telegram_user_id} sent via bot.")
                    key_extended_or_created = True # Mark as done

                else:
                    logger_webhook_process.error(f"Unknown protocol {chosen_protocol} for payment {yookassa_payment_id}.")
                    # Do not send message to user as this is an internal error.

                if bot_instance and msg_text: # For Outline, message is set directly
                     await bot_instance.send_message(chat_id=telegram_user_id, text=msg_text, parse_mode='Markdown')
            
            if not key_extended_or_created and action == "create":
                 logger_webhook_process.error(f"Key creation action specified, but no key was created for payment {yookassa_payment_id} / user {telegram_user_id}.")


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
