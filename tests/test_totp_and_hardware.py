from src.services.browser import fingerprint
from src.services.profile.manager import ProfileManager
from src.utils.totp import clean_totp_secret, generate_totp, parse_account_line


def test_clean_and_generate_totp():
    assert clean_totp_secret("JBSW Y3DP EHPK 3PXP") == "JBSWY3DPEHPK3PXP"
    assert clean_totp_secret("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP&issuer=Google") == "JBSWY3DPEHPK3PXP"
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
            gpu_pair=gpus[-1],  # On macos, gpus[-1] is Intel/AMD; ensures notched-panel auto-resolution works
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

    # F3: Chrome engine disables the 5 Camoufox hardware/WebRTC/Canvas dropdowns and Roll button
    chrome_page = DummyPage()
    open_profile_dialog(
        chrome_page,
        None,
        on_save,
        Profile("chrome-fixture", engine="chrome", os_type="macos"),
    )
    chrome_controls = list(walk(chrome_page.dialog))
    hw_dropdowns = [c for c in chrome_controls if isinstance(c, ft.Dropdown)][3:]
    assert len(hw_dropdowns) == 5
    assert all(c.disabled for c in hw_dropdowns)
    chrome_page.dialog.on_dismiss(None)
