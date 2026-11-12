from src.services.browser import fingerprint
from src.services.profile.manager import ProfileManager
from src.utils.totp import clean_totp_secret, generate_totp, parse_account_line


def test_clean_and_generate_totp():
    assert clean_totp_secret("JBSW Y3DP EHPK 3PXP") == "JBSWY3DPEHPK3PXP"
    assert (
        clean_totp_secret("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&issuer=Google")
        == "JBSWY3DPEHPK3PXP"
    )
    assert clean_totp_secret("not-base32-!!!") == ""
    res = generate_totp("JBSW Y3DP EHPK 3PXP", now=0)
    assert res is not None
    code, remaining = res
    assert len(code) == 6
    assert code.isdigit()
    assert code == "282760"
    assert remaining == 30


def test_parse_account_line():
    parsed = parse_account_line("user@gmail.com | Pass123! | JBSW Y3DP EHPK 3PXP | Backup mail")
    assert parsed == {
        "account_uid": "user@gmail.com",
        "account_password": "Pass123!",
        "two_fa_secret": "JBSWY3DPEHPK3PXP",
        "notes": "Backup mail",
    }
    parsed_no_2fa = parse_account_line("user@gmail.com | Pass123! | Just a note without 2fa")
    assert parsed_no_2fa == {
        "account_uid": "user@gmail.com",
        "account_password": "Pass123!",
        "two_fa_secret": "",
        "notes": "Just a note without 2fa",
    }
    parsed_url_note = parse_account_line("new@example.test|password|https://example.test/recovery")
    assert parsed_url_note == {
        "account_uid": "new@example.test",
        "account_password": "password",
        "two_fa_secret": "",
        "notes": "https://example.test/recovery",
    }


def test_coherent_hardware_fingerprint_generation(tmp_path):
    gpus = fingerprint.available_gpus("macos")
    assert len(gpus) > 0
    apple_gpus = [g for g in gpus if "Apple M" in g[1]]
    selected_gpu = apple_gpus[0] if apple_gpus else gpus[0]

    fp = fingerprint.generate_coherent_fingerprint(
        "macos",
        screen_spec="1440x900",
        cpu_cores=8,
        gpu_pair=selected_gpu,
    )
    details = fingerprint.extract_details(fp, "macos")
    assert details["screen"] == "1440x900"
    assert details["hardware_concurrency"] in (8, 10, 12, 14, 16, 24)
    assert details["gpu_vendor"] == selected_gpu[0]
    assert details["gpu_renderer"] == selected_gpu[1]

    fingerprint.save_fingerprint(str(tmp_path), "macos", fp)
    loaded = fingerprint.load_saved_fingerprint(str(tmp_path))
    assert loaded == ("macos", fp)
    summary = fingerprint.summary(str(tmp_path))
    assert summary is not None
    assert summary["screen"] == "1440x900"


def test_coherence_zero_violations_across_all_os():
    from camoufox import coherence
    from camoufox.fingerprints import from_fpgen, is_software_renderer

    os_map = {"windows": "win", "macos": "mac", "linux": "lin"}
    for os_type, target_os in os_map.items():
        gpus = fingerprint.available_gpus(os_type)
        assert len(gpus) > 0
        for _vendor, renderer in gpus:
            assert not is_software_renderer(renderer)
            assert renderer != "Mozilla"
            assert coherence.gpu_fits_os(renderer, target_os)

        # Test both default auto and explicit GPU + screen overrides
        fp = fingerprint.generate_coherent_fingerprint(
            os_type,
            screen_spec="1512x982" if os_type == "macos" else "1920x1080",
            cpu_cores=8,
            gpu_pair=gpus[
                -1
            ],  # On macos, gpus[-1] is Intel/AMD; ensures notched-panel auto-resolution works
        )
        cfg = from_fpgen(fp, "158")
        cfg["webGl:vendor"] = fp["webgl"]["vendor"]
        cfg["webGl:renderer"] = fp["webgl"]["renderer"]
        violations = coherence.validate(cfg, target_os)
        assert violations == [], f"Unexpected coherence violations on {os_type}: {violations}"


