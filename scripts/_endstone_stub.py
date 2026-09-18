# -*- coding: utf-8 -*-
"""arc_esper_career 离线测试共享的 endstone stub（覆盖主插件全部导入与行为）。

默认只提供真实 endstone 0.11.x 已实装的 API；
设 ARC_STUB_FUTURE_ENCHANT=1 可模拟 endstone 未来实装附魔事件后的环境，
用于验证工匠职业的自动解锁路径。
"""
import os
import sys
import types

endstone = types.ModuleType("endstone")
endstone.__path__ = []


class GameMode:
    SURVIVAL = "SURVIVAL"
    ADVENTURE = "ADVENTURE"
    CREATIVE = "CREATIVE"
    SPECTATOR = "SPECTATOR"


endstone.GameMode = GameMode


class Player:
    """占位类型（插件仅用于类型标注）。"""


class Location:
    def __init__(self, dimension=None, x=0.0, y=0.0, z=0.0, pitch=0.0, yaw=0.0):
        self.dimension = dimension
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.pitch = float(pitch)
        self.yaw = float(yaw)

    def distance(self, other):
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2) ** 0.5


endstone.Player = Player
endstone.Location = Location


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    setattr(endstone, name.split(".", 1)[1], m)
    return m


cmd_mod = _mod("endstone.command")
cmd_mod.Command = type("Command", (), {})
cmd_mod.CommandSender = type("CommandSender", (), {})

evt_mod = _mod("endstone.event")


def event_handler(*a, **k):
    def deco(fn):
        return fn
    return deco


evt_mod.event_handler = event_handler


class _Cancellable:
    def __init__(self, *a, **k):
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    @property
    def is_cancelled(self):
        return self._cancelled


# 事件名单对齐真实 endstone 0.11.x（EnchantItemEvent / PrepareItemEnchantEvent 不存在，
# 不要往这里加真实 API 里没有的事件，除非走下面的 FUTURE 开关）
for cls_name in (
    "ActorDamageEvent", "ActorDeathEvent", "BlockBreakEvent", "BlockCookEvent",
    "BlockGrowEvent", "BlockPlaceEvent", "PlayerDeathEvent",
    "PlayerEmoteEvent", "PlayerInteractEvent", "PlayerItemConsumeEvent",
    "PlayerJoinEvent", "PlayerJumpEvent", "PlayerQuitEvent", "PlayerRespawnEvent",
    "PluginEnableEvent",
):
    setattr(evt_mod, cls_name, type(cls_name, (_Cancellable,), {}))

# 模拟 endstone 未来实装附魔事件（文档已收录、0.11.x 未实装）
if os.environ.get("ARC_STUB_FUTURE_ENCHANT") == "1":
    for cls_name in ("EnchantItemEvent", "PrepareItemEnchantEvent"):
        setattr(evt_mod, cls_name, type(cls_name, (_Cancellable,), {}))

plugin_mod = _mod("endstone.plugin")
plugin_mod.Plugin = type("Plugin", (), {})

form_mod = _mod("endstone.form")


# 表单签名对齐真实 endstone 0.11.x（参数名不一致要在离线测试期暴露，别加 **k 兜底）
class _BaseForm:
    def __init__(self, title="", content="", on_submit=None, on_close=None):
        self.title = title
        self.content = content
        self.on_submit = on_submit
        self.on_close = on_close


class ActionForm(_BaseForm):
    def __init__(self, title="", content="", buttons=None, on_submit=None, on_close=None):
        super().__init__(title, content, on_submit, on_close)
        self.buttons = []

    def add_button(self, text, icon=None, on_click=None):
        self.buttons.append((text, on_click))
        return self


class MessageForm(_BaseForm):
    def __init__(self, title="", content="", button1="", button2="",
                 on_submit=None, on_close=None):
        super().__init__(title, content, on_submit, on_close)
        self.button1 = button1
        self.button2 = button2


