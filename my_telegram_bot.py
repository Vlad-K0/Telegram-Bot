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

# --- Импорты из вашего файла database.py ---
from database import User, OutlineKey, Payment, create_db_tables, get_async_session

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
BASE_PRICE_PER_MONTH = Decimal(os.getenv("BASE_PRICE_PER_MONTH", "160.00"))
# --- КОНЕЦ НАСТРОЕК ---

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

outline_client = None
if API_URL and CERT_SHA256:
    try:
        outline_client = OutlineVPN(api_url=API_URL, cert_sha256=CERT_SHA256)
        logger.info("Успешное подключение к Outline VPN API.")
    except Exception as e:
        logger.error(f"Ошибка Outline API: {e}", exc_info=True)

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    YooKassaConfiguration.account_id = YOOKASSA_SHOP_ID
    YooKassaConfiguration.secret_key = YOOKASSA_SECRET_KEY
    logger.info("Конфигурация ЮKassa установлена.")
else:
    logger.warning("YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY не найдены в .env.")

# --- ОПРЕДЕЛЕНИЕ КНОПОК МЕНЮ ---
BUTTON_BUY_1_MONTH = "Купить/Продлить на 1 месяц 💳"
BUTTON_MY_KEYS = "🔑 Мои ключи"

main_menu_keyboard = [
    [KeyboardButton(BUTTON_BUY_1_MONTH)],
    [KeyboardButton(BUTTON_MY_KEYS)],
]
REPLY_MARKUP_MAIN_MENU = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик команды /start. Регистрирует пользователя и показывает главное меню.
    """
    user_tg = update.effective_user
    logger.info(f"User {user_tg.first_name} ({user_tg.id}) started.")
    async for session in get_async_session():
        stmt = select(User).where(User.telegram_id == user_tg.id)
        db_user = (await session.execute(stmt)).scalar_one_or_none()
        if not db_user:
            db_user = User(telegram_id=user_tg.id, username=user_tg.username, first_name=user_tg.first_name, last_name=user_tg.last_name)
            session.add(db_user)
            await session.commit()
            logger.info(f"User {db_user.username} ({db_user.telegram_id}) added to DB.")
    
    await update.message.reply_html(f"Привет, {user_tg.mention_html()}! 👋\n\nЯ помогу вам приобрести и управлять доступом к VPN.\n\nВыберите опцию в меню:", reply_markup=REPLY_MARKUP_MAIN_MENU)

async def initiate_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик нажатия на кнопку покупки/продления.
    """
    await initiate_yookassa_payment(update, context, months=1, duration_days=30)


async def initiate_yookassa_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, months: int, duration_days: int):
    """
    Создает платеж в ЮKassa. Эта функция теперь универсальна.
    """
    user_tg = update.effective_user
    chat_id = update.effective_chat.id
    logger.info(f"User {user_tg.id} initiated a payment for {months} months.")
    
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        await context.bot.send_message(chat_id, "Сервис оплаты временно недоступен.")
        return

    payment_amount = BASE_PRICE_PER_MONTH * months
    
    async for session in get_async_session():
        stmt_user = select(User).where(User.telegram_id == user_tg.id)
        db_user = (await session.execute(stmt_user)).scalar_one_or_none()
        if not db_user:
            await context.bot.send_message(chat_id, "Ошибка: пользователь не найден. Нажмите /start.")
            return

        now = datetime.utcnow()
        stmt_key = select(OutlineKey).where(
            and_(OutlineKey.user_id == db_user.id, OutlineKey.is_active == True, OutlineKey.expires_at > now)
        ).order_by(OutlineKey.expires_at.desc())
        active_key = (await session.execute(stmt_key)).scalars().first()
        
        yookassa_metadata = {
            "internal_user_db_id": str(db_user.id), 
            "telegram_user_id": str(user_tg.id), 
            "duration_days": str(duration_days)
        }

        if active_key:
            description = f"Продление подписки Outline VPN на {months} мес."
            yookassa_metadata["action"] = "extend"
            yookassa_metadata["key_to_extend_id"] = active_key.id
        else:
            description = f"Новая подписка Outline VPN на {months} мес."
            yookassa_metadata["action"] = "create"

        idempotency_key = str(uuid.uuid4())
        return_url = f"https://t.me/{context.bot.username}"

        builder = PaymentRequestBuilder()
        builder.set_amount({"value": str(payment_amount), "currency": "RUB"}) \
            .set_capture(True) \
            .set_confirmation({"type": "redirect", "return_url": return_url}) \
            .set_description(description) \
            .set_metadata(yookassa_metadata)
        
        payment_request = builder.build()
        yookassa_payment_obj = await asyncio.to_thread(YooKassaPaymentObject.create, payment_request, idempotency_key)

        if yookassa_payment_obj and yookassa_payment_obj.confirmation and yookassa_payment_obj.confirmation.confirmation_url:
            new_db_payment = Payment(
                yookassa_payment_id=yookassa_payment_obj.id, 
                user_id=db_user.id, 
                amount=payment_amount, 
                status=yookassa_payment_obj.status, 
                description=description, 
                additional_data=json.dumps(yookassa_metadata)
            )
            session.add(new_db_payment)
            await session.commit()
            logger.info(f"Created Yookassa payment ID: {yookassa_payment_obj.id} for user {db_user.id}.")
            await context.bot.send_message(chat_id, f"Для оплаты перейдите по ссылке:\n{yookassa_payment_obj.confirmation.confirmation_url}")
        else:
            logger.error(f"Yookassa payment creation failed for user {db_user.id}.")
            await context.bot.send_message(chat_id, "Не удалось создать ссылку на оплату. Попробуйте позже.")


