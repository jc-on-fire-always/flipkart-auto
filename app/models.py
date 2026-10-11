"""Domain models and SQLAlchemy ORM schemas."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class AvailabilityState(str, Enum):
    """Product stock availability states."""
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"

    @property
    def is_purchasable(self) -> bool:
        """Helper to guarantee UNKNOWN is never treated as purchasable."""
        return self == AvailabilityState.IN_STOCK


class NotificationChannel(str, Enum):
    """Available notification dispatch channels."""
    WHATSAPP = "whatsapp"
    EMAIL = "email"


class NotificationStatus(str, Enum):
    """Status of notification delivery attempt."""
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class RestockEventStatus(str, Enum):
    """Lifecycle status of a restock event."""
    DETECTED = "DETECTED"
    NOTIFIED = "NOTIFIED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


# --- Pydantic Data Models ---

class StockObservation(BaseModel):
    """Observation recorded during a stock inspection."""
    product_id: str
    product_name: str
    product_url: str
    availability: AvailabilityState
    observed_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_timestamp: Optional[datetime] = None
    price: Optional[float] = None
    provider_name: str
    confidence_score: float = Field(default=1.0, ge=0.0, le=1.0)
    raw_evidence: dict[str, Any] = Field(default_factory=dict)


class RestockEvent(BaseModel):
    """Event triggered when a product transitions to IN_STOCK."""
    id: Optional[int] = None
    product_id: str
    product_name: str
    product_url: str
    detected_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    price: Optional[float] = None
    previous_state: AvailabilityState
    current_state: AvailabilityState
    status: RestockEventStatus = RestockEventStatus.DETECTED


class NotificationResult(BaseModel):
    """Result of a notification delivery attempt."""
    channel: NotificationChannel
    status: NotificationStatus
    provider_message_id: Optional[str] = None
    error_message: Optional[str] = None
    attempt_count: int = 1
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --- SQLAlchemy ORM Database Records ---

class ProductRecord(Base):
    """Persisted product metadata and current stock state."""
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    url = Column(Text, nullable=False)
    max_price = Column(Float, nullable=True)
    last_known_availability = Column(String(50), default=AvailabilityState.UNKNOWN.value, nullable=False)
    consecutive_in_stock_count = Column(Integer, default=0, nullable=False)
    last_checked_at_utc = Column(DateTime, nullable=True)
    created_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)

    observations = relationship("StockObservationRecord", back_populates="product", cascade="all, delete-orphan")
    restock_events = relationship("RestockEventRecord", back_populates="product", cascade="all, delete-orphan")


class StockObservationRecord(Base):
    """Historical record of every stock check."""
    __tablename__ = "stock_observations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(255), ForeignKey("products.product_id"), nullable=False, index=True)
    availability = Column(String(50), nullable=False)
    price = Column(Float, nullable=True)
    provider_name = Column(String(100), nullable=False)
    confidence_score = Column(Float, default=1.0, nullable=False)
    evidence_json = Column(Text, nullable=True)
    observed_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    product = relationship("ProductRecord", back_populates="observations")


class RestockEventRecord(Base):
    """Restock transitions persisted to prevent duplicate alerts."""
    __tablename__ = "restock_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    product_id = Column(String(255), ForeignKey("products.product_id"), nullable=False, index=True)
    detected_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)
    price = Column(Float, nullable=True)
    previous_state = Column(String(50), nullable=False)
    current_state = Column(String(50), nullable=False)
    status = Column(String(50), default=RestockEventStatus.DETECTED.value, nullable=False)
    cooldown_until_utc = Column(DateTime, nullable=False, index=True)

    product = relationship("ProductRecord", back_populates="restock_events")
    notification_logs = relationship("NotificationLogRecord", back_populates="restock_event", cascade="all, delete-orphan")


class NotificationLogRecord(Base):
    """Log of every notification attempt per channel."""
    __tablename__ = "notification_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    restock_event_id = Column(Integer, ForeignKey("restock_events.id"), nullable=True, index=True)
    channel = Column(String(50), nullable=False)
    status = Column(String(50), nullable=False)
    provider_message_id = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    attempt_count = Column(Integer, default=1, nullable=False)
    created_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False, index=True)

    restock_event = relationship("RestockEventRecord", back_populates="notification_logs")


class ServiceHealthRecord(Base):
    """Singleton health tracker for heartbeat and error status."""
    __tablename__ = "service_health"

    id = Column(Integer, primary_key=True, default=1)
    last_successful_check_utc = Column(DateTime, nullable=True)
    last_observed_availability = Column(String(50), nullable=True)
    last_successful_notification_utc = Column(DateTime, nullable=True)
    consecutive_errors = Column(Integer, default=0, nullable=False)
    last_error_summary = Column(Text, nullable=True)
    active_provider = Column(String(100), nullable=True)
    updated_at_utc = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc), nullable=False)
