from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "ops"
TASK_DIR = OPS / "tasks"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}

EXPECTED_STAGES = {
    "source-shopify",
    "source-olx",
    "enrich",
    "compose",
    "send",
    "backup",
    "digest",
}


def _compose_services() -> dict[str, str]:
    """Return ``{service: block}`` for the top level of the compose file."""
    lines = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").splitlines()
    services: dict[str, list[str]] = {}
    in_services = False
    current: str | None = None
    for line in lines:
        if line.strip() == "services:" and not line.startswith(" "):
            in_services = True
            continue
        if not in_services:
            continue
        if line and not line.startswith(" "):
            break
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = match.group(1)
            services[current] = []
            continue
        if current is not None:
            services[current].append(line)
    return {name: "\n".join(block) for name, block in services.items()}


def test_dockerfile_builds_both_targets_with_a_non_root_user() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "AS slim" in dockerfile
    assert "AS browser" in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "FORGESMITH_DATA_DIR=/app/data" in dockerfile
    assert "FORGESMITH_BROWSER_NO_SANDBOX=true" in dockerfile
    assert "ENTRYPOINT [" not in dockerfile


def test_build_context_excludes_data_and_secrets() -> None:
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert "data" in ignored
    assert ".env" in ignored
    assert "!.env.example" in ignored
    assert "tests" in ignored


def test_every_compose_service_is_profiled() -> None:
    services = _compose_services()

    assert {"app", "app-live", "app-browser", "ollama", "db", "adminer"} <= set(services)
    for name, block in services.items():
        assert "profiles:" in block, f"{name} would start with a bare `compose up`"


def test_production_data_lives_on_a_named_volume() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "fs-data:/app/data" in compose
    assert "sqlite+aiosqlite:////app/data/forgesmith.db" in compose
    assert "./outbox:/app/data/outbox" in compose
    assert "./backups:/app/data/backups" in compose
    assert "host.docker.internal:host-gateway" in compose
    # The browser target must not overwrite the slim image tag.
    assert "image: forgesmith-browser:" in compose


def _task(stage: str) -> ET.Element:
    return ET.parse(TASK_DIR / f"forgesmith-{stage}.xml").getroot()


def test_task_set_is_exactly_the_scheduled_stages() -> None:
    stages = {
        path.name.removeprefix("forgesmith-").removesuffix(".xml")
        for path in TASK_DIR.glob("forgesmith-*.xml")
    }

    assert stages == EXPECTED_STAGES
    assert "approve" not in stages


def test_tasks_never_overlap_and_use_the_wrapper() -> None:
    for stage in EXPECTED_STAGES:
        task = _task(stage)
        policy = task.find("t:Settings/t:MultipleInstancesPolicy", NS)
        command = task.find("t:Actions/t:Exec/t:Command", NS)
        arguments = task.find("t:Actions/t:Exec/t:Arguments", NS)

        assert policy is not None and policy.text == "IgnoreNew"
        assert command is not None and command.text == "powershell.exe"
        assert arguments is not None
        assert "ops\\run.ps1" in arguments.text
        tail = arguments.text.split('run.ps1"', 1)[1].strip()
        assert tail.split()[0] == stage


def test_send_task_has_no_catch_up_and_no_live_flag() -> None:
    task = _task("send")
    availability = task.find("t:Settings/t:StartWhenAvailable", NS)
    arguments = task.find("t:Actions/t:Exec/t:Arguments", NS)

    assert availability is not None and availability.text == "false"
    assert arguments is not None
    # run.ps1 owns the four live gates; the task only supplies budgets.
    assert "--live" not in arguments.text
    assert "--max-runtime" in arguments.text


def test_runner_wrapper_has_a_busy_exit_and_no_approve_stage() -> None:
    runner = (OPS / "run.ps1").read_text(encoding="utf-8")

    assert "exit 75" in runner
    assert "Global\\forgesmith-" in runner
    assert "Profile = 'live'" in runner
    assert "Command = @('send', '--live', '--yes')" in runner
    assert "'approve'" not in runner


def test_linux_runner_is_single_flight() -> None:
    runner = (OPS / "run.sh").read_text(encoding="utf-8")

    assert "flock -n" in runner
    assert "exit 75" in runner
    assert "approve is never scheduled" in runner


def test_scheduled_backup_retains_a_bounded_set() -> None:
    runner = (OPS / "run.ps1").read_text(encoding="utf-8")
    linux_runner = (OPS / "run.sh").read_text(encoding="utf-8")

    assert "Command = @('backup', '--keep', '22')" in runner
    assert "backup --keep 22" in linux_runner


def test_installer_imports_every_task_and_refuses_approve() -> None:
    installer = (OPS / "install-tasks.ps1").read_text(encoding="utf-8")

    assert "schtasks.exe /Create /XML" in installer
    assert "approval is human-only" in installer
