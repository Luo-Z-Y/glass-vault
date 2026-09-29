"""HTTP services: the trust zones as separate processes.

``gateway_api`` is the gateway zone: the only process that holds the ``Bank``
and the only one that can move money. The agent zone (``agent_api``) reaches it
only over HTTP, sees a read-only snapshot, and has no credential that moves
money: every write needs a passkey assertion from the customer's device over
the exact plan.
"""
