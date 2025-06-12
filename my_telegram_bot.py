import logging
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from outline_vpn.outline_vpn import OutlineVPN
from dotenv import load_dotenv
from sqlalchemy.future import select
from sqlalchemy import and_
from datetime import datetime, timedelta
import asyncio
import uuid
from decimal import Decimal
import json
import httpx

# --- Импорты ---
from database import User, VpnKey, Payment, create_db_tables, get_async_session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from yookassa import Configuration as YooKassaConfiguration
from yookassa import Payment as YooKassaPaymentObject
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem

# --- Загрузка настроек ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_URL = os.getenv("API_URL")
CERT_SHA256 = os.getenv("CERT_SHA256")
AMNEZIA_API_URL = os.getenv("AMNEZIA_API_URL")
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
BASE_PRICE_PER_MONTH = Decimal(os.getenv("BASE_PRICE_PER_MONTH", "160.00"))
# Длительность бесплатного пробного периода в днях
FREE_TRIAL_DAYS = 30

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Инициализация клиентов ---
outline_client = None
if API_URL and CERT_SHA256:
    try:
        outline_client = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
        logger.info("Успешное подключение к Outline VPN API.")
    except Exception as e:
        logger.error(f"Ошибка Outline API: {e}")

if AMNEZIA_API_URL:
    logger.info("Amnezia API URL загружен.")
else:
    logger.info("Amnezia API URL не найден в .env.")

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    YooKassaConfiguration.account_id = YOOKASSA_SHOP_ID
    YooKassaConfiguration.secret_key = YOOKASSA_SECRET_KEY
    logger.info("Конфигурация ЮKassa установлена.")

# --- Определение кнопок меню и клавиатур ---
BUTTON_GET_KEY = "🔑 Получить/Продлить доступ"
BUTTON_MY_KEYS = "ℹ️ Моя подписка"

PROTOCOL_CALLBACK_OUTLINE = "proto_outline"
PROTOCOL_CALLBACK_AMNEZIA = "proto_amnezia"

protocol_keyboard = InlineKeyboardMarkup([
    [InlineKeyboardButton("Outline VPN", callback_data=PROTOCOL_CALLBACK_OUTLINE)],
    [InlineKeyboardButton("Amnezia VPN (WireGuard)", callback_data=PROTOCOL_CALLBACK_AMNEZIA)],
])

# --- AMNEZIA API CLIENT FUNCTIONS ---
async def create_amnezia_user(telegram_id: int) -> dict | None:
    """
    Creates a new user in Amnezia API.
    """
    amnezia_api_url = os.getenv("AMNEZIA_API_URL")
    if not amnezia_api_url:
        logger.error("AMNEZIA_API_URL not set in environment variables.")
        return None

    url = f"{amnezia_api_url}/api/users/"
    data = {"telegram_id": telegram_id}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=data)

        if response.status_code == 200 or response.status_code == 201: # Typically 201 for created
            logger.info(f"Successfully created Amnezia user for telegram_id {telegram_id}. Response: {response.json()}")
            return response.json()
        else:
            logger.error(f"Failed to create Amnezia user for telegram_id {telegram_id}. Status: {response.status_code}, Response: {response.text}")
            return None
    except httpx.RequestError as e:
        logger.error(f"RequestError while creating Amnezia user for telegram_id {telegram_id}: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"JSONDecodeError while parsing Amnezia user creation response for telegram_id {telegram_id}: {e}")
        return None

async def get_amnezia_config(telegram_id: int) -> str | None:
    """
    Retrieves VPN configuration for a user from Amnezia API.
    """
    amnezia_api_url = os.getenv("AMNEZIA_API_URL")
    if not amnezia_api_url:
        logger.error("AMNEZIA_API_URL not set in environment variables.")
        return None

    url = f"{amnezia_api_url}/api/users/{telegram_id}/config/"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url)

        if response.status_code == 200:
            logger.info(f"Successfully retrieved Amnezia config for telegram_id {telegram_id}.")
            return response.text
        else:
            logger.error(f"Failed to retrieve Amnezia config for telegram_id {telegram_id}. Status: {response.status_code}, Response: {response.text}")
            return None
    except httpx.RequestError as e:
        logger.error(f"RequestError while retrieving Amnezia config for telegram_id {telegram_id}: {e}")
        return None


