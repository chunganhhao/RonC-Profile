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
    page.dialog.actions[-1].content.on_click(None)
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
    chrome_page.dialog.actions[-1].content.on_click(None)
    assert saved["webrtc_mode"] == "disable"
    assert saved["canvas_mode"] == "noise"
    assert saved["initial_fingerprint"] is None
    next(
        c for c in chrome_controls if getattr(c, "icon", None) == ft.Icons.CASINO_OUTLINED
    ).on_click(None)
    chrome_page.dialog.actions[-1].content.on_click(None)
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
    page.dialog.actions[-1].content.on_click(None)
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
        exit_ip="203.0.113.42",
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
                ip="203.0.113.42",
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
    body = rpage.dialog.content.content
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
    assert split_recovery_and_notes(None, "alt@example.test") == ("alt@example.test", "")
    assert split_recovery_and_notes(None, "Backup account alt@example.test") == (
        "alt@example.test",
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
    spage.dialog.actions[-1].content.on_click(None)


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


def test_extension_relocation_fallback_after_workspace_rename(tmp_path):
    """Stale absolute paths in extensions.json fall back to current extensions_dir/<id>."""
    import io
    import json
    import shutil
    import zipfile
    from pathlib import Path

    from src.services.browser.extensions import ExtensionService, compute_chrome_extension_id

    old_root = tmp_path / "foxprofile"
    new_root = tmp_path / "RonC Profile"
    svc_old = ExtensionService(
        file_path=str(old_root / "extensions.json"),
        extensions_dir=str(old_root / "extensions_data"),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest_version": 3, "name": "PortableExt", "version": "1.0"}),
        )
    installed = svc_old.install_from_zip(buf.getvalue(), filename="portable.zip")
    assert str(old_root) in installed.path

    # Simulate renaming the workspace folder while extensions.json still stores old_root.
    shutil.move(str(old_root), str(new_root))
    svc_new = ExtensionService(
        file_path=str(new_root / "extensions.json"),
        extensions_dir=str(new_root / "extensions_data"),
    )
    relocated = svc_new.get(installed.id)
    assert relocated is not None
    expected_dir = (new_root / "extensions_data" / installed.id).resolve()
    assert relocated.path == str(expected_dir)
    assert relocated.chrome_id == compute_chrome_extension_id(expected_dir)
    paths, _, _ = svc_new.resolve_for_profile([installed.id])
    assert paths == [str(expected_dir)]

    # Reconcile stale chrome_id even when extensions.json already has the renamed path.
    ext_cfg = json.loads((new_root / "extensions.json").read_text())
    ext_cfg["extensions"][0]["path"] = str(expected_dir)
    ext_cfg["extensions"][0]["chrome_id"] = "a" * 32
    ext_cfg["extensions"][0]["chrome_ext_id"] = "a" * 32
    (new_root / "extensions.json").write_text(json.dumps(ext_cfg))
    reconciled = svc_new.get(installed.id)
    assert reconciled is not None
    assert reconciled.chrome_id == compute_chrome_extension_id(expected_dir)

    # Multi-segment / dot IDs must not resolve to extensions_dir or wipe it on remove.
    bad_cfg = {
        "extensions": [
            {
                "id": "sub/..",
                "name": "BadRel",
                "path": str(new_root / "missing" / "."),
                "chrome_id": "b" * 32,
            }
        ]
    }
    (new_root / "extensions.json").write_text(json.dumps(bad_cfg))
    bad_entry = svc_new.get("sub/..")
    assert bad_entry is not None
    assert Path(bad_entry.path) != (new_root / "extensions_data").resolve()
    bad_entry.path = str((new_root / "extensions_data").resolve())
    svc_new._save()
    assert svc_new.remove_extension("sub/..")
    assert (new_root / "extensions_data").is_dir()
    assert expected_dir.is_dir()


def test_rename_workspace_script_blocks_keyless_extensions_without_confirmation(tmp_path):
    """rename_workspace_to_ronc.sh must refuse keyless extensions unless explicitly confirmed."""
    import base64
    import io
    import json
    import os
    import subprocess
    import zipfile
    from pathlib import Path

    from src.services.browser.extensions import ExtensionService, compute_chrome_extension_id

    script = Path(__file__).resolve().parents[1] / "scripts/rename_workspace_to_ronc.sh"
    parent = tmp_path / "ws"
    old_dir = parent / "foxprofile"
    new_dir = parent / "RonC Profile"
    old_dir.mkdir(parents=True)

    svc = ExtensionService(
        file_path=str(old_dir / "extensions.json"),
        extensions_dir=str(old_dir / "extensions_data"),
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"manifest_version": 3, "name": "KeylessExt", "version": "1.0"}),
        )
    installed = svc.install_from_zip(buf.getvalue(), filename="keyless.zip")

    env = {**os.environ, "RONC_RENAME_PARENT": str(parent)}
    blocked = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert blocked.returncode != 0
    assert installed.id in blocked.stderr
    assert old_dir.is_dir() and not new_dir.exists()

    confirmed = subprocess.run(
        ["bash", str(script), "--allow-extension-id-reset"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert confirmed.returncode == 0, confirmed.stderr
    assert new_dir.is_dir() and not old_dir.exists()

    # Keyed extensions preserve their Chrome ID without needing the reset flag.
    parent_keyed = tmp_path / "ws_keyed"
    old_keyed = parent_keyed / "foxprofile"
    new_keyed = parent_keyed / "RonC Profile"
    old_keyed.mkdir(parents=True)
    svc_k = ExtensionService(
        file_path=str(old_keyed / "extensions.json"),
        extensions_dir=str(old_keyed / "extensions_data"),
    )
    key_b64 = base64.b64encode(b"fixed-public-key-der").decode("ascii")
    buf_k = io.BytesIO()
    with zipfile.ZipFile(buf_k, "w") as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {"manifest_version": 3, "name": "KeyedExt", "version": "1.0", "key": key_b64}
            ),
        )
    installed_k = svc_k.install_from_zip(buf_k.getvalue(), filename="keyed.zip")
    ok_keyed = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "RONC_RENAME_PARENT": str(parent_keyed)},
        capture_output=True,
        text=True,
    )
    assert ok_keyed.returncode == 0, ok_keyed.stderr
    assert new_keyed.is_dir()
    cfg = json.loads((new_keyed / "extensions.json").read_text())
    expected_cid = compute_chrome_extension_id(
        new_keyed / "extensions_data" / installed_k.id, {"key": key_b64}
    )
    assert cfg["extensions"][0]["chrome_id"] == expected_cid
    assert cfg["extensions"][0]["chrome_id"] == installed_k.chrome_id

    # P2 #1 regression: keyless extension resolved via stale-path basename fallback (id != folder)
    # must be blocked by default without mutating fixture files or chrome_id.
    parent_bn = tmp_path / "ws_basename"
    old_bn = parent_bn / "foxprofile"
    new_bn = parent_bn / "RonC Profile"
    actual_ext = old_bn / "extensions_data" / "actual-folder"
    actual_ext.mkdir(parents=True)
    (actual_ext / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "KeylessLegacy", "version": "1.0"})
    )
    stale_registry = {
        "extensions": [
            {
                "id": "logical-id",
                "name": "KeylessLegacy",
                "path": "/gone/previous-workspace/extensions_data/actual-folder",
                "chrome_id": "a" * 32,
                "enabled": True,
                "apply_to_all": True,
            }
        ]
    }
    (old_bn / "extensions.json").write_text(json.dumps(stale_registry))
    svc_bn = ExtensionService(
        file_path=str(old_bn / "extensions.json"),
        extensions_dir=str(old_bn / "extensions_data"),
    )
    resolved_bn = svc_bn.get("logical-id")
    assert resolved_bn is not None
    assert resolved_bn.path == str(actual_ext.resolve())
    expected_before_id = compute_chrome_extension_id(actual_ext.resolve())
    assert resolved_bn.chrome_id == expected_before_id
    # Keep stale path untouched in extensions.json (matching real pre-rename stale registry).
    stale_registry["extensions"][0]["chrome_id"] = expected_before_id
    (old_bn / "extensions.json").write_text(json.dumps(stale_registry))
    snapshot_before = (old_bn / "extensions.json").read_text()

    blocked_bn = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "RONC_RENAME_PARENT": str(parent_bn)},
        capture_output=True,
        text=True,
    )
    assert blocked_bn.returncode != 0
    assert "logical-id" in blocked_bn.stderr
    assert old_bn.is_dir() and not new_bn.exists()
    assert (old_bn / "extensions.json").read_text() == snapshot_before
    assert compute_chrome_extension_id(actual_ext.resolve()) == expected_before_id

    # Escape / outside-extensions_data symlink must not bypass containment or break rename cleanly.
    outside_target = tmp_path / "outside_ext"
    outside_target.mkdir()
    (outside_target / "manifest.json").write_text(
        json.dumps({"manifest_version": 3, "name": "OutsideSymlink", "version": "1.0"})
    )
    parent_sym = tmp_path / "ws_symlink"
    old_sym = parent_sym / "foxprofile"
    new_sym = parent_sym / "RonC Profile"
    (old_sym / "extensions_data").mkdir(parents=True)
    (old_sym / "extensions_data" / "escape-folder").symlink_to(outside_target)
    (old_sym / "extensions.json").write_text(
        json.dumps(
            {
                "extensions": [
                    {
                        "id": "escape-id",
                        "name": "OutsideSymlink",
                        "path": "/gone/previous-workspace/extensions_data/escape-folder",
                        "chrome_id": "c" * 32,
                    }
                ]
            }
        )
    )
    ok_sym = subprocess.run(
        ["bash", str(script)],
        env={**os.environ, "RONC_RENAME_PARENT": str(parent_sym)},
        capture_output=True,
        text=True,
    )
    assert ok_sym.returncode == 0, ok_sym.stderr
    assert new_sym.is_dir() and not old_sym.exists()

    # With explicit --allow-extension-id-reset, basename fallback rename succeeds and reconciles to a new chrome_id
    # (confirmed ID reset, NOT storage preservation).
    confirmed_bn = subprocess.run(
        ["bash", str(script), "--allow-extension-id-reset"],
        env={**os.environ, "RONC_RENAME_PARENT": str(parent_bn)},
        capture_output=True,
        text=True,
    )
    assert confirmed_bn.returncode == 0, confirmed_bn.stderr
    assert new_bn.is_dir() and not old_bn.exists()
    after_bn_svc = ExtensionService(
        file_path=str(new_bn / "extensions.json"),
        extensions_dir=str(new_bn / "extensions_data"),
    )
    after_bn = after_bn_svc.get("logical-id")
    expected_moved_dir = (new_bn / "extensions_data" / "actual-folder").resolve()
    assert after_bn is not None
    assert after_bn.path == str(expected_moved_dir)
    assert after_bn.chrome_id == compute_chrome_extension_id(expected_moved_dir)
    assert after_bn.chrome_id != expected_before_id


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


