import random
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from enum import Enum
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
from .data import LotteryPersistence


class PrizeLevel(Enum):
    SPECIAL = "特等奖"
    FIRST = "一等奖"
    SECOND = "二等奖"
    THIRD = "三等奖"
    PARTICIPATE = "参与奖"
    NONE = "未中奖"

    @property
    def emoji(self) -> str:
        return {
            PrizeLevel.SPECIAL: "🎊",
            PrizeLevel.FIRST: "🥇",
            PrizeLevel.SECOND: "🥈",
            PrizeLevel.THIRD: "🥉",
            PrizeLevel.PARTICIPATE: "🎁",
            PrizeLevel.NONE: "😢",
        }[self]

    @classmethod
    def from_name(cls, name: str) -> "PrizeLevel | None":
        for lvl in cls:
            if lvl.value == name:
                return lvl
        return None


class LotteryActivity:
    def __init__(self, group_id: str, template: dict[PrizeLevel, dict], mode: str = "instant"):
        self.group_id = group_id
        self.mode = mode                     # 'instant' or 'scheduled'
        self.is_active = False
        self.is_drawn = False                # 定时模式下是否已开奖
        self.cron_expr: Optional[str] = None # 自动开奖的 CRON 表达式（仅定时模式）
        self.job_id: Optional[str] = None    # 调度任务 ID
        self.created_at = datetime.now().isoformat()
        self.participants: dict[str, str] = {}   # user_id -> nickname
        self.winners: dict[str, str] = {}        # user_id -> prize_level
        # 复制模板（含名称）
        self.prize_config = {
            lvl: {
                "probability": cfg["probability"],
                "count": cfg["count"],
                "remaining": cfg["count"],
                "name": cfg["name"],
            }
            for lvl, cfg in template.items()
        }

    def add_participant(self, user_id: str, nickname: str) -> bool:
        if user_id not in self.participants:
            self.participants[user_id] = nickname
            return True
        return False

    def has_participated(self, user_id: str) -> bool:
        return user_id in self.participants

    def add_winner(self, user_id: str, prize_level: PrizeLevel):
        self.winners[user_id] = prize_level.value

    def to_dict(self) -> dict:
        return {
            "group_id": self.group_id,
            "mode": self.mode,
            "is_active": self.is_active,
            "is_drawn": self.is_drawn,
            "cron_expr": self.cron_expr,
            "job_id": self.job_id,
            "created_at": self.created_at,
            "participants": self.participants,
            "winners": self.winners,
            "prize_config": {lvl.name: cfg for lvl, cfg in self.prize_config.items()},
        }

    @classmethod
    def from_dict(cls, data: dict, template: dict[PrizeLevel, dict]) -> "LotteryActivity":
        activity = cls(data["group_id"], template, data.get("mode", "instant"))
        activity.is_active = data["is_active"]
        activity.is_drawn = data.get("is_drawn", False)
        activity.cron_expr = data.get("cron_expr")
        activity.job_id = data.get("job_id")
        activity.created_at = data["created_at"]
        activity.participants = data["participants"]
        activity.winners = data["winners"]

        saved_config: dict[str, dict] = data.get("prize_config", {})
        for lvl_name, cfg in saved_config.items():
            try:
                lvl = PrizeLevel[lvl_name]
                if lvl in activity.prize_config:
                    activity.prize_config[lvl] = {
                        "probability": cfg["probability"],
                        "count": cfg["count"],
                        "remaining": cfg["remaining"],
                        "name": cfg["name"],
                    }
            except KeyError:
                logger.warning(f"[LotteryActivity] 忽略未知奖项等级: {lvl_name}")
        return activity