main_menu_keyboard = [
    [KeyboardButton(BUTTON_GET_KEY)],
    [KeyboardButton(BUTTON_MY_KEYS)],
]
REPLY_MARKUP_MAIN_MENU = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)

# --- ОБРАБОТЧИКИ КОМАНД ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_tg = update.effective_user
    logger.info(f"User {user_tg.first_name} ({user_tg.id}) started.")
    async for session in get_async_session():
        stmt = select(User).where(User.telegram_id == user_tg.id)
        db_user = (await session.execute(stmt)).scalar_one_or_none()
        if not db_user:
            db_user = User(telegram_id=user_tg.id, username=user_tg.username, first_name=user_tg.first_name)
            session.add(db_user)
            await session.commit()
    
    await update.message.reply_html(f"Привет, {user_tg.mention_html()}! 👋\n\nЯ помогу вам получить доступ к быстрому и безопасному VPN.", reply_markup=REPLY_MARKUP_MAIN_MENU)

async def get_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Sends a message asking the user to choose a VPN protocol.
    """
    await update.message.reply_text("Пожалуйста, выберите тип VPN подключения:", reply_markup=protocol_keyboard)

async def handle_protocol_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the user's protocol selection from the InlineKeyboard.
    """
    query = update.callback_query
    await query.answer()
    chosen_protocol = query.data
    context.user_data['chosen_protocol'] = chosen_protocol

    protocol_name = "Outline VPN" if chosen_protocol == PROTOCOL_CALLBACK_OUTLINE else "Amnezia VPN (WireGuard)"
    await query.edit_message_text(text=f"Вы выбрали: {protocol_name}.\nОбрабатываю запрос...")
    
    await initiate_key_or_payment_flow(update, context)


