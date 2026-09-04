"""Verify release archives and smoke-test installations in fresh environments."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Any

RIME_COMMIT = "bce5de31031e81e64ec9b839ec949724678c1a8d"
MAX_ARCHIVE_FILE_SIZE = 16 * 1024 * 1024

_DISTRIBUTION_FILES = (
    "METADATA",
    "WHEEL",
    "entry_points.txt",
    "top_level.txt",
    "licenses/LICENSE",
    "RECORD",
)
_EGG_INFO_FILES = (
    "PKG-INFO",
    "SOURCES.txt",
    "dependency_links.txt",
    "entry_points.txt",
    "requires.txt",
    "top_level.txt",
)
_GENERATED_CASE_SUFFIXES = (".in", ".diff")
_PRIVATE_KEY_RE = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_TOKEN_RE = re.compile(rb"\bypt_([A-Za-z0-9_-]{20,})\b")
_GITHUB_TOKEN_RE = re.compile(rb"\b(?:gh[opusr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")
_AWS_ACCESS_KEY_RE = re.compile(rb"\bAKIA[A-Z0-9]{16}\b")
_ENV_ASSIGNMENT_RE = re.compile(
    rb"(?im)^[ \t]*(?:export[ \t]+)?"
    rb"(YUKICODER_TOKEN(?:_[0-9]+)?|YUKICODER_API_KEY)"
    rb"[ \t]*=[ \t]*([^\r\n#]+)"
)
_REQUIREMENT_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)(.*)$")
_SETUP_CFG_LF = b"[egg_info]\ntag_build = \ntag_date = 0\n\n"


class VerificationError(RuntimeError):
    """Raised when a release invariant is not satisfied."""


@dataclass(frozen=True)
class ProjectSpec:
    root: Path
    project: Mapping[str, Any]
    name: str
    version: str
    archive_stem: str
    wheel_name: str
    sdist_name: str
    source_files: Mapping[str, Path]
    package_files: frozenset[str]
    test_files: frozenset[str]


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise VerificationError(f"{label} must be a table")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{label} must be a non-empty string")
    return value


def _is_exact_generated_setup_cfg(data: bytes) -> bool:
    """Accept setuptools' exact platform-native LF or CRLF rendering."""

    return data in (_SETUP_CFG_LF, _SETUP_CFG_LF.replace(b"\n", b"\r\n"))


def _canonical_utf8_text(data: bytes, label: str) -> bytes:
    """Normalize one consistent LF or CRLF text while rejecting other changes."""

    without_crlf = data.replace(b"\r\n", b"")
    if b"\r" in without_crlf or (b"\r\n" in data and b"\n" in without_crlf):
        raise VerificationError(f"{label} has bare or mixed newlines")
    normalized = data.replace(b"\r\n", b"\n")
    try:
        normalized.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerificationError(f"{label} is not UTF-8") from exc
    return normalized


def _regular_python_tree(root: Path, relative_root: str) -> dict[str, Path]:
    source_root = root / relative_root
    if source_root.is_symlink() or not source_root.is_dir():
        raise VerificationError(f"source archive tree is not a directory: {source_root}")
    result: dict[str, Path] = {}
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink():
            raise VerificationError(f"source archive input must not be a symlink: {path}")
        if path.is_file() and path.suffix == ".py":
            result[path.relative_to(root).as_posix()] = path
    if not result:
        raise VerificationError(f"source archive tree has no Python files: {source_root}")
    return result


def _load_project(root: Path) -> ProjectSpec:
    root = root.resolve(strict=True)
    try:
        configuration = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VerificationError(f"could not read pyproject.toml: {exc}") from exc
    project = _object(configuration.get("project"), "project")
    name = _string(project.get("name"), "project.name")
    version = _string(project.get("version"), "project.version")
    if name != "yukitools-rime":
        raise VerificationError(f"unexpected project name: {name!r}")
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", version) is None:
        raise VerificationError(f"release version is not a three-part version: {version!r}")

    source_files: dict[str, Path] = {}
    for relative in ("LICENSE", "MANIFEST.in", "README.md", "pyproject.toml"):
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise VerificationError(f"required source file is not regular: {path}")
        source_files[relative] = path

    package_root = root / "src" / "yukitools_rime"
    for path in sorted(package_root.rglob("*")):
        if path.is_symlink():
            raise VerificationError(f"package source must not be a symlink: {path}")
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"):
            source_files[path.relative_to(root).as_posix()] = path
    package_files = frozenset(
        relative for relative in source_files if relative.startswith("src/yukitools_rime/")
    )
    if "src/yukitools_rime/py.typed" not in package_files:
        raise VerificationError("source tree is missing src/yukitools_rime/py.typed")

    test_sources = _regular_python_tree(root, "tests")
    source_files.update(test_sources)
    source_files.update(_regular_python_tree(root, "tools"))
    test_files = frozenset(test_sources)

    archive_stem = re.sub(r"[-_.]+", "_", name)
    return ProjectSpec(
        root=root,
        project=project,
        name=name,
        version=version,
        archive_stem=archive_stem,
        wheel_name=f"{archive_stem}-{version}-py3-none-any.whl",
        sdist_name=f"{archive_stem}-{version}.tar.gz",
        source_files=source_files,
        package_files=package_files,
        test_files=frozenset(test_files),
    )


