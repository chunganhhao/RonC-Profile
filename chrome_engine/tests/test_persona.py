import json

import pytest

from chrome_engine import persona as personas
from chrome_engine.persona import Persona, PersonaError


def test_generate_is_deterministic_per_seed_and_platform():
    a = personas.generate("windows", seed=12345)
    b = personas.generate("windows", seed=12345)
    assert a == b
    assert personas.generate("macos", seed=12345).platform == "macos"


def test_generate_varies_with_seed():
    values = {
        (p.platform_version, p.hardware_concurrency)
        for p in (personas.generate("windows", seed=s) for s in range(1, 200))
    }
    assert len(values) > 5


@pytest.mark.parametrize("platform", personas.PLATFORMS)
def test_generated_values_are_valid(platform):
    for seed in range(1, 100):
        p = personas.generate(platform, seed=seed)
        personas.validate(p)
        assert 1 <= p.seed <= personas.SEED_MAX


def test_random_seed_when_none():
    personas_list = [personas.generate("linux") for _ in range(40)]
    seeds = {p.seed for p in personas_list}
    assert len(seeds) > 25
    # Upstream #100: every auto-generated seed must be a multiple of 3 (deviceMemory == 8 GB)
    assert all(p.seed % 3 == 0 and p.has_valid_device_memory_seed for p in personas_list)


def test_align_seed_for_device_memory():
    for raw in (1, 2, 4, 5, 7, 1337, personas.SEED_MAX):
        aligned = personas.align_seed_for_device_memory(raw)
        assert 1 <= aligned <= personas.SEED_MAX
        assert aligned % 3 == 0
        assert personas.seed_gives_valid_device_memory(aligned)
    # Consecutive unaligned seeds (e.g. 5000, 5001, 5002) remain distinct and divisible by 3
    mapped = [
        Persona(seed=s, platform="windows", platform_version="15.0.0").to_args()[0]
        for s in (5000, 5001, 5002)
    ]
    assert len(set(mapped)) == 3
    assert all(int(arg.split("=")[1]) % 3 == 0 for arg in mapped)
    unaligned = Persona(seed=1337, platform="windows", platform_version="15.0.0")
    assert not unaligned.has_valid_device_memory_seed
    fixed = unaligned.with_aligned_seed()
    assert fixed.has_valid_device_memory_seed
    assert fixed.seed == 4011


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"seed": 0}, "seed"),
        ({"seed": 2**31}, "seed"),
        ({"platform": "android"}, "platform"),
        ({"platform_version": "15"}, "platform_version"),
        ({"brand": "Firefox"}, "brand"),
        ({"brand": "Edge", "platform": "linux", "platform_version": "6.8.0"}, "Edge"),
        ({"hardware_concurrency": 0}, "hardware_concurrency"),
        ({"hardware_concurrency": 128}, "hardware_concurrency"),
        ({"timezone": "Mars/Olympus"}, "timezone"),
        ({"locale": "english"}, "locale"),
        ({"locale": "zh-hans-cn"}, "locale"),
        ({"locale": "de_DE"}, "locale"),
        ({"locale": "es-41"}, "locale"),
        ({"locale": "not_a_locale!"}, "locale"),
        ({"locale": "../en"}, "locale"),
    ],
)
def test_validation(changes, message):
    base = {"seed": 7, "platform": "windows", "platform_version": "15.0.0"}
    with pytest.raises(PersonaError, match=message):
        Persona(**{**base, **changes})


def test_to_args():
    p = Persona(
        seed=42,
        platform="macos",
        platform_version="15.6.1",
        brand="Edge",
        hardware_concurrency=10,
        timezone="Asia/Tokyo",
        locale="ja-JP",
    )
    assert p.to_args() == [
        "--fingerprint=42",
        "--fingerprint-platform=macos",
        "--fingerprint-platform-version=15.6.1",
        "--fingerprint-brand=Edge",
        "--fingerprint-hardware-concurrency=10",
        "--timezone=Asia/Tokyo",
        "--lang=ja-JP",
        "--accept-lang=ja-JP,ja,en-US,en",
    ]


