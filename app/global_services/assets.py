from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.decimal_math import ZERO, decimal, price, quantity
from app.models import Asset, AssetInstrument


@dataclass
class OrderValidation:
    valid: bool
    errors: list[str]
    normalized_quantity: Decimal
    normalized_price: Decimal | None


class AssetRegistryService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def instrument(self, exchange: str, symbol: str) -> AssetInstrument | None:
        instrument = self.session.scalar(select(AssetInstrument).where(
            AssetInstrument.exchange == exchange,
            AssetInstrument.exchange_symbol == symbol,
        ))
        if instrument:
            return instrument
        asset = self.session.scalar(select(Asset).where(Asset.symbol == symbol))
        if not asset:
            return None
        return self.session.scalar(select(AssetInstrument).where(
            AssetInstrument.asset_id == asset.id, AssetInstrument.exchange == exchange
        ))

    def validate_order(
        self, exchange: str, symbol: str, raw_quantity: Decimal, raw_price: Decimal | None
    ) -> OrderValidation:
        instrument = self.instrument(exchange, symbol)
        if not instrument:
            return OrderValidation(False, ["UNKNOWN_INSTRUMENT"], ZERO, None)
        errors: list[str] = []
        normalized_quantity = quantity(raw_quantity, instrument.step_size)
        normalized_price = price(raw_price) if raw_price is not None else None
        if instrument.trading_status != "TRADING":
            errors.append("INSTRUMENT_NOT_TRADING")
        if normalized_quantity <= ZERO or normalized_quantity < instrument.min_quantity:
            errors.append("BELOW_MIN_QUANTITY")
        if normalized_quantity != quantity(raw_quantity):
            errors.append("INVALID_QUANTITY_STEP")
        if normalized_price is not None:
            ticks = decimal(normalized_price) / decimal(instrument.tick_size)
            if ticks != ticks.to_integral_value():
                errors.append("INVALID_PRICE_TICK")
            if normalized_quantity * normalized_price < instrument.min_notional:
                errors.append("BELOW_MIN_NOTIONAL")
        return OrderValidation(not errors, errors, normalized_quantity, normalized_price)

    def cross_exchange_map(self, symbol: str) -> dict[str, str]:
        asset = self.session.scalar(select(Asset).where(Asset.symbol == symbol))
        if not asset:
            return {}
        instruments = self.session.scalars(
            select(AssetInstrument).where(AssetInstrument.asset_id == asset.id)
        ).all()
        return {item.exchange: item.exchange_symbol for item in instruments}
