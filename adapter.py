"""OnebotAdapter：反向 WS server 适配器（NapCat 拨入，Universal 角色）。

继承 gateway.platforms.base.BasePlatformAdapter；负责连接生命周期、
入站事件分派、门控、_call_api（echo/future 机制）与出站发送全家。
后续步骤（implement.md 步骤 4-6）填充完整实现。
"""
