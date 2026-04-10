from astrbot.api.star import Context, Star


class RocketChatAdapterPlugin(Star):
    """
    Rocket.Chat 消息平台适配器插件入口。

    本插件以"平台适配器"形式接入 AstrBot，不提供对话指令，
    仅在初始化时导入适配器模块以触发 @register_platform_adapter
    装饰器完成注册。注册完成后，用户可在 AstrBot WebUI 的
    「平台」页面中添加 rocket_chat 类型的平台实例。
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        # 导入适配器模块，触发 @register_platform_adapter 装饰器注册
        # 注意：不要删除此行！此“导入即注册”的隐式行为是框架要求的适配器加载方式。
        from .rocketchat_adapter import RocketChatAdapter  # noqa: F401

    async def initialize(self) -> None:
        """插件异步初始化（可选）。"""

    async def terminate(self) -> None:
        """插件销毁（可选）。"""
