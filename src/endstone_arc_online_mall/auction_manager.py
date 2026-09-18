"""拍卖行：托管、出价、到期结算、离线补发。

与商城的防负债相反：拍卖结算**不校验余额**，
直接经 arc_core.decrease_player_money_by_name 扣款，不足则扣成负数（欠银行）。

物品托管依赖 arc_inventory：
- api_get_inventory_items(player)  列出背包物品
- api_remove_item(player, item_info)  抠走托管物品
- api_give_item_count(player, item_info)  发货（返回实际入包数）
"""

import json
import time

from . import config

STATUS_ACTIVE = "active"
STATUS_SETTLED = "settled"
STATUS_CANCELLED = "cancelled"


class AuctionError(Exception):
    """带玩家可读 message 的业务异常。"""


class AuctionManager:
    def __init__(self, plugin):
        self.plugin = plugin

    # ---------- 创建（托管） ----------

    def create_auction(self, player, item_info: dict, quantity: int,
                       start_price: float, min_increment: float,
                       duration_minutes: int) -> tuple[bool, str]:
        quantity = int(quantity)
        start_price = self._round_money(start_price)
        min_increment = self._round_money(min_increment)
        duration_minutes = int(duration_minutes)

        if not item_info or not str(item_info.get("type") or ""):
            return False, "物品信息无效"
        if quantity < 1:
            return False, "数量至少为 1"
        if start_price < self.plugin.setting_float("AUCTION_MIN_START_PRICE", config.AUCTION_MIN_START_PRICE):
            return False, "起拍价低于服务器下限"
        if min_increment < self.plugin.setting_float("AUCTION_MIN_INCREMENT_FLOOR", config.AUCTION_MIN_INCREMENT_FLOOR):
            return False, "每次最低加价低于服务器下限"
        if duration_minutes < self.plugin.setting_int("AUCTION_MIN_DURATION_MINUTES", config.AUCTION_MIN_DURATION_MINUTES):
            return False, "拍卖时长过短"
        if duration_minutes > self.plugin.setting_int("AUCTION_MAX_DURATION_MINUTES", config.AUCTION_MAX_DURATION_MINUTES):
            return False, "拍卖时长过长"

        core = self.plugin.core_plugin()
        inv = self.plugin.inventory_plugin()
        if core is None or inv is None:
            return False, "弧光核心或背包插件未加载，拍卖不可用"

        xuid = self.plugin.xuid_of(player)
        name = str(getattr(player, "name", "") or "")
        active = self.plugin.db.query_all(
            "SELECT id FROM auctions WHERE seller_xuid=? AND status=?", (xuid, STATUS_ACTIVE))
        limit = self.plugin.setting_int("AUCTION_MAX_ACTIVE_PER_PLAYER", config.AUCTION_MAX_ACTIVE_PER_PLAYER)
        if len(active) >= limit:
            return False, f"你已有 {len(active)} 场拍卖进行中（上限 {limit}）"

        # 托管：从背包扣下物品，不足则原样退回
        escrow = dict(item_info)
        escrow["count"] = quantity
        try:
            removed = int(inv.api_remove_item(player, escrow) or 0)
        except Exception as e:
            return False, f"托管失败：{e}"
        if removed < quantity:
            if removed > 0:
                escrow["count"] = removed
                try:
                    inv.api_give_item_count(player, escrow)
                except Exception:
                    self.plugin.logger.error(
                        f"[ARCOnlineMall] 托管部分扣取后退还失败！玩家={name} 详情={json.dumps(escrow)}")
            return False, f"背包内物品数量不足（找到 {removed} 件）"

        now = int(time.time())
        ok = self.plugin.db.insert("auctions", {
            "item_type": str(item_info.get("type") or ""),
            "item_data": json.dumps(escrow, ensure_ascii=False),
            "quantity": quantity,
            "seller_xuid": xuid,
            "seller_name": name,
            "start_price": start_price,
            "min_increment": min_increment,
            "current_price": None,
            "current_bidder_xuid": None,
            "current_bidder_name": None,
            "status": STATUS_ACTIVE,
            "created_time": now,
            "end_time": now + duration_minutes * 60,
        })
        if not ok:
            try:
                inv.api_give_item_count(player, escrow)
            except Exception:
                self.plugin.logger.error(f"[ARCOnlineMall] 拍卖入库失败且退物失败！玩家={name} 详情={json.dumps(escrow)}")
            return False, "拍卖创建失败，物品已退回"

        self.plugin.broadcast(
            f"[弧光商城] {name} 发起拍卖：{self.item_display(escrow)} ×{quantity}，"
            f"起拍价 {start_price:.2f} 元，每次至少加价 {min_increment:.2f} 元，"
            f"{self.duration_text(duration_minutes)}后截止！输入 /om 前往拍卖行参与竞拍"
        )
        return True, "拍卖已发起"

    # ---------- 出价 ----------

    def required_bid(self, auction: dict, increment: float) -> float:
        """加价额 → 目标出价：基准价（当前最高价，无则起拍价）+ 加价额。"""
        base = float(auction.get("current_price") or 0) or float(auction.get("start_price") or 0)
        return self._round_money(base + max(0.0, float(increment)))

    def place_bid(self, player, auction_id: int, increment: float) -> tuple[bool, str]:
        auction = self.get_auction(auction_id)
        if auction is None or auction.get("status") != STATUS_ACTIVE:
            return False, "拍卖不存在或已结束"
        if int(auction.get("end_time") or 0) <= int(time.time()):
            return False, "拍卖已截止"

        xuid = self.plugin.xuid_of(player)
        if xuid == auction.get("seller_xuid"):
            return False, "不能竞拍自己发起的拍卖"
        if auction.get("current_bidder_xuid") == xuid:
            return False, "你已是当前最高出价者"

        min_increment = float(auction.get("min_increment") or 0)
        if increment < min_increment:
            return False, f"加价低于最低加价 {min_increment:.2f} 元，出价无效"

        amount = self.required_bid(auction, increment)

        # 验资入场：出价 + 已领先的其他拍卖出价 不得超过 总资产（余额+定期存款+领地价值），
        # 保证结算负债有不动产兜底，也防止一资产多押
        if self.plugin.setting_bool("AUCTION_ASSET_VERIFY_ENABLED",
                                    config.AUCTION_ASSET_VERIFY_ENABLED):
            assets = self.plugin.core_assets(player)
            if assets is not None:
                committed = self.plugin.db.query_one(
                    "SELECT COALESCE(SUM(current_price), 0) AS s FROM auctions "
                    "WHERE status=? AND current_bidder_xuid=?", (STATUS_ACTIVE, xuid))
                leading = self._round_money((committed or {}).get("s"))
                if amount + leading > assets["total"]:
                    return False, (
                        f"验资不足：出价 {amount:.2f} 元"
                        + (f"（另已领先出价 {leading:.2f} 元）" if leading > 0 else "")
                        + f"超过你的总资产 {assets['total']:.2f} 元"
                        f"（余额 {assets['balance']:.2f}｜存款 {assets['deposits']:.2f}｜"
                        f"领地 {assets['lands']:.2f}）")

        name = str(getattr(player, "name", "") or "")
        now = int(time.time())
        # 防狙击延时：剩余不足 5 分钟时，每有一次新出价，截止时间顺延 1 分钟
        extend_seconds = 60 if int(auction.get("end_time") or 0) - now < 300 else 0
        if not self.plugin.db.execute(
            "UPDATE auctions SET current_price=?, current_bidder_xuid=?, current_bidder_name=?, "
            "end_time=end_time+? WHERE id=? AND status='active' AND end_time>?",
            (amount, xuid, name, extend_seconds, int(auction_id), now),
        ):
            return False, "出价失败，请稍后再试"

        # 竞价先落库再记历史；领先者被超时提醒
        self.plugin.db.insert("auction_bids", {
            "auction_id": int(auction_id),
            "bidder_xuid": xuid,
            "bidder_name": name,
            "increment": self._round_money(increment),
            "amount": amount,
            "bid_time": now,
        })
        prev = self.plugin.player_by_xuid(str(auction.get("current_bidder_xuid") or ""))
        if prev is not None:
            extra = "，剩余不足5分钟，拍卖已延时1分钟" if extend_seconds else ""
            self.plugin.toast(
                prev, "竞拍提醒",
                f"你在「{self.item_display(json.loads(auction['item_data']))}」的拍卖中被 {name} 超过"
                f"（{amount:.2f} 元）{extra}")
        return True, amount

    # ---------- 结算 ----------

    def settle_due(self) -> None:
        """scheduler 每秒调用；把到期的 active 拍卖逐场结算。"""
        now = int(time.time())
        try:
            due = self.plugin.db.query_all(
                "SELECT * FROM auctions WHERE status=? AND end_time<=?", (STATUS_ACTIVE, now))
        except Exception as e:
            self.plugin.logger.error(f"[ARCOnlineMall] 读取到期拍卖失败: {e}")
            return
        for row in due:
            try:
                self.settle_auction(dict(row))
            except Exception as e:
                self.plugin.logger.error(f"[ARCOnlineMall] 结算拍卖 #{row.get('id')} 异常: {e}")

    def settle_auction(self, auction: dict) -> None:
        auction_id = int(auction["id"])
        item_info = json.loads(auction.get("item_data") or "{}")
        label = f"{self.item_display(item_info)} ×{int(auction.get('quantity') or 0)}"

        winner_xuid = auction.get("current_bidder_xuid")
        if not winner_xuid:
            # 流拍：退托管物品（背包有空位直退，否则走永不过期邮件）
            self._finish(auction_id, STATUS_SETTLED, "流拍")
            self._deliver_return(auction.get("seller_xuid"), auction.get("seller_name"),
                                 item_info, "拍卖流拍（无人出价）")
            self.plugin.broadcast(f"[弧光商城] 拍卖流拍：{label}（无人出价），物品已退还 {auction.get('seller_name')}（背包或邮箱）")
            return

        price = self._round_money(float(auction.get("current_price") or 0))
        winner_name = str(auction.get("current_bidder_name") or "")
        core = self.plugin.core_plugin()
        debt = 0.0

        if core is None:
            # 无经济插件兜底：不能凭空成交，按流拍处理并退物品给卖家
            self._finish(auction_id, STATUS_SETTLED, "结算失败：弧光核心未加载")
            self._deliver_return(auction.get("seller_xuid"), auction.get("seller_name"),
                                 item_info, "拍卖结算失败（弧光核心未加载）")
            self.plugin.logger.error(f"[ARCOnlineMall] 拍卖 #{auction_id} 结算时弧光核心缺失，已流拍退物")
            return

        # 强制扣款：不校验余额，不足直接扣成负数（欠银行）
        try:
            charged = bool(core.decrease_player_money_by_name(winner_name, price, notify=False))
        except Exception as e:
            charged = False
            self.plugin.logger.error(f"[ARCOnlineMall] 拍卖 #{auction_id} 强制扣款异常: {e}")
        if not charged:
            # 扣款写库失败属异常情况：按流拍退物，避免赢家白得物品
            self._finish(auction_id, STATUS_SETTLED, "结算失败：扣款异常")
            self._deliver_return(auction.get("seller_xuid"), auction.get("seller_name"),
                                 item_info, "拍卖结算失败（扣款异常）")
            self.plugin.broadcast(f"[弧光商城] 拍卖结算异常：{label} 已流拍，物品退还卖家")
            return

        try:
            core.increase_player_money_by_name(str(auction.get("seller_name") or ""), price, notify=False)
        except Exception as e:
            self.plugin.logger.error(f"[ARCOnlineMall] 拍卖 #{auction_id} 卖家收款失败: {e}")

        try:
            balance = float(core.api_get_player_money(winner_name) or 0)
            if balance < 0:
                debt = -balance
        except Exception:
            pass

        self._finish(auction_id, STATUS_SETTLED, f"成交价 {price:.2f}")
        self._deliver_won(winner_xuid, winner_name, item_info, price)
        msg = f"[弧光商城] 拍卖成交：{winner_name} 以 {price:.2f} 元拍得 {label}！"
        if debt > 0:
            msg += f"（余额不足，已欠银行 {debt:.2f} 元）"
        self.plugin.broadcast(msg)

    def _finish(self, auction_id: int, status: str, note: str) -> None:
        self.plugin.db.update("auctions", {"status": status, "settle_note": note},
                              "id=?", (auction_id,))

    # ---------- 取消 / 查询 ----------

    def cancel_auction(self, player, auction_id: int) -> tuple[bool, str]:
        auction = self.get_auction(auction_id)
        if auction is None or auction.get("status") != STATUS_ACTIVE:
            return False, "拍卖不存在或已结束"
        if self.plugin.xuid_of(player) != auction.get("seller_xuid"):
            return False, "只有卖家可以取消拍卖"
        if auction.get("current_bidder_xuid"):
            return False, "已有玩家出价，拍卖不可取消"
        self._finish(int(auction_id), STATUS_CANCELLED, "卖家取消")
        item_info = json.loads(auction.get("item_data") or "{}")
        self._deliver_return(auction.get("seller_xuid"), auction.get("seller_name"),
                             item_info, "卖家取消拍卖")
        return True, "拍卖已取消，托管物品已退还（背包或邮箱）"

    def get_auction(self, auction_id: int) -> dict | None:
        row = self.plugin.db.query_one("SELECT * FROM auctions WHERE id=?", (int(auction_id),))
        return dict(row) if row else None

    def list_active(self) -> list[dict]:
        rows = self.plugin.db.query_all(
            "SELECT * FROM auctions WHERE status=? ORDER BY end_time ASC", (STATUS_ACTIVE,))
        return [dict(r) for r in rows]

    def list_mine(self, xuid: str) -> list[dict]:
        rows = self.plugin.db.query_all(
            "SELECT * FROM auctions WHERE seller_xuid=? AND status IN (?,?) ORDER BY id DESC LIMIT 30",
            (xuid, STATUS_ACTIVE, STATUS_SETTLED))
        return [dict(r) for r in rows]

    # ---------- 发货与离线补发 ----------

    def _deliver_return(self, xuid, name, item_info: dict, reason: str) -> None:
        """退回路径（流拍/取消/结算异常）：

        在线且背包有空位 → 直接退背包；否则走 arc_core 邮件（附件含完整 NBT，永不过期）；
        邮件系统不可用时回落到旧的补发队列。
        """
        xuid = str(xuid or "")
        name = str(name or "")
        if not xuid:
            self.plugin.logger.error(
                f"[ARCOnlineMall] 退回失败：无 xuid，详情={json.dumps(item_info, ensure_ascii=False)}")
            return
        player = self.plugin.player_by_xuid(xuid)
        if player is not None and self._has_empty_slot(player):
            self._give(player, item_info)
            return
        if self._mail_return(xuid, name, item_info, reason):
            if player is not None:
                self.plugin.toast(player, "背包已满",
                                  f"退回物品已转入邮箱（30 天内领取），请到邮箱查收")
            return
        self.plugin.db.insert("pending_deliveries", {
            "xuid": xuid,
            "player_name": name,
            "item_data": json.dumps(item_info, ensure_ascii=False),
            "quantity": int(item_info.get("count") or 1),
            "created_time": int(time.time()),
        })
        self.plugin.logger.info(f"[ARCOnlineMall] {name or xuid} 退回物品进入补发队列（邮件系统不可用）")

    def _has_empty_slot(self, player) -> bool:
        """主背包 36 格里是否还有空位（拿不到背包信息时按有空间处理，走原直发路径）。"""
        inv = self.plugin.inventory_plugin()
        if inv is None:
            return True
        try:
            items = inv.api_get_inventory_items(player) or []
        except Exception:
            return True
        used = sum(1 for it in items
                   if isinstance(it, dict) and isinstance(it.get("slot_index"), int))
        return used < 36

    def _mail_item(self, xuid, name, item_info: dict, title: str, content: str,
                   expire_days: float | None = None) -> bool:
        """经 arc_core 邮件发放物品（富物品条目，含 NBT）。expire_days=None 走默认 30 天。"""
        core = self.plugin.core_plugin()
        fn = getattr(core, "api_send_mail", None) if core is not None else None
        if not callable(fn):
            return False
        try:
            ok = bool(fn(
                title=title, content=content,
                player_name=name, xuid=xuid,
                items=[dict(item_info)], sender_name="弧光商城",
                expire_days=expire_days,
            ))
        except Exception as e:
            self.plugin.logger.warning(f"[ARCOnlineMall] 邮件发放异常: {e}")
            return False
        if ok:
            self.plugin.logger.info(f"[ARCOnlineMall] 已向 {name or xuid} 发放邮件：{title}")
        return ok

    def _mail_return(self, xuid, name, item_info: dict, reason: str) -> bool:
        display = self.item_display(item_info)
        count = int(item_info.get("count") or 1)
        return self._mail_item(
            xuid, name, item_info,
            title=f"拍卖退回：{display} ×{count}",
            content=f"{reason}，退回物品已存入本邮件附件（含完整 NBT），"
                    f"请于 30 天内领取，过期未领将作废。",
        )

    def _deliver_won(self, xuid, name, item_info: dict, price: float) -> None:
        """赢家收货：优先走邮件（默认 30 天有效期）；邮件不可用回落直发/补发队列。"""
        xuid = str(xuid or "")
        if xuid and self._mail_item(
                xuid, name, item_info,
                title=f"拍卖成交：{self.item_display(item_info)} ×{int(item_info.get('count') or 1)}",
                content=f"恭喜以 {price:.2f} 元拍得，货款已结算。"
                        f"请于 30 天内领取附件，过期未领将作废。"):
            return
        self._deliver(xuid, name, item_info)

    def _deliver(self, xuid, name, item_info: dict) -> None:
        """在线直接发货；离线进补发队列，PlayerJoinEvent 时补发。"""
        xuid = str(xuid or "")
        name = str(name or "")
        if not xuid:
            self.plugin.logger.error(f"[ARCOnlineMall] 发货失败：无 xuid，详情={json.dumps(item_info, ensure_ascii=False)}")
            return
        player = self.plugin.player_by_xuid(xuid)
        if player is not None:
            self._give(player, item_info)
            return
        self.plugin.db.insert("pending_deliveries", {
            "xuid": xuid,
            "player_name": name,
            "item_data": json.dumps(item_info, ensure_ascii=False),
            "quantity": int(item_info.get("count") or 1),
            "created_time": int(time.time()),
        })
        self.plugin.logger.info(f"[ARCOnlineMall] {name or xuid} 离线，物品进入补发队列")

    def _give(self, player, item_info: dict) -> None:
        inv = self.plugin.inventory_plugin()
        if inv is None:
            self.plugin.logger.error("[ARCOnlineMall] arc_inventory 未加载，无法发货")
            return
        try:
            given = int(inv.api_give_item_count(player, dict(item_info)) or 0)
        except Exception as e:
            given = 0
            self.plugin.logger.error(f"[ARCOnlineMall] 发货异常: {e}")
        expect = int(item_info.get("count") or 1)
        if given < expect:
            remain = dict(item_info)
            remain["count"] = expect - given
            self.plugin.db.insert("pending_deliveries", {
                "xuid": self.plugin.xuid_of(player),
                "player_name": str(getattr(player, "name", "") or ""),
                "item_data": json.dumps(remain, ensure_ascii=False),
                "quantity": expect - given,
                "created_time": int(time.time()),
            })
            self.plugin.toast(player, "背包已满",
                              f"有 {expect - given} 件物品暂时存放在商城，请清理背包后重新进服领取")

    def deliver_pending(self, player) -> None:
        """玩家进服时补发队列中的物品。"""
        xuid = self.plugin.xuid_of(player)
        rows = self.plugin.db.query_all(
            "SELECT * FROM pending_deliveries WHERE xuid=? ORDER BY id ASC", (xuid,))
        for row in rows:
            try:
                item_info = json.loads(row.get("item_data") or "{}")
            except Exception:
                item_info = {}
            if not item_info:
                self.plugin.db.delete("pending_deliveries", "id=?", (row["id"],))
                continue
            self._give(player, item_info)
            self.plugin.db.delete("pending_deliveries", "id=?", (row["id"],))
        if rows:
            self.plugin.toast(player, "弧光商城", "你在商城的离线物品已到账，请查收")

    # ---------- 工具 ----------

    @staticmethod
    def item_display(item_info: dict) -> str:
        return str((item_info or {}).get("name") or (item_info or {}).get("type") or "未知物品")

    @staticmethod
    def duration_text(minutes: int) -> str:
        minutes = int(minutes)
        if minutes % 1440 == 0:
            return f"{minutes // 1440} 天"
        if minutes % 60 == 0:
            return f"{minutes // 60} 小时"
        return f"{minutes} 分钟"

    @staticmethod
    def remaining_text(end_time: int, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        left = max(0, int(end_time) - now)
        hours, seconds = divmod(left, 3600)
        minutes, seconds = divmod(seconds, 60)
        if hours:
            return f"{hours}小时{minutes:02d}分"
        if minutes:
            return f"{minutes}分{seconds:02d}秒"
        return f"{seconds}秒"

    @staticmethod
    def _round_money(value) -> float:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return 0.0
