"""limiter.py — shared slowapi Limiter instance.

Lives in its own module rather than main.py so router files can import it
and apply @limiter.limit(...) directly. main.py imports the routers, so the
routers can't import the limiter back out of main.py without a circular
import (SEC-7's actual root cause — see BUGFIXES.md).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
