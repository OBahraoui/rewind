"""Settings loaded from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(RuntimeError):
    """A required setting is missing or malformed."""


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Load KEY=VALUE lines into os.environ without overriding real env vars.

    Later lines win over earlier ones (so `rewind keys >> .env` works), and empty values are skipped
    so a blank template line never masks a default.
    """
    p = Path(path)
    if not p.is_file():
        return
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            values[key] = value
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class FlowRef:
    """How to start one Duo flow: a foundational definition or an AI Catalog consumer."""

    kind: str  # "definition" | "catalog"
    value: str

    @classmethod
    def parse(cls, raw: str) -> FlowRef:
        if raw.strip().lower() in {"none", "off", "skip"}:
            return cls("none", "")
        kind, sep, value = raw.partition(":")
        if not sep or kind not in {"definition", "catalog"} or not value:
            raise ConfigError(f"flow reference must be 'definition:<name>', 'catalog:<id>' or 'none', got {raw!r}")
        return cls(kind, value)

    @property
    def enabled(self) -> bool:
        return self.kind != "none"

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}" if self.enabled else "none"


@dataclass(frozen=True)
class Settings:
    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = ""
    project_id: str = ""
    namespace_id: str = ""
    webhook_secret: str = ""

    store_url: str = "file://.rewind"
    hash_key: str = ""
    api_token: str = ""
    public_url: str = ""
    policy_path: str = ""
    trigger_label: str = "rewind:auto"
    tick_seconds: int = 15

    flows: dict[str, FlowRef] = field(default_factory=dict)
    max_fix_attempts: int = 2
    max_review_fix_attempts: int = 1  # times the developer agent addresses a review's requested changes
    max_flows_per_day: int = 25  # credit guard: flows Rewind may start per UTC day

    gcp_project: str = ""
    gcp_region: str = "europe-west1"
    cloud_run_services: dict[str, str] = field(default_factory=dict)  # environment -> service
    rewind_service: str = "rewind"

    @classmethod
    def from_env(cls, dotenv: bool = True) -> Settings:
        if dotenv:
            load_dotenv()
        flows = {
            step: FlowRef.parse(_env(f"REWIND_FLOW_{step.upper()}", default))
            for step, default in {
                "plan": "none",  # set to catalog:<id> once flows/rewind_planner.yml is enabled
                "develop": "definition:developer/v1",
                "review": "definition:code_review/v1",
                "fix": "definition:fix_pipeline/v1",
            }.items()
        }
        services = {
            env: svc
            for env, svc in {
                "staging": _env("CLOUD_RUN_SERVICE_STAGING", "rewind-demo-staging"),
                "production": _env("CLOUD_RUN_SERVICE_PRODUCTION", "rewind-demo-production"),
            }.items()
            if svc
        }
        return cls(
            gitlab_url=_env("GITLAB_URL", "https://gitlab.com").rstrip("/"),
            gitlab_token=_env("GITLAB_TOKEN"),
            project_id=_env("GITLAB_PROJECT_ID"),
            namespace_id=_env("GITLAB_NAMESPACE_ID"),
            webhook_secret=_env("GITLAB_WEBHOOK_SECRET"),
            store_url=_env("REWIND_STORE", "file://.rewind"),
            hash_key=_env("REWIND_HASH_KEY"),
            api_token=_env("REWIND_API_TOKEN"),
            public_url=_env("REWIND_PUBLIC_URL").rstrip("/"),
            policy_path=_env("REWIND_POLICY_PATH"),
            trigger_label=_env("REWIND_TRIGGER_LABEL", "rewind:auto"),
            tick_seconds=int(_env("REWIND_TICK_SECONDS", "15") or 15),
            flows=flows,
            max_fix_attempts=int(_env("REWIND_MAX_FIX_ATTEMPTS", "2") or 2),
            max_review_fix_attempts=int(_env("REWIND_MAX_REVIEW_FIX_ATTEMPTS", "1") or 1),
            max_flows_per_day=int(_env("REWIND_MAX_FLOWS_PER_DAY", "25") or 25),
            gcp_project=_env("GCP_PROJECT_ID"),
            gcp_region=_env("GCP_REGION", "europe-west1"),
            cloud_run_services=services if _env("GCP_PROJECT_ID") else {},
            rewind_service=_env("CLOUD_RUN_SERVICE_REWIND", "rewind"),
        )

    def require(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, n)]
        if missing:
            env_names = {
                "gitlab_token": "GITLAB_TOKEN",
                "project_id": "GITLAB_PROJECT_ID",
                "hash_key": "REWIND_HASH_KEY",
                "webhook_secret": "GITLAB_WEBHOOK_SECRET",
                "api_token": "REWIND_API_TOKEN",
                "gcp_project": "GCP_PROJECT_ID",
                "public_url": "REWIND_PUBLIC_URL",
            }
            raise ConfigError("missing settings: " + ", ".join(env_names.get(n, n) for n in missing))
