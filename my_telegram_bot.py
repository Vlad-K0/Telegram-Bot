import logging
import os
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
# REMOVE: from outline_vpn.outline_vpn import OutlineVPN
from dotenv import load_dotenv
from sqlalchemy.future import select
from sqlalchemy import and_ # and_ может еще понадобиться
from datetime import datetime, timedelta
import asyncio
import uuid
from decimal import Decimal
import json
# REMOVE: import httpx # marzpy использует aiohttp

# --- Импорты ---
from database import User as DbUser, VpnKey, Payment, create_db_tables, get_async_session # Renamed User to DbUser to avoid conflict
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from yookassa import Configuration as YooKassaConfiguration
from yookassa import Payment as YooKassaPaymentObject
from yookassa.domain.request.payment_request_builder import PaymentRequestBuilder
from yookassa.domain.models.receipt import Receipt, ReceiptItem

# +++ Marzban Imports +++
from marzpy import Marzban
from marzpy.api.user import User as MarzbanUser # Alias для класса пользователя Marzban

# --- Загрузка настроек ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# REMOVE: API_URL = os.getenv("API_URL")
# REMOVE: CERT_SHA256 = os.getenv("CERT_SHA256")
# REMOVE: AMNEZIA_API_URL = os.getenv("AMNEZIA_API_URL")

# +++ Marzban Settings +++
MARZBAN_PANEL_URL = os.getenv("MARZBAN_PANEL_URL")
MARZBAN_USERNAME = os.getenv("MARZBAN_USERNAME")
MARZBAN_PASSWORD = os.getenv("MARZBAN_PASSWORD")
MARZBAN_DEFAULT_DATA_LIMIT_GB_TRIAL = int(os.getenv("MARZBAN_DEFAULT_DATA_LIMIT_GB_TRIAL", "5")) # ГБ для триала
MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID = int(os.getenv("MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID", "50")) # ГБ для платной подписки на месяц


YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
BASE_PRICE_PER_MONTH = Decimal(os.getenv("BASE_PRICE_PER_MONTH", "160.00"))
FREE_TRIAL_DAYS = int(os.getenv("FREE_TRIAL_DAYS", "30")) # Оставляем, но теперь это для Marzban

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Инициализация клиентов ---
# REMOVE: outline_client = None
# REMOVE: if API_URL and CERT_SHA256:
# REMOVE:     try:
# REMOVE:         outline_client = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
# REMOVE:         logger.info("Успешное подключение к Outline VPN API.")
# REMOVE:     except Exception as e:
# REMOVE:         logger.error(f"Ошибка Outline API: {e}")

# REMOVE: if AMNEZIA_API_URL:
# REMOVE:     logger.info("Amnezia API URL загружен.")
# REMOVE: else:
# REMOVE:     logger.info("Amnezia API URL не найден в .env.")

# +++ Marzban Client +++
marzban_client: Marzban | None = None
marzban_api_token: str | None = None # Токен будем получать и обновлять при необходимости

async def initialize_marzban_client():
    global marzban_client
    if MARZBAN_PANEL_URL and MARZBAN_USERNAME and MARZBAN_PASSWORD:
        try:
            marzban_client = Marzban(username=MARZBAN_USERNAME, password=MARZBAN_PASSWORD, base_url=MARZBAN_PANEL_URL)
            logger.info("Клиент Marzban инициализирован. URL: %s", MARZBAN_PANEL_URL)
            # Первоначальное получение токена может быть здесь или отложено до первого вызова API
        except Exception as e:
            logger.error(f"Ошибка инициализации клиента Marzban: {e}")
            marzban_client = None
    else:
        logger.error("Не заданы MARZBAN_PANEL_URL, MARZBAN_USERNAME или MARZBAN_PASSWORD в .env. Клиент Marzban не будет работать.")

# Функция для получения/обновления токена Marzban
async def get_marzban_api_token(force_refresh: bool = False) -> str | None:
    global marzban_api_token
    if not marzban_client:
        logger.error("Клиент Marzban не инициализирован.")
        return None

    if marzban_api_token and not force_refresh:
        # TODO: Добавить проверку времени жизни токена, если это возможно с marzpy
        # Пока что, если токен есть и не просят обновить принудительно, возвращаем его.
        return marzban_api_token

    try:
        token = await marzban_client.get_token()
        if token:
            marzban_api_token = token
            logger.info("Токен Marzban успешно получен/обновлен.")
            return marzban_api_token
        else:
            logger.error("Не удалось получить токен Marzban (ответ None).")
            marzban_api_token = None
            return None
    except Exception as e:
        logger.error(f"Ошибка при получении токена Marzban: {e}", exc_info=True)
        marzban_api_token = None
        return None


if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    YooKassaConfiguration.account_id = YOOKASSA_SHOP_ID
    YooKassaConfiguration.secret_key = YOOKASSA_SECRET_KEY
    logger.info("Конфигурация ЮKassa установлена.")

