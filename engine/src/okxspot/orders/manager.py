from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from okxspot.errors import AmbiguousOrderError, OkxSpotError
from okxspot.exchange.base import Exchange
from okxspot.models import OPEN_STATUSES, Order, OrderPurpose, OrderRequest, OrderStatus, StopOrder, StopRequest
from okxspot.orders.journal import OrderJournal
from okxspot.risk.manager import RiskManager
from okxspot.state import StateStore


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)


class OrderManager:
    """Idempotent submits and restart reconciliation by client order id."""

    def __init__(
        self,
        exchange: Exchange,
        state: StateStore,
        journal: OrderJournal | None = None,
        risk: RiskManager | None = None,
    ) -> None:
        self.exchange = exchange
        self.state = state
        self.journal = journal
        self.risk = risk

    def _log(self, event: str, **fields: object) -> None:
        if self.journal is not None:
            self.journal.append(event, **fields)

    def _gate(self, request: OrderRequest, now: datetime | None) -> None:
        if self.risk is None:
            return
        clock = now or datetime.now(UTC)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=UTC)
        notional = request.qty * (request.price or Decimal("0"))
        if notional <= 0:
            notional = request.qty
        decision = self.risk.pretrade(
            inst_id=request.inst_id,
            side=request.side,
            purpose=request.purpose,
            notional=notional,
            now=clock,
            record=True,
        )
        if not decision.allowed:
            raise OkxSpotError(f"pretrade blocked: {decision.reason}")

    def submit(self, request: OrderRequest, now: datetime | None = None) -> Order:
        local = self.state.get_order(request.client_order_id)
        if local is not None and local.status not in {OrderStatus.REJECTED, OrderStatus.CANCELED}:
            self._log("duplicate_suppressed", client_order_id=request.client_order_id, status=local.status)
            return local
        remote = self.exchange.get_order(request.inst_id, request.client_order_id)
        if remote is not None and remote.status not in {OrderStatus.REJECTED, OrderStatus.CANCELED}:
            self.state.upsert_order(remote)
            self._log("queried", client_order_id=request.client_order_id, status=remote.status)
            return remote
        self._gate(request, now)
        order = self._place(request)
        self.state.upsert_order(order)
        self._log("submitted", client_order_id=request.client_order_id, status=order.status)
        return order

    def _place(self, request: OrderRequest) -> Order:
        try:
            return self.exchange.place_order(request)
        except AmbiguousOrderError:
            self._log("ambiguous", client_order_id=request.client_order_id, status="unknown")
            remote = self.exchange.get_order(request.inst_id, request.client_order_id)
            self._log("queried", client_order_id=request.client_order_id, status=getattr(remote, "status", None))
            if remote is not None:
                return remote
            parked = Order(
                inst_id=request.inst_id,
                side=request.side,
                ord_type=request.ord_type,
                qty=request.qty,
                client_order_id=request.client_order_id,
                status=OrderStatus.UNKNOWN,
                price=request.price,
                purpose=request.purpose,
            )
            self.state.upsert_order(parked)
            self._log("parked", client_order_id=request.client_order_id, status=OrderStatus.UNKNOWN)
            return parked

    def place_stop(self, request: StopRequest, now: datetime | None = None) -> StopOrder:
        if self.risk is not None:
            clock = now or datetime.now(UTC)
            if clock.tzinfo is None:
                clock = clock.replace(tzinfo=UTC)
            decision = self.risk.pretrade(
                inst_id=request.inst_id,
                side=__import__("okxspot.models", fromlist=["Side"]).Side.SELL,
                purpose=OrderPurpose.STOP,
                notional=request.qty * request.stop_price,
                now=clock,
                record=True,
            )
            if not decision.allowed:
                raise OkxSpotError(f"pretrade blocked: {decision.reason}")
        for existing in self.exchange.get_open_algos(request.inst_id):
            if existing.client_order_id == request.client_order_id:
                return existing
        try:
            return self.exchange.place_algo_stop(request)
        except AmbiguousOrderError:
            self._log("ambiguous_algo", client_order_id=request.client_order_id, status="unknown")
            for _ in range(3):
                for existing in self.exchange.get_open_algos(request.inst_id):
                    if existing.client_order_id == request.client_order_id:
                        return existing
            raise

    def reconcile(self) -> ReconcileReport:
        """Match the local open set with the exchange.

        Orders that exist only on the exchange are adopted. Local orders that
        the exchange no longer knows about are marked unknown and are not
        re-submitted.
        """
        report = ReconcileReport()
        remote_open = self.exchange.get_open_orders()
        remote_ids = {order.client_order_id for order in remote_open}
        local_open = [order for order in self.state.list_orders() if order.status in OPEN_STATUSES]
        for remote in remote_open:
            prev = self.state.get_order(remote.client_order_id)
            self.state.upsert_order(remote)
            if prev is None:
                report.adopted.append(remote.client_order_id)
                self._log("reconciled", client_order_id=remote.client_order_id, status=remote.status)
            else:
                report.updated.append(remote.client_order_id)
                self._log("reconciled", client_order_id=remote.client_order_id, status=remote.status)
        for local in local_open:
            if local.client_order_id in remote_ids:
                continue
            found = self.exchange.get_order(local.inst_id, local.client_order_id)
            if found is None:
                local.status = OrderStatus.UNKNOWN
                self.state.upsert_order(local)
                report.missing.append(local.client_order_id)
                self._log("mismatch", client_order_id=local.client_order_id, status=OrderStatus.UNKNOWN)
                continue
            self.state.upsert_order(found)
            report.updated.append(local.client_order_id)
            self._log("reconciled", client_order_id=found.client_order_id, status=found.status)
            if found.status is OrderStatus.FILLED:
                report.filled.append(local.client_order_id)
        return report
