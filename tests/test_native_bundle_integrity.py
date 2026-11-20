"""Native capability requires the pinned whole payload, not a self-authorized launcher."""

import json

import pytest

from chrome_engine import engine
from chrome_engine.native_bundle import bundle_sha256
from chrome_engine.persona import Persona


@pytest.fixture
def accepted_bundle(tmp_path, monkeypatch):
    app = tmp_path / "Chromium.app"
    executable = app / "Contents/MacOS/Chromium"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"launcher")
    framework = (
        app / "Contents/Frameworks/Chromium Framework.framework/Versions/A/Chromium Framework"
    )
    framework.parent.mkdir(parents=True)
    framework.write_bytes(b"reviewed framework")
    framework.parent.with_name("Current").symlink_to("A", target_is_directory=True)
    helper = framework.parent / "Helpers/Chromium Helper.app/Contents/MacOS/Chromium Helper"
    helper.parent.mkdir(parents=True)
    helper.write_bytes(b"reviewed helper")
    monkeypatch.setattr(engine, "NATIVE_REALM_BUNDLES", {bundle_sha256(app)})
    import hashlib

    manifest = {
        "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "bundle_sha256": bundle_sha256(app),
        "acceptance": "NATIVE_MATRIX_PASSED_NO_RELEASE_APPROVAL",
        "cases": {"macos:2": {}, "macos:3": {}},
    }
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest))
    return app, executable, framework, helper, manifest


def accepted(executable):
    return engine._has_verified_native_realms(
        executable, Persona(seed=3, platform="macos", platform_version="15.6.1")
    )


def test_complete_pinned_bundle_is_accepted_and_portable(accepted_bundle, tmp_path):
    import shutil

    app, executable, _, _, _ = accepted_bundle
    assert accepted(executable)
    clone = tmp_path / "clone"
    clone.mkdir()
    shutil.copytree(app, clone / app.name, symlinks=True)
    shutil.copy2(tmp_path / "native-acceptance.json", clone)
    assert accepted(clone / executable.relative_to(tmp_path))


@pytest.mark.parametrize("change", ["missing", "framework", "helper", "extra", "mode", "link"])
def test_changed_payload_is_not_accepted(accepted_bundle, tmp_path, change):
    app, executable, framework, helper, _ = accepted_bundle
    if change == "missing":
        framework.unlink()
    elif change == "framework":
        framework.write_bytes(b"unpatched framework")
    elif change == "helper":
        helper.write_bytes(b"unpatched helper")
    elif change == "extra":
        (app / "injected.dylib").write_bytes(b"new payload")
    elif change == "mode":
        framework.chmod(framework.stat().st_mode ^ 0o100)
    else:
        link = framework.parent.with_name("Current")
        link.unlink()
        link.symlink_to(tmp_path, target_is_directory=True)
    assert not accepted(executable)


def test_sibling_manifest_cannot_authorize_unknown_payload(accepted_bundle, tmp_path):
    app, executable, framework, _, manifest = accepted_bundle
    framework.write_bytes(b"self-authorized replacement")
    manifest["bundle_sha256"] = bundle_sha256(app)
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest))
    assert not accepted(executable)


def test_pinned_legacy_manifest_requires_no_installed_migration(accepted_bundle, tmp_path):
    _, executable, _, _, manifest = accepted_bundle
    del manifest["bundle_sha256"]
    (tmp_path / "native-acceptance.json").write_text(json.dumps(manifest))
    assert accepted(executable)


def test_missing_platform_case_and_corrupt_manifest_fail_closed(accepted_bundle, tmp_path):
    _, executable, _, _, manifest = accepted_bundle
    del manifest["cases"]["macos:3"]
    path = tmp_path / "native-acceptance.json"
    path.write_text(json.dumps(manifest))
    assert not accepted(executable)
    path.write_bytes(b"\xff")
    assert not accepted(executable)