def test_to_args_leaves_geo_to_the_ip():
    args = Persona(seed=1, platform="windows", platform_version="10.0.0").to_args()
    assert not any(a.startswith(("--timezone", "--lang", "--accept-lang")) for a in args)


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        ("vi-VN", "vi-VN,vi,en-US,en"),
        ("en-US", "en-US,en"),
        ("en-GB", "en-GB,en"),
        ("fr", "fr,en-US,en"),
        ("zh-Hans-CN", "zh-Hans-CN,zh,en-US,en"),
        ("zh-Hant-TW", "zh-Hant-TW,zh,en-US,en"),
        ("es-419", "es-419,es,en-US,en"),
        ("en-001", "en-001,en"),
        ("fil-PH", "fil-PH,fil,en-US,en"),
    ],
)
def test_accept_languages(locale, expected):
    p = Persona(seed=7, platform="macos", platform_version="15.6.1", locale=locale)
    assert p.locale == locale
    assert personas.accept_languages(locale) == expected


def test_with_geo_only_fills_open_fields():
    pinned = Persona(seed=1, platform="windows", platform_version="10.0.0", timezone="Europe/Paris")
    filled = pinned.with_geo("Asia/Tokyo", "ja-JP")
    assert filled.timezone == "Europe/Paris"
    assert filled.locale == "ja-JP"


def test_json_roundtrip_ignores_unknown_keys():
    p = personas.generate("windows", seed=99, locale="vi-VN")
    data = {**p.to_json(), "future_field": 1}
    assert Persona.from_json(data) == p


def test_load_or_create_persists(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    assert personas.persona_path(tmp_path).is_file()
    assert personas.load_or_create(tmp_path, "windows") == first


def test_load_or_create_regenerates_when_os_changes(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    second = personas.load_or_create(tmp_path, "macos")
    assert second.platform == "macos"
    assert personas.load(tmp_path) == second
    assert second != first


def test_overrides_keep_the_device(tmp_path):
    first = personas.load_or_create(tmp_path, "windows")
    pinned = personas.load_or_create(tmp_path, "windows", timezone="Asia/Tokyo", locale="ja-JP")
    assert (pinned.seed, pinned.platform_version) == (first.seed, first.platform_version)
    assert (pinned.timezone, pinned.locale) == ("Asia/Tokyo", "ja-JP")
    # None leaves the saved value, "" clears it.
    kept = personas.load_or_create(tmp_path, "windows", timezone=None)
    assert kept.timezone == "Asia/Tokyo"
    cleared = personas.load_or_create(tmp_path, "windows", timezone="", locale="")
    assert (cleared.timezone, cleared.locale) == (None, None)
    assert cleared.seed == first.seed


def test_corrupt_file_is_regenerated(tmp_path):
    personas.persona_path(tmp_path).write_text("{not json", encoding="utf-8")
    p = personas.load_or_create(tmp_path, "linux")
    assert json.loads(personas.persona_path(tmp_path).read_text(encoding="utf-8"))["seed"] == p.seed


def test_invalid_saved_values_are_regenerated(tmp_path):
    personas.persona_path(tmp_path).write_text(
        json.dumps({"seed": -1, "platform": "windows"}), "utf-8"
    )
    assert personas.load(tmp_path) is None
    assert personas.load_or_create(tmp_path, "windows").seed > 0


@pytest.mark.parametrize(
    ("host", "platform", "level"),
    [
        ("windows", "windows", personas.OK),
        ("windows", "macos", personas.WARN),
        ("windows", "linux", personas.UNSUPPORTED),
        ("linux", "windows", personas.WARN),
        ("macos", "macos", personas.OK),
        ("macos", "windows", personas.WARN),
        ("macos", "linux", personas.UNSUPPORTED),
    ],
)
def test_compatibility(host, platform, level):
    assert personas.compatibility(host, platform)[0] == level


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"screen_width": 1440}, "screen_width"),
        ({"screen_width": 200, "screen_height": 900}, "screen_width"),
        (
            {"screen_width": 1440, "screen_height": 900, "avail_top": 38, "avail_height": 880},
            "exceed",
        ),
        ({"color_depth": 16}, "color_depth"),
        ({"color_gamut": "rec2020"}, "color_gamut"),
        ({"color_scheme": "auto"}, "color_scheme"),
        ({"device_scale_factor": 8.0}, "device_scale_factor"),
        ({"device_model": "   "}, "device_model"),
    ],
)
def test_display_validation(changes, message):
    base = {"seed": 3, "platform": "macos", "platform_version": "15.6.1"}
    with pytest.raises(PersonaError, match=message):
        Persona(**{**base, **changes})