async def my_keys_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Показывает активные ключи пользователя и кнопку для продления.
    """
    user_tg = update.effective_user
    now = datetime.utcnow()
    async for session in get_async_session():
        stmt = select(OutlineKey).join(User).where(
            User.telegram_id == user_tg.id,
            OutlineKey.is_active == True,
            OutlineKey.expires_at > now
        )
        active_keys = (await session.execute(stmt)).scalars().all()

        if not active_keys:
            await update.message.reply_text("У вас нет активных ключей. Вы можете приобрести новый, нажав на кнопку в меню.")
            return

        response_text = "🔑 Ваш активный ключ:\n\n"
        # Обычно у пользователя один активный ключ, берем первый
        key = active_keys[0] 
        expires_str = key.expires_at.strftime('%d.%m.%Y в %H:%M')
        response_text += f"Действителен до: *{expires_str} UTC*\n\n`{key.access_url}`\n\nНажмите кнопку ниже, чтобы продлить подписку."

        # Создаем Inline-кнопку
        keyboard = [
            [InlineKeyboardButton("Продлить подписку на 1 месяц", callback_data="extend_1_month")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(response_text, parse_mode='Markdown', reply_markup=reply_markup)


async def extend_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие на inline-кнопку "Продлить подписку".
    """
    query = update.callback_query
    # Убираем "часики" на кнопке
    await query.answer()
    
    # Запускаем тот же процесс оплаты
    await initiate_yookassa_payment(update, context, months=1, duration_days=30)


async def check_and_deactivate_expired_keys():
    """
    Планировщик, который деактивирует просроченные ключи.
    """
    logger.info("APScheduler: Checking expired keys...")
    async for session in get_async_session():
        try:
            stmt = select(OutlineKey).where(OutlineKey.is_active == True, OutlineKey.expires_at <= datetime.utcnow())
            expired_keys = (await session.execute(stmt)).scalars().all()
            if not expired_keys:
                return
            
            logger.info(f"APScheduler: Found {len(expired_keys)} expired keys to deactivate.")
            for key in expired_keys:
                if not outline_client: continue
                try:
                    await asyncio.to_thread(outline_client.delete_key, key.outline_id_on_server)
                    key.is_active = False
                    logger.info(f"APScheduler: Key {key.outline_id_on_server} deleted and marked inactive.")
                except Exception as e_del:
                    logger.error(f"APScheduler: Error deleting key {key.outline_id_on_server}: {e_del}")
            await session.commit()
        except Exception as e_main:
            logger.error(f"APScheduler: Global error: {e_main}", exc_info=True)


def main() -> None:
    """
    Основная функция для запуска бота.
    """
    if not BOT_TOKEN:
        logger.critical("CRITICAL: BOT_TOKEN not found!")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_and_deactivate_expired_keys, 'interval', hours=1)
    
    async def post_init(app: Application) -> None:
        await create_db_tables()
        scheduler.start()
        logger.info("APScheduler started.")

    application.post_init = post_init
    
    # --- РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_BUY_1_MONTH}$"), initiate_payment_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_KEYS}$"), my_keys_handler))
    application.add_handler(CallbackQueryHandler(extend_callback_handler, pattern="^extend_1_month$"))
    
    logger.info("Bot starting...")
    application.run_polling()
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")
    logger.info("Bot stopped.")

if __name__ == "__main__":
    main()