async def initiate_key_or_payment_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Decides whether to issue a free trial key or initiate payment based on protocol and user history.
    """
    chosen_protocol = context.user_data.get('chosen_protocol')
    if not chosen_protocol:
        logger.error("chosen_protocol not found in user_data during initiate_key_or_payment_flow")
        # Try to determine chat_id to send an error message
        chat_id_error = None
        if update.callback_query and update.callback_query.message:
            chat_id_error = update.callback_query.message.chat_id
        elif update.message: # This case should ideally not happen if flow starts from callback
            chat_id_error = update.message.chat_id

        if chat_id_error:
            await context.bot.send_message(chat_id_error, "Произошла ошибка: протокол не выбран. Пожалуйста, попробуйте снова.")
        return

    user_tg = update.effective_user
    # Determine chat_id from callback query or message
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    elif update.message: # Should be callback_query for this flow
        chat_id = update.message.chat_id
        logger.warning("initiate_key_or_payment_flow called from a message update, expected callback_query.")
        # Potentially guide user back to protocol selection if this is an unexpected entry point
    else:
        logger.error("Could not determine chat_id in initiate_key_or_payment_flow")
        return


    async for session in get_async_session():
        db_user = (await session.execute(select(User).where(User.telegram_id == user_tg.id))).scalar_one()
        
        # Check for existing trial key
        stmt_trial_key = select(VpnKey).where(
            VpnKey.user_id == db_user.id,
            VpnKey.is_trial == True,
            # VpnKey.protocol == chosen_protocol # Optional: allow one trial per protocol type? For now, one trial overall.
        )
        trial_key_exists = (await session.execute(stmt_trial_key)).scalars().first()

        if trial_key_exists:
            logger.info(f"User {user_tg.id} already had a trial key. Proceeding to payment for protocol {chosen_protocol}.")
            await context.bot.send_message(chat_id, "У вас уже был пробный период. Для получения доступа необходимо оплатить.")
            await initiate_yookassa_payment(update, context, months=1, duration_days=30, chosen_protocol=chosen_protocol)
        else:
            logger.info(f"User {user_tg.id} is eligible for a free trial for protocol {chosen_protocol}.")
            await context.bot.send_message(chat_id, "🎉 Поздравляем! Как новому пользователю, мы дарим вам бесплатный пробный доступ.")

            if chosen_protocol == PROTOCOL_CALLBACK_OUTLINE:
                if not outline_client:
                    await context.bot.send_message(chat_id, "Не удалось связаться с Outline VPN-сервером для выдачи пробного ключа. Попробуйте позже.")
                    return
                try:
                    new_key_obj = await asyncio.to_thread(outline_client.create_key)
                    expires_at = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)

                    new_db_key = VpnKey(
                        key_uuid_on_server=str(new_key_obj.key_id),
                        access_url=new_key_obj.access_url,
                        name=f"tg_user_{db_user.id}_trial_outline",
                        protocol=PROTOCOL_CALLBACK_OUTLINE,
                        user_id=db_user.id,
                        expires_at=expires_at,
                        is_active=True,
                        is_trial=True # Explicitly set trial flag
                    )
                    session.add(new_db_key)
                    await session.commit()

                    expires_str = expires_at.strftime('%d.%m.%Y в %H:%M')
                    msg_text = (
                        f"✅ Ваш бесплатный ключ Outline VPN готов!\n\n"
                        f"🔑 Ключ доступа:\n`{new_key_obj.access_url}`\n\n"
                        f"Он будет действителен до: *{expires_str} UTC*."
                    )
                    await context.bot.send_message(chat_id, msg_text, parse_mode='Markdown')
                except Exception as e:
                    logger.error(f"Error creating a free Outline key for user {user_tg.id}: {e}")
                    await context.bot.send_message(chat_id, "Произошла ошибка при создании бесплатного Outline ключа. Свяжитесь с поддержкой.")

            elif chosen_protocol == PROTOCOL_CALLBACK_AMNEZIA:
                if not AMNEZIA_API_URL:
                    logger.error("AMNEZIA_API_URL not configured for trial key.")
                    await context.bot.send_message(chat_id, "Сервис Amnezia VPN временно недоступен для выдачи пробного ключа.")
                    return

                created_amnezia_data = await create_amnezia_user(user_tg.id)
                if created_amnezia_data:
                    config_text = await get_amnezia_config(user_tg.id)
                    if config_text:
                        expires_at = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)
                        new_db_key = VpnKey(
                            key_uuid_on_server=str(user_tg.id), # Using telegram_id as unique server ID for Amnezia user
                            access_url=config_text, # Storing config itself as access_url
                            name=f"tg_user_{db_user.id}_trial_amnezia",
                            protocol=PROTOCOL_CALLBACK_AMNEZIA,
                            user_id=db_user.id,
                            expires_at=expires_at,
                            is_active=True,
                            is_trial=True # Explicitly set trial flag
                        )
                        session.add(new_db_key)
                        await session.commit()

                        expires_str = expires_at.strftime('%d.%m.%Y в %H:%M')
                        # Sending config as a file for Amnezia, as it's usually larger
                        config_filename = f"amnezia_vpn_config_tg_{user_tg.id}.conf" # Or .txt, .ovpn etc. depending on format

                        # Create an in-memory file-like object
                        from io import BytesIO
                        config_bytes = config_text.encode('utf-8')
                        config_file_like = BytesIO(config_bytes)
                        config_file_like.name = config_filename

                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=config_file_like,
                            filename=config_filename,
                            caption=(
                                f"✅ Ваш бесплатный ключ Amnezia VPN (WireGuard) готов!\n\n"
                                f"Конфигурационный файл прикреплен выше.\n\n"
                                f"Он будет действителен до: *{expires_str} UTC*."
                            ),
                            parse_mode='Markdown'
                        )
                    else:
                        logger.error(f"Failed to get Amnezia config for trial key for user {user_tg.id}.")
                        await context.bot.send_message(chat_id, "Не удалось получить конфигурацию для Amnezia VPN после создания пользователя. Свяжитесь с поддержкой.")
                else:
                    logger.error(f"Failed to create Amnezia user for trial key for user {user_tg.id}.")
                    await context.bot.send_message(chat_id, "Не удалось создать пользователя для Amnezia VPN. Свяжитесь с поддержкой.")
            else:
                logger.warning(f"Unknown protocol '{chosen_protocol}' selected by user {user_tg.id}.")
                await context.bot.send_message(chat_id, f"Выбран неизвестный протокол. Пожалуйста, попробуйте снова.")


async def my_keys_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает активные ключи пользователя и кнопки для продления.
    """
    user_tg = update.effective_user
    now = datetime.utcnow()
    keys_found = False
    async for session in get_async_session():
        stmt = select(VpnKey).join(User).where(
            User.telegram_id == user_tg.id,
            VpnKey.is_active == True,
            VpnKey.expires_at > now
        ).order_by(VpnKey.expires_at) # Order by expiration

        active_keys = (await session.execute(stmt)).scalars().all()

        if not active_keys:
            await update.message.reply_text("У вас нет активных ключей. Нажмите 'Получить/Продлить доступ', чтобы получить свой первый ключ или оплатить новый.")
            return

        keys_found = True
        for key in active_keys:
            protocol_type_str = "Неизвестный протокол"
            access_info = ""
            if key.protocol == PROTOCOL_CALLBACK_OUTLINE:
                protocol_type_str = "Outline VPN"
                access_info = f"Ключ доступа:\n`{key.access_url}`\n"
            elif key.protocol == PROTOCOL_CALLBACK_AMNEZIA:
                protocol_type_str = "Amnezia VPN (WireGuard)"
                # For Amnezia, access_url stores the config.
                # Consider if sending the full config inline is good UX or if it should be a file.
                # For now, as per plan, inline, but truncated if too long.
                config_preview = key.access_url
                if len(config_preview) > 200: # Truncate long configs for display
                    config_preview = config_preview[:200] + "..."
                access_info = f"Конфигурация Amnezia:\n`{config_preview}`\n(Полная конфигурация была отправлена при создании ключа)\n"

            expires_str = key.expires_at.strftime('%d.%m.%Y в %H:%M')

            response_text_part = (
                f"Тип VPN: *{protocol_type_str}*\n"
                f"Действителен до: *{expires_str} UTC*\n\n"
                f"{access_info}"
            )

            keyboard = [[InlineKeyboardButton("Продлить на 1 месяц", callback_data=f"extend_key_{key.id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(response_text_part, parse_mode='Markdown', reply_markup=reply_markup)

    if not keys_found: # Should be covered by the check inside session loop, but as a fallback
        await update.message.reply_text("У вас нет активных ключей. Нажмите 'Получить/Продлить доступ', чтобы получить свой первый ключ или оплатить новый.")


async def extend_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие на inline-кнопку "Продлить подписку" для конкретного ключа.
    """
    query = update.callback_query
    await query.answer()

    key_id_to_extend_str = context.matches[0].group(1) if context.matches and context.matches[0].groups() else None
    if not key_id_to_extend_str:
        await query.message.reply_text("Ошибка: не удалось определить ID ключа для продления.")
        logger.error("extend_callback_handler: key_id not found in callback_data.")
        return

    key_id_to_extend = int(key_id_to_extend_str)
    user_tg_id = query.from_user.id

    async for session in get_async_session():
        # Fetch the specific key to extend
        key_to_extend_obj = await session.get(VpnKey, key_id_to_extend)

        if not key_to_extend_obj:
            await query.message.reply_text("Ошибка: ключ для продления не найден.")
            logger.error(f"extend_callback_handler: VpnKey with id {key_id_to_extend} not found.")
            return

        # Verify key belongs to the user (important for security)
        if key_to_extend_obj.user.telegram_id != user_tg_id:
            await query.message.reply_text("Ошибка: этот ключ не принадлежит вам.")
            logger.warning(f"User {user_tg_id} tried to extend key {key_id_to_extend} not belonging to them (owner: {key_to_extend_obj.user.telegram_id}).")
            return

        protocol_for_payment = key_to_extend_obj.protocol
        await initiate_yookassa_payment(update, context, months=1, duration_days=30, chosen_protocol=protocol_for_payment, key_id_to_extend=key_id_to_extend)


async def initiate_yookassa_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, months: int, duration_days: int, chosen_protocol: str, key_id_to_extend: int | None = None):
    """
    Создает платеж в ЮKassa.
    """
    user_tg = update.effective_user # This is the user who interacted with the bot

    # Determine chat_id from callback query (most common for this flow) or message
    chat_id = None
    if update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    elif update.message: # Less common for payment initiation via inline button, but possible if called directly
        chat_id = update.message.chat_id
    
    if not chat_id and user_tg: # Fallback if chat_id couldn't be determined but we have user
         chat_id = user_tg.id # This is user_id, bot might not be able to send message if no prior chat.
         logger.warning(f"initiate_yookassa_payment: chat_id determined from user_tg.id ({user_tg.id}), this might be user_id.")

    if not chat_id:
        logger.error("initiate_yookassa_payment: Critical error, could not determine chat_id to send messages.")
        # Cannot send message to user, so just return.
        return

    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        await context.bot.send_message(chat_id, "Сервис оплаты временно недоступен.")
        return

    payment_amount = BASE_PRICE_PER_MONTH * months
    
    async for session in get_async_session():
        db_user = (await session.execute(select(User).where(User.telegram_id == user_tg.id))).scalar_one()
        
        receipt = Receipt()
        receipt.customer = {"email": f"user_{user_tg.id}@telegram.bot"}
        receipt.items = [
            ReceiptItem({
                "description": f"Подписка Outline VPN на {months} мес.",
                "quantity": 1.0,
                "amount": {"value": str(payment_amount), "currency": "RUB"},
                "vat_code": 1
            })
        ]

        # Metadata setup
        yookassa_metadata = {
            "internal_user_db_id": str(db_user.id),
            "telegram_user_id": str(user_tg.id), # user_tg.id is reliable here
            "duration_days": str(duration_days),
            "chosen_protocol": chosen_protocol
        }
        
        description_protocol_part = "Outline VPN" if chosen_protocol == PROTOCOL_CALLBACK_OUTLINE else "Amnezia VPN"

        # Determine action: "create" or "extend"
        # The key_id_to_extend is now the definitive source for "extend" action
        if key_id_to_extend:
            yookassa_metadata["action"] = "extend"
            yookassa_metadata["key_to_extend_db_id"] = key_id_to_extend # Use the specific DB ID of the key
            description = f"Продление подписки {description_protocol_part} на {months} мес. (ID ключа: {key_id_to_extend})"
        else:
            # Fallback: check for any active key of the chosen protocol if no specific key_id is given
            # This part might be redundant if key_id_to_extend is always passed for extensions from my_keys_handler
            active_key_for_protocol = (await session.execute(
                select(VpnKey).where(
                    VpnKey.user_id == db_user.id,
                    VpnKey.is_active == True,
                    VpnKey.protocol == chosen_protocol,
                    VpnKey.expires_at > datetime.utcnow()
                )
            )).scalars().first()

            if active_key_for_protocol:
                 yookassa_metadata["action"] = "extend"
                 # If we found an active_key here, it implies we are extending it.
                 # For clarity, webhook should prioritize key_to_extend_db_id if present.
                 yookassa_metadata["key_to_extend_id"] = active_key_for_protocol.id # Old field, might be useful for compatibility or logging
                 yookassa_metadata["key_to_extend_db_id"] = active_key_for_protocol.id # Explicitly set new field
                 description = f"Продление подписки {description_protocol_part} на {months} мес. (Автоматически выбран ключ ID: {active_key_for_protocol.id})"
            else:
                yookassa_metadata["action"] = "create"
                description = f"Новая подписка {description_protocol_part} на {months} мес."

        # Update receipt description to be more generic or based on final action
        receipt_items = [
            ReceiptItem({
                "description": description, # Use the detailed description
                "quantity": 1.0,
                "amount": {"value": str(payment_amount), "currency": "RUB"},
                "vat_code": 1 # Assuming VAT code 1 (No VAT) or adjust as needed
            })
        ]
        receipt.items = receipt_items

        builder = PaymentRequestBuilder()
        builder.set_amount({"value": str(payment_amount), "currency": "RUB"}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": f"https://t.me/{context.bot.username}"}) \
            .set_description(description) \
            .set_metadata(yookassa_metadata) \
            .set_receipt(receipt)
        
        payment_request = builder.build()
        yookassa_payment_obj = await asyncio.to_thread(YooKassaPaymentObject.create, payment_request, str(uuid.uuid4()))

        if yookassa_payment_obj and yookassa_payment_obj.confirmation:
            new_db_payment = Payment(
                yookassa_payment_id=yookassa_payment_obj.id, 
                user_id=db_user.id, 
                amount=payment_amount, 
                currency="RUB",
                status=yookassa_payment_obj.status, 
                description=description, 
                additional_data=json.dumps(yookassa_metadata)
            )
            session.add(new_db_payment)
            await session.commit()
            await context.bot.send_message(chat_id, f"Для оплаты перейдите по ссылке:\n{yookassa_payment_obj.confirmation.confirmation_url}")
        else:
            await context.bot.send_message(chat_id, "Не удалось создать ссылку на оплату.")

# --- ПЛАНИРОВЩИК ЗАДАЧ ---
async def check_and_deactivate_expired_keys():
    logger.info("APScheduler: Checking expired keys...")
    async for session in get_async_session():
        keys_modified = False
        try:
            stmt = select(VpnKey).where(
                VpnKey.is_active == True,
                VpnKey.expires_at <= datetime.utcnow()
            )
            expired_keys = (await session.execute(stmt)).scalars().all()

            if not expired_keys:
                return

            for key in expired_keys:
                keys_modified = True # Mark that we are changing at least one key
                logger.info(f"Processing expired key ID {key.id} (Protocol: {key.protocol}, UUID: {key.key_uuid_on_server}) for user {key.user_id}.")
                if key.protocol == PROTOCOL_CALLBACK_OUTLINE:
                    if outline_client:
                        try:
                            await asyncio.to_thread(outline_client.delete_key, key.key_uuid_on_server)
                            logger.info(f"Successfully deleted Outline key UUID {key.key_uuid_on_server} from server.")
                        except Exception as e:
                            logger.error(f"Error deleting Outline key UUID {key.key_uuid_on_server} from server: {e}")
                            # Still mark as inactive in DB to prevent retries / keep DB consistent
                    else:
                        logger.warning(f"Outline client not available. Cannot delete expired Outline key UUID {key.key_uuid_on_server} from server.")
                    key.is_active = False # Always mark inactive in DB for Outline

                elif key.protocol == PROTOCOL_CALLBACK_AMNEZIA:
                    key.is_active = False
                    logger.warning(
                        f"Amnezia key ID {key.id} (UUID: {key.key_uuid_on_server}) for user {key.user_id} has expired and was marked inactive in the DB. "
                        f"Actual deactivation/deletion on the Amnezia (wg-easy) server is not implemented in this bot "
                        f"due to lack of a dedicated API endpoint in the AmnesiaVPN/server project. "
                        f"The key might still be functional on the wg-easy server if the user has the config."
                    )
                else:
                    logger.warning(f"Unknown protocol '{key.protocol}' for expired key ID {key.id}. Marking as inactive.")
                    key.is_active = False

            if keys_modified:
                await session.commit()
                logger.info(f"Committed changes for {len(expired_keys)} expired keys.")
            else:
                logger.info("No expired keys needed database changes in this run.")

        except Exception as e:
            logger.error(f"Scheduler error in check_and_deactivate_expired_keys: {e}")
            await session.rollback() # Rollback in case of other errors during session

# --- ЗАПУСК БОТА ---
def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN not found!")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_and_deactivate_expired_keys, 'interval', hours=1)
    
    async def post_init(app: Application):
        await create_db_tables()
        scheduler.start()
        logger.info("APScheduler started.")

    application.post_init = post_init
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_GET_KEY}$"), get_key_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_KEYS}$"), my_keys_handler))
    # Updated pattern for extend_callback_handler
    application.add_handler(CallbackQueryHandler(extend_callback_handler, pattern=r"^extend_key_(\d+)$"))
    application.add_handler(CallbackQueryHandler(handle_protocol_selection, pattern=f"^({PROTOCOL_CALLBACK_OUTLINE}|{PROTOCOL_CALLBACK_AMNEZIA})$"))
    
    logger.info("Bot starting...")
    application.run_polling()
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")

if __name__ == "__main__":
    main()
