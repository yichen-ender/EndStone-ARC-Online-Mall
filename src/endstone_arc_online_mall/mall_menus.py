"""网上商城表单 UI：主菜单 → 商品列表（分页/搜索）→ 详情（报价+购买）。

表单约定（与项目其他插件一致）：
- ActionForm.add_button(text, icon=None, on_click=callable(player))
- ModalForm(controls=[...], on_submit=callable(player, json_str))
- MessageForm on_submit=callable(player, choice:int)，0=button1
"""

import json

from endstone.form import ActionForm, Label, ModalForm, TextInput

from . import config
from .mall_service import dimension_label

PAGE_SIZE = config.PAGE_SIZE


class MallMenus:
    """Mixin：挂在 ARCOnlineMallPlugin 上，self 即插件实例。"""

    # ---------- 主菜单 ----------

    def open_mall_main(self, player) -> None:
        lines = ["欢迎使用弧光网上商城", ""]
        balance = self.core_money(player)
        if balance is not None:
            lines.append(f"当前存款：{balance:.2f} 元")
        lines.append("商品来自全服按钮商店与木牌商店，")
        lines.append("下单后按距离收取配送费并立即送货上门。")
        form = ActionForm(title="弧光网上商城", content="\n".join(lines))
        form.add_button("浏览全部商品", on_click=lambda p: self.show_shop_list(p, 0, ""))
        form.add_button("搜索商品", on_click=lambda p: self.show_search(p))
        form.add_button("拍卖行", on_click=lambda p: self.open_auction_main(p))
        form.add_button("刷新", on_click=lambda p: self.open_mall_main(p))
        player.send_form(form)

    # ---------- 商品列表 ----------

    def show_search(self, player) -> None:
        hint = Label(text="输入物品名称关键字（支持模糊搜索，留空显示全部）")
        inp = TextInput(label="关键字", placeholder="例如：石、钻石、shulker", default_value="")

        def _submit(p, json_str: str) -> None:
            try:
                data = json.loads(json_str)
            except Exception:
                return
            # ModalForm 返回数组中 Label 提示控件占第 0 位（null），输入值从下标 1 开始
            keyword = str(data[1] if isinstance(data, list) and len(data) > 1
                          else (data if not isinstance(data, list) else "") or "").strip()
            self.show_shop_list(p, 0, keyword)

        player.send_form(ModalForm(title="搜索商品", controls=[hint, inp], on_submit=_submit))

    def show_shop_list(self, player, page: int = 0, keyword: str = "") -> None:
        shops = self.mall.list_sell_shops()
        if keyword:
            kw = keyword.lower()
            shops = [s for s in shops if kw in str(s.get("item_type") or "").lower()
                     or kw in str((s.get("item_info") or {}).get("name") or "").lower()
                     or kw in str(s.get("owner_name") or "").lower()]
        if not shops:
            form = ActionForm(title="网上商城", content="暂无可网购的商品。\n\n可能是商店插件未加载或没有出售向商店。")
            form.add_button("返回", on_click=lambda p: self.open_mall_main(p))
            player.send_form(form)
            return

        page = max(0, min(page, max(0, len(shops) - 1) // PAGE_SIZE))
        chunk = shops[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        total_pages = (len(shops) + PAGE_SIZE - 1) // PAGE_SIZE
        balance = self.core_money(player)
        head = f"共 {len(shops)} 件商品" + (f"（关键字：{keyword}）" if keyword else "")
        if balance is not None:
            head += f"｜存款 {balance:.2f} 元"
        form = ActionForm(title=f"商品列表 {page + 1}/{total_pages}", content=head)
        for shop in chunk:
            distance, cross = self.mall.distance_info(shop, player)
            label = (f"{self.mall.item_display(shop)}  {self.mall.unit_price(shop):.2f}元"
                     f"  [{self.mall.distance_text(distance, cross)}]")
            form.add_button(label, on_click=lambda p, s=shop: self.show_shop_detail(p, s["source"], s["id"]))
        if page > 0:
            form.add_button("上一页", on_click=lambda p: self.show_shop_list(p, page - 1, keyword))
        if page < total_pages - 1:
            form.add_button("下一页", on_click=lambda p: self.show_shop_list(p, page + 1, keyword))
        form.add_button("重新搜索", on_click=lambda p: self.show_search(p))
        form.add_button("返回主菜单", on_click=lambda p: self.open_mall_main(p))
        player.send_form(form)

    # ---------- 详情与购买 ----------

    def show_shop_detail(self, player, source: str, shop_id: int) -> None:
        shop = self.mall.find_shop(source, shop_id)
        if shop is None:
            self.show_shop_list(player, 0)
            return
        bill = self.mall.quote(shop, player, 1)
        distance, _ = self.mall.distance_info(shop, player)
        stock = self.mall.stock_of(shop)

        lines = [
            f"商品：{self.mall.item_display(shop)}",
            f"单价：{bill['unit_price']:.2f} 元",
            f"库存：{'无限' if stock >= 2_147_483_647 else stock}",
            f"店主：{shop.get('owner_name') or '系统'}"
            + ("（官方商店）" if int(shop.get("is_infinite") or 0) else ""),
            "",
            f"商店位置：{dimension_label(str(shop.get('dimension') or ''), self.dimension_aliases())} "
            f"({shop.get('x')}, {shop.get('y')}, {shop.get('z')})〔{shop.get('dimension') or '未知'}〕",
            f"距离你：{self.mall.distance_text(distance, bill['cross_dimension'])}",
            "",
            f"配送费：{bill['delivery_fee']:.2f} 元"
            + ("（含跨维度费）" if bill["cross_dimension"] else ""),
            f"平台手续费：{bill['platform_fee']:.2f} 元（{self._fee_rate_percent()}%）",
            f"购买 1 件合计：{bill['total']:.2f} 元",
            "",
            "嫌运费贵？记下坐标自己去买，运费为 0！",
        ]
        form = ActionForm(title="商品详情", content="\n".join(lines))
        form.add_button("购买", on_click=lambda p: self.show_buy_quantity(p, source, shop_id))
        form.add_button("刷新报价", on_click=lambda p: self.show_shop_detail(p, source, shop_id))
        form.add_button("返回列表", on_click=lambda p: self.show_shop_list(p, 0))
        player.send_form(form)

    def show_buy_quantity(self, player, source: str, shop_id: int) -> None:
        shop = self.mall.find_shop(source, shop_id)
        if shop is None:
            self.toast(player, "购买失败", "商店不存在或已关闭")
            return
        stock = self.mall.stock_of(shop)
        hint = Label(text=f"购买 {self.mall.item_display(shop)}｜单价 {self.mall.unit_price(shop):.2f} 元\n"
                          f"运费与手续费按整单收取，库存上限 {stock}")
        inp = TextInput(label="购买数量", placeholder="正整数", default_value="1")

        def _submit(p, json_str: str) -> None:
            try:
                data = json.loads(json_str)
                # ModalForm 返回数组中 Label 提示控件占第 0 位（null），输入值从下标 1 开始
                qty = int(str(data[1] if isinstance(data, list) and len(data) > 1
                              else (data if not isinstance(data, list) else "") or "").strip())
            except Exception:
                self.toast(p, "购买失败", "数量格式不正确")
                return
            self._do_purchase(p, source, shop_id, qty)

        player.send_form(ModalForm(title="确认购买", controls=[hint, inp], on_submit=_submit))

    def _do_purchase(self, player, source: str, shop_id: int, qty: int) -> None:
        shop = self.mall.find_shop(source, shop_id)
        ok, result = self.mall.purchase(player, source, shop_id, qty)
        if not ok:
            self.toast(player, "购买失败", str(result))
            if shop is not None:
                self.show_shop_detail(player, source, shop_id)
            return
        bill = result
        msg = (f"已购 {self.mall.item_display(shop)} ×{bill['quantity']}，"
               f"商品款 {bill['goods']:.2f} + 手续费 {bill['platform_fee']:.2f}"
               f" + 配送费 {bill['delivery_fee']:.2f}，合计 {bill['total']:.2f} 元")
        self.toast(player, "网购成功", msg)

    # ---------- 工具 ----------

    def _fee_rate_percent(self) -> str:
        rate = self.setting_float("PLATFORM_FEE_RATE", config.PLATFORM_FEE_RATE)
        return f"{rate * 100:g}"
