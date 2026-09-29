"""The control plane, while it moves here from `server.py` (#182).

Every `/_mock` path is still answered by `Handler._control`, through this one
registration. Each step of #182 moves some of its endpoints into modules of
their own, registered ahead of this, and the last step deletes it.
"""
from __future__ import annotations

from typing import List, Tuple

from . import ANY, CONTROL, route, split


@route(ANY, CONTROL, rest=True)
def control(h, rest: List[str]) -> Tuple[int, int]:
    return h._control(h.method, split(h.path)[0], h.query, h.body)