def test_quick_paste_flushes_all_fields_in_one_click_on_real_flet_session(tmp_path, monkeypatch):
    """Quick Paste Parse must flush every mutated field + live 2FA OTP in a single click."""
    import asyncio
    from unittest.mock import patch

    import flet as ft
    import msgpack
    from flet.controls.context import _context_page
    from flet.messaging.connection import Connection
    from flet.messaging.protocol import configure_encode_object_for_msgpack
    from flet.messaging.session import Session
    from flet.pubsub.pubsub_hub import PubSubHub

    from src.core import config as app_config
    from src.core.strings import get_string
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))
    enc = configure_encode_object_for_msgpack(ft.BaseControl)

    def walk(ctrl):
        yield ctrl
        for attr in ("content", "controls", "actions", "title"):
            children = getattr(ctrl, attr, None)
            for child in children if isinstance(children, list) else [children]:
                if isinstance(child, ft.Control):
                    yield from walk(child)

    class CaptureConnection(Connection):
        def __init__(self, loop):
            super().__init__()
            self.loop = loop
            self.pubsubhub = PubSubHub(loop=loop)
            self.sent = []

        def send_message(self, message):
            # Real Flet transports msgpack-encode every outbound patch, which commits
            # control `_prev_*` snapshots for subsequent ObjectPatch diffs.
            packed = msgpack.packb(message.body.patch, default=enc)
            self.sent.append(msgpack.unpackb(packed, strict_map_key=False))

    async def scenario():
        conn = CaptureConnection(asyncio.get_running_loop())
        session = Session(conn)
        page = session.page
        msgpack.packb(session.get_page_patch(), default=enc)
        token = _context_page.set(page)
        dialog = None
        try:
            with patch("src.utils.totp.time.time", return_value=0):
                open_profile_dialog(page, None, lambda *a, **kw: None)
                dialog = page._dialogs.controls[-1]
                page.update()
                conn.sent.clear()

                controls = list(walk(dialog))
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
                fields = [c for c in controls if isinstance(c, ft.TextField)]
                name_field = next(
                    c for c in fields if c.hint_text == get_string("enter_profile_name")
                )
                uid_field = next(c for c in fields if c.hint_text == "user@gmail.com / 100089...")
                pass_field = next(c for c in fields if c.password is True)
                two_fa_field = next(c for c in fields if c.hint_text == "JBSWY3DPEHPK3PXP")
                rec_field = next(
                    c for c in fields if c.hint_text == get_string("recovery_email_hint")
                )
                backup_field = next(
                    c for c in fields if getattr(c, "key", None) == "dialog_backup_codes_field"
                )
                notes_field = next(c for c in fields if c.hint_text == get_string("notes_hint"))
                totp_preview = next(
                    c
                    for c in controls
                    if isinstance(c, ft.Text) and getattr(c, "_refresh_hook", None) is not None
                )

                # Simulate client syncing pasted text into Quick Paste field, then 1 click on Parse.
                paste_field.value = (
                    "acct1@example.test | Pass123! | JBSW Y3DP EHPK 3PXP | "
                    "backup@example.test extra note"
                )
                page.update()
                conn.sent.clear()

                await session.dispatch_event(paste_btn._i, "click", None)

                assert name_field.value == "acct1"
                assert uid_field.value == "acct1@example.test"
                assert pass_field.value == "Pass123!"
                assert two_fa_field.value == "JBSWY3DPEHPK3PXP"
                assert rec_field.value == "backup@example.test"
                assert notes_field.value == "extra note"
                assert paste_field.value == ""
                assert totp_preview.visible is True
                assert "282760" in totp_preview.value

                def patched_values_by_control_id(patches):
                    """Resolve Flet 1.x wire patch ops to {control._i: {prop: value}}."""

                    def map_tree(node_spec, obj, nodes):
                        node_id = node_spec[0]
                        nodes[node_id] = obj
                        if len(node_spec) > 1 and isinstance(node_spec[1], dict):
                            for key, child_spec in node_spec[1].items():
                                child = (
                                    obj[key] if isinstance(key, int) else getattr(obj, key, None)
                                )
                                if child is not None:
                                    map_tree(child_spec, child, nodes)

                    by_id = {}
                    for patch_ops in patches:
                        if not patch_ops:
                            continue
                        nodes = {}
                        map_tree(patch_ops[0], page, nodes)
                        for op in patch_ops[1:]:
                            if isinstance(op, list) and len(op) >= 4 and op[0] == 0:
                                target = nodes.get(op[1])
                                if isinstance(target, ft.BaseControl):
                                    by_id.setdefault(target._i, {})[op[2]] = op[3]
                    return by_id

                # Wire proof: ALL mutated controls must be flushed per-field in Click 1.
                patched = patched_values_by_control_id(conn.sent)
                assert patched[name_field._i]["value"] == "acct1"
                assert patched[uid_field._i]["value"] == "acct1@example.test"
                assert patched[pass_field._i]["value"] == "Pass123!"
                assert patched[two_fa_field._i]["value"] == "JBSWY3DPEHPK3PXP"
                assert patched[rec_field._i]["value"] == "backup@example.test"
                assert patched[notes_field._i]["value"] == "extra note"
                assert patched[paste_field._i]["value"] == ""
                assert patched[totp_preview._i]["visible"] is True
                assert "282760" in patched[totp_preview._i]["value"]

                # Click 2 on empty Quick Paste field must be a no-op with zero wire patches.
                conn.sent.clear()
                await session.dispatch_event(paste_btn._i, "click", None)
                assert conn.sent == []

                # Enter/submit event fallback with a new UID (no recovery) flushes UID + clears
                # the previous account's recovery email and backup codes on the wire in one flush.
                backup_field.value = "1111 0001\n1111 0002"
                page.update()
                conn.sent.clear()
                paste_field.value = ""
                await session.dispatch_event(
                    paste_field._i,
                    "submit",
                    "acct2@example.test | Pass456! | JBSW Y3DP EHPK 3PXP",
                )
                assert uid_field.value == "acct2@example.test"
                assert pass_field.value == "Pass456!"
                assert two_fa_field.value == "JBSWY3DPEHPK3PXP"
                assert rec_field.value == ""
                assert backup_field.value == ""
                patched_b = patched_values_by_control_id(conn.sent)
                assert patched_b[uid_field._i]["value"] == "acct2@example.test"
                assert patched_b[pass_field._i]["value"] == "Pass456!"
                assert patched_b[rec_field._i]["value"] == ""
                assert patched_b[backup_field._i]["value"] == ""
        finally:
            if dialog is not None:
                await session.dispatch_event(dialog._i, "dismiss", None)
            _context_page.reset(token)

    asyncio.run(scenario())