def test_profile_manager_persists_account_and_hardware_fields(tmp_path, monkeypatch):
    import src.services.profile.manager as pm_mod

    profiles_file = str(tmp_path / "profiles.json")
    data_dir = str(tmp_path / "profiles")
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(pm_mod, "DATA_DIR", data_dir)

    mgr = ProfileManager()
    fp = fingerprint.generate_coherent_fingerprint("macos", screen_spec="1440x900", cpu_cores=8)
    ok = mgr.add_profile(
        name="Gmail Farming 01",
        proxy="",
        os_type="macos",
        group="Gmail",
        account_uid="farm01@gmail.com",
        account_password="SecretPassword!",
        two_fa_secret="JBSW Y3DP EHPK 3PXP",
        notes="Warming Day 1",
        start_url="https://mail.google.com",
        webrtc_mode="disable",
        canvas_mode="noise",
        initial_fingerprint=fp,
    )
    assert ok is True
    profile = mgr.profiles["Gmail Farming 01"]
    assert profile.group == "Gmail"
    assert profile.account_uid == "farm01@gmail.com"
    assert profile.two_fa_secret == "JBSWY3DPEHPK3PXP"
    assert profile.webrtc_mode == "disable"
    assert profile.canvas_mode == "noise"

    saved_pair = fingerprint.load_saved_fingerprint(mgr._data_path("Gmail Farming 01"))
    assert saved_pair is not None
    saved_os, saved_fp = saved_pair
    assert saved_os == "macos"
    assert saved_fp["screen"]["width"] == 1440

    updated = mgr.update_profile(
        "Gmail Farming 01",
        "Gmail Farming 01 Edited",
        "",
        "macos",
        group="Facebook",
        account_uid="fb_uid_1000",
        webrtc_mode="proxy",
        canvas_mode="native",
    )
    assert updated is True
    edited = mgr.profiles["Gmail Farming 01 Edited"]
    assert edited.group == "Facebook"
    assert edited.account_uid == "fb_uid_1000"
    assert edited.webrtc_mode == "proxy"
    assert edited.canvas_mode == "native"

    # Reload from disk to verify JSON persistence
    reloaded = ProfileManager()
    p2 = reloaded.profiles["Gmail Farming 01 Edited"]
    assert p2.group == "Facebook"
    assert p2.account_uid == "fb_uid_1000"
    assert p2.two_fa_secret == "JBSWY3DPEHPK3PXP"
    assert p2.webrtc_mode == "proxy"
    assert p2.canvas_mode == "native"


def test_f4_reject_unsupported_otpauth_parameters(tmp_path, monkeypatch):
    import src.services.profile.manager as pm_mod

    # Valid default otpauth URI
    assert (
        clean_totp_secret(
            "otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&digits=6&period=30&algorithm=SHA1"
        )
        == "JBSWY3DPEHPK3PXP"
    )
    # Non-default period / digits / algorithm / hotp must be rejected
    assert clean_totp_secret("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&period=60&digits=8") == ""
    assert clean_totp_secret("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&algorithm=SHA256") == ""
    assert clean_totp_secret("otpauth://hotp/Test?secret=JBSWY3DPEHPK3PXP&counter=1") == ""
    assert (
        generate_totp("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&period=60&digits=8", now=59)
        is None
    )

    profiles_file = str(tmp_path / "profiles.json")
    data_dir = str(tmp_path / "profiles")
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", profiles_file)
    monkeypatch.setattr(pm_mod, "DATA_DIR", data_dir)
    mgr = ProfileManager()
    assert (
        mgr.add_profile(
            "Bad2FA",
            "",
            "macos",
            two_fa_secret="otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&period=60&digits=8",
        )
        is False
    )


