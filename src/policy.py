"""Which uplink to prefer: Wi-Fi when it works, cellular when Wi-Fi keeps failing.

Pure logic (no I/O) so it can be tested with a fake clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

WIFI = "wifi"
CELLULAR = "cellular"
NONE = "none"


@dataclass
class Decision:
    preferred: str  # interface the default route should prefer: wifi | cellular
    active: str  # interface actually carrying traffic now: wifi | cellular | none
    changed: bool  # preferred changed on this step (routing must be updated)


class UplinkPolicy:
    """Hysteresis between Wi-Fi and cellular.

    - Wi-Fi is preferred. After `wifi_down_threshold` consecutive failed Wi-Fi probes, switch to
      cellular if cellular works.
    - On cellular, switch back once Wi-Fi has probed good continuously for
      `wifi_up_stable_seconds` (avoids flapping on a marginal network), or immediately if
      cellular fails while Wi-Fi works.
    """

    def __init__(
        self,
        wifi_down_threshold: int = 3,
        wifi_up_stable_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        has_cellular: bool = True,
    ):
        self.wifi_down_threshold = max(1, wifi_down_threshold)
        self.wifi_up_stable_seconds = wifi_up_stable_seconds
        self.clock = clock
        self.has_cellular = has_cellular

        self.preferred = WIFI
        self.active = NONE
        self.active_since = clock()
        self._wifi_failures = 0
        self._wifi_good_since: Optional[float] = None

    def step(self, wifi_ok: bool, cellular_ok: bool) -> Decision:
        now = self.clock()
        cellular_ok = cellular_ok and self.has_cellular

        if wifi_ok:
            self._wifi_failures = 0
            if self._wifi_good_since is None:
                self._wifi_good_since = now
        else:
            self._wifi_failures += 1
            self._wifi_good_since = None

        previous = self.preferred
        if self.preferred == WIFI:
            if self._wifi_failures >= self.wifi_down_threshold and cellular_ok:
                self.preferred = CELLULAR
        else:
            wifi_stable = self._wifi_good_since is not None and now - self._wifi_good_since >= self.wifi_up_stable_seconds
            if wifi_stable or (wifi_ok and not cellular_ok) or not self.has_cellular:
                self.preferred = WIFI

        if self.preferred == WIFI:
            active = WIFI if wifi_ok else (CELLULAR if cellular_ok else NONE)
        else:
            active = CELLULAR if cellular_ok else (WIFI if wifi_ok else NONE)

        if active != self.active:
            self.active = active
            self.active_since = now

        return Decision(preferred=self.preferred, active=active, changed=self.preferred != previous)