def test_quick_paste_recovery_email_cross_account_integrity(tmp_path, monkeypatch):
    """Quick Paste must never carry Account A's recovery email onto Account B while preserving same-UID recovery."""
    import flet as ft

    from src.core import config as app_config
    from src.models.profile import Profile
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))

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

    def run_paste_and_save(initial_profile, lines):
        page = DummyPage()
        saved = {}

        def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
            saved.update({"name": name, "engine": engine, **kwargs})
            return None

        open_profile_dialog(page, None, on_save, initial_profile)
        try:
            controls = list(walk(page.dialog))
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
            for line in lines:
                paste_field.value = line
                paste_btn.on_click(None)
            page.dialog.actions[-1].content.on_click(None)
            return saved
        finally:
            if page.dialog and page.dialog.on_dismiss:
                page.dialog.on_dismiss(None)

    # 1. Create A -> B where B supplies its own recovery email + note
    saved_ab_rec = run_paste_and_save(
        None,
        [
            "acctA@example.test | PassA | JBSW Y3DP EHPK 3PXP | recA@example.test noteA",
            "acctB@example.test | PassB | JBSW Y3DP EHPK 3PXP | recB@example.test noteB",
        ],
    )
    assert saved_ab_rec["account_uid"] == "acctB@example.test"
    assert saved_ab_rec["account_password"] == "PassB"
    assert saved_ab_rec["recovery_email"] == "recB@example.test"
    assert saved_ab_rec["notes"] == "noteB"

    # 2. Create A -> B where B has no recovery email (A's recovery must be cleared)
    saved_ab_no_rec = run_paste_and_save(
        None,
        [
            "acctA@example.test | PassA | JBSW Y3DP EHPK 3PXP | recA@example.test noteA",
            "acctB@example.test | PassB | JBSW Y3DP EHPK 3PXP | only note",
        ],
    )
    assert saved_ab_no_rec["account_uid"] == "acctB@example.test"
    assert saved_ab_no_rec["recovery_email"] == ""
    assert saved_ab_no_rec["notes"] == "only note"

    # 3. Edit existing A -> paste B without recovery: keep Edit display name, clear A's recovery
    existing_a = Profile(
        "Pinned Display Name",
        os_type="macos",
        account_uid="acctA@example.test",
        account_password="OldPass",
        two_fa_secret="JBSWY3DPEHPK3PXP",
        recovery_email="recA@example.test",
        notes="Old note",
    )
    saved_edit_b = run_paste_and_save(
        existing_a,
        ["acctB@example.test | NewPassB | JBSW Y3DP EHPK 3PXP"],
    )
    assert saved_edit_b["name"] == "Pinned Display Name"
    assert saved_edit_b["account_uid"] == "acctB@example.test"
    assert saved_edit_b["account_password"] == "NewPassB"
    assert saved_edit_b["recovery_email"] == ""
    assert saved_edit_b["notes"] == ""

    # 4. Edit existing A -> re-paste same UID (case-insensitive) without recovery: preserve manual recovery
    saved_same_uid = run_paste_and_save(
        existing_a,
        ["ACCTA@example.test | RotatedPass | JBSW Y3DP EHPK 3PXP | refreshed"],
    )
    assert saved_same_uid["name"] == "Pinned Display Name"
    assert saved_same_uid["account_uid"] == "ACCTA@example.test"
    assert saved_same_uid["account_password"] == "RotatedPass"
    assert saved_same_uid["recovery_email"] == "recA@example.test"
    assert saved_same_uid["notes"] == "refreshed"

    # 5. Edit existing A -> paste B with legacy recovery URL note (Engram #6995):
    # clear A's recovery address, leave 2FA empty, preserve URL in notes.
    saved_url_note = run_paste_and_save(
        existing_a,
        ["acctB@example.test|PassURL|https://example.test/recovery"],
    )
    assert saved_url_note["name"] == "Pinned Display Name"
    assert saved_url_note["account_uid"] == "acctB@example.test"
    assert saved_url_note["account_password"] == "PassURL"
    assert saved_url_note["two_fa_secret"] == ""
    assert saved_url_note["recovery_email"] == ""
    assert saved_url_note["notes"] == "https://example.test/recovery"