# --- Определение кнопок меню и клавиатур ---
BUTTON_GET_KEY = "🔑 Получить/Продлить доступ" # Текст можно поменять на "🔑 Моя подписка / Доступ"
BUTTON_MY_KEYS = "ℹ️ Моя подписка" # Оставляем

# REMOVE: PROTOCOL_CALLBACK_OUTLINE = "proto_outline"
# REMOVE: PROTOCOL_CALLBACK_AMNEZIA = "proto_amnezia"

# REMOVE: protocol_keyboard = InlineKeyboardMarkup([
# REMOVE:     [InlineKeyboardButton("Outline VPN", callback_data=PROTOCOL_CALLBACK_OUTLINE)],
# REMOVE:     [InlineKeyboardButton("Amnezia VPN (WireGuard)", callback_data=PROTOCOL_CALLBACK_AMNEZIA)],
# REMOVE: ])

# REMOVE AMNEZIA API CLIENT FUNCTIONS (create_amnezia_user, get_amnezia_config)

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
        # Используем DbUser для обращения к нашей модели User
        stmt = select(User).where(User.telegram_id == user_tg.id)
        db_user_obj = (await session.execute(stmt)).scalar_one_or_none() # переименовал переменную во избежание путаницы
        if not db_user_obj:
            db_user_obj = DbUser(telegram_id=user_tg.id, username=user_tg.username, first_name=user_tg.first_name)
            session.add(db_user_obj)
            await session.commit()
            await session.refresh(db_user_obj) # Обновляем для получения default значений, если есть
    
    await update.message.reply_html(f"Привет, {user_tg.mention_html()}! 👋\n\nЯ помогу вам получить доступ к быстрому и безопасному VPN.", reply_markup=REPLY_MARKUP_MAIN_MENU)

