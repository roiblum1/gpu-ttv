"""Image build, image lock, GitLab CI and offline scripts (ADR-0007; AGENT.md §15, §20.8).

Every pinned reference in the Containerfile and .gitlab-ci.yml must be in offline/images.lock by
digest; the offline scripts run against stub `skopeo`/`oc` binaries, so nothing leaves the machine.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "offline/images.lock"
LOCK_LINE = re.compile(
    r"^(?P<host>[a-z0-9.-]+)/(?P<path>[a-z0-9._/-]+):(?P<tag>[A-Za-z0-9._-]+)"
    r"@(?P<digest>sha256:[0-9a-f]{64}) \S.*$"
)
PINNED = re.compile(r"^(?P<host>[^/]+)/(?P<path>[a-z0-9._/-]+)@(?P<digest>sha256:[0-9a-f]{64})$")
BASH = shutil.which("bash")
GRAFANA_DIGEST = "sha256:ae509d9ae8c1021ca32ec370a9ca975d40165eda9fccd1e1102d317ff8787cd7"
SCRIPTS = [
    *sorted((ROOT / "offline").glob("*.sh")),
    ROOT / "deploy/gpu-cluster-rbac/discover-kinds.sh",
]

pytestmark = [pytest.mark.unit, pytest.mark.skipif(BASH is None, reason="needs bash")]


def lock_entries() -> list[re.Match[str]]:
    """Every image line of the lock file, parsed."""
    lines = [ln for ln in LOCK.read_text().splitlines() if ln.strip() and not ln.startswith("#")]
    matches = [LOCK_LINE.match(ln) for ln in lines]
    assert all(matches), [ln for ln, m in zip(lines, matches, strict=True) if not m]
    return [m for m in matches if m]


def locked() -> set[tuple[str, str]]:
    """(path, digest) of every locked image; the host is wherever it is mirrored."""
    return {(m["path"], m["digest"]) for m in lock_entries()}


def test_lock_lists_unique_index_digests_including_grafana() -> None:
    entries = lock_entries()
    refs = [(m["host"], m["path"], m["tag"]) for m in entries]
    assert len(refs) == len(set(refs))
    assert ("grafana/grafana", GRAFANA_DIGEST) in locked()
    assert all(m["tag"] != "latest" for m in entries)


def test_containerfile_base_images_are_pinned_and_locked() -> None:
    text = (ROOT / "Containerfile").read_text()
    args = dict(re.findall(r"^ARG (\w+_IMAGE)=(\S+)$", text, flags=re.MULTILINE))
    assert set(args) == {"PYTHON_IMAGE", "UV_IMAGE"}
    for ref in args.values():
        m = PINNED.match(ref)
        assert m, ref
        assert (m["path"], m["digest"]) in locked(), ref
    froms = re.findall(r"^FROM (\S+)", text, flags=re.MULTILINE)
    assert froms == ["${UV_IMAGE}", "${PYTHON_IMAGE}", "${PYTHON_IMAGE}"]
    user = re.findall(r"^USER (\S+)$", text, flags=re.MULTILINE)
    assert user == ["1001"]
    assert 'CMD ["ttv-engine"]' in text


def test_build_context_is_an_allow_list() -> None:
    lines = (ROOT / ".containerignore").read_text().splitlines()
    rules = [ln for ln in lines if ln and not ln.startswith("#")]
    assert rules[0] == "*"
    included = {ln[1:].rstrip("/") for ln in rules if ln.startswith("!")}
    assert included == {"pyproject.toml", "uv.lock", "src", "migrations", "config", "offline"}


def ci_images(doc: dict[str, Any]) -> list[str]:
    """Every job and service image of a GitLab CI document."""
    found: list[str] = []
    for job in doc.values():
        if not isinstance(job, dict):
            continue
        for item in [job.get("image"), *job.get("services", [])]:
            ref = item.get("name") if isinstance(item, dict) else item
            if ref:
                found.append(ref)
    return found


def test_gitlab_ci_images_come_from_the_internal_registry_by_locked_digest() -> None:
    doc = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text())
    images = ci_images(doc)
    assert len(images) >= 5
    for ref in images:
        assert ref.startswith("${INTERNAL_REGISTRY}/"), ref
        m = PINNED.match(ref.replace("${INTERNAL_REGISTRY}", "registry.internal.example"))
        assert m, ref
        assert (m["path"], m["digest"]) in locked(), ref
    assert {"gate", "charts", "build-image"} <= set(doc)
    for name, job in doc.items():
        # An unquoted ": " turns a script line into a mapping, which GitLab rejects.
        script = job.get("script", []) if isinstance(job, dict) else []
        assert all(isinstance(line, str) for line in script), name
    gate = "\n".join(doc["gate"]["script"])
    for check in ("ruff check", "ruff format --check", "ty check src tests", "pytest -q"):
        assert check in gate


def test_every_locked_image_is_used() -> None:
    used = "\n".join(
        p.read_text()
        for p in (ROOT / "Containerfile", ROOT / ".gitlab-ci.yml", ROOT / "offline/lib.sh",
                  ROOT / "offline/sbom.sh", ROOT / "grafana/README.md", ROOT / "README.md")
    )  # fmt: skip
    for m in lock_entries():
        assert m["digest"] in used or m["path"] in used, m.group(0)


def bash(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a repo script (or `bash -n`) without raising."""
    assert BASH
    cmd = [BASH, *args]
    return subprocess.run(cmd, capture_output=True, text=True, check=False, env=env)  # noqa: S603


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_and_are_executable(script: Path) -> None:
    assert os.access(script, os.X_OK), script
    result = bash("-n", str(script))
    assert result.returncode == 0, result.stderr


