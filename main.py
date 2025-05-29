# -*- coding: utf-8 -*-
"""
监听白名单群组中非@消息，通过SLM判断相关性，触发主LLM回复的插件
清理LLM回复中可能混入的奇怪前缀
解决Dify插件在无活跃会话时报错的问题
增加基于历史消息队列的更人性化相关性判断逻辑
"""

import asyncio
import json
import re # 导入 re 模块
from collections import deque # 导入 deque
from typing import Dict, List # 导入 Dict 和 List 类型提示

# 导入消息组件 Plain
from astrbot.api.message_components import Plain

# 使用 astrbot 提供的 logger 接口
from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
# 显式导入一些类
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
# ProviderRequest 和 LLMResponse 的导入（如果直接调用 provider.text_chat 后需要处理原始响应）
# from astrbot.api.provider import ProviderRequest, LLMResponse


# 定义一个正则表达式，用于匹配并移除消息开头可能出现的 [发送者/时间]: 前缀
MESSAGE_PREFIX_PATTERN = re.compile(r"^\[.*?\/.*?\]:\s*")

# 定义历史消息队列的最大长度
MESSAGE_HISTORY_LENGTH = 5


@register(
    "astrbot_plugin_smart_listener",
    "琴葉", 
    "智能监听白名单群组消息并回复相关内容 (增加历史判断)",
    "1.1.0", 
    "https://github.com/MagicFoxDemon/astrbot_plugin_smart-listener"
)
class SmartListenerPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.context = context

        # 从配置文件加载配置
        self.enabled: bool = self.config.get("enabled", True)
        self.relevance_checker_provider_id: str = self.config.get("relevance_checker_provider_id", "")
        # SLM 系统提示词需要修改以适配自己的角色
        self.relevance_checker_system_prompt: str = self.config.get("relevance_checker_system_prompt", "You are an assistant that analyzes chat history. Given a sequence of messages ending with 'Latest Message:', determine if the 'Latest Message' is relevant to the character 'Astrbot', considering the preceding messages as context. Reply ONLY with 'yes' if it is relevant, and 'no' if it is not.")
        # 读取群组白名单列表，并确保元素是字符串
        self.group_whitelist: List[str] = [str(g) for g in self.config.get("group_whitelist", [])]

        # 存储每个群组的消息历史队列 {group_id: deque([msg1, msg2, ...])}
        # 队列中存储的是清理后的消息文本
        self._message_history: Dict[str, deque] = {}

        # 获取用于相关性判断的SLM供应商实例 (初始化时不检查是否找到，在需要使用时再检查)
        self._relevance_checker_provider = None

        logger.info("智能监听插件初始化完成！状态: {}".format("启用" if self.enabled else "禁用"))
        if self.enabled:
             logger.info(f"相关性判断LLM供应商ID: '{self.relevance_checker_provider_id}'")
             logger.info(f"已配置的群组白名单: {self.group_whitelist}")
             logger.info(f"历史消息队列长度 (用于SLM判断): {MESSAGE_HISTORY_LENGTH}")


    # Helper 函数，用于获取相关性判断的SLM供应商实例
    def _get_relevance_checker_provider(self):
        """根据配置文件指定的ID获取SLM供应商实例"""
        if self._relevance_checker_provider:
            return self._relevance_checker_provider

        provider = None
        if self.relevance_checker_provider_id:
            provider = self.context.get_provider_by_id(self.relevance_checker_provider_id)
            if not provider:
                logger.warning(f"配置文件指定的SLM供应商ID '{self.relevance_checker_provider_id}' 未找到或未加载。智能监听功能将无法正常工作。")
        else:
            logger.warning("配置文件未指定SLM供应商ID (relevance_checker_provider_id)。智能监听功能将无法正常工作。")

        self._relevance_checker_provider = provider
        return provider


    # 监听群聊消息
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    # 优先级可以根据需要调整
    # @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=5)
    async def on_group_message_filtered(self, event: AstrMessageEvent):
        """监听群聊消息，判断是否在白名单、是否为@消息及相关性，并触发回复"""

        if not self.enabled:
            return

        group_id = event.get_group_id()

        if not group_id:
             logger.error("收到了群聊消息但 group_id 为 None，可能是平台适配器问题。忽略此消息。")
             return

        # 检查群组ID是否在白名单中
        # 注意：self.group_whitelist 中的是字符串，group_id 可能是数字，需要转换
        if str(group_id) not in self.group_whitelist:
            return

        # 检查消息是否为 @了机器人 或包含了唤醒词的消息
        if event.is_at_or_wake_command:
            # logger.debug(f"消息 '{event.get_message_str()}' 是 @消息或唤醒词消息，由默认LLM处理。") # 避免日志过多
            return # 直接返回，让事件继续传播，由默认流程处理

        message_text = event.get_message_str()

        # 如果消息内容为空，或者发送者是机器人自己，则忽略
        if not message_text or event.get_sender_id() == event.get_self_id():
            return

        # --- 清理消息文本中的前缀（用于发送给SLM和主LLM作为Prompt，也用于存入历史）---
        cleaned_message_text = MESSAGE_PREFIX_PATTERN.sub("", message_text, count=1)
        # 如果清理后消息为空，忽略 (e.g., 消息只有前缀或空白)
        if not cleaned_message_text:
             logger.debug("清理后消息文本为空，忽略。")
             return
        # logger.debug(f"原始消息文本: '{message_text}' -> 清理后用于LLM/历史的文本: '{cleaned_message_text}'") # 避免日志过多


        # --- 获取SLM，判断相关性，触发主LLM回复 ---

        slm_provider = self._get_relevance_checker_provider()

        if not slm_provider:
            return # 警告已在 _get_relevance_checker_provider 中记录

        try:
            # --- 修改：构建包含历史和最新消息的Prompt给SLM ---
            # 获取当前群组的历史消息队列，如果不存在则创建一个新的deque (maxlen=MESSAGE_HISTORY_LENGTH)
            if group_id not in self._message_history:
                # 注意：这里maxlen是5，是为了存储【历史】消息，不包含当前消息
                self._message_history[group_id] = deque(maxlen=MESSAGE_HISTORY_LENGTH)

            # 获取当前的【历史】消息 (deque 会自动处理长度)
            history_messages = list(self._message_history[group_id]) # 转换为列表方便格式化

            # 构造发送给SLM的Prompt，包含历史消息和最新消息
            slm_prompt_parts = ["Chat History:"]
            if not history_messages:
                slm_prompt_parts.append("None (This is the start of a new potential conversation thread).")
            else:
                for i, msg in enumerate(history_messages):
                    slm_prompt_parts.append(f"{i + 1}. {msg}")

            slm_prompt_parts.append(f"\nLatest Message: {cleaned_message_text}") # 加入最新的消息
            # 修改这里，让Prompt更清晰地指导SLM判断
            slm_prompt_parts.append("\nConsidering the chat history above, is the Latest Message relevant to the character? Reply ONLY with 'yes' or 'no'.")

            slm_prompt = "\n".join(slm_prompt_parts)

            logger.debug(f"发送给SLM的Prompt:\n{slm_prompt}")


            # 调用SLM进行相关性判断
            # 使用更新后的系统提示词
            slm_response = await slm_provider.text_chat(
                prompt=slm_prompt, # 使用包含历史的Prompt
                system_prompt=self.relevance_checker_system_prompt, # 使用可能更新的系统提示词
            )

            relevance_judgment = slm_response.completion_text.strip().lower() if slm_response and slm_response.completion_text else ""

            logger.debug(f"SLM对最新消息 '{cleaned_message_text}' 的判断结果: '{relevance_judgment}'")

            # --- 无论SLM判断结果如何，将当前的清理后的消息加入历史队列 ---
            # 这是为了下一次判断提供上下文
            if group_id not in self._message_history:
                 # 再次检查并创建，以防万一，虽然理论上上面已经创建了
                 self._message_history[group_id] = deque(maxlen=MESSAGE_HISTORY_LENGTH)
            self._message_history[group_id].append(cleaned_message_text)
            # logger.debug(f"更新群组 {group_id} 的消息历史队列。当前队列长度: {len(self._message_history[group_id])}") # 避免日志过多
            # deque 的 maxlen 会自动处理挤出旧消息


            # 如果SLM判断消息与琴葉有关 (回复为 "yes")
            if relevance_judgment == "yes":
                logger.info(f"SLM判断最新消息 '{cleaned_message_text}' 与bot角色有关，在白名单群组 {group_id} 触发主LLM回复。")

                # 获取当前会话ID和会话对象，用于 request_llm 提供上下文 (解决Dify问题)
                umo = event.unified_msg_origin
                curr_cid = await self.context.conversation_manager.get_curr_conversation_id(umo)

                session_id_to_use = curr_cid # 默认使用当前活跃的会话ID
                conversation = None
                # 这个 contexts 是给主LLM用的，存储的是AstrBot会话管理中的历史，格式可能不同
                # 且可能比SLM用的历史要长
                contexts = []

                if curr_cid:
                    conversation = await self.context.conversation_manager.get_conversation(umo, curr_cid)
                    if conversation:
                        try:
                            contexts = json.loads(conversation.history)
                            # logger.debug(f"为当前活跃会话ID {curr_cid} 找到现有会话历史给主LLM。") # 避免日志过多
                        except json.JSONDecodeError:
                            logger.error(f"解析当前活跃会话 {curr_cid} 的历史记录失败。")
                            contexts = []
                    else:
                         logger.warning(f"找到当前活跃会话ID {curr_cid} 但无法获取会话对象。将尝试使用ID进行回复。")
                         pass # contexts 和 conversation 保持初始的空值
                else:
                    # 如果没有找到当前活跃的会话ID
                    # 使用群组ID作为 fallback 的会话ID (字符串形式)
                    session_id_to_use = str(group_id)
                    logger.debug(f"当前Origin {umo} 无活跃会话，使用群组ID {group_id} 作为会话ID {session_id_to_use}。")

                    # 尝试加载以群组ID为会话ID的会话对象和历史
                    fallback_conversation = await self.context.conversation_manager.get_conversation(umo, session_id_to_use)

                    if fallback_conversation:
                         conversation = fallback_conversation
                         try:
                             contexts = json.loads(conversation.history)
                             # logger.debug(f"为 fallback 会话ID {session_id_to_use} 找到现有会话历史给主LLM。") # 避免日志过多
                         except json.JSONDecodeError:
                             logger.error(f"解析 fallback 会话 {session_id_to_use} 的历史记录失败。")
                             contexts = []
                    else:
                         logger.debug(f"Fallback会话ID {session_id_to_use} 不存在现有会话，将尝试创建新会话。")
                         pass # contexts 和 conversation 保持初始的空值


                # 触发主LLM回复，使用清理后的文本作为 prompt
                # 总是传递 session_id_to_use，即使 contexts 和 conversation 为空/None
                # 传递给主LLM的Prompt是当前这条清理后的消息文本
                yield event.request_llm(
                    prompt=cleaned_message_text, # 使用清理后的文本作为当前消息输入给主LLM
                    session_id=session_id_to_use, # 使用确定的会话ID (可能是活跃ID，也可能是群组ID)
                    contexts=contexts, # 提供给主LLM的对话历史 (AstrBot会话管理中的)
                    conversation=conversation, # 提供会话对象 (如果找到)
                    # func_tool_manager=self.context.get_llm_tool_manager(),
                    # system_prompt=""
                    # image_urls=[]
                )

                # 触发回复后，停止事件传播
                event.stop_event()
                logger.info("已触发主LLM回复并停止事件传播。")

            else:
                # SLM判断为不相关，不触发回复，事件继续传播
                # logger.debug(f"SLM判断最新消息不相关，不触发回复。消息: '{cleaned_message_text}'") # 避免日志过多
                pass # 事件自然流转

        except Exception as e:
            logger.error(f"处理消息 '{cleaned_message_text}' 过程中发生异常 (SLM判断或触发主LLM): {str(e)}", exc_info=True)
            # 即使发生异常，也要尝试将消息加入历史，以便下次判断有更多上下文 (如果异常发生在加入历史之前)
            # 为了保险，再检查一次并尝试加入，避免队列丢失消息，但要注意异常可能导致重复加入
            # 这里的处理取决于异常发生在try块的哪个位置，简单起见，前面已经加了一次，这里不再重复加
            pass # 静默处理错误


    # 事件钩子，在发送消息前清理消息链中的前缀
    @filter.on_decorating_result()
    async def clean_message_prefix(self, event: AstrMessageEvent):
        """
        在消息发送前清理消息链中 Plain 消息可能包含的 [Sender/Time]: 前缀。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        for component in result.chain:
            if isinstance(component, Plain):
                original_text = component.text
                cleaned_text = MESSAGE_PREFIX_PATTERN.sub("", original_text, count=1)
                if original_text != cleaned_text:
                    component.text = cleaned_text
                    


    # 插件停止时的清理逻辑
    async def terminate(self):
        """插件停止时的清理逻辑"""
        logger.info("智能监听插件正在停止...")
        self._relevance_checker_provider = None # 清理缓存的供应商实例
        self._message_history.clear() # 清理历史消息队列，释放内存
        logger.info("智能监听插件已停止，历史消息队列已清空。")
        pass # 当前插件没有更复杂的清理需求

