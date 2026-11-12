import json
import os
import pathlib
import zipfile
from datetime import datetime

from ...core.logging import get_logger
from ...models.profile import (
    CANVAS_MODES,
    ENGINES,
    WEBRTC_MODES,
    Profile,
    normalize_tags,
    split_recovery_and_notes,
    valid_display_order,
)
from ...utils.totp import clean_totp_secret
from ...utils.validation import (
    normalize_start_url,
    valid_or_none,
    validate_locale,
    validate_profile_name,
    validate_proxy_format,
    validate_timezone,
)
from ..browser.fingerprint import IDENTITY_FILES

logger = get_logger("profile.transfer")


def export_to_zip(
    profile: Profile,
    profile_data_dir: str,
    export_path: str,
    include_data: bool = True,
    exclude_files: frozenset[str] = frozenset(),
) -> tuple[bool, str]:
    """Create a ZIP archive of a profile and optionally its browser data."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_filename = f"{profile.name}_{timestamp}.zip"
        zip_path = os.path.join(export_path, zip_filename)

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            zipf.writestr("profile.json", json.dumps(profile.to_dict(), indent=2))

            if include_data and pathlib.Path(profile_data_dir).exists():
                for root, _dirs, files in os.walk(profile_data_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        if os.path.relpath(file_path, profile_data_dir) in exclude_files:
                            continue
                        arcname = os.path.join(
                            "data",
                            os.path.relpath(file_path, profile_data_dir),
                        )
                        zipf.write(file_path, arcname)
            else:
                # The fingerprint is the profile's identity, not browser data:
                # keep it so the imported profile presents the same device.
                for identity in IDENTITY_FILES:
                    fp_file = os.path.join(profile_data_dir, identity)
                    if pathlib.Path(fp_file).exists():
                        zipf.write(fp_file, os.path.join("data", identity))

        logger.info("Exported profile %s to %s", profile.name, zip_path)
        return True, zip_path
    except Exception as e:
        logger.exception("Error exporting profile %s: %s", profile.name, e)
        return False, str(e)


def read_profile_meta(zip_path: str) -> dict | None:
    """profile.json of an archive without extracting anything, or None if unreadable."""
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            return json.loads(zipf.read("profile.json"))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        return None


def import_from_zip(
    zip_path: str,
    data_dir: str,
) -> tuple[bool, Profile | str]:
    """Extract a profile from a ZIP archive.

    Returns (True, Profile) on success or (False, error_message) on failure.
    The caller is responsible for registering the profile in the manager.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zipf:
            if "profile.json" not in zipf.namelist():
                return False, "Invalid profile archive (missing profile.json)"

            profile_data = json.loads(zipf.read("profile.json"))
            name = profile_data.get("name")

            if not name:
                return False, "Invalid profile data (missing name)"
            valid, msg = validate_profile_name(name)
            if not valid:
                return False, f"Invalid profile name in archive: {msg}"

            tags = normalize_tags(profile_data.get("tags", []))
            display_order = profile_data.get("display_order")
            if not valid_display_order(display_order):
                return False, "Invalid display order in archive"
            proxy = profile_data.get("proxy")
            if proxy in ("", "None"):
                proxy = None  # Legacy CLI direct-connection sentinel.
            if proxy is not None and (
                not isinstance(proxy, str) or not validate_proxy_format(proxy)[0]
            ):
                return False, "Invalid proxy configuration in archive"
            profile = Profile(
                name=name,
                core_version=profile_data.get("core_version")
                if isinstance(profile_data.get("core_version"), str)
                else None,
                proxy=proxy,
                os_type=profile_data.get("os_type", "windows"),
                timezone=valid_or_none(profile_data.get("timezone"), validate_timezone),
                locale=valid_or_none(profile_data.get("locale"), validate_locale),
                engine=profile_data.get("engine")
                if profile_data.get("engine") in ENGINES
                else "camoufox",
                group=tags[0] if tags else (profile_data.get("group") or "").strip() or None,
                tags=tags,
                extension_ids=[
                    str(x).strip()
                    for x in (profile_data.get("extension_ids") or [])
                    if isinstance(x, str) and x.strip()
                ]
                if isinstance(profile_data.get("extension_ids"), list)
                else [],
                display_order=display_order,
                account_uid=(profile_data.get("account_uid") or "").strip() or None,
                account_password=profile_data.get("account_password") or None,
                two_fa_secret=clean_totp_secret(profile_data.get("two_fa_secret")) or None,
                recovery_email=split_recovery_and_notes(
                    profile_data.get("recovery_email"), profile_data.get("notes")
                )[0]
                or None,
                notes=split_recovery_and_notes(
                    profile_data.get("recovery_email"), profile_data.get("notes")
                )[1]
                or None,
                start_url=normalize_start_url(profile_data.get("start_url")) or None,
                webrtc_mode=profile_data.get("webrtc_mode")
                if profile_data.get("webrtc_mode") in WEBRTC_MODES
                else "proxy",
                canvas_mode=profile_data.get("canvas_mode")
                if profile_data.get("canvas_mode") in CANVAS_MODES
                else "auto",
            )

            data_files = [f for f in zipf.namelist() if f.startswith("data/")]
            if data_files:
                profile_data_dir = os.path.join(data_dir, name)
                root = pathlib.Path(profile_data_dir).resolve()
                root.mkdir(exist_ok=True, parents=True)

                for file in data_files:
                    if file.endswith("/"):
                        continue
                    target_path = os.path.join(
                        profile_data_dir,
                        os.path.relpath(file, "data"),
                    )
                    # Reject entries such as data/../../evil that would be
                    # written outside the profile's own directory.
                    if not pathlib.Path(target_path).resolve().is_relative_to(root):
                        return False, f"Unsafe path in archive: {file}"
                    pathlib.Path(target_path).parent.mkdir(
                        exist_ok=True,
                        parents=True,
                    )
                    with zipf.open(file) as src, open(target_path, "wb") as dst:
                        dst.write(src.read())

            logger.info("Imported profile from zip: %s", name)
            return True, profile
    except zipfile.BadZipFile:
        return False, "Invalid zip file"
    except Exception as e:
        logger.exception("Error importing profile: %s", e)
        return False, str(e)