def test_macos_personas_cover_all_apple_silicon_chips_and_enforce_coherence():
    chips = set()
    models = set()
    schemes = set()
    for i in range(13 * 4):
        seed = 3 * i + 3
        p = personas.generate("macos", seed=seed)
        slot = personas.apple_silicon_slot_for_seed(seed)
        chips.add(slot.chip)
        models.add(p.device_model)
        schemes.add(p.color_scheme)
        assert p.hardware_concurrency in {c for c, _ in slot.cores}
        assert p.platform_version in {v for v, _ in slot.os_versions}
        if slot.chip in ("Apple M4", "Apple M4 Pro", "Apple M4 Max"):
            assert int(p.platform_version.split(".")[0]) >= 15
        assert p.screen_width is not None and p.screen_height is not None
        assert p.avail_top is not None and p.avail_height is not None
        assert p.avail_top + p.avail_height <= p.screen_height
        args = p.to_args()
        assert f"--fingerprint-screen-width={p.screen_width}" in args
        assert f"--fingerprint-screen-height={p.screen_height}" in args
        assert f"--fingerprint-device-scale-factor={p.device_scale_factor:g}" in args
        assert any(a.startswith("--fingerprint-display-profile=") for a in args)
        assert personas.Persona.from_json(p.to_json()) == p

    assert len(chips) == 12
    assert len(models) > 10
    assert schemes == {"light", "dark"}


def test_macos_generated_model_cpu_constraints_and_defaults():
    p45 = personas.generate("macos", seed=45)
    assert p45.device_model == "MacBook Pro 16 (M3 Pro)"
    assert p45.hardware_concurrency == 12

    p537 = personas.generate("macos", seed=537)
    assert p537.device_model == "MacBook Pro 16 (M1 Pro)"
    assert p537.hardware_concurrency == 10

    model_allowed: dict[str, set[int]] = {}
    for slot in personas._APPLE_SILICON_SLOTS:
        for disp in slot.displays:
            allowed = {c for c, _ in personas.allowed_macos_cores_for_model(slot, disp.model)}
            model_allowed[disp.model] = allowed

    assert model_allowed["MacBook Pro 16 (M3 Pro)"] == {12}
    assert model_allowed["MacBook Pro 16 (M1 Pro)"] == {10}
    assert model_allowed["MacBook Pro 16 (M2 Pro)"] == {12}
    assert model_allowed["MacBook Pro 16 (M4 Pro)"] == {14}
    assert model_allowed["MacBook Pro 14 (M4)"] == {10}
    assert model_allowed["Mac mini (M4) 1080p"] == {10}

    seen_models = set()
    for i in range(600):
        seed = 3 * i + 3
        p = personas.generate("macos", seed=seed)
        seen_models.add(p.device_model)
        assert p.hardware_concurrency in model_allowed[p.device_model]
        assert (
            personas.default_cpu_for_persona("macos", seed, p.device_model)
            == p.hardware_concurrency
        )
    assert seen_models == set(model_allowed)

    baseline = personas.generate("macos", seed=3, coherence_026=False)
    assert baseline.device_model is None
    assert baseline.screen_width is None
    assert not baseline.requires_apple_silicon_026
    assert not any(a.startswith("--fingerprint-display-profile") for a in baseline.to_args())


def test_legacy_mac_cpu_matches_its_apple_silicon_chip():
    for seed in range(3, 3 * 600, 3):
        p = personas.generate("macos", seed=seed, coherence_026=False)
        slot = personas.apple_silicon_slot_for_seed(seed)
        assert p.hardware_concurrency in {cores for cores, _ in slot.cores}
    assert personas.generate("macos", seed=495, coherence_026=False).hardware_concurrency == 8


def test_save_cleans_up_temp_file_on_replace_failure(tmp_path, monkeypatch):
    from pathlib import Path

    existing = personas.generate("macos", seed=3, coherence_026=True)
    personas.save(tmp_path, existing)
    orig_bytes = personas.persona_path(tmp_path).read_bytes()
    unrelated = tmp_path / "unrelated.tmp"
    unrelated.write_text("keep-me", encoding="utf-8")

    orig_replace = Path.replace
    saw_tmp_bytes = False

    def fail_replace(self, target):
        nonlocal saw_tmp_bytes
        if self.suffix == ".tmp" and Path(target).name == personas.PERSONA_FILE:
            saw_tmp_bytes = len(self.read_bytes()) > 0
            raise OSError("injected-replace-failure")
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", fail_replace)
    updated = personas.generate("macos", seed=45, coherence_026=True)
    with pytest.raises(OSError, match="injected-replace-failure"):
        personas.save(tmp_path, updated)

    assert saw_tmp_bytes is True
    assert personas.persona_path(tmp_path).read_bytes() == orig_bytes
    assert unrelated.read_text(encoding="utf-8") == "keep-me"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["chrome_persona.json", "unrelated.tmp"]
