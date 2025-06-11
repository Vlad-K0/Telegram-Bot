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

# --- Импорты ---
from database import User, OutlineKey, Payment, create_db_tables, get_async_session
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

if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    YooKassaConfiguration.account_id = YOOKASSA_SHOP_ID
    YooKassaConfiguration.secret_key = YOOKASSA_SECRET_KEY
    logger.info("Конфигурация ЮKassa установлена.")

# --- Определение кнопок меню ---
BUTTON_GET_KEY = "🔑 Получить/Продлить доступ"
BUTTON_MY_KEYS = "ℹ️ Моя подписка"

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
    Главный обработчик, который решает, выдать бесплатный ключ или инициировать оплату.
    """
    user_tg = update.effective_user
    chat_id = update.effective_chat.id
    
    async for session in get_async_session():
        db_user = (await session.execute(select(User).where(User.telegram_id == user_tg.id))).scalar_one()
        
        stmt_keys = select(OutlineKey).where(OutlineKey.user_id == db_user.id)
        existing_key = (await session.execute(stmt_keys)).scalars().first()
        
        if existing_key:
            logger.info(f"User {user_tg.id} is an existing user. Initiating payment.")
            await initiate_yookassa_payment(update, context, months=1, duration_days=30)
        else:
            logger.info(f"User {user_tg.id} is a new user. Issuing a free trial key.")
            await context.bot.send_message(chat_id, "🎉 Поздравляем! Как новому пользователю, мы дарим вам бесплатный пробный доступ.")
            
            if not outline_client:
                await context.bot.send_message(chat_id, "Не удалось связаться с VPN-сервером. Попробуйте позже.")
                return

            try:
                new_key_obj = await asyncio.to_thread(outline_client.create_key)
                expires_at = datetime.utcnow() + timedelta(days=FREE_TRIAL_DAYS)
                
                new_db_key = OutlineKey(
                    outline_id_on_server=str(new_key_obj.key_id),
                    access_url=new_key_obj.access_url,
                    name=f"tg_user_{db_user.id}_trial",
                    user_id=db_user.id,
                    expires_at=expires_at,
                    is_active=True
                )
                session.add(new_db_key)
                await session.commit()
                
                expires_str = expires_at.strftime('%d.%m.%Y в %H:%M')
                msg_text = (
                    f"✅ Ваш бесплатный ключ готов!\n\n"
                    f"🔑 Ключ доступа:\n`{new_key_obj.access_url}`\n\n"
                    f"Он будет действителен до: *{expires_str} UTC*."
                )
                await context.bot.send_message(chat_id, msg_text, parse_mode='Markdown')

            except Exception as e:
                logger.error(f"Error creating a free key for user {user_tg.id}: {e}")
                await context.bot.send_message(chat_id, "Произошла ошибка при создании бесплатного ключа. Свяжитесь с поддержкой.")

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
        active_key = (await session.execute(stmt)).scalars().first()

        if not active_key:
            await update.message.reply_text("У вас нет активных ключей. Нажмите 'Получить/Продлить доступ', чтобы получить свой первый ключ бесплатно!")
            return

        expires_str = active_key.expires_at.strftime('%d.%m.%Y в %H:%M')
        response_text = f"🔑 Ваш активный ключ действителен до: *{expires_str} UTC*\n\n`{active_key.access_url}`"
        
        keyboard = [[InlineKeyboardButton("Продлить подписку на 1 месяц", callback_data="extend_1_month")]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(response_text, parse_mode='Markdown', reply_markup=reply_markup)

async def extend_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает нажатие на inline-кнопку "Продлить подписку".
    """
    query = update.callback_query
    await query.answer()
    await initiate_yookassa_payment(update, context, months=1, duration_days=30)

async def initiate_yookassa_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, months: int, duration_days: int):
    """
    Создает платеж в ЮKassa.
    """
    user_tg = update.effective_user
    chat_id = update.effective_chat.id
    
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

        active_key = (await session.execute(select(OutlineKey).where(and_(OutlineKey.user_id == db_user.id, OutlineKey.is_active == True, OutlineKey.expires_at > datetime.utcnow())))).scalars().first()
        
        yookassa_metadata = {"internal_user_db_id": str(db_user.id), "telegram_user_id": str(user_tg.id), "duration_days": str(duration_days)}
        description = f"Подписка Outline VPN на {months} мес."

        if active_key:
            yookassa_metadata["action"] = "extend"
            yookassa_metadata["key_to_extend_id"] = active_key.id
            description = f"Продление подписки на {months} мес."
        else:
            yookassa_metadata["action"] = "create"

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
        try:
            stmt = select(OutlineKey).where(and_(OutlineKey.is_active == True, OutlineKey.expires_at <= datetime.utcnow()))
            expired_keys = (await session.execute(stmt)).scalars().all()
            if not expired_keys or not outline_client: return
            
            for key in expired_keys:
                try:
                    await asyncio.to_thread(outline_client.delete_key, key.outline_id_on_server)
                    key.is_active = False
                except Exception as e: logger.error(f"Error deleting key {key.id}: {e}")
            await session.commit()
        except Exception as e: logger.error(f"Scheduler error: {e}")

# --- ЗАПУСК БОТА ---
def main() -> None:
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN not found!")
        return

    application = Application.builder().token(BOT_TOKEN).build()
    
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(check_and_deactivate_expired_keys, 'interval', minutes=5)
    
    async def post_init(app: Application):
        await create_db_tables()
        scheduler.start()
        logger.info("APScheduler started.")

    application.post_init = post_init
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_GET_KEY}$"), get_key_handler))
    application.add_handler(MessageHandler(filters.TEXT & filters.Regex(f"^{BUTTON_MY_KEYS}$"), my_keys_handler))
    application.add_handler(CallbackQueryHandler(extend_callback_handler, pattern="^extend_1_month$"))
    
    logger.info("Bot starting...")
    application.run_polling()
    if scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler stopped.")

if __name__ == "__main__":
    main()
