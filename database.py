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

    outline_keys = relationship("OutlineKey", back_populates="user")
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
    outline_key_association = relationship("OutlineKey", back_populates="payment", uselist=False)

class OutlineKey(Base):
    __tablename__ = "outline_keys"
    id = Column(Integer, primary_key=True, index=True)
    outline_id_on_server = Column(String, nullable=False, index=True)
    access_url = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=30))
    is_active = Column(Boolean, default=True)
    
    user = relationship("User", back_populates="outline_keys")
    payment = relationship("Payment", back_populates="outline_key_association")


async def create_db_tables():
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    print("Таблицы базы данных проверены/созданы.")

async def get_async_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
