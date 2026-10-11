"""Database engine initialization, connection management, and repository operations."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import (
    AvailabilityState,
    Base,
    NotificationLogRecord,
    NotificationResult,
    ProductRecord,
    RestockEvent,
    RestockEventRecord,
    ServiceHealthRecord,
    StockObservation,
    StockObservationRecord,
)

logger = logging.getLogger(__name__)


def create_db_engine(database_url: str) -> Engine:
    """Create SQLAlchemy engine with appropriate dialect tuning."""
    if database_url.startswith("sqlite"):
        # Ensure target directory exists for sqlite
        db_path = database_url.replace("sqlite:///", "")
        if db_path and db_path != ":memory:":
            path_obj = Path(db_path)
            if path_obj.parent:
                path_obj.parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            echo=False,
        )

        # Enable SQLite Write-Ahead Logging (WAL) and foreign keys for high reliability
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    return create_engine(database_url, pool_pre_ping=True, echo=False)


def init_db(engine: Engine) -> None:
    """Initialize database tables and create initial health record if absent."""
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with SessionLocal() as session:
        health = session.get(ServiceHealthRecord, 1)
        if not health:
            health = ServiceHealthRecord(id=1, updated_at_utc=datetime.now(timezone.utc))
            session.add(health)
            session.commit()
    logger.debug("Database schema verified and initialized.")


class DatabaseManager:
    """Encapsulates session creation and query operations."""

    def __init__(self, database_url: str):
        self.engine = create_db_engine(database_url)
        init_db(self.engine)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """Provide a transactional scope around a series of operations."""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_or_create_product(
        self,
        product_id: str,
        name: str,
        url: str,
        max_price: Optional[float] = None,
    ) -> ProductRecord:
        with self.session_scope() as session:
            product = session.query(ProductRecord).filter_by(product_id=product_id).first()
            if not product:
                product = ProductRecord(
                    product_id=product_id,
                    name=name,
                    url=url,
                    max_price=max_price,
                    last_known_availability=AvailabilityState.UNKNOWN.value,
                    consecutive_in_stock_count=0,
                )
                session.add(product)
                session.flush()
            else:
                # Update metadata if changed
                product.name = name
                product.url = url
                product.max_price = max_price
                session.flush()
            session.expunge(product)
            return product

    def get_product(self, product_id: str) -> Optional[ProductRecord]:
        with self.session_scope() as session:
            product = session.query(ProductRecord).filter_by(product_id=product_id).first()
            if product:
                session.expunge(product)
            return product

    def record_observation(self, obs: StockObservation) -> None:
        """Persist observation record to history and update product last check timestamp."""
        with self.session_scope() as session:
            obs_record = StockObservationRecord(
                product_id=obs.product_id,
                availability=obs.availability.value,
                price=obs.price,
                provider_name=obs.provider_name,
                confidence_score=obs.confidence_score,
                evidence_json=json.dumps(obs.raw_evidence) if obs.raw_evidence else None,
                observed_at_utc=obs.observed_at_utc,
            )
            session.add(obs_record)

            product = session.query(ProductRecord).filter_by(product_id=obs.product_id).first()
            if product:
                product.last_checked_at_utc = obs.observed_at_utc
                product.updated_at_utc = datetime.now(timezone.utc)

    def update_product_state(
        self,
        product_id: str,
        availability: Optional[AvailabilityState] = None,
        consecutive_in_stock_count: Optional[int] = None,
    ) -> None:
        """Explicitly update product stock availability state and verification counter."""
        with self.session_scope() as session:
            product = session.query(ProductRecord).filter_by(product_id=product_id).first()
            if product:
                if availability is not None and availability != AvailabilityState.UNKNOWN:
                    product.last_known_availability = availability.value
                if consecutive_in_stock_count is not None:
                    product.consecutive_in_stock_count = consecutive_in_stock_count
                product.updated_at_utc = datetime.now(timezone.utc)

    def is_in_cooldown(self, product_id: str) -> bool:
        """Check if product is within alert cooldown window."""
        with self.session_scope() as session:
            now = datetime.now(timezone.utc)
            active_event = (
                session.query(RestockEventRecord)
                .filter(
                    RestockEventRecord.product_id == product_id,
                    RestockEventRecord.cooldown_until_utc > now,
                )
                .order_by(RestockEventRecord.detected_at_utc.desc())
                .first()
            )
            return active_event is not None

    def record_restock_event(self, event: RestockEvent, cooldown_seconds: int) -> int:
        """Create and persist a new restock event with cooldown boundary."""
        with self.session_scope() as session:
            cooldown_until = event.detected_at_utc + timedelta(seconds=cooldown_seconds)
            event_record = RestockEventRecord(
                product_id=event.product_id,
                detected_at_utc=event.detected_at_utc,
                price=event.price,
                previous_state=event.previous_state.value,
                current_state=event.current_state.value,
                status=event.status.value,
                cooldown_until_utc=cooldown_until,
            )
            session.add(event_record)
            session.flush()
            event_id = event_record.id
            return event_id

    def record_notification_log(self, restock_event_id: Optional[int], result: NotificationResult) -> None:
        with self.session_scope() as session:
            valid_event_id = None
            if restock_event_id is not None:
                exists = session.query(RestockEventRecord.id).filter_by(id=restock_event_id).first()
                if exists:
                    valid_event_id = restock_event_id

            log_record = NotificationLogRecord(
                restock_event_id=valid_event_id,
                channel=result.channel.value,
                status=result.status.value,
                provider_message_id=result.provider_message_id,
                error_message=result.error_message,
                attempt_count=result.attempt_count,
                created_at_utc=result.timestamp_utc,
            )
            session.add(log_record)

    def update_health(
        self,
        is_success: bool,
        availability: Optional[AvailabilityState] = None,
        notification_success: bool = False,
        error_summary: Optional[str] = None,
        active_provider: Optional[str] = None,
    ) -> None:
        """Update service health tracking."""
        with self.session_scope() as session:
            health = session.get(ServiceHealthRecord, 1)
            if not health:
                health = ServiceHealthRecord(id=1)
                session.add(health)

            now = datetime.now(timezone.utc)
            if is_success:
                health.last_successful_check_utc = now
                health.consecutive_errors = 0
                health.last_error_summary = None
                if availability:
                    health.last_observed_availability = availability.value
            else:
                health.consecutive_errors += 1
                if error_summary:
                    health.last_error_summary = error_summary

            if notification_success:
                health.last_successful_notification_utc = now

            if active_provider:
                health.active_provider = active_provider

            health.updated_at_utc = now

    def get_health_snapshot(self) -> dict:
        """Read health record for inspection without leaking any secrets."""
        with self.session_scope() as session:
            health = session.get(ServiceHealthRecord, 1)
            if not health:
                return {"status": "uninitialized"}
            return {
                "last_successful_check_utc": (
                    health.last_successful_check_utc.isoformat()
                    if health.last_successful_check_utc
                    else None
                ),
                "last_observed_availability": health.last_observed_availability,
                "last_successful_notification_utc": (
                    health.last_successful_notification_utc.isoformat()
                    if health.last_successful_notification_utc
                    else None
                ),
                "consecutive_errors": health.consecutive_errors,
                "last_error_summary": health.last_error_summary,
                "active_provider": health.active_provider,
                "updated_at_utc": health.updated_at_utc.isoformat() if health.updated_at_utc else None,
            }