def stub(directory: Path, name: str, body: str) -> None:
    """Write an executable stub command into a directory."""
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n")
    path.chmod(0o755)


def dir_archive(path: Path, manifest: bytes) -> None:
    """A tar of skopeo's dir: layout holding only a top-level manifest, as archive_image writes."""
    with tarfile.open(path, "w") as tar:
        info = tarfile.TarInfo("./manifest.json")
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))


def make_bundle(tmp_path: Path) -> tuple[Path, str]:
    """A one-image bundle with SHA256SUMS, as make-bundle.sh writes it."""
    bundle = tmp_path / "bundle"
    (bundle / "images").mkdir(parents=True)
    ref = next(m.group(0).split()[0] for m in lock_entries() if m["path"] == "astral-sh/uv")
    dir_archive(bundle / "images/uv.tar", b'{"schemaVersion": 2}')
    (bundle / "images/index.txt").write_text(f"{ref} images/uv.tar\n")
    sums = [
        f"{hashlib.sha256((bundle / f).read_bytes()).hexdigest()}  {f}"
        for f in ("images/index.txt", "images/uv.tar")
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return bundle, ref


def run_import(tmp_path: Path, bundle: Path) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run import-images.sh with a stub skopeo that only records its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "skopeo.log"
    keep = f'[ "$1" != copy ] || cp "${{5#dir:}}/manifest.json" "{tmp_path}/seen"'
    stub(bin_dir, "skopeo", f'echo "$*" >> "{log}"; {keep}')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)}
    result = bash(
        str(ROOT / "offline/import-images.sh"), "--registry", "registry.internal.example",
        "--bundle", str(bundle), env=env,
    )  # fmt: skip
    return result, log.read_text().splitlines() if log.exists() else []


def test_import_verifies_checksums_then_pushes_by_path_tag_and_digest(tmp_path: Path) -> None:
    bundle, ref = make_bundle(tmp_path)
    result, calls = run_import(tmp_path, bundle)
    assert result.returncode == 0, result.stderr
    digest = ref.split("@")[1]
    copy, inspect = calls
    assert re.fullmatch(
        rf"copy --quiet --all --preserve-digests dir:{tmp_path}/gpu-ttv-import\.\w+/image "
        r"docker://registry\.internal\.example/astral-sh/uv:0\.13\.6",
        copy,
    ), copy
    assert inspect == f"inspect --raw docker://registry.internal.example/astral-sh/uv@{digest}"
    assert (tmp_path / "seen").read_bytes() == b'{"schemaVersion": 2}'
    assert not list(tmp_path.glob("gpu-ttv-import.*"))  # the unpacked copy is removed


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_import_refuses_a_tampered_bundle_before_pushing(tmp_path: Path) -> None:
    bundle, _ = make_bundle(tmp_path)
    (bundle / "images/uv.tar").write_bytes(b"tampered archive")
    result, calls = run_import(tmp_path, bundle)
    assert result.returncode != 0
    assert calls == []


