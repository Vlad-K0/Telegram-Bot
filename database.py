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
    print("КРИТИЧЕСКАЯ ОШИБКА: Не все переменные для подключения к БД установлены в .env файле (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD).")
    # exit(1) # Можно завершить программу, если БД критична для старта

DATABASE_URL = f"postgresql+psycopg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

async_engine = create_async_engine(DATABASE_URL, echo=False)
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

    def __repr__(self):
        return f"<User(telegram_id={self.telegram_id}, username={self.username})>"

class Payment(Base): # Модель для платежей ЮKassa
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True) # Наш внутренний ID платежа
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    yookassa_payment_id = Column(String, unique=True, index=True, nullable=False) # ID платежа в ЮKassa
    amount = Column(Numeric(10, 2), nullable=False) # Сумма платежа, например, 100.00
    currency = Column(String(3), nullable=False, default="RUB") # Валюта, например, RUB
    status = Column(String(30), nullable=False, default="pending") # pending, waiting_for_capture, succeeded, canceled
    description = Column(String, nullable=True)
    # Поле 'metadata' было переименовано в 'additional_data' из-за резервирования SQLAlchemy
    additional_data = Column(String, nullable=True) # Для хранения информации (ранее metadata)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="payments")
    outline_key_association = relationship("OutlineKey", back_populates="payment", uselist=False, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Payment(id={self.id}, yookassa_payment_id={self.yookassa_payment_id}, status={self.status})>"

class OutlineKey(Base):
    __tablename__ = "outline_keys"
    id = Column(Integer, primary_key=True, index=True)
    outline_id_on_server = Column(String, nullable=False, index=True)
    access_url = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, default=lambda: datetime.utcnow() + timedelta(days=30)) # По умолчанию 30 дней, будет перезаписано при платной подписке
    is_active = Column(Boolean, default=True)
    
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, unique=True) # Связь с платежом

    user = relationship("User", back_populates="outline_keys")
    payment = relationship("Payment", back_populates="outline_key_association")

    def __repr__(self):
        return f"<OutlineKey(outline_id_on_server={self.outline_id_on_server}, user_id={self.user_id}, active={self.is_active})>"

# --- Конец моделей ---

async def create_db_tables():
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD]):
        print("Пропуск создания таблиц БД: конфигурация БД неполная.")
        return
    async with async_engine.begin() as conn:
        # При изменении структуры моделей (например, добавление/удаление колонок или изменение их типов),
        # Base.metadata.create_all не обновит существующие таблицы.
        # Для разработки, если вы готовы потерять данные, можно раскомментировать следующую строку
        # для удаления и пересоздания всех таблиц при каждом запуске.
        # ВНИМАНИЕ: это удалит все данные!
        # await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    print("Таблицы базы данных проверены/созданы (если конфигурация БД была полной).")

async def get_async_session() -> AsyncSession:
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASSWORD]):
        raise ConnectionError("Конфигурация базы данных неполная. Невозможно создать сессию.")
    async with AsyncSessionLocal() as session:
        yield session