async def get_key_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает запрос на получение/продление доступа.
    Сразу переходит к логике выдачи ключа или оплаты.
    """
    # query = update.callback_query # Это было бы если это CallbackQueryHandler
    # await query.answer() # Не нужно для MessageHandler
    # await query.edit_message_text(text="Обрабатываю ваш запрос...") # Не нужно для MessageHandler
    
    # Для MessageHandler:
    await update.message.reply_text("⏳ Обрабатываю ваш запрос на доступ...")
    await initiate_key_or_payment_flow(update, context) # Передаем update и context дальше

# REMOVE: async def handle_protocol_selection(...)

async def initiate_key_or_payment_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Решает, выдать ли бесплатный пробный доступ Marzban или инициировать оплату.
    """
    user_tg = update.effective_user
    chat_id = update.effective_chat.id

    if not marzban_client:
        await context.bot.send_message(chat_id, "VPN сервис временно недоступен. Пожалуйста, попробуйте позже. (Клиент Marzban не инициализирован)")
        return

    async for session in get_async_session():
        db_user_obj = (await session.execute(select(DbUser).where(DbUser.telegram_id == user_tg.id))).scalar_one()
        
        # Проверка на существующий триальный ключ/подписку
        stmt_trial_key = select(VpnKey).where(
            VpnKey.user_id == db_user_obj.id,
            VpnKey.is_trial == True,
        )
        trial_key_exists = (await session.execute(stmt_trial_key)).scalars().first()

        if trial_key_exists:
            logger.info(f"User {user_tg.id} ({db_user_obj.username}) уже использовал пробный период. Переход к оплате.")
            await context.bot.send_message(chat_id, "Вы уже использовали пробный период. Для получения доступа необходимо оплатить.")
            # Передаем None для marzban_username_to_extend, так как это может быть новая подписка или продление существующей платной
            await initiate_yookassa_payment(update, context, months=1, duration_days=30)
        else:
            logger.info(f"User {user_tg.id} ({db_user_obj.username}) получает пробный доступ Marzban.")

            marzban_api_token_val = await get_marzban_api_token()
            if not marzban_api_token_val:
                await context.bot.send_message(chat_id, "Не удалось связаться с VPN сервисом для выдачи пробного доступа. (Ошибка токена Marzban)")
                return

            try:
                # Генерация имени пользователя для Marzban
                # Можно использовать telegram_id или uuid, если marzban_username должен быть уникальным global, а не только для нашего бота
                # Пока используем telegram_id, т.к. он уникален для пользователя бота
                marzban_trial_username = f"trial_tg_{user_tg.id}_{uuid.uuid4().hex[:6]}"

                # Вычисляем дату истечения триала
                trial_expires_dt = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)
                trial_expire_timestamp = int(trial_expires_dt.timestamp())

                # Объем данных для триала (в байтах)
                trial_data_limit_bytes = MARZBAN_DEFAULT_DATA_LIMIT_GB_TRIAL * (1024**3)

                new_marzban_user_config = MarzbanUser(
                    username=marzban_trial_username,
                    proxies={}, # Оставить пустым для использования настроек по умолчанию из Marzban User Template
                    inbounds={}, # Аналогично
                    expire=trial_expire_timestamp,
                    data_limit=trial_data_limit_bytes,
                    data_limit_reset_strategy="no_reset", # или другая стратегия, если нужна
                    status="active"
                    # online_at, on_hold_expire_duration, on_hold_data_limit - можно не указывать для простоты
                )

                # Используем asyncio.to_thread, если marzpy клиент не полностью асинхронный под капотом
                # Судя по README marzpy, он использует aiohttp, так что его методы уже async.
                # Однако, если есть сомнения или сложные вычисления внутри marzpy, to_thread безопаснее.
                # Для простоты, предполагаем, что marzpy.add_user() корректно асинхронен.
                created_marzban_user = await marzban_client.add_user(user=new_marzban_user_config, token=marzban_api_token_val)

                if not created_marzban_user or not created_marzban_user.subscription_url:
                    logger.error(f"Не удалось создать пользователя Marzban или отсутствует subscription_url для {marzban_trial_username}.")
                    await context.bot.send_message(chat_id, "Произошла ошибка при создании пробного доступа в VPN сервисе. Попробуйте позже.")
                    return

                new_db_vpn_key = VpnKey(
                    marzban_username=marzban_trial_username,
                    subscription_url=created_marzban_user.subscription_url,
                    name=f"Пробная подписка Marzban для {db_user_obj.username or user_tg.id}",
                    user_id=db_user_obj.id,
                    expires_at=trial_expires_dt,
                    is_active=True,
                    is_trial=True
                )
                session.add(new_db_vpn_key)
                await session.commit()
                await session.refresh(new_db_vpn_key)

                expires_str = trial_expires_dt.strftime('%d.%m.%Y в %H:%M')
                msg_text = (
                    f"🎉 Поздравляем! Вам предоставлен бесплатный пробный доступ к VPN.\n\n"
                    f"🔗 Ваша ссылка-подписка:\n`{created_marzban_user.subscription_url}`\n\n"
                    f"ℹ️ Используйте эту ссылку в любом совместимом приложении (например, V2Ray, Clash, Shadowrocket и др.).\n"
                    f"🗓️ Доступ действителен до: *{expires_str} UTC*\n"
                    f"📊 Лимит трафика: *{MARZBAN_DEFAULT_DATA_LIMIT_GB_TRIAL} ГБ*"
                )
                await context.bot.send_message(chat_id, msg_text, parse_mode='Markdown')

            except Exception as e:
                # Здесь можно добавить retry логику или более специфичную обработку ошибок Marzban
                # Например, если пользователь с таким marzban_username уже существует (маловероятно с uuid)
                logger.error(f"Ошибка при создании пробного пользователя Marzban для user_tg_id {user_tg.id}: {e}", exc_info=True)
                # Попытка получить токен заново, если ошибка связана с токеном
                if "token" in str(e).lower(): # Очень грубая проверка
                    marzban_api_token_val = await get_marzban_api_token(force_refresh=True)
                    if marzban_api_token_val:
                        await context.bot.send_message(chat_id, "Произошла временная ошибка связи с VPN сервисом. Пожалуйста, попробуйте еще раз.")
                        return

                await context.bot.send_message(chat_id, "Произошла ошибка при создании пробного доступа. Свяжитесь с поддержкой.")

async def my_keys_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
                    if marzban_api_token_val:
                        await context.bot.send_message(chat_id, "Произошла временная ошибка связи с VPN сервисом. Пожалуйста, попробуйте еще раз.")
                        return

                await context.bot.send_message(chat_id, "Произошла ошибка при создании пробного доступа. Свяжитесь с поддержкой.")