def test_f1_f2_f3_dialog_regressions():
    from unittest.mock import patch

    import flet as ft

    from src.models.profile import Profile
    from src.ui.dialogs.profile import open_profile_dialog

    class DummyPage:
        dialog = None

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            pass

    def walk(ctrl):
        yield ctrl
        for attr in ("content", "controls", "actions", "title"):
            children = getattr(ctrl, attr, None)
            for child in children if isinstance(children, list) else [children]:
                if isinstance(child, ft.Control):
                    yield from walk(child)

    page = DummyPage()
    saved = {}

    def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
        saved.update({"name": name, "engine": engine, **kwargs})
        return None

    # Open dialog with existing account + 2FA + notes at t=29
    with patch("src.utils.totp.time.time", return_value=29):
        open_profile_dialog(
            page,
            None,
            on_save,
            Profile(
                "fixture",
                os_type="macos",
                account_uid="old@example.test",
                account_password="old",
                two_fa_secret="JBSWY3DPEHPK3PXP",
                notes="old recovery",
            ),
        )

    controls = list(walk(page.dialog))
    preview = next(
        c for c in controls if isinstance(c, ft.Text) and "Live 2FA OTP:" in str(c.value)
    )
    with patch("src.utils.totp.time.time", return_value=29):
        assert "282760 (1s)" in preview.value

    # F2: Advance time to t=60 -> preview automatically reflects new window (602287 (30s))
    with patch("src.utils.totp.time.time", return_value=60):
        assert "602287 (30s)" in preview.value

    # F1: Quick Paste a new account without 2FA or notes -> clears old 2FA & notes
    paste_field = next(
        c
        for c in controls
        if isinstance(c, ft.TextField) and c.hint_text == "uid|password|2FA_SECRET|note"
    )
    paste_btn = next(
        c
        for c in controls
        if isinstance(c, ft.OutlinedButton)
        and c.on_click
        and c.on_click.__name__ == "_on_quick_paste"
    )
    paste_field.value = "new@example.test|new-password"
    paste_btn.on_click(None)
    page.dialog.actions[-1].on_click(None)
    assert saved["account_uid"] == "new@example.test"
    assert saved["account_password"] == "new-password"
    assert saved["two_fa_secret"] == ""
    assert saved["notes"] == ""

    # F2 dismissal: after save/close, timer is stopped
    with patch("src.utils.totp.time.time", return_value=90):
        assert preview.visible is False

    # F3: Chrome engine disables Screen/GPU/Canvas dropdowns while keeping CPU Cores, WebRTC, and Roll New enabled
    chrome_page = DummyPage()
    open_profile_dialog(
        chrome_page,
        None,
        on_save,
        Profile(
            "chrome-fixture",
            engine="chrome",
            os_type="macos",
            webrtc_mode="disable",
            canvas_mode="noise",
        ),
    )
    chrome_controls = list(walk(chrome_page.dialog))
    hw_dropdowns = [c for c in chrome_controls if isinstance(c, ft.Dropdown)][3:]
    assert len(hw_dropdowns) == 5
    # [screen_dropdown, cores_dropdown, gpu_dropdown, webrtc_dropdown, canvas_dropdown]
    assert [c.disabled for c in hw_dropdowns] == [True, False, True, False, True]
    chrome_page.dialog.actions[-1].on_click(None)
    assert saved["webrtc_mode"] == "disable"
    assert saved["canvas_mode"] == "noise"
    assert isinstance(saved["initial_fingerprint"].get("seed"), int)
    chrome_page.dialog.on_dismiss(None)