class ModalForm(_BaseForm):
    def __init__(self, title="", controls=None, submit_button=None, icon=None,
                 on_submit=None, on_close=None):
        super().__init__(title, "", on_submit, on_close)
        self.controls = list(controls or [])


class Dropdown:
    def __init__(self, label="", options=None, default_index=None):
        self.label = label
        self.options = list(options or [])
        self.default_index = default_index


class Label:
    def __init__(self, text=""):
        self.text = text


class TextInput:
    def __init__(self, label="", placeholder="", default_value=None):
        self.label = label
        self.placeholder = placeholder
        self.default_value = default_value


class Toggle:
    def __init__(self, label="", default_value=False):
        self.label = label
        self.default_value = default_value


class Slider:
    def __init__(self, label="", min=0, max=100, step=20, default_value=None):
        self.label = label
        self.min = min
        self.max = max
        self.step = step
        self.default_value = default_value if default_value is not None else min


form_mod.ActionForm = ActionForm
form_mod.MessageForm = MessageForm
form_mod.ModalForm = ModalForm
form_mod.Dropdown = Dropdown
form_mod.Label = Label
form_mod.TextInput = TextInput
form_mod.Toggle = Toggle
form_mod.Slider = Slider

inv_mod = _mod("endstone.inventory")


class ItemStack:
    def __init__(self, type="minecraft:stone", amount=1, data=0):
        self.type = type
        self.amount = int(amount)
        self.data = data
        self._nbt = None
        self._meta = None

    @property
    def nbt(self):
        return self._nbt

    @nbt.setter
    def nbt(self, value):
        self._nbt = value

    @property
    def item_meta(self):
        return self._meta if self._meta is not None else _ItemMeta()

    def set_item_meta(self, meta):
        self._meta = meta
        return True


class _ItemMeta:
    def __init__(self):
        self.lore = []


inv_mod.ItemStack = ItemStack

# ---- endstone.nbt ----
nbt_mod = _mod("endstone.nbt")


class StringTag:
    def __init__(self, value=""):
        self.value = str(value)


class IntTag:
    def __init__(self, value=0):
        self.value = int(value)


class FloatTag:
    def __init__(self, value=0.0):
        self.value = float(value)


class DoubleTag:
    def __init__(self, value=0.0):
        self.value = float(value)


class CompoundTag:
    """行为对齐文档：setdefault / keys / pop / to_dict。"""

    def __init__(self, mapping=None):
        self._data = {}
        for k, v in dict(mapping or {}).items():
            self._data[str(k)] = v

    def setdefault(self, key, tag):
        key = str(key)
        if key not in self._data:
            self._data[key] = tag
        return self._data[key]

    def keys(self):
        return list(self._data.keys())

    def values(self):
        return list(self._data.values())

    def items(self):
        return list(self._data.items())

    def pop(self, key, default=None):
        return self._data.pop(str(key), default)

    def to_dict(self):
        out = {}
        for k, v in self._data.items():
            out[k] = v.value if hasattr(v, "value") else v
        return out


nbt_mod.StringTag = StringTag
nbt_mod.IntTag = IntTag
nbt_mod.FloatTag = FloatTag
nbt_mod.DoubleTag = DoubleTag
nbt_mod.CompoundTag = CompoundTag

# ---- endstone.potion（effect_compat 用） ----
potion_mod = _mod("endstone.potion")


class Effect:
    def __init__(self, effect_type, duration, amplifier=0, ambient=False,
                 particles=True, icon=True):
        self.type = effect_type
        self.duration = duration
        self.amplifier = amplifier
        self.ambient = ambient
        self.particles = particles
        self.icon = icon


class _EffectTypeMeta(type):
    def __getattr__(cls, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return f"stub_effect:{name.lower()}"


class EffectType(metaclass=_EffectTypeMeta):
    @staticmethod
    def get(key):
        return f"stub_effect:{str(key).split(':')[-1]}"


potion_mod.Effect = Effect
potion_mod.EffectType = EffectType

sys.modules["endstone"] = endstone


def add_src_to_path():
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
