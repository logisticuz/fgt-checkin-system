"""Check service pins; regenerate from the tested venv with --write-locks."""

import re
import sys
from collections import deque
from importlib import metadata
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
SERVICES = ("backend", "fgt_dashboard")
PYTHON_VERSION = "3.11.10"
PYTHON_IMAGE = f"python:{PYTHON_VERSION}-slim-bookworm"


def requirements(path):
    return [
        Requirement(line.split("#", 1)[0].strip())
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    ]


def installed_lock(service):
    """Walk installed metadata for Linux CPython, including requested extras only."""
    environment = {
        **default_environment(),
        "implementation_name": "cpython",
        "implementation_version": PYTHON_VERSION,
        "os_name": "posix",
        "platform_machine": "x86_64",
        "platform_python_implementation": "CPython",
        "platform_release": "",
        "platform_system": "Linux",
        "platform_version": "",
        "python_full_version": PYTHON_VERSION,
        "python_version": "3.11",
        "sys_platform": "linux",
    }
    pending = deque(requirements(ROOT / service / "requirements.txt"))
    pins, extras = {}, {}
    while pending:
        requirement = pending.popleft()
        if requirement.marker and not requirement.marker.evaluate({**environment, "extra": ""}):
            continue
        distribution = metadata.distribution(requirement.name)
        name = canonicalize_name(requirement.name)
        if requirement.url or distribution.version not in requirement.specifier:
            raise ValueError(
                f"Installed {name}=={distribution.version} does not satisfy {requirement}"
            )
        python_specifier = distribution.metadata.get("Requires-Python", "")
        if PYTHON_VERSION not in SpecifierSet(python_specifier):
            raise ValueError(f"{name} requires Python {python_specifier}")
        requested = extras.get(name, set()) | set(requirement.extras)
        if name in pins and requested == extras[name]:
            continue
        pins[name], extras[name] = distribution.version, requested
        for dependency in map(Requirement, distribution.requires or []):
            if dependency.marker is None or any(
                dependency.marker.evaluate({**environment, "extra": extra})
                for extra in ("", *sorted(requested))
            ):
                # The parent extra has been evaluated; downstream extras belong to dependency.
                dependency.marker = None
                pending.append(dependency)
    return "".join(f"{name}=={version}\n" for name, version in sorted(pins.items()))


@pytest.mark.parametrize("service", SERVICES)
def test_lock_has_only_exact_unique_pins_and_covers_top_level_intent(service):
    path = ROOT / service / "requirements.lock"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.!+_-]*", line)
        for line in lines
    )
    locked = requirements(path)
    pins = {canonicalize_name(req.name): next(iter(req.specifier)).version for req in locked}
    assert len(pins) == len(lines)
    for requirement in requirements(ROOT / service / "requirements.txt"):
        assert pins[canonicalize_name(requirement.name)] in requirement.specifier
    assert pins["psycopg-binary"] == pins["psycopg"]
    if service == "backend":
        assert pins["cryptography"] == "44.0.0"


@pytest.mark.parametrize("service", SERVICES)
def test_dockerfile_copies_and_installs_its_lock_with_a_pinned_python_image(service):
    dockerfile = (ROOT / service / "Dockerfile").read_text(encoding="utf-8")
    assert f"FROM {PYTHON_IMAGE}\n" in dockerfile
    copy_source = "requirements.lock" if service == "backend" else "fgt_dashboard/requirements.lock"
    assert re.search(rf"^COPY {re.escape(copy_source)} \.\/?$", dockerfile, re.MULTILINE)
    assert "RUN pip install --no-cache-dir -r requirements.lock\n" in dockerfile
    assert dockerfile.index(f"COPY {copy_source}") < dockerfile.index("RUN pip install")


@pytest.mark.parametrize("name", ["docker-compose.dev.yml", "docker-compose.prod.yml"])
def test_nginx_uses_the_same_release_index_as_prod(name):
    compose = (ROOT / name).read_text(encoding="utf-8")
    images = re.findall(r"^\s+image:\s*(nginx:\S+)\s*$", compose, re.MULTILINE)
    assert images == [
        "nginx:1.29.4@sha256:"
        "c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec"
    ]


def test_nginx_smoke_uses_the_reviewed_release_index():
    smoke = (ROOT / "tests" / "security_smoke.sh").read_text(encoding="utf-8")
    images = re.findall(r"^\s+(nginx:\S+)\s+>/dev/null$", smoke, re.MULTILINE)
    expected = (
        "nginx:1.29.4@sha256:"
        "c881927c4077710ac4b1da63b83aa163937fb47457950c267d92f7e4dedf4aec"
    )
    assert images == [expected, expected]


def test_n8n_uses_the_same_release_and_multiarch_index_on_dev_and_prod():
    expected = (
        "n8nio/n8n:2.4.8@sha256:"
        "cae1f28d585a99d9ce9a56f82fbfc99fc100862ffef988d73fa2048d2687b0cc"
    )
    for name in ("docker-compose.dev.yml", "docker-compose.prod.yml"):
        compose = (ROOT / name).read_text(encoding="utf-8")
        images = re.findall(r"^\s+image:\s*(n8nio/n8n:\S+)\s*$", compose, re.MULTILINE)
        assert images == [expected]
        assert not re.search(r":(?:latest|stable)(?:@|$)", images[0])


@pytest.mark.parametrize("name", ["docker-compose.dev.yml", "docker-compose.prod.yml"])
def test_compose_images_do_not_use_latest_or_stable_tags(name):
    compose = (ROOT / name).read_text(encoding="utf-8")
    images = re.findall(r"^\s+image:\s*(\S+)\s*$", compose, re.MULTILINE)
    assert images
    assert all(not re.search(r":(?:latest|stable)(?:@|$)", image) for image in images)


if __name__ == "__main__":
    if sys.argv[1:] != ["--write-locks"]:
        raise SystemExit("Usage: python -m tests.test_dependency_pins --write-locks")
    # Derive both before writing either so a missing/incompatible distribution fails early.
    generated = {service: installed_lock(service) for service in SERVICES}
    for service, content in generated.items():
        (ROOT / service / "requirements.lock").write_text(content, encoding="utf-8", newline="\n")
        print(f"{service}: {len(content.splitlines())} pinned distributions")
