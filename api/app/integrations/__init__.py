"""Outbound integrations with third-party services.

Distinct from `app.events`, which is the CRM's *own* outbound bus to endpoints a
workspace admin registered. This package holds clients for services the product
itself calls — today only Bolna.

Everything here is a seam: a `Protocol` describing the operation, a real HTTP
implementation, and a fake the test suite drives instead. No test in this
repository is allowed to need a real third-party account, and no credential ever
appears in a return value, a log line or an exception message.
"""

from __future__ import annotations