def _validate_archive_name(name: str, *, directory: bool = False) -> str:
    normalized = name[:-1] if directory and name.endswith("/") else name
    if not normalized or "\\" in normalized or normalized.startswith("/"):
        raise VerificationError(f"unsafe archive member name: {name!r}")
    path = PurePosixPath(normalized)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise VerificationError(f"unsafe archive member name: {name!r}")
    return normalized


def _looks_like_placeholder(raw_value: bytes) -> bool:
    value = raw_value.strip().strip(b"'\"").lower()
    if not value:
        return True
    if value.startswith((b"<", b"$", b"%")):
        return True
    if any(word in value for word in (b"example", b"placeholder", b"changeme")):
        return True
    if value.startswith(b"ypt_"):
        value = value[4:]
    return bool(value) and set(value) <= set(b"x._-")


def _check_sensitive_content(label: str, data: bytes) -> None:
    if _PRIVATE_KEY_RE.search(data):
        raise VerificationError(f"private key material found in {label}")
    if _GITHUB_TOKEN_RE.search(data):
        raise VerificationError(f"GitHub token-like value found in {label}")
    if _AWS_ACCESS_KEY_RE.search(data):
        raise VerificationError(f"AWS access-key-like value found in {label}")
    for match in _TOKEN_RE.finditer(data):
        if not _looks_like_placeholder(b"ypt_" + match.group(1)):
            raise VerificationError(f"yukicoder token-like value found in {label}")
    for match in _ENV_ASSIGNMENT_RE.finditer(data):
        value = match.group(2)
        compact = value.strip().strip(b"'\"")
        if len(compact) >= 16 and not _looks_like_placeholder(value):
            key = match.group(1).decode("ascii", errors="replace")
            raise VerificationError(f"non-placeholder {key} assignment found in {label}")


def _check_generated_case_name(name: str) -> None:
    path = PurePosixPath(name)
    parts = tuple(part.casefold() for part in path.parts)
    if ".env" in parts or "rime-out" in parts:
        raise VerificationError(f"credential/generated path found in artifact: {name}")
    manifest = path.name.casefold() == "manifest.in" and len(path.parts) <= 2
    if not manifest and name.casefold().endswith(_GENERATED_CASE_SUFFIXES):
        raise VerificationError(f"generated testcase found in artifact: {name}")


def _expected_entry_points(spec: ProjectSpec) -> bytes:
    scripts = _object(spec.project.get("scripts"), "project.scripts")
    lines = ["[console_scripts]"]
    for name in sorted(scripts):
        value = _string(scripts[name], f"project.scripts.{name}")
        lines.append(f"{name} = {value}")
    return ("\n".join(lines) + "\n").encode()


def _canonical_requirement(requirement: str) -> str:
    main, separator, marker = requirement.partition(";")
    compact = re.sub(r"\s+", "", main)
    match = _REQUIREMENT_RE.fullmatch(compact)
    if match is None:
        raise VerificationError(f"unsupported requirement spelling: {requirement!r}")
    name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
    suffix = match.group(2)
    if suffix:
        suffix = ",".join(sorted(suffix.split(",")))
    if not separator:
        return name + suffix
    normalized_marker = re.sub(r"\s+", "", marker).replace("'", '"')
    return f"{name}{suffix};{normalized_marker}"


