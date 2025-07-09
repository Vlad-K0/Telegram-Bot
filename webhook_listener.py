import logging
import os
import json
import asyncio
import uuid # Для генерации marzban_username при необходимости

from flask import Flask, request # Оставляем Flask для текущей структуры, но помним о рекомендации перейти на ASGI
from dotenv import load_dotenv
from sqlalchemy.future import select
# REMOVE: from sqlalchemy import and_ # Если не используется, можно удалить. Пока оставлю.
from datetime import datetime, timedelta
# REMOVE: from outline_vpn.outline_vpn import OutlineVPN
from telegram import Bot as TelegramBotInstance

# +++ Marzban Imports +++
from marzpy import Marzban
from marzpy.api.user import User as MarzbanUser

# --- 1. ЗАГРУЗКА НАСТРОЕК ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
# REMOVE: API_URL = os.getenv("API_URL")
# REMOVE: CERT_SHA256 = os.getenv("CERT_SHA256")
# REMOVE: AMNEZIA_API_URL_WH = os.getenv("AMNEZIA_API_URL")

# +++ Marzban Settings (дублируются из my_telegram_bot.py, можно вынести в общий config.py) +++
MARZBAN_PANEL_URL = os.getenv("MARZBAN_PANEL_URL")
MARZBAN_USERNAME = os.getenv("MARZBAN_USERNAME")
MARZBAN_PASSWORD = os.getenv("MARZBAN_PASSWORD")
# Лимиты трафика для платной подписки (в ГБ), если нужны при создании пользователя
MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID_WH = int(os.getenv("MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID", "50"))


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__) # log используется для Flask, logger_webhook_process для логики обработки


# --- 2. ИМПОРТ МОДЕЛЕЙ БАЗЫ ДАННЫХ ---
try:
    # Используем DbUser для нашей модели User, чтобы не конфликтовать с MarzbanUser
    from database import User as DbUser, VpnKey, Payment, AsyncSessionLocal
    log.info("Модели БД успешно импортированы в webhook_listener.")
except ImportError as e:
    log.error(f"Не удалось импортировать модели БД: {e}")
    DbUser, VpnKey, Payment, AsyncSessionLocal = None, None, None, None

# REMOVE: Импорты Amnezia и констант протоколов
# try:
#     from my_telegram_bot import create_amnezia_user, get_amnezia_config, PROTOCOL_CALLBACK_OUTLINE, PROTOCOL_CALLBACK_AMNEZIA
#     log.info("Amnezia client functions and protocol constants imported successfully into webhook_listener.")
# except ImportError as e:
#     log.error(f"Could not import Amnezia client functions or protocol constants from my_telegram_bot: {e}")
#     create_amnezia_user, get_amnezia_config = None, None
#     PROTOCOL_CALLBACK_OUTLINE, PROTOCOL_CALLBACK_AMNEZIA = 'proto_outline', 'proto_amnezia' # Fallback defaults


# --- 3. ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ---
# REMOVE: outline_client_webhook = None
# REMOVE: ... (логика инициализации outline_client_webhook) ...

# +++ Marzban Client for Webhook +++
# Используем отдельные переменные для клиента и токена в вебхуке,
# чтобы избежать конфликта состояния с основным ботом, если они работают в разных процессах.
marzban_client_wh: Marzban | None = None
marzban_api_token_wh: str | None = None

async def initialize_marzban_client_wh():
    global marzban_client_wh
    if MARZBAN_PANEL_URL and MARZBAN_USERNAME and MARZBAN_PASSWORD:
        try:
            marzban_client_wh = Marzban(username=MARZBAN_USERNAME, password=MARZBAN_PASSWORD, base_url=MARZBAN_PANEL_URL)
            log.info("Webhook: Клиент Marzban инициализирован. URL: %s", MARZBAN_PANEL_URL)
        except Exception as e:
            log.error(f"Webhook: Ошибка инициализации клиента Marzban: {e}")
            marzban_client_wh = None
    else:
        log.error("Webhook: Не заданы MARZBAN_PANEL_URL, MARZBAN_USERNAME или MARZBAN_PASSWORD в .env. Клиент Marzban не будет работать.")

