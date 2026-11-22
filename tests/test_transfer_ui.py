import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.models.profile import Profile
from src.ui.actions import transfer
from src.ui.handlers import AppHandlers


@pytest.mark.parametrize("outcome", ["success", "failure", "error", "cancel"])
def test_import_handler(outcome):
    async def run():
        loop_thread = threading.get_ident()
        control = SimpleNamespace(disabled=False, update=Mock())
        picker = SimpleNamespace(
            pick_files=AsyncMock(
                return_value=[] if outcome == "cancel" else [SimpleNamespace(path="p.zip")]
            )
        )

        def do_import(path):
            assert path == "p.zip"
            assert threading.get_ident() != loop_thread
            assert control.disabled
            if outcome == "error":
                raise RuntimeError("unexpected")
            return outcome == "success", "result"

        pm = SimpleNamespace(import_profile=Mock(side_effect=do_import))
        log, refresh = Mock(), Mock()
        handler = AppHandlers(
            pm,
            None,
            None,
            None,
            lambda: None,
            lambda: SimpleNamespace(file_picker=picker),
            log,
            refresh,
            lambda: (),
        )
        if outcome == "error":
            with pytest.raises(RuntimeError, match="unexpected"):
                await handler.on_import(SimpleNamespace(control=control))
        else:
            await handler.on_import(SimpleNamespace(control=control))
        assert not control.disabled
        assert control.update.call_count == 2
        assert refresh.call_count == (outcome == "success")
        assert log.call_count == (outcome in ("success", "failure"))

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "failure", "error", "cancel", "empty"])
def test_export_handler(outcome):
    async def run():
        loop_thread = threading.get_ident()
        page = SimpleNamespace(
            width=1000,
            height=800,
            on_resize=None,
            update=Mock(),
            pop_dialog=Mock(),
            show_dialog=Mock(),
        )
        picker = SimpleNamespace(
            get_directory_path=AsyncMock(return_value=None if outcome == "cancel" else "/out")
        )

        def do_export(name, directory, include_data):
            assert (name, directory, include_data) == ("alice", "/out", True)
            assert threading.get_ident() != loop_thread
            assert button.disabled
            if outcome == "error":
                raise RuntimeError("unexpected")
            return outcome == "success", "result"

        pm = SimpleNamespace(
            list_profiles=lambda: [Profile(name="alice")],
            export_profile=Mock(side_effect=do_export),
        )
        log = Mock()
        transfer.export_profile(page, picker, pm, log)
        dialog = page.show_dialog.call_args.args[0]
        button = dialog.actions[-1]
        dialog.content.content.controls[4].value = outcome != "empty"
        if outcome == "error":
            with pytest.raises(RuntimeError, match="unexpected"):
                await button.on_click(SimpleNamespace(control=button))
        else:
            await button.on_click(SimpleNamespace(control=button))
        assert not button.disabled
        assert pm.export_profile.call_count == (outcome not in ("cancel", "empty"))
        assert log.call_count == (outcome in ("success", "failure"))
        assert page.pop_dialog.call_count == (outcome in ("success", "failure"))

    asyncio.run(run())
