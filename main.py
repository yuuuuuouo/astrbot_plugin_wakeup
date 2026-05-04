"""
astrbot_plugin_wakeup - 隐式生物钟与主动唤醒引擎

赋予大模型自主规划时间跨度的能力。模型在回复末尾输出 [NEXT: Xm] 标签，
系统静默截获并转化为定时任务，时间到后构造伪造消息注入 AstrBot 完整处理流程，
让模型根据上下文决定是否主动发起对话。

作者: yuuuuuouo
版本: 0.2.0
"""

import asyncio
import json
import os
import re
import time
from typing import Dict, List

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp


@register(
    "astrbot_plugin_wakeup",
    "yuuuuuouo",
    "隐式生物钟与主动唤醒引擎",
    "0.2.0",
)
class WakeupPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # ---------- 配置 ----------
        self.trigger_keyword = str(
            self.config.get("trigger_keyword", "NEXT")
        ).strip().upper() or "NEXT"
        self.wakeup_prompt = str(
            self.config.get("wakeup_prompt", "")
        ).strip() or (
            "设定的休眠时间已到，用户尚未回复你的上一条消息。"
            "请结合对话上下文和你当前的状态，决定是否要主动发送消息。"
            "注意：以上为系统推送，不是用户说的话。"
        )
        self.default_silence_minutes = int(
            self.config.get("default_silence_minutes", 360)
        )
        self.allowed_models = [
            m.strip().lower()
            for m in self.config.get("allowed_models", [])
            if m and m.strip()
        ]
        self._bot_qq_id = str(
            self.config.get("bot_qq_id", "")
        ).strip()
        self._extra_commands = [
            c.strip().lower().lstrip("/")
            for c in self.config.get("extra_commands", [])
            if c and c.strip()
        ]

        # ---------- 正则 ----------
        self.next_pattern = re.compile(
            rf"\[{re.escape(self.trigger_keyword)}"
            rf"\s*:\s*(\d+)\s*m\]",
            re.IGNORECASE,
        )

        # ---------- 运行时状态 ----------
        self.alarms: Dict[str, asyncio.Task] = {}
        self.alarm_records: Dict[str, float] = {}
        self.last_raw_text: Dict[str, str] = {}
        self._terminated = False
        self._waking_umos: set = set()
        self._smashed_umos: set = set()
        self._cqhttp_bot = None

        # ---------- 持久化 ----------
        try:
            data_dir = StarTools.get_data_dir(
                "astrbot_plugin_wakeup"
            )
        except Exception:
            data_dir = os.path.dirname(
                os.path.abspath(__file__)
            )
        os.makedirs(data_dir, exist_ok=True)
        self.data_file = os.path.join(
            data_dir, "wakeup_alarms.json"
        )

        logger.info(
            f"[wakeup] 插件已加载 | "
            f"关键词={self.trigger_keyword} | "
            f"白名单={self.allowed_models or '全部'} | "
            f"兜底={self.default_silence_minutes}m"
        )

    # ==================== 生命周期 ====================

    async def initialize(self):
        self._terminated = False

        # 延迟搜索 bot 实例（给适配器连接留时间）
        async def _delayed_search():
            await asyncio.sleep(3)
            if self._cqhttp_bot is None:
                await self._try_acquire_bot()

        asyncio.create_task(_delayed_search())
        await self._restore_alarms()

    async def terminate(self):
        logger.info(
            "[wakeup] 插件卸载，取消内存中的闹钟"
            "（持久化记录保留，重载后恢复）"
        )
        self._terminated = True
        for task in list(self.alarms.values()):
            if not task.done():
                task.cancel()
        self.alarms.clear()
        self.last_raw_text.clear()
        
    # ==================== 主动获取 Bot 实例 ====================

    async def _try_acquire_bot(self) -> bool:
        """从 AstrBot 内部主动搜索 CQHttp 实例和机器人 QQ 号"""
        # 搜索 bot 实例
        if self._cqhttp_bot is None:
            try:
                ctx = self.context
                for mgr_name in [
                    "platform_manager",
                    "_platform_manager",
                    "platform_mgr",
                    "_platform_mgr",
                ]:
                    mgr = getattr(ctx, mgr_name, None)
                    if mgr is None:
                        continue
                    for list_name in [
                        "platforms",
                        "platform_insts",
                        "_platforms",
                        "adapters",
                    ]:
                        plist = getattr(
                            mgr, list_name, None
                        )
                        if not plist or not hasattr(
                            plist, "__iter__"
                        ):
                            continue
                        for p in plist:
                            bot = getattr(
                                p, "bot", None
                            )
                            if bot and hasattr(
                                bot, "send_private_msg"
                            ):
                                self._cqhttp_bot = bot
                                logger.info(
                                    "[wakeup] ✅ 主动获取到"
                                    " CQHttp 实例"
                                )
                                break
                        if self._cqhttp_bot:
                            break
                    if self._cqhttp_bot:
                        break
            except Exception as e:
                logger.debug(
                    f"[wakeup] 搜索 bot 实例失败: {e}"
                )

        # 获取机器人 QQ 号
        if self._cqhttp_bot and not self._bot_qq_id:
            try:
                info = await self._cqhttp_bot.get_login_info()
                qq = str(info.get("user_id", ""))
                if qq and qq != "0":
                    self._bot_qq_id = qq
                    logger.info(
                        f"[wakeup] ✅ 自动获取到机器人"
                        f" QQ: {self._bot_qq_id}"
                    )
            except Exception as e:
                logger.debug(
                    f"[wakeup] get_login_info 失败: {e}"
                )

        return (
            self._cqhttp_bot is not None
            and bool(self._bot_qq_id)
        )

    # ==================== 指令: /wakeup ====================

    @filter.command("wakeup")
    async def cmd_wakeup(self, event: AstrMessageEvent):
        if not self.alarm_records:
            yield event.plain_result(
                "当前没有活跃的闹钟。"
            )
            return
        now = time.time()
        lines = ["⏰ 当前活跃的闹钟：", ""]
        for umo, target_ts in self.alarm_records.items():
            remaining = target_ts - now
            if remaining <= 0:
                status = "即将触发"
            else:
                h = int(remaining // 3600)
                m = int((remaining % 3600) // 60)
                s = int(remaining % 60)
                if h > 0:
                    status = f"剩余 {h}h {m}m {s}s"
                elif m > 0:
                    status = f"剩余 {m}m {s}s"
                else:
                    status = f"剩余 {s}s"
            short = (
                umo
                if len(umo) <= 40
                else f"...{umo[-30:]}"
            )
            lines.append(f"  • {short}")
            lines.append(f"    {status}")
            lines.append("")
        yield event.plain_result("\n".join(lines).strip())

    # ==================== 指令: /smash ====================

    @filter.command("smash")
    async def cmd_smash(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        if umo in self.alarms:
            old = self.alarms.pop(umo)
            if not old.done():
                old.cancel()
            self._smashed_umos.add(umo)
            self._remove_alarm_record(umo)
            logger.info(
                f"[wakeup] 🔨 手动砸碎闹钟 | umo={umo}"
            )
            yield event.plain_result("⏰ 闹钟已砸碎。")
        else:
            yield event.plain_result(
                "当前没有活跃的闹钟。"
            )

    # ==================== 钩子: LLM 响应 ====================

    @filter.on_llm_response()
    async def on_llm_response_hook(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        umo = event.unified_msg_origin

        # 注入 pipeline 完成后清理唤醒标记
        if umo in self._waking_umos:

            async def _cleanup(u=umo):
                await asyncio.sleep(30)
                self._waking_umos.discard(u)

            asyncio.create_task(_cleanup())

        raw = getattr(resp, "completion_text", "") or ""
        self.last_raw_text[umo] = raw
        await self._try_schedule_from_text(
            event, raw, source="on_llm_response"
        )

    # ==================== 钩子: 装饰阶段兜底 ====================

    @filter.on_decorating_result()
    async def on_decorating_result_hook(
        self, event: AstrMessageEvent
    ):
        umo = event.unified_msg_origin
        result = event.get_result()
        if result is None:
            return
        parts = []
        for seg in result.chain or []:
            if isinstance(seg, Comp.Plain):
                parts.append(seg.text)
            elif hasattr(seg, "text"):
                parts.append(
                    getattr(seg, "text", "") or ""
                )
        chain_text = "".join(parts)
        last_raw = self.last_raw_text.get(umo, "")
        if last_raw and self.next_pattern.search(last_raw):
            return
        await self._try_schedule_from_text(
            event, chain_text, source="on_decorating_result"
        )

    # ==================== 钩子: 用户消息 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(
        self, event: AstrMessageEvent
    ):
        # 过滤机器人自身消息
        try:
            if (
                event.get_sender_id()
                == event.get_self_id()
            ):
                return
        except Exception:
            pass

        # 获取 CQHttp 实例 + 机器人 QQ
        if self._cqhttp_bot is None and hasattr(
            event, "bot"
        ):
            self._cqhttp_bot = event.bot
            logger.info(
                "[wakeup] ✅ 已获取 CQHttp 实例引用"
            )
        if not self._bot_qq_id:
            try:
                detected = str(event.get_self_id())
                if detected and detected != "None":
                    self._bot_qq_id = detected
                    logger.info(
                        f"[wakeup] ✅ 自动检测到机器人 QQ: "
                        f"{self._bot_qq_id}"
                    )
            except Exception:
                pass

        # 收集消息文本
        msg_text = (
            getattr(event, "message_str", None) or ""
        ).strip()

        # 从 message_chain 提取
        has_chain = False
        try:
            chain = getattr(event, "message_chain", None)
            if chain:
                for seg in chain:
                    if (
                        isinstance(seg, Comp.Plain)
                        and seg.text
                        and seg.text.strip()
                    ):
                        has_chain = True
                        break
                    elif hasattr(seg, "text"):
                        if (
                            getattr(seg, "text", "") or ""
                        ).strip():
                            has_chain = True
                            break
                    elif not isinstance(seg, Comp.Plain):
                        has_chain = True
                        break
        except Exception:
            pass

        # 空消息过滤
        if not msg_text and not has_chain:
            return

        # ============================================
        # 指令过滤（兼容 AstrBot v4.24.2+）
        # v4.24.2 会把 / 前缀剥掉再传给 message_str，
        # 所以无法用 startswith("/") 判断。
        # 改为：匹配已知指令关键词，命中则跳过不砸碎。
        # ============================================
        _KNOWN_COMMANDS = {
            # AstrBot 内置指令
            "help", "new", "provider", "reset",
            "sid", "stats", "stop",
            # 本插件指令
            "smash", "wakeup",
            # 常见插件指令（按需追加）
            "status", "about", "plugin", "plugins",
            "config", "reload", "update", "version",
            "t2i", "tts", "stt", "draw", "img",
            "search", "web", "wiki",
            "music", "song",
            "weather",
            "roll", "dice",
            "menu", "list",
            "bind", "unbind",
            "on", "off", "enable", "disable",
            "set", "get", "info",
            "ban", "unban", "kick",
            "mute", "unmute", "switch",
            "recall", "revoke", "ls",
        }

        _KNOWN_COMMANDS.update(self._extra_commands)

        # 检查 1：message_str 被剥掉 / 后是否匹配已知指令
        _msg_lower = msg_text.lower()
        _msg_first_word = _msg_lower.split()[0] if _msg_lower else ""
        if _msg_first_word in _KNOWN_COMMANDS:
            logger.debug(
                f"[wakeup] 检测到指令(关键词匹配)，"
                f"跳过砸碎 | msg={msg_text[:50]}"
            )
            return

        # 检查 2：原始文本仍以 / 开头（兼容旧版本）
        if msg_text.startswith("/"):
            logger.debug(
                f"[wakeup] 检测到指令(/前缀)，"
                f"跳过砸碎 | msg={msg_text[:50]}"
            )
            return

        # 检查 3：利用 is_at_or_wake_command 属性
        # 如果消息是唤醒指令且文本匹配已知指令，跳过
        _is_wake_cmd = getattr(
            event, "is_at_or_wake_command", None
        )
        if _is_wake_cmd and _msg_first_word in _KNOWN_COMMANDS:
            return

        umo = event.unified_msg_origin

        # 唤醒期间不砸碎
        if umo in self._waking_umos:
            return

        # 砸碎闹钟
        if umo in self.alarms:
            old = self.alarms.pop(umo)
            if not old.done():
                old.cancel()
                self._smashed_umos.add(umo)
            logger.info(
                f"[wakeup] 🔨 用户发消息，砸碎闹钟 | "
                f"umo={umo}"
            )
            self._remove_alarm_record(umo)
        else:
            self._smashed_umos.discard(umo)

        self.last_raw_text.pop(umo, None)

    # ==================== 标签提取与调度 ====================

    async def _try_schedule_from_text(
        self,
        event: AstrMessageEvent,
        text: str,
        source: str,
    ):
        umo = event.unified_msg_origin
        if not text:
            return
        match = self.next_pattern.search(text)
        if match:
            minutes = int(match.group(1))
            logger.info(
                f"[wakeup] 🎯 [{source}] 捕获 "
                f"[{self.trigger_keyword}: {minutes}m] | "
                f"umo={umo}"
            )
            await self._schedule_alarm_by_umo(
                umo, minutes * 60
            )
            return
        if (
            source == "on_llm_response"
            and self.default_silence_minutes > 0
        ):
            logger.info(
                f"[wakeup] ⚠️ [{source}] 未捕获标签，"
                f"兜底 {self.default_silence_minutes}m | "
                f"umo={umo}"
            )
            await self._schedule_alarm_by_umo(
                umo, self.default_silence_minutes * 60
            )

    async def _schedule_alarm_by_umo(
        self, umo: str, delay_seconds: int
    ):
        if umo in self.alarms:
            old = self.alarms.pop(umo)
            if not old.done():
                old.cancel()
        self._smashed_umos.discard(umo)
        task = asyncio.create_task(
            self._alarm_task(umo, delay_seconds)
        )
        self.alarms[umo] = task
        self.alarm_records[umo] = time.time() + delay_seconds
        self._save_alarm_records()
        logger.info(
            f"[wakeup] ⏰ 闹钟已设定 | umo={umo} | "
            f"{delay_seconds}s ({delay_seconds // 60}m)"
        )

    # ==================== 伪造消息注入 ====================

    async def _wakeup_via_inject(self, umo: str):
        """构造伪造消息，直接调用 CQHttp 内部事件处理"""
        try:
            from aiocqhttp import Event as CQEvent
        except ImportError:
            raise RuntimeError(
                "未安装 aiocqhttp，本插件需要 "
                "aiocqhttp 适配器"
            )

        if self._cqhttp_bot is None:
            raise RuntimeError(
                "未获取到 CQHttp 实例"
                "（需要先收到至少一条消息）"
            )
        if not self._bot_qq_id:
            raise RuntimeError(
                "未检测到机器人 QQ 号"
                "（请在配置中填写 bot_qq_id "
                "或先发一条消息）"
            )

        # 解析 umo（用 rsplit 防止前面部分含有 : 导致错位）
        parts = umo.rsplit(":", 2)
        if len(parts) < 3:
            raise RuntimeError(
                f"无法解析 umo: {umo}"
            )
        session_id = parts[2]
        msg_type_str = parts[1]
        is_group = "Group" in msg_type_str

        prompt = self.wakeup_prompt

        # 构造伪造事件
        if is_group:
            if "_" in session_id:
                uid, gid = session_id.rsplit("_", 1)
            else:
                raise RuntimeError(
                    "非独立会话模式的群聊暂不支持"
                )
            payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": (
                    int(time.time()) % 2147483647
                ),
                "group_id": int(gid),
                "user_id": int(uid),
                "message": [
                    {
                        "type": "text",
                        "data": {"text": prompt},
                    }
                ],
                "raw_message": prompt,
                "font": 0,
                "sender": {
                    "user_id": int(uid),
                    "nickname": "wakeup",
                    "card": "",
                },
                "time": int(time.time()),
                "self_id": int(self._bot_qq_id),
            }
        else:
            payload = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": (
                    int(time.time()) % 2147483647
                ),
                "user_id": int(session_id),
                "message": [
                    {
                        "type": "text",
                        "data": {"text": prompt},
                    }
                ],
                "raw_message": prompt,
                "font": 0,
                "sender": {
                    "user_id": int(session_id),
                    "nickname": "wakeup",
                    "sex": "unknown",
                    "age": 0,
                },
                "time": int(time.time()),
                "self_id": int(self._bot_qq_id),
            }

        fake_event = CQEvent.from_payload(payload)
        if fake_event is None:
            raise RuntimeError(
                "CQEvent.from_payload 返回 None"
            )

        # 标记唤醒中
        self._waking_umos.add(umo)

        try:
            # 查找内部事件处理方法
            handler = getattr(
                self._cqhttp_bot, "_handle_event", None
            )
            if handler is None:
                handler = getattr(
                    self._cqhttp_bot,
                    "handle_event",
                    None,
                )
            if handler is None:
                candidates = [
                    a
                    for a in dir(self._cqhttp_bot)
                    if "event" in a.lower()
                ]
                raise RuntimeError(
                    f"CQHttp 无可用的事件处理方法，"
                    f"相关属性: {candidates}"
                )

            await handler(fake_event)

            logger.info(
                f"[wakeup] 📨 伪造消息已注入 pipeline | "
                f"umo={umo}"
            )
        except Exception:
            self._waking_umos.discard(umo)
            raise

        # 兜底清理唤醒标记
        async def _delayed_cleanup(u=umo):
            await asyncio.sleep(120)
            self._waking_umos.discard(u)

        asyncio.create_task(_delayed_cleanup())

    # ==================== 闹钟任务 ====================

    async def _alarm_task(
        self, umo: str, delay_seconds: int
    ):
        my_task = self.alarms.get(umo)

        try:
            await asyncio.sleep(delay_seconds)

            if self._terminated:
                return
            if umo in self._smashed_umos:
                self._smashed_umos.discard(umo)
                return

            logger.info(
                f"[wakeup] ⏰ 倒计时结束，准备唤醒 | "
                f"umo={umo}"
            )

            if not self._is_model_allowed(umo):
                return

            # 等待 bot 实例 + QQ 号就绪
            if (
                self._cqhttp_bot is None
                or not self._bot_qq_id
            ):
                logger.info(
                    "[wakeup] ⏳ 等待 Bot 实例和 QQ 号..."
                )
                await self._try_acquire_bot()
                if (
                    self._cqhttp_bot is None
                    or not self._bot_qq_id
                ):
                    # 最多等 2 分钟
                    for _ in range(24):
                        await asyncio.sleep(5)
                        if self._terminated:
                            return
                        if umo in self._smashed_umos:
                            self._smashed_umos.discard(
                                umo
                            )
                            return
                        await self._try_acquire_bot()
                        if (
                            self._cqhttp_bot
                            and self._bot_qq_id
                        ):
                            break
                if (
                    self._cqhttp_bot is None
                    or not self._bot_qq_id
                ):
                    logger.error(
                        f"[wakeup] 💀 等待超时，Bot 未就绪"
                        f" | umo={umo}"
                    )
                    if (
                        self.default_silence_minutes > 0
                        and not self._terminated
                    ):

                        async def _wait_fb(
                            u=umo, d=300
                        ):
                            await asyncio.sleep(3)
                            if (
                                u not in self.alarms
                                and not self._terminated
                            ):
                                await (
                                    self
                                    ._schedule_alarm_by_umo(
                                        u, d
                                    )
                                )

                        asyncio.create_task(_wait_fb())
                    return
                logger.info(
                    "[wakeup] ✅ Bot 已就绪，继续唤醒"
                )

            # 注入唤醒
            inject_ok = False
            for attempt in range(1, 4):
                if (
                    self._terminated
                    or umo in self._smashed_umos
                ):
                    return
                try:
                    await self._wakeup_via_inject(umo)
                    inject_ok = True
                    break
                except Exception as e:
                    logger.warning(
                        f"[wakeup] ⚠️ 注入第 "
                        f"{attempt}/3 次失败: "
                        f"{type(e).__name__}: {e}"
                    )
                    if attempt < 3:
                        await asyncio.sleep(5)

            if inject_ok:
                # 安全网：3 分钟内没新闹钟就补一个
                if self.default_silence_minutes > 0:

                    async def _safety(u=umo):
                        fb = (
                            self.default_silence_minutes
                            * 60
                        )
                        await asyncio.sleep(180)
                        if (
                            u not in self.alarms
                            and not self._terminated
                        ):
                            logger.warning(
                                f"[wakeup] ⚠️ 安全网触发，"
                                f"设兜底闹钟 | umo={u}"
                            )
                            await (
                                self._schedule_alarm_by_umo(
                                    u, fb
                                )
                            )

                    asyncio.create_task(_safety())
            else:
                # 全部失败：设兜底闹钟稍后重试
                logger.error(
                    f"[wakeup] 💀 注入 3 次均失败 | "
                    f"umo={umo}"
                )
                if (
                    self.default_silence_minutes > 0
                    and not self._terminated
                ):

                    async def _retry(
                        u=umo,
                        d=self.default_silence_minutes
                        * 60,
                    ):
                        await asyncio.sleep(3)
                        if (
                            u not in self.alarms
                            and not self._terminated
                        ):
                            logger.info(
                                f"[wakeup] 🔄 失败后设兜底"
                                f"闹钟 | umo={u}"
                            )
                            await (
                                self._schedule_alarm_by_umo(
                                    u, d
                                )
                            )

                    asyncio.create_task(_retry())

        except asyncio.CancelledError:
            raise
        finally:
            current = self.alarms.get(umo)
            if current is my_task:
                self.alarms.pop(umo, None)
                self._remove_alarm_record(umo)
            self._smashed_umos.discard(umo)

    # ==================== 模型白名单 ====================

    def _is_model_allowed(self, umo: str) -> bool:
        if not self.allowed_models:
            return True
        try:
            provider = None
            if hasattr(self.context, "session_manager"):
                session = (
                    self.context.session_manager.get_session(
                        umo
                    )
                )
                if session and getattr(
                    session, "provider_id", None
                ):
                    provider = self.context.get_provider(
                        session.provider_id
                    )
            if provider is None:
                provider = (
                    self.context.get_using_provider()
                )
            if provider is None:
                return False
            model = (provider.get_model() or "").lower()
            return any(
                a in model for a in self.allowed_models
            )
        except Exception:
            return False

    # ==================== 持久化 ====================

    def _save_alarm_records(self):
        try:
            with open(
                self.data_file, "w", encoding="utf-8"
            ) as f:
                json.dump(
                    self.alarm_records,
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning(f"[wakeup] 保存失败: {e}")

    def _load_alarm_records(self) -> Dict[str, float]:
        if not os.path.exists(self.data_file):
            return {}
        try:
            with open(
                self.data_file, "r", encoding="utf-8"
            ) as f:
                data = json.load(f)
            return {
                str(k): float(v)
                for k, v in data.items()
            }
        except Exception:
            return {}

    def _remove_alarm_record(self, umo: str):
        if umo in self.alarm_records:
            self.alarm_records.pop(umo, None)
            self._save_alarm_records()

    async def _restore_alarms(self):
        records = self._load_alarm_records()
        if not records:
            logger.info("[wakeup] 无需恢复的闹钟")
            return
        now = time.time()
        restored = overdue = 0
        for umo, target_ts in records.items():
            remaining = target_ts - now
            if remaining <= 0:
                logger.info(
                    f"[wakeup] ⏰ 过期闹钟，5s 后触发 | "
                    f"umo={umo}"
                )
                task = asyncio.create_task(
                    self._alarm_task(umo, 5)
                )
                self.alarms[umo] = task
                self.alarm_records[umo] = now + 5
                overdue += 1
            else:
                logger.info(
                    f"[wakeup] ♻️ 恢复闹钟 | umo={umo} | "
                    f"{int(remaining)}s "
                    f"({int(remaining) // 60}m)"
                )
                task = asyncio.create_task(
                    self._alarm_task(umo, int(remaining))
                )
                self.alarms[umo] = task
                self.alarm_records[umo] = target_ts
                restored += 1
        self._save_alarm_records()
        logger.info(
            f"[wakeup] 恢复完成 | 正常={restored} | "
            f"过期触发={overdue}"
        )