def _requirements(spec: ProjectSpec) -> tuple[list[str], dict[str, list[str]]]:
    raw_dependencies = spec.project.get("dependencies", [])
    if not isinstance(raw_dependencies, list) or not all(
        isinstance(value, str) for value in raw_dependencies
    ):
        raise VerificationError("project.dependencies must be a list of strings")
    dependencies = list(raw_dependencies)
    optional_table = _object(
        spec.project.get("optional-dependencies", {}),
        "project.optional-dependencies",
    )
    optional: dict[str, list[str]] = {}
    for extra, raw_values in optional_table.items():
        if not isinstance(extra, str):
            raise VerificationError("optional dependency names must be strings")
        if not isinstance(raw_values, list) or not all(
            isinstance(value, str) for value in raw_values
        ):
            raise VerificationError(f"optional dependency {extra!r} must be a string list")
        optional[extra] = list(raw_values)
    return dependencies, optional


def _expected_metadata_requirements(spec: ProjectSpec) -> Counter[str]:
    dependencies, optional = _requirements(spec)
    expected = [_canonical_requirement(value) for value in dependencies]
    for extra, values in optional.items():
        expected.extend(_canonical_requirement(f'{value}; extra == "{extra}"') for value in values)
    return Counter(expected)


def _render_requires_txt(spec: ProjectSpec) -> bytes:
    dependencies, optional = _requirements(spec)
    lines = [_canonical_requirement(value) for value in dependencies]
    for extra, values in optional.items():
        if lines:
            lines.append("")
        lines.append(f"[{extra}]")
        lines.extend(_canonical_requirement(value) for value in values)
    return ("\n".join(lines) + "\n").encode()


def _single_header(message: Message, name: str) -> str:
    values = message.get_all(name, [])
    if len(values) != 1:
        raise VerificationError(f"metadata requires exactly one {name} header")
    return str(values[0])


def _validate_metadata(data: bytes, spec: ProjectSpec) -> None:
    message = BytesParser(policy=policy.default).parsebytes(data)
    expected_headers = {
        "Metadata-Version": "2.4",
        "Name": spec.name,
        "Version": spec.version,
        "Summary": _string(spec.project.get("description"), "project.description"),
        "Requires-Python": _string(
            spec.project.get("requires-python"),
            "project.requires-python",
        ),
        "Description-Content-Type": "text/markdown",
    }
    license_expression = _string(spec.project.get("license"), "project.license")
    expected_headers["License-Expression"] = license_expression
    authors = spec.project.get("authors", [])
    if not isinstance(authors, list) or not authors:
        raise VerificationError("project.authors must be a non-empty list")
    rendered_authors: list[str] = []
    for index, raw_author in enumerate(authors):
        author = _object(raw_author, f"project.authors[{index}]")
        rendered_authors.append(
            f"{_string(author.get('name'), 'author.name')} "
            f"<{_string(author.get('email'), 'author.email')}>"
        )
    expected_headers["Author-email"] = ", ".join(rendered_authors)

    for name, expected in expected_headers.items():
        actual = _single_header(message, name)
        if actual != expected:
            raise VerificationError(
                f"metadata {name} mismatch: expected {expected!r}, got {actual!r}"
            )
    if message.get_all("License-File", []) != ["LICENSE"]:
        raise VerificationError("metadata must contain exactly License-File: LICENSE")

    actual_requirements = Counter(
        _canonical_requirement(str(value)) for value in message.get_all("Requires-Dist", [])
    )
    expected_requirements = _expected_metadata_requirements(spec)
    if actual_requirements != expected_requirements:
        raise VerificationError(
            "metadata Requires-Dist mismatch: "
            f"expected {sorted(expected_requirements.elements())}, "
            f"got {sorted(actual_requirements.elements())}"
        )
    _, optional = _requirements(spec)
    actual_extras = Counter(str(value) for value in message.get_all("Provides-Extra", []))
    if actual_extras != Counter(optional.keys()):
        raise VerificationError(
            f"metadata extras mismatch: expected {sorted(optional)}, "
            f"got {sorted(actual_extras.elements())}"
        )
    readme = (spec.root / "README.md").read_bytes()
    payload = message.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raise VerificationError("metadata long description is not bytes")
    if _canonical_utf8_text(payload, "metadata long description") != _canonical_utf8_text(
        readme, "README.md"
    ):
        raise VerificationError("metadata long description does not exactly match README.md")


