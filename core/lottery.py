import random
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from enum import Enum
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
        self.scheduled_time: Optional[str] = None  # ISO格式，仅定时模式有效
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
            "scheduled_time": self.scheduled_time,
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
        activity.scheduled_time = data.get("scheduled_time")
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
        self.persistence.load(self)

    def start_activity(self, group_id: str, mode: str = None) -> Tuple[bool, str]:
        """开启抽奖活动，mode可选 'instant' 或 'scheduled'，未指定时使用默认模式"""
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
            # 定时模式：只报名
            if activity.has_participated(user_id):
                return "您已经报名过了", None
            activity.add_participant(user_id, nickname)
            self.persistence.save(self)
            return f"报名成功！当前共{len(activity.participants)}人参与，等待开奖", None
        else:
            # 即时模式：原逻辑
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
        返回 (是否成功, 结果消息, 中奖字典 {user_id: prize_level})
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

        # 剩余参与者列表（用于抽奖，每人最多中一个）
        remaining = participants.copy()
        winners: dict[str, PrizeLevel] = {}

        # 按等级优先级从高到低抽取（SPECIAL, FIRST, SECOND, THIRD, PARTICIPATE）
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

        # 记录中奖结果
        for uid, lvl in winners.items():
            activity.winners[uid] = lvl.value
        activity.is_drawn = True
        activity.is_active = False  # 开奖后活动结束
        self.persistence.save(self)

        # 构建消息
        if not winners:
            return True, "很遗憾，没有人中奖。", {}
        lines = ["🎉 开奖结果 🎉"]
        # 按等级分组显示
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
        self.persistence.save(self)
        return True, "抽奖活动已停止"

    def delete_activity(self, group_id: str) -> bool:
        if group_id not in self.activities:
            return False
        del self.activities[group_id]
        self.persistence.save(self)
        return True

    def set_scheduled_time(self, group_id: str, dt: datetime) -> bool:
        """设置定时开奖时间（仅定时模式有效）"""
        activity = self.activities.get(group_id)
        if not activity or not activity.is_active or activity.mode != "scheduled":
            return False
        if activity.is_drawn:
            return False
        activity.scheduled_time = dt.isoformat()
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