# SPDX-License-Identifier: GPL-3.0-only
"""Per-device views of the one GATT database on the dongle.

Bumble keeps a single attribute database per controller, so every virtual device
(SmartRow clone, PiRower, PiRower HR) would show every service. A fitness app then
binds rower and heart rate to whichever device it connected first (8 Nov: Peloton
took Rower Data from "PiRower HR" and heart rate from "PiRower", and no rowing
data came through). This hides, per connection, the services that don't belong to
the device the central connected to: the connection's own address
(Connection.self_address) says which advertising set it came in on.

How: the server's `attributes` becomes a view while an ATT request from a given
bearer is handled (a context variable carries the bearer into the request's task),
and `get_attribute` refuses hidden handles, so discovery, reads, writes and
subscriptions all see only the device's own services. Notifications are unaffected:
a connection can only subscribe to what it can see.
"""

from __future__ import annotations

import contextvars
from typing import Callable

from bumble import gatt_server

_bearer: contextvars.ContextVar = contextvars.ContextVar("srb_gatt_bearer", default=None)


def connection_of(bearer):
    """The Connection behind an ATT bearer (itself, or an EATT channel's connection)."""
    return bearer if hasattr(bearer, "self_address") else getattr(bearer, "connection", None)


class ViewServer(gatt_server.Server):
    """gatt_server.Server whose attributes are filtered per bearer by `srb_hidden(bearer)`."""

    srb_hidden: Callable | None = None  # bearer -> set of hidden handles (empty: all visible)

    @property
    def attributes(self):
        full = self.__dict__["attributes"]
        bearer = _bearer.get()
        if bearer is None or self.srb_hidden is None:
            return full
        hidden = self.srb_hidden(bearer)
        return [a for a in full if a.handle not in hidden] if hidden else full

    @attributes.setter
    def attributes(self, value):
        self.__dict__["attributes"] = value

    def get_attribute(self, handle: int):
        bearer = _bearer.get()
        if bearer is not None and self.srb_hidden is not None and handle in self.srb_hidden(bearer):
            return None
        return super().get_attribute(handle)

    def on_gatt_pdu(self, bearer, att_pdu) -> None:
        token = _bearer.set(bearer)  # tasks the handler spawns inherit it
        try:
            super().on_gatt_pdu(bearer, att_pdu)
        finally:
            _bearer.reset(token)


def install(server: gatt_server.Server, hidden: Callable) -> None:
    """Turn the device's server into a ViewServer that hides `hidden(bearer)` handles."""
    server.__class__ = ViewServer
    server.srb_hidden = hidden


def handles(service) -> set[int]:
    """Every attribute handle in a service (its declaration to its group end)."""
    return set(range(service.handle, service.end_group_handle + 1))
