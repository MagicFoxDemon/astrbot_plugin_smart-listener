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
import re  # 导入 re 模块
from collections import deque  # 导入 deque
from typing import Dict, List  # 导入 Dict 和 List 类型提示

# 导入消息组件 Plain
from astrbot.api.message_components import Plain

# 使用 astrbot 提供的 logger 接口
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
# 显式导入一些类
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig


# 定义一个正则表达式，用于匹配并移除消息开头可能出现的 [发送者/时间]: 前缀
MESSAGE_PREFIX_PATTERN = re.compile(r"^\[.*?\/.*?\]:\s*")

# 定义历史消息队列的最大长度
MESSAGE_HISTORY_LENGTH = 5


@register(
    "astrbot_plugin_smart_listener",
    "Smart Listener",  # 插件名称
    "智能监听白名单群组消息 (Bot消息加入历史, 兼容指令)",  # 插件描述
    "1.2.2",  # 版本号
    "https://github.com/MagicFoxDemon/astrbot_plugin_smart-listener",  # 仓库地址
)
class SmartListenerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        # 从配置文件加载配置
        self.enabled: bool = self.config.get("enabled", True)
        self.relevance_checker_provider_id: str = self.config.get("relevance_checker_provider_id", "")
        # SLM 系统提示词，用于判断消息是否相关
        self.relevance_checker_system_prompt: str = self.config.get(
            "relevance_checker_system_prompt",
            "You are an assistant that analyzes chat history. Given a sequence of messages, determine if the LAST message is relevant to the character 'Bot', considering the preceding messages as context. Reply ONLY with 'yes' if it is relevant, and 'no' if it is not.",
        )
        # 读取群组白名单列表，并确保元素是字符串
        self.group_whitelist: List[str] = [str(g) for g in self.config.get("group_whitelist", [])]

        # 存储每个群组的消息历史队列 {group_id: deque([msg1, msg2, ...])}
        # 队列中存储的是清理后的消息文本
        self._message_history: Dict[str, deque] = {}

        # 获取用于相关性判断的 SLM 供应商实例 (初始化时不检查是否找到，在需要使用时再检查)
        self._relevance_checker_provider = None

        logger.info("智能监听插件初始化完成！状态: {}".format("启用" if self.enabled else "禁用"))
        if self.enabled:
            logger.info(f"相关性判断 LLM 供应商 ID: '{self.relevance_checker_provider_id}'")
            logger.info(f"已配置的群组白名单: {self.group_whitelist}")
            logger.info(f"历史消息队列长度 (用于 SLM 判断): {MESSAGE_HISTORY_LENGTH}")

    # Helper 函数，用于获取相关性判断的 SLM 供应商实例
    def _get_relevance_checker_provider(self):
        """根据配置文件指定的 ID 获取 SLM 供应商实例"""
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

    # 监听群聊消息
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    # 优先级可以根据需要调整
    # @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=5)
    async def on_group_message_filtered(self, event: AstrMessageEvent):
        """监听群聊消息，判断是否在白名单、是否为@消息，并将 Bot 消息加入历史"""

        if not self.enabled:
            return

        group_id = event.get_group_id()

        if not group_id:
            logger.error("收到了群聊消息但 group_id 为 None，可能是平台适配器问题。忽略此消息。")
            return

        # 检查群组 ID 是否在白名单中
        if str(group_id) not in self.group_whitelist:
            return

        message_text = event.get_message_str()

        # 如果消息内容为空，或者发送者是机器人自己，则忽略
        if not message_text or event.get_sender_id() == event.get_self_id():
            return

        # --- 清理消息文本中的前缀（用于存入历史和发送给 SLM）---
        cleaned_message_text = MESSAGE_PREFIX_PATTERN.sub("", message_text, count=1)
        if not cleaned_message_text:
            logger.debug("清理后消息文本为空，忽略。")
            return
        # logger.debug(f"原始消息文本: '{message_text}' -> 清理后用于历史/LLM的文本: '{cleaned_message_text}'")  # 避免日志过多

        # --- 修改：@消息也加入历史，但不触发回复 ---
        is_at_command = event.is_at_or_wake_command

        # 先将清理后的消息加入历史
        self._add_message_to_history(group_id, cleaned_message_text)

        # 如果是@消息，则直接返回，不进行后续处理
        if is_at_command:
            # logger.debug("@消息已加入历史，但不触发回复。")  # 避免日志过多
            return  # 停止当前流程

        # --- 获取 SLM，判断相关性，触发主 LLM 回复 ---
        slm_provider = self._get_relevance_checker_provider()

        if not slm_provider:
            return  # 警告已在 _get_relevance_checker_provider 中记录

        try:
            # --- 构建包含历史和最新消息的 Prompt 给 SLM ---
            history_messages = self._get_history_messages(group_id)
            slm_prompt = self._build_slm_prompt(history_messages, cleaned_message_text)

            logger.debug(f"发送给 SLM 的 Prompt:\n{slm_prompt}")

            # 调用 SLM 进行相关性判断
            slm_response = await slm_provider.text_chat(
                prompt=slm_prompt,
                system_prompt=self.relevance_checker_system_prompt,
            )

            relevance_judgment = (
                slm_response.completion_text.strip().lower() if slm_response and slm_response.completion_text else ""
            )

            logger.debug(f"SLM 对最新消息 '{cleaned_message_text}' 的判断结果: '{relevance_judgment}'")

            # 如果 SLM 判断消息与 Bot 有关 (回复为 "yes")
            if relevance_judgment == "yes":
                logger.info(f"SLM 判断最新消息 '{cleaned_message_text}' 与 Bot 有关，在白名单群组 {group_id} 触发主 LLM 回复。")

                # 获取当前会话 ID 和会话对象，用于 request_llm 提供上下文 (解决 Dify 问题)
                umo = event.unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

                session_id_to_use = curr_cid  # 默认使用当前活跃的会话 ID
                conversation = None
                contexts = []

                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                    if conversation:
                        try:
                            contexts = json.loads(conversation.history)
                            # logger.debug(f"为当前活跃会话 ID {curr_cid} 找到现有会话历史给主LLM。")  # 避免日志过多
                        except json.JSONDecodeError:
                            logger.error(f"解析当前活跃会话 {curr_cid} 的历史记录失败。")
                            contexts = []
                    else:
                        logger.warning(f"找到当前活跃会话 ID {curr_cid} 但无法获取会话对象。将尝试使用 ID 进行回复。")
                        pass  # contexts 和 conversation 保持初始的空值
                else:
                    # 如果没有找到当前活跃的会话 ID
                    # 使用群组 ID 作为 fallback 的会话 ID (字符串形式)
                    session_id_to_use = str(group_id)
                    logger.debug(f"当前 Origin {umo} 无活跃会话，使用群组 ID {group_id} 作为会话 ID {session_id_to_use}。")

                    # 尝试加载以群组 ID 为会话 ID 的会话对象和历史
                    fallback_conversation = await self.context.conversation_manager.get_conversation(
                        umo, session_id_to_use
                    )

                    if fallback_conversation:
                        conversation = fallback_conversation
                        try:
                            contexts = json.loads(conversation.history)
                            # logger.debug(f"为 fallback 会话 ID {session_id_to_use} 找到现有会话历史给主LLM。")  # 避免日志过多
                        except json.JSONDecodeError:
                            logger.error(f"解析 fallback 会话 {session_id_to_use} 的历史记录失败。")
                            contexts = []
                    else:
                        logger.debug(f"Fallback 会话 ID {session_id_to_use} 不存在现有会话，将尝试创建新会话。")
                        pass  # contexts 和 conversation 保持初始的空值

                # 触发主 LLM 回复，使用清理后的文本作为 prompt
                # 总是传递 session_id_to_use，即使 contexts 和 conversation 为空/None
                yield event.request_llm(
                    prompt=cleaned_message_text,  # 使用清理后的文本作为当前消息输入给主LLM
                    session_id=session_id_to_use,  # 使用确定的会话 ID (可能是活跃 ID，也可能是群组 ID)
                    contexts=contexts,  # 提供给主LLM 的对话历史
                    conversation=conversation,  # 提供会话对象 (如果找到)
                    # func_tool_manager=self.context.get_llm_tool_manager(),
                    # system_prompt=""
                    # image_urls=[]
                )

                # 触发回复后，停止事件传播
                event.stop_event()
                logger.info("已触发主 LLM 回复并停止事件传播。")

            else:
                # SLM 判断为不相关，不触发回复，事件继续传播
                # logger.debug(f"SLM 判断最新消息不相关，不触发回复。消息: '{cleaned_message_text}'")  # 避免日志过多
                pass  # 事件自然流转

        except Exception as e:
            logger.error(f"处理消息 '{cleaned_message_text}' 过程中发生异常 (SLM 判断或触发主 LLM): {str(e)}", exc_info=True)
            pass  # 静默处理错误

    # 修改 on_decorating_result，直接处理 Bot 消息
    @filter.on_decorating_result()
    async def on_message_decorated(self, event: AstrMessageEvent):
        """在消息发送前/后处理消息，包括清理前缀和添加 Bot 消息到历史"""
        # 检查是否启用了插件
        if not self.enabled:
            return

        # 尝试获取 group_id 和 event_type，如果获取不到，则直接返回
        group_id = None
        event_type = None

        # 优先尝试从 event 对象中获取 event_type
        if hasattr(event, "event_type"):
            event_type = event.event_type

        # 如果 event_type 是 GROUP_MESSAGE，则尝试获取 group_id
        if event_type == filter.EventMessageType.GROUP_MESSAGE:
            group_id = event.get_group_id()
        elif hasattr(event, "group_id"):
            # 兼容其他类型的事件
            group_id = event.group_id
        else:
            # 如果是其他类型的事件，且无法获取 group_id，忽略
            logger.debug(f"无法获取事件的 group_id，事件类型: {type(event)}，忽略此消息。")
            return

        # 检查群组 ID 是否存在
        if not group_id:
            logger.error(f"无法获取事件的 group_id，事件类型: {type(event)}。忽略此消息。")
            return

        # 检查群组 ID 是否在白名单中
        if str(group_id) not in self.group_whitelist:
            return

        # 获取消息结果
        result = event.get_result()

        # 检查是否存在结果以及消息链
        if not result or not result.chain:
            return

        # 提取消息文本
        bot_message_text = self._extract_text_from_message_chain(result.chain)

        # 如果消息内容为空，则忽略
        if not bot_message_text:
            logger.debug("Bot 发送的消息为空，忽略。")
            return

        # 清理消息文本中的前缀 (Bot 消息不需要清理前缀，但为了统一处理，还是调用一下)
        cleaned_bot_message_text = MESSAGE_PREFIX_PATTERN.sub("", bot_message_text, count=1)

        # 将 Bot 消息加入历史队列
        self._add_message_to_history(group_id, cleaned_bot_message_text)

        logger.debug(f"Bot 消息已加入群组 {group_id} 的历史队列。消息内容: '{cleaned_bot_message_text}'")

        # --- 清理消息链中的前缀 (保持原有功能) ---
        for component in result.chain:
            if isinstance(component, Plain):
                original_text = component.text
                cleaned_text = MESSAGE_PREFIX_PATTERN.sub("", original_text, count=1)
                if original_text != cleaned_text:
                    component.text = cleaned_text
                    # logger.debug(f"在发送前清理消息链中的前缀。原始: '{original_text[:50]}...' 清理后: '{cleaned_text[:50]}...'")  # 避免日志过多

    def _add_message_to_history(self, group_id: str, message_text: str):
        """将消息添加到指定群组的历史消息队列"""
        if not message_text:
            logger.warning("尝试添加到历史的消息文本为空，忽略。")
            return

        if group_id not in self._message_history:
            self._message_history[group_id] = deque(maxlen=MESSAGE_HISTORY_LENGTH)

        self._message_history[group_id].append(message_text)
        # logger.debug(f"消息已添加到群组 {group_id} 的历史队列。队列长度: {len(self._message_history[group_id])}")  # 避免日志过多

    def _get_history_messages(self, group_id: str) -> List[str]:
        """获取指定群组的历史消息列表"""
        if group_id not in self._message_history:
            return []

        return list(self._message_history[group_id])

    def _build_slm_prompt(self, history_messages: List[str], latest_message: str) -> str:
        """构建发送给 SLM 的 Prompt"""
        slm_prompt_parts = ["Chat History:"]
        if not history_messages:
            slm_prompt_parts.append("None (This is the start of a new potential conversation thread).")
        else:
            for i, msg in enumerate(history_messages):
                slm_prompt_parts.append(f"{i + 1}. {msg}")

        slm_prompt_parts.append(f"\nLatest Message: {latest_message}")
        slm_prompt_parts.append(
            "\nConsidering the chat history above, is the LAST message relevant to the character 'Bot'? Reply ONLY with 'yes' or 'no'."
        )

        return "\n".join(slm_prompt_parts)

    def _extract_text_from_message_chain(self, message_chain: List) -> str:
        """从消息链中提取文本"""
        text = ""
        for component in message_chain:
            if isinstance(component, Plain):
                text += component.text
        return text

    # 插件停止时的清理逻辑
    async def terminate(self):
        """插件停止时的清理逻辑"""
        logger.info("智能监听插件正在停止...")
        self._relevance_checker_provider = None  # 清理缓存的供应商实例
        self._message_history.clear()  # 清理历史消息队列，释放内存
        logger.info("智能监听插件已停止，历史消息队列已清空。")
        pass  # 当前插件没有更复杂的清理需求