async def my_keys_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает активные подписки пользователя Marzban и кнопки для продления.
    """
    user_tg = update.effective_user
    chat_id = update.effective_chat.id
    now = datetime.utcnow()
    subscriptions_found = False

    if not marzban_client:
        await update.message.reply_text("VPN сервис временно недоступен. (Клиент Marzban не инициализирован)")
        return

    async for session in get_async_session():
        db_user_obj = (await session.execute(select(DbUser).where(DbUser.telegram_id == user_tg.id))).scalar_one()

        # Выбираем все активные (is_active=True) подписки пользователя из нашей БД
        # Дополнительно можно фильтровать по VpnKey.expires_at > now, но Marzban API даст точный статус
        stmt = select(VpnKey).where(
            VpnKey.user_id == db_user_obj.id,
            VpnKey.is_active == True
        ).order_by(VpnKey.expires_at.desc()) # Сначала более свежие

        active_subscriptions_db = (await session.execute(stmt)).scalars().all()

        if not active_subscriptions_db:
            await update.message.reply_text("У вас нет активных VPN подписок.\nНажмите '🔑 Получить/Продлить доступ', чтобы оформить.")
            return

        marzban_api_token_val = await get_marzban_api_token()
        if not marzban_api_token_val:
            await update.message.reply_text("Не удалось связаться с VPN сервисом для получения информации о подписках. (Ошибка токена Marzban)")
            return

        for db_sub in active_subscriptions_db:
            try:
                marzban_user_info = await marzban_client.get_user(username=db_sub.marzban_username, token=marzban_api_token_val)

                if not marzban_user_info:
                    logger.warning(f"Пользователь Marzban {db_sub.marzban_username} не найден в панели для sub ID {db_sub.id}. Возможно, был удален вручную.")
                    # Можно пометить такую подписку как неактивную в нашей БД
                    # db_sub.is_active = False
                    # await session.commit()
                    # await update.message.reply_text(f"Подписка (внутр. ID: {db_sub.id}) не найдена на VPN сервере. Обратитесь в поддержку.")
                    # Пока просто пропустим ее отображение или покажем как неактивную
                    await update.message.reply_text(
                        f"⚠️ Подписка с именем `{db_sub.marzban_username}` не найдена на сервере.\n"
                        f"Ссылка: `{db_sub.subscription_url}` (может быть неактивна)\n"
                        f"Пожалуйста, свяжитесь с поддержкой, если считаете это ошибкой.",
                        parse_mode='Markdown'
                    )
                    continue

                subscriptions_found = True

                # Форматирование данных о трафике
                used_traffic_gb = round(marzban_user_info.used_traffic / (1024**3), 2)
                data_limit_gb_str = "Безлимитно"
                if marzban_user_info.data_limit > 0:
                    data_limit_gb_str = f"{round(marzban_user_info.data_limit / (1024**3), 2)} ГБ"

                # Дата истечения
                expires_at_dt = datetime.fromtimestamp(marzban_user_info.expire) if marzban_user_info.expire else None
                expires_str = "Никогда"
                is_expired_on_marzban = False
                if expires_at_dt:
                    expires_str = expires_at_dt.strftime('%d.%m.%Y в %H:%M UTC')
                    if expires_at_dt < now:
                        is_expired_on_marzban = True

                # Статус пользователя в Marzban
                status_translation = {
                    "active": "Активна ✅",
                    "disabled": "Отключена (администратором) 🚫",
                    "expired": "Истекла (по времени) ⏳",
                    "limited": "Истекла (по трафику) 📈"
                }
                marzban_status_str = status_translation.get(marzban_user_info.status, marzban_user_info.status)

                # Обновляем локальный expires_at и is_active, если есть расхождения и подписка на сервере активна
                # Это важно, если expires_at в Marzban был изменен вручную или другим процессом
                if expires_at_dt and db_sub.expires_at != expires_at_dt and marzban_user_info.status == "active":
                    db_sub.expires_at = expires_at_dt
                    logger.info(f"Обновлена дата истечения для локальной подписки ID {db_sub.id} на {expires_at_dt} из Marzban.")

                if marzban_user_info.status != "active" and db_sub.is_active:
                    db_sub.is_active = False # Если в Marzban не активна, то и у нас не активна
                    logger.info(f"Подписка ID {db_sub.id} помечена неактивной, т.к. статус в Marzban: {marzban_user_info.status}")
                elif marzban_user_info.status == "active" and not db_sub.is_active and (not expires_at_dt or expires_at_dt > now) :
                    # Если в Marzban активна, а у нас нет (и не истекла), активируем
                    db_sub.is_active = True
                    logger.info(f"Подписка ID {db_sub.id} помечена активной, т.к. статус в Marzban: {marzban_user_info.status} и не истекла.")

                # Если подписка в Marzban истекла по времени или трафику, но у нас еще активна
                if (is_expired_on_marzban or marzban_user_info.status in ["expired", "limited"]) and db_sub.is_active:
                    db_sub.is_active = False
                    logger.info(f"Подписка ID {db_sub.id} помечена неактивной из-за статуса/истечения в Marzban ({marzban_user_info.status}, истекла: {is_expired_on_marzban}).")

                await session.commit() # Сохраняем изменения в is_active/expires_at для db_sub

                # Не показываем пользователю неактивные подписки, которые уже неактивны и в Marzban,
                # или если они были помечены неактивными только что из-за статуса Marzban.
                if not db_sub.is_active and marzban_user_info.status != "active":
                    # Можно добавить логирование, что такая подписка была, но не отображается
                    logger.info(f"Пропуск отображения неактивной подписки ID {db_sub.id} (статус Marzban: {marzban_user_info.status})")
                    continue


                response_text_part = (
                    f"🔗 **Ссылка-подписка:**\n`{db_sub.subscription_url}`\n\n"
                    f"👤 Имя пользователя (Marzban): `{db_sub.marzban_username}`\n"
                    f"📊 Трафик: Использовано {used_traffic_gb} ГБ из {data_limit_gb_str}\n"
                    f"🗓️ Действительна до: *{expires_str}*\n"
                    f"🚦 Статус на сервере: *{marzban_status_str}*\n"
                    f"{'🔑 (Пробная)' if db_sub.is_trial else '💳 (Платная)'}"
                )

                # Кнопка продления только если подписка не "disabled" администратором
                keyboard_buttons = []
                if marzban_user_info.status != "disabled":
                     keyboard_buttons.append([InlineKeyboardButton("Продлить на 1 месяц", callback_data=f"extend_sub_{db_sub.id}")])

                reply_markup = InlineKeyboardMarkup(keyboard_buttons) if keyboard_buttons else None
                await update.message.reply_text(response_text_part, parse_mode='Markdown', reply_markup=reply_markup)

            except Exception as e:
                logger.error(f"Ошибка при получении информации о подписке Marzban {db_sub.marzban_username} (ID {db_sub.id}): {e}", exc_info=True)
                if "token" in str(e).lower(): # Очень грубая проверка
                    marzban_api_token_val = await get_marzban_api_token(force_refresh=True) # Обновляем токен
                await update.message.reply_text(f"Не удалось загрузить детали для подписки `{db_sub.marzban_username}`. Попробуйте позже.", parse_mode='Markdown')

        if not subscriptions_found and not active_subscriptions_db: # Если изначально не было найдено активных в БД
             pass # Сообщение "нет активных подписок" уже было отправлено выше
        elif not subscriptions_found and active_subscriptions_db: # Если были в БД, но ни одна не прошла проверку Marzban или неактивна
            await update.message.reply_text("Не найдено актуальных активных подписок. Возможно, все ваши подписки истекли или были деактивированы на сервере.")

async def extend_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие на inline-кнопку "Продлить подписку" для конкретной подписки Marzban.
    """
    query = update.callback_query
    await query.answer()

    # Извлекаем ID подписки (VpnKey.id) из callback_data
    # pattern в main() должен быть r"^extend_sub_(\d+)$"
    subscription_db_id_str = context.matches[0].group(1) if context.matches and context.matches[0].groups() else None
    if not subscription_db_id_str:
        await query.message.reply_text("Ошибка: не удалось определить ID подписки для продления.")
        logger.error("extend_callback_handler: subscription_db_id not found in callback_data.")
        return

    subscription_db_id = int(subscription_db_id_str)
    user_tg_id = query.from_user.id

    async for session in get_async_session():
        # Получаем объект подписки из нашей БД
        db_subscription = await session.get(VpnKey, subscription_db_id)

        if not db_subscription:
            await query.message.reply_text("Ошибка: подписка для продления не найдена в базе данных.")
            logger.error(f"extend_callback_handler: VpnKey with id {subscription_db_id} not found.")
            return

        # Проверка, принадлежит ли подписка этому пользователю
        # db_subscription.user уже загружен, т.к. VpnKey.user это relationship
        # Нужно убедиться, что db_subscription.user.telegram_id это то, что мы ожидаем
        # Это можно сделать через join при запросе db_subscription или проверить после.
        # Проще всего, если user_id в VpnKey соответствует DbUser.id, а не telegram_id.
        # Текущая модель: VpnKey.user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
        # User.telegram_id = Column(Integer, unique=True, index=True, nullable=False)
        # Значит, нужно сначала получить DbUser.id по telegram_id

        db_user_obj = (await session.execute(select(DbUser).where(DbUser.telegram_id == user_tg_id))).scalar_one()
        if not db_user_obj or db_subscription.user_id != db_user_obj.id:
            await query.message.reply_text("Ошибка: эта подписка не принадлежит вам.")
            logger.warning(f"User {user_tg_id} tried to extend subscription {subscription_db_id} not belonging to them (owner user_id: {db_subscription.user_id}, this user_id: {db_user_obj.id if db_user_obj else 'None'}).")
            return

        # Проверка статуса подписки в Marzban перед продлением
        marzban_api_token_val = await get_marzban_api_token()
        if not marzban_api_token_val:
            await query.message.reply_text("Не удалось связаться с VPN сервисом. Попробуйте позже. (Ошибка токена Marzban)")
            return

        try:
            marzban_user_info = await marzban_client.get_user(username=db_subscription.marzban_username, token=marzban_api_token_val)
            if not marzban_user_info:
                await query.message.reply_text(f"Не удалось найти вашу подписку ({db_subscription.marzban_username}) на VPN сервере. Обратитесь в поддержку.")
                return
            if marzban_user_info.status == "disabled":
                await query.message.reply_text(f"Ваша подписка ({db_subscription.marzban_username}) отключена администратором и не может быть продлена. Обратитесь в поддержку.")
                return
        except Exception as e:
            logger.error(f"Ошибка при проверке статуса Marzban пользователя {db_subscription.marzban_username} перед продлением: {e}", exc_info=True)
            await query.message.reply_text("Произошла ошибка при проверке статуса вашей подписки. Пожалуйста, попробуйте позже.")
            return

        # Инициируем платеж, передавая marzban_username для продления
        await initiate_yookassa_payment(
            update,
            context,
            months=1,
            duration_days=30, # Стандартная длительность для платной подписки
            marzban_username_to_extend=db_subscription.marzban_username,
            subscription_db_id_to_extend=db_subscription.id # Передаем ID из нашей БД для связи платежа
        )