def test_discover_kinds_lists_readable_crd_kinds_and_leaves_out_credentials(tmp_path: Path) -> None:
    listing = {
        "run.ai": "trainingworkloads.run.ai\nrunaijobs.run.ai\naccesssecrets.run.ai",
        "serving.kserve.io": "llminferenceservices.serving.kserve.io",
    }
    cases = "\n".join(f'  --api-group={g}) printf "%s\\n" "{v}" ;;' for g, v in listing.items())
    stub(tmp_path, "oc", f'case "$2" in\n{cases}\n  *) : ;;\nesac')
    script = ROOT / "deploy/gpu-cluster-rbac/discover-kinds.sh"
    result = bash(str(script), env={**os.environ, "OC": str(tmp_path / "oc")})
    assert result.returncode == 0, result.stderr
    kinds = yaml.safe_load(result.stdout)["discoveredKinds"]
    assert kinds == [
        {"apiGroup": "run.ai", "resources": ["runaijobs", "trainingworkloads"]},
        {"apiGroup": "serving.kserve.io", "resources": ["llminferenceservices"]},
    ]
    assert "left out (credentials): accesssecrets" in result.stdout


def test_app_chart_and_image_versions_agree() -> None:
    """A release tag vX.Y.Z must match all of them (.github/workflows/release.yml checks it)."""
    app = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    chart = yaml.safe_load((ROOT / "deploy/helm/gpu-ttv/Chart.yaml").read_text())
    image = re.search(r"^ARG VERSION=(\S+)$", (ROOT / "Containerfile").read_text(), re.MULTILINE)
    assert app and image
    assert app[1] == chart["version"] == chart["appVersion"] == image[1]


def test_github_actions_are_pinned_by_commit() -> None:
    """A tag can be moved; a commit cannot. Local reusable workflows are fine."""
    uses = [
        u
        for f in sorted((ROOT / ".github/workflows").glob("*.yml"))
        for u in re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", f.read_text(), re.MULTILINE)
    ]
    assert uses
    for ref in uses:
        assert ref.startswith("./") or re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_release_runs_the_gate_then_publishes_by_digest() -> None:
    flows = ROOT / ".github/workflows"
    ci = yaml.safe_load((flows / "ci.yml").read_text())
    release = yaml.safe_load((flows / "release.yml").read_text())
    assert "workflow_call" in ci[True]  # PyYAML reads the key `on` as True
    assert release[True] == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}
    assert release["permissions"] == {"contents": "read"}
    jobs = release["jobs"]
    assert jobs["gate"] == {"uses": "./.github/workflows/ci.yml"}
    publish = jobs["publish"]
    assert publish["needs"] == "gate"
    assert publish["permissions"] == {"contents": "write", "packages": "write"}
    script = "\n".join(s.get("run", "") for s in publish["steps"])
    assert "podman push --digestfile" in script
    assert 'offline/set-chart-image.sh deploy/helm/gpu-ttv/values.yaml ghcr.io' in script
    assert "helm package deploy/helm/gpu-ttv" in script
    assert "helm package deploy/gpu-cluster-rbac" in script
    assert 'gh release create "$GITHUB_REF_NAME"' in script
    assert "make-bundle.sh --out" not in script  # the user needs only the image (2026-11-26)


def test_set_chart_image_points_the_chart_at_a_digest_and_keeps_comments(tmp_path: Path) -> None:
    values = tmp_path / "values.yaml"
    shutil.copy(ROOT / "deploy/helm/gpu-ttv/values.yaml", values)
    digest = "sha256:" + "ab" * 32
    script = str(ROOT / "offline/set-chart-image.sh")
    result = bash(script, str(values), "ghcr.io", "roiblum1/gpu-ttv", digest)
    assert result.returncode == 0, result.stderr
    image = yaml.safe_load(values.read_text())["image"]
    assert (image["registry"], image["repository"], image["digest"]) == (
        "ghcr.io",
        "roiblum1/gpu-ttv",
        digest,
    )
    assert values.read_text().count("\n#") == (
        (ROOT / "deploy/helm/gpu-ttv/values.yaml").read_text().count("\n#")
    )
    assert bash(script, str(values), "ghcr.io", "a/b", "sha256:short").returncode == 2
    (tmp_path / "other.yaml").write_text("x: 1\n")
    assert bash(script, str(tmp_path / "other.yaml"), "ghcr.io", "a/b", digest).returncode == 1


def test_mirror_stops_when_an_archive_does_not_keep_the_pinned_digest(tmp_path: Path) -> None:
    """A stub skopeo writes a manifest whose hash cannot match the first locked digest."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub(bin_dir, "skopeo", 'd="${@: -1}"; printf "{}" > "${d#dir:}/manifest.json"')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = bash(str(ROOT / "offline/mirror-images.sh"), "--out", str(tmp_path / "out"), env=env)
    assert result.returncode == 1
    assert "not the pinned digest" in result.stderr
    assert (tmp_path / "out/images/index.txt").read_text() == ""