async def get_marzban_api_token_wh(force_refresh: bool = False) -> str | None:
    global marzban_api_token_wh
    if not marzban_client_wh:
        log.error("Webhook: Клиент Marzban не инициализирован.")
        return None

    if marzban_api_token_wh and not force_refresh:
        return marzban_api_token_wh

    try:
        token = await marzban_client_wh.get_token()
        if token:
            marzban_api_token_wh = token
            log.info("Webhook: Токен Marzban успешно получен/обновлен.")
            return marzban_api_token_wh
        else:
            log.error("Webhook: Не удалось получить токен Marzban (ответ None).")
            marzban_api_token_wh = None
            return None
    except Exception as e:
        log.error(f"Webhook: Ошибка при получении токена Marzban: {e}", exc_info=True)
        marzban_api_token_wh = None
        return None

# Вызов инициализации клиента при старте вебхук-приложения (если это модуль)
# Для Flask это лучше делать в before_first_request или при создании app, но в async контексте.
# Пока оставим так, предполагая, что модуль импортируется один раз.
# Для ASGI-приложения это было бы в startup-хуке.
# asyncio.run(initialize_marzban_client_wh()) # Это не сработает здесь на уровне модуля.
# Инициализацию нужно будет вызвать в подходящий момент, например, перед первым использованием.

