"""拍卖行表单 UI：主菜单 → 列表/详情/出价，发起（选物品→填参数→确认托管），我的拍卖。

出价为固定档位：出价 = 基准价 + N × 每次最低加价，N 从 1/5/10 次中选，
不支持自定义金额。基准价 = 当前最高价（无人出价时为起拍价）。
出价受验资上限约束：不得超过 总资产（余额+定期存款+领地价值），
由 AUCTION_ASSET_VERIFY_ENABLED 控制，核心过旧不支持时自动跳过验资。
"""

import json

from endstone.form import ActionForm, Dropdown, Label, ModalForm, Slider, TextInput, MessageForm

from . import config
from .auction_manager import STATUS_ACTIVE


class AuctionMenus:
    """Mixin：挂在 ARCOnlineMallPlugin 上，self 即插件实例。"""

    # ---------- 主菜单 ----------

    def open_auction_main(self, player) -> None:
        if self.core_plugin() is None or self.inventory_plugin() is None:
            form = ActionForm(title="拍卖行", content="拍卖行依赖 弧光核心 与 arc_inventory，当前不可用。")
            form.add_button("返回", on_click=lambda p: self.open_mall_main(p))
            player.send_form(form)
            return
        active = self.auction.list_active()
        form = ActionForm(
            title="弧光拍卖行",
            content=f"正在进行中的拍卖：{len(active)} 场\n\n拍卖截止时按最高价强制扣款，\n余额不足将扣成负数（欠银行），出价请量力而行！",
        )
        form.add_button("浏览拍卖", on_click=lambda p: self.show_auction_list(p, 0))
        form.add_button("搜索拍卖", on_click=lambda p: self.show_auction_search(p))
        form.add_button("发起拍卖", on_click=lambda p: self.show_inventory_pick(p))
        form.add_button("我的拍卖", on_click=lambda p: self.show_my_auctions(p))
        form.add_button("返回商城", on_click=lambda p: self.open_mall_main(p))
        player.send_form(form)

    # ---------- 浏览与出价 ----------

    def _auction_matches(self, auction: dict, keyword: str) -> bool:
        """模糊匹配：关键字包含在拍品显示名、物品 ID 或卖家名里即命中（不区分大小写）。

        例：搜「石」可命中 圆石/石砖/磨制石砖；搜「diamond」可命中 minecraft:diamond。
        """
        kw = str(keyword or "").strip().lower()
        if not kw:
            return True
        item_info = self._item_info(auction)
        hay = " ".join((
            self.auction.item_display(item_info),
            str(auction.get("item_type") or ""),
            str(auction.get("seller_name") or ""),
        )).lower()
        return kw in hay

    def show_auction_search(self, player) -> None:
        hint = Label(text="输入拍品名称关键字（支持模糊搜索，如：石、钻石、shulker）")
        inp = TextInput(label="关键字", placeholder="例如：石", default_value="")

        def _submit(p, json_str: str) -> None:
            try:
                data = json.loads(json_str)
            except Exception:
                return
            # ModalForm 返回数组中 Label 提示控件占第 0 位（null），输入值从下标 1 开始
            keyword = str(data[1] if isinstance(data, list) and len(data) > 1
                          else (data if not isinstance(data, list) else "") or "").strip()
            self.show_auction_list(p, 0, keyword)

        player.send_form(ModalForm(title="搜索拍卖", controls=[hint, inp], on_submit=_submit))

    def show_auction_list(self, player, page: int = 0, keyword: str = "") -> None:
        auctions = self.auction.list_active()
        if keyword:
            auctions = [a for a in auctions if self._auction_matches(a, keyword)]
        if not auctions:
            empty_msg = (f"没有匹配「{keyword}」的拍卖。"
                         if keyword else "当前没有进行中的拍卖。\n快输入 /om 发起第一场吧！")
            form = ActionForm(title="拍卖行", content=empty_msg)
            form.add_button("重新搜索", on_click=lambda p: self.show_auction_search(p))
            form.add_button("浏览全部", on_click=lambda p: self.show_auction_list(p, 0))
            form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
            player.send_form(form)
            return

        page_size = config.PAGE_SIZE
        total_pages = (len(auctions) + page_size - 1) // page_size
        page = max(0, min(page, total_pages - 1))
        head = f"共 {len(auctions)} 场拍卖进行中" + (f"（关键字：{keyword}）" if keyword else "")
        form = ActionForm(title=f"拍卖列表 {page + 1}/{total_pages}", content=head)
        for a in auctions[page * page_size:(page + 1) * page_size]:
            item_info = self._item_info(a)
            price = a.get("current_price")
            price_text = f"当前 {price:.2f}元" if price else f"起拍 {a['start_price']:.2f}元"
            left = self.auction.remaining_text(int(a["end_time"]))
            form.add_button(
                f"{self.auction.item_display(item_info)}×{a['quantity']}  {price_text}  剩{left}",
                on_click=lambda p, aid=a["id"]: self.show_auction_detail(p, aid),
            )
        if page > 0:
            form.add_button("上一页", on_click=lambda p: self.show_auction_list(p, page - 1, keyword))
        if page < total_pages - 1:
            form.add_button("下一页", on_click=lambda p: self.show_auction_list(p, page + 1, keyword))
        form.add_button("刷新", on_click=lambda p: self.show_auction_list(p, page, keyword))
        form.add_button("重新搜索", on_click=lambda p: self.show_auction_search(p))
        form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
        player.send_form(form)

    def show_auction_detail(self, player, auction_id: int) -> None:
        a = self.auction.get_auction(auction_id)
        if a is None or a.get("status") != STATUS_ACTIVE:
            self.toast(player, "拍卖行", "该拍卖已结束")
            self.show_auction_list(player, 0)
            return
        item_info = self._item_info(a)
        has_bid = bool(a.get("current_bidder_xuid"))
        base = float(a.get("current_price") or 0) or float(a.get("start_price") or 0)
        if has_bid:
            price_line = f"当前最高价：{a['current_price']:.2f} 元（{a['current_bidder_name']}）"
        else:
            price_line = "当前价：无（你将成为第一个出价者）"
        lines = [
            f"拍品：{self.auction.item_display(item_info)} ×{a['quantity']}",
            f"卖家：{a.get('seller_name')}",
            "",
            f"起拍价：{a['start_price']:.2f} 元",
            f"每次最低加价：{a['min_increment']:.2f} 元",
            price_line,
            f"距离截止：{self.auction.remaining_text(int(a['end_time']))}",
            "",
            "出价为固定档位：基准价 + 1/5/10 次最低加价，",
            f"即 {base + a['min_increment']:.2f} / {base + a['min_increment'] * 5:.2f} / "
            f"{base + a['min_increment'] * 10:.2f} 元。",
            "截止后按最高价强制扣款，可扣成负数（欠银行）！",
        ]
        form = ActionForm(title="拍卖详情", content="\n".join(lines))
        form.add_button("刷新", on_click=lambda p: self.show_auction_detail(p, auction_id))
        # 卖家不能竞拍自己发起的拍卖：直接不给出价入口（place_bid 服务端仍保留拦截兜底）
        is_seller = self.xuid_of(player) == a.get("seller_xuid")
        if not is_seller:
            form.add_button("我要出价", on_click=lambda p: self.show_bid_form(p, auction_id))
        if is_seller and not has_bid:
            form.add_button("取消拍卖", on_click=lambda p: self._do_cancel(p, auction_id))
        form.add_button("返回列表", on_click=lambda p: self.show_auction_list(p, 0))
        player.send_form(form)

    def show_bid_form(self, player, auction_id: int) -> None:
        a = self.auction.get_auction(auction_id)
        if a is None or a.get("status") != STATUS_ACTIVE or int(a["end_time"]) <= _now():
            self.toast(player, "拍卖行", "该拍卖已结束")
            return
        base = float(a.get("current_price") or 0) or float(a.get("start_price") or 0)
        step = float(a.get("min_increment") or 0)
        assets = self._player_assets(player)
        cap_line = ""
        if assets is not None:
            cap_line = (
                f"\n你的验资上限：{assets['total']:.2f} 元"
                f"（余额 {assets['balance']:.2f}｜存款 {assets['deposits']:.2f}｜"
                f"领地 {assets['lands']:.2f}）")
        form = ActionForm(
            title="参与竞拍",
            content=(
                f"竞拍 {self.auction.item_display(self._item_info(a))} ×{a['quantity']}\n"
                f"基准价：{base:.2f} 元｜每次最低加价：{step:.2f} 元\n\n"
                "选择加价档位（加价次数 × 最低加价）：\n"
                "截止后按最高价强制扣款，可扣成负数（欠银行）！"
                + cap_line
            ),
        )
        for times in config.AUCTION_BID_STEP_CHOICES:
            price = base + step * times
            label = f"加价{times}次：{price:.2f} 元"
            if assets is not None and price > assets["total"]:
                label += "（超上限）"
            form.add_button(
                label,
                on_click=lambda p, inc=step * times: self._confirm_bid(p, auction_id, inc))
        form.add_button("返回详情", on_click=lambda p: self.show_auction_detail(p, auction_id))
        player.send_form(form)

    def _confirm_bid(self, player, auction_id: int, increment: float) -> None:
        a = self.auction.get_auction(auction_id)
        if a is None or a.get("status") != STATUS_ACTIVE or int(a["end_time"]) <= _now():
            self.toast(player, "拍卖行", "该拍卖已结束")
            return
        base = float(a.get("current_price") or 0) or float(a.get("start_price") or 0)
        assets = self._player_assets(player)
        if assets is not None and base + increment > assets["total"]:
            self.toast(
                player, "验资不足",
                f"出价 {base + increment:.2f} 元超过你的总资产 "
                f"{assets['total']:.2f} 元（余额+存款+领地价值）")
            return

        def _go() -> None:
            ok, result = self.auction.place_bid(player, auction_id, increment)
            if not ok:
                self.toast(player, "出价失败", str(result))
                self.show_auction_detail(player, auction_id)
                return
            self.toast(player, "出价成功", f"你当前出价 {result:.2f} 元，截止前被超过将失去拍品")

        player.send_form(MessageForm(
            title="确认出价",
            content=(f"你将出价 {base + increment:.2f} 元"
                     f"（基准 {base:.2f} + 加价 {increment:.2f}）\n"
                     "截止后按最高价强制扣款，余额不足将扣成负数（欠银行）！"),
            button1="确认出价", button2="再想想",
            on_submit=lambda s, choice: _go() if int(choice) == 0 else None))

    # ---------- 发起拍卖 ----------

    def show_inventory_pick(self, player) -> None:
        inv = self.inventory_plugin()
        items = []
        if inv is not None:
            try:
                items = list(inv.api_get_inventory_items(player) or [])
            except Exception:
                items = []
        items = [it for it in items if int(it.get("count") or 0) > 0]
        if not items:
            form = ActionForm(title="发起拍卖", content="背包里没有可拍卖的物品。")
            form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
            player.send_form(form)
            return
        form = ActionForm(title="选择拍品", content="选择要拍卖的背包物品（整组托管）")
        for it in items:
            label = f"{self.auction.item_display(it)} ×{it['count']}"
            form.add_button(label, on_click=lambda p, info=dict(it): self.show_create_form(p, info))
        form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
        player.send_form(form)

    def show_create_form(self, player, item_info: dict) -> None:
        choices = [self.auction.duration_text(m) for m in config.AUCTION_DURATION_CHOICES]
        default_increment = self.setting_float("AUCTION_DEFAULT_INCREMENT", config.AUCTION_DEFAULT_INCREMENT)
        min_start = self.setting_float("AUCTION_MIN_START_PRICE", config.AUCTION_MIN_START_PRICE)
        max_qty = max(1, int(item_info.get("count") or 1))
        hint = Label(text=(
            f"拍品：{self.auction.item_display(item_info)}（持有 {max_qty} 件）\n"
            f"确认后物品立即托管，流拍或取消时退还\n起拍价不低于 {min_start:.2f} 元"))
        start_inp = TextInput(label="起拍价（元）", placeholder=f"不低于 {min_start:g}", default_value="100")
        incr_inp = TextInput(label="每次最低加价（元）", placeholder="例如 1000",
                             default_value=f"{default_increment:g}")
        dur_drop = Dropdown(label="拍卖时长", options=choices, default_index=0)
        qty_slider = Slider(label="上架数量（件）", min=1, max=max_qty, step=1,
                            default_value=max_qty)

        def _submit(p, json_str: str) -> None:
            try:
                data = json.loads(json_str)
                # ModalForm 返回数组中 Label 提示控件占第 0 位（null），输入值从下标 1 开始
                start_price = float(str(data[1]).strip())
                increment = float(str(data[2]).strip())
                duration = config.AUCTION_DURATION_CHOICES[int(data[3])]
                quantity = min(max_qty, max(1, int(float(data[4]))))
            except Exception:
                self.toast(p, "发起失败", "价格或时长格式不正确")
                return
            confirm = (
                f"拍品：{self.auction.item_display(item_info)} ×{quantity}\n"
                f"起拍价：{start_price:.2f} 元｜每次最低加价：{increment:.2f} 元\n"
                f"时长：{self.auction.duration_text(duration)}\n\n"
                f"确认后物品立即从背包托管，发起后全服播报！"
            )

            def _go() -> None:
                ok, msg = self.auction.create_auction(
                    p, item_info, quantity,
                    start_price, increment, duration)
                if ok:
                    self.toast(p, "拍卖已发起", "全服播报已发送，祝你拍出好价钱")
                else:
                    self.toast(p, "发起失败", msg)

            p.send_form(MessageForm(title="确认发起拍卖", content=confirm,
                                    button1="确认发起", button2="再想想",
                                    on_submit=lambda s, choice: _go() if int(choice) == 0 else None))

        player.send_form(ModalForm(title="发起拍卖",
                                   controls=[hint, start_inp, incr_inp, dur_drop, qty_slider],
                                   on_submit=_submit))

    # ---------- 我的拍卖 ----------

    def show_my_auctions(self, player) -> None:
        mine = self.auction.list_mine(self.xuid_of(player))
        if not mine:
            form = ActionForm(title="我的拍卖", content="你还没有发起过拍卖。")
            form.add_button("发起拍卖", on_click=lambda p: self.show_inventory_pick(p))
            form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
            player.send_form(form)
            return
        form = ActionForm(title="我的拍卖", content="进行中的拍卖可查看状态；无人出价时可取消")
        for a in mine:
            item_info = self._item_info(a)
            if a.get("status") == STATUS_ACTIVE:
                label = (f"[进行中] {self.auction.item_display(item_info)}×{a['quantity']} "
                         f"{(a.get('current_price') or a['start_price']):.2f}元 剩"
                         f"{self.auction.remaining_text(int(a['end_time']))}")
            else:
                label = f"[已结束] {self.auction.item_display(item_info)}×{a['quantity']}（{a.get('settle_note') or '已结算'}）"
            form.add_button(label, on_click=lambda p, aid=a["id"]: self.show_my_detail(p, aid))
        form.add_button("返回", on_click=lambda p: self.open_auction_main(p))
        player.send_form(form)

    def show_my_detail(self, player, auction_id: int) -> None:
        a = self.auction.get_auction(auction_id)
        if a is None:
            self.show_my_auctions(player)
            return
        item_info = self._item_info(a)
        if a.get("status") == STATUS_ACTIVE:
            self.show_auction_detail(player, auction_id)
            return
        form = ActionForm(
            title="拍卖记录",
            content=(f"拍品：{self.auction.item_display(item_info)} ×{a['quantity']}\n"
                     f"结果：{a.get('settle_note') or '已结算'}\n"
                     f"买家：{a.get('current_bidder_name') or '无'}\n"
                     f"成交价：{(a.get('current_price') or 0):.2f} 元"),
        )
        form.add_button("返回", on_click=lambda p: self.show_my_auctions(p))
        player.send_form(form)

    def _do_cancel(self, player, auction_id: int) -> None:
        ok, msg = self.auction.cancel_auction(player, auction_id)
        self.toast(player, "取消拍卖" if ok else "取消失败", msg)
        self.show_my_auctions(player)

    # ---------- 工具 ----------

    def _player_assets(self, player) -> dict | None:
        """验资开启且核心支持时返回玩家资产数据（余额/存款/领地/total），否则 None。"""
        if not self.setting_bool("AUCTION_ASSET_VERIFY_ENABLED",
                                 config.AUCTION_ASSET_VERIFY_ENABLED):
            return None
        return self.core_assets(player)

    @staticmethod
    def _item_info(auction: dict) -> dict:
        try:
            return json.loads(auction.get("item_data") or "{}")
        except Exception:
            return {}


def _now() -> int:
    import time
    return int(time.time())
