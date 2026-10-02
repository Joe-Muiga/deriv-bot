"""
event_bus.py — tiny in-process pub/sub joining the Scout to the edge gate and
the Follower (Oct 2026, Scout+Follower phase).

Topics used:
  "entry"   Scout opened a contract.
  "result"  Scout contract CONFIRMED settled (never published unconfirmed).

Handlers may be plain functions or coroutine functions. publish() never raises
and never blocks the Scout: a broken subscriber is logged and skipped.
"""
import asyncio
import logging
from collections import defaultdict
from typing import Callable, Dict, List

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self):
        self._subs: Dict[str, List[Callable]] = defaultdict(list)

    def subscribe(self, topic: str, handler: Callable) -> None:
        if handler not in self._subs[topic]:
            self._subs[topic].append(handler)

    def clear(self) -> None:
        self._subs.clear()

    def publish(self, topic: str, event: dict) -> None:
        for h in list(self._subs.get(topic, ())):
            try:
                out = h(event)
                if asyncio.iscoroutine(out):
                    try:
                        asyncio.get_running_loop().create_task(out)
                    except RuntimeError:
                        out.close()      # no running loop: cannot await it
            except Exception as exc:
                logger.error(f"EVENT BUS: handler for '{topic}' failed: {exc}")


bus = EventBus()          # the one shared bus for this process
