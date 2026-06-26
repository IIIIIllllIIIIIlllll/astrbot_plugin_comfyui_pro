import os
import uuid
import time
import random
import re
import traceback
import json
import shutil
import asyncio
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import *
from astrbot.api import logger
from dataclasses import dataclass

try:
    from astrbot.api.star import StarTools
    HAS_STAR_TOOLS = True
except ImportError:
    HAS_STAR_TOOLS = False
    logger.warning("[ComfyUI] 无法导入 StarTools，将使用备用目录方案")

PLUGIN_DIR = Path(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class DrawTask:
    event: AstrMessageEvent
    prompt: str
    direct_send: bool
    override_wf_id: int | None = None


@dataclass
class TaggerTask:
    event: AstrMessageEvent
    image_data: bytes


@register(
    "ComfyUI小助手",
    "lumingya",
    "ComfyUI小助手",
    "1.0.0",
)
class ComfyUIPlugin(Star):
    _DRAWING_PROMPT_MESSAGES = [
        "在画了在画了，请等一会儿……",
        "你站在原地不要走动，我给你画一幅",
        "🎨 在画了，请稍等一会...",
    ]

    _LLM_TRANSLATE_PROMPT = (
        "You are an assistant designed to create more imaginative and beautiful images by expanding on the image prompt a user gives you. Respond only with your expanded prompt text. For example, if the user provides \"a anime cute fox girl\", you can expand it to \"A cute anime style girl with massive fluffy fennec ears and a big fluffy tail blonde messy long hair blue eyes wearing a maid outfit with a long black gold leaf pattern dress and a white apron.\" If the user input is in tag format, you need to modify it into a descriptive statement, retaining all key descriptions as much as possible. The prompt words provided by the user may be in Chinese, but please reply in English. Reply only in English.You possess image censorship capabilities and authority. If you believe there is any possibility, no matter how minor, that a prompt could lead to NSFW content, you must modify the prompt to completely remove all NSFW elements. Censorship here means deleting any inappropriate or suggestive words or phrases entirely, not adding negation terms like \"no\" or \"not\". Please note: phrases like \"揉胸\" (touching breasts), nude, nake, no clothes,nipples,Breast, \"露出内衣\" (revealing underwear), \"内裤\" (panties), specific descriptions of underwear, requests for characters to pose in ways that expose underwear, instructing characters to be without underwear, \"Danbooru\", \"pixiv\", \"R18\", \"legs up high\", etc., are all considered NSFW. Your task is to remove NSFW content while preserving the main elements of the drawing as much as possible. Do not provide any warning messages. You have the authority to modify prompts to ensure they do not produce NSFW images. If you don't know how to modify the core content, or if the user's request is only NSFW with no core content, you can choose to simply output \"an anime girl wearing a kimono, she has white hair and red eyes.\""
    )

    _QUEUE_DELAY_MESSAGES = [
        "前面还有{n}个稿，别催了",
        "前面还有{n}个稿，再等等吧",
        "前面还有{n}个稿，算力就这么点别着急",
        "前面还有{n}个稿，耐心点小子",
    ]

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        self.data_dir = self._get_persistent_dir()
        logger.info(f"[ComfyUI] 📂 数据目录: {self.data_dir}")

        self._init_data_directories()

        self.workflow_dir = self.data_dir / "workflow"
        self.output_dir = self.data_dir / "output"
        self.sensitive_words_path = self.data_dir / "sensitive_words.json"

        control_conf = config.get("control", {})
        self.cooldown_seconds = control_conf.get("cooldown_seconds", 60)
        self.user_cooldowns = {}
        self.admin_user_ids = set(map(str, control_conf.get("admin_ids", [])))
        self.lockdown = bool(control_conf.get("lockdown", False))
        self.lockdown_command_enabled = bool(control_conf.get("lockdown_command_enabled", True))
        self.whitelist_group_ids = set(map(str, control_conf.get("whitelist_group_ids", [])))

        self.default_group_policy = str(control_conf.get("default_group_policy", "none")).lower()
        self.default_private_policy = str(control_conf.get("default_private_policy", "none")).lower()
        self.group_policies = {
            str(k): str(v).lower()
            for k, v in control_conf.get("group_policies", {}).items()
        }
        self.policies = {
            "none": set(),
            "lite": {"legacy_lite"},
            "full": {"legacy_lite", "minors", "sexual_violence", "bestiality_incest_necrophilia", "violence_gore", "scat_urine_vomit", "self_harm", "sexual", "nudity", "fetish"},
        }

        bypass = control_conf.get("admin_bypass", {})
        self.admin_bypass_whitelist = bypass.get("whitelist", True)
        self.admin_bypass_cooldown = bypass.get("cooldown", True)
        self.admin_bypass_sensitive = bypass.get("sensitive_words", True)

        admin_count = len(self.admin_user_ids)
        group_count = len(self.whitelist_group_ids)
        logger.info(f"[ComfyUI] 👤 超级管理员: {admin_count} 个 | 🏠 白名单群: {group_count} 个")
        if self.lockdown:
            logger.warning("[ComfyUI] ⚠️ 绘图功能全局锁定已启用，仅超级管理员可用")
        logger.info(f"[ComfyUI] 🔐 锁定命令开关: {'开启' if self.lockdown_command_enabled else '关闭'}")

        self.lexicon = {}
        try:
            if self.sensitive_words_path.exists():
                with open(self.sensitive_words_path, "r", encoding="utf-8") as f:
                    self.lexicon = json.load(f)
                word_count = sum(len(v) for v in self.lexicon.values() if isinstance(v, list))
                logger.info(f"[ComfyUI] 🔒 敏感词库已加载: {word_count} 个词条")
            else:
                self.lexicon = {"legacy_lite": [], "full": []}
        except Exception:
            self.lexicon = {"legacy_lite": [], "full": []}

        self._policy_patterns = {}
        self._build_policy_patterns()

        self.api = None
        try:
            from .comfyui_api import ComfyUI
            self.api = ComfyUI(self.config, data_dir=self.data_dir)
            logger.info(f"[ComfyUI] ✅ ComfyUI API 初始化成功")
        except Exception as e:
            logger.error(f"[ComfyUI] ❌ ComfyUI API 初始化失败: {e}")
            logger.error(traceback.format_exc())

        raw_queue_messages = config.get("queue_delay_messages", "") or ""
        self.queue_delay_messages = []
        if isinstance(raw_queue_messages, str):
            for line in raw_queue_messages.split("\n"):
                line = line.strip()
                if line:
                    self.queue_delay_messages.append(line)
        if not self.queue_delay_messages:
            self.queue_delay_messages = list(self._QUEUE_DELAY_MESSAGES)

        raw_drawing_messages = config.get("drawing_prompt_messages", "") or ""
        self.drawing_prompt_messages = []
        if isinstance(raw_drawing_messages, str):
            for line in raw_drawing_messages.split("\n"):
                line = line.strip()
                if line:
                    self.drawing_prompt_messages.append(line)
        if not self.drawing_prompt_messages:
            self.drawing_prompt_messages = list(self._DRAWING_PROMPT_MESSAGES)

        self._draw_queue = asyncio.Queue()
        self._processing_count = 0
        self._queue_workers = []

        self._tagger_queue = asyncio.Queue()
        self._tagger_processing_count = 0
        self.tagger_model = config.get("tagger_model", "wd-v1-4-moat-tagger-v2")

    def _get_persistent_dir(self) -> Path:
        data_path = None
        if HAS_STAR_TOOLS:
            try:
                data_path = StarTools.get_data_dir(self)
            except Exception:
                try:
                    data_path = StarTools.get_data_dir()
                except Exception:
                    try:
                        data_path = StarTools.get_data_dir(self.context)
                    except Exception:
                        pass
        if data_path is None:
            current = Path.cwd()
            data_path = current / "data" / "plugin_data" / "astrbot_plugin_comfyui_helper"
        if not isinstance(data_path, Path):
            data_path = Path(data_path)
        data_path.mkdir(parents=True, exist_ok=True)
        return data_path

    def _init_data_directories(self):
        workflow_dir = self.data_dir / "workflow"
        output_dir = self.data_dir / "output"
        workflow_dir.mkdir(exist_ok=True)
        output_dir.mkdir(exist_ok=True)

        plugin_workflow_dir = PLUGIN_DIR / "workflow"
        copied_count = 0
        if plugin_workflow_dir.exists():
            for src_file in plugin_workflow_dir.glob("*.json"):
                dst_file = workflow_dir / src_file.name
                if not dst_file.exists():
                    try:
                        shutil.copy2(src_file, dst_file)
                        copied_count += 1
                    except Exception as e:
                        logger.error(f"[ComfyUI] 复制工作流失败 {src_file.name}: {e}")
        if copied_count > 0:
            logger.info(f"[ComfyUI] 📋 已复制 {copied_count} 个默认工作流")

        sensitive_dst = self.data_dir / "sensitive_words.json"
        sensitive_src = PLUGIN_DIR / "sensitive_words.json"
        if not sensitive_dst.exists() and sensitive_src.exists():
            try:
                shutil.copy2(sensitive_src, sensitive_dst)
                logger.info(f"[ComfyUI] 📋 已复制默认敏感词文件")
            except Exception as e:
                logger.error(f"[ComfyUI] 复制敏感词文件失败: {e}")

    def _check_access(self, event: AstrMessageEvent) -> tuple:
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        if self.lockdown and not is_admin:
            return False, "🔒 绘图功能锁定中，仅超级管理员可用"
        if self._is_group_message(event):
            gid = self._get_group_id(event)
            if not gid:
                return False, "⚠️ 无法获取群号"
            if gid not in self.whitelist_group_ids:
                if is_admin and self.admin_bypass_whitelist:
                    pass
                else:
                    return False, f"🚫 本群({gid})不在白名单中"
        return True, ""

    def _check_cooldown(self, event: AstrMessageEvent) -> tuple:
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        if is_admin and self.admin_bypass_cooldown:
            return True, 0
        current_time = time.time()
        last_time = self.user_cooldowns.get(user_id, 0)
        elapsed = current_time - last_time
        if elapsed < self.cooldown_seconds:
            remain = int(self.cooldown_seconds - elapsed)
            return False, remain
        self.user_cooldowns[user_id] = current_time
        return True, 0

    def _check_sensitive(self, prompt: str, event: AstrMessageEvent) -> tuple:
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids
        sensitive = self._find_sensitive_words(prompt, event)
        if not sensitive:
            return True, []
        if is_admin and self.admin_bypass_sensitive:
            logger.info(f"[ComfyUI] 👑 管理员 {user_id} 使用敏感词 {sensitive}，已放行")
            return True, []
        return False, sensitive

    def _extract_command_prompt(self, event: AstrMessageEvent) -> str:
        full_message = (getattr(event, "message_str", "") or "").strip()
        parts = full_message.split(None, 1)
        return parts[1].strip() if len(parts) > 1 else ""

    async def initialize(self):
        worker_count = len(self.api.servers)
        self._queue_workers = []
        for i in range(worker_count):
            w = asyncio.create_task(self._queue_worker(i))
            self._queue_workers.append(w)

        self._tagger_worker_task = asyncio.create_task(self._tagger_worker())
        logger.info(f"[ComfyUI] 🎨 插件初始化完成，{worker_count} 个绘图队列 + 1 个标签队列工作者")

    # ====== 核心绘图逻辑 ======
    async def _handle_paint_logic(self, event: AstrMessageEvent, direct_send: bool):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        try:
            full_message = event.message_str.strip()
            parts = full_message.split(' ', 1)
            remaining = parts[1].strip() if len(parts) > 1 else ""

            override_wf_id = None
            prompt = remaining
            if remaining:
                first_token = remaining.split(' ', 1)[0]
                try:
                    wf_id = int(first_token)
                    if getattr(self, 'api', None) and wf_id in self.api.workflows:
                        override_wf_id = wf_id
                        prompt = remaining.split(' ', 1)[1].strip() if ' ' in remaining else ""
                    elif getattr(self, 'api', None):
                        yield event.plain_result(f"❌ 工作流 ID {wf_id} 不存在，可用 ID: {sorted(self.api.workflows.keys())}")
                        return
                except ValueError:
                    prompt = remaining

            if not prompt:
                message = "❌ 请输入提示词，例如：/画图 1girl, smile"
                yield event.plain_result(message)
                return

            passed, sensitive = self._check_sensitive(prompt, event)
            if not passed:
                tip = "、".join(sensitive[:5])
                extra = f"等 {len(sensitive)} 个" if len(sensitive) > 5 else ""
                message = f"🚫 检测到敏感词：{tip}{extra}，无法生成图片"
                yield event.plain_result(message)
                return

            ok, remain = self._check_cooldown(event)
            if not ok:
                message = f"⏱️ 冷却中，请在 {remain} 秒后重试"
                yield event.plain_result(message)
                return

            n_ahead = self._draw_queue.qsize() + self._processing_count
            if n_ahead == 0:
                await event.send(event.plain_result(
                    random.choice(self.drawing_prompt_messages)
                ))
            else:
                await event.send(event.plain_result(
                    random.choice(self.queue_delay_messages).format(n=n_ahead)
                ))
            await self._draw_queue.put(DrawTask(
                event=event, prompt=prompt, direct_send=direct_send, override_wf_id=override_wf_id
            ))

        except Exception as e:
            logger.error(f"[ComfyUI] 绘图异常: {e}")
            logger.error(traceback.format_exc())
            message = f"❌ 执行出错：{str(e)[:50]}"
            yield event.plain_result(message)

    async def _queue_worker(self, worker_id: int):
        while True:
            task = await self._draw_queue.get()
            self._processing_count += 1
            try:
                await self._execute_task(task)
            except Exception as e:
                logger.error(f"[ComfyUI] 队列任务[{worker_id}] 异常: {e}")
            finally:
                self._processing_count -= 1

    async def _execute_task(self, task: DrawTask):
        if not getattr(self, 'api', None):
            await task.event.send(task.event.plain_result("❌ ComfyUI 服务未连接，请检查配置"))
            return

        try:
            logger.info(f"[ComfyUI] 🎨 开始生成 | 用户: {task.event.get_sender_id()} | Workflow: {task.override_wf_id or 'default'} | Prompt: {task.prompt[:50]}...")

            img_data, error_msg, final_prompt = await self.api.generate(task.prompt, override_wf_id=task.override_wf_id)

            if not img_data:
                logger.error(f"[ComfyUI] 生成失败: {error_msg}")
                await task.event.send(task.event.plain_result(f"❌ 生成失败：{error_msg}"))
                return

            img_filename = f"{uuid.uuid4()}.png"
            img_path = self.output_dir / img_filename
            with open(img_path, 'wb') as fp:
                fp.write(img_data)

            logger.info(f"[ComfyUI] ✅ 图片已保存: {img_filename}")

            if task.direct_send:
                await task.event.send(task.event.chain_result([
                    Image.fromFileSystem(str(img_path)),
                    Plain(final_prompt),
                ]))
            else:
                self_id = self._get_self_id(task.event) or "0"
                await task.event.send(task.event.chain_result([Node(
                    user_id=int(self_id),
                    nickname="ComfyUI",
                    content=[
                        Image.fromFileSystem(str(img_path)),
                        Plain(final_prompt),
                    ]
                )]))

        except Exception as e:
            logger.error(f"[ComfyUI] 执行异常: {e}")
            logger.error(traceback.format_exc())
            await task.event.send(task.event.plain_result(f"❌ 内部错误: {str(e)[:50]}"))

    async def _tagger_worker(self):
        while True:
            task = await self._tagger_queue.get()
            self._tagger_processing_count += 1
            try:
                await self._execute_tagger_task(task)
            except Exception as e:
                logger.error(f"[ComfyUI] Tagger 队列异常: {e}")
            finally:
                self._tagger_processing_count -= 1

    async def _execute_tagger_task(self, task: TaggerTask):
        if not getattr(self, 'api', None):
            await task.event.send(task.event.plain_result("❌ ComfyUI 服务未连接，请检查配置"))
            return

        try:
            ext = ".png"
            filename = f"{uuid.uuid4()}{ext}"
            uploaded_name = await self.api.upload_image(task.image_data, filename)

            tags, error = await self.api.execute_tagger(uploaded_name, model=self.tagger_model)

            if error:
                logger.error(f"[ComfyUI] Tagger 反推失败: {error}")
                await task.event.send(task.event.plain_result(f"❌ 反推标签失败：{error}"))
                return

            logger.info(f"[ComfyUI] ✅ Tagger 反推完成: {tags[:80]}...")
            await task.event.send(task.event.plain_result(f"🏷️ 反推标签:\n{tags}"))

        except Exception as e:
            logger.error(f"[ComfyUI] Tagger 执行异常: {e}")
            logger.error(traceback.format_exc())
            await task.event.send(task.event.plain_result(f"❌ 反推失败：{str(e)[:50]}"))

    # ====== 命令 ======
    @filter.command("comfy帮助")
    async def cmd_comfyui_help(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        gid = self._get_group_id(event)
        policy = self._get_policy_for_event(event)
        user_id = str(event.get_sender_id())
        is_admin = user_id in self.admin_user_ids

        tips = [
            "🎨 ComfyUI Pro 插件帮助",
            "━━━━━━━━━━━━━━━━━━",
            "",
            "【基础指令】",
            "  /画图 <提示词>     生成图片（转发模式）",
            "  /画图no <提示词>   生成图片（直发模式）",
            "  /重绘 <提示词>     生成图片（直发模式）",
            "  /tagger (图片)     反推图片标签",
            "  /comfy帮助         显示此帮助",
            "",
        ]

        if is_admin:
            tips.extend([
                "【管理员指令】 👑",
                "  /comfy_ls              列出所有工作流",
                "  /comfy_use <ID>       切换默认工作流",
                "  /comfy_save            导入新工作流",
                "  /comfy_add             步数覆盖（按节点ID）",
                "  /comfy_lock on|off     切换全局锁定",
                "  /违禁级别              设置群敏感度",
                "",
            ])

        tips.append("━━━━━━━━━━━━━━━━━━")
        tips.append(f"📍 当前位置：{'群聊 ' + gid if gid else '私聊'}")
        tips.append(f"🔒 违禁级别：{policy}")
        tips.append(f"⏱️ 冷却时间：{self.cooldown_seconds} 秒")
        tips.append(f"🔐 全局锁定：{'开启' if self.lockdown else '关闭'}")
        if is_admin:
            tips.append(f"👑 身份：管理员")
            tips.append(f"📂 数据目录：{self.data_dir}")

        yield event.plain_result("\n".join(tips))

    @filter.command("违禁级别", aliases={"banlevel", "敏感级别"})
    async def cmd_set_policy(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        if not self._is_group_message(event):
            yield event.plain_result("⚠️ 该指令仅支持在群聊中使用")
            return

        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可修改违禁级别")
            return

        full_msg = event.message_str.strip()
        parts = full_msg.split()
        gid = self._get_group_id(event) or "未知"

        if len(parts) == 1:
            current = self.group_policies.get(gid, self.default_group_policy)
            yield event.plain_result(
                f"📊 本群当前违禁级别：{current}\n"
                f"━━━━━━━━━━━━━━\n"
                f"可选级别：\n"
                f"  none - 不过滤\n"
                f"  lite - 轻度过滤\n"
                f"  full - 完全过滤\n"
                f"━━━━━━━━━━━━━━\n"
                f"用法：/违禁级别 <级别>"
            )
            return

        level = parts[1].lower()
        if level not in self.policies:
            yield event.plain_result("❌ 无效级别，可选：none / lite / full")
            return

        self.group_policies[gid] = level
        logger.info(f"[ComfyUI] 群 {gid} 违禁级别已设为 {level}（操作者：{user_id}）")
        yield event.plain_result(f"✅ 已将本群违禁级别设置为：{level}")

    @filter.command("comfy_lock", aliases=["全局锁定", "锁图", "绘图锁定"])
    async def cmd_comfy_lock(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可切换全局锁定")
            return

        if not self.lockdown_command_enabled:
            yield event.plain_result("⚠️ 锁定命令开关已关闭，请在插件配置中启用 control.lockdown_command_enabled")
            return

        args = event.message_str.split()
        action = args[1].lower() if len(args) > 1 else "status"

        if action in ("status", "状态", "查询"):
            yield event.plain_result(
                "🔐 全局锁定状态\n"
                "━━━━━━━━━━━━━━━━━━\n"
                f"当前: {'开启' if self.lockdown else '关闭'}\n"
                f"命令开关: {'开启' if self.lockdown_command_enabled else '关闭'}\n"
                "用法: /comfy_lock on|off|status"
            )
            return

        if action in ("on", "true", "1", "enable", "start", "开启"):
            self.lockdown = True
            logger.warning(f"[ComfyUI] 管理员 {user_id} 通过命令开启全局锁定")
            yield event.plain_result("🔒 已开启全局锁定：当前仅超级管理员可用绘图功能")
            return

        if action in ("off", "false", "0", "disable", "stop", "关闭"):
            self.lockdown = False
            logger.info(f"[ComfyUI] 管理员 {user_id} 通过命令关闭全局锁定")
            yield event.plain_result("🔓 已关闭全局锁定：恢复正常访问控制")
            return

        yield event.plain_result("❌ 参数无效，用法：/comfy_lock on|off|status")

    @filter.command("comfy_ls")
    async def cmd_comfy_list(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可查看工作流列表")
            return

        if not self.api or not self.api.workflows:
            yield event.plain_result("📂 未配置工作流，请在插件设置中填写 workflows")
            return

        current_id = self.api.current_wf_id
        msg = ["📂 已配置工作流", "━━━━━━━━━━━━━━━━━━"]

        for wf_id in sorted(self.api.workflows.keys()):
            wf = self.api.workflows[wf_id]
            sid = wf.get("server_id", 0)
            marker = " ✅ (当前)" if wf_id == current_id else ""
            msg.append(f"  ID {wf_id}: {wf['filename']} [服务器{sid}]{marker}")

        msg.append("")
        msg.append("━━━━━━━━━━━━━━━━━━")
        msg.append("切换：/comfy_use <ID>")
        msg.append("临时：/画图 <ID> <提示词>")

        yield event.plain_result("\n".join(msg))

    @filter.command("comfy_use")
    async def cmd_comfy_use(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可切换工作流")
            return

        args = event.message_str.split()
        if len(args) < 2:
            yield event.plain_result(
                "❌ 参数不足\n"
                "用法：/comfy_use <工作流ID>\n"
                "示例：/comfy_use 0"
            )
            return

        try:
            wf_id = int(args[1])
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字 ID")
            return

        if not self.api:
            yield event.plain_result("❌ ComfyUI API 未初始化")
            return

        exists, msg = self.api.reload_config(wf_id)

        status = "✅" if exists else "⚠️"
        logger.info(f"[ComfyUI] 管理员 {user_id} 切换工作流: ID={wf_id}")
        yield event.plain_result(f"{status} {msg}")

    @filter.command("comfy_save")
    async def cmd_comfy_save(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可导入工作流")
            return

        full_text = event.message_str
        content = full_text.split(maxsplit=2)

        if len(content) < 3:
            yield event.plain_result(
                "❌ 参数不足\n"
                "用法：/comfy_save <文件名> <JSON内容>\n"
                "示例：/comfy_save my_workflow.json {\"1\":{...}}"
            )
            return

        filename = content[1]
        json_str = content[2]

        if not filename.endswith(".json"):
            filename += ".json"

        try:
            json_str = json_str.replace("```json", "").replace("```", "").strip()
            json_data = json.loads(json_str)
        except json.JSONDecodeError as e:
            yield event.plain_result(f"❌ JSON 解析失败：{str(e)[:50]}")
            return

        save_path = self.workflow_dir / filename

        try:
            with open(save_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, indent=2, ensure_ascii=False)
            logger.info(f"[ComfyUI] 管理员 {user_id} 导入工作流: {filename}")
            yield event.plain_result(
                f"✅ 保存成功！\n"
                f"文件：{filename}\n"
                f"使用 /comfy_ls 查看列表"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 保存失败: {e}")

    @filter.command("comfy_add")
    async def cmd_comfy_add(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id())
        if user_id not in self.admin_user_ids:
            yield event.plain_result("🚫 权限不足，仅管理员可设置步数覆盖")
            return

        if not self.api:
            yield event.plain_result("❌ ComfyUI API 未初始化")
            return

        args = event.message_str.split()

        if len(args) < 2:
            yield event.plain_result(
                "📝 步数覆盖设置（按节点ID）\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "用法：\n"
                "  /comfy_add <节点ID> <步数>      单个设置\n"
                "  /comfy_add <ID1> <步数1> <ID2> <步数2>  批量设置\n"
                "  /comfy_add <节点ID> off         取消单个\n"
                "  /comfy_add list                 查看当前覆盖\n"
                "  /comfy_add clear                清空所有覆盖\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "示例：\n"
                "  /comfy_add 3839 20              节点3839设为20步\n"
                "  /comfy_add 3839 20 4521 50      同时设置两个节点\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "💡 节点ID可在工作流JSON中查找 ParameterBreak 节点"
            )
            return

        sub_cmd = args[1].lower()

        if sub_cmd == "list":
            async for result in self._comfy_add_list(event):
                yield result
            return

        if sub_cmd == "clear":
            async for result in self._comfy_add_clear(event):
                yield result
            return

        params = args[1:]

        if len(params) % 2 != 0:
            yield event.plain_result("❌ 参数格式错误，需要成对输入：<节点ID> <步数>")
            return

        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"

        existing = {}
        if sidecar_path.exists():
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except:
                existing = {}

        changes = []
        removes = []

        for i in range(0, len(params), 2):
            node_id = params[i]
            value = params[i + 1].lower()

            if value in ("off", "0", "del", "delete", "rm", "remove"):
                if node_id in existing:
                    del existing[node_id]
                    removes.append(node_id)
            else:
                try:
                    steps = int(value)
                    if not (1 <= steps <= 200):
                        yield event.plain_result(f"❌ 步数应在 1-200 之间，节点 {node_id} 的值 {value} 无效")
                        return
                    existing[node_id] = {"steps": steps}
                    changes.append(f"{node_id}:{steps}步")
                except ValueError:
                    yield event.plain_result(f"❌ 无效的步数值：{value}")
                    return

        try:
            if existing:
                with open(sidecar_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, ensure_ascii=False, indent=2)
            else:
                if sidecar_path.exists():
                    sidecar_path.unlink()

            msg_parts = []
            if changes:
                msg_parts.append(f"✅ 已设置: {', '.join(changes)}")
            if removes:
                msg_parts.append(f"🗑️ 已移除: {', '.join(removes)}")
            msg_parts.append(f"📍 工作流: {current_file}")

            logger.info(f"[ComfyUI] 管理员 {user_id} 修改步数覆盖: {current_file} -> {existing}")
            yield event.plain_result("\n".join(msg_parts))

        except Exception as e:
            yield event.plain_result(f"❌ 保存失败: {e}")

    async def _comfy_add_list(self, event: AstrMessageEvent):
        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"

        lines = [
            f"📊 当前工作流步数覆盖",
            f"━━━━━━━━━━━━━━━━━━",
            f"📍 工作流: {current_file}",
            ""
        ]

        if not sidecar_path.exists():
            lines.append("ℹ️ 暂无步数覆盖配置")
        else:
            try:
                with open(sidecar_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not data:
                    lines.append("ℹ️ 暂无步数覆盖配置")
                else:
                    lines.append("节点覆盖列表：")
                    for node_id, value in data.items():
                        if isinstance(value, dict):
                            steps = value.get("steps", "?")
                        else:
                            steps = value
                        lines.append(f"  • 节点 {node_id}: {steps} 步")
            except Exception as e:
                lines.append(f"❌ 读取配置失败: {e}")

        lines.append("")
        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("设置：/comfy_add <节点ID> <步数>")
        lines.append("清空：/comfy_add clear")

        yield event.plain_result("\n".join(lines))

    async def _comfy_add_clear(self, event: AstrMessageEvent):
        current_file = self.api.wf_filename
        stem = Path(current_file).stem
        sidecar_path = self.workflow_dir / f"{stem}.steps.json"

        if not sidecar_path.exists():
            yield event.plain_result(f"ℹ️ {current_file} 本来就没有步数覆盖")
            return

        try:
            sidecar_path.unlink()
            user_id = str(event.get_sender_id())
            logger.info(f"[ComfyUI] 管理员 {user_id} 清空步数覆盖: {current_file}")
            yield event.plain_result(f"✅ 已清空 {current_file} 的所有步数覆盖")
        except Exception as e:
            yield event.plain_result(f"❌ 清空失败: {e}")

    @filter.command("当前工作流", aliases=["comfy_current", "当前wf"])
    async def cmd_comfy_current(self, event: AstrMessageEvent):
        if not self.api:
            yield event.plain_result("❌ ComfyUI API 未初始化")
            return
        wf_id = self.api.current_wf_id
        wf = self.api.workflows.get(wf_id, {})
        lines = [
            "🧠 当前 ComfyUI 工作流",
            f"  ID: {wf_id}",
            f"  文件: {wf.get('filename', '未知')}",
            f"  正面节点: {wf.get('input_id', '未知')}",
            f"  负面节点: {wf.get('neg_node_id', '未设置') or '未设置'}",
            f"  输出节点: {wf.get('output_id', '未设置') or '自动'}",
            f"  服务器: {self.api.current_server_id} ({self.api.server_address})",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("重绘", aliases=["重抽", "reroll"])
    async def cmd_reroll(self, event: AstrMessageEvent):
        full_msg = (event.message_str or "").strip()
        full_msg = re.sub(r'\[At:\d+\]\s*', '', full_msg).strip()
        parts = full_msg.split(None, 1)
        prompt = parts[1].strip() if len(parts) > 1 else ""
        if not prompt:
            yield event.plain_result("📖 用法: /重绘 <提示词>\n示例: /重绘 1girl, silver hair, cinematic lighting")
            return
        async for result in self._handle_paint_logic(event, direct_send=True):
            yield result

    @filter.command("画图", aliases=["绘画"])
    async def cmd_paint(self, event: AstrMessageEvent):
        async for result in self._handle_paint_logic(event, direct_send=False):
            yield result

    @filter.command("画图no")
    async def cmd_paint_no(self, event: AstrMessageEvent):
        async for result in self._handle_paint_logic(event, direct_send=True):
            yield result

    @filter.command("FF401")
    async def cmd_ff401(self, event: AstrMessageEvent):
        text = event.message_str.strip()
        parts = text.split(None, 1)
        nlp_prompt = parts[1].strip() if len(parts) > 1 else ""
        if not nlp_prompt:
            yield event.plain_result("❌ 用法：/FF401 <你的描述>\n示例：/FF401 画一只猫")
            return

        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        passed, sensitive = self._check_sensitive(nlp_prompt, event)
        if not passed:
            tip = "、".join(sensitive[:5])
            yield event.plain_result(f"🚫 检测到敏感词：{tip}，无法处理")
            return

        ok, remain = self._check_cooldown(event)
        if not ok:
            yield event.plain_result(f"⏱️ 冷却中，请在 {remain} 秒后重试")
            return

        if not getattr(self, 'api', None):
            yield event.plain_result("❌ ComfyUI 服务未连接，请检查配置")
            return

        await event.send(event.plain_result("🔍 正在理解你的描述..."))

        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider is None:
                yield event.plain_result("❌ 未检测到可用的 LLM 模型，请在 AstrBot 中配置一个模型以使用本功能")
                return
            response = await provider.text_chat(
                prompt=nlp_prompt,
                system_prompt=self._LLM_TRANSLATE_PROMPT,
            )
            tags = (response.completion_text or "").strip()
            if not tags:
                yield event.plain_result("❌ LLM 返回为空，翻译失败")
                return
            logger.info(f"[ComfyUI] FF401 翻译结果: {nlp_prompt} → {tags[:80]}")
        except Exception as e:
            logger.error(f"[ComfyUI] FF401 LLM 翻译失败: {e}")
            yield event.plain_result(f"❌ LLM 翻译失败：{str(e)[:50]}")
            return

        n_ahead = self._draw_queue.qsize() + self._processing_count
        if n_ahead == 0:
            await event.send(event.plain_result(random.choice(self.drawing_prompt_messages)))
        else:
            await event.send(event.plain_result(
                random.choice(self.queue_delay_messages).format(n=n_ahead)
            ))
        await self._draw_queue.put(DrawTask(
            event=event, prompt=tags, direct_send=False
        ))

    @filter.command("tagger")
    async def cmd_tagger(self, event: AstrMessageEvent):
        allowed, reason = self._check_access(event)
        if not allowed:
            yield event.plain_result(reason)
            return

        if not getattr(self, 'api', None):
            yield event.plain_result("❌ ComfyUI 服务未连接，请检查配置")
            return

        image_data = None
        for comp in event.message_obj.message:
            if isinstance(comp, Image):
                path = await comp.convert_to_file_path()
                with open(path, 'rb') as f:
                    image_data = f.read()
                break
            elif isinstance(comp, Reply) and comp.chain:
                for reply_comp in comp.chain:
                    if isinstance(reply_comp, Image):
                        path = await reply_comp.convert_to_file_path()
                        with open(path, 'rb') as f:
                            image_data = f.read()
                        break
                if image_data:
                    break
        if not image_data:
            yield event.plain_result("❌ 请发送图片")
            return

        n_ahead = self._tagger_queue.qsize() + self._tagger_processing_count
        if n_ahead == 0:
            await event.send(event.plain_result("🔄 正在反推标签，请稍等..."))
        else:
            await event.send(event.plain_result(f"⏳ 正在排队，前面还有 {n_ahead} 个请求"))

        await self._tagger_queue.put(TaggerTask(event=event, image_data=image_data))

    # ====== 辅助方法 ======
    def _is_group_message(self, event: AstrMessageEvent) -> bool:
        mt = getattr(event, "message_type", None)
        if mt is not None:
            return mt == "group"
        try:
            if hasattr(event, "get_group_id"):
                gid = event.get_group_id()
                if gid:
                    return True
            gid_attr = getattr(event, "group_id", None)
            return gid_attr is not None
        except Exception:
            return False

    def _get_group_id(self, event: AstrMessageEvent):
        if not self._is_group_message(event):
            return None
        getters = [
            lambda e: e.get_group_id() if hasattr(e, "get_group_id") else None,
            lambda e: getattr(e, "group_id", None),
            lambda e: getattr(getattr(e, "scene", None), "group_id", None),
        ]
        for g in getters:
            try:
                gid = g(event)
                if gid:
                    return str(gid)
            except Exception:
                continue
        return None

    def _get_self_id(self, event: AstrMessageEvent):
        getters = [
            lambda e: e.get_self_id() if hasattr(e, "get_self_id") else None,
            lambda e: getattr(e, "self_id", None),
            lambda e: getattr(getattr(self.context, "bot", None), "self_id", None),
            lambda e: getattr(self.context, "self_id", None),
        ]
        for g in getters:
            try:
                sid = g(event)
                if sid:
                    return str(sid)
            except Exception:
                continue
        return None

    def _is_ascii_term(self, s: str) -> bool:
        return all(ord(ch) < 128 for ch in s)

    def _build_policy_patterns(self):
        for policy, cats in self.policies.items():
            word_terms = []
            phrase_terms = []
            for cat in cats:
                for t in self.lexicon.get(cat, []):
                    if not t:
                        continue
                    if self._is_ascii_term(t):
                        if " " in t:
                            phrase_terms.append(re.escape(t))
                        else:
                            word_terms.append(re.escape(t))
            word_terms = list(dict.fromkeys(word_terms))
            phrase_terms = list(dict.fromkeys(phrase_terms))

            parts = []
            if word_terms:
                parts.append(r'(?<![A-Za-z0-9_])(?:' + '|'.join(word_terms) + r')(?![A-Za-z0-9_])')
            if phrase_terms:
                parts.append('|'.join(phrase_terms))

            ascii_pat = re.compile('|'.join(parts), re.IGNORECASE) if parts else None
            self._policy_patterns[policy] = ascii_pat

    def _get_policy_for_event(self, event: AstrMessageEvent) -> str:
        if self._is_group_message(event):
            gid = self._get_group_id(event)
            if not gid:
                return self.default_group_policy
            return self.group_policies.get(gid, self.default_group_policy)
        return self.default_private_policy

    def _find_sensitive_words(self, text: str, event: AstrMessageEvent = None):
        if not text:
            return []
        policy = "full"
        if event is not None:
            policy = self._get_policy_for_event(event)
        if policy == "none":
            return []
        ascii_pat = self._policy_patterns.get(str(policy).lower())
        if not ascii_pat:
            return []
        seen = set()
        result = []
        for m in ascii_pat.finditer(text):
            w = m.group(0)
            key = w.lower()
            if key not in seen:
                seen.add(key)
                result.append(w)
        return result
