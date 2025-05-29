# AstrBot 智能监听器 astrbot-plugin-smart-listener

## 🧠 项目简介

`astrbot-plugin-smart-listener` 是一个为 [AstrBot](https://github.com/Soulter/AstrBot) 设计的智能监听插件，旨在提升机器人在指定群组中对非 `@` 消息的响应策略。通过调用轻量语言模型，插件能够判断群聊消息与bot角色的相关性，仅在话题和bot的角色相关时触发机器人进行进行响应并回复，从而让bot更合时宜的回复群友的发言。

---

## ✨ 功能特性

- **群组白名单**：仅在配置中指定的群组启用。
- **非 `@` 消息处理**：增强 AstrBot 对未被提及消息的感知能力。
- **SLM 相关性判断**：通过可配置的小模型判断消息是否与特定主题相关。
- **上下文感知**：维持每个群组的近期消息历史，提供语境参考。
- **按需触发 LLM**：仅在 SLM 判断消息相关时才调用主语言模型。

---

## 🛠 安装

1. 如果插件已发布到 AstrBot 插件市场，您可以直接从市场安装。
2. 也可以将整个astrbot_plugin_smart-listenerk克隆到AstrBot/data/plugins/

**配置方式**
- **插件默认禁用。启用前需要指定判断相关性模型供应商，并根据你的bot的人格编写相关性判断提示词。**
- **可以使用默认的提示词，将名字修改为你的机器人的名字即可。**
- **在AstrBot的服务提供商中添加提供商后将提供商的ID填入relevance_checker_provider_id即可**
- **请确保 relevance_checker_provider_id 指向一个响应快、成本低的小型语言模型。**
- **group_whitelist 需填写群组的实际 ID。**

完成后，保存配置并让插件重载即可。

## ⚙️ 工作原理概述

插件启动后加载配置，包括白名单群组、SLM 提供者等。

每当 AstrBot 接收到群消息：

-   检查是否来自白名单群组且未被 @。
-   清理消息文本前缀，保证内容干净。
-   构建上下文 Prompt（群历史 + 当前消息）。
-   调用配置的 SLM 进行相关性判断。
-   更新该群的消息历史队列。
-   若 SLM 返回 yes，则触发主 LLM 进行回复处理。

## ⚠️ 注意事项

-   `relevance_checker_provider_id` 必须是一个在 AstrBot 中配置可用的 SLM 模型 ID。
-   `group_whitelist` 必须包含正确的群组 ID，否则插件将不会在任何群组中生效。
-   `relevance_checker_system_prompt` 应根据实际使用的角色/关键词定制。
-   消息历史队列为定长，仅提供短期上下文参考。可以在代码中修改消息队列的长度。但是考虑到使用的slm的上下文长度，不建议将队列设置的太长。
-   代码大部分都是AI写的。

## 🤝 贡献方式

欢迎你来参与这个项目的建设！

## 👩‍💻 作者

琴葉

## 📄 License

本项目采用 MIT License 许可。

