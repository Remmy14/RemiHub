"""Pinned dossier reviewer for the independent codex-review worker.

The authenticated app-server is trusted control plane. No model tools are
registered. A credential-free transport rehearsal must prove this for the exact
binary, bundled model catalog and effective configuration before signed-in use.
This is a tool-removal boundary, not an OS sandbox for arbitrary Codex tools.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import threading

MAX_BYTES = 512 * 1024
MAX_FILES = 256
MAX_WIRE_BYTES = 2 * 1024 * 1024
SUPPORTED_VERSION = "0.162.1"
PINNED_CLI_SHA256 = "2c7a3b7a82c9faf9db5a020b80176ec48081d4b58166f59be0940e85e8c16060"
PINNED_MODEL = "gpt-6-sol"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def private_directory(path):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("Control directory must not traverse symlinks")
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Control directory must be owned and mode 0700")
    return path


def dossier(repository_path: Path, prompt: str) -> str:
    """Include the ENTIRE frozen tree or block. Never truncate or select context.

    Callers must use the existing trusted freeze/identity/evidence validation.
    The exact binary patch is carried independently of its display rendering.
    """
    if len(prompt.encode()) > MAX_WIRE_BYTES:
        raise ValueError("Complete dossier context exceeds review budget")
    context = json.loads(prompt)
    raw = base64.b64decode(context.pop("raw_diff_b64"), validate=True)
    tests = context.get("validation")
    if not isinstance(tests, list) or not tests or any(not isinstance(t, dict) or t.get("status") != "passed" for t in tests):
        raise ValueError("Complete passing validation evidence required")
    root = Path(repository_path)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid frozen tree")
    budget = len(raw) + len(prompt.encode())
    selection = context.pop("source_selection", None)
    if selection is not None:
        from backend.core.codex_review_dossier import selected_dossier
        return selected_dossier(root, context, raw, selection)
    files = []
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Frozen dossier rejects links and special files")
        if len(files) >= MAX_FILES or budget + info.st_size > MAX_BYTES:
            raise ValueError("Complete dossier exceeds review budget")
        # O_NOFOLLOW prevents a final-component symlink substitution. The trusted
        # frozen tree is private; hostile same-UID mutation is outside the boundary.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            content = stream.read(MAX_BYTES + 1)
        budget += len(content)
        if budget > MAX_BYTES:
            raise ValueError("Complete dossier exceeds review budget")
        entry = {"path": path.relative_to(root).as_posix(), "mode": stat.S_IMODE(info.st_mode),
                 "size": len(content), "sha256": sha(content), "bytes_b64": base64.b64encode(content).decode()}
        try:
            entry["text"] = content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Non-text source requires a different review path") from None
        files.append(entry)
    if not files:
        raise ValueError("Empty frozen dossier")
    payload = {"format": "remihub-frozen-review-v1", "complete_tree": True,
               "context": context, "raw_diff_b64": base64.b64encode(raw).decode(),
               "raw_diff_sha256": sha(raw), "files": files}
    wire = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    if len(wire.encode()) > MAX_WIRE_BYTES:
        raise ValueError("Complete encoded dossier exceeds review budget")
    return wire


def tool_config(features):
    """Explicit CLI overrides; an empty TOML table alone is additive, not a reset."""
    return {**{"features." + f: False for f in features},
            "web_search": "disabled", "tools.update_plan.enabled": False,
            "tools.experimental_request_user_input.enabled": False,
            "skills.include_instructions": False, "cloud.skills.enabled": False,
            "project_doc_max_bytes": 0, "agents.enabled": False,
            "mcp_servers": {}, "apps._default.enabled": False,
            "plugins._default.enabled": False, "analytics.enabled": False,
            "sandbox_mode": "read-only", "approval_policy": "never",
            "cli_auth_credentials_store": "file", "forced_login_method": "chatgpt"}


def toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
    return json.dumps(value)


class BoundedReviewGateway:
    """Opt-in compatible run_turn/prepare/cancel adapter using official low-level SDK.

    `qualifier` is trusted host code, called before opening the signed-in home.
    No caller-supplied model config, provider URL, tool handler or API key exists.
    """
    requires_raw_diff = True
    requires_real_dossier = True

    def __init__(self, *, codex_bin, control_home, qualifier, timeout_seconds=120):
        supplied_binary = Path(codex_bin)
        if not supplied_binary.is_absolute():
            raise ValueError("Reviewer CLI requires absolute path")
        self.codex_bin = supplied_binary.resolve(strict=True)
        self.control_home = private_directory(control_home)
        for inherited in (os.environ.get('HOME'), os.environ.get('CODEX_HOME')):
            if inherited and Path(inherited).resolve() == self.control_home:
                raise ValueError("Reviewer requires a dedicated home separate from worker login")
        if any((self.control_home / name).exists() for name in ("config.toml", "requirements.toml", ".codex")):
            raise ValueError("Dedicated control home must not contain user configuration")
        self.qualifier = qualifier
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._client = None
        self._cancelled = False

    def prepare(self):
        with self._lock:
            self._cancelled = False

    def cancel(self):
        with self._lock:
            self._cancelled = True
            client = self._client
        if client is not None:
            # Closing private stdio destroys this dedicated app-server, including
            # initialization waits. Never connects to a shared daemon.
            client.close()

    def run_turn(self, *, repository_path, prompt, model):
        if not model:
            raise ValueError("A fixed qualified model is required")
        root = Path(repository_path).resolve(strict=True)
        if root == self.control_home or root in self.control_home.parents:
            raise ValueError("Control home must be outside candidate tree")
        context = json.loads(prompt)
        if "source_selection" not in context:
            raise KeyError("Verified real-card source selection required")
        wire = dossier(Path(repository_path), prompt)
        with Runtime(self.codex_bin, model) as runtime:
            runtime.cancel_requested = lambda: self._cancelled
            proof = self.qualifier(runtime)
            if proof != runtime.identity:
                raise ValueError("Qualification identity mismatch")
            return self._run(runtime, wire, home=self.control_home)

    def _run(self, runtime, wire, *, home, provider=None, output_schema=None):
        from openai_codex.client import CodexClient
        from pydantic import BaseModel
        class EffectiveConfig(BaseModel):
            config: dict
        from backend.core.codex_review import REVIEW_INSTRUCTIONS, REVIEW_SCHEMA, parse_verdict
        stopped = threading.Event()
        def watch_cancel():
            while not stopped.wait(0.05):
                if getattr(runtime, "cancel_requested", lambda: False)():
                    self.cancel()
                    return
        watcher = threading.Thread(target=watch_cancel, daemon=True)
        timer = threading.Timer(self.timeout_seconds, self.cancel)
        timer.daemon = True
        # No repository, tool sandbox, host filesystem or authentication material
        # is given to the model. This private empty cwd is control-plane state.
        with tempfile.TemporaryDirectory(prefix="bounded-review-cwd-") as cwd:
            client = CodexClient(runtime.sdk_config(home, cwd, provider))
            with self._lock:
                if self._cancelled:
                    raise TimeoutError("Review cancelled")
                self._client = client
            timer.start()
            watcher.start()
            try:
                client.start()
                with self._lock:
                    if self._cancelled:
                        raise TimeoutError("Review interrupted before initialization")
                client.initialize()
                if provider is None:
                    account = client.account_read({'refreshToken': False}).model_dump(mode='json')
                    if not isinstance(account.get('account'), dict) or account['account'].get('type') != 'chatgpt':
                        raise ValueError("Dedicated reviewer requires ChatGPT account authentication")
                effective = client.request("config/read", {"includeLayers": True, "cwd": cwd},
                                           response_model=EffectiveConfig).model_dump(by_alias=True, mode="json")
                runtime.check_effective(effective, provider, home=home, cwd=cwd)
                thread = client.thread_start({"cwd": cwd, "model": runtime.model,
                    "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": True,
                    "environments": [], "dynamicTools": [], "config": runtime.config,
                    "baseInstructions": "Review only the supplied frozen dossier. No tools exist. Omitted context is unavailable; block if needed to establish correctness. Never infer whole-tree coverage.",
                    "developerInstructions": REVIEW_INSTRUCTIONS})
                thread_id = thread.thread.id
                started = client.turn_start(thread_id, wire, {"cwd": cwd,
                    "approvalPolicy": "never", "sandboxPolicy": {"type": "readOnly"},
                    "outputSchema": output_schema or REVIEW_SCHEMA})
                turn_id = started.turn.id
                client.register_turn_notifications(turn_id)
                messages = []
                try:
                    while True:
                        event = client.next_turn_notification(turn_id)
                        payload = event.payload.model_dump(by_alias=True, mode="json")
                        if event.method == "item/completed":
                            item = payload["item"]
                            if item["type"] == "agentMessage":
                                messages.append(item["text"])
                            elif item["type"] not in {"reasoning", "userMessage"}:
                                if provider and item["type"] == "mcpToolCall" and item.get("status") == "failed":
                                    continue
                                raise ValueError("Unexpected reviewer tool/activity item: " + (str(item)[:1500] if provider else item["type"]))
                        if event.method == "turn/completed":
                            if payload["turn"]["status"] != "completed":
                                raise RuntimeError("Bounded reviewer did not complete" + ((": " + str(payload["turn"].get("error"))) if provider else ""))
                            break
                finally:
                    client.unregister_turn_notifications(turn_id)
                with self._lock:
                    if self._cancelled:
                        raise TimeoutError("Review interrupted")
                response = messages[-1] if messages else ""
                parse_verdict(response)
                return {"response": response, "thread_id": thread_id, "turn_id": turn_id,
                        "dossier_sha256": sha(wire.encode()), "boundary": "qualified-no-tools",
                        "qualification": runtime.identity}
            finally:
                timer.cancel()
                stopped.set()
                watcher.join(1)
                client.close()
                with self._lock:
                    self._client = None


class Runtime:
    """Freeze binary, feature switches and bundled model catalog for one run."""
    def __init__(self, binary, model):
        self.binary = Path(binary).resolve(strict=True)
        self.model = model
        if model != PINNED_MODEL:
            raise ValueError("Unqualified reviewer model")
        if sha(self.binary.read_bytes()) != PINNED_CLI_SHA256:
            raise ValueError("Unqualified reviewer CLI hash")
        self._directory = tempfile.TemporaryDirectory(prefix="bounded-review-runtime-")
        self.root = Path(self._directory.name)
        self.identity = {}
        self.effective_baseline = None

    def __enter__(self):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root), "CODEX_HOME": str(self.root)}
        def call(*args):
            return subprocess.run([str(self.binary), *args], env=env, cwd=self.root,
                                  check=True, capture_output=True, timeout=15).stdout
        version = call("--version").decode().strip()
        if version != "codex-cli " + SUPPORTED_VERSION:
            raise ValueError("Unqualified Codex version: " + version)
        features = call("features", "list").decode()
        self.config = tool_config([line.split()[0] for line in features.splitlines()
                                  if line.strip() and line.split()[-2] != "removed" and "." not in line.split()[0]])
        catalog = call("debug", "models", "--bundled")
        if self.model not in {m["slug"] for m in json.loads(catalog)["models"]}:
            raise ValueError("Model absent from pinned bundled catalog")
        self.catalog = self.root / "catalog.json"
        self.catalog.write_bytes(catalog)
        self.config["model_catalog_json"] = str(self.catalog)
        self.config["model"] = self.model
        self.identity = {"cli_sha256": sha(self.binary.read_bytes()), "version": version,
                         "catalog_sha256": sha(catalog), "model": self.model,
                         "policy_sha256": sha(json.dumps({k: v for k, v in self.config.items()
                            if k != "model_catalog_json"}, sort_keys=True).encode())}
        return self

    def __exit__(self, *args):
        self._directory.cleanup()

    def sdk_config(self, home, cwd, provider=None):
        from openai_codex import CodexConfig
        # env -i is necessary: CodexConfig.env MERGES with the parent environment.
        args = ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin", "HOME=" + str(home),
                "CODEX_HOME=" + str(home), str(self.binary)]
        config = dict(self.config)
        if provider:
            config.update({"model_provider": "synthetic", "model_providers.synthetic": provider})
        else:
            config["model_provider"] = "openai"
        for key, value in config.items():
            args.extend(["--config", key + "=" + toml(value)])
        args.extend(["app-server", "--listen", "stdio://"])
        return CodexConfig(launch_args_override=tuple(args), cwd=str(cwd))

    def check_effective(self, response, provider=None, *, home=None, cwd=None):
        cfg = response["config"]
        for key, expected in self.config.items():
            value = cfg
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            # Removed flags may be silently omitted; catalog qualification checks
            # their actual effect. All current flags must report false.
            if key.startswith("tools."):
                # These per-thread overrides are absent from config/read in
                # 0.162.1. The transport qualification must prove their effect.
                continue
            if key.startswith("features.") and value is None:
                continue
            if value != expected:
                raise ValueError("Ineffective reviewer override: " + key)
        # Empty objects are additive in CLI config. Assert no inherited entries.
        if cfg.get("mcp_servers") or cfg.get("notify"):
            raise ValueError("Inherited executable configuration")
        if cfg.get("model_provider") != ("synthetic" if provider else "openai"):
            raise ValueError("Unexpected provider")
        # Compare the whole effective configuration across synthetic and signed
        # control processes. Provider selection and private scratch paths are the
        # only permitted differences. Empty-table CLI overrides never erase an
        # inherited executable entry, so comparing only requested keys is unsafe.
        normalized = json.loads(json.dumps(cfg))
        normalized["model_provider"] = "<provider>"
        normalized.get("model_providers", {}).pop("synthetic", None)
        encoded = json.dumps(normalized, sort_keys=True)
        for value in (str(self.root), str(home) if home else None, str(cwd) if cwd else None):
            if value:
                encoded = encoded.replace(value, "<private>")
        if self.effective_baseline is None:
            self.effective_baseline = encoded
        elif encoded != self.effective_baseline:
            raise ValueError("Effective configuration differs from qualification")
        return cfg


def development_gateway(*, codex_bin, control_home, timeout_seconds=180):
    """Select the trusted packaged qualifier without validation-script imports."""
    from backend.core.codex_review_qualification import qualify
    return BoundedReviewGateway(codex_bin=codex_bin, control_home=control_home,
                                qualifier=qualify, timeout_seconds=timeout_seconds)
