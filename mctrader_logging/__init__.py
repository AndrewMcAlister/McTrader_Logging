"""McTrader_Logging — one log sink and one correlation id for every service.

Four data endpoints' worth of observability, without any of them being able to block trading:
the client queues and returns, the service batches and appends.

See README.md for the fire-and-forget contract and client.py for why it is not negotiable.
"""

__version__ = "0.1.0"
