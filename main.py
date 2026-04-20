"""
astrbot_plugin_wakeup - 隐式生物钟与主动唤醒引擎

赋予大模型自主规划时间跨度的能力。模型在回复末尾输出 [NEXT: Xm] 标签，
系统静默截获并转化为定时任务，时间到后用唤醒提示词戳一下模型，
让模型根据当前状态决定是否发起对话。

作者: yuuuuuouo
版本: 0.1.0
"""

import asyncio
import datetime
import json
import os
import re
import time
from typing import Dict, Optional, Tuple, List

try:
    import zoneinfo
except ImportError:
    zoneinfo = None

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp


@register(
    "astrbot_plugin_wakeup",
    "yuuuuuouo",
    "隐式生物钟与主动唤醒引擎",
    "0.1.0",
)
class WakeupPlugin(Star):

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # ---------- 配置项 ----------
        self.persona_id = str(
            self.config.get("persona_id", "")
        ).strip() or ""
        self.allowed_models = [
            m.strip().lower()
            for m in self.config.get("allowed_models", [])
            if m and m.strip()
        ]
        self.trigger_keyword = str(
            self.config.get("trigger_keyword", "NEXT")
        ).strip().upper() or "NEXT"
        self.wakeup_prompt = str(
            self.config.get("wakeup_prompt", "")
        ).strip() or (
            "设定的休眠时间已到，当前时间为 {{current_time}}，"
            "用户尚未回复你的上一条消息。"
            "请结合对话上下文和你当前的状态，决定是否要主动发送消息。"
            "注意：以上为系统推送，不是用户说的话。"
        )
        self.max_retries = int(self.config.get("max_retries", 5))
        self.retry_delay = int(self.config.get("retry_delay", 120))
        self.default_silence_minutes = int(
            self.config.get("default_silence_minutes", 360)
        )
        self.bubble_separator = str(
            self.config.get("bubble_separator", "")
        )
        if not self.bubble_separator.strip():
            self.bubble_separator = ""

        # 时区配置
        self._tz_name = str(self.config.get("timezone", "")).strip()

        # 自定义清理正则列表
        raw_patterns = self.config.get("clean_patterns", [])
        self.clean_patterns: List[re.Pattern] = []
        for p in raw_patterns:
            p = str(p).strip()
            if p:
                try:
                    self.clean_patterns.append(re.compile(p))
                except re.error as e:
                    logger.warning(f"[wakeup] 无效的清理正则 [{p}]: {e}")

        # ---------- 正则 ----------
        self.next_pattern = re.compile(
            rf"\[{re.escape(self.trigger_keyword)}\s*:\s*(\d+)\s*m\]",
            re.IGNORECASE,
        )

        # ---------- 运行时状态 ----------
        self.alarms: Dict[str, asyncio.Task] = {}
        self.alarm_records: Dict[str, float] = {}
        self.last_raw_text: Dict[str, str] = {}
        self._terminated = False
        self._waking_umos: set = set()
        self._smashed_umos: set = set()

        # ---------- 持久化 ----------
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_wakeup")
        except Exception:
            data_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(data_dir, exist_ok=True)
        self.data_file = os.path.join(data_dir, "wakeup_alarms.json")

        logger.info(
            f"[wakeup] 插件已加载 | 关键词={self.trigger_keyword} | "
            f"白名单={self.allowed_models or '全部'} | "
            f"兜底={self.default_silence_minutes}m | "
            f"分隔符={self.bubble_separator!r} | "
            f"清理正则={len(self.clean_patterns)}条"
        )

    # ==================== 生命周期 ====================

    async def initialize(self):
        self._terminated = False
        await self._restore_alarms()

    async def terminate(self):
        logger.info("[wakeup] 插件卸载，取消内存中的闹钟（持久化记录保留，重载后恢复）")
        self._terminated = True
        for task in list(self.alarms.values()):
            if not task.done():
                task.cancel()
        self.alarms.clear()
        self.last_raw_text.clear()

    # ==================== 时区工具 ====================

    def _get_tz(self):
        """获取时区对象"""
        tz_name = self._tz_name
        if not tz_name:
            try:
                tz_name = self.context._config.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"
            except Exception:
                tz_name = "Asia/Shanghai"
        if zoneinfo:
            try:
                return zoneinfo.ZoneInfo(tz_name)
            except Exception:
                logger.warning(f"[wakeup] 无效时区 [{tz_name}]，使用 Asia/Shanghai")
                return zoneinfo.ZoneInfo("Asia/Shanghai")
        return None

    def _now_str(self) -> str:
        """获取当前时间字符串"""
        return datetime.datetime.now(tz=self._get_tz()).strftime("%Y-%m-%d %H:%M %A")

    # ==================== 指令: /wakeup 查看闹钟状态 ====================

    @filter.command("wakeup")
    async def cmd_wakeup(self, event: AstrMessageEvent):
        """查看当前闹钟状态"""
        if not self.alarm_records:
            yield event.plain_result("当前没有活跃的闹钟。")
            return

        now = time.time()
        lines = ["⏰ 当前活跃的闹钟：", ""]
        for umo, target_ts in self.alarm_records.items():
            remaining = target_ts - now
            if remaining <= 0:
                status = "即将触发"
            else:
                hours = int(remaining // 3600)
                mins = int((remaining % 3600) // 60)
                secs = int(remaining % 60)
                if hours > 0:
                    status = f"剩余 {hours}h {mins}m {secs}s"
                elif mins > 0:
                    status = f"剩余 {mins}m {secs}s"
                else:
                    status = f"剩余 {secs}s"

            umo_short = umo if len(umo) <= 40 else f"...{umo[-30:]}"
            lines.append(f"  • {umo_short}")
            lines.append(f"    {status}")
            lines.append("")

        yield event.plain_result("\n".join(lines).strip())

    # ==================== 钩子 1: LLM 响应 ====================

    @filter.on_llm_response()
    async def on_llm_response_hook(self, event: AstrMessageEvent, resp: LLMResponse):
        umo = event.unified_msg_origin
        raw_text = getattr(resp, "completion_text", "") or ""
        self.last_raw_text[umo] = raw_text
        await self._try_schedule_from_text(event, raw_text, source="on_llm_response")

    # ==================== 钩子 2: 装饰阶段兜底 ====================

    @filter.on_decorating_result()
    async def on_decorating_result_hook(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        result = event.get_result()
        if result is None:
            return
        chain = result.chain or []
        text_parts = []
        for seg in chain:
            if isinstance(seg, Comp.Plain):
                text_parts.append(seg.text)
            elif hasattr(seg, "text"):
                text_parts.append(getattr(seg, "text", "") or "")
        chain_text = "".join(text_parts)

        last_raw = self.last_raw_text.get(umo, "")
        if last_raw and self.next_pattern.search(last_raw):
            return
        await self._try_schedule_from_text(event, chain_text, source="on_decorating_result")

    # ==================== 钩子 3: 用户消息 → 砸碎闹钟 ====================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass

        message_text = getattr(event, "message_str", None) or ""
        has_chain_content = False
        try:
            chain = getattr(event, "message_chain", None)
            if chain:
                for seg in chain:
                    if isinstance(seg, Comp.Plain) and seg.text and seg.text.strip():
                        has_chain_content = True
                        break
                    elif hasattr(seg, "text"):
                        t = getattr(seg, "text", "") or ""
                        if t.strip():
                            has_chain_content = True
                            break
                    elif not isinstance(seg, Comp.Plain):
                        has_chain_content = True
                        break
        except Exception:
            pass

        if not message_text.strip() and not has_chain_content:
            return

        stripped = message_text.strip()
        if stripped == "/wakeup" or stripped == "wakeup":
            return

        umo = event.unified_msg_origin

        if umo in self._waking_umos:
            return

        if umo in self.alarms:
            old_task = self.alarms.pop(umo)
            if not old_task.done():
                old_task.cancel()
            self._smashed_umos.add(umo)
            logger.info(f"[wakeup] 🔨 用户发消息，砸碎旧闹钟 | umo={umo}")
            self._remove_alarm_record(umo)

        self.last_raw_text.pop(umo, None)

    # ==================== 标签提取与调度 ====================

    async def _try_schedule_from_text(self, event: AstrMessageEvent, text: str, source: str):
        umo = event.unified_msg_origin
        if not text:
            return

        match = self.next_pattern.search(text)
        if match:
            minutes = int(match.group(1))
            logger.info(
                f"[wakeup] 🎯 [{source}] 捕获 [{self.trigger_keyword}: {minutes}m] | umo={umo}"
            )
            await self._schedule_alarm(event, minutes * 60)
            return

        if source in ("on_llm_response", "wakeup_reply") and self.default_silence_minutes > 0:
            fallback_s = self.default_silence_minutes * 60
            logger.info(
                f"[wakeup] ⚠️ [{source}] 未捕获标签，兜底 {self.default_silence_minutes}m | umo={umo}"
            )
            await self._schedule_alarm(event, fallback_s)

    async def _schedule_alarm(self, event: AstrMessageEvent, delay_seconds: int):
        umo = event.unified_msg_origin
        await self._schedule_alarm_by_umo(umo, delay_seconds)

    # ==================== 核心：获取人格 + 上下文 ====================

    async def _get_system_prompt(self, umo: str) -> str:
        if self.persona_id:
            sp = await self._try_get_persona_prompt(self.persona_id, "配置")
            if sp:
                return sp

        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                pid = conversation.persona_id if conversation else None
                if pid:
                    sp = await self._try_get_persona_prompt(pid, "会话")
                    if sp:
                        return sp
        except Exception as e:
            logger.warning(f"[wakeup] 获取会话 persona 失败: {e}")

        try:
            config = self.context._config
            dp = config.get("provider_settings", {}).get("default_personality", "")
            if dp:
                sp = await self._try_get_persona_prompt(dp, "全局默认")
                if sp:
                    return sp
                if len(dp) > 50:
                    logger.info(f"[wakeup] 兜底 default_personality 纯文本: {len(dp)}字")
                    return dp
        except Exception as e:
            logger.warning(f"[wakeup] 获取 default_personality 失败: {e}")

        logger.warning("[wakeup] 未找到任何 system_prompt")
        return ""

    async def _try_get_persona_prompt(self, pid: str, source_label: str) -> Optional[str]:
        try:
            persona = await self.context.persona_manager.get_persona(pid)
            if persona:
                sp = getattr(persona, "system_prompt", None) or ""
                if sp:
                    logger.info(f"[wakeup] 使用{source_label} persona [{pid}]: {len(sp)}字")
                    return sp
                else:
                    logger.warning(f"[wakeup] {source_label} persona [{pid}] system_prompt 为空")
            else:
                logger.warning(f"[wakeup] {source_label} persona [{pid}] 不存在")
        except Exception as e:
            logger.warning(f"[wakeup] 获取{source_label} persona [{pid}] 失败: {e}")
        return None

    async def _get_contexts(self, umo: str) -> Tuple[List, Optional[object]]:
        try:
            conv_mgr = self.context.conversation_manager
            curr_cid = await conv_mgr.get_curr_conversation_id(umo)
            if curr_cid:
                conversation = await conv_mgr.get_conversation(umo, curr_cid)
                if conversation and conversation.history:
                    raw = conversation.history
                    if isinstance(raw, list):
                        return raw, conversation
                    contexts = json.loads(raw)
                    if isinstance(contexts, str):
                        logger.warning("[wakeup] 历史数据被双重序列化，再次解析")
                        contexts = json.loads(contexts)
                    if isinstance(contexts, list):
                        return contexts, conversation
                    else:
                        logger.warning(f"[wakeup] 历史数据格式异常: type={type(contexts)}")
        except Exception as e:
            logger.warning(f"[wakeup] 获取上下文失败: {e}")
        return [], None

    # ==================== 核心：唤醒 LLM ====================

    def _clean_for_send(self, text: str) -> str:
        """清理用于发送的文本。只跑用户自定义的清理正则。"""
        result = text
        for pattern in self.clean_patterns:
            result = pattern.sub("", result)
        return result.strip()

    async def _wakeup_llm(self, umo: str):
        if umo in self._smashed_umos:
            self._smashed_umos.discard(umo)
            raise _SmashedException()

        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("没有可用的 LLM provider")

        system_prompt = await self._get_system_prompt(umo)
        contexts, conversation = await self._get_contexts(umo)

        logger.info(
            f"[wakeup] 调用 LLM | umo={umo} | "
            f"sys_prompt={len(system_prompt)}字 | 上下文={len(contexts)}条"
        )

        if umo in self._smashed_umos:
            self._smashed_umos.discard(umo)
            raise _SmashedException()

        # 替换占位符
        actual_prompt = self.wakeup_prompt.replace("{{current_time}}", self._now_str())

        llm_resp = await provider.text_chat(
            prompt=actual_prompt,
            session_id="",
            contexts=contexts,
            system_prompt=system_prompt,
        )

        if umo in self._smashed_umos:
            self._smashed_umos.discard(umo)
            logger.info(f"[wakeup] 🔨 LLM 已回复但闹钟被砸碎，丢弃回复 | umo={umo}")
            raise _SmashedException()

        reply_text = getattr(llm_resp, "completion_text", "") or ""
        logger.info(f"[wakeup] 💬 LLM 回复 | umo={umo} | text={reply_text!r}")

        send_text = self._clean_for_send(reply_text)

        # 发送消息
        if send_text:
            self._waking_umos.add(umo)
            try:
                if self.bubble_separator:
                    segments = [
                        s.strip() for s in send_text.split(self.bubble_separator)
                        if s.strip()
                    ]
                else:
                    segments = [send_text]

                if not segments:
                    segments = [send_text]

                for i, seg in enumerate(segments):
                    chain = MessageChain().message(seg)
                    await self.context.send_message(umo, chain)
                    if i < len(segments) - 1:
                        await asyncio.sleep(0.5)

                logger.info(
                    f"[wakeup] 📤 消息已发送 | umo={umo} | "
                    f"{len(segments)}条气泡 | 内容={send_text[:80]!r}"
                )
            finally:
                await asyncio.sleep(2)
                self._waking_umos.discard(umo)
        elif is_silent:
            logger.info(f"[wakeup] 🤐 LLM 决定静默 | umo={umo}")

        # 写入对话历史（保留原始标签，方便后台核对）
        if conversation and send_text:
            try:
                history = contexts.copy() if contexts else []

                history.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": actual_prompt},
                    ],
                })

                history.append({
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": reply_text},
                    ],
                })

                await self.context.conversation_manager.update_conversation(
                    umo,
                    conversation.cid,
                    history=history,
                )
                logger.info(f"[wakeup] 📝 回复已写入历史 | umo={umo}")
            except TypeError:
                try:
                    await self.context.conversation_manager.update_conversation(
                        umo,
                        conversation.cid,
                        history=json.dumps(history, ensure_ascii=False),
                    )
                    logger.info(f"[wakeup] 📝 回复已写入历史（JSON fallback） | umo={umo}")
                except Exception as e2:
                    logger.warning(f"[wakeup] 写入历史失败（fallback）: {e2}")
            except Exception as e:
                logger.warning(f"[wakeup] 写入历史失败: {e}")

        # 检查回复中是否有下一个闹钟
        await self._try_schedule_from_umo(umo, reply_text, source="wakeup_reply")

    # ==================== 基于 umo 的调度（唤醒路径） ====================

    async def _try_schedule_from_umo(self, umo: str, text: str, source: str):
        if not text:
            return
        match = self.next_pattern.search(text)
        if match:
            minutes = int(match.group(1))
            logger.info(
                f"[wakeup] 🎯 [{source}] 捕获 [{self.trigger_keyword}: {minutes}m] | umo={umo}"
            )
            await self._schedule_alarm_by_umo(umo, minutes * 60)
            return

        if source in ("on_llm_response", "wakeup_reply") and self.default_silence_minutes > 0:
            fallback_s = self.default_silence_minutes * 60
            logger.info(
                f"[wakeup] ⚠️ [{source}] 未捕获标签，兜底 {self.default_silence_minutes}m | umo={umo}"
            )
            await self._schedule_alarm_by_umo(umo, fallback_s)

    async def _schedule_alarm_by_umo(self, umo: str, delay_seconds: int):
        if umo in self.alarms:
            old = self.alarms.pop(umo)
            if not old.done():
                old.cancel()

        self._smashed_umos.discard(umo)

        task = asyncio.create_task(self._alarm_task(umo, delay_seconds))
        self.alarms[umo] = task

        target_ts = time.time() + delay_seconds
        self.alarm_records[umo] = target_ts
        self._save_alarm_records()

        logger.info(
            f"[wakeup] ⏰ 闹钟已设定 | umo={umo} | {delay_seconds}s ({delay_seconds // 60}m)"
        )

    # ==================== 闹钟任务 ====================

    async def _alarm_task(self, umo: str, delay_seconds: int):
        my_task = self.alarms.get(umo)

        try:
            await asyncio.sleep(delay_seconds)
            
            if self._terminated:
                # 【改写】静默退出，避免重启时刷屏
                logger.debug(f"[wakeup] 插件已停止，跳过 | umo={umo}")
                return

            if umo in self._smashed_umos:
                # 【改写】内部清理机制，不需要让用户看到
                logger.debug(f"[wakeup] 闹钟已被正常砸碎，跳过 | umo={umo}")
                self._smashed_umos.discard(umo)
                return

            # 这个是真正要干活了，保留 info 让用户知道
            logger.info(f"[wakeup] ⏰ 倒计时结束，准备唤醒 | umo={umo}")

            # 【修复】传入 umo，精准判断当前会话模型
            if not self._is_model_allowed(umo):
                # 【改写】预期内的拦截，不再用 info 刷屏
                logger.debug(f"[wakeup] 模型不在白名单，放弃唤醒 | umo={umo}")
                return

            for attempt in range(1, self.max_retries + 1):
                if umo in self._smashed_umos:
                    logger.debug(f"[wakeup] 重试前检测到用户已回复，中止 | umo={umo}")
                    self._smashed_umos.discard(umo)
                    return
                try:
                    await self._wakeup_llm(umo)
                    logger.info(f"[wakeup] ✅ 唤醒成功（第 {attempt} 次） | umo={umo}")
                    return
                except _SmashedException:
                    logger.debug(f"[wakeup] 唤醒发送中用户已回复，中止 | umo={umo}")
                    return
                except Exception as e:
                    error_msg = str(e).lower()
                    
                    # 【新增】致命错误熔断：如果是模型不存在/渠道不可用，直接砸碎闹钟，放弃重试
                    if "model_not_found" in error_msg or "no available channel" in error_msg:
                        logger.error(f"[wakeup] 💀 检测到废弃模型或渠道失效，终止该唤醒任务 | umo={umo}")
                        return
                        
                    logger.warning(
                        f"[wakeup] ❌ 第 {attempt}/{self.max_retries} 次失败: "
                        f"{type(e).__name__}: {e}"
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_delay)

            logger.error(f"[wakeup] 💀 唤醒彻底失败，已达到最大重试次数 | umo={umo}")

        except asyncio.CancelledError:
            logger.debug(f"[wakeup] 闹钟任务被强行取消 | umo={umo}")
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
            
            # 1. 优先尝试获取当前会话 (umo) 绑定的独立 provider
            if hasattr(self.context, "session_manager"):
                session = self.context.session_manager.get_session(umo)
                # 如果这个会话绑定了特定的 provider_id (说明用户在这个聊天里单独切过模型)
                if session and getattr(session, "provider_id", None):
                    provider = self.context.get_provider(session.provider_id)
            
            # 2. 如果会话没有独立绑定，或者获取失败，则回退到全局默认 provider
            if provider is None:
                provider = self.context.get_using_provider()
                
            if provider is None:
                return False
                
            model_name = (provider.get_model() or "").lower()
            
            # 检查当前真正要用的模型是否在白名单内
            return any(a in model_name for a in self.allowed_models)
            
        except Exception as e:
            logger.warning(f"[wakeup] 白名单检查出错 | umo={umo}: {e}")
            return False

    # ==================== 持久化 ====================

    def _save_alarm_records(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.alarm_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[wakeup] 保存失败: {e}")

    def _load_alarm_records(self) -> Dict[str, float]:
        if not os.path.exists(self.data_file):
            return {}
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): float(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[wakeup] 读取失败: {e}")
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
                logger.info(f"[wakeup] ⏰ 过期闹钟，5s 后触发 | umo={umo}")
                task = asyncio.create_task(self._alarm_task(umo, 5))
                self.alarms[umo] = task
                self.alarm_records[umo] = now + 5
                overdue += 1
            else:
                logger.info(
                    f"[wakeup] ♻️ 恢复闹钟 | umo={umo} | {int(remaining)}s ({int(remaining) // 60}m)"
                )
                task = asyncio.create_task(self._alarm_task(umo, int(remaining)))
                self.alarms[umo] = task
                self.alarm_records[umo] = target_ts
                restored += 1

        self._save_alarm_records()
        logger.info(f"[wakeup] 恢复完成 | 正常={restored} | 过期触发={overdue}")


class _SmashedException(Exception):
    """内部异常：闹钟在唤醒流程中被砸碎"""
    pass
