"""Test suite for Hassgram.

Written against :mod:`unittest` rather than pytest so the suite needs nothing
that running the bot does not already need::

    python3 -m unittest discover -s tests

pytest collects it too, if you prefer its output::

    python3 -m pytest tests

Asynchronous cases derive from :class:`unittest.IsolatedAsyncioTestCase`, which
gives each test its own event loop -- the bot's caches and the module-level
token store are process-global, so tests reset them in ``setUp`` instead.
"""

import logging

# The bot configures root logging at import time and logs warnings on paths the
# suite exercises on purpose (denied access, a missing STT engine). Keep that
# out of the test output without muting the logger itself: assertLogs installs
# its own handler, so the cases that assert on log lines still work.
logging.getLogger("hassgram").addHandler(logging.NullHandler())
logging.getLogger("hassgram").propagate = False
# IsolatedAsyncioTestCase runs the loop in debug mode, which complains about
# any coroutine slower than 100 ms -- the mock-transport HTTP cases qualify.
logging.getLogger("asyncio").setLevel(logging.ERROR)