def test_ponytail_findings_start_url_auto_screen_os_reset_and_card_perf(tmp_path, monkeypatch):
    import json
    import zipfile
    from unittest.mock import patch

    import flet as ft
    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container
    from src.models.profile import Profile
    from src.services.profile import manager as pm_mod
    from src.services.profile.transfer import import_from_zip
    from src.ui.components.profile_card import build_profile_card
    from src.ui.dialogs.profile import open_profile_dialog
    from src.utils.validation import normalize_start_url, validate_start_url

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))

    # Finding #1: start_url validation & scheme sanitization
    assert normalize_start_url("mail.google.com") == "https://mail.google.com"
    assert normalize_start_url("https://example.com/login?x=1") == "https://example.com/login?x=1"
    for unsafe in (
        "file:///etc/passwd",
        "%66ile:///etc/passwd",
        "javascript:alert(1)",
        "chrome://settings",
        "about:config",
    ):
        assert normalize_start_url(unsafe) == ""
        assert validate_start_url(unsafe)[0] is False

    pm = pm_mod.ProfileManager()
    assert not pm.add_profile("bad-url", "", "macos", start_url="file:///etc/passwd")
    assert pm.add_profile("good-url", "", "macos", start_url="mail.google.com")
    assert pm.profiles["good-url"].start_url == "https://mail.google.com"
    assert not pm.update_profile(
        "good-url", "good-url", "", "macos", start_url="javascript:alert(1)"
    )

    # ZIP import strips unsafe start_url
    zip_path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr(
            "profile.json",
            json.dumps({"name": "zip-prof", "os_type": "macos", "start_url": "file:///etc/passwd"}),
        )
    ok, imported = import_from_zip(str(zip_path), str(tmp_path / "data"))
    assert ok and imported.start_url is None

    # API rejects unsafe start_url and invalid webrtc_mode/canvas_mode
    with TestClient(create_app(Container()), base_url="http://127.0.0.1") as client:
        assert (
            client.post(
                "/api/v1/profiles", json={"name": "p-bad", "start_url": "file:///etc/passwd"}
            ).status_code
            == 400
        )
        assert (
            client.post(
                "/api/v1/profiles", json={"name": "p-bad2", "webrtc_mode": "invalid"}
            ).status_code
            == 400
        )
        assert (
            client.patch(
                "/api/v1/profiles/good-url", json={"start_url": "chrome://version"}
            ).status_code
            == 400
        )

    # Finding #4: Selecting "auto" in screen_dropdown resets _custom_screen and keeps dropdown value "auto"
    class DummyPage:
        def __init__(self):
            self.dialog = None

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            pass

    def walk(ctrl):
        yield ctrl
        for attr in ("content", "controls", "actions"):
            val = getattr(ctrl, attr, None)
            if isinstance(val, list):
                for item in val:
                    yield from walk(item)
            elif val is not None:
                yield from walk(val)

    saved = {}
    page = DummyPage()
    open_profile_dialog(
        page,
        None,
        lambda *a, **kw: (saved.update(kw), None)[1],
        Profile("auto-scr-test", os_type="macos"),
    )
    dropdowns = [c for c in walk(page.dialog) if isinstance(c, ft.Dropdown)]
    screen_dd = dropdowns[3]
    screen_dd.value = "2560x1440"
    screen_dd.on_select(None)
    assert screen_dd.value == "2560x1440"
    screen_dd.value = "auto"
    screen_dd.on_select(None)
    assert screen_dd.value == "auto"
    page.dialog.actions[-1].on_click(None)
    assert not saved["initial_fingerprint"].get("_custom_screen")
    page.dialog.on_dismiss(None)

    # Finding #9: Updating OS without initial_fingerprint resets stale saved fingerprint/persona
    fp_dir = tmp_path / "data" / "good-url"
    fp_file = fp_dir / "fingerprint.json"
    persona_file = fp_dir / "chrome_persona.json"
    fp_file.write_text('{"os": "macos", "fingerprint": {}}', encoding="utf-8")
    persona_file.write_text('{"platform": "macos"}', encoding="utf-8")
    assert pm.update_profile("good-url", "good-url", "", "windows")
    assert not fp_file.exists() and not persona_file.exists()

    # Finding #11: build_profile_card does not call generate_totp during render
    with patch(
        "src.utils.totp.generate_totp", side_effect=AssertionError("should not be called on render")
    ):
        card = build_profile_card(
            Profile("card-test", two_fa_secret="JBSWY3DPEHPK3PXP"),
            is_loading=False,
            is_running=False,
            on_launch=lambda *_: None,
            on_edit=lambda *_: None,
            on_delete=lambda *_: None,
            on_copy_2fa=lambda *_: None,
            row_index=1,
        )
        assert card is not None


