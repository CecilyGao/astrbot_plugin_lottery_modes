from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools
from .utils import get_nickname
from .core.lottery import LotteryManager, LotteryPersistence, PrizeLevel
import re


@register("astrbot_plugin_lottery_modes", "Zhalslar", "群聊抽奖插件（支持即时/定时+CRON自动开奖）", "1.2.0")
class LotteryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.lottery_data_file = (
            StarTools.get_data_dir("astrbot_plugin_lottery_modes") / "lottery_data.json"
        )
        self.persistence = LotteryPersistence(str(self.lottery_data_file))
        self.manager = LotteryManager(self.persistence, config)

        # 注入发送消息的回调，使用 session_origin 直接发送
        self.manager.send_group_message_callback = self._send_message_by_origin

    async def _send_message(self, event: AstrMessageEvent, msg: str):
        """发送纯文本消息，不添加回复引用"""
        chain = MessageChain([Plain(msg)])
        await event.send(chain, reply_to_message_id=None)

    async def _send_message_by_origin(self, session_origin: str, message: str):
        """通过 unified_msg_origin 发送消息"""
        try:
            await self.context.send_message(session_origin, MessageChain([Plain(message)]))
        except Exception as e:
            logger.error(f"[Lottery] 发送消息失败 origin={session_origin}: {e}")

    # ======================== 原有命令 ========================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启抽奖")
    async def start_lottery(self, event: AstrMessageEvent):
        msg_str = event.message_str.strip()
        mode = None
        if "定时" in msg_str:
            mode = "scheduled"
        elif "即时" in msg_str:
            mode = "instant"
        # 传入当前事件的 unified_msg_origin，用于自动开奖时发送消息
        ok, msg = self.manager.start_activity(event.get_group_id(), mode, event.unified_msg_origin)
        await self._send_message(event, msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽")
    async def draw_lottery(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        nickname = await get_nickname(event, user_id)
        msg, prize_level = self.manager.draw_lottery(group_id, user_id, nickname)

        if prize_level is None:
            await self._send_message(event, msg)
            return
        activity = self.manager.activities.get(group_id)
        if not activity or prize_level not in activity.prize_config:
            await self._send_message(event, msg)
            return
        prize_name = activity.prize_config[prize_level]["name"]
        await self._send_message(event, f"{prize_level.emoji} {msg}: {prize_name}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置奖项")
    async def set_prize(self, event: AstrMessageEvent):
        m = re.match(
            r"设置奖项\s+(特等奖|一等奖|二等奖|三等奖)\s+(\d*\.?\d+)\s+(\d+)",
            event.message_str,
        )
        if not m:
            await self._send_message(event, "格式错误\n正确示例：设置奖项 特等奖 0.01 1")
            return

        prize_name, prob, count = m.group(1), float(m.group(2)), int(m.group(3))
        if not (0 <= prob <= 1) or count <= 0:
            await self._send_message(event, "概率须在 0-1 之间，数量须为正整数")
            return

        lvl = PrizeLevel.from_name(prize_name)
        if not lvl:
            await self._send_message(event, f"未知的奖项等级：{prize_name}")
            return

        ok = self.manager.set_prize_config(event.get_group_id(), lvl, prob, count)
        if not ok:
            await self._send_message(event, "当前群没有进行中的抽奖活动")
            return

        await self._send_message(
            event,
            f"{lvl.emoji} 已设置 {prize_name}：\n"
            f"中奖概率：{prob * 100:.1f} %\n"
            f"奖品数量：{count} 个"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭抽奖")
    async def stop_lottery(self, event: AstrMessageEvent):
        _, msg = self.manager.stop_activity(event.get_group_id())
        await self._send_message(event, msg)

    @filter.command("重置抽奖")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_lottery(self, event: AstrMessageEvent):
        ok = self.manager.delete_activity(event.get_group_id())
        await self._send_message(event, "本群抽奖已清空，可重新开启" if ok else "当前无抽奖可重置")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽奖状态")
    async def lottery_status(self, event: AstrMessageEvent):
        data = self.manager.get_status_and_winners(event.get_group_id())
        if not data:
            await self._send_message(event, "当前群聊没有抽奖活动")
            return

        ov = data["overview"]
        mode_name = "即时开奖" if ov["mode"] == "instant" else "定时开奖"
        status = "进行中" if ov["active"] else "已结束"
        if ov["mode"] == "scheduled" and ov["active"] and not ov["is_drawn"]:
            status = "报名中"
        lines = [
            f"📊 本群抽奖活动 [{mode_name}] {status}",
            f"参与 {ov['participants']} 人　中奖 {ov['winners']} 人",
        ]
        if ov["mode"] == "scheduled" and ov["active"] and not ov["is_drawn"]:
            if ov.get("cron_expr"):
                lines.append(f"⏰ 自动开奖 CRON: {ov['cron_expr']}")
            else:
                lines.append("💡 使用“设置开奖cron <表达式>”设置自动开奖，或“开奖”立即开奖")
        lines.append("🎁 奖品剩余：")
        lines += [f"{p['name']}：{p['remaining']}/{p['total']}" for p in data["prize_left"]]
        await self._send_message(event, "\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("中奖名单")
    async def winner_list(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        activity = self.manager.activities.get(group_id)
        if not activity:
            await self._send_message(event, "当前群聊没有抽奖活动")
            return
        data = self.manager.get_status_and_winners(group_id)
        if not data or not data["winners_by_lvl"]:
            await self._send_message(event, "暂无中奖者" if data else "当前群聊没有抽奖活动")
            return

        lines = ["🏆 中奖名单："]
        for lvl, uids in data["winners_by_lvl"].items():
            user_names = [activity.participants.get(uid, uid) for uid in uids]
            lines.append(f"{lvl}：{'、'.join(user_names)}")
        await self._send_message(event, "\n".join(lines))

    # ======================== 定时模式专用命令 ========================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置开奖cron")
    async def set_draw_cron(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        if len(parts) < 2:
            await self._send_message(event, "用法：设置开奖cron <cron表达式>，例如：设置开奖cron 0 12 * * *")
            return
        cron_expr = parts[1].strip()
        ok, msg = self.manager.set_cron(event.get_group_id(), cron_expr)
        await self._send_message(event, msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消开奖cron")
    async def cancel_draw_cron(self, event: AstrMessageEvent):
        ok, msg = self.manager.cancel_cron(event.get_group_id())
        await self._send_message(event, msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开奖")
    async def draw_now(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        act = self.manager.activities.get(group_id)
        if not act or not act.is_active or act.mode != "scheduled":
            await self._send_message(event, "当前群没有进行中的定时抽奖活动")
            return
        if act.is_drawn:
            await self._send_message(event, "已经开奖过了")
            return
        success, msg, _ = self.manager.perform_draw(group_id)
        if success:
            await self._send_message(event, msg)
        else:
            await self._send_message(event, f"开奖失败：{msg}")

    async def terminate(self):
        """插件终止时关闭调度器"""
        self.manager.shutdown()
        logger.info("抽奖插件已终止")