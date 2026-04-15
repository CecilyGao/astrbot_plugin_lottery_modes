import asyncio
import re
from datetime import datetime
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools
from .utils import get_nickname
from .core.lottery import LotteryManager, LotteryPersistence, PrizeLevel


@register("astrbot_plugin_lottery", "Zhalslar", "群聊抽奖插件（支持即开即中/定时开奖）", "...")
class LotteryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config = config
        self.lottery_data_file = (
            StarTools.get_data_dir("astrbot_plugin_lottery") / "lottery_data.json"
        )
        self.persistence = LotteryPersistence(str(self.lottery_data_file))
        self.manager = LotteryManager(self.persistence, config)
        self.scheduled_tasks: dict[str, asyncio.Task] = {}  # group_id -> task
        self._restore_scheduled_tasks()

    def _restore_scheduled_tasks(self):
        """插件启动时恢复未开奖的定时任务"""
        for group_id, act in self.manager.activities.items():
            if (act.mode == "scheduled" and act.is_active and not act.is_drawn
                    and act.scheduled_time):
                try:
                    dt = datetime.fromisoformat(act.scheduled_time)
                    if dt > datetime.now():
                        self._schedule_draw(group_id, dt)
                    else:
                        # 已过时，立即开奖？或者忽略，让管理员重新设置
                        logger.warning(f"[Lottery] 群 {group_id} 的定时开奖时间已过，请重新设置")
                except Exception as e:
                    logger.error(f"[Lottery] 恢复定时任务失败 {group_id}: {e}")

    def _schedule_draw(self, group_id: str, dt: datetime):
        """创建定时开奖任务"""
        self._cancel_scheduled_draw(group_id)  # 取消旧任务
        delay = (dt - datetime.now()).total_seconds()
        if delay <= 0:
            return
        async def _draw():
            await asyncio.sleep(delay)
            if group_id in self.manager.activities:
                # 注意：这里需要在异步环境中发送消息，使用 context 或者 event 的 reply
                # 由于没有原始 event，我们通过 context 主动发送消息到群
                success, msg, _ = self.manager.perform_draw(group_id)
                if success:
                    # 尝试发送开奖结果到群（需要 context 支持主动发送）
                    await self._send_group_message(group_id, msg)
                else:
                    await self._send_group_message(group_id, f"定时开奖失败：{msg}")
            self.scheduled_tasks.pop(group_id, None)
        task = asyncio.create_task(_draw())
        self.scheduled_tasks[group_id] = task

    def _cancel_scheduled_draw(self, group_id: str):
        """取消群组的定时开奖任务"""
        if group_id in self.scheduled_tasks:
            self.scheduled_tasks[group_id].cancel()
            del self.scheduled_tasks[group_id]

    async def _send_group_message(self, group_id: str, message: str):
        """主动发送群消息（需要适配器支持，简单实现可能通过 context 的 send_message）"""
        # 注意：此方法依赖于 AstrBot 的主动消息能力，若不可用则仅记录日志
        try:
            # 使用 context 的 send_group_message 方法（需要确认是否存在）
            # 这里简单使用 logger 警告，实际请根据框架 API 调整
            logger.info(f"[Lottery] 向群 {group_id} 发送消息：{message}")
            # 如果框架支持，可以调用 self.context.send_group_message(group_id, message)
        except Exception as e:
            logger.error(f"[Lottery] 发送群消息失败: {e}")

    # ======================== 命令 ========================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开启抽奖")
    async def start_lottery(self, event: AstrMessageEvent):
        """开启抽奖活动，可选模式：开启抽奖 [即时/定时]"""
        msg_str = event.message_str.strip()
        mode = None
        if "定时" in msg_str:
            mode = "scheduled"
        elif "即时" in msg_str:
            mode = "instant"
        ok, msg = self.manager.start_activity(event.get_group_id(), mode)
        yield event.plain_result(msg)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽")
    async def draw_lottery(self, event: AstrMessageEvent):
        """参与抽奖（即时模式立即开奖，定时模式报名）"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        nickname = await get_nickname(event, user_id)
        msg, prize_level = self.manager.draw_lottery(group_id, user_id, nickname)

        if prize_level is None:
            yield event.plain_result(msg)
            return
        activity = self.manager.activities.get(group_id)
        if not activity or prize_level not in activity.prize_config:
            yield event.plain_result(msg)
            return
        prize_name = activity.prize_config[prize_level]["name"]
        yield event.plain_result(f"{prize_level.emoji} {msg}: {prize_name}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置奖项")
    async def set_prize(self, event: AstrMessageEvent):
        m = re.match(r"设置奖项\s+(特等奖|一等奖|二等奖|三等奖)\s+(\d*\.?\d+)\s+(\d+)", event.message_str)
        if not m:
            yield event.plain_result("格式错误\n正确示例：设置奖项 特等奖 0.01 1")
            return
        prize_name, prob, count = m.group(1), float(m.group(2)), int(m.group(3))
        if not (0 <= prob <= 1) or count <= 0:
            yield event.plain_result("概率须在 0-1 之间，数量须为正整数")
            return
        lvl = PrizeLevel.from_name(prize_name)
        if not lvl:
            yield event.plain_result(f"未知的奖项等级：{prize_name}")
            return
        ok = self.manager.set_prize_config(event.get_group_id(), lvl, prob, count)
        if not ok:
            yield event.plain_result("当前群没有进行中的抽奖活动")
            return
        yield event.plain_result(
            f"{lvl.emoji} 已设置 {prize_name}：\n"
            f"中奖概率：{prob * 100:.1f} %\n"
            f"奖品数量：{count} 个"
        )

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("关闭抽奖")
    async def stop_lottery(self, event: AstrMessageEvent):
        _, msg = self.manager.stop_activity(event.get_group_id())
        yield event.plain_result(msg)

    @filter.command("重置抽奖")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def reset_lottery(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        self._cancel_scheduled_draw(group_id)
        ok = self.manager.delete_activity(group_id)
        yield event.plain_result("本群抽奖已清空，可重新开启" if ok else "当前无抽奖可重置")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("抽奖状态")
    async def lottery_status(self, event: AstrMessageEvent):
        data = self.manager.get_status_and_winners(event.get_group_id())
        if not data:
            yield event.plain_result("当前群聊没有抽奖活动")
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
            lines.append("💡 使用“设置开奖时间 HH:MM” 或 “开奖” 立即开奖")
        lines.append("🎁 奖品剩余：")
        lines += [f"{p['name']}：{p['remaining']}/{p['total']}" for p in data["prize_left"]]
        yield event.plain_result("\n".join(lines))

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.command("中奖名单")
    async def winner_list(self, event: AstrMessageEvent):
        group_id = event.get_group_id()
        activity = self.manager.activities.get(group_id)
        if not activity:
            yield event.plain_result("当前群聊没有抽奖活动")
            return
        data = self.manager.get_status_and_winners(group_id)
        if not data or not data["winners_by_lvl"]:
            yield event.plain_result("暂无中奖者" if data else "当前群聊没有抽奖活动")
            return
        lines = ["🏆 中奖名单："]
        for lvl, uids in data["winners_by_lvl"].items():
            user_names = [activity.participants.get(uid, uid) for uid in uids]
            lines.append(f"{lvl}：{'、'.join(user_names)}")
        yield event.plain_result("\n".join(lines))

    # ---------- 定时模式专用命令 ----------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("设置开奖时间")
    async def set_draw_time(self, event: AstrMessageEvent):
        """设置定时开奖时间，格式：设置开奖时间 20:00"""
        m = re.search(r"(\d{1,2}):(\d{2})", event.message_str)
        if not m:
            yield event.plain_result("格式错误，请使用：设置开奖时间 20:00")
            return
        hour, minute = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt = dt.replace(day=now.day + 1)  # 设为明天
        group_id = event.get_group_id()
        act = self.manager.activities.get(group_id)
        if not act or not act.is_active or act.mode != "scheduled":
            yield event.plain_result("当前群没有进行中的定时抽奖活动")
            return
        if act.is_drawn:
            yield event.plain_result("活动已开奖，无法设置时间")
            return
        if self.manager.set_scheduled_time(group_id, dt):
            self._schedule_draw(group_id, dt)
            yield event.plain_result(f"已设置开奖时间为 {dt.strftime('%Y-%m-%d %H:%M')}")
        else:
            yield event.plain_result("设置失败，请检查活动状态")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("开奖")
    async def draw_now(self, event: AstrMessageEvent):
        """立即开奖（仅限定时模式）"""
        group_id = event.get_group_id()
        act = self.manager.activities.get(group_id)
        if not act or not act.is_active or act.mode != "scheduled":
            yield event.plain_result("当前群没有进行中的定时抽奖活动")
            return
        if act.is_drawn:
            yield event.plain_result("已经开奖过了")
            return
        success, msg, _ = self.manager.perform_draw(group_id)
        if success:
            self._cancel_scheduled_draw(group_id)
            yield event.plain_result(msg)
        else:
            yield event.plain_result(f"开奖失败：{msg}")

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消开奖")
    async def cancel_draw_time(self, event: AstrMessageEvent):
        """取消已设置的定时开奖"""
        group_id = event.get_group_id()
        act = self.manager.activities.get(group_id)
        if not act or not act.is_active or act.mode != "scheduled":
            yield event.plain_result("当前群没有进行中的定时抽奖活动")
            return
        if act.is_drawn:
            yield event.plain_result("活动已开奖，无法取消")
            return
        act.scheduled_time = None
        self.manager.persistence.save(self.manager)
        self._cancel_scheduled_draw(group_id)
        yield event.plain_result("已取消定时开奖，可使用“开奖”手动开奖")

    async def terminate(self):
        """插件终止时取消所有定时任务"""
        for task in self.scheduled_tasks.values():
            task.cancel()
        self.scheduled_tasks.clear()
        logger.info("抽奖插件已终止")