def test_space_delimited_quick_paste_and_trait_hash_and_geo_sync_and_responsive_dialogs():
    import flet as ft

    from src.models.profile import Profile
    from src.services.proxy.geo_check import GeoCheckResult, SourceResult
    from src.ui.dialogs.profile import _geo_lines, open_profile_dialog

    # 1. Space-delimited and tab-delimited Quick Paste formats
    p_space_3 = parse_account_line("user@gmail.com 9351702a dtvw2ocwirobpfkabcdef23456723456")
    assert p_space_3 == {
        "account_uid": "user@gmail.com",
        "account_password": "9351702a",
        "two_fa_secret": "DTVW2OCWIROBPFKABCDEF23456723456",
        "notes": "",
    }
    p_space_4 = parse_account_line(
        "user@gmail.com 9351702a dtvw2ocwirobpfkabcdef23456723456 Recovery mail note"
    )
    assert p_space_4 == {
        "account_uid": "user@gmail.com",
        "account_password": "9351702a",
        "two_fa_secret": "DTVW2OCWIROBPFKABCDEF23456723456",
        "notes": "Recovery mail note",
    }
    p_space_grouped = parse_account_line("user@gmail.com Pass123! JBSW Y3DP EHPK 3PXP Backup note")
    assert p_space_grouped == {
        "account_uid": "user@gmail.com",
        "account_password": "Pass123!",
        "two_fa_secret": "JBSWY3DPEHPK3PXP",
        "notes": "Backup note",
    }
    p_space_url_note = parse_account_line("user@gmail.com Pass123! https://example.test/recovery")
    assert p_space_url_note == {
        "account_uid": "user@gmail.com",
        "account_password": "Pass123!",
        "two_fa_secret": "",
        "notes": "https://example.test/recovery",
    }
    p_tab = parse_account_line("user@gmail.com\tPass123!\tJBSWY3DPEHPK3PXP\tTab note")
    assert p_tab == {
        "account_uid": "user@gmail.com",
        "account_password": "Pass123!",
        "two_fa_secret": "JBSWY3DPEHPK3PXP",
        "notes": "Tab note",
    }

    # 2. extract_details normalizes runtime Firefox version & computes trait_hash
    fp = fingerprint.generate_coherent_fingerprint("macos", screen_spec="1512x982", cpu_cores=8)
    details = fingerprint.extract_details(fp, "macos")
    assert "rv:158.0" in details["user_agent"]
    assert "Firefox/158.0" in details["user_agent"]
    assert details["trait_hash"].startswith("#") and len(details["trait_hash"]) == 9

    # 3. _geo_lines warns on Timezone/Locale mismatch and provides Use Timezone, Use Locale, and Sync Both buttons
    geo_res = GeoCheckResult(
        exit_ip="66.93.164.242",
        country="US",
        auto_timezone="America/Chicago",
        suggested_locale="en-US",
        timezone="America/Chicago",
        timezone_pinned=False,
        locale=None,
        locale_pinned=False,
        sources=[
            SourceResult(
                "ipinfo",
                ip="66.93.164.242",
                country="US",
                city="Chicago",
                timezone="America/Chicago",
            ),
        ],
        warnings=[],
        error=None,
    )
    applied = {"tz": "", "loc": ""}
    ctrls = _geo_lines(
        geo_res,
        use_locale=lambda v: applied.__setitem__("loc", v),
        use_timezone=lambda v: applied.__setitem__("tz", v),
        use_both=lambda t_v, l_v: (applied.__setitem__("tz", t_v), applied.__setitem__("loc", l_v)),
        current_tz="",
        current_locale="",
    )
    texts = [c.value for c in ctrls if isinstance(c, ft.Text)]
    assert any("America/Chicago" in t and "en-US" in t and "\u26a0" in t for t in texts)
    btn_row = next(c for c in ctrls if isinstance(c, ft.Row))
    assert len(btn_row.controls) == 3
    # Click Use Timezone
    btn_row.controls[0].on_click(None)
    assert applied["tz"] == "America/Chicago"
    # Click Use Locale
    btn_row.controls[1].on_click(None)
    assert applied["loc"] == "en-US"
    # Click Sync Both
    applied["tz"] = applied["loc"] = ""
    btn_row.controls[2].on_click(None)
    assert applied == {"tz": "America/Chicago", "loc": "en-US"}

    # Once synced, _geo_lines shows ✓ No mismatch found and no action buttons
    synced_ctrls = _geo_lines(
        geo_res,
        use_locale=lambda v: None,
        use_timezone=lambda v: None,
        use_both=lambda t_v, l_v: None,
        current_tz="America/Chicago",
        current_locale="en-US",
    )
    synced_texts = [c.value for c in synced_ctrls if isinstance(c, ft.Text)]
    assert any("\u2713" in t for t in synced_texts)
    assert not any(isinstance(c, ft.Row) for c in synced_ctrls)

    # 4. Responsive dialog resize scaling & cleanup on dismiss
    class ResizablePage:
        def __init__(self, width=1280, height=820):
            self.width = width
            self.height = height
            self.dialog = None
            self.on_resize = None

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            self.dialog = None

    rpage = ResizablePage(1280, 820)
    open_profile_dialog(rpage, None, lambda *a, **kw: None, Profile("resp-test", os_type="macos"))
    body = rpage.dialog.content
    w_initial = body.width
    assert 800 <= w_initial <= 1120
    # Shrink window to compact width -> columns stack & width scales down
    rpage.width = 640
    rpage.height = 560
    rpage.on_resize(None)
    assert body.width < w_initial
    assert len(body.content.controls) == 3  # stacked left_column + Divider + right_column
    # Expand window back to wide -> 2-column Row restored
    rpage.width = 1360
    rpage.height = 900
    rpage.on_resize(None)
    assert len(body.content.controls) == 1 and isinstance(body.content.controls[0], ft.Row)
    rpage.dialog.on_dismiss(None)
    assert rpage.on_resize is None


