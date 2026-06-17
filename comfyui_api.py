import asyncio
import json
import os
import random
import re
from pathlib import Path

import aiohttp
import requests

from astrbot.api import logger


DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_READ_TIMEOUT = 180
DEFAULT_REQUEST_TIMEOUT = (DEFAULT_CONNECT_TIMEOUT, DEFAULT_READ_TIMEOUT)
DEFAULT_RETRY_TOTAL = 3
DEFAULT_RETRY_BACKOFF = 1.0
_HTTP_SESSION = None


def _normalize_server_address(server_address):
    server_address = (server_address or "").strip()
    if not server_address:
        return server_address
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", server_address):
        server_address = f"http://{server_address}"
    return server_address.rstrip("/")


def _coerce_timeout(value):
    if value is None or value == "":
        return DEFAULT_REQUEST_TIMEOUT
    if isinstance(value, (int, float)):
        value = max(float(value), 0.1)
        return (min(value, DEFAULT_CONNECT_TIMEOUT), value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        connect_timeout = max(float(value[0]), 0.1)
        read_timeout = max(float(value[1]), 0.1)
        return (connect_timeout, read_timeout)
    return DEFAULT_REQUEST_TIMEOUT


def _build_http_session():
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION
    session = requests.Session()
    try:
        from requests.adapters import HTTPAdapter
        try:
            from urllib3.util.retry import Retry
        except Exception:
            from requests.packages.urllib3.util.retry import Retry
        retry = Retry(
            total=DEFAULT_RETRY_TOTAL,
            connect=DEFAULT_RETRY_TOTAL,
            read=DEFAULT_RETRY_TOTAL,
            status=DEFAULT_RETRY_TOTAL,
            backoff_factor=DEFAULT_RETRY_BACKOFF,
            status_forcelist=(408, 409, 425, 429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    except Exception:
        pass
    _HTTP_SESSION = session
    return session


def _http_request(method, url, **kwargs):
    session = _build_http_session()
    timeout = _coerce_timeout(kwargs.pop("timeout", None))
    return session.request(method=method, url=url, timeout=timeout, **kwargs)


def _http_get(url, **kwargs):
    return _http_request("GET", url, **kwargs)


def _http_post(url, **kwargs):
    return _http_request("POST", url, **kwargs)


class ComfyUI:
    def __init__(self, config: dict, data_dir: Path = None) -> None:
        self.servers = {}
        self.server_address = ""
        self.url = ""
        self.current_server_id = 0

        sub_conf = config.get("sub_config", {})
        self.steps = sub_conf.get("steps", 20)
        self.width = sub_conf.get("width", 768)
        self.height = sub_conf.get("height", 1024)
        self.neg_prompt = sub_conf.get("negative_prompt", "")

        self.workflows = {}
        self.current_wf_id = 0
        self.wf_filename = "workflow_api.json"
        self.input_id = "6"
        self.neg_node_id = ""
        self.output_id = ""
        self.seed_id = None

        if data_dir is not None:
            self.data_dir = Path(data_dir)
        else:
            self.data_dir = Path(os.path.dirname(os.path.abspath(__file__)))
            logger.warning("[ComfyUI API] data_dir missing, fallback to plugin dir")

        self.workflow_dir = self.data_dir / "workflow"
        self._parse_servers(config.get("servers", ""))
        self._parse_workflows(config.get("workflows", ""))

        logger.info(
            f"[ComfyUI API] loaded | {len(self.servers)} server(s), {len(self.workflows)} workflow(s)"
        )

    def _parse_servers(self, raw: str):
        self.servers = {}
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 2:
                continue
            try:
                sid = int(parts[0])
            except ValueError:
                continue
            address = parts[1]
            if not address:
                continue
            self.servers[sid] = address

        if not self.servers:
            self.servers[0] = "127.0.0.1:8188"
            logger.warning("[ComfyUI] 未配置服务器，使用默认 127.0.0.1:8188")

    def _parse_workflows(self, raw: str):
        self.workflows = {}
        for line in raw.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 5:
                continue
            try:
                wf_id = int(parts[0])
            except ValueError:
                continue
            server_id = 0
            if len(parts) > 5 and parts[5]:
                try:
                    server_id = int(parts[5])
                except ValueError:
                    server_id = 0
            self.workflows[wf_id] = {
                "filename": parts[1] or "workflow_api.json",
                "input_id": parts[2] or "6",
                "neg_node_id": parts[3] or "",
                "output_id": parts[4] or "",
                "server_id": server_id,
            }

        if 0 in self.workflows:
            self._apply_workflow_config(0)
        elif self.workflows:
            first_id = next(iter(self.workflows))
            self._apply_workflow_config(first_id)
        else:
            self.current_wf_id = 0
            self.wf_filename = "workflow_api.json"
            self.input_id = "6"
            self.neg_node_id = ""
            self.output_id = ""
            logger.warning("[ComfyUI] 未配置工作流，使用硬编码默认值")

        self.workflow_path = self.workflow_dir / self.wf_filename

    def _apply_workflow_config(self, workflow_id: int):
        wf = self.workflows.get(workflow_id)
        if not wf:
            logger.warning(f"[ComfyUI] 工作流 ID {workflow_id} 不存在，无法应用")
            return
        self.current_wf_id = workflow_id
        self.wf_filename = wf["filename"]
        self.workflow_path = self.workflow_dir / self.wf_filename
        self.input_id = str(wf["input_id"])
        self.neg_node_id = str(wf["neg_node_id"])
        self.output_id = str(wf["output_id"])

        server_id = wf.get("server_id", 0)
        self._apply_server(server_id)

    def _apply_server(self, server_id: int):
        raw = self.servers.get(server_id)
        if not raw:
            raw = self.servers.get(0, "127.0.0.1:8188")
            server_id = 0
        self.current_server_id = server_id
        self.server_address = _normalize_server_address(raw)
        self.url = self.server_address

    def reload_config(self, workflow_id: int):
        if workflow_id not in self.workflows:
            return False, f"❌ 工作流 ID {workflow_id} 不存在"

        self._apply_workflow_config(workflow_id)

        exists = self.workflow_path.exists()
        status = "exists" if exists else "missing"

        logger.info(
            f"[ComfyUI] switch workflow -> ID={workflow_id} ({self.wf_filename}) | server={self.current_server_id} [{status}] | "
            f"Input:{self.input_id} | Neg:{self.neg_node_id} | Output:{self.output_id or 'auto'}"
        )
        return exists, (
            f"已切换至 ID={workflow_id} ({self.wf_filename})，文件状态：{status}\n"
            f"服务器: {self.current_server_id} ({self.server_address})\n"
            f"当前节点设置: Positive={self.input_id}, Negative={self.neg_node_id}, Output={self.output_id or '自动'}"
        )

    def _load_workflow(self):
        if not self.workflow_path.exists():
            raise FileNotFoundError(f"工作流文件不存在: {self.workflow_path}")
        with open(self.workflow_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _inject_params(self, workflow, prompt):
        node = workflow.get(self.input_id)
        if not node:
            logger.error(f"critical: input node id {self.input_id} not found in workflow")
            return prompt

        inputs = node.get("inputs", {})
        target_keys = [
            "text",
            "opt_text",
            "string",
            "text_positive",
            "positive",
            "prompt",
            "wildcard_text",
        ]
        final_prompt = prompt.strip()
        for key in target_keys:
            if key in inputs:
                existing_prompt = str(inputs.get(key, "")).strip()
                user_prompt = prompt.strip()
                if existing_prompt and user_prompt:
                    inputs[key] = f"{user_prompt}, {existing_prompt}"
                elif user_prompt:
                    inputs[key] = user_prompt
                final_prompt = inputs[key]
                break

        if self.neg_node_id and self.neg_prompt:
            neg_node = workflow.get(self.neg_node_id)
            if neg_node:
                n_inputs = neg_node.get("inputs", {})
                n_keys = ["text", "string", "negative", "text_negative", "prompt"]
                for n_key in n_keys:
                    if n_key in n_inputs:
                        existing_neg = str(n_inputs.get(n_key, "")).strip()
                        config_neg = self.neg_prompt.strip()
                        if existing_neg and config_neg:
                            n_inputs[n_key] = f"{existing_neg}, {config_neg}"
                        elif config_neg:
                            n_inputs[n_key] = config_neg
                        break

        overrides = self._load_steps_override()
        if overrides:
            count = self._apply_steps_override(workflow, overrides)
            if count > 0:
                override_info = ", ".join([f"{k}:{v}步" for k, v in overrides.items()])
                logger.info(f"[ComfyUI] steps override applied: {override_info} (updated {count} targets)")
            else:
                logger.info("[ComfyUI] steps override configured but no matching reference was found")

        base_seed = random.randint(1, 999999999999999)
        ks_count = 0
        offset = 0

        for _, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue

            changed = False
            if "seed" in n_inputs:
                n_inputs["seed"] = base_seed + offset
                offset += 1
                changed = True
            if "noise_seed" in n_inputs:
                n_inputs["noise_seed"] = base_seed + offset
                offset += 1
                changed = True
            if changed:
                ks_count += 1

        logger.info(
            f"[ComfyUI] base seed: {base_seed}, updated {ks_count} seed/noise_seed inputs"
        )

        return final_prompt

    def _load_steps_override(self) -> dict:
        try:
            stem = self.workflow_path.stem
            sidecar = self.workflow_path.parent / f"{stem}.steps.json"
            if not sidecar.exists():
                return {}
            with open(sidecar, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            result = {}
            for key, value in data.items():
                if isinstance(value, dict) and "steps" in value:
                    steps = value.get("steps")
                    if isinstance(steps, (int, float)) and steps > 0:
                        result[str(key)] = int(steps)
                elif isinstance(value, (int, float)) and value > 0:
                    result[str(key)] = int(value)
            return result
        except Exception as e:
            logger.warning(f"[ComfyUI] failed to read steps override file: {e}")
            return {}

    def _apply_steps_override(self, workflow: dict, overrides: dict):
        if not overrides:
            return 0

        pb_nodes = {}
        for nid, node_data in workflow.items():
            if isinstance(node_data, dict) and node_data.get("class_type") == "ParameterBreak":
                pb_nodes[str(nid)] = node_data

        if not pb_nodes:
            logger.debug("[ComfyUI] no ParameterBreak nodes detected")
            return 0

        valid_overrides = {}
        for pb_id, steps in overrides.items():
            if pb_id in pb_nodes:
                valid_overrides[pb_id] = steps
            else:
                logger.warning(f"[ComfyUI] override target node {pb_id} not found in current workflow")

        if not valid_overrides:
            return 0

        override_count = 0
        steps_keys = ("steps", "steps_total")

        for nid, node_data in workflow.items():
            if not isinstance(node_data, dict):
                continue
            n_inputs = node_data.get("inputs", {})
            if not isinstance(n_inputs, dict):
                continue
            for key in steps_keys:
                if key not in n_inputs:
                    continue
                value = n_inputs[key]
                if isinstance(value, list) and len(value) == 2:
                    ref_node_id = str(value[0])
                    if ref_node_id in valid_overrides:
                        new_steps = valid_overrides[ref_node_id]
                        n_inputs[key] = new_steps
                        override_count += 1
                        logger.debug(f"[ComfyUI] node {nid}.{key}: [{ref_node_id}] -> {new_steps}")

        return override_count

    async def generate(self, prompt, override_wf_id: int = None):
        client_id = str(random.randint(100000, 999999))

        prev_wf_id = self.current_wf_id
        if override_wf_id is not None:
            if override_wf_id not in self.workflows:
                return None, f"工作流 ID {override_wf_id} 不存在", prompt
            self._apply_workflow_config(override_wf_id)

        try:
            try:
                workflow = self._load_workflow()
            except Exception as e:
                return None, str(e), prompt

            final_prompt = self._inject_params(workflow, prompt)

            async with aiohttp.ClientSession() as session:
                payload = {"prompt": workflow, "client_id": client_id}
                try:
                    async with session.post(f"{self.url}/prompt", json=payload) as resp:
                        if resp.status != 200:
                            return None, f"连接 ComfyUI 失败: {resp.status}", final_prompt
                        res_json = await resp.json()
                        prompt_id = res_json.get("prompt_id")
                except Exception as e:
                    return None, f"请求报错: {str(e)}", final_prompt

                for _ in range(300):
                    await asyncio.sleep(1)
                    try:
                        async with session.get(f"{self.url}/history/{prompt_id}") as h_resp:
                            if h_resp.status != 200:
                                continue
                            history = await h_resp.json()
                    except Exception:
                        continue

                    if prompt_id in history:
                        outputs = history[prompt_id].get("outputs", {})
                        img_info = None

                        if self.output_id and self.output_id in outputs:
                            imgs = outputs[self.output_id].get("images", [])
                            if imgs:
                                img_info = imgs[0]

                        if not img_info:
                            for node_out in outputs.values():
                                if "images" in node_out and node_out["images"]:
                                    img_info = node_out["images"][0]
                                    break

                        if img_info:
                            fname = img_info["filename"]
                            sfolder = img_info["subfolder"]
                            itype = img_info["type"]
                            img_url = f"{self.url}/view?filename={fname}&subfolder={sfolder}&type={itype}"

                            async with session.get(img_url) as img_res:
                                if img_res.status == 200:
                                    return await img_res.read(), None, final_prompt
                                return None, "下载图片失败", final_prompt

                        return None, "工作流执行完成，但未找到输出图片", final_prompt

                return None, "生成超时", final_prompt
        finally:
            if override_wf_id is not None:
                self._apply_workflow_config(prev_wf_id)