class LotteryManager:
    def __init__(self, persistence: LotteryPersistence, config: AstrBotConfig):
        self.activities: Dict[str, LotteryActivity] = {}
        prize_config = config["default_prize_config"]
        self.template = {PrizeLevel[k.upper()]: v for k, v in prize_config.items()}
        self.default_mode = config.get("lottery_mode", "instant")
        self.persistence = persistence
        self.scheduler = AsyncIOScheduler()
        self.scheduler.start()
        self.persistence.load(self)
        self._restore_scheduled_jobs()

    def _restore_scheduled_jobs(self):
        """恢复未开奖的定时活动的 CRON 调度"""
        for group_id, act in self.activities.items():
            if (act.mode == "scheduled" and act.is_active and not act.is_drawn
                    and act.cron_expr):
                self._schedule_draw(act)

    def _schedule_draw(self, activity: LotteryActivity):
        """根据活动的 cron_expr 添加调度任务"""
        if not activity.cron_expr or activity.mode != "scheduled":
            return
        # 取消旧任务
        self._cancel_draw_job(activity)
        # 创建新任务
        job_id = f"lottery_draw_{activity.group_id}"
        try:
            trigger = CronTrigger.from_crontab(activity.cron_expr)
            job = self.scheduler.add_job(
                self._auto_draw,
                trigger=trigger,
                id=job_id,
                args=[activity.group_id],
                replace_existing=True,
            )
            activity.job_id = job_id
            logger.info(f"[Lottery] 群 {activity.group_id} 已设置自动开奖: {activity.cron_expr}")
        except Exception as e:
            logger.error(f"[Lottery] 设置自动开奖失败: {e}")
            activity.cron_expr = None
            activity.job_id = None

    def _cancel_draw_job(self, activity: LotteryActivity):
        """取消活动的调度任务"""
        if activity.job_id:
            try:
                self.scheduler.remove_job(activity.job_id)
            except Exception:
                pass
            activity.job_id = None

    async def _auto_draw(self, group_id: str):
        """定时开奖的回调函数"""
        activity = self.activities.get(group_id)
        if not activity or not activity.is_active or activity.is_drawn:
            return
        logger.info(f"[Lottery] 定时开奖触发: 群 {group_id}")
        success, msg, _ = self.perform_draw(group_id)
        if success:
            # 注意：这里需要发送消息到群，需要外部传入发送函数
            # 我们通过回调属性来发送，由 main.py 注入
            if hasattr(self, 'send_group_message_callback'):
                await self.send_group_message_callback(group_id, msg)
        else:
            logger.warning(f"[Lottery] 自动开奖失败: {msg}")

    def set_cron(self, group_id: str, cron_expr: str) -> Tuple[bool, str]:
        """设置定时开奖的 CRON 表达式（仅限定时模式且活动未开奖）"""
        activity = self.activities.get(group_id)
        if not activity:
            return False, "当前群没有抽奖活动"
        if not activity.is_active:
            return False, "抽奖活动未开启"
        if activity.mode != "scheduled":
            return False, "当前模式不是定时开奖模式"
        if activity.is_drawn:
            return False, "活动已开奖，无法修改"
        try:
            # 验证 CRON 表达式
            CronTrigger.from_crontab(cron_expr)
        except Exception as e:
            return False, f"CRON 表达式无效: {e}"
        activity.cron_expr = cron_expr
        self._schedule_draw(activity)
        self.persistence.save(self)
        return True, f"已设置自动开奖 CRON: {cron_expr}"

    def cancel_cron(self, group_id: str) -> Tuple[bool, str]:
        """取消自动开奖"""
        activity = self.activities.get(group_id)
        if not activity:
            return False, "当前群没有抽奖活动"
        if activity.mode != "scheduled":
            return False, "当前模式不是定时开奖模式"
        if not activity.cron_expr:
            return False, "未设置自动开奖"
        self._cancel_draw_job(activity)
        activity.cron_expr = None
        self.persistence.save(self)
        return True, "已取消自动开奖"

    def start_activity(self, group_id: str, mode: str = None) -> Tuple[bool, str]:
        """开启抽奖活动，mode可选 'instant' 或 'scheduled'"""
        if group_id in self.activities and self.activities[group_id].is_active:
            return False, "该群已有进行中的抽奖活动"
        if mode is None:
            mode = self.default_mode
        if mode not in ("instant", "scheduled"):
            return False, "无效的模式，请使用 instant 或 scheduled"
        activity = LotteryActivity(group_id, self.template, mode)
        activity.is_active = True
        self.activities[group_id] = activity
        self.persistence.save(self)
        mode_name = "即时开奖" if mode == "instant" else "定时开奖"
        return True, f"抽奖活动已开启，模式：{mode_name}"

    def draw_lottery(self, group_id: str, user_id: str, nickname: str) -> Tuple[str, Optional[PrizeLevel]]:
        """抽奖入口：即时模式立即抽奖，定时模式只报名"""
        if group_id not in self.activities:
            return "该群没有抽奖活动", None
        activity = self.activities[group_id]
        if not activity.is_active:
            return "抽奖活动未开启", None
        if activity.is_drawn:
            return "抽奖已结束", None

        if activity.mode == "scheduled":
            if activity.has_participated(user_id):
                return "您已经报名过了", None
            activity.add_participant(user_id, nickname)
            self.persistence.save(self)
            return f"报名成功！当前共{len(activity.participants)}人参与，等待开奖", None
        else:
            if activity.has_participated(user_id):
                return "您已经参与过本次抽奖", None
            activity.add_participant(user_id, nickname)
            prize_level = self._draw_prize(activity)
            if prize_level != PrizeLevel.NONE:
                activity.add_winner(user_id, prize_level)
                self.persistence.save(self)
                return f"恭喜您中了{prize_level.value}", prize_level
            else:
                self.persistence.save(self)
                return "很遗憾，您未中奖", PrizeLevel.NONE

    def _draw_prize(self, activity: LotteryActivity) -> PrizeLevel:
        rand = random.random()
        cum = 0.0
        for lvl, cfg in sorted(activity.prize_config.items(), key=lambda x: x[1]["probability"]):
            if cfg["remaining"] > 0:
                cum += cfg["probability"]
                if rand <= cum:
                    cfg["remaining"] -= 1
                    return lvl
        return PrizeLevel.NONE

    def perform_draw(self, group_id: str) -> Tuple[bool, str, dict]:
        """
        执行定时开奖（按等级顺序随机抽取，每人最多中一奖）
        返回 (是否成功, 结果消息, 中奖字典)
        """
        activity = self.activities.get(group_id)
        if not activity or not activity.is_active:
            return False, "没有进行中的抽奖活动", {}
        if activity.is_drawn:
            return False, "已经开奖过了", {}
        if activity.mode != "scheduled":
            return False, "当前模式不是定时开奖模式", {}

        participants = list(activity.participants.keys())
        if not participants:
            return False, "没有参与者，无法开奖", {}

        remaining = participants.copy()
        winners: dict[str, PrizeLevel] = {}

        priority_order = [
            PrizeLevel.SPECIAL,
            PrizeLevel.FIRST,
            PrizeLevel.SECOND,
            PrizeLevel.THIRD,
            PrizeLevel.PARTICIPATE,
        ]
        for lvl in priority_order:
            cfg = activity.prize_config.get(lvl)
            if not cfg or cfg["remaining"] <= 0 or not remaining:
                continue
            draw_count = min(cfg["remaining"], len(remaining))
            if draw_count > 0:
                selected = random.sample(remaining, draw_count)
                for uid in selected:
                    winners[uid] = lvl
                    remaining.remove(uid)
                cfg["remaining"] -= draw_count

        for uid, lvl in winners.items():
            activity.winners[uid] = lvl.value
        activity.is_drawn = True
        activity.is_active = False
        # 开奖后取消定时任务
        self._cancel_draw_job(activity)
        self.persistence.save(self)

        if not winners:
            return True, "很遗憾，没有人中奖。", {}
        lines = ["🎉 开奖结果 🎉"]
        grouped = {}
        for uid, lvl in winners.items():
            grouped.setdefault(lvl.value, []).append(uid)
        for lvl_name in [lvl.value for lvl in priority_order if lvl.value in grouped]:
            nicknames = [activity.participants.get(uid, uid) for uid in grouped[lvl_name]]
            lines.append(f"{lvl_name}：{'、'.join(nicknames)}")
        return True, "\n".join(lines), {uid: lvl for uid, lvl in winners.items()}

    def stop_activity(self, group_id: str) -> Tuple[bool, str]:
        if group_id not in self.activities:
            return False, "该群没有抽奖活动"
        activity = self.activities[group_id]
        if not activity.is_active:
            return False, "抽奖活动已经停止"
        activity.is_active = False
        # 停止活动时取消定时任务
        self._cancel_draw_job(activity)
        self.persistence.save(self)
        return True, "抽奖活动已停止"

    def delete_activity(self, group_id: str) -> bool:
        if group_id not in self.activities:
            return False
        activity = self.activities[group_id]
        self._cancel_draw_job(activity)
        del self.activities[group_id]
        self.persistence.save(self)
        return True

    def set_prize_config(self, group_id: str, prize_level: PrizeLevel, probability: float, count: int) -> bool:
        activity = self.activities.get(group_id)
        if not activity or not activity.is_active:
            return False
        activity.prize_config[prize_level] = {
            "probability": probability,
            "count": count,
            "remaining": count,
            "name": activity.prize_config[prize_level]["name"],
        }
        self.persistence.save(self)
        return True

    def get_status_and_winners(self, group_id: str) -> Optional[dict]:
        activity = self.activities.get(group_id)
        if not activity:
            return None
        overview = {
            "active": activity.is_active,
            "mode": activity.mode,
            "is_drawn": activity.is_drawn,
            "participants": len(activity.participants),
            "winners": len(activity.winners),
            "cron_expr": activity.cron_expr,
        }
        prize_left = [
            {
                "level": lvl.value,
                "name": cfg["name"],
                "remaining": cfg["remaining"],
                "total": cfg["count"],
            }
            for lvl, cfg in activity.prize_config.items()
            if cfg["probability"] > 0
        ]
        winners_by_lvl = {}
        for uid, lvl_name in activity.winners.items():
            winners_by_lvl.setdefault(lvl_name, []).append(uid)
        return {
            "overview": overview,
            "prize_left": prize_left,
            "winners_by_lvl": winners_by_lvl,
        }

    def shutdown(self):
        """关闭调度器，插件终止时调用"""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)