def test_chrome_extensions_zip_recovery_email_split_and_chrome_persona(tmp_path, monkeypatch):
    import io
    import json
    import zipfile

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container
    from src.models.profile import Profile, split_recovery_and_notes
    from src.services.browser import extensions as ext_mod
    from src.services.browser import fingerprint
    from src.services.profile import manager as pm_mod
    from src.ui.dialogs.extensions import open_extension_settings_dialog, open_extensions_dialog

    # 1. split_recovery_and_notes handles legacy formats cleanly
    assert split_recovery_and_notes(None, "Mail Recovery: recovery@gmail.com") == (
        "recovery@gmail.com",
        "",
    )
    assert split_recovery_and_notes(None, "tienbac928@gmail.com") == ("tienbac928@gmail.com", "")
    assert split_recovery_and_notes(None, "Backup account tienbac928@gmail.com") == (
        "tienbac928@gmail.com",
        "Backup account",
    )
    assert split_recovery_and_notes("explicit@gmail.com", "Keep note") == (
        "explicit@gmail.com",
        "Keep note",
    )

    p_legacy = Profile("leg", notes="Mail Recovery: old@gmail.com")
    assert p_legacy.effective_recovery_email == "old@gmail.com"
    assert p_legacy.effective_notes == ""

    # 2. Chrome Persona generator & extractor
    cp = fingerprint.generate_chrome_persona("macos", cpu_cores=12, seed=987654321)
    cd = fingerprint.extract_chrome_details(cp, "macos")
    assert cd["trait_hash"].startswith("#SEED-")
    assert cd["hardware_concurrency"] == 12
    assert "Chrome/148.0.0.0" in cd["user_agent"]
    persona_path = fingerprint.save_chrome_persona(str(tmp_path / "cprof"), cp)
    assert fingerprint.load_saved_chrome_persona(str(tmp_path / "cprof"))["seed"] == cd["seed"]
    assert persona_path.endswith("chrome_persona.json")

    # 3. ExtensionService .zip install, pin_mode, profile assignment, and resolve_for_profile
    ext_svc = ext_mod.ExtensionService(
        extensions_file=str(tmp_path / "extensions.json"),
        extensions_dir=str(tmp_path / "extensions_dir"),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "nested/manifest.json",
            json.dumps(
                {
                    "manifest_version": 3,
                    "name": "Gemini Pro 24m",
                    "version": "1.0.0",
                    "description": "Google One / AI Pro",
                    "icons": {"128": "icon.png"},
                }
            ),
        )
        zf.writestr("nested/icon.png", b"\x89PNG\r\n\x1a\nfake")
    entry = ext_svc.install_from_zip(buf.getvalue(), filename="gemini_pro.zip")
    assert entry.name == "Gemini Pro 24m"
    assert entry.version == "1.0.0"
    assert len(entry.chrome_ext_id) == 32
    assert all("a" <= ch <= "p" for ch in entry.chrome_ext_id)

    ext_svc.update_extension(entry.id, pin_mode="force_pinned")
    paths, pinned, unpinned = ext_svc.resolve_for_profile([entry.id])
    assert len(paths) == 1
    assert pinned == [entry.chrome_ext_id]
    assert unpinned == []

    ext_svc.update_extension(entry.id, pin_mode="force_unpinned")
    _, pinned2, unpinned2 = ext_svc.resolve_for_profile([entry.id])
    assert pinned2 == []
    assert unpinned2 == [entry.chrome_ext_id]

    # 4. ProfileManager + ExtensionService assignment & FastAPI routes
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    pm.add_profile("chrome-1", "", "macos", engine="chrome", recovery_email="rec1@gmail.com")
    pm.add_profile("chrome-2", "", "windows", engine="chrome")
    ext_svc.assign_profiles(entry.id, ["chrome-1"], pm)
    assert pm.profiles["chrome-1"].extension_ids == [entry.id]
    assert ext_svc.usage_counts(pm)[entry.id] == 1

    container = Container()
    container._instances["pm"] = pm
    container._instances["es"] = ext_svc
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        r_list = client.get("/api/v1/extensions")
        assert r_list.status_code == 200
        assert len(r_list.json()["items"]) == 1
        r_icon = client.get(f"/api/v1/extensions/{entry.id}/icon")
        assert r_icon.status_code == 200
        r_patch = client.patch(
            f"/api/v1/extensions/{entry.id}",
            json={"pin_mode": "force_pinned", "assigned_profiles": ["chrome-1", "chrome-2"]},
        )
        assert r_patch.status_code == 200
        assert r_patch.json()["pin_mode"] == "force_pinned"
        assert pm.profiles["chrome-2"].extension_ids == [entry.id]

    # 5. Flet Extensions Dialog & Extension Settings Dialog
    class DummyPage:
        def __init__(self):
            self.width = 1280
            self.height = 820
            self.dialog = None
            self.on_resize = None
            self.overlay = []

        def update(self):
            pass

        def show_dialog(self, dlg):
            self.dialog = dlg

        def pop_dialog(self):
            self.dialog = None

    dpage = DummyPage()
    open_extensions_dialog(dpage, ext_svc, pm)
    assert dpage.dialog is not None
    dpage.dialog.on_dismiss(None)

    spage = DummyPage()
    open_extension_settings_dialog(spage, entry, ext_svc, pm)
    assert spage.dialog is not None
    # Confirm button saves pin_mode & assigned_profiles
    spage.dialog.actions[-1].on_click(None)