def test_quick_paste_r2_account_boundary_persists_recovery_and_backup_codes(tmp_path, monkeypatch):
    """Persisted ProfileManager checks for Codex §9 R2.1 (UID edits/clears/blank-UID Parse) and R2.2 (backup codes)."""
    import flet as ft

    from src.core import config as app_config
    from src.core.strings import get_string
    from src.services.profile import manager as pm_mod
    from src.ui.dialogs.profile import open_profile_dialog

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))

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

    def open_fields(page, pm, profile_name=None):
        captured = {}
        profile = pm.profiles[profile_name] if profile_name else None

        def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
            captured.update({"name": name, **kwargs})
            if profile is None:
                ok = pm.add_profile(
                    name,
                    proxy,
                    os_type,
                    timezone=timezone,
                    locale=locale,
                    engine=engine,
                    **kwargs,
                )
            else:
                ok = pm.update_profile(
                    profile.name,
                    name,
                    proxy,
                    os_type,
                    new_timezone=timezone,
                    new_locale=locale,
                    **kwargs,
                )
            return None if ok else "save failed"

        open_profile_dialog(page, None, on_save, profile)
        controls = list(walk(page.dialog))
        fields = [c for c in controls if isinstance(c, ft.TextField)]
        refs = {
            "paste": next(c for c in fields if c.hint_text == "uid|password|2FA_SECRET|note"),
            "paste_btn": next(
                c
                for c in controls
                if isinstance(c, ft.OutlinedButton)
                and c.on_click
                and c.on_click.__name__ == "_on_quick_paste"
            ),
            "uid": next(c for c in fields if c.hint_text == "user@gmail.com / 100089..."),
            "rec": next(c for c in fields if c.hint_text == get_string("recovery_email_hint")),
            "backup": next(
                c for c in fields if getattr(c, "key", None) == "dialog_backup_codes_field"
            ),
            "save_btn": page.dialog.actions[-1].content,
            "captured": captured,
        }
        return refs

    def seed_profile(pm, name, *, backup_codes="1111 0001\n1111 0002"):
        assert pm.add_profile(
            name,
            "",
            "macos",
            account_uid="acctA@example.test",
            account_password="OldPassA",
            two_fa_secret="JBSWY3DPEHPK3PXP",
            recovery_email="recA@example.test",
            backup_codes=backup_codes,
            notes="Old note",
        )

    pm = ProfileManager()
    seed_profile(pm, "EditClearUID")
    seed_profile(pm, "EditTypeB")
    seed_profile(pm, "EditBlankParse")
    seed_profile(pm, "EditDirectClearCodes")
    seed_profile(pm, "EditDirectNewCodes")
    seed_profile(pm, "EditSameUID")
    seed_profile(pm, "EditConcurrentCodes", backup_codes="")

    # R2.1a: Edit A -> clear UID textbox -> Quick Paste B -> Save & reload
    p = DummyPage()
    r = open_fields(p, pm, "EditClearUID")
    try:
        r["uid"].value = ""
        r["paste"].value = "acctB@example.test|PassB"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["EditClearUID"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.recovery_email is None
    assert reloaded.backup_codes is None

    # R2.1b: Edit A -> manually type B into UID textbox -> Quick Paste B -> Save & reload
    p = DummyPage()
    r = open_fields(p, pm, "EditTypeB")
    try:
        r["uid"].value = "acctB@example.test"
        r["paste"].value = "acctB@example.test|PassB"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["EditTypeB"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.recovery_email is None
    assert reloaded.backup_codes is None

    # R2.1c: Edit A -> blank-UID Quick Paste `|temp-pass` -> Quick Paste B -> Save & reload
    p = DummyPage()
    r = open_fields(p, pm, "EditBlankParse")
    try:
        r["paste"].value = "|temp-pass"
        r["paste_btn"].on_click(None)
        r["paste"].value = "acctB@example.test|PassB"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["EditBlankParse"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.recovery_email is None
    assert reloaded.backup_codes is None

    # R2.1d: Fresh Create with deliberately entered recovery -> Quick Paste A (no recovery) keeps it
    p = DummyPage()
    r = open_fields(p, pm, None)
    try:
        r["rec"].value = "deliberate@example.test"
        r["paste"].value = "freshA@example.test|FreshPass"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["freshA"]
    assert reloaded.account_uid == "freshA@example.test"
    assert reloaded.recovery_email == "deliberate@example.test"

    # R2.2a: Direct Edit A -> Quick Paste B (no new backup codes) clears A's stored backup codes
    p = DummyPage()
    r = open_fields(p, pm, "EditDirectClearCodes")
    try:
        r["paste"].value = "acctB@example.test|PassB|JBSWY3DPEHPK3PXP"
        r["paste_btn"].on_click(None)
        assert r["backup"].value == ""
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["EditDirectClearCodes"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.recovery_email is None
    assert reloaded.backup_codes is None

    # R2.2b: Direct Edit A -> Quick Paste B -> operator supplies B's replacement backup codes
    p = DummyPage()
    r = open_fields(p, pm, "EditDirectNewCodes")
    try:
        r["paste"].value = "acctB@example.test|PassB|JBSWY3DPEHPK3PXP"
        r["paste_btn"].on_click(None)
        assert r["backup"].value == ""
        r["backup"].value = "2222 0001\n2222 0002"
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    reloaded = ProfileManager().profiles["EditDirectNewCodes"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.backup_codes == "2222 0001\n2222 0002"

    # R2.2c: Same-UID Quick Paste keeps recovery & omits unchanged backup_codes (concurrent safety)
    p = DummyPage()
    r = open_fields(p, pm, "EditSameUID")
    try:
        r["paste"].value = "ACCTA@example.test|RotatedPass|JBSWY3DPEHPK3PXP"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    assert "backup_codes" not in r["captured"]
    reloaded = ProfileManager().profiles["EditSameUID"]
    assert reloaded.recovery_email == "recA@example.test"
    assert reloaded.backup_codes == "1111 0001\n1111 0002"

    # R2.2d: Concurrent inline backup codes written for A while Edit dialog is open ->
    # Quick Paste A->B still sends an explicit clear so B never inherits A's concurrent codes.
    p = DummyPage()
    r = open_fields(p, pm, "EditConcurrentCodes")
    try:
        assert pm.update_profile(
            "EditConcurrentCodes",
            "EditConcurrentCodes",
            "",
            "macos",
            backup_codes="9999 0001\n9999 0002",
        )
        r["paste"].value = "acctB@example.test|PassB"
        r["paste_btn"].on_click(None)
        r["save_btn"].on_click(None)
    finally:
        p.dialog.on_dismiss(None)
    assert r["captured"].get("backup_codes") == ""
    reloaded = ProfileManager().profiles["EditConcurrentCodes"]
    assert reloaded.account_uid == "acctB@example.test"
    assert reloaded.backup_codes is None


def test_quick_paste_r3_final_uid_guard_revert_and_literal_recovery_reopen(tmp_path, monkeypatch):
    """Real Flet 1.1.2 Session Save->reload->reopen tests for Codex §11 R3.1, R3.2, and R3.3."""
    import asyncio

    import flet as ft
    import msgpack
    from flet.controls.context import _context_page
    from flet.messaging.connection import Connection
    from flet.messaging.protocol import configure_encode_object_for_msgpack
    from flet.messaging.session import Session
    from flet.pubsub.pubsub_hub import PubSubHub

    from src.core import config as app_config
    from src.core.strings import get_string
    from src.models.profile import split_recovery_and_notes
    from src.services.profile import manager as pm_mod
    from src.ui.dialogs.profile import open_profile_dialog

    # R3.3 parser unit checks: whole literal emails beat legacy label prefix stripping.
    assert split_recovery_and_notes("", "recovery-b@example.test") == (
        "recovery-b@example.test",
        "",
    )
    assert split_recovery_and_notes("", "recovery.ops@example.test") == (
        "recovery.ops@example.test",
        "",
    )
    assert split_recovery_and_notes("", "Recovery: alt-b@example.test") == (
        "alt-b@example.test",
        "",
    )
    assert split_recovery_and_notes("", "Recovery:alt-b@example.test") == (
        "alt-b@example.test",
        "",
    )
    assert split_recovery_and_notes("", "Mail Recovery: recovery-b@example.test") == (
        "recovery-b@example.test",
        "",
    )
    assert split_recovery_and_notes("", "https://example.test/recovery") == (
        "",
        "https://example.test/recovery",
    )

    monkeypatch.setattr(pm_mod, "PROFILES_FILE", str(tmp_path / "profiles.json"))
    monkeypatch.setattr(pm_mod, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(app_config, "EXTENSIONS_FILE", str(tmp_path / "extensions.json"))
    monkeypatch.setattr(app_config, "EXTENSIONS_DIR", str(tmp_path / "extensions"))
    enc = configure_encode_object_for_msgpack(ft.BaseControl)

    def walk(ctrl):
        yield ctrl
        for attr in ("content", "controls", "actions", "title"):
            children = getattr(ctrl, attr, None)
            for child in children if isinstance(children, list) else [children]:
                if isinstance(child, ft.Control):
                    yield from walk(child)

    class CaptureConnection(Connection):
        def __init__(self, loop):
            super().__init__()
            self.loop = loop
            self.pubsubhub = PubSubHub(loop=loop)
            self.sent = []

        def send_message(self, message):
            packed = msgpack.packb(message.body.patch, default=enc)
            self.sent.append(msgpack.unpackb(packed, strict_map_key=False))

    def seed(pm, name, *, backup_codes="1111 0001\n1111 0002"):
        assert pm.add_profile(
            name,
            "",
            "macos",
            account_uid="acctA@example.test",
            account_password="OldPassA",
            two_fa_secret="JBSWY3DPEHPK3PXP",
            recovery_email="recA@example.test",
            backup_codes=backup_codes,
            notes="Old note",
        )

    async def open_dialog_refs(session, pm, profile_name):
        page = session.page
        captured = {}
        profile = pm.profiles[profile_name]

        def on_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
            captured.clear()
            captured.update({"name": name, **kwargs})
            ok = pm.update_profile(
                profile.name,
                name,
                proxy,
                os_type,
                new_timezone=timezone,
                new_locale=locale,
                **kwargs,
            )
            return None if ok else "save failed"

        open_profile_dialog(page, None, on_save, profile)
        dialog = page._dialogs.controls[-1]
        page.update()
        controls = list(walk(dialog))
        fields = [c for c in controls if isinstance(c, ft.TextField)]
        return {
            "dialog": dialog,
            "paste": next(c for c in fields if c.hint_text == "uid|password|2FA_SECRET|note"),
            "paste_btn": next(
                c
                for c in controls
                if isinstance(c, ft.OutlinedButton)
                and c.on_click
                and c.on_click.__name__ == "_on_quick_paste"
            ),
            "uid": next(c for c in fields if c.hint_text == "user@gmail.com / 100089..."),
            "pass": next(c for c in fields if c.password is True),
            "two_fa": next(c for c in fields if c.hint_text == "JBSWY3DPEHPK3PXP"),
            "rec": next(c for c in fields if c.hint_text == get_string("recovery_email_hint")),
            "backup": next(
                c for c in fields if getattr(c, "key", None) == "dialog_backup_codes_field"
            ),
            "notes": next(c for c in fields if c.hint_text == get_string("notes_hint")),
            "account_error": next(
                c for c in controls if getattr(c, "key", None) == "account_cred_error"
            ),
            "save_btn": dialog.actions[-1].content,
            "captured": captured,
        }

    async def reload_and_reopen_values(session, profile_name):
        fresh_pm = pm_mod.ProfileManager()
        reloaded = fresh_pm.profiles[profile_name]
        reopened = await open_dialog_refs(session, fresh_pm, profile_name)
        try:
            return reloaded, {
                "uid": reopened["uid"].value,
                "pass": reopened["pass"].value,
                "two_fa": reopened["two_fa"].value,
                "rec": reopened["rec"].value,
                "backup": reopened["backup"].value,
                "notes": reopened["notes"].value,
            }
        finally:
            await session.dispatch_event(reopened["dialog"]._i, "dismiss", None)

    async def scenario():
        conn = CaptureConnection(asyncio.get_running_loop())
        session = Session(conn)
        page = session.page
        msgpack.packb(session.get_page_patch(), default=enc)
        token = _context_page.set(page)
        try:
            pm = pm_mod.ProfileManager()
            seed(pm, "R31_PostParseEdit")
            seed(pm, "R31_OnChangeNoReparse")
            seed(pm, "R31_IntentionalC")
            seed(pm, "R31_BlankEditPasteB")
            seed(pm, "R32_RevertEmptyConcurrent", backup_codes="")
            seed(pm, "R32_RevertPopulatedConcurrent", backup_codes="1111 0001\n1111 0002")
            seed(pm, "R32_ExplicitClearThenRevert", backup_codes="1111 0001\n1111 0002")
            seed(pm, "R32_ActualReplaceConcurrent", backup_codes="")
            seed(pm, "R33_BareRecoveryLiteral")
            seed(pm, "R33_LabeledRecovery")
            seed(pm, "R33_LegacyUrlNote")

            # R3.1 Route 1a: Paste B + B's backup codes + Parse B again -> manually change UID to C ->
            # Save MUST block (no leak on disk); clearing mismatched fields allows Save & clean reopen.
            r = await open_dialog_refs(session, pm, "R31_PostParseEdit")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "acctB@example.test|PassB|recovery-b@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == "recovery-b@example.test"
                assert r["backup"].value == ""

                session.apply_patch(r["backup"]._i, {"value": "2222 0001\n2222 0002"})
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "acctB@example.test|PassB|recovery-b@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == "2222 0001\n2222 0002"

                session.apply_patch(r["uid"]._i, {"value": "acctC@example.test"})
                session.apply_patch(r["pass"]._i, {"value": "PassC"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is True
                assert "acctB@example.test" in r["account_error"].value
                assert r["captured"] == {}

                # Second blind Save click still blocks (no double-click bypass).
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is True
                assert r["captured"] == {}
                assert pm_mod.ProfileManager().profiles["R31_PostParseEdit"].account_uid == (
                    "acctA@example.test"
                )

                # Clear mismatched fields -> Save succeeds, disk & reopened dialog match clean C.
                session.apply_patch(r["rec"]._i, {"value": ""})
                session.apply_patch(r["backup"]._i, {"value": ""})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is False
                assert r["captured"]["account_uid"] == "acctC@example.test"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R31_PostParseEdit")
            assert (
                reloaded.account_uid == "acctC@example.test" and ui["uid"] == "acctC@example.test"
            )
            assert reloaded.recovery_email is None and ui["rec"] == ""
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R3.1 Route 1b: Real interactive `change` event binds B's backup codes without 2nd Parse.
            r = await open_dialog_refs(session, pm, "R31_OnChangeNoReparse")
            try:
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["backup"]._i, {"value": "2222 0001"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 0001")
                session.apply_patch(r["uid"]._i, {"value": "acctC@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is True
                assert r["captured"] == {}
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            assert pm_mod.ProfileManager().profiles["R31_OnChangeNoReparse"].account_uid == (
                "acctA@example.test"
            )

            # R3.1 Control: Intentionally supplying C's own recovery & backup codes after UID edit saves cleanly.
            r = await open_dialog_refs(session, pm, "R31_IntentionalC")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "acctB@example.test|PassB|recovery-b@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["uid"]._i, {"value": "acctC@example.test"})
                session.apply_patch(r["rec"]._i, {"value": "recC@example.test"})
                session.apply_patch(r["backup"]._i, {"value": "3333 0001\n3333 0002"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is False
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R31_IntentionalC")
            assert (
                reloaded.account_uid == "acctC@example.test" and ui["uid"] == "acctC@example.test"
            )
            assert (
                reloaded.recovery_email == "recC@example.test" and ui["rec"] == "recC@example.test"
            )
            assert (
                reloaded.backup_codes == "3333 0001\n3333 0002"
                and ui["backup"] == "3333 0001\n3333 0002"
            )

            # R3.1 Route 2: Open Edit A -> blank UID -> edit recovery & backup -> Paste B (no recovery) ->
            # Save & reopen must NOT inherit edited A values as unbound Fresh Create.
            r = await open_dialog_refs(session, pm, "R31_BlankEditPasteB")
            try:
                session.apply_patch(r["uid"]._i, {"value": ""})
                session.apply_patch(r["rec"]._i, {"value": "recA-edited@example.test"})
                session.apply_patch(r["backup"]._i, {"value": "1111 0009"})
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == ""
                assert r["backup"].value == ""
                await session.dispatch_event(r["save_btn"]._i, "click", None)
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R31_BlankEditPasteB")
            assert (
                reloaded.account_uid == "acctB@example.test" and ui["uid"] == "acctB@example.test"
            )
            assert reloaded.recovery_email is None and ui["rec"] == ""
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R3.2a (Codex repro): A has empty codes -> Parse B -> concurrent sheet written for A ->
            # Parse A again -> Save without editing codes must preserve A's concurrent sheet.
            r = await open_dialog_refs(session, pm, "R32_RevertEmptyConcurrent")
            try:
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert pm.update_profile(
                    "R32_RevertEmptyConcurrent",
                    "R32_RevertEmptyConcurrent",
                    "",
                    "macos",
                    backup_codes="9999 0001\n9999 0002",
                )
                session.apply_patch(r["paste"]._i, {"value": "acctA@example.test|PassA2"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert "backup_codes" not in r["captured"]
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R32_RevertEmptyConcurrent")
            assert (
                reloaded.account_uid == "acctA@example.test" and ui["uid"] == "acctA@example.test"
            )
            assert (
                reloaded.backup_codes == "9999 0001\n9999 0002"
                and ui["backup"] == "9999 0001\n9999 0002"
            )

            # R3.2b: A originally had codes -> Parse B auto-clears -> concurrent sheet written for A ->
            # Parse A restores A's snapshot -> Save omits backup_codes & preserves concurrent sheet.
            r = await open_dialog_refs(session, pm, "R32_RevertPopulatedConcurrent")
            try:
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                assert pm.update_profile(
                    "R32_RevertPopulatedConcurrent",
                    "R32_RevertPopulatedConcurrent",
                    "",
                    "macos",
                    backup_codes="8888 0001\n8888 0002",
                )
                session.apply_patch(r["paste"]._i, {"value": "acctA@example.test|PassA2"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == "1111 0001\n1111 0002"
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert "backup_codes" not in r["captured"]
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R32_RevertPopulatedConcurrent")
            assert (
                reloaded.backup_codes == "8888 0001\n8888 0002"
                and ui["backup"] == "8888 0001\n8888 0002"
            )

            # R3.2c: Explicit user clear of A's codes before B detour is respected on revert to A.
            r = await open_dialog_refs(session, pm, "R32_ExplicitClearThenRevert")
            try:
                session.apply_patch(r["backup"]._i, {"value": ""})
                await session.dispatch_event(r["backup"]._i, "change", "")
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["backup"]._i, {"value": "2222 0001"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 0001")
                session.apply_patch(r["paste"]._i, {"value": "acctA@example.test|PassA2"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == ""
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R32_ExplicitClearThenRevert")
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R3.2d Control: Actual A->B replacement still clears a concurrent A update on disk.
            r = await open_dialog_refs(session, pm, "R32_ActualReplaceConcurrent")
            try:
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert pm.update_profile(
                    "R32_ActualReplaceConcurrent",
                    "R32_ActualReplaceConcurrent",
                    "",
                    "macos",
                    backup_codes="7777 0001\n7777 0002",
                )
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == ""
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R32_ActualReplaceConcurrent")
            assert (
                reloaded.account_uid == "acctB@example.test" and ui["uid"] == "acctB@example.test"
            )
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R3.3a: Bare `recovery-b@example.test` must save & reopen intact (never `b@example.test`).
            r = await open_dialog_refs(session, pm, "R33_BareRecoveryLiteral")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "account-b@example.test|fixture-pass|recovery-b@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == "recovery-b@example.test"
                assert r["notes"].value == ""
                await session.dispatch_event(r["save_btn"]._i, "click", None)
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R33_BareRecoveryLiteral")
            assert reloaded.recovery_email == "recovery-b@example.test"
            assert ui["rec"] == "recovery-b@example.test" and ui["notes"] == ""

            # R3.3b Control: Labeled `Recovery: alt-b@example.test` still extracts `alt-b@example.test`.
            r = await open_dialog_refs(session, pm, "R33_LabeledRecovery")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "account-b@example.test|fixture-pass|Recovery: alt-b@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == "alt-b@example.test"
                await session.dispatch_event(r["save_btn"]._i, "click", None)
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R33_LabeledRecovery")
            assert (
                reloaded.recovery_email == "alt-b@example.test"
                and ui["rec"] == "alt-b@example.test"
            )

            # R3.3c Control (#6995): Recovery URL stays in notes with empty 2FA & recovery email.
            r = await open_dialog_refs(session, pm, "R33_LegacyUrlNote")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "account-b@example.test|fixture-pass|https://example.test/recovery"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["two_fa"].value == ""
                assert r["rec"].value == ""
                assert r["notes"].value == "https://example.test/recovery"
                await session.dispatch_event(r["save_btn"]._i, "click", None)
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R33_LegacyUrlNote")
            assert reloaded.two_fa_secret is None and ui["two_fa"] == ""
            assert reloaded.recovery_email is None and ui["rec"] == ""
            assert (
                reloaded.notes == "https://example.test/recovery"
                and ui["notes"] == "https://example.test/recovery"
            )

            # --- Codex §13 R4.1, R4.2, R4.3 Save->reload->reopen regressions ---
            seed(pm, "R41_BlankParseThenB")
            seed(pm, "R41_BlankParseManualB")
            seed(pm, "R42_EditedABackupDetour", backup_codes="1111 2101")
            seed(pm, "R42_EditedEmptyABackupDetour", backup_codes="")
            seed(pm, "R43_ManualUidPopulated", backup_codes="1111 2101")
            seed(pm, "R43_ManualUidEmpty", backup_codes="")

            # R4.1a (Codex repro): Edit A -> blank-UID Parse with recovery -> Parse B (no recovery) ->
            # Save & reopen must NOT file A's pasted recovery address under B.
            r = await open_dialog_refs(session, pm, "R41_BlankParseThenB")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "|fixture|alt-a-new@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == "alt-a-new@example.test"
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["rec"].value == ""
                assert r["backup"].value == ""
                await session.dispatch_event(r["save_btn"]._i, "click", None)
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R41_BlankParseThenB")
            assert (
                reloaded.account_uid == "acctB@example.test" and ui["uid"] == "acctB@example.test"
            )
            assert reloaded.recovery_email is None and ui["rec"] == ""
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R4.1b: Edit A -> blank-UID Parse with recovery -> manually set UID to B -> Save blocks.
            r = await open_dialog_refs(session, pm, "R41_BlankParseManualB")
            try:
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "|fixture|alt-a-new@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["uid"]._i, {"value": "acctB@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is True
                assert r["captured"] == {}
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            assert pm_mod.ProfileManager().profiles["R41_BlankParseManualB"].account_uid == (
                "acctA@example.test"
            )

            # R4.1c Control: Fresh Create blank-UID Parse with recovery -> Parse A preserves recovery.
            captured_create = {}

            def on_create_save(name, proxy, os_type, timezone, locale, engine, **kwargs):
                captured_create.update({"name": name, **kwargs})
                ok = pm.add_profile(
                    name,
                    proxy,
                    os_type,
                    timezone=timezone,
                    locale=locale,
                    engine=engine,
                    **kwargs,
                )
                return None if ok else "save failed"

            open_profile_dialog(page, None, on_create_save, None)
            create_dlg = page._dialogs.controls[-1]
            page.update()
            try:
                c_ctrls = list(walk(create_dlg))
                c_fields = [c for c in c_ctrls if isinstance(c, ft.TextField)]
                c_paste = next(c for c in c_fields if c.hint_text == "uid|password|2FA_SECRET|note")
                c_paste_btn = next(
                    c
                    for c in c_ctrls
                    if isinstance(c, ft.OutlinedButton)
                    and c.on_click
                    and c.on_click.__name__ == "_on_quick_paste"
                )
                c_save_btn = create_dlg.actions[-1].content
                session.apply_patch(c_paste._i, {"value": "|fixture|deliberate-fresh@example.test"})
                await session.dispatch_event(c_paste_btn._i, "click", None)
                session.apply_patch(c_paste._i, {"value": "freshR4@example.test|FreshPass"})
                await session.dispatch_event(c_paste_btn._i, "click", None)
                await session.dispatch_event(c_save_btn._i, "click", None)
            finally:
                await session.dispatch_event(create_dlg._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "freshR4")
            assert reloaded.account_uid == "freshR4@example.test" and ui["uid"] == (
                "freshR4@example.test"
            )
            assert reloaded.recovery_email == "deliberate-fresh@example.test"
            assert ui["rec"] == "deliberate-fresh@example.test"

            # R4.2a (Codex repro): Edit A (1111 2101) -> edit A's codes to 2222 2101 -> Parse B ->
            # Parse A -> Save & reopen must retain A's deliberate edit (2222 2101), not stale 1111 2101.
            r = await open_dialog_refs(session, pm, "R42_EditedABackupDetour")
            try:
                session.apply_patch(r["backup"]._i, {"value": "2222 2101"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 2101")
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                session.apply_patch(r["paste"]._i, {"value": "acctA@example.test|PassA2"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == "2222 2101"
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == "2222 2101"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R42_EditedABackupDetour")
            assert (
                reloaded.account_uid == "acctA@example.test" and ui["uid"] == "acctA@example.test"
            )
            assert reloaded.backup_codes == "2222 2101" and ui["backup"] == "2222 2101"

            # R4.2b: Empty-initial A edited to 2222 2101 -> Parse B -> enter B codes -> Parse A ->
            # Save & reopen must retain A's deliberate edit and drop B's codes.
            r = await open_dialog_refs(session, pm, "R42_EditedEmptyABackupDetour")
            try:
                session.apply_patch(r["backup"]._i, {"value": "2222 2101"})
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                session.apply_patch(r["backup"]._i, {"value": "3333 3101"})
                await session.dispatch_event(r["backup"]._i, "change", "3333 3101")
                session.apply_patch(r["paste"]._i, {"value": "acctA@example.test|PassA2"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == "2222 2101"
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == "2222 2101"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R42_EditedEmptyABackupDetour")
            assert reloaded.backup_codes == "2222 2101" and ui["backup"] == "2222 2101"

            # R4.3a (Codex repro): Manual-only UID edit on initially populated A without Parse must
            # omit unchanged backup_codes and preserve a concurrent inline backup update on disk.
            r = await open_dialog_refs(session, pm, "R43_ManualUidPopulated")
            try:
                assert pm.update_profile(
                    "R43_ManualUidPopulated",
                    "R43_ManualUidPopulated",
                    "",
                    "macos",
                    backup_codes="9999 2101",
                )
                session.apply_patch(r["uid"]._i, {"value": "acctA-renamed@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert "backup_codes" not in r["captured"]
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R43_ManualUidPopulated")
            assert reloaded.account_uid == "acctA-renamed@example.test"
            assert ui["uid"] == "acctA-renamed@example.test"
            assert reloaded.backup_codes == "9999 2101" and ui["backup"] == "9999 2101"

            # R4.3b (Codex repro): Manual-only UID edit on initially empty A without Parse must
            # omit unchanged backup_codes and preserve a concurrent inline backup update on disk.
            r = await open_dialog_refs(session, pm, "R43_ManualUidEmpty")
            try:
                assert pm.update_profile(
                    "R43_ManualUidEmpty",
                    "R43_ManualUidEmpty",
                    "",
                    "macos",
                    backup_codes="9999 2102",
                )
                session.apply_patch(r["uid"]._i, {"value": "acctA-renamed-empty@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert "backup_codes" not in r["captured"]
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R43_ManualUidEmpty")
            assert reloaded.account_uid == "acctA-renamed-empty@example.test"
            assert ui["uid"] == "acctA-renamed-empty@example.test"
            assert reloaded.backup_codes == "9999 2102" and ui["backup"] == "9999 2102"

            # --- Codex §15 R5.1 Save->reload->reopen regressions (manual UID return to A) ---
            seed(pm, "R51_ManualReturnPopulated", backup_codes="1111 2101")
            seed(pm, "R51_ManualReturnEmpty", backup_codes="")
            seed(pm, "R51_ManualReturnConcurrent", backup_codes="1111 2101")
            seed(pm, "R51_ManualReturnExplicitClear", backup_codes="1111 2101")
            seed(pm, "R51_ManualReturnRetryAfterRecGuard", backup_codes="1111 2101")
            seed(pm, "R51_ManualReturnOversized", backup_codes="1111 2101")

            # R5.1a (Codex repro — populated A): Edit A -> replace codes with 2222 2101 ->
            # Parse B -> manually type A's UID/password without Parse -> Save persists 2222 2101.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnPopulated")
            try:
                session.apply_patch(r["backup"]._i, {"value": "2222 2101"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 2101")
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                session.apply_patch(r["pass"]._i, {"value": "OldPassA"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == "2222 2101"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R51_ManualReturnPopulated")
            assert (
                reloaded.account_uid == "acctA@example.test" and ui["uid"] == "acctA@example.test"
            )
            assert reloaded.backup_codes == "2222 2101" and ui["backup"] == "2222 2101"

            # R5.1b (Codex repro — empty A): Edit A (no sheet) -> enter 2222 2101 -> Parse B ->
            # manually type A's UID/password without Parse -> Save persists 2222 2101.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnEmpty")
            try:
                session.apply_patch(r["backup"]._i, {"value": "2222 2101"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 2101")
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert r["backup"].value == ""
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                session.apply_patch(r["pass"]._i, {"value": "OldPassA"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == "2222 2101"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R51_ManualReturnEmpty")
            assert (
                reloaded.account_uid == "acctA@example.test" and ui["uid"] == "acctA@example.test"
            )
            assert reloaded.backup_codes == "2222 2101" and ui["backup"] == "2222 2101"

            # R5.1c Control: Untouched A + Parse B + concurrent inline update + manual return to A
            # must omit backup_codes and preserve the newer concurrent sheet on disk.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnConcurrent")
            try:
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                assert pm.update_profile(
                    "R51_ManualReturnConcurrent",
                    "R51_ManualReturnConcurrent",
                    "",
                    "macos",
                    backup_codes="9999 5101",
                )
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert "backup_codes" not in r["captured"]
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R51_ManualReturnConcurrent")
            assert reloaded.backup_codes == "9999 5101" and ui["backup"] == "9999 5101"

            # R5.1d Control: Explicit clear of A's codes before Parse B + manual return to A
            # must not reintroduce old codes.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnExplicitClear")
            try:
                session.apply_patch(r["backup"]._i, {"value": ""})
                await session.dispatch_event(r["backup"]._i, "change", "")
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"].get("backup_codes") == ""
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R51_ManualReturnExplicitClear")
            assert reloaded.backup_codes is None and ui["backup"] == ""

            # R5.1e Edge: Manual return blocked once by B's recovery email must retain stashed
            # deliberate A backup edit when the user clears B's recovery email and retries Save.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnRetryAfterRecGuard")
            try:
                session.apply_patch(r["backup"]._i, {"value": "2222 2101"})
                await session.dispatch_event(r["backup"]._i, "change", "2222 2101")
                session.apply_patch(
                    r["paste"]._i,
                    {"value": "acctB@example.test|PassB|recB@example.test"},
                )
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is True
                assert r["captured"] == {}

                session.apply_patch(r["rec"]._i, {"value": ""})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["account_error"].visible is False
                assert r["captured"].get("backup_codes") == "2222 2101"
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(
                session, "R51_ManualReturnRetryAfterRecGuard"
            )
            assert reloaded.recovery_email is None and ui["rec"] == ""
            assert reloaded.backup_codes == "2222 2101" and ui["backup"] == "2222 2101"

            # R5.1f Validation: Stashed deliberate edit exceeding MAX_BACKUP_CODES_LENGTH must
            # still fail normalize_backup_codes validation on Save without touching disk.
            r = await open_dialog_refs(session, pm, "R51_ManualReturnOversized")
            try:
                oversized = "1111 2101\n" * 500
                session.apply_patch(r["backup"]._i, {"value": oversized})
                await session.dispatch_event(r["backup"]._i, "change", oversized)
                session.apply_patch(r["paste"]._i, {"value": "acctB@example.test|PassB"})
                await session.dispatch_event(r["paste_btn"]._i, "click", None)
                session.apply_patch(r["uid"]._i, {"value": "acctA@example.test"})
                await session.dispatch_event(r["save_btn"]._i, "click", None)
                assert r["captured"] == {}
            finally:
                await session.dispatch_event(r["dialog"]._i, "dismiss", None)
            reloaded, ui = await reload_and_reopen_values(session, "R51_ManualReturnOversized")
            assert reloaded.backup_codes == "1111 2101" and ui["backup"] == "1111 2101"
        finally:
            _context_page.reset(token)

    asyncio.run(scenario())