# --- 4. ОСНОВНАЯ ЛОГИКА ОБРАБОТКИ ПЛАТЕЖА ---
async def process_yookassa_notification_standalone(notification_data: dict): # Убрали outline_client из аргументов
    logger_webhook_process = logging.getLogger('yookassa_process_marzban') # Новое имя логгера для ясности
    bot_instance = None
    if BOT_TOKEN:
        bot_instance = TelegramBotInstance(token=BOT_TOKEN)

    # Инициализация клиента Marzban, если еще не сделана (важно для worker-based серверов)
    if not marzban_client_wh:
        await initialize_marzban_client_wh()

    if not marzban_client_wh: # Проверка после попытки инициализации
        logger_webhook_process.error("Критическая ошибка: Клиент Marzban не инициализирован в вебхуке.")
        # В этом случае мы не можем обработать платеж для VPN.
        # YooKassa ожидает 200 OK, иначе будет повторять.
        # Если мы не можем ничего сделать, возможно, стоит вернуть ошибку, чтобы YooKassa повторила позже,
        # но это может привести к зацикливанию, если проблема с конфигом Marzban постоянная.
        # Пока что просто логируем и выходим (YooKassa получит 200 OK от Flask и не повторит).
        # Это означает, что платеж прошел, но VPN не выдан. Требуется ручное вмешательство.
        # TODO: Рассмотреть отправку уведомления администратору в этом случае.
        return


    event = notification_data.get("event")
    payment_object = notification_data.get("object")

    if not (event and payment_object and payment_object.get("id")):
        logger_webhook_process.error("Некорректные данные уведомления YooKassa.")
        return

    yookassa_payment_id = payment_object.get("id")

    if event == "payment.succeeded" and payment_object.get("status") == "succeeded":
        logger_webhook_process.info(f"Платеж {yookassa_payment_id} УСПЕШНО ПРОШЕЛ.")
        
        async with AsyncSessionLocal() as session: # Используем async with для сессии
            try:
                stmt = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
                db_payment = (await session.execute(stmt)).scalar_one_or_none()

                if not db_payment:
                    logger_webhook_process.warning(f"Платеж {yookassa_payment_id} не найден в нашей БД. Возможно, уже обработан или ошибка.")
                    return

                if db_payment.status == "succeeded":
                    logger_webhook_process.warning(f"Платеж {yookassa_payment_id} уже помечен как 'succeeded' в нашей БД.")
                    return # Предотвращение двойной обработки

                additional_data = json.loads(db_payment.additional_data or '{}')
                action = additional_data.get("action", "create") # "create" или "extend"
                duration_days = int(additional_data.get("duration_days", 30))
                telegram_user_id = int(additional_data.get("telegram_user_id"))
                user_db_id = int(additional_data.get("internal_user_db_id")) # ID из нашей таблицы users
                
                # Получаем токен Marzban API
                marzban_api_token_val = await get_marzban_api_token_wh()
                if not marzban_api_token_val:
                    logger_webhook_process.error(f"Не удалось получить токен Marzban для обработки платежа {yookassa_payment_id}.")
                    # Оставляем платеж в pending, чтобы попробовать обработать позже или вручную.
                    # TODO: Уведомить администратора.
                    return

                new_marzban_user_obj_from_api = None # Для хранения объекта пользователя от Marzban API

                if action == "extend":
                    marzban_username_to_extend = additional_data.get("marzban_username")
                    subscription_db_id = additional_data.get("subscription_db_id") # ID VpnKey из нашей БД

                    if not marzban_username_to_extend or not subscription_db_id:
                        logger_webhook_process.error(f"Для action='extend' платежа {yookassa_payment_id} отсутствуют marzban_username или subscription_db_id в metadata.")
                        # Попытаться создать как новую подписку? Или ошибка? Пока ошибка.
                        # TODO: Уведомить администратора.
                        return

                    db_subscription_to_extend = await session.get(VpnKey, int(subscription_db_id))
                    if not db_subscription_to_extend or db_subscription_to_extend.user_id != user_db_id:
                        logger_webhook_process.error(f"Подписка ID {subscription_db_id} для продления не найдена или не принадлежит пользователю {user_db_id} (платеж {yookassa_payment_id}).")
                        # TODO: Уведомить администратора.
                        return

                    if db_subscription_to_extend.marzban_username != marzban_username_to_extend:
                         logger_webhook_process.error(f"Несоответствие marzban_username для подписки ID {subscription_db_id}: в БД {db_subscription_to_extend.marzban_username}, в метаданных {marzban_username_to_extend}.")
                         # TODO: Уведомить администратора.
                         return

                    try:
                        current_marzban_user = await marzban_client_wh.get_user(username=marzban_username_to_extend, token=marzban_api_token_val)
                        if not current_marzban_user:
                            logger_webhook_process.warning(f"Пользователь Marzban {marzban_username_to_extend} не найден для продления (платеж {yookassa_payment_id}). Попытка создать нового.")
                            action = "create" # Переходим к созданию нового, если старый не найден
                        else:
                            # Продление существующего пользователя
                            current_expire_dt = datetime.fromtimestamp(current_marzban_user.expire) if current_marzban_user.expire else datetime.utcnow()
                            # Если подписка уже истекла, считаем от текущего момента, иначе от даты истечения
                            start_date_for_продление = max(datetime.utcnow(), current_expire_dt)
                            new_expire_dt = start_date_for_продление + timedelta(days=duration_days)
                            new_expire_timestamp = int(new_expire_dt.timestamp())

                            # Предполагаем, что data_limit сбрасывается/устанавливается заново при продлении
                            # или можно добавить логику добавления трафика, если Marzban это поддерживает через modify_user
                            new_data_limit_bytes = MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID_WH * (1024**3)

                            modified_user_config = MarzbanUser(
                                username=marzban_username_to_extend, # username не меняется
                                proxies=current_marzban_user.proxies, # Сохраняем текущие proxies/inbounds
                                inbounds=current_marzban_user.inbounds,
                                expire=new_expire_timestamp,
                                data_limit=new_data_limit_bytes, # Новый лимит на период
                                status="active", # Активируем, если был disabled (кроме админского disabled)
                                data_limit_reset_strategy=current_marzban_user.data_limit_reset_strategy # или "no_reset"
                            )

                            new_marzban_user_obj_from_api = await marzban_client_wh.modify_user(
                                username=marzban_username_to_extend,
                                token=marzban_api_token_val,
                                user=modified_user_config
                            )

                            db_subscription_to_extend.expires_at = new_expire_dt
                            db_subscription_to_extend.is_active = True
                            db_subscription_to_extend.payment_id = db_payment.id # Обновляем связь с последним платежом
                            # db_subscription_to_extend.subscription_url можно обновить, если он мог измениться
                            if new_marzban_user_obj_from_api and new_marzban_user_obj_from_api.subscription_url:
                                db_subscription_to_extend.subscription_url = new_marzban_user_obj_from_api.subscription_url

                            # session.add(db_subscription_to_extend) # Уже в сессии
                            logger_webhook_process.info(f"Подписка Marzban {marzban_username_to_extend} продлена до {new_expire_dt}.")
                            if bot_instance:
                                await bot_instance.send_message(
                                    chat_id=telegram_user_id,
                                    text=f"✅ Ваша VPN подписка ({marzban_username_to_extend}) успешно продлена!\n\n"
                                         f"Новая дата окончания: {new_expire_dt.strftime('%d.%m.%Y %H:%M')} UTC\n"
                                         f"Лимит трафика: {MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID_WH} ГБ"
                                )
                    except Exception as e_extend:
                        logger_webhook_process.error(f"Ошибка при продлении пользователя Marzban {marzban_username_to_extend} (платеж {yookassa_payment_id}): {e_extend}", exc_info=True)
                        # TODO: Уведомить администратора. Платеж прошел, но продление не удалось.
                        # Не меняем статус платежа, чтобы можно было повторить вручную.
                        if "token" in str(e_extend).lower(): await get_marzban_api_token_wh(force_refresh=True)
                        return # Выходим, чтобы не пометить платеж как успешный в БД


                if action == "create": # Если это создание нового или fallback с продления
                    paid_marzban_username = f"paid_tg_{telegram_user_id}_{uuid.uuid4().hex[:8]}"
                    paid_expire_dt = datetime.utcnow() + timedelta(days=duration_days)
                    paid_expire_timestamp = int(paid_expire_dt.timestamp())
                    paid_data_limit_bytes = MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID_WH * (1024**3)

                    new_paid_marzban_user_config = MarzbanUser(
                        username=paid_marzban_username,
                        proxies={},
                        inbounds={},
                        expire=paid_expire_timestamp,
                        data_limit=paid_data_limit_bytes,
                        data_limit_reset_strategy="no_reset",
                        status="active"
                    )
                    try:
                        new_marzban_user_obj_from_api = await marzban_client_wh.add_user(user=new_paid_marzban_user_config, token=marzban_api_token_val)
                        if not new_marzban_user_obj_from_api or not new_marzban_user_obj_from_api.subscription_url:
                            logger_webhook_process.error(f"Не удалось создать платного пользователя Marzban или отсутствует subscription_url для {paid_marzban_username} (платеж {yookassa_payment_id}).")
                            # TODO: Уведомить администратора.
                            return # Выходим, платеж не обработан до конца

                        new_db_vpn_key = VpnKey(
                            marzban_username=paid_marzban_username,
                            subscription_url=new_marzban_user_obj_from_api.subscription_url,
                            name=f"Платная подписка Marzban для user_db_id {user_db_id}",
                            user_id=user_db_id,
                            payment_id=db_payment.id, # Связываем с текущим платежом
                            created_at=datetime.utcnow(),
                            expires_at=paid_expire_dt,
                            is_active=True,
                            is_trial=False
                        )
                        session.add(new_db_vpn_key)
                        # db_payment.marzban_subscription_association = new_db_vpn_key # Устанавливаем связь

                        logger_webhook_process.info(f"Создана новая платная подписка Marzban {paid_marzban_username} до {paid_expire_dt}.")
                        if bot_instance:
                            await bot_instance.send_message(
                                chat_id=telegram_user_id,
                                text=f"✅ Оплата прошла успешно! Ваша новая VPN подписка готова.\n\n"
                                     f"🔗 Ссылка-подписка:\n`{new_marzban_user_obj_from_api.subscription_url}`\n\n"
                                     f"🗓️ Действительна до: {paid_expire_dt.strftime('%d.%m.%Y %H:%M')} UTC\n"
                                     f"📊 Лимит трафика: {MARZBAN_DEFAULT_DATA_LIMIT_GB_PAID_WH} ГБ",
                                parse_mode='Markdown'
                            )
                    except Exception as e_create:
                        logger_webhook_process.error(f"Ошибка при создании платного пользователя Marzban {paid_marzban_username} (платеж {yookassa_payment_id}): {e_create}", exc_info=True)
                        # TODO: Уведомить администратора.
                        if "token" in str(e_create).lower(): await get_marzban_api_token_wh(force_refresh=True)
                        return # Выходим, платеж не обработан до конца

                # Если все операции с Marzban прошли успешно
                db_payment.status = "succeeded"
                db_payment.updated_at = datetime.utcnow()
                # session.add(db_payment) # Уже в сессии
                await session.commit()
                logger_webhook_process.info(f"Платеж {yookassa_payment_id} успешно обработан и все операции выполнены.")

            except Exception as e_outer:
                logger_webhook_process.error(f"Общая ошибка при обработке платежа {yookassa_payment_id}: {e_outer}", exc_info=True)
                await session.rollback()
                # TODO: Уведомить администратора.
                # Не возвращаем ошибку Flask, чтобы YooKassa не повторяла, если проблема в нашей логике.
                # Если ошибка была связана с временной недоступностью Marzban, платеж останется pending.
            # finally:
            #     await session.close() # async with AsyncSessionLocal() закроет автоматически

    elif event == "payment.canceled":
        logger_webhook_process.info(f"Платеж {yookassa_payment_id} был ОТМЕНЕН.")
        # Можно обновить статус в нашей БД, если нужно
        async with AsyncSessionLocal() as session:
            try:
                stmt_cancel = select(Payment).where(Payment.yookassa_payment_id == yookassa_payment_id)
                db_payment_cancel = (await session.execute(stmt_cancel)).scalar_one_or_none()
                if db_payment_cancel and db_payment_cancel.status != "succeeded": # Не меняем, если уже успешно обработан
                    db_payment_cancel.status = "canceled"
                    db_payment_cancel.updated_at = datetime.utcnow()
                    await session.commit()
            except Exception as e_cancel:
                logger_webhook_process.error(f"Ошибка при обновлении статуса отмененного платежа {yookassa_payment_id}: {e_cancel}", exc_info=True)
                await session.rollback()
    else:
        logger_webhook_process.info(f"Получено уведомление YooKassa с событием {event} для платежа {yookassa_payment_id}. Статус: {payment_object.get('status')}. Не обрабатывается.")


# --- 5. FLASK ПРИЛОЖЕНИЕ ---
flask_app = Flask(__name__)

@flask_app.route('/yookassa_webhook', methods=['POST'])
def yookassa_webhook_route():
    json_data = request.get_json()
    log.info(f"Webhook received data: {json_data}")

    try:
        # Запускаем нашу асинхронную логику
        asyncio.run(process_yookassa_notification_standalone(json_data)) # Убрали outline_client_webhook
    except Exception as e:
        log.error(f"Critical error in webhook processing: {e}", exc_info=True)
        return "Internal Server Error", 500

    return "OK", 200

if __name__ == '__main__':
    flask_app.run(host='0.0.0.0', port=5001)
