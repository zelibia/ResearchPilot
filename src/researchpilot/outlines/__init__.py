"""P0-05 versioned outline backend."""

from .contracts import OUTLINE_PROTOCOL_VERSION, ActorContext, OutlineEditCommand
from .errors import OutlineError
from .service import OutlineApplicationService

__all__ = [
    "OUTLINE_PROTOCOL_VERSION",
    "ActorContext",
    "OutlineApplicationService",
    "OutlineEditCommand",
    "OutlineError",
]
