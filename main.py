"""私聊话段合并（DM merge）：把判官那套「等一口气说完」搬到私聊，并合并成一条上下文。

群里的判官是这么处理连发的：消息到达先等 ``burst_settle``（默认 3.5 秒），
期间又来新消息就把前面那条丢掉，让最新那条去判——拆成两三句发的内容只会
得到一条回复。私聊必回复（等价于判官恒返回 1），而整条判官链在
``need_active_reply`` 第一行就 ``message_type != GROUP_MESSAGE`` 短路了，
所以这层等待在私聊完全不存在：用户连发三条，机器人就回三条。

这个插件只补这一件事，不送判官、不加任何判定：

1. 私聊消息确定要调用 LLM 后（``on_waiting_llm_request``）先等 ``settle`` 秒；
2. 等待期间有新消息到达 → 并入本条（默认），本条结束时把几段文字合成一条
   用户消息、附带的图片接到消息链后面，模型看到的是「用户连着说了几句」，
   不是只剩下最后一句；
3. ``merge=false`` 时退回旧行为：前面那条让位（``stop_event``），只回最新；
4. 上一条还在生成时先排队等它结束，避免 B、C 都过了本钩子、再在
   AstrBot 的 session 锁队列里各回一条；
5. 斜杠指令、白名单外的人直接放行，不陪等。

``merge_window`` 是合并上限：用户一直说个不停时最多等到这个秒数就发出去，
不会无限攒。``min_gap`` 是两次回复的最小间隔：刚回完就来的话会继续攒着，
避免"你一句我一句"变成一问一答的连珠炮。被合并的消息不进入对话历史，
只有合并后的那一条会落库。

优先级 ``maxsize``：抢在 basicZ 的 gate 之前跑完，让等待和合并发生在所有
闸门和 session 锁排队之前；改的是 ``event.message_str``，后面的钩子和
``astr_main_agent`` 取到的就是合并后的文本，消息链里的图片也能带进请求。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from sys import maxsize

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image

# 指令自声明（插件开发规范要求，见 docs/PLUGIN-DEV.md）：本插件无斜线指令
DECLARED_COMMANDS: dict[str, tuple[str, str]] = {}
DECLARED_BEHAVIOR = (
    "无斜线指令：私聊连发时等用户说完，把几段合成一条再回一次"
    "（不送判官，不加任何判定）"
)

CONFIG_PATH = Path(r"D:\QQNTBOT\data\private_settle.json")
CMD_CONFIG = Path(r"D:\QQNTBOT\apps\AstrBot\data\cmd_config.json")

DEFAULTS: dict = {
    "enabled": True,        # 总开关
    "merge": True,          # True=连发合并成一条；False=旧行为，只回最后一条
    "settle": 5.0,          # 安静多少秒算「这一口气说完了」
    "merge_window": 20.0,   # 一直说个不停时，最多攒这么久就发（硬上限）
    "min_gap": 10.0,        # 两次回复之间至少隔多久（0=不限）
    "poll": 0.2,            # 轮询间隔
    "inflight_wait": 45.0,  # 上一条还在生成时最多排多久
    "whitelist_only": True, # 只对私聊白名单里的人生效
    "log": True,
}

_cfg_cache: tuple[float, dict] = (-1.0, {})
_wl_cache: tuple[float, tuple[bool, set[str]]] = (-1.0, (False, set()))


def load_config() -> dict:
    """读取 private_settle.json；按 mtime 缓存，改完立即生效。

    Returns:
        合并默认值后的配置；文件缺失或损坏时返回默认值（不抛异常）。
    """
    global _cfg_cache
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        return dict(DEFAULTS)
    stamp, cached = _cfg_cache
    if mtime == stamp and cached:
        return cached

    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[dm] private_settle.json 读不了，用默认值: {exc}")
        raw = {}
    if isinstance(raw, dict):
        for key, default in DEFAULTS.items():
            if key not in raw:
                continue
            val = raw[key]
            try:
                if isinstance(default, bool):
                    cfg[key] = bool(val)
                elif isinstance(default, int):
                    cfg[key] = int(val)
                elif isinstance(default, float):
                    cfg[key] = float(val)
            except (TypeError, ValueError):
                pass
    # 夹住范围：settle 太大会让每条私聊都慢，太小等于没等
    cfg["settle"] = max(0.0, min(15.0, float(cfg["settle"])))
    cfg["merge_window"] = max(1.0, min(60.0, float(cfg["merge_window"])))
    cfg["min_gap"] = max(0.0, min(60.0, float(cfg.get("min_gap", 0.0))))
    cfg["poll"] = max(0.05, min(1.0, float(cfg["poll"])))
    cfg["inflight_wait"] = max(0.0, min(120.0, float(cfg["inflight_wait"])))
    _cfg_cache = (mtime, cfg)
    return cfg


def _private_allowed(qq: str) -> bool:
    """检查这个 QQ 是否在私聊白名单里（与 access 的 ``/私聊`` 同一份数据）。

    ``enable_id_white_list`` 关着时人人可聊；开着只认
    ``qq:FriendMessage:<qq>`` 条目。

    Args:
        qq: 发消息的人。

    Returns:
        True 表示会回复，值得为它等一会儿。
    """
    global _wl_cache
    try:
        mtime = CMD_CONFIG.stat().st_mtime
    except OSError:
        return True  # 读不到配置就不拦，宁可多等也别把正常对话漏掉
    stamp, cached = _wl_cache
    if mtime != stamp:
        try:
            cfg = json.loads(CMD_CONFIG.read_text(encoding="utf-8-sig"))
            ps = cfg.get("platform_settings") or {}
            entries = ps.get("id_whitelist") or []
            cached = (
                not ps.get("enable_id_white_list", False),
                {
                    str(e).rsplit(":", 1)[-1]
                    for e in entries
                    if "FriendMessage" in str(e)
                },
            )
        except Exception as exc:  # noqa: BLE001 - 配置坏了不该拖垮回复
            logger.warning(f"[dm] 白名单读不了，先按全部放行: {exc}")
            cached = (True, set())
        _wl_cache = (mtime, cached)
    open_all, privs = cached
    return open_all or qq in privs


def _images_of(event: AstrMessageEvent) -> list:
    """取出本条消息里的图片组件，用来接进合并后的消息链。

    Args:
        event: 后到的那条私聊消息。

    Returns:
        图片组件列表；平台差异或结构异常时返回空列表。
    """
    try:
        chain = getattr(getattr(event, "message_obj", None), "message", None)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(chain, list):
        return []
    return [comp for comp in chain if isinstance(comp, Image)]


class Main(star.Star):
    """每个私聊会话一份序号、在途标记和正在攒的那一组消息，全部在内存里。"""

    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._seq: dict[str, int] = {}        # umo -> 最新组序号
        self._pending: dict[str, dict] = {}   # umo -> 正在攒的那一组
        self._inflight: dict[str, float] = {} # umo -> 生成结束的兜底时间戳
        self._last_sent: dict[str, float] = {} # umo -> 上一条回复真实发完的时间
        self._waited = 0
        self._merged = 0
        self._yielded = 0
        logger.info("[dm] private settle ready (merge mode)")

    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=maxsize - 1,
    )
    async def hold_private(self, event: AstrMessageEvent) -> None:
        """在最前面拦住私聊消息：等用户说完，把连发合成一条再交给后续链路。

        必须挂在**消息事件**（WakingCheckStage）而不是
        ``on_waiting_llm_request``。框架 4.29 的 follow-up 机制会在
        ``on_waiting_llm_request`` **之前**执行 ``try_capture_follow_up``：
        只要该会话还有活跃 agent run，新到达的消息就被注入当前 run 并直接
        ``return``（见 process_stage/method/agent_sub_stages/internal.py），
        本插件完全看不到那条消息，于是那一轮自己又回一次——这就是「每句都
        回」的直接来源。挂在消息事件上先把消息 stop 掉，后面整条 pipeline
        （含 follow-up）都不会再看到它。
        """
        cfg = load_config()
        if not cfg["enabled"]:
            return
        try:
            if not event.is_private_chat():
                return
        except Exception:  # noqa: BLE001 - 平台适配器差异，判不了就当群聊
            return

        text = (event.message_str or "").strip()
        umo = event.unified_msg_origin

        # 斜杠指令必须立刻执行，不能为了等话段慢几秒
        if text[:1] in ("/", "／"):
            return

        pics = _images_of(event)
        if not text and not pics:
            # 戳一戳/系统事件之类：没有话段可合并，放行让框架自己处理
            return

        sender = str(event.get_sender_id() or "")
        if cfg["whitelist_only"] and not _private_allowed(sender):
            return

        now = time.time()

        # 0) 已经有一组在攒：并进去，自己不再回复（stop，整条 pipeline 都看不到）。
        st = self._pending.get(umo)
        if st is not None:
            if cfg["merge"]:
                if text:
                    st["texts"].append(text)
                if pics:
                    st["images"].extend(pics)
                st["count"] += 1
                st["last"] = now
                self._merged += 1
                if cfg["log"]:
                    logger.info(
                        f"[dm] merged into #{st['seq']} "
                        f"({st['count']} msgs) | {text[:20] or '[图片]'}"
                    )
                event.stop_event()
                return
            # 旧行为：让旧组让位，这条自己接管当 owner（不 stop 自己）
            st["yield"] = True
            self._pending.pop(umo, None)
            self._yielded += 1
            if cfg["log"]:
                logger.info(
                    f"[dm] newer message arrived, yield #{st['seq']} "
                    f"| {text[:20] or '[图片]'}"
                )

        # 1) 自己当这一组的 owner。纯图片也建组：否则首张图直接放行、自己触发
        #    一轮请求，用户连发几张图就会回好几条。
        seq = self._seq.get(umo, 0) + 1
        self._seq[umo] = seq
        st = {
            "seq": seq,
            "texts": [text] if text else [],
            "images": [],  # owner 自己的图已在本事件消息链里，只需收别人的
            "count": 1,
            "last": now,
            "start": now,
            "settle_start": now,
            "yield": False,
        }
        self._pending[umo] = st
        poll = float(cfg["poll"])
        try:
            # 2) 上一条还在生成：先排队等它结束。私聊里 basicZ 走 @/唤醒直通、
            #    不抢在途锁，但 AstrBot 的 session 锁会把同一会话的 LLM 请求串起来：
            #    如果 B、C 都在上一条生成期间过了本钩子，它们会依次各回一条。
            inflight_wait = float(cfg["inflight_wait"])
            if inflight_wait > 0:
                deadline = time.time() + inflight_wait
                while self._inflight.get(umo, 0.0) > time.time():
                    if time.time() >= deadline:
                        logger.info(f"[dm] inflight wait cap {inflight_wait:.0f}s, go on")
                        break
                    await asyncio.sleep(poll)
                    if self._taken_over(umo, st):
                        self._drop(event, st, "inflight")
                        return

            # 3) 等这一口气说完：安静 settle 秒，并且离上次回复至少 min_gap 秒。
            #    只看「安静」不够——真人打字常常停三四秒再补一句，3.5 秒窗口
            #    刚过就发出去，下一句永远赶不上（实测差 0.58 秒就各回了一条）。
            #    加上 min_gap 后，刚回完就来的话会继续攒着，合成一批再回。
            settle = float(cfg["settle"])
            gap = float(cfg["min_gap"])
            st["settle_start"] = time.time()
            window = float(cfg["merge_window"])
            reason = "quiet"
            if settle > 0 or gap > 0:
                while True:
                    await asyncio.sleep(poll)
                    if self._taken_over(umo, st):
                        self._drop(event, st, f"settle {settle:.1f}s")
                        return
                    now = time.time()
                    quiet = now - st["last"] >= settle
                    gap_ok = gap <= 0 or now - self._last_sent.get(umo, 0.0) >= gap
                    if quiet and gap_ok:
                        break
                    if now - st["settle_start"] >= window:
                        reason = "window"
                        if cfg["log"]:
                            logger.info(
                                f"[dm] merge window {window:.0f}s hit, send now "
                                f"({st['count']} msgs, "
                                f"quiet={quiet}, gap_ok={gap_ok})"
                            )
                        break

            # 4) 先把组从 pending 摘掉，再并进本事件。晚于这一步到达的消息会
            #    新建一组当 owner，不会并进一个已经不再读取的组（不丢话）。
            if self._pending.get(umo) is st:
                self._pending.pop(umo, None)
            merged = self._apply_merge(event, st)
            if cfg["log"]:
                self._waited += 1
                tail = f", merged {merged}" if merged else ""
                logger.info(
                    f"[dm] {reason} {settle:.1f}s/gap {gap:.0f}s done, reply"
                    f"{tail} (waited {self._waited} / yielded {self._yielded}) "
                    f"| {text[:24] or '[图片]'}"
                )
        finally:
            if self._pending.get(umo) is st:
                self._pending.pop(umo, None)

    def _apply_merge(self, event: AstrMessageEvent, st: dict) -> int:
        """把手头攒的几段文字和图片并进 owner 事件。

        Args:
            event: 这一组的 owner 事件，接下来会真的去请求 LLM。
            st: 这一组的累积状态。

        Returns:
            额外并入的消息条数（不含 owner 自己）。
        """
        texts: list[str] = st.get("texts") or []
        images: list = st.get("images") or []
        if texts:
            merged_text = "\n".join(t for t in texts if t)
            if merged_text:
                event.message_str = merged_text
                # 少数实现读消息对象上那份副本，一起改掉免得两边不一致
                obj = getattr(event, "message_obj", None)
                if obj is not None and hasattr(obj, "message_str"):
                    obj.message_str = merged_text
        if images:
            chain = getattr(getattr(event, "message_obj", None), "message", None)
            if isinstance(chain, list):
                # owner 自己的图已经在链里，这里只补后到的，按对象身份去重
                existing = {id(comp) for comp in chain}
                for comp in images:
                    if id(comp) not in existing:
                        chain.append(comp)
                        existing.add(id(comp))
        return max(0, int(st.get("count", 1)) - 1)

    def _taken_over(self, umo: str, st: dict) -> bool:
        """这一组是否该让位：被标了 yield，或被后来的组顶替。

        ``pending`` 被直接清空（窗口到期、新消息另开一组）不算被顶替——
        手里攒的内容还在 ``st`` 里，照常发出去才不会丢话。

        Args:
            umo: 会话标识。
            st: 自己这一组的状态。

        Returns:
            True 表示该停掉本条事件，交给别的组回复。
        """
        if st.get("yield"):
            return True
        current = self._pending.get(umo)
        return current is not None and current is not st

    def _drop(self, event: AstrMessageEvent, st: dict, why: str) -> None:
        """本条让位：停止事件，交给后面的消息去回。"""
        if load_config().get("log", True):
            logger.info(f"[dm] yield #{st['seq']} ({why})")
        event.stop_event()

    # --- 在途跟踪：只用于排队，不参与任何判定 ---------------------------

    @filter.on_llm_request(priority=maxsize - 500)
    async def mark_inflight(self, event: AstrMessageEvent, req) -> None:
        """LLM 开始生成时打标，后续到达的私聊消息先等它结束。"""
        if event.is_private_chat():
            self._inflight[event.unified_msg_origin] = time.time() + 180.0

    @filter.on_llm_response()
    async def clear_inflight(self, event: AstrMessageEvent, response) -> None:
        """拿到回复就放开队列（与 basicZ 释放 busy 同一时机）。"""
        if event.is_private_chat():
            self._inflight.pop(event.unified_msg_origin, None)

    @filter.after_message_sent()
    async def clear_after_send(self, event: AstrMessageEvent) -> None:
        """兜底：消息真的发出去后一定放开，避免标记残留。"""
        if not event.is_private_chat():
            return
        umo = event.unified_msg_origin
        self._inflight.pop(umo, None)
        # 记下真实发完的时刻，min_gap 用它算「离上次回复多久」
        self._last_sent[umo] = time.time()
