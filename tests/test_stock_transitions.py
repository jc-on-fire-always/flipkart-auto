"""Tests for stock state machine transitions, debounce policy, and duplicate suppression."""

from app.models import AvailabilityState
from app.services.stock_service import StockService


def test_out_of_stock_to_in_stock_creates_exactly_one_event(base_settings, db_manager, mock_stock_provider):
    """Scenario 1: OUT_OF_STOCK -> IN_STOCK triggers exactly one restock event."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # 1. First check establishes initial baseline as OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    obs1, event1 = service.execute_stock_check()
    assert obs1.availability == AvailabilityState.OUT_OF_STOCK
    assert event1 is None

    # 2. Transition to IN_STOCK
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK, price=54999.0)
    obs2, event2 = service.execute_stock_check()
    assert obs2.availability == AvailabilityState.IN_STOCK
    assert event2 is not None
    assert event2.current_state == AvailabilityState.IN_STOCK
    assert event2.previous_state == AvailabilityState.OUT_OF_STOCK
    assert event2.price == 54999.0


def test_in_stock_to_in_stock_creates_no_duplicate_event(base_settings, db_manager, mock_stock_provider):
    """Scenario 2: Continued IN_STOCK checks create no duplicate restock events."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # Baseline as OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # Transition to IN_STOCK (fires event)
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK)
    _, event1 = service.execute_stock_check()
    assert event1 is not None

    # Subsequent check remains IN_STOCK (must NOT fire duplicate event)
    _, event2 = service.execute_stock_check()
    assert event2 is None

    _, event3 = service.execute_stock_check()
    assert event3 is None


def test_in_stock_to_out_of_stock_updates_state_without_alert(base_settings, db_manager, mock_stock_provider):
    """Scenario 3: IN_STOCK -> OUT_OF_STOCK updates DB state without firing an alert."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # Baseline OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # Move to IN_STOCK
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK)
    service.execute_stock_check()

    # Move back to OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    obs, event = service.execute_stock_check()

    assert obs.availability == AvailabilityState.OUT_OF_STOCK
    assert event is None

    # Confirm database reflects OUT_OF_STOCK
    product = db_manager.get_product("itm1234567890")
    assert product.last_known_availability == AvailabilityState.OUT_OF_STOCK.value


def test_unknown_availability_never_creates_restock_event(base_settings, db_manager, mock_stock_provider):
    """Scenario 4: UNKNOWN availability is never treated as IN_STOCK and never alerts."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # Baseline OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # Return UNKNOWN
    mock_stock_provider.set_state(AvailabilityState.UNKNOWN)
    obs, event = service.execute_stock_check()

    assert obs.availability == AvailabilityState.UNKNOWN
    assert event is None

    # Prior valid state must be preserved
    product = db_manager.get_product("itm1234567890")
    assert product.last_known_availability == AvailabilityState.OUT_OF_STOCK.value


def test_already_in_stock_on_startup_policy(base_settings, db_manager, mock_stock_provider):
    """Scenario 13: Initial startup when product is already in stock follows config."""
    # With INITIAL_ALERT_ON_STARTUP=False (default)
    base_settings.INITIAL_ALERT_ON_STARTUP = False
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK)
    service1 = StockService(base_settings, db_manager, mock_stock_provider)
    obs1, event1 = service1.execute_stock_check()
    assert obs1.availability == AvailabilityState.IN_STOCK
    assert event1 is None  # Suppressed on initial run

    # Reset product in DB and test with INITIAL_ALERT_ON_STARTUP=True
    with db_manager.session_scope() as s:
        p = s.query(db_manager.get_product("itm1234567890").__class__).filter_by(product_id="itm1234567890").first()
        p.last_known_availability = AvailabilityState.UNKNOWN.value

    base_settings.INITIAL_ALERT_ON_STARTUP = True
    service2 = StockService(base_settings, db_manager, mock_stock_provider)
    obs2, event2 = service2.execute_stock_check()
    assert obs2.availability == AvailabilityState.IN_STOCK
    assert event2 is not None


def test_verification_count_debounces_fluctuations(base_settings, db_manager, mock_stock_provider):
    """Scenario 14: Fluctuations require N consecutive confirmations before alerting."""
    base_settings.VERIFICATION_COUNT = 2
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # Baseline OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # 1st IN_STOCK observation (1/2 verifications - must NOT alert yet)
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK)
    _, event1 = service.execute_stock_check()
    assert event1 is None

    # 2nd consecutive IN_STOCK observation (2/2 verifications - alerts now!)
    _, event2 = service.execute_stock_check()
    assert event2 is not None
    assert event2.current_state == AvailabilityState.IN_STOCK


def test_price_ceiling_suppresses_alert(base_settings, db_manager, mock_stock_provider):
    """Alert is suppressed if item is in stock but listed above MAX_PRODUCT_PRICE."""
    base_settings.MAX_PRODUCT_PRICE = 50000.0
    service = StockService(base_settings, db_manager, mock_stock_provider)

    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # Price 55000.0 exceeds ceiling 50000.0
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK, price=55000.0)
    _, event = service.execute_stock_check()
    assert event is None