def test_reviewer_findings_legacy_recovery_upload_dedupe_pin_validation(tmp_path, monkeypatch):
    import base64
    import io
    import json
    import zipfile

    from fastapi.testclient import TestClient

    from src.api.app import create_app
    from src.core.container import Container
    from src.services.browser import extensions as ext_mod
    from src.services.profile import manager as pm_mod

    # P1: legacy notes holding a recovery mail are normalized on load and survive inline edits
    store = tmp_path / "profiles.json"
    store.write_text(
        json.dumps(
            {
                "leg": {
                    "name": "leg",
                    "os_type": "macos",
                    "notes": "Mail Recovery: old@gmail.com\nMy note",
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(store))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    pm = pm_mod.ProfileManager()
    assert pm.profiles["leg"].recovery_email == "old@gmail.com"
    assert pm.profiles["leg"].notes == "My note"
    assert pm.update_profile("leg", "leg", "", "macos", notes="Updated note")
    assert pm.profiles["leg"].recovery_email == "old@gmail.com"
    assert pm.update_profile("leg", "leg", "", "macos", recovery_email="")
    assert pm.profiles["leg"].effective_recovery_email == ""
    assert pm.profiles["leg"].effective_notes == "Updated note"

    # P1: uploading the same .zip twice updates one entry; P2: invalid pin_mode -> 400
    ext_svc = ext_mod.ExtensionService(
        file_path=str(tmp_path / "extensions.json"), extensions_dir=str(tmp_path / "ext")
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json", json.dumps({"manifest_version": 3, "name": "Dup", "version": "1.0"})
        )
    payload = {"filename": "dup.zip", "zip_base64": base64.b64encode(buf.getvalue()).decode()}
    container = Container()
    container._instances["pm"] = pm
    container._instances["es"] = ext_svc
    with TestClient(create_app(container), base_url="http://127.0.0.1") as client:
        first = client.post("/api/v1/extensions/upload", json=payload).json()
        second = client.post("/api/v1/extensions/upload", json=payload).json()
        assert first["id"] == second["id"]
        assert first["source_zip"] == "dup.zip"
        assert len(ext_svc.list_entries()) == 1
        assert (
            client.patch(
                f"/api/v1/extensions/{first['id']}", json={"pin_mode": "bogus"}
            ).status_code
            == 400
        )

    # P2: tampered icon path outside the extension dir is never served
    entry = ext_svc.list_entries()[0]
    entry.icon_rel_path = "../../profiles.json"
    assert entry.icon_abs_path is None


def test_atomic_extension_reimport_preserves_on_failure(tmp_path):
    """Corrupted re-import must not delete or break a working installed extension."""
    import io
    import json
    import struct
    import zipfile
    from pathlib import Path

    import pytest

    from src.services.browser.extensions import ExtensionService

    svc = ExtensionService(
        file_path=str(tmp_path / "extensions.json"),
        extensions_dir=str(tmp_path / "extensions"),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest_version": 3, "name": "AtomicTest", "version": "1.0"}),
        )
        zf.writestr("code.js", "console.log('original');")
    good_bytes = buf.getvalue()
    entry = svc.install_from_zip(good_bytes, filename="ext.zip")

    target_path = Path(entry.path)
    assert target_path.exists()
    assert (target_path / "code.js").read_text() == "console.log('original');"
    assert len(svc.resolve_for_profile([entry.id])[0]) == 1

    # Corrupt zip payload
    with zipfile.ZipFile(io.BytesIO(good_bytes)) as zf:
        offset = zf.getinfo("code.js").header_offset
    corrupt = bytearray(good_bytes)
    fn_len, extra_len = struct.unpack_from("<HH", corrupt, offset + 26)
    corrupt[offset + 30 + fn_len + extra_len] ^= 1

    with pytest.raises(ValueError, match="Bad CRC-32"):
        svc.install_from_zip(bytes(corrupt), filename="ext.zip")

    # Working extension and files must remain completely intact
    assert target_path.exists()
    assert (target_path / "code.js").read_text() == "console.log('original');"
    assert svc.get(entry.id) is not None
    assert len(svc.resolve_for_profile([entry.id])[0]) == 1


def test_chrome_persona_preserves_identity_on_metadata_update(tmp_path):
    """save_chrome_persona must preserve brand, version, timezone, locale on metadata edits."""
    from pathlib import Path

    from src.services.browser import fingerprint as fp_svc

    profile_dir = str(tmp_path / "chrome_p1")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)

    original_persona = {
        "seed": 4242,
        "platform": "windows",
        "platform_version": "10.0.0",
        "brand": "Edge",
        "hardware_concurrency": 20,
        "timezone": "Europe/London",
        "locale": "en-GB",
    }
    fp_svc.save_chrome_persona(profile_dir, original_persona)
    saved = fp_svc.load_saved_chrome_persona(profile_dir)
    assert saved["seed"] == 4242
    assert saved["brand"] == "Edge"
    assert saved["hardware_concurrency"] == 20
    assert saved["platform_version"] == "10.0.0"
    assert saved["timezone"] == "Europe/London"

    # Simulate metadata-only or core-only update (e.g. from Web / API PATCH)
    partial_update = {
        "seed": 4242,
        "platform": "windows",
        "hardware_concurrency": 16,
    }
    fp_svc.save_chrome_persona(profile_dir, partial_update)
    updated = fp_svc.load_saved_chrome_persona(profile_dir)
    assert updated["seed"] == 4242
    assert updated["hardware_concurrency"] == 16
    assert updated["brand"] == "Edge", "Brand must be preserved"
    assert updated["platform_version"] == "10.0.0", "Platform version must be preserved"
    assert updated["timezone"] == "Europe/London", "Timezone must be preserved"
    assert updated["locale"] == "en-GB", "Locale must be preserved"
