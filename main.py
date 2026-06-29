from astrbot.api import logger
from astrbot.api.star import Context, Star


class RocketChatAdapterPlugin(Star):
    """
    Rocket.Chat message platform adapter plugin entry.

    This plugin registers as a platform adapter in AstrBot.
    It imports the adapter module on init to trigger the
    @register_platform_adapter decorator for registration.
    Users can then add rocket_chat type platform instances
    in the AstrBot WebUI Platform page.
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        from .rocketchat_adapter import RocketChatAdapter  # noqa: F401

    async def initialize(self) -> None:
        pass

    async def terminate(self) -> None:
        """
        Restart all running Rocket.Chat adapter instances on plugin unload.

        AstrBot plugin reload only calls Star.terminate() and does NOT
        automatically stop running Platform adapter instances.
        Without explicit cleanup here, the old adapter keeps running old code,
        forcing users to manually disconnect and reconnect to apply changes.

        platform_manager.reload(config) will:
        1. terminate_platform(id) - close old WebSocket / HTTP session
        2. load_platform(config) - create a fresh instance with the new class
        """
        pm = self.context.platform_manager
        configs_to_reload = []

        for inst in list(pm.platform_insts):
            if inst.config.get("type") == "rocket_chat":
                configs_to_reload.append(inst.config)

        for config in configs_to_reload:
            try:
                logger.info(
                    "[RocketChat] plugin reloading, restarting adapter id=%s ...",
                    config.get("id", "unknown"),
                )
                reload_fn = getattr(pm, "reload", None)
                if reload_fn:
                    await reload_fn(config)
            except Exception as exc:
                logger.error(
                    "[RocketChat] failed to restart adapter id=%s: %r",
                    config.get("id", "unknown"),
                    exc,
                )
