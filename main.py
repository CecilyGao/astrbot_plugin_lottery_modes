# 在 __init__ 中添加回调
self.manager.send_group_message_callback = self._send_group_message

async def _send_group_message(self, group_id: str, message: str):
    """发送群消息（用于自动开奖）"""
    try:
        # 构造 session_id
        platform_id = "aiocqhttp"  # 实际应从配置或事件中获取，这里简化
        # 实际应该从活动对象中获取原始平台信息，但为了演示，我们假设
        # 更好的做法是在 start_activity 时记录平台信息
        session_id = f"{platform_id}:GroupMessage:{group_id}"
        await self.context.send_message(session_id, MessageChain([Plain(message)]))
    except Exception as e:
        logger.error(f"[Lottery] 发送自动开奖结果失败: {e}")

# 新增命令
@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("设置开奖cron")
async def set_draw_cron(self, event: AstrMessageEvent):
    """设置自动开奖 CRON 表达式（仅定时模式）"""
    parts = event.message_str.strip().split(maxsplit=1)
    if len(parts) < 2:
        yield event.plain_result("用法：设置开奖cron <cron表达式>，例如：设置开奖cron 0 12 * * *")
        return
    cron_expr = parts[1].strip()
    ok, msg = self.manager.set_cron(event.get_group_id(), cron_expr)
    yield event.plain_result(msg)

@filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
@filter.permission_type(filter.PermissionType.ADMIN)
@filter.command("取消开奖cron")
async def cancel_draw_cron(self, event: AstrMessageEvent):
    """取消自动开奖"""
    ok, msg = self.manager.cancel_cron(event.get_group_id())
    yield event.plain_result(msg)

# 修改已有的抽奖状态命令，增加 cron 显示
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
        if ov.get("cron_expr"):
            lines.append(f"⏰ 自动开奖 CRON: {ov['cron_expr']}")
        else:
            lines.append("💡 使用“设置开奖cron <表达式>”设置自动开奖，或“开奖”立即开奖")
    lines.append("🎁 奖品剩余：")
    lines += [f"{p['name']}：{p['remaining']}/{p['total']}" for p in data["prize_left"]]
    yield event.plain_result("\n".join(lines))

# 在 terminate 方法中关闭调度器
async def terminate(self):
    self.manager.shutdown()
    logger.info("抽奖插件已终止")