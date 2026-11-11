from __future__ import annotations

from dataclasses import dataclass, field

from okxspot.errors import AmbiguousOrderError
from okxspot.exchange.base import Exchange
from okxspot.models import OPEN_STATUSES, Order, OrderRequest, OrderStatus
from okxspot.state import StateStore


@dataclass
class ReconcileReport:
    adopted: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    filled: list[str] = field(default_factory=list)


class OrderManager:
    """Idempotent submits and restart reconciliation by client order id."""

    def __init__(self, exchange: Exchange, state: StateStore) -> None:
        self.exchange = exchange
        self.state = state

    def submit(self, request: OrderRequest) -> Order:
        local = self.state.get_order(request.client_order_id)
        if local is not None:
            return local
        remote = self.exchange.get_order(request.inst_id, request.client_order_id)
        if remote is not None:
            self.state.upsert_order(remote)
            return remote
        order = self._place(request)
        self.state.upsert_order(order)
        return order

    def _place(self, request: OrderRequest) -> Order:
        try:
            return self.exchange.place_order(request)
        except AmbiguousOrderError:
            remote = self.exchange.get_order(request.inst_id, request.client_order_id)
            if remote is not None:
                return remote
            try:
                return self.exchange.place_order(request)
            except AmbiguousOrderError:
                remote = self.exchange.get_order(request.inst_id, request.client_order_id)
                if remote is None:
                    raise
                return remote

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
            else:
                report.updated.append(remote.client_order_id)
        for local in local_open:
            if local.client_order_id in remote_ids:
                continue
            found = self.exchange.get_order(local.inst_id, local.client_order_id)
            if found is None:
                local.status = OrderStatus.UNKNOWN
                self.state.upsert_order(local)
                report.missing.append(local.client_order_id)
                continue
            self.state.upsert_order(found)
            report.updated.append(local.client_order_id)
            if found.status is OrderStatus.FILLED:
                report.filled.append(local.client_order_id)
        return report