async def initiate_yookassa_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    months: int,
    duration_days: int,
    marzban_username_to_extend: str | None = None, # Имя пользователя в Marzban для продления
    subscription_db_id_to_extend: int | None = None # ID VpnKey из нашей БД для продления
    ):
    """
    Создает платеж в ЮKassa для новой подписки Marzban или продления существующей.
    """
    user_tg = update.effective_user
    chat_id = None
    if update.callback_query and update.callback_query.message: # Если вызвано из callback_handler (например, "Продлить")
        chat_id = update.callback_query.message.chat_id
    elif update.message: # Если вызвано из message_handler (например, после проверки на триал)
        chat_id = update.message.chat_id
    
    if not chat_id and user_tg:
         chat_id = user_tg.id
         logger.warning(f"initiate_yookassa_payment: chat_id определен из user_tg.id ({user_tg.id}).")

    if not chat_id:
        logger.error("initiate_yookassa_payment: Критическая ошибка, не удалось определить chat_id.")
        return

    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        await context.bot.send_message(chat_id, "Сервис оплаты временно недоступен.")
        return

    payment_amount = BASE_PRICE_PER_MONTH * months
    
    async for session in get_async_session():
        db_user_obj = (await session.execute(select(DbUser).where(DbUser.telegram_id == user_tg.id))).scalar_one()
        
        # Метаданные для YooKassa
        yookassa_metadata = {
            "internal_user_db_id": str(db_user_obj.id), # ID пользователя из нашей таблицы users
            "telegram_user_id": str(user_tg.id),
            "duration_days": str(duration_days),
            # "chosen_protocol" больше не нужен
        }
        
        description_service_part = "VPN подписки (Marzban)"

        if marzban_username_to_extend and subscription_db_id_to_extend:
            yookassa_metadata["action"] = "extend"
            yookassa_metadata["marzban_username"] = marzban_username_to_extend
            yookassa_metadata["subscription_db_id"] = subscription_db_id_to_extend # ID VpnKey из нашей БД
            description = f"Продление {description_service_part} ({marzban_username_to_extend}) на {months} мес."
        else:
            # Это сценарий создания новой платной подписки (например, после того как триал был использован)
            yookassa_metadata["action"] = "create"
            # marzban_username будет сгенерирован в вебхуке после успешной оплаты
            description = f"Новая {description_service_part} на {months} мес."
            # Проверим, нет ли у пользователя уже активной НЕ ТРИАЛЬНОЙ подписки, чтобы случайно не создать вторую платную
            # Это больше для информации, т.к. вебхук должен быть идемпотентным или создавать нового юзера если нужно
            active_paid_sub_stmt = select(VpnKey).where(
                VpnKey.user_id == db_user_obj.id,
                VpnKey.is_trial == False,
                VpnKey.is_active == True,
                VpnKey.expires_at > datetime.utcnow()
            )
            existing_active_paid_sub = (await session.execute(active_paid_sub_stmt)).scalars().first()
            if existing_active_paid_sub:
                logger.warning(f"Пользователь {user_tg.id} пытается создать новую платную подписку, уже имея активную платную {existing_active_paid_sub.marzban_username}.")
                # Можно добавить доп. логику: предложить продлить существующую или подтвердить создание новой.
                # Пока что, позволяем создать новый платеж на новую подписку. Вебхук разберется.
                # Или можно перенаправить на продление существующей, если она одна.
                # description = f"Новая/Продление {description_service_part} на {months} мес." # Более общий текст
                # yookassa_metadata["action"] = "create_or_extend" # Если хотим универсальный обработчик в вебхуке
                pass


        receipt_items = [
            ReceiptItem({
                "description": description,
                "quantity": 1.0,
                "amount": {"value": str(payment_amount), "currency": "RUB"},
                "vat_code": 1
            })
        ]

        receipt = Receipt()
        receipt.customer = {"email": f"user_{user_tg.id}@telegram.bot"} # или другое валидное поле, если email нет
        receipt.items = receipt_items

        builder = PaymentRequestBuilder()
        builder.set_amount({"value": str(payment_amount), "currency": "RUB"}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": f"https://t.me/{context.bot.username}"}) \
            .set_description(description) \
            .set_metadata(yookassa_metadata) \
            .set_receipt(receipt)
        
        # Генерируем idempotency_key для предотвращения дублирования платежей при сбоях
        idempotency_key_payload = f"{db_user_obj.id}_{yookassa_metadata['action']}_{marzban_username_to_extend or 'new'}_{months}_{duration_days}"
        idempotency_key = str(uuid.uuid5(uuid.NAMESPACE_DNS, idempotency_key_payload)) # Пример генерации

        try:
            payment_request = builder.build()
            # YooKassaPaymentObject.create - блокирующий вызов, используем to_thread
            yookassa_payment_obj = await asyncio.to_thread(
                YooKassaPaymentObject.create, payment_request, idempotency_key
            )

            if yookassa_payment_obj and yookassa_payment_obj.confirmation:
                new_db_payment = Payment(
                    yookassa_payment_id=yookassa_payment_obj.id,
                    user_id=db_user_obj.id,
                    amount=payment_amount,
                    currency="RUB", # Можно брать из yookassa_payment_obj.amount.currency
                    status=yookassa_payment_obj.status,
                    description=description,
                    additional_data=json.dumps(yookassa_metadata)
                )
                session.add(new_db_payment)
                await session.commit()
                await context.bot.send_message(chat_id, f"Для оплаты перейдите по ссылке:\n{yookassa_payment_obj.confirmation.confirmation_url}")
            else:
                logger.error(f"Не удалось создать платеж YooKassa для пользователя {user_tg.id}. Ответ: {yookassa_payment_obj}")
                await context.bot.send_message(chat_id, "Не удалось создать ссылку на оплату. Пожалуйста, попробуйте позже.")
        except Exception as e:
            logger.error(f"Ошибка при создании платежа YooKassa для пользователя {user_tg.id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, "Произошла ошибка при формировании запроса на оплату. Пожалуйста, попробуйте позже.")
        except Exception as e:
            logger.error(f"Ошибка при создании платежа YooKassa для пользователя {user_tg.id}: {e}", exc_info=True)
            await context.bot.send_message(chat_id, "Произошла ошибка при формировании запроса на оплату. Пожалуйста, попробуйте позже.")

# --- ПЛАНИРОВЩИК ЗАДАЧ ---
async def check_and_deactivate_expired_keys():
    logger.info("APScheduler: Checking expired Marzban subscriptions...")

    if not marzban_client:
        logger.error("APScheduler: Marzban client not initialized. Skipping check.")
        return

    marzban_api_token_val = await get_marzban_api_token()
    if not marzban_api_token_val:
        logger.error("APScheduler: Failed to get Marzban API token. Skipping check.")
        return

    async for session in get_async_session():
        keys_modified_count = 0
        try:
            # Выбираем подписки, которые активны в нашей БД и у которых подошло время истечения
            stmt = select(VpnKey).where(
                VpnKey.is_active == True,
                VpnKey.expires_at <= datetime.utcnow()
            )
            expired_db_subscriptions = (await session.execute(stmt)).scalars().all()

            if not expired_db_subscriptions:
                logger.info("APScheduler: No subscriptions found in DB that are marked active and past expiration time.")
                return

            logger.info(f"APScheduler: Found {len(expired_db_subscriptions)} potentially expired subscriptions in DB to check/deactivate.")

            for db_sub in expired_db_subscriptions:
                logger.info(f"APScheduler: Processing DB subscription ID {db_sub.id} (Marzban User: {db_sub.marzban_username}) for user_id {db_sub.user_id}.")
                try:
                    # Сначала проверим статус в Marzban, чтобы не удалять, если она была продлена другим способом
                    marzban_user_info = await marzban_client.get_user(username=db_sub.marzban_username, token=marzban_api_token_val)

                    needs_deactivation_in_marzban = True
                    if marzban_user_info:
                        if marzban_user_info.status == "active":
                            marzban_expires_dt = datetime.fromtimestamp(marzban_user_info.expire) if marzban_user_info.expire else None
                            if marzban_expires_dt and marzban_expires_dt > datetime.utcnow():
                                # Подписка была продлена в Marzban, обновим нашу БД
                                logger.info(f"Subscription {db_sub.marzban_username} (DB ID: {db_sub.id}) was extended in Marzban to {marzban_expires_dt}. Updating local DB.")
                                db_sub.expires_at = marzban_expires_dt
                                db_sub.is_active = True # Убедимся, что она активна
                                # Возможно, нужно обновить и data_limit, если он изменился
                                await session.commit()
                                keys_modified_count +=1
                                needs_deactivation_in_marzban = False # Не удаляем из Marzban и не деактивируем локально (уже обновили)
                            # Если же marzban_expires_dt все еще <= now, то удаляем
                    else:
                        # Пользователя нет в Marzban, значит можно просто деактивировать у нас
                        logger.info(f"User {db_sub.marzban_username} (DB ID: {db_sub.id}) not found in Marzban. Deactivating locally.")
                        needs_deactivation_in_marzban = False
                        # Удалять из Marzban нечего, но локально деактивировать надо

                    if needs_deactivation_in_marzban:
                        logger.info(f"Attempting to delete user {db_sub.marzban_username} from Marzban panel.")
                        await marzban_client.delete_user(username=db_sub.marzban_username, token=marzban_api_token_val)
                        logger.info(f"Successfully deleted user {db_sub.marzban_username} from Marzban (or user already deleted).")

                    # В любом случае (удален из Marzban или не найден там), если needs_deactivation_in_marzban был true (или не найден)
                    # и мы дошли сюда без обновления (т.е. он действительно истек), деактивируем локально.
                    # Если needs_deactivation_in_marzban=false И он не был обновлен (т.е. не найден в Marzban), тоже деактивируем локально.
                    if needs_deactivation_in_marzban or (not marzban_user_info and not needs_deactivation_in_marzban):
                        db_sub.is_active = False
                        logger.info(f"Deactivated subscription ID {db_sub.id} (Marzban User: {db_sub.marzban_username}) in local DB.")
                        keys_modified_count +=1

                except Exception as e:
                    # Если ошибка "User not found" от marzpy, то это нормально, можно просто деактивировать локально.
                    # Нужно проверить, какой тип исключения кидает marzpy для "user not found"
                    # Например, if isinstance(e, MarzbanUserNotFoundError): ...
                    err_msg = str(e).lower()
                    if "user not found" in err_msg or "not found" in err_msg: # Грубая проверка
                        logger.warning(f"User {db_sub.marzban_username} not found in Marzban during deactivation (Error: {e}). Deactivating locally.")
                        db_sub.is_active = False
                        keys_modified_count +=1
                    elif "token" in err_msg:
                         logger.error(f"Marzban API token error during deactivation of {db_sub.marzban_username}: {e}. Attempting to refresh token for next run.")
                         await get_marzban_api_token(force_refresh=True) # Обновить токен для следующих попыток
                    else:
                        logger.error(f"Error processing expired Marzban subscription for {db_sub.marzban_username} (DB ID: {db_sub.id}): {e}", exc_info=True)
                        # Не меняем статус в БД, чтобы попробовать в следующий раз, если это временная ошибка API Marzban
                        # кроме ошибки токена, которую мы уже попробовали обновить

            if keys_modified_count > 0:
                await session.commit()
                logger.info(f"APScheduler: Committed changes for {keys_modified_count} subscriptions.")
            else:
                logger.info("APScheduler: No subscriptions required database changes in this run.")

        except Exception as e:
            logger.error(f"APScheduler error in check_and_deactivate_expired_keys: {e}", exc_info=True)
            await session.rollback()

# --- ЗАПУСК БОТА ---
def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN not found!")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    # Инициализация клиента Marzban при старте
    async def post_init(app: Application):
        await initialize_marzban_client() # Инициализируем клиент Marzban
        # Затем создаем таблицы БД (если их нет)
        await create_db_tables()

        # Запуск планировщика
        scheduler = AsyncIOScheduler(timezone="UTC") # Перенес инициализацию сюда, чтобы она была после async context
        scheduler.add_job(check_and_deactivate_expired_keys, 'interval', hours=1) # Можно сделать чаще, например, каждые 10-15 минут
        scheduler.start()
        app.job_queue = scheduler # Сохраняем scheduler в application context если нужно будет им управлять
        logger.info("APScheduler started.")

    application.post_init = post_init
    
    # Обработчики команд
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_GET_KEY}$"), get_key_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_KEYS}$"), my_keys_handler))
    
    # Обновленный pattern для extend_callback_handler
    application.add_handler(CallbackQueryHandler(extend_callback_handler, pattern=r"^extend_sub_(\d+)$"))

    # Удаляем старый обработчик выбора протокола
    # application.add_handler(CallbackQueryHandler(handle_protocol_selection, pattern=f"^({PROTOCOL_CALLBACK_OUTLINE}|{PROTOCOL_CALLBACK_AMNEZIA})$"))

    async def on_shutdown(app: Application):
        logger.info("Bot is shutting down...")
        if app.job_queue and app.job_queue.running: # type: ignore
            app.job_queue.shutdown() # type: ignore
            logger.info("APScheduler stopped.")
        if marzban_client: # Хотя у marzpy нет явного close() или dispose() метода в README
            logger.info("Marzban client does not have an explicit close method in marzpy.")
        # Здесь можно было бы добавить await async_engine.dispose(), если бы это было легко сделать синхронно с PTB < v20
        # Для PTB v20+ это делается в application.shutdown()

    application.post_shutdown = on_shutdown

    logger.info("Bot starting...")
    application.run_polling()
    # Код после run_polling() для PTB < v20 обычно не выполняется при штатном завершении через сигналы,
    # поэтому логику остановки лучше помещать в post_shutdown или управлять циклом asyncio самому (для v20+)
    # if scheduler.running: # Этот блок может не всегда срабатывать как ожидается
    #     scheduler.shutdown()
    #     logger.info("APScheduler stopped.")

if __name__ == "__main__":
    main()
