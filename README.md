# 🎨 ComfyUI 小助手

> 基于 [lumingya/astrbot_plugin_comfyui_pro](https://github.com/lumingya/astrbot_plugin_comfyui_pro) 修改，感谢原作者 [lumingya](https://github.com/lumingya) 的优秀工作。

## 介绍

一个功能强大的 AstrBot 插件，旨在将你本地的 **ComfyUI** 无缝集成到聊天机器人中。

支持指令出图（`/画图`）以及 LLM 辅助出图（`/FF401`），支持多工作流热切换、任务队列、完善的权限控制和敏感词过滤。

## 🚀 核心优势：轻松使用你自己的工作流

本插件最大的特点就是让你几乎无缝地使用你在 ComfyUI 中已经搭建好的工作流。

### 一、导出你的工作流
  在你的工作流界面，点击菜单的 **`Save (API Format)`** 按钮，将工作流导出为 `.json` 文件。

### 二、找到关键节点 ID
记下你的工作流中 **输入** 和 **输出** 节点的 ID。开启开发者模式后，ID 会显示在每个节点的标题上方。
*   **输入节点 (Input ID)**: 通常是接收提示词的 `CLIP Text Encode` 节点。
*   **输出节点 (Output ID)**: 最终生成图像的 `Save Image` 或 `Preview Image` 节点。

### 三、放置并配置
1.  将导出的 `.json` 文件放入插件的 `workflow` 目录中。
    *   路径为: `data/plugin_data/astrbot_plugin_comfyui_helper/workflow/`
2.  进入插件设置，在"ComfyUI 工作流配置"中按以下格式填写：
    *   **servers**: 配置 ComfyUI 服务器地址，每行一个 `id;IP:端口`
    *   **workflows**: 配置工作流，每行一个 `id;工作流文件;正面节点ID;负面节点ID;输出节点ID;服务器ID`
    *   负面节点ID、输出节点ID 和 服务器ID 可留空，留空则分别为"不注入负面提示词"、"自动检测输出"、"使用 0 号服务器"
3.  示例配置：
    ```
    servers:
    0;127.0.0.1:8188
    1;192.168.5.12:8189

    workflows:
    0;pencil.json;8;3;;0
    1;z-image.json;6;7;;1
    ```
4.  **完成！** 现在你的机器人就可以使用这些工作流进行绘画了。

---

## ✨ 主要功能

### 🔌 ComfyUI 深度集成
*   **便捷工作流导入**: 完美支持 ComfyUI 的 API 格式工作流。
*   **多工作流热切换**: 通过管理员指令 `/comfy_use`，随时切换不同的工作流。
*   **工作流提示词拼接**: 用户输入的提示词会与工作流中已有的提示词自动拼接，用户提示词在前。
*   **智能种子注入**: 自动寻找种子节点并随机化，避免生成重复图片。

### 📋 任务队列
*   **多工人并行队列**: 每个 ComfyUI 节点对应一个工人，多节点并行处理任务，最大化利用算力。
*   **独立 Tagger 队列**: 图片反推标签使用独立队列，不与绘图任务互相阻塞。
*   **精确队列提示**: 队列位置计入正在执行的任务，新用户会收到准确的前排等待提示。

### 🤖 LLM 辅助翻译 (/FF401)
*   **自然语言转标签**: 通过 `/FF401 <描述>` 让 LLM 将中文描述转换为英文绘图提示词。
*   **内置 NSFW 过滤**: LLM 提示词内置审核机制，自动过滤不适合的内容。

### 🛡️ 完善的风控与权限
*   **分级违禁词过滤**: 内置 `Lite` 和 `Full` 两级敏感词库，支持中英文过滤，可为不同群组设置不同策略。
*   **白名单与全局锁定**: 可设置仅在白名单群组生效，或一键开启"全局锁定"。
*   **管理员特权**: 管理员可配置"无视冷却"、"无视白名单"、"无视敏感词"等超级权限。

---

## ⚙️ 详细配置说明

在 AstrBot 仪表盘 -> 插件 -> `ComfyUI小助手` 中点击设置：

### 1. 服务器配置 (Servers)
*   `servers`: 多行文本框，每行一个 ComfyUI 实例。
*   格式：`id;IP:端口;[token]`，`id` 为数字标识，`0` 号为默认服务器。
*   `token` 可选，用于 [ComfyUI-Login](https://github.com/TheMmfiy/ComfyUI-Login) 鉴权，会在 HTTP 请求头中携带 `Authorization: Bearer <token>`。
*   示例：
    ```
    0;127.0.0.1:8188
    1;192.168.5.12:8189
    2;192.168.5.13:8190;my_auth_token
    ```

### 2. 工作流配置 (Workflows)
*   `workflows`: 多行文本框，每行一个工作流。
*   格式：`id;工作流文件;正面节点ID;负面节点ID;输出节点ID;服务器ID`
*   说明：
    *   `id` 为数字标识，`0` 号为默认工作流
    *   `负面节点ID` 留空则不注入负面提示词
    *   `输出节点ID` 留空则自动检测
    *   `服务器ID` 留空则使用 `0` 号服务器
*   示例：
    ```
    0;pencil.json;8;3;;0
    1;z-image.json;6;7;;1
    ```

### 3. 临场切换工作流
*   在 `/画图` 指令中，可以在提示词前加工作流 ID：
    *   `/画图 猫` — 使用默认工作流 (ID 0)
    *   `/画图 1 猫` — 使用 ID 1 的工作流（本次临时）

### 3. 队列消息 (Queue Messages)
*   `Queue Delay Messages`: 当队列中有任务时显示的提示消息，每行一条，`{n}` 会被替换为待处理任务数。
*   `Drawing Prompt Messages`: 无队列时显示的提示消息，每行一条。

---

## 📖 指令与用法

### 直接指令
*   `/画图 <提示词>`: 以合并转发方式发送图片 + 提示词。

### 图片处理
*   `/tagger` (回复图片或附带图片): 调用 WD14 Tagger 反推图片标签，支持直接发送图片或回复引用消息中的图片。

### LLM 辅助
*   `/FF401 <描述>`: 用自然语言描述画面，LLM 自动翻译为英文提示词后出图。

### 管理指令 (仅管理员)
*   `/comfy_ls`: 列出所有已配置工作流。
*   `/comfy_use <ID>`: 切换默认工作流（如 `/comfy_use 1`）。
*   `/comfy_save <文件名> <JSON>`: 导入工作流。
*   `/comfy_add <节点ID> <步数>`: 步数覆盖设置。
*   `/comfy_lock on|off|status`: 动态切换全局锁定。
*   `/违禁级别 <none/lite/full>`: 调整当前群的敏感词拦截等级。
*   `/comfy帮助`: 查看所有可用指令。

---

## ❓ 常见问题 (FAQ)

**Q: LLM 回复了 `<pic prompt="...">`，但没有出图？**
A: 该插件已移除 LLM 自动绘图模式，出图需要手动使用 `/画图` 或 `/FF401` 指令。

**Q: 我新添加了 `.json` 工作流文件，怎么使用？**
A: 将文件放入 `workflow` 目录后，在插件设置的 `workflows` 文本框里新增一行配置即可，无需重载插件。

**Q: 生成的图片总是一样的？**
A: 插件会自动寻找并修改名为 `seed` 或 `noise_seed` 的参数。如果你的工作流使用了非常规的自定义种子节点，插件可能无法识别。

---

## 📂 目录结构

```
data/plugin_data/astrbot_plugin_comfyui_helper/
├── workflow/                        # 你的工作流文件
│   ├── workflow_api.json
│   └── my_custom_workflow.json
├── output/                          # 生成的图片历史
│   └── *.png
└── sensitive_words.json             # 敏感词配置

plugins/astrbot_plugin_comfyui_helper/  # 插件目录
├── main.py
├── comfyui_api.py
└── ...
```

---

## 🙏 致谢

本插件基于 [lumingya/astrbot_plugin_comfyui_pro](https://github.com/lumingya/astrbot_plugin_comfyui_pro) 修改，感谢原作者 [lumingya](https://github.com/lumingya) 的优秀工作。

### 本版本的修改

- 插件名称改为 "ComfyUI小助手"
- 数据目录改为 `astrbot_plugin_comfyui_helper`
- 简化了配置说明
