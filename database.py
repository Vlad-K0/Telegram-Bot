import os
from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Boolean, Numeric
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD]):
    print("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные для подключения к БД установлены в .env файле.")

DATABASE_URL = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Настройка для стабильного подключения к БД
async_engine = create_async_engine(DATABASE_URL, echo=False, pool_recycle=1800)

Base = declarative_base()
AsyncSessionLocal = sessionmaker(
    bind=async_engine, class_=AsyncSession, expire_on_commit=False
)

# --- Модели ---
class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    vpn_keys = relationship("VpnKey", back_populates="user")
    payments = relationship("Payment", back_populates="user")

class Payment(Base):
    __tablename__ = "payments"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    yookassa_payment_id = Column(String, unique=True, index=True, nullable=False)
    amount = Column(Numeric(10, 2), nullable=False)
    # --- ВОССТАНОВЛЕННОЕ ПОЛЕ ---
    currency = Column(String(3), nullable=False, default="RUB")
    status = Column(String(30), nullable=False, default="pending")
    description = Column(String, nullable=True)
    additional_data = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="payments")
    # Связь Payment с VpnKey (теперь подпиской Marzban)
    # Можно переименовать outline_key_association для большей ясности, например, в marzban_subscription_association
    marzban_subscription_association = relationship("VpnKey", back_populates="payment", uselist=False)

class VpnKey(Base): # Класс можно переименовать в MarzbanSubscription или UserSubscription для ясности
    __tablename__ = "vpn_keys" # Таблицу тоже можно переименовать, например, в user_subscriptions
    id = Column(Integer, primary_key=True, index=True)

    # Новые поля для Marzban
    marzban_username = Column(String, unique=True, index=True, nullable=False)
    subscription_url = Column(String, nullable=False) # Ссылка-подписка от Marzban

    name = Column(String, nullable=True) # Можно оставить для внутреннего имени или удалить
    is_trial = Column(Boolean, default=False, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, unique=True) # unique=True означает, что один платеж может быть связан только с одной подпиской
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=30))
    is_active = Column(Boolean, default=True)
    
    user = relationship("User", back_populates="vpn_keys")
    # Связь VpnKey с Payment
    # back_populates должен совпадать с именем relationship в Payment
    payment = relationship("Payment", back_populates="marzban_subscription_association")


async def create_db_tables():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Таблицы базы данных проверены/созданы.")

async def get_async_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
