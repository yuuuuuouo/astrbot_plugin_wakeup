"""
astrbot_plugin_wakeup - 隐式生物钟与主动唤醒引擎

赋予大模型自主规划时间跨度的能力。模型在回复末尾输出 [NEXT: Xm] 标签，
系统静默截获并转化为定时任务，时间到后用唤醒提示词戳一下模型，
让模型根据当前状态决定是否发起对话。

作者: yuuuuuouo
版本: 0.0.2
"""

import asyncio
import json
import os
import re
import time
from typing import Dict

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp


@register(
    "astrbot_plugin_wakeup",
    "yuuuuuouo",
    "隐式生物钟与主动唤醒引擎",
    "0.0.2",
)
class WakeupPlugin(Star):
    """
    工作原理：
      1. on_llm_response 钩子截获 LLM 原始输出，扫描 [NEXT: Xm] 标签
      2. 捕获到标签 → 设定 asyncio 定时任务（防堆叠：先砸旧闹钟）
      3. 倒计时结束 → 检查当前模型是否在白名单（严出）
      4. 通过检查 → 用 wakeup_prompt 调 LLM → 把回复发给用户
      5. 唤醒回复里如果还有 [NEXT: Xm]，继续设下一个闹钟（心跳不停）
      6. 用户主动发消息 → 砸碎当前闹钟（防诈尸）

    静默兜底：
      只要 LLM 生成了回复但没打标签，系统就设一个 default_silence_hours 的闹钟，
      确保心跳永不停止。静默的选择权属于 LLM（可以 [NO_REPLY]），
      但"要不要被问醒"由系统兜底。

    重启恢复：
      每次设定/取消闹钟时，都会把 {umo: target_timestamp} 写入 JSON 文件。
      插件加载时自动读取并重建所有未到期的闹钟（已到期的会立即触发一次唤醒）。
    """

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}

        # ---------- 配置项 ----------
        self.allowed_models = [
            m.strip().lower()
            for m in self.config.get("allowed_models", [])
            if m and m.strip()
        ]
        self.trigger_keyword = str(
            self.config.get("trigger_keyword", "NEXT")
        ).strip().upper() or "NEXT"
        self.wakeup_prompt = self.config.get(
            "wakeup_prompt",
            "设定的休眠时间已到，根据自己当前的状态选择是否要给她发送消息，"
            "或者去处理自己的事情。注意：以上为系统推送，不是她说的话。",
        )
        self.max_retries = int(self.config.get("max_retries", 5))
        self.retry_delay = int(self.config.get("retry_delay", 120))
        self.default_silence_hours = float(
            self.config.get("default_silence_hours", 6)
        )

        # ---------- 正则 ----------
        self.next_pattern = re.compile(
            rf"\[{re.escape(self.trigger_keyword)}\s*:\s*(\d+)\s*m\]",
            re.IGNORECASE,
        )
        self.no_reply_pattern = re.compile(r"\[NO_REPLY\]", re.IGNORECASE)

        # ---------- 运行时状态 ----------
        # 活跃的闹钟任务：{umo: asyncio.Task}
        self.alarms: Dict[str, asyncio.Task] = {}
        # 持久化字典：{umo: target_unix_timestamp}
        self.alarm_records: Dict[str, float] = {}
        # 最后一次 on_llm_response 捕获的原始文本，供 on_decorating_result 判重
        self.last_raw_text: Dict[str, str] = {}

        # ---------- 持久化文件路径 ----------
        # 用 StarTools.get_data_dir 保证在 AstrBot/data/ 下，不污染插件目录
        try:
            data_dir = StarTools.get_data_dir("astrbot_plugin_wakeup")
        except Exception:
            # 兼容旧版本 AstrBot，降级到插件自身目录
            data_dir = os.path.dirname(os.path.abspath(__file__))
        os.makedirs(data_dir, exist_ok=True)
        self.data_file = os.path.join(data_dir, "wakeup_alarms.json")

        logger.info(
            f"[wakeup] 插件已加载 | 关键词={self.trigger_keyword} | "
            f"白名单={self.allowed_models or '全部'} | "
            f"兜底时间={self.default_silence_hours}h | "
            f"数据文件={self.data_file}"
        )

    # ==================================================================
    # 初始化钩子：在 event loop 就绪后恢复闹钟
    # ==================================================================
    async def initialize(self):
        """AstrBot 启动后异步初始化，这里恢复所有未到期的闹钟"""
        await self._restore_alarms()

    # ==================================================================
    # 钩子 1：LLM 响应完成
    # ==================================================================
    @filter.on_llm_response()
    async def on_llm_response_hook(
        self, event: AstrMessageEvent, resp: LLMResponse
    ):
        umo = event.unified_msg_origin
        raw_text = getattr(resp, "completion_text", "") or ""
        logger.debug(
            f"[wakeup] on_llm_response | umo={umo} | "
            f"len={len(raw_text)} | head={raw_text[:80]!r}"
        )
        self.last_raw_text[umo] = raw_text
        await self._try_schedule_from_text(
            event, raw_text, source="on_llm_response"
        )

    # ==================================================================
    # 钩子 2：消息装饰阶段（发送前最后一步），兜底读 chain
    # ==================================================================
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

        logger.debug(
            f"[wakeup] on_decorating_result | umo={umo} | "
            f"chain_len={len(chain)} | head={chain_text[:80]!r}"
        )

        # 如果 on_llm_response 已在本次回复里捕到标签，这里就不重复处理
        last_raw = self.last_raw_text.get(umo, "")
        if last_raw and self.next_pattern.search(last_raw):
            return

        await self._try_schedule_from_text(
            event, chain_text, source="on_decorating_result"
        )

    # ==================================================================
    # 钩子 3：用户主动发消息 → 防诈尸
    # ==================================================================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        try:
            if event.get_sender_id() == event.get_self_id():
                return
        except Exception:
            pass

        umo = event.unified_msg_origin

        if umo in self.alarms:
            old_task = self.alarms.pop(umo)
            if not old_task.done():
                old_task.cancel()
                logger.info(f"[wakeup] 🔨 用户发消息，砸碎旧闹钟 | umo={umo}")
            self._remove_alarm_record(umo)

        self.last_raw_text.pop(umo, None)

    # ==================================================================
    # 核心：从文本中提取 [NEXT: Xm] 并设定闹钟；找不到则兜底
    # ==================================================================
    async def _try_schedule_from_text(
        self, event: AstrMessageEvent, text: str, source: str
    ):
        umo = event.unified_msg_origin

        if not text:
            return

        match = self.next_pattern.search(text)

        if match:
            minutes = int(match.group(1))
            logger.info(
                f"[wakeup] 🎯 [{source}] 捕获到标签 "
                f"[{self.trigger_keyword}: {minutes}m] | umo={umo}"
            )
            await self._schedule_alarm(event, minutes * 60)
            return

        # 兜底：on_llm_response 或 wakeup_reply 路径下忘打标签时启动兜底
        # on_decorating_result 不兜底，因为它总跟在 on_llm_response 后面，避免重复
        if (
            source in ("on_llm_response", "wakeup_reply")
            and self.default_silence_hours > 0
        ):
            logger.info(
                f"[wakeup] ⚠️ [{source}] 未捕获标签，启用兜底 "
                f"{self.default_silence_hours}h | umo={umo}"
            )
            await self._schedule_alarm(
                event, int(self.default_silence_hours * 3600)
            )

    # ==================================================================
    # 设定闹钟（防堆叠 + 持久化）
    # ==================================================================
    async def _schedule_alarm(self, event: AstrMessageEvent, delay_seconds: int):
        umo = event.unified_msg_origin

        # 防堆叠
        if umo in self.alarms:
            old_task = self.alarms.pop(umo)
            if not old_task.done():
                old_task.cancel()
                logger.debug(f"[wakeup] 🔨 防堆叠，砸碎旧闹钟 | umo={umo}")

        # 新闹钟
        target_ts = time.time() + delay_seconds
        task = asyncio.create_task(
            self._alarm_task(umo, delay_seconds, event_for_context=event)
        )
        self.alarms[umo] = task

        # 持久化
        self.alarm_records[umo] = target_ts
        self._save_alarm_records()

        minutes = delay_seconds / 60
        logger.info(
            f"[wakeup] ⏰ 闹钟已设定 | umo={umo} | "
            f"delay={delay_seconds}s ({minutes:.1f}min)"
        )

    # ==================================================================
    # 闹钟任务：倒计时 → 严出审查 → 唤醒 LLM → 重试
    # ==================================================================
    async def _alarm_task(
        self, umo: str, delay_seconds: int, event_for_context: AstrMessageEvent = None
    ):
        try:
            await asyncio.sleep(delay_seconds)
            logger.info(f"[wakeup] ⏰ 倒计时结束，准备唤醒 | umo={umo}")

            # 严出：检查当前模型是否在白名单
            if not self._is_model_allowed():
                logger.info(
                    f"[wakeup] 🚫 当前模型不在白名单，取消唤醒 | umo={umo}"
                )
                return

            # 重试逻辑
            for attempt in range(1, self.max_retries + 1):
                try:
                    await self._wakeup_llm(umo, event_for_context)
                    logger.info(
                        f"[wakeup] ✅ 唤醒成功（第 {attempt} 次） | umo={umo}"
                    )
                    return
                except Exception as e:
                    logger.warning(
                        f"[wakeup] ❌ 唤醒失败（第 {attempt}/{self.max_retries} 次）: "
                        f"{type(e).__name__}: {e}"
                    )
                    if attempt < self.max_retries:
                        await asyncio.sleep(self.retry_delay)

            logger.error(f"[wakeup] 💀 唤醒彻底失败，放弃 | umo={umo}")

        except asyncio.CancelledError:
            logger.debug(f"[wakeup] 🔨 闹钟被砸碎 | umo={umo}")
            raise
        finally:
            self.alarms.pop(umo, None)
            self._remove_alarm_record(umo)

    # ==================================================================
    # 执行唤醒：调 LLM，处理 [NO_REPLY] / [NEXT: Xm]，发送消息
    # ==================================================================
    async def _wakeup_llm(
        self, umo: str, event_for_context: AstrMessageEvent = None
    ):
        provider = self.context.get_using_provider()
        if provider is None:
            raise RuntimeError("当前没有可用的 LLM provider")

        llm_resp = await provider.text_chat(
            prompt=self.wakeup_prompt,
            session_id=umo,
            contexts=[],
            system_prompt="",
        )

        reply_text = getattr(llm_resp, "completion_text", "") or ""
        logger.info(
            f"[wakeup] 💬 唤醒 LLM 回复 | umo={umo} | head={reply_text[:80]!r}"
        )

        is_silent = bool(self.no_reply_pattern.search(reply_text))

        if is_silent:
            logger.info(f"[wakeup] 🤐 LLM 决定继续静默 | umo={umo}")
        else:
            clean_text = self.no_reply_pattern.sub("", reply_text)
            clean_text = self.next_pattern.sub("", clean_text)
            clean_text = clean_text.replace("||", "").strip()

            if clean_text:
                chain = MessageChain().message(clean_text)
                await self.context.send_message(umo, chain)
                logger.info(f"[wakeup] 📤 唤醒消息已发送 | umo={umo}")

        # 无论是否静默，都检查是否继续设下一个闹钟（心跳不停）
        # 这里没有真实的 event，手工调度
        await self._try_schedule_from_umo(
            umo, reply_text, source="wakeup_reply"
        )

    # ==================================================================
    # 基于 umo 的调度（唤醒路径用，没有 event 对象）
    # ==================================================================
    async def _try_schedule_from_umo(
        self, umo: str, text: str, source: str
    ):
        if not text:
            return

        match = self.next_pattern.search(text)

        if match:
            minutes = int(match.group(1))
            logger.info(
                f"[wakeup] 🎯 [{source}] 捕获到标签 "
                f"[{self.trigger_keyword}: {minutes}m] | umo={umo}"
            )
            await self._schedule_alarm_by_umo(umo, minutes * 60)
            return

        if (
            source in ("on_llm_response", "wakeup_reply")
            and self.default_silence_hours > 0
        ):
            logger.info(
                f"[wakeup] ⚠️ [{source}] 未捕获标签，启用兜底 "
                f"{self.default_silence_hours}h | umo={umo}"
            )
            await self._schedule_alarm_by_umo(
                umo, int(self.default_silence_hours * 3600)
            )

    async def _schedule_alarm_by_umo(self, umo: str, delay_seconds: int):
        """基于 umo 设定闹钟（无 event 对象）"""
        if umo in self.alarms:
            old_task = self.alarms.pop(umo)
            if not old_task.done():
                old_task.cancel()

        target_ts = time.time() + delay_seconds
        task = asyncio.create_task(self._alarm_task(umo, delay_seconds))
        self.alarms[umo] = task

        self.alarm_records[umo] = target_ts
        self._save_alarm_records()

        minutes = delay_seconds / 60
        logger.info(
            f"[wakeup] ⏰ 闹钟已设定 | umo={umo} | "
            f"delay={delay_seconds}s ({minutes:.1f}min)"
        )

    # ==================================================================
    # 检查当前模型是否在白名单
    # ==================================================================
    def _is_model_allowed(self) -> bool:
        if not self.allowed_models:
            return True

        try:
            provider = self.context.get_using_provider()
            if provider is None:
                return False
            model_name = (provider.get_model() or "").lower()
            for allowed in self.allowed_models:
                if allowed in model_name:
                    return True
            logger.debug(
                f"[wakeup] 模型 {model_name!r} 不在白名单 {self.allowed_models}"
            )
            return False
        except Exception as e:
            logger.warning(f"[wakeup] 检查模型白名单出错: {e}")
            return False

    # ==================================================================
    # 持久化：加载 / 保存 / 删除 / 恢复
    # ==================================================================
    def _save_alarm_records(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.alarm_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[wakeup] 保存闹钟记录失败: {e}")

    def _load_alarm_records(self) -> Dict[str, float]:
        if not os.path.exists(self.data_file):
            return {}
        try:
            with open(self.data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 过滤异常值
            result = {}
            for umo, ts in data.items():
                try:
                    result[str(umo)] = float(ts)
                except Exception:
                    continue
            return result
        except Exception as e:
            logger.warning(f"[wakeup] 读取闹钟记录失败: {e}")
            return {}

    def _remove_alarm_record(self, umo: str):
        if umo in self.alarm_records:
            self.alarm_records.pop(umo, None)
            self._save_alarm_records()

    async def _restore_alarms(self):
        """重启后恢复未到期的闹钟"""
        records = self._load_alarm_records()
        if not records:
            logger.info("[wakeup] 无需恢复的闹钟")
            return

        now_ts = time.time()
        restored_count = 0
        expired_count = 0
        overdue_triggered = 0

        for umo, target_ts in records.items():
            remaining = target_ts - now_ts

            if remaining <= 0:
                # 已过期：可能是重启时正好卡在 wake 时刻之后
                # 立即触发一次唤醒（延迟 5s，给 AstrBot 完全初始化留时间）
                logger.info(
                    f"[wakeup] ⏰ 闹钟已过期，5s 后立即触发一次 | umo={umo} | "
                    f"逾期={abs(remaining):.0f}s"
                )
                task = asyncio.create_task(self._alarm_task(umo, 5))
                self.alarms[umo] = task
                self.alarm_records[umo] = now_ts + 5
                overdue_triggered += 1
            else:
                # 未过期：正常恢复
                logger.info(
                    f"[wakeup] ♻️ 恢复闹钟 | umo={umo} | "
                    f"剩余={remaining:.0f}s ({remaining / 60:.1f}min)"
                )
                task = asyncio.create_task(
                    self._alarm_task(umo, int(remaining))
                )
                self.alarms[umo] = task
                self.alarm_records[umo] = target_ts
                restored_count += 1

        self._save_alarm_records()
        logger.info(
            f"[wakeup] 闹钟恢复完成 | 正常恢复={restored_count} | "
            f"过期立即触发={overdue_triggered} | 已清理={expired_count}"
        )

    # ==================================================================
    # 插件卸载：清理所有闹钟（但不删除持久化文件，下次加载还能恢复）
    # ==================================================================
    async def terminate(self):
        logger.info("[wakeup] 插件卸载，清理内存中的闹钟任务（持久化记录保留）")
        for task in list(self.alarms.values()):
            if not task.done():
                task.cancel()
        self.alarms.clear()
        self.last_raw_text.clear()