def _validate_wheel_record(
    files: Mapping[str, bytes],
    record_name: str,
) -> None:
    try:
        rows = list(csv.reader(io.StringIO(files[record_name].decode("utf-8"), newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise VerificationError(f"invalid wheel RECORD: {exc}") from exc
    if any(len(row) != 3 for row in rows):
        raise VerificationError("every wheel RECORD row must have exactly three fields")
    if len({row[0] for row in rows}) != len(rows):
        raise VerificationError("wheel RECORD contains duplicate paths")
    if {row[0] for row in rows} != set(files):
        raise VerificationError("wheel RECORD paths do not exactly match wheel contents")
    for path, digest, size in rows:
        if path == record_name:
            if digest or size:
                raise VerificationError("wheel RECORD must not hash itself")
            continue
        expected_digest = (
            base64.urlsafe_b64encode(hashlib.sha256(files[path]).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        if digest != f"sha256={expected_digest}" or size != str(len(files[path])):
            raise VerificationError(f"wheel RECORD hash or size mismatch for {path}")


def _wheel_expected_files(spec: ProjectSpec) -> frozenset[str]:
    distribution = f"{spec.archive_stem}-{spec.version}.dist-info"
    package = {relative.removeprefix("src/") for relative in spec.package_files}
    return frozenset(package | {f"{distribution}/{name}" for name in _DISTRIBUTION_FILES})


def _verify_wheel(path: Path, spec: ProjectSpec) -> bytes:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise VerificationError(f"wheel CRC validation failed: {path.name}")
            information = archive.infolist()
            names = [item.filename for item in information]
            if len(names) != len(set(names)):
                raise VerificationError("wheel contains duplicate member names")
            for item in information:
                name = _validate_archive_name(item.filename, directory=item.is_dir())
                if item.is_dir():
                    raise VerificationError(f"wheel contains an unexpected directory: {name}")
                if item.flag_bits & 1:
                    raise VerificationError(f"wheel contains an encrypted member: {name}")
                mode = (item.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise VerificationError(f"wheel contains a symlink: {name}")
                if item.file_size > MAX_ARCHIVE_FILE_SIZE:
                    raise VerificationError(f"wheel member is unexpectedly large: {name}")
                _check_generated_case_name(name)
            expected = _wheel_expected_files(spec)
            if set(names) != expected:
                raise VerificationError(
                    "wheel contents mismatch: "
                    f"missing={sorted(expected - set(names))}, "
                    f"extra={sorted(set(names) - expected)}"
                )
            files = {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile) as exc:
        raise VerificationError(f"could not inspect wheel {path}: {exc}") from exc

    for name, data in files.items():
        _check_sensitive_content(f"{path.name}:{name}", data)
    for source_name in spec.package_files:
        wheel_name = source_name.removeprefix("src/")
        if files[wheel_name] != spec.source_files[source_name].read_bytes():
            raise VerificationError(f"wheel source differs from checkout: {wheel_name}")

    distribution = f"{spec.archive_stem}-{spec.version}.dist-info"
    metadata_name = f"{distribution}/METADATA"
    entry_points_name = f"{distribution}/entry_points.txt"
    top_level_name = f"{distribution}/top_level.txt"
    license_name = f"{distribution}/licenses/LICENSE"
    wheel_metadata_name = f"{distribution}/WHEEL"
    record_name = f"{distribution}/RECORD"
    if files[entry_points_name] != _expected_entry_points(spec):
        raise VerificationError("wheel console entry points do not match pyproject.toml")
    if files[top_level_name] != b"yukitools_rime\n":
        raise VerificationError("wheel top_level.txt is not exact")
    if files[license_name] != (spec.root / "LICENSE").read_bytes():
        raise VerificationError("wheel LICENSE does not match the source LICENSE")

    wheel_message = BytesParser(policy=policy.default).parsebytes(files[wheel_metadata_name])
    expected_wheel_headers = {
        "Wheel-Version": "1.0",
        "Root-Is-Purelib": "true",
        "Tag": "py3-none-any",
    }
    for name, expected_value in expected_wheel_headers.items():
        actual_value = _single_header(wheel_message, name)
        if actual_value != expected_value:
            raise VerificationError(
                f"WHEEL {name} mismatch: expected {expected_value!r}, got {actual_value!r}"
            )
    if not _single_header(wheel_message, "Generator").startswith("setuptools"):
        raise VerificationError("wheel must be generated by setuptools")
    _validate_wheel_record(files, record_name)
    return files[metadata_name]


def _sdist_expected(spec: ProjectSpec) -> tuple[frozenset[str], frozenset[str]]:
    egg_info = f"src/{spec.archive_stem}.egg-info"
    relative_files = (
        set(spec.source_files)
        | {f"{egg_info}/{name}" for name in _EGG_INFO_FILES}
        | {"PKG-INFO", "setup.cfg"}
    )
    root_name = f"{spec.archive_stem}-{spec.version}"
    files = frozenset(f"{root_name}/{name}" for name in relative_files)
    directories = {root_name}
    for member in files:
        for parent in PurePosixPath(member).parents:
            if str(parent) != ".":
                directories.add(parent.as_posix())
    return files, frozenset(directories)


def _verify_sdist(path: Path, spec: ProjectSpec) -> bytes:
    expected_files, expected_directories = _sdist_expected(spec)
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            names = [
                _validate_archive_name(member.name, directory=member.isdir()) for member in members
            ]
            if len(names) != len(set(names)):
                raise VerificationError("sdist contains duplicate member names")
            actual_files: set[str] = set()
            actual_directories: set[str] = set()
            files: dict[str, bytes] = {}
            for member, name in zip(members, names, strict=True):
                _check_generated_case_name(name)
                if member.isdir():
                    actual_directories.add(name)
                    continue
                if not member.isfile():
                    raise VerificationError(f"sdist contains a link or special file: {name}")
                if member.size > MAX_ARCHIVE_FILE_SIZE:
                    raise VerificationError(f"sdist member is unexpectedly large: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    raise VerificationError(f"could not read sdist member: {name}")
                files[name] = stream.read()
                actual_files.add(name)
    except (OSError, tarfile.TarError) as exc:
        raise VerificationError(f"could not inspect sdist {path}: {exc}") from exc

    if actual_files != set(expected_files):
        raise VerificationError(
            "sdist file contents mismatch: "
            f"missing={sorted(set(expected_files) - actual_files)}, "
            f"extra={sorted(actual_files - set(expected_files))}"
        )
    if actual_directories != set(expected_directories):
        raise VerificationError(
            "sdist directory contents mismatch: "
            f"missing={sorted(set(expected_directories) - actual_directories)}, "
            f"extra={sorted(actual_directories - set(expected_directories))}"
        )
    for name, data in files.items():
        _check_sensitive_content(f"{path.name}:{name}", data)

    root_name = f"{spec.archive_stem}-{spec.version}"
    for source_name, source_path in spec.source_files.items():
        archive_name = f"{root_name}/{source_name}"
        if files[archive_name] != source_path.read_bytes():
            raise VerificationError(f"sdist source differs from checkout: {source_name}")

    egg_info = f"{root_name}/src/{spec.archive_stem}.egg-info"
    metadata = files[f"{root_name}/PKG-INFO"]
    if files[f"{egg_info}/PKG-INFO"] != metadata:
        raise VerificationError("sdist PKG-INFO copies differ")
    if files[f"{egg_info}/entry_points.txt"] != _expected_entry_points(spec):
        raise VerificationError("sdist console entry points do not match pyproject.toml")
    if files[f"{egg_info}/top_level.txt"] != b"yukitools_rime\n":
        raise VerificationError("sdist top_level.txt is not exact")
    if files[f"{egg_info}/dependency_links.txt"] != b"\n":
        raise VerificationError("sdist dependency_links.txt is not exact")
    if files[f"{egg_info}/requires.txt"] != _render_requires_txt(spec):
        raise VerificationError("sdist requires.txt does not match pyproject.toml")
    if not _is_exact_generated_setup_cfg(files[f"{root_name}/setup.cfg"]):
        raise VerificationError("sdist generated setup.cfg is not exact")

    manifest_name = f"{egg_info}/SOURCES.txt"
    try:
        manifest = {line for line in files[manifest_name].decode("utf-8").splitlines() if line}
    except UnicodeDecodeError as exc:
        raise VerificationError("sdist SOURCES.txt is not UTF-8") from exc
    manifest_expected = {
        name.removeprefix(f"{root_name}/")
        for name in expected_files
        if name not in {f"{root_name}/PKG-INFO", f"{root_name}/setup.cfg"}
    }
    if manifest != manifest_expected:
        raise VerificationError(
            "sdist SOURCES.txt mismatch: "
            f"missing={sorted(manifest_expected - manifest)}, "
            f"extra={sorted(manifest - manifest_expected)}"
        )
    return metadata


def _resolve_artifacts(dist_dir: Path, spec: ProjectSpec) -> tuple[Path, Path]:
    try:
        entries = tuple(dist_dir.resolve(strict=True).iterdir())
    except OSError as exc:
        raise VerificationError(f"could not inspect artifact directory: {exc}") from exc
    expected = {spec.wheel_name, spec.sdist_name}
    actual = {entry.name for entry in entries}
    if actual != expected:
        raise VerificationError(
            f"artifact set mismatch: expected {sorted(expected)}, got {sorted(actual)}"
        )
    for entry in entries:
        if entry.is_symlink() or not entry.is_file():
            raise VerificationError(f"artifact must be a regular file: {entry}")
    return dist_dir.resolve() / spec.wheel_name, dist_dir.resolve() / spec.sdist_name


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    for name in tuple(environment):
        if name.startswith("YUKICODER_") or name in {"GH_TOKEN", "GITHUB_TOKEN"}:
            environment.pop(name)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    environment["PIP_NO_INPUT"] = "1"
    return environment


def _run(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=_clean_environment(),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VerificationError(f"could not run {command!r}: {exc}") from exc
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise VerificationError(f"command failed ({result.returncode}): {command!r}\n{output}")
    return result


def _venv_python(environment: Path) -> Path:
    if os.name == "nt":
        return environment / "Scripts" / "python.exe"
    return environment / "bin" / "python"


_PUBLIC_IMPORT_SMOKE = r"""
import importlib
import importlib.metadata
import pkgutil
import sys
from pathlib import Path

import yukitools_rime

expected_version = sys.argv[1]
if yukitools_rime.__version__ != expected_version:
    raise SystemExit("installed __version__ mismatch")
package_path = Path(yukitools_rime.__file__).resolve()
if "site-packages" not in package_path.as_posix().casefold():
    raise SystemExit(f"package was not imported from site-packages: {package_path}")
for module in pkgutil.walk_packages(
    yukitools_rime.__path__,
    prefix="yukitools_rime.",
):
    if module.name != "yukitools_rime.__main__":
        importlib.import_module(module.name)
distribution = importlib.metadata.distribution("yukitools-rime")
if distribution.version != expected_version:
    raise SystemExit("installed distribution version mismatch")
entry_points = [
    entry
    for entry in distribution.entry_points
    if entry.group == "console_scripts" and entry.name == "yukitools-rime"
]
if len(entry_points) != 1 or entry_points[0].value != "yukitools_rime.cli:main":
    raise SystemExit("installed console entry point mismatch")
marker = package_path.with_name("py.typed")
if not marker.is_file():
    raise SystemExit("installed py.typed marker is missing")
license_files = [
    file
    for file in distribution.files or ()
    if str(file).replace("\\", "/").endswith(".dist-info/licenses/LICENSE")
]
if len(license_files) != 1:
    raise SystemExit("installed distribution must contain exactly one LICENSE")
license_text = Path(distribution.locate_file(license_files[0])).read_text(encoding="utf-8")
if "Apache License" not in license_text or "Version 2.0, January 2004" not in license_text:
    raise SystemExit("installed LICENSE is not Apache License 2.0")
"""


_WINDOWS_PATH_SMOKE = r"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from yukitools_rime import rime_plugin
from yukitools_rime.errors import ValidationError
from yukitools_rime.models import ProjectConfig, validate_basename
from yukitools_rime.rime_config import render_project_block

invalid_names = {
    "reserved device": "CON",
    "reserved device with suffix": "nul.txt",
    "backslash path": r"nested\child",
    "drive-qualified backslash path": r"C:\work\problem",
    "drive-qualified slash path": "D:/work/problem",
}
for label, value in invalid_names.items():
    try:
        validate_basename(value, label=label)
    except ValidationError:
        pass
    else:
        raise SystemExit(f"{label} was accepted: {value!r}")
if os.name == "nt" and rime_plugin._is_within(r"C:\root", r"D:\other"):
    raise SystemExit("cross-drive source path was accepted")


with tempfile.TemporaryDirectory(prefix="yukitools-rime-windows-") as temporary:
    project = Path(temporary) / "project with spaces 雪"
    project.mkdir()
    project_source = (
        "\ufeffuse_plugin('rime_plus')\r\n\r\n"
        + render_project_block(ProjectConfig()).replace("\n", "\r\n")
    ).encode("utf-8")
    (project / "PROJECT").write_bytes(project_source)
    (project / ".gitignore").write_bytes(b"# preserve me\r\n")

    command = [sys.executable, "-m", "yukitools_rime", "init", str(project)]
    child_environment = os.environ.copy()
    child_environment["PYTHONIOENCODING"] = "cp1252"
    for _ in range(2):
        result = subprocess.run(
            command,
            env=child_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise SystemExit(result.stdout + result.stderr)

    if (project / "PROJECT").read_bytes() != project_source:
        raise SystemExit("init did not preserve an existing BOM/CRLF PROJECT")
    ignore = (project / ".gitignore").read_bytes()
    non_newlines = ignore.replace(b"\r\n", b"")
    if (
        not ignore.startswith(b"# preserve me\r\n")
        or b"\r" in non_newlines
        or b"\n" in non_newlines
    ):
        raise SystemExit("init did not preserve .gitignore CRLF consistently")
    for required in ("PROJECT", ".gitignore", ".env.example"):
        if not (project / required).is_file():
            raise SystemExit(f"init did not create {required}")
    first_snapshot = {
        name: (project / name).read_bytes()
        for name in ("PROJECT", ".gitignore", ".env.example")
    }
    result = subprocess.run(
        command,
        env=child_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise SystemExit(result.stdout + result.stderr)
    second_snapshot = {
        name: (project / name).read_bytes()
        for name in ("PROJECT", ".gitignore", ".env.example")
    }
    if second_snapshot != first_snapshot:
        raise SystemExit("init is not byte-for-byte idempotent")
"""


_RIME_PROJECT_SMOKE = r"""
import sys
from pathlib import Path

from yukitools_rime.models import (
    GeneratorConfig,
    JudgeConfig,
    ProblemConfig,
    ProblemSettings,
    ProjectConfig,
    SolutionConfig,
)
from yukitools_rime.rime_config import (
    TestsetConfig,
    render_problem_block,
    render_project_block,
    render_solution_block,
    render_testset_block,
)

project = Path(sys.argv[1])
problem = project / "a"
testset = problem / "tests"
solution = problem / "solution"
ordinary = problem / "ordinary"
unknown = problem / "unknown"
testset.mkdir(parents=True)
solution.mkdir()
ordinary.mkdir()
unknown.mkdir()
(project / "PROJECT").write_text(
    'use_plugin("rime_plus")\n\n'
    + render_project_block(ProjectConfig(rime_out_dir="generated")),
    encoding="utf-8",
)
(problem / "PROBLEM").write_text(
    render_problem_block(
        ProblemConfig(
            problem_id=12345,
            settings=ProblemSettings(
                title="Installed wheel smoke",
                tags="test",
                level=1.5,
                time_limit_ms=2000,
                memory_limit=512,
                eps_mode="-",
                eps="0",
                wip=True,
                recruiting_tester=False,
                problem_type=0,
                judge_type=1,
            ),
            rime_id="A",
            reference_solution="solution",
        )
    ),
    encoding="utf-8",
)
(testset / "TESTSET").write_text(
    render_testset_block(
        TestsetConfig(
            generator=GeneratorConfig(
                lang_id="python3",
                src="generator.py",
                test_case_num=1,
                prefix="sample",
                rime_kind="script",
            ),
            judge=JudgeConfig(
                lang_id="python3",
                src="judge.py",
                rime_kind="script",
            ),
        )
    ),
    encoding="utf-8",
)
(solution / "SOLUTION").write_text(
    render_solution_block(
        SolutionConfig(lang_id="python3", src="main.py", rime_kind="script")
    ),
    encoding="utf-8",
)
(ordinary / "SOLUTION").write_text('cxx_solution("main.cpp")\n', encoding="utf-8")
(unknown / "SOLUTION").write_text(
    render_solution_block(
        SolutionConfig(lang_id="remote-unknown", src="main.txt", rime_kind=None)
    ),
    encoding="utf-8",
)
for path in (
    testset / "generator.py",
    testset / "judge.py",
    solution / "main.py",
    unknown / "main.txt",
):
    path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
(ordinary / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
"""


def _installed_package_smoke(
    python: Path,
    *,
    version: str,
    workspace: Path,
) -> None:
    _run([str(python), "-m", "pip", "check"], cwd=workspace)
    _run([str(python), "-c", _PUBLIC_IMPORT_SMOKE, version], cwd=workspace)
    version_result = _run(
        [str(python), "-m", "yukitools_rime", "--version"],
        cwd=workspace,
    )
    if version_result.stdout.strip() != f"yukitools-rime {version}":
        raise VerificationError("installed CLI --version output is incorrect")
    _run([str(python), "-m", "yukitools_rime", "--help"], cwd=workspace)
    console_name = "yukitools-rime.exe" if os.name == "nt" else "yukitools-rime"
    console = python.with_name(console_name)
    if not console.is_file():
        raise VerificationError(f"installed console executable is missing: {console}")
    console_version = _run([str(console), "--version"], cwd=workspace)
    if console_version.stdout.strip() != f"yukitools-rime {version}":
        raise VerificationError("installed console executable --version is incorrect")
    _run([str(console), "--help"], cwd=workspace)


def _rime_smoke(
    python: Path,
    *,
    rime_dir: Path,
    workspace: Path,
) -> None:
    commit = _run(
        ["git", "-C", str(rime_dir), "rev-parse", "HEAD"],
        cwd=workspace,
    ).stdout.strip()
    if commit != RIME_COMMIT:
        raise VerificationError(
            f"reference Rime commit mismatch: expected {RIME_COMMIT}, got {commit}"
        )
    if not (rime_dir / "rime.py").is_file():
        raise VerificationError(f"reference Rime checkout has no rime.py: {rime_dir}")
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            str(rime_dir),
        ],
        cwd=workspace,
    )
    project = workspace / "installed wheel rime smoke"
    _run(
        [str(python), "-m", "yukitools_rime", "init", str(project)],
        cwd=workspace,
    )
    _run(
        [str(python), "-c", _RIME_PROJECT_SMOKE, str(project)],
        cwd=workspace,
    )
    rime_name = "rime.exe" if os.name == "nt" else "rime"
    rime = python.with_name(rime_name)
    if not rime.is_file():
        raise VerificationError(f"installed Rime console executable is missing: {rime}")
    _run([str(rime), "help"], cwd=project, timeout=60)
    _run([str(rime), "build"], cwd=project, timeout=60)
    if not (project / "a" / "generated").is_dir():
        raise VerificationError("reference Rime did not create the configured output")
    if (project / "a" / "rime-out").exists():
        raise VerificationError("reference Rime ignored the configured output directory")


def _install_smoke(
    artifact: Path,
    *,
    launcher: str,
    spec: ProjectSpec,
    rime_dir: Path | None,
    windows_path_smoke: bool,
) -> None:
    with tempfile.TemporaryDirectory(prefix="yukitools-rime-dist-") as temporary:
        workspace = Path(temporary).resolve()
        environment = workspace / "venv"
        _run([launcher, "-m", "venv", str(environment)], cwd=workspace)
        python = _venv_python(environment)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                str(artifact),
            ],
            cwd=workspace,
        )
        _installed_package_smoke(
            python,
            version=spec.version,
            workspace=workspace,
        )
        if windows_path_smoke:
            _run([str(python), "-c", _WINDOWS_PATH_SMOKE], cwd=workspace)
        if rime_dir is not None:
            _rime_smoke(
                python,
                rime_dir=rime_dir,
                workspace=workspace,
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify exact release archives and fresh installations."
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="directory containing exactly one wheel and one sdist",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path("."),
        help="source checkout used to validate artifact contents",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python launcher used to create fresh virtual environments",
    )
    parser.add_argument(
        "--rime-dir",
        type=Path,
        help=f"reference Rime checkout at commit {RIME_COMMIT}",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="validate archives without creating installation environments",
    )
    parser.add_argument(
        "--skip-sdist-install",
        action="store_true",
        help="still validate the sdist but only fresh-install the wheel",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        spec = _load_project(args.project_root)
        wheel, sdist = _resolve_artifacts(args.dist_dir, spec)
        wheel_metadata = _verify_wheel(wheel, spec)
        sdist_metadata = _verify_sdist(sdist, spec)
        if wheel_metadata != sdist_metadata:
            raise VerificationError("wheel METADATA and sdist PKG-INFO differ")
        _validate_metadata(wheel_metadata, spec)
        print(f"verified exact contents and metadata: {wheel.name}, {sdist.name}")

        if not args.static_only:
            rime_dir = args.rime_dir.resolve(strict=True) if args.rime_dir is not None else None
            _install_smoke(
                wheel,
                launcher=args.python,
                spec=spec,
                rime_dir=rime_dir,
                windows_path_smoke=os.name == "nt",
            )
            if rime_dir is None:
                print("verified fresh wheel install")
            else:
                print("verified fresh wheel install and Rime init/load/build")
            if not args.skip_sdist_install:
                _install_smoke(
                    sdist,
                    launcher=args.python,
                    spec=spec,
                    rime_dir=None,
                    windows_path_smoke=False,
                )
                print("verified fresh sdist install")
    except (OSError, VerificationError) as exc:
        print(f"distribution verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
