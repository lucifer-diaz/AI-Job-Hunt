"""LiteLLMConfigManager: applies the part of the AI configuration that lives inside LiteLLM, and nothing else.

    desired snapshot (Job Hunt OS)      live state (LiteLLM /model/info)
                 └──────── plan: create / delete / conflicts ───────┘
                                      │ validate (credentials, name conflicts, LiteLLM's own auto-router check)
                                      ▼
            admin_api: POST /model/new, POST /model/delete        config_file: render → temp file → validate
                       (undone in reverse on any failure)                      → atomic replace → restart → verify
                                      │                                         (backup restored on failure)
                                      ▼
                              verify against /model/info

Managed by Job Hunt OS — and therefore the only things it creates, replaces or removes:
  * deployments of models marked *managed* in Settings → AI Models (for example a local Ollama model);
  * the auto router deployment, when "Job Hunt OS manages the auto router" is on.
Each carries `model_info.jobhunt_managed`, `jobhunt_owner` and `jobhunt_fingerprint`; ownership is never inferred
from a name. Everything else in LiteLLM — deployments defined in its own config, router_settings (including its
fallbacks), general_settings, keys, callbacks — is unmanaged and never touched. A desired name that an unmanaged
deployment already uses is refused rather than shadowed.

Worker assignments, generation settings, fallbacks and escalation are not stored in LiteLLM at all: they travel
with each request, so changing them needs no sync and no restart.

LiteLLM 1.102 facts this relies on (checked against the installed proxy, not assumed):
  * /model/new, /model/{id}/update and /model/delete only work with STORE_MODEL_IN_DB=True; YAML-defined
    deployments cannot be changed through them at all.
  * The proxy reads its config file only at startup (there is no file watcher or reload endpoint), so config_file
    mode needs a restart before a change is live. `include:` lists in the config are merged at startup, which is
    how the managed file joins the operator's config without Job Hunt OS ever editing that config.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.services.ai_workers import AUTO_ROUTER_TIERS
from app.services.litellm_client import (
    LiteLLMAPI,
    LiteLLMError,
    LiteLLMUnavailable,
    LiteLLMUnsupported,
)
from app.services.model_resolution import router_active
from app.services.redaction import redact_text
from app.settings import get_settings

MANAGED_MARK = "jobhunt_managed"
FILE_HEADER = "# Managed by Job Hunt OS. Do not edit: every Apply in Settings → AI Models rewrites this file."
AUTO_ROUTER_MODEL = "auto_router/complexity_router"
CLASSIFIER_TYPES = {"HEURISTIC": "heuristic", "HEURISTIC_V2": "heuristic_v2", "LLM": "llm", "CUSTOM": "custom"}
# litellm_params keys whose value must be a LiteLLM secret reference, never a literal secret.
SECRET_PARAMS = ("api_key", "aws_secret_access_key", "aws_access_key_id", "aws_session_token", "vertex_credentials",
                 "azure_ad_token", "client_secret")
SECRET_REFERENCE = "os.environ/"
VERIFY_WAIT_SECONDS = 10


def owner_tag(user_id: int) -> str:
    """Which Job Hunt OS install and account a managed deployment belongs to. A hash, never the secret itself."""
    settings = get_settings()
    seed = f"{settings.app_base_url}|{settings.secret_key or ''}".encode()
    return f"jobhunt:{hashlib.sha256(seed).hexdigest()[:10]}:{user_id}"


@dataclass(frozen=True)
class Deployment:
    model_name: str
    litellm_params: dict[str, Any]
    kind: str  # model | auto_router

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({"name": self.model_name, "params": self.litellm_params}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def model_info(self, owner: str) -> dict[str, Any]:
        return {MANAGED_MARK: True, "jobhunt_owner": owner, "jobhunt_fingerprint": self.fingerprint,
                "jobhunt_kind": self.kind}

    def public(self) -> dict[str, Any]:
        return {"model_name": self.model_name, "kind": self.kind, "litellm_params": self.litellm_params}


def auto_router_config(routing: dict[str, Any]) -> dict[str, Any]:
    """The complexity_router_config LiteLLM runs for the auto router (see LiteLLM's complexity router README)."""
    tiers = routing.get("tier_models") or {}
    config: dict[str, Any] = {"classifier_type": CLASSIFIER_TYPES.get(routing.get("classifier") or "", "heuristic"),
                              "tiers": {tier: tiers[tier] for tier in AUTO_ROUTER_TIERS if tiers.get(tier)}}
    if routing.get("default_model"):
        config["default_model"] = routing["default_model"]
    if routing.get("classifier") == "LLM" and routing.get("classifier_model"):
        config["classifier_llm_config"] = {"model": routing["classifier_model"]}
    if routing.get("classifier") == "CUSTOM" and routing.get("classifier_plugin"):
        config["classifier_plugin"] = routing["classifier_plugin"]
    return config


def render(snapshot: dict[str, Any]) -> list[Deployment]:
    """The deployments a configuration wants LiteLLM to have, in a stable order."""
    out = [Deployment(name, dict(entry["deployment_params"]), "model")
           for name, entry in sorted((snapshot.get("models") or {}).items())
           if entry.get("managed") and entry.get("enabled", True) and entry.get("deployment_params")]
    routing = snapshot.get("routing") or {}
    if router_active(snapshot) and routing.get("manage_auto_router"):
        out.append(Deployment(routing["auto_router_model_name"],
                              {"model": AUTO_ROUTER_MODEL, "complexity_router_config": auto_router_config(routing)},
                              "auto_router"))
    return out


def literal_secrets(params: dict[str, Any]) -> list[str]:
    """Keys that hold a literal credential instead of an `os.environ/NAME` reference."""
    found = []
    for key, value in params.items():
        if (key in SECRET_PARAMS or "secret" in key.lower()) and value and not (
                isinstance(value, str) and value.startswith(SECRET_REFERENCE)):
            found.append(key)
    return found


@dataclass
class LiveDeployment:
    model_name: str
    model_id: str | None
    owned: bool  # managed by this install and account
    managed_elsewhere: bool  # managed by another Job Hunt OS install or account
    fingerprint: str | None
    params: dict[str, Any] = field(default_factory=dict)


def classify_live(deployments: list[dict[str, Any]], owner: str) -> list[LiveDeployment]:
    out = []
    for item in deployments:
        info = item.get("model_info") or {}
        mine = bool(info.get(MANAGED_MARK)) and info.get("jobhunt_owner") == owner
        out.append(LiveDeployment(model_name=str(item.get("model_name")), model_id=info.get("id"), owned=mine,
                                  managed_elsewhere=bool(info.get(MANAGED_MARK)) and not mine,
                                  fingerprint=info.get("jobhunt_fingerprint"), params=item.get("litellm_params") or {}))
    return out


@dataclass
class SyncPlan:
    mode: str
    desired: list[Deployment]
    creates: list[Deployment] = field(default_factory=list)
    deletes: list[LiveDeployment] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    live_known: bool = False

    @property
    def needs_sync(self) -> bool:
        return bool(self.creates or self.deletes)

    def public(self) -> dict[str, Any]:
        restart = self.needs_sync and self.mode == "config_file"
        return {"mode": self.mode, "live_known": self.live_known, "needs_sync": self.needs_sync,
                "create": [d.public() for d in self.creates],
                "remove": [{"model_name": d.model_name, "model_id": d.model_id} for d in self.deletes],
                "unchanged": self.unchanged, "conflicts": self.conflicts, "restart_required": restart,
                "managed": [d.model_name for d in self.desired]}


@dataclass
class SyncResult:
    status: str  # SYNCED | PENDING | FAILED
    mode: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    verified: bool | None = None
    rolled_back: bool = False
    restart_required: bool = False
    message: str = ""
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {"status": self.status, "mode": self.mode, "operations": self.operations, "verified": self.verified,
                "rolled_back": self.rolled_back, "restart_required": self.restart_required,
                "message": self.message, "error": self.error}


class LiteLLMConfigManager:
    """One account's view of its LiteLLM: plan, validate, apply, verify."""

    def __init__(self, *, base_url: str | None, api_key: str | None, user_id: int, mode: str):
        self.base_url, self.api_key, self.user_id, self.mode = base_url, api_key, user_id, mode
        self.owner = owner_tag(user_id)

    def api(self) -> LiteLLMAPI:
        if not self.base_url:
            raise LiteLLMUnavailable("No LiteLLM base URL is configured")
        return LiteLLMAPI(self.base_url, self.api_key, user_id=self.user_id, service="config_sync")

    # ───────────── plan ─────────────

    def plan(self, snapshot: dict[str, Any], live: list[dict[str, Any]] | None) -> SyncPlan:
        desired = render(snapshot)
        plan = SyncPlan(mode=self.mode, desired=desired, live_known=live is not None)
        if live is None:
            # Unknown live state: everything desired is treated as needing a write; apply refuses in admin mode.
            plan.creates = list(desired)
            return plan
        classified = classify_live(live, self.owner)
        ours = [d for d in classified if d.owned]
        unmanaged_names = {d.model_name for d in classified if not d.owned}
        plan.conflicts = sorted({d.model_name for d in desired if d.model_name in unmanaged_names})
        live_prints = {d.fingerprint for d in ours}
        wanted = {d.fingerprint for d in desired}
        plan.creates = [d for d in desired if d.fingerprint not in live_prints and d.model_name not in plan.conflicts]
        plan.deletes = [d for d in ours if d.fingerprint not in wanted]
        plan.unchanged = sorted(d.model_name for d in desired if d.fingerprint in live_prints)
        return plan

    # ───────────── validate ─────────────

    def validate(self, plan: SyncPlan, snapshot: dict[str, Any]) -> tuple[list[str], list[str]]:
        """(errors, warnings) about applying the plan. Nothing here changes LiteLLM."""
        errors, warnings = [], []
        for name in plan.conflicts:
            errors.append(f"LiteLLM already has a deployment named {name} that Job Hunt OS does not manage; "
                          "rename the managed model instead of shadowing it")
        for deployment in plan.desired:
            if not deployment.litellm_params.get("model"):
                errors.append(f"{deployment.model_name}: the deployment needs a LiteLLM `model` (e.g. ollama_chat/…)")
            for key in literal_secrets(deployment.litellm_params):
                errors.append(f"{deployment.model_name}: {key} must be an os.environ/NAME reference, never a literal "
                              "secret")
        if plan.needs_sync:
            if self.mode == "disabled":
                errors.append("These changes live inside LiteLLM ("
                              + ", ".join(sorted({d.model_name for d in [*plan.creates, *plan.deletes]}))
                              + ") but LiteLLM synchronization is disabled. Choose admin_api or config_file under "
                                "AI Models → LiteLLM, or undo them")
            elif self.mode == "admin_api" and not plan.live_known:
                errors.append("LiteLLM is unreachable, so the change cannot be applied through its admin API")
            elif self.mode == "config_file":
                errors.extend(self._file_errors(warnings))
        router = next((d for d in plan.desired if d.kind == "auto_router"), None)
        if router is not None and plan.live_known and (plan.needs_sync or router in plan.creates):
            try:
                with self.api() as api:
                    problem = api.validate_complexity_router(router.litellm_params["complexity_router_config"])
                if problem:
                    errors.append(f"LiteLLM rejected the auto router config: {problem}")
            except LiteLLMUnsupported:
                warnings.append("This LiteLLM cannot validate an auto router config in advance; it is checked when "
                                "the router is created")
            except LiteLLMError as exc:
                warnings.append(f"The auto router config could not be validated by LiteLLM: {exc}")
        return errors, warnings

    def _file_errors(self, warnings: list[str]) -> list[str]:
        settings = get_settings()
        path = settings.litellm_managed_config_path
        if not path:
            return ["config_file mode needs LITELLM_MANAGED_CONFIG_PATH: the one file Job Hunt OS may write"]
        errors = []
        target = Path(path).expanduser()
        if target.exists():
            head = target.read_text(encoding="utf-8").lstrip()
            if head and not head.startswith(FILE_HEADER):
                errors.append(f"{target} exists and was not written by Job Hunt OS; it will not be overwritten")
            elif head and f"# owner: {self.owner}" not in head:
                errors.append(f"{target} is managed by another Job Hunt OS account or install")
        elif not target.parent.is_dir():
            errors.append(f"The directory for {target} does not exist")
        main = settings.litellm_config_path
        if not main:
            warnings.append("Set LITELLM_CONFIG_PATH so Job Hunt OS can check that LiteLLM's config includes the "
                            "managed file (it is only read, never written)")
            return errors
        try:
            loaded = yaml.safe_load(Path(main).expanduser().read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            return [*errors, f"LiteLLM's config {main} could not be read: {type(exc).__name__}"]
        includes = loaded.get("include") or [] if isinstance(loaded, dict) else []
        base = Path(main).expanduser().resolve().parent
        if not any((base / str(entry)).resolve() == target.resolve() for entry in includes):
            errors.append(f"LiteLLM's config {main} does not include the managed file. Add\n"
                          f"include:\n  - {os.path.relpath(target.resolve(), base)}\nto it once (Job Hunt OS never "
                          "edits that file)")
        return errors

    # ───────────── apply ─────────────

    def apply(self, plan: SyncPlan, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> SyncResult:
        if self.mode == "config_file":
            return self._apply_file(snapshot)
        if not plan.needs_sync:
            verified, _ = self.verify(snapshot) if plan.live_known and plan.desired else (plan.live_known or None, [])
            return SyncResult("SYNCED", self.mode, verified=verified,
                              message="Nothing to change inside LiteLLM; the new settings apply to the next request")
        return self._apply_admin(plan, snapshot, previous)

    def _apply_admin(self, plan: SyncPlan, snapshot: dict[str, Any], previous: dict[str, Any] | None) -> SyncResult:
        operations: list[dict[str, Any]] = []
        undo: list[tuple[str, Any]] = []
        known = {d.fingerprint: d for d in render(previous or {})}
        try:
            with self.api() as api:
                # Create first, then remove: a replaced deployment never leaves its model name unserved.
                for deployment in plan.creates:
                    model_id = api.new_model(deployment.model_name, deployment.litellm_params,
                                             deployment.model_info(self.owner))
                    undo.append(("delete", model_id or deployment))
                    operations.append({"op": "create", "model_name": deployment.model_name, "model_id": model_id})
                for live in plan.deletes:
                    if live.model_id:
                        api.delete_model(live.model_id)
                    undo.append(("create", known.get(live.fingerprint or "")
                                 or Deployment(live.model_name, live.params, "model")))
                    operations.append({"op": "delete", "model_name": live.model_name, "model_id": live.model_id})
        except LiteLLMError as exc:
            rolled_back = self._undo(undo)
            hint = " Set STORE_MODEL_IN_DB=True on the LiteLLM proxy to allow runtime changes." \
                if "STORE_MODEL_IN_DB" in str(exc) else ""
            return SyncResult("FAILED", self.mode, operations, rolled_back=rolled_back,
                              error=f"{exc}{hint}", message="LiteLLM rejected the change; the previous LiteLLM "
                              "configuration was restored" if rolled_back else "LiteLLM rejected the change and "
                              "could not be fully restored; check it before applying again")
        verified, differences = self._verify_with_wait(snapshot)
        if verified is False:
            rolled_back = self._undo(undo)
            return SyncResult("FAILED", self.mode, operations, verified=False, rolled_back=rolled_back,
                              error="LiteLLM did not report the new configuration: " + "; ".join(differences)[:500],
                              message="Verification failed; the previous LiteLLM configuration was restored"
                              if rolled_back else "Verification failed and the rollback was incomplete")
        return SyncResult("SYNCED", self.mode, operations, verified=verified,
                          message="Applied through the LiteLLM admin API and verified")

    def _undo(self, undo: list[tuple[str, Any]]) -> bool:
        """Reverse the operations already applied, newest first. True when every one was reversed."""
        complete = True
        try:
            with self.api() as api:
                for action, target in reversed(undo):
                    try:
                        if action == "delete":
                            model_id = target if isinstance(target, str) else self._id_of(api, target)
                            if model_id:
                                api.delete_model(model_id)
                        else:
                            api.new_model(target.model_name, target.litellm_params, target.model_info(self.owner))
                    except LiteLLMError:
                        complete = False
        except LiteLLMError:
            return not undo
        return complete

    def _id_of(self, api: LiteLLMAPI, deployment: Deployment) -> str | None:
        for live in classify_live(api.model_info(), self.owner):
            if live.owned and live.fingerprint == deployment.fingerprint:
                return live.model_id
        return None

    def render_file(self, snapshot: dict[str, Any]) -> str:
        entries = [{"model_name": d.model_name, "litellm_params": d.litellm_params,
                    "model_info": d.model_info(self.owner)} for d in render(snapshot)]
        body = yaml.safe_dump({"model_list": entries}, sort_keys=False, default_flow_style=False)
        return f"{FILE_HEADER}\n# owner: {self.owner}\n{body}"

    def _apply_file(self, snapshot: dict[str, Any]) -> SyncResult:
        settings = get_settings()
        target = Path(settings.litellm_managed_config_path or "").expanduser()
        text = self.render_file(snapshot)
        current = target.read_text(encoding="utf-8") if target.exists() else None
        if current is None and not render(snapshot):
            return SyncResult("SYNCED", self.mode, message="Nothing is managed inside LiteLLM; no file to write")
        if current == text:
            verified, _ = self.verify(snapshot)
            return SyncResult("SYNCED" if verified is not False else "PENDING", self.mode, verified=verified,
                              restart_required=verified is False,
                              message="The managed file already matches" + (
                                  "" if verified is not False else "; LiteLLM has not loaded it yet (restart it)"))
        # Validate the rendered file before it can replace anything.
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, dict) or not all(
                isinstance(e, dict) and e.get("model_name") and (e.get("litellm_params") or {}).get("model")
                for e in parsed.get("model_list") or []):
            return SyncResult("FAILED", self.mode, error="The rendered LiteLLM config is not valid")
        backup = target.with_name(target.name + ".jobhunt-backup")
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            if current is not None:
                backup.write_text(current, encoding="utf-8")
            os.replace(temp_name, target)  # atomic on the same filesystem: never a half-written config
        except OSError as exc:
            Path(temp_name).unlink(missing_ok=True)
            return SyncResult("FAILED", self.mode, error=f"Could not write {target}: {type(exc).__name__}")
        operations = [{"op": "write_file", "path": str(target), "deployments": len(parsed.get("model_list") or [])}]
        if not settings.litellm_restart_command:
            return SyncResult("PENDING", self.mode, operations, restart_required=True,
                              message=f"Wrote {target}. LiteLLM reads its config only at startup: restart it, then "
                                      "use Verify. Until then LiteLLM still runs the previous configuration")
        restarted, detail = self._restart()
        verified, differences = self._verify_with_wait(snapshot) if restarted else (False, [detail])
        if verified:
            return SyncResult("SYNCED", self.mode, operations, verified=True,
                              message="Managed file replaced, LiteLLM restarted and verified")
        # Put the previous file back and restart onto it, so LiteLLM is never left half-configured.
        if current is None:
            target.unlink(missing_ok=True)
        elif backup.exists():
            os.replace(backup, target)
        else:
            target.write_text(current, encoding="utf-8")
        restored, _ = self._restart()
        return SyncResult("FAILED", self.mode, operations, verified=False, rolled_back=restored,
                          error=("LiteLLM did not come back with the new configuration: "
                                 + "; ".join(differences))[:500],
                          message="The previous managed file was restored" + (
                              " and LiteLLM restarted onto it" if restored else "; restart LiteLLM manually"))

    def _restart(self) -> tuple[bool, str]:
        settings = get_settings()
        try:
            completed = subprocess.run(shlex.split(settings.litellm_restart_command or ""), capture_output=True,
                                       timeout=120, check=False)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return False, f"restart command failed: {type(exc).__name__}"
        if completed.returncode != 0:
            return False, redact_text(f"restart command exited {completed.returncode}: "
                                      f"{completed.stderr.decode(errors='replace')[-200:]}", 300) or ""
        deadline = time.monotonic() + max(5, settings.litellm_restart_wait_seconds)
        while time.monotonic() < deadline:
            try:
                with self.api() as api:
                    api.readiness()
                return True, ""
            except LiteLLMError:
                time.sleep(2)
        return False, "LiteLLM did not become ready after the restart"

    # ───────────── verify ─────────────

    def verify(self, snapshot: dict[str, Any]) -> tuple[bool | None, list[str]]:
        """Whether LiteLLM runs exactly the managed deployments the snapshot wants. None when it cannot be read."""
        try:
            with self.api() as api:
                live = api.model_info()
        except LiteLLMError:
            return None, ["LiteLLM is unreachable"]
        plan = self.plan(snapshot, live)
        differences = [f"missing {d.model_name}" for d in plan.creates] + \
                      [f"unexpected {d.model_name}" for d in plan.deletes] + \
                      [f"{name} is defined outside Job Hunt OS" for name in plan.conflicts]
        return not differences, differences

    def _verify_with_wait(self, snapshot: dict[str, Any]) -> tuple[bool | None, list[str]]:
        deadline = time.monotonic() + VERIFY_WAIT_SECONDS
        verified, differences = self.verify(snapshot)
        while verified is False and time.monotonic() < deadline:
            time.sleep(1)
            verified, differences = self.verify(snapshot)
        return verified, differences
