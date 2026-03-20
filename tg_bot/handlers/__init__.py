from .commands import router as commands_router
from .ai_confirm import router as ai_confirm_router
from .setups import router as setups_router

__all__ = ("commands_router", "ai_confirm_router", "setups_router")
