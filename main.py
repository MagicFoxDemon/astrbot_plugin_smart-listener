# -*- coding: utf-8 -*-
"""
监听白名单群组中消息，通过 SLM 判断相关性，触发主 LLM 回复的插件
清理 LLM 回复中可能混入的奇怪前缀
解决 Dify 插件在无活跃会话时报错的问题
增加基于历史消息队列的更人性化相关性判断逻辑
@消息和 Bot 消息加入历史消息队列
"""

import asyncio
import json
import re
from collections import deque
from typing import Dict, List, Tuple

from astrbot.api.message_components import Plain
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig

MESSAGE_PREFIX_PATTERN = re.compile(r"^\[.*?\/.*?\]:\s*")
MESSAGE_HISTORY_LENGTH = 5


@register(
    "astrbot_plugin_smart_listener",
    "Smart Listener",
    "智能监听白名单群组消息 (Bot 消息加入历史, 兼容指令, SLM Prompt 优化)",
    "1.4.0",
    "https://github.com/MagicFoxDemon/astrbot_plugin_smart-listener",
)
class SmartListenerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        self.enabled: bool = self.config.get("enabled", True)
        self.relevance_checker_provider_id: str = self.config.get("relevance_checker_provider_id", "")
        self.relevance_checker_system_prompt: str = self.config.get(
            "relevance_checker_system_prompt",
            "You are an assistant that analyzes chat history. Given a sequence of messages, determine if the LAST message is relevant to the character '{character}', considering the preceding messages as context. Reply ONLY with 'yes' if it is relevant, and 'no' if it is not.".format(character=self.config.get("character", "Bot")),
        )
        self.group_whitelist: List[str] = [str(g) for g in self.config.get("group_whitelist", [])]

        # 读取配置文件中的 character 值，默认为 "Bot"
        self.character_name: str = self.config.get("character", "Bot")

        self._message_history: Dict[str, deque] = {}
        self._relevance_checker_provider = None

        logger.info("智能监听插件初始化完成！状态: {}".format("启用" if self.enabled else "禁用"))
        if self.enabled:
            logger.info(f"相关性判断 LLM 供应商 ID: '{self.relevance_checker_provider_id}'")
            logger.info(f"已配置的角色名: '{self.character_name}'")
            logger.info(f"已配置的群组白名单: {self.group_whitelist}")
            logger.info(f"历史消息队列长度: {MESSAGE_HISTORY_LENGTH}")

    def _get_relevance_checker_provider(self):
        if self._relevance_checker_provider:
            return self._relevance_checker_provider

        provider = None
        if self.relevance_checker_provider_id:
            provider = self.context.get_provider_by_id(self.relevance_checker_provider_id)
            if not provider:
                logger.warning(
                    f"配置文件指定的 SLM 供应商 ID '{self.relevance_checker_provider_id}' 未找到或未加载。智能监听功能将无法正常工作。"
                )
        else:
            logger.warning("配置文件未指定 SLM 供应商 ID (relevance_checker_provider_id)。智能监听功能将无法正常工作。")

        self._relevance_checker_provider = provider
        return provider

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message_filtered(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        group_id = event.get_group_id()

        if not group_id:
            logger.error("收到了群聊消息但 group_id 为 None，可能是平台适配器问题。忽略此消息。")
            return

        if str(group_id) not in self.group_whitelist:
            return

        message_text = event.get_message_str()
        sender_id = event.get_sender_id()
        self_id = event.get_self_id()

        if not message_text or sender_id == self_id:
            return

        cleaned_message_text = MESSAGE_PREFIX_PATTERN.sub("", message_text, count=1)
        if not cleaned_message_text:
            logger.debug("清理后消息文本为空，忽略。")
            return
        logger.debug(f"原始消息文本: '{message_text}' -> 清理后用于历史/LLM 的文本: '{cleaned_message_text}'")

        is_at_command = event.is_at_or_wake_command

        sender = "user"
        self._add_message_to_history(group_id, (sender, cleaned_message_text))

        if is_at_command:
            return

        slm_provider = self._get_relevance_checker_provider()

        if not slm_provider:
            return

        try:
            history_messages = self._get_history_messages(group_id)
            slm_prompt = self._build_slm_prompt(history_messages, (sender, cleaned_message_text))

            logger.debug(f"发送给 SLM 的 Prompt:\n{slm_prompt}")

            slm_response = await slm_provider.text_chat(
                prompt=slm_prompt,
                system_prompt=self.relevance_checker_system_prompt,
            )

            relevance_judgment = (
                slm_response.completion_text.strip().lower() if slm_response and slm_response.completion_text else ""
            )

            logger.debug(f"SLM 对最新消息 '{cleaned_message_text}' 的判断结果: '{relevance_judgment}'")

            if relevance_judgment == "yes":
                logger.info(f"SLM 判断最新消息 '{cleaned_message_text}' 与 Bot 有关，在白名单群组 {group_id} 触发主 LLM 回复。")

                umo = event.unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

                session_id_to_use = curr_cid
                conversation = None
                contexts = []

                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                    if conversation:
                        try:
                            contexts = json.loads(conversation.history)
                        except json.JSONDecodeError:
                            logger.error(f"解析当前活跃会话 {curr_cid} 的历史记录失败。")
                            contexts = []
                    else:
                        logger.warning(f"找到当前活跃会话 ID {curr_cid} 但无法获取会话对象。将尝试使用 ID 进行回复。")
                        pass
                else:
                    session_id_to_use = str(group_id)
                    logger.debug(f"当前 Origin {umo} 无活跃会话，使用群组 ID {group_id} 作为会话 ID {session_id_to_use}。")

                    fallback_conversation = await self.context.conversation_manager.get_conversation(
                        umo, session_id_to_use
                    )

                    if fallback_conversation:
                        conversation = fallback_conversation
                        try:
                            contexts = json.loads(conversation.history)
                        except json.JSONDecodeError:
                            logger.error(f"解析 fallback 会话 {session_id_to_use} 的历史记录失败。")
                            contexts = []
                    else:
                        logger.debug(f"Fallback 会话 ID {session_id_to_use} 不存在现有会话，将尝试创建新会话。")
                        pass

                yield event.request_llm(
                    prompt=cleaned_message_text,
                    session_id=session_id_to_use,
                    contexts=contexts,
                    conversation=conversation,
                )

                event.stop_event()
                logger.info("已触发主 LLM 回复并停止事件传播。")

            else:
                pass

        except Exception as e:
            logger.error(f"处理消息 '{cleaned_message_text}' 过程中发生异常 (SLM 判断或触发主 LLM): {str(e)}", exc_info=True)
            pass

    @filter.on_decorating_result()
    async def on_message_decorated(self, event: AstrMessageEvent):
        if not self.enabled:
            return

        group_id = None
        event_type = None

        if hasattr(event, "event_type"):
            event_type = event.event_type

        if event_type == filter.EventMessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()
        elif hasattr(event, "group_id"):
            group_id = event.group_id
        else:
            logger.debug(f"无法获取事件的 group_id，事件类型: {type(event)}，忽略此消息。")
            return

        if not group_id:
            logger.error(f"无法获取事件的 group_id，事件类型: {type(event)}。忽略此消息。")
            return

        if str(group_id) not in self.group_whitelist:
            return

        result = event.get_result()

        if not result or not result.chain:
            return

        bot_message_text = self._extract_text_from_message_chain(result.chain)

        if not bot_message_text:
            logger.debug("Bot 发送的消息为空，忽略。")
            return

        cleaned_bot_message_text = MESSAGE_PREFIX_PATTERN.sub("", bot_message_text, count=1)

        sender = self.character_name
        self._add_message_to_history(group_id, (sender, cleaned_bot_message_text))

        logger.debug(f"Bot 消息已加入群组 {group_id} 的历史队列。消息内容: '{cleaned_bot_message_text}'")

        for component in result.chain:
            if isinstance(component, Plain):
                original_text = component.text
                cleaned_text = MESSAGE_PREFIX_PATTERN.sub("", original_text, count=1)
                if original_text != cleaned_text:
                    component.text = cleaned_text

    def _add_message_to_history(self, group_id: str, message: Tuple[str, str]):
        sender, message_text = message
        if not message_text:
            logger.warning("尝试添加到历史的消息文本为空，忽略。")
            return

        if group_id not in self._message_history:
            self._message_history[group_id] = deque(maxlen=MESSAGE_HISTORY_LENGTH)

        self._message_history[group_id].append(message)

    def _get_history_messages(self, group_id: str) -> List[Tuple[str, str]]:
        if group_id not in self._message_history:
            return []

        return list(self._message_history[group_id])

    def _build_slm_prompt(self, history_messages: List[Tuple[str, str]], latest_message: Tuple[str, str]) -> str:
        slm_prompt_parts = ["Chat History:"]
        if not history_messages:
            slm_prompt_parts.append("None (This is the start of a new potential conversation thread).")
        else:
            for i, (sender, msg) in enumerate(history_messages):
                slm_prompt_parts.append(f"{i + 1}. {sender.capitalize()}: {msg}")

        latest_sender, latest_msg = latest_message
        slm_prompt_parts.append(f"\nLatest Message: {latest_sender.capitalize()}: {latest_msg}")
        slm_prompt_parts.append(
            "\nConsidering the chat history above, is the LAST message relevant to the character '{character}'? Reply ONLY with 'yes' or 'no'.".format(character=self.character_name)
        )

        return "\n".join(slm_prompt_parts)

    def _extract_text_from_message_chain(self, message_chain: List) -> str:
        text = ""
        for component in message_chain:
            if isinstance(component, Plain):
                text += component.text
        return text

    async def terminate(self):
        logger.info("智能监听插件正在停止...")
        self._relevance_checker_provider = None
        self._message_history.clear()
        logger.info("智能监听插件已停止，历史消息队列已清空。")
        pass

