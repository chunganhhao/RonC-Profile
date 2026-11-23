from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING

from ...core import config as app_config
from ...core.logging import get_logger
from ..browser.lease import profile_lease, waiting_lease

if TYPE_CHECKING:
    from ...interfaces.protocols import IBrowserLauncher, IProfileManager

logger = get_logger("warming.scheduler")

WARMING_PRESETS: dict[str, list[str]] = {
    "youtube_trust": [
        "https://www.youtube.com",
        "https://www.google.com/search?q=technology+trends",
        "https://news.google.com",
    ],
    "social_news": [
        "https://www.wikipedia.org",
        "https://www.reddit.com",
        "https://news.ycombinator.com",
    ],
    "ecommerce_cookie": [
        "https://www.amazon.com",
        "https://www.ebay.com",
        "https://www.etsy.com",
    ],
    "custom": [],
}

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
# How often a running task rereads the shared file for a Stop or Delete sent to another process.
SHARED_STOP_POLL_SECONDS = 0.25
# A navigation step can take 25 s before the owner sees a Stop; then it closes the browser.
PEER_STOP_WAIT_SECONDS = 60.0
# A run's result has no caller to retry it, so its save waits out another process's write.
STORE_WAIT_SECONDS = 5.0


class WarmingTargetSaveError(OSError):
    pass


class WarmingTaskNotFound(LookupError):
    pass


class WarmingStopTimeout(TimeoutError):
    pass


def validate_schedule_time(value: str) -> bool:
    val = (value or "").strip()
    if not val:
        return True
    return bool(_TIME_RE.match(val))


def normalize_warming_urls(urls: list[str] | str) -> list[str]:
    raw_list = urls.split() if isinstance(urls, str) else [str(u).strip() for u in urls]
    cleaned: list[str] = []
    for u in raw_list:
        if not u:
            continue
        if "://" not in u:
            u = "https://" + u
        if u.lower().startswith(("http://", "https://")):
            cleaned.append(u)
    return cleaned


@dataclass
class WarmingTask:
    id: str
    name: str
    profile_names: list[str] = field(default_factory=list)
    preset: str = "youtube_trust"
    custom_urls: list[str] = field(default_factory=list)
    duration_minutes: int = 3
    schedule_time: str = ""  # "HH:MM" or "" for manual
    repeat_daily: bool = True
    enabled: bool = True
    status: str = "idle"  # "idle" | "running" | "completed" | "failed"
    last_run: float = 0.0
    last_result: str = ""
    last_triggered_slot: str = ""

    def resolved_urls(self) -> list[str]:
        if self.preset == "custom" or self.custom_urls:
            custom = normalize_warming_urls(self.custom_urls)
            if custom:
                return custom
        return list(WARMING_PRESETS.get(self.preset) or WARMING_PRESETS["youtube_trust"])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> WarmingTask | None:
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        tid = str(data.get("id") or secrets.token_hex(5))
        preset = str(data.get("preset") or "youtube_trust")
        if preset not in WARMING_PRESETS:
            preset = "youtube_trust"
        sched = str(data.get("schedule_time") or "").strip()
        if not validate_schedule_time(sched):
            sched = ""
        dur = max(1, min(60, int(data.get("duration_minutes") or 3)))
        pnames = [str(p).strip() for p in (data.get("profile_names") or []) if str(p).strip()]
        urls = normalize_warming_urls(data.get("custom_urls") or [])
        status = str(data.get("status") or "idle")
        if status not in ("idle", "running", "completed", "failed"):
            status = "idle"
        return cls(
            id=tid,
            name=name,
            profile_names=pnames,
            preset=preset,
            custom_urls=urls,
            duration_minutes=dur,
            schedule_time=sched,
            repeat_daily=bool(data.get("repeat_daily", True)),
            enabled=bool(data.get("enabled", True)),
            status=status,
            last_run=float(data.get("last_run") or 0.0),
            last_result=str(data.get("last_result") or ""),
            last_triggered_slot=str(data.get("last_triggered_slot") or ""),
        )


class WarmingSchedulerService:
    """Manages account warming presets, scheduled tasks, and automated browsing journeys."""

    def __init__(self, file_path: str | None = None) -> None:
        self._explicit_path = file_path
        self._lock = threading.RLock()
        self._store_locked = False
        self._lease_lock = threading.RLock()
        self._tasks: dict[str, WarmingTask] = {}
        self._saved_tasks: dict[str, dict] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._profile_active_users: dict[str, int] = {}
        self._profile_launched_by_warming: set[str] = set()
        self._daemon_stop = threading.Event()
        self._daemon_thread: threading.Thread | None = None
        self._daemon_log_fn: Callable[[str], None] | None = None
        self._daemon_on_update: Callable[[], None] | None = None
        self.reload()

    @property
    def file_path(self) -> str:
        return self._explicit_path or app_config.WARMING_FILE

    def reload(self) -> None:
        with self._lock:
            try:
                self._tasks = self._read_tasks()
            except OSError as e:
                logger.error("Failed to load warming tasks from %s: %s", self.file_path, e)
                self._tasks = {}
            for task in self._tasks.values():
                if task.status == "running":
                    task.status = "idle"
            self._saved_tasks = self._snapshot()

    def _read_tasks(self) -> dict[str, WarmingTask]:
        try:
            with open(self.file_path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise OSError("Invalid warming task store") from exc
        items = raw.get("tasks", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise OSError("Invalid warming task store")
        tasks = {}
        try:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                if not item.get("id"):
                    payload = json.dumps([index, item], sort_keys=True).encode()
                    item = {**item, "id": hashlib.sha256(payload).hexdigest()[:10]}
                if (task := WarmingTask.from_dict(item)) is not None:
                    tasks[task.id] = task
        except (TypeError, ValueError, OverflowError) as exc:
            raise OSError("Invalid warming task store") from exc
        return tasks

    def _snapshot(self) -> dict[str, dict]:
        return {tid: task.to_dict() for tid, task in self._tasks.items()}

    @contextlib.contextmanager
    def _store_lease(self, *, timeout: float = STORE_WAIT_SECONDS) -> Iterator[None]:
        with self._lock:
            if self._store_locked:
                yield
                return
            with waiting_lease(self.file_path, timeout):
                self._store_locked = True
                try:
                    yield
                finally:
                    self._store_locked = False

    def _save(self) -> None:
        with self._store_lease():
            path = self.file_path
            disk = {tid: task.to_dict() for tid, task in self._read_tasks().items()}
            current = self._snapshot()
            # Persist only local changes so stale snapshots cannot undo another writer's edits.
            for tid in self._saved_tasks.keys() - current.keys():
                disk.pop(tid, None)
            for tid, task in current.items():
                previous = self._saved_tasks.get(tid)
                if previous is None:
                    disk[tid] = task
                elif tid in disk:
                    disk[tid].update(
                        {key: value for key, value in task.items() if value != previous[key]}
                    )
            parent = os.path.dirname(os.path.abspath(path))
            os.makedirs(parent, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=parent, prefix=".warming_", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(list(disk.values()), f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            self._saved_tasks = current

    def list_tasks(self) -> list[WarmingTask]:
        with self._lock:
            return list(self._tasks.values())

    def get_task(self, task_id: str) -> WarmingTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    @contextlib.contextmanager
    def retarget_profile(self, old_name: str, new_name: str) -> Iterator[None]:
        with contextlib.ExitStack() as lease:
            try:
                lease.enter_context(self._store_lease())
                latest = self._read_tasks()
            except OSError as exc:
                logger.exception("Could not read warming targets for rename")
                raise WarmingTargetSaveError("Could not save renamed warming targets") from exc
            for tid, task in latest.items():
                if task.status == "running" and tid not in self._cancel_flags:
                    task.status = "idle"
                if tid in self._tasks:
                    current = self._tasks[tid]
                    current.__dict__.update(vars(task))
                    latest[tid] = current
            self._tasks = latest
            self._saved_tasks = self._snapshot()
            previous = [
                (task, task.profile_names)
                for task in self._tasks.values()
                if old_name in task.profile_names
            ]
            if not previous:
                yield
                return
            for task, names in previous:
                task.profile_names = [new_name if name == old_name else name for name in names]
            try:
                try:
                    self._save()
                except OSError as exc:
                    logger.exception("Could not save renamed warming targets")
                    raise WarmingTargetSaveError("Could not save renamed warming targets") from exc
            except Exception:
                for task, names in previous:
                    task.profile_names = names
                raise
            try:
                yield
            except Exception:
                for task, names in previous:
                    task.profile_names = names
                self._save()
                raise

    def add_task(
        self,
        name: str,
        profile_names: list[str],
        preset: str = "youtube_trust",
        custom_urls: list[str] | str | None = None,
        duration_minutes: int = 3,
        schedule_time: str = "",
        repeat_daily: bool = True,
        enabled: bool = True,
    ) -> WarmingTask | None:
        clean_name = (name or "").strip()
        clean_profiles = [p.strip() for p in profile_names if p and p.strip()]
        if not clean_name or not clean_profiles:
            return None
        sched = (schedule_time or "").strip()
        if not validate_schedule_time(sched):
            return None
        if preset not in WARMING_PRESETS:
            preset = "youtube_trust"
        urls = normalize_warming_urls(custom_urls or [])
        if preset == "custom" and not urls:
            return None
        with self._lock:
            tid = secrets.token_hex(5)
            task = WarmingTask(
                id=tid,
                name=clean_name,
                profile_names=clean_profiles,
                preset=preset,
                custom_urls=urls,
                duration_minutes=max(1, min(60, int(duration_minutes or 3))),
                schedule_time=sched,
                repeat_daily=repeat_daily,
                enabled=enabled,
            )
            self._tasks[tid] = task
            try:
                self._save()
            except Exception:
                del self._tasks[tid]
                raise
            return task

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            previous_tasks = self._tasks.copy()
            del self._tasks[task_id]
            try:
                self._save()
            except Exception:
                self._tasks = previous_tasks
                raise
            cancel = self._cancel_flags.pop(task_id, None)
            if cancel:
                cancel.set()
            return True

    def stop_task(self, task_id: str) -> bool:
        """Stop a run, waiting for the process that owns it when that is another process."""
        with self._store_lease():
            task = self._tasks.get(task_id)
            if not task:
                return False
            previous = task.status, self._saved_tasks.get(task_id)
            saved = self._read_tasks().get(task_id)
            if saved is not None and task_id in self._saved_tasks:
                # Start from the shared status: this process may never have seen a peer's run start.
                task.status = saved.status
                self._saved_tasks[task_id] = {**self._saved_tasks[task_id], "status": saved.status}
            task.status = "idle"
            try:
                self._save()
            except Exception:
                task.status = previous[0]
                if previous[1] is not None:
                    self._saved_tasks[task_id] = previous[1]
                raise
            cancel = self._cancel_flags.get(task_id)
            if cancel:
                cancel.set()
                return True
        try:
            with waiting_lease(self._run_lease_path(task_id), PEER_STOP_WAIT_SECONDS):
                pass
        except (BlockingIOError, PermissionError) as exc:
            raise WarmingStopTimeout(task_id) from exc
        return True

    def _run_lease_path(self, task_id: str) -> str:
        return f"{self.file_path}.run-{task_id}"

    def _own_run(
        self,
        run_lease: contextlib.ExitStack,
        task: WarmingTask,
        cancel_ev: threading.Event,
        *args,
    ) -> bool:
        """Run the task while holding its cross-process run lease, released after the result is saved."""
        with run_lease:
            threading.Thread(
                target=self._watch_shared_stop, args=(task.id, cancel_ev), daemon=True
            ).start()
            return self._execute_task(task, cancel_ev, *args)

    def _watch_shared_stop(self, task_id: str, cancel_ev: threading.Event) -> None:
        """Carry a Stop or Delete that a peer process wrote to the shared file to this run."""
        while True:
            time.sleep(SHARED_STOP_POLL_SECONDS)
            if cancel_ev.is_set() or self._cancel_flags.get(task_id) is not cancel_ev:
                return
            try:
                saved = self._read_tasks().get(task_id)
            except OSError:
                continue
            if saved is None or saved.status != "running":
                cancel_ev.set()
                return

    def execute_task_sync(
        self,
        task_id: str,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        step_dwell_seconds: float | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> bool:
        """Execute a warming task synchronously."""
        with self._store_lease():
            started = self._begin_task(task_id)
        if started is None:
            return False
        task, cancel_ev, _, run_lease = started
        return self._own_run(
            run_lease, task, cancel_ev, pm, bl, log_fn, step_dwell_seconds, on_update
        )

    def _begin_task(
        self, task_id: str, *, scheduled_slot: str | None = None, require_exists: bool = False
    ) -> tuple[WarmingTask, threading.Event, tuple[str, str], contextlib.ExitStack] | None:
        task = self._tasks.get(task_id)
        if task is None or task_id not in self._read_tasks():
            if require_exists:
                raise WarmingTaskNotFound(task_id)
            return None
        if task.status == "running":
            return None
        run_lease = contextlib.ExitStack()
        try:
            run_lease.enter_context(profile_lease(self._run_lease_path(task_id)))
        # Another process is running this task: it owns the result.
        except (BlockingIOError, PermissionError):
            return None
        previous_state = task.status, task.last_triggered_slot
        task.status = "running"
        if scheduled_slot is not None:
            task.last_triggered_slot = scheduled_slot
        cancel_ev = threading.Event()
        self._cancel_flags[task_id] = cancel_ev
        try:
            self._save()
        except Exception:
            task.status, task.last_triggered_slot = previous_state
            self._cancel_flags.pop(task_id, None)
            run_lease.close()
            raise
        return replace(task), cancel_ev, previous_state, run_lease

    def _execute_task(
        self,
        task: WarmingTask,
        cancel_ev: threading.Event,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None,
        step_dwell_seconds: float | None,
        on_update: Callable[[], None] | None,
    ) -> bool:
        log = log_fn or (lambda m: logger.info(m))
        task_id = task.id
        urls = task.resolved_urls()
        targets = task.profile_names
        dur_mins = task.duration_minutes
        task_name = task.name

        if on_update:
            with contextlib.suppress(Exception):
                on_update()

        log(f"[Warming] Starting '{task_name}' for {len(targets)} profile(s) ({len(urls)} sites)")

        total_budget_seconds = float(max(1, dur_mins) * 60)
        per_url_dwell = (
            float(step_dwell_seconds)
            if step_dwell_seconds is not None
            else min(20.0, max(2.0, total_budget_seconds / max(1, len(urls))))
        )

        succeeded = 0
        for prof_name in targets:
            if cancel_ev.is_set():
                break
            prof = pm.profiles.get(prof_name)
            if not prof:
                log(f"[Warming] Skipped missing profile '{prof_name}'")
                continue

            acquired_lease = False
            try:
                with self._lease_lock:
                    self._profile_active_users[prof_name] = (
                        self._profile_active_users.get(prof_name, 0) + 1
                    )
                    acquired_lease = True
                    need_launch = not bl.is_running(prof_name)
                    if need_launch:
                        started = bl.start_thread(prof, log)
                        if started:
                            self._profile_launched_by_warming.add(prof_name)
                    else:
                        started = False

                if need_launch and started:
                    ok, err = bl.wait_for_launch(prof_name, timeout=45.0)
                    if not ok:
                        log(f"[Warming] '{prof_name}' launch failed: {err}")
                        continue

                visited_any = False
                single_pass = step_dwell_seconds is not None
                remaining_dwell = total_budget_seconds
                while not cancel_ev.is_set():
                    visited_in_pass = False
                    for url in urls:
                        if cancel_ev.is_set() or (not single_pass and remaining_dwell <= 0):
                            break
                        try:
                            bl.control(
                                prof_name,
                                "navigate",
                                {"url": url, "wait_until": "domcontentloaded"},
                                timeout=25.0,
                            )
                            visited_any = True
                            visited_in_pass = True
                            log(f"[Warming] [{prof_name}] Visited {url}")
                        except Exception as nav_err:
                            log(f"[Warming] [{prof_name}] Navigate warning on {url}: {nav_err}")
                            continue

                        dwell = (
                            per_url_dwell if single_pass else min(per_url_dwell, remaining_dwell)
                        )
                        if not single_pass:
                            remaining_dwell -= dwell
                        if dwell > 0 and not cancel_ev.wait(dwell / 2.0):
                            with contextlib.suppress(Exception):
                                bl.control(
                                    prof_name,
                                    "scroll",
                                    {"delta_y": 380, "humanize": True},
                                    timeout=8.0,
                                )
                            cancel_ev.wait(dwell / 2.0)
                    if single_pass or not visited_in_pass or remaining_dwell <= 0:
                        break

                if visited_any:
                    succeeded += 1
            except Exception as e:
                log(f"[Warming] Error warming '{prof_name}': {e}")
            finally:
                if acquired_lease:
                    with self._lease_lock:
                        rem = self._profile_active_users.get(prof_name, 1) - 1
                        if rem > 0:
                            self._profile_active_users[prof_name] = rem
                            candidate_stop = False
                        else:
                            self._profile_active_users.pop(prof_name, None)
                            candidate_stop = prof_name in self._profile_launched_by_warming
                    if candidate_stop:
                        is_live = False
                        try:
                            is_live = bl.is_running(prof_name)
                        finally:
                            with self._lease_lock:
                                if self._profile_active_users.get(prof_name, 0) == 0:
                                    self._profile_launched_by_warming.discard(prof_name)
                                    if is_live:
                                        with contextlib.suppress(Exception):
                                            bl.stop_profile(prof_name)

        with self._lock:
            is_current_owner = self._cancel_flags.get(task_id) is cancel_ev
            if is_current_owner:
                self._cancel_flags.pop(task_id, None)
                task = self._tasks.get(task_id)
                if task:
                    task.last_run = time.time()
                    if cancel_ev.is_set():
                        task.status = "idle"
                        task.last_result = f"Stopped ({succeeded}/{len(targets)})"
                    elif succeeded > 0:
                        task.status = "completed"
                        task.last_result = f"OK ({succeeded}/{len(targets)} profiles)"
                    else:
                        task.status = "failed"
                        task.last_result = f"Failed (0/{len(targets)} profiles)"
                    if not task.repeat_daily and task.schedule_time:
                        task.enabled = False
                    with self._store_lease():
                        self._save()

        log(f"[Warming] Finished '{task_name}': {succeeded}/{len(targets)} profile(s) warmed")
        if on_update:
            with contextlib.suppress(Exception):
                on_update()
        return succeeded > 0

    def run_task_async(
        self,
        task_id: str,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
        *,
        require_exists: bool = False,
        scheduled_slot: str | None = None,
    ) -> bool:
        with self._store_lease():
            started = self._begin_task(
                task_id, require_exists=require_exists, scheduled_slot=scheduled_slot
            )
            if started is None:
                return False
            task, cancel_ev, previous_state, run_lease = started
            try:
                threading.Thread(
                    target=self._own_run,
                    args=(run_lease, task, cancel_ev, pm, bl, log_fn, None, on_update),
                    daemon=True,
                ).start()
            except RuntimeError:
                current = self._tasks[task_id]
                current.status, current.last_triggered_slot = previous_state
                self._cancel_flags.pop(task_id, None)
                run_lease.close()
                self._save()
                raise
        return True

    def check_due_tasks(
        self,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
        now: datetime.datetime | None = None,
    ) -> list[str]:
        """Trigger any enabled scheduled task matching current HH:MM that has not run this minute."""
        dt = now or datetime.datetime.now()
        hhmm = dt.strftime("%H:%M")
        slot = dt.strftime("%Y-%m-%d %H:%M")
        triggered: list[str] = []
        with self._store_lease():
            latest = self._read_tasks()
            for task in self._tasks.values():
                saved = latest.get(task.id)
                if (
                    saved is None
                    or not task.enabled
                    or not saved.enabled
                    or task.schedule_time != hhmm
                    or saved.schedule_time != hhmm
                    or task.last_triggered_slot == slot
                    or saved.last_triggered_slot == slot
                ):
                    continue
                if self.run_task_async(
                    task.id, pm, bl, log_fn=log_fn, on_update=on_update, scheduled_slot=slot
                ):
                    triggered.append(task.id)
        return triggered

    def start_daemon(
        self,
        pm: IProfileManager,
        bl: IBrowserLauncher,
        log_fn: Callable[[str], None] | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            if log_fn is not None:
                self._daemon_log_fn = log_fn
            if on_update is not None:
                self._daemon_on_update = on_update
            if (
                self._daemon_thread
                and self._daemon_thread.is_alive()
                and not self._daemon_stop.is_set()
            ):
                return
            if self._daemon_stop.is_set():
                self._daemon_stop = threading.Event()
            stop_ev = self._daemon_stop

            def _loop() -> None:
                while not stop_ev.wait(15.0):
                    with contextlib.suppress(Exception):
                        self.check_due_tasks(
                            pm,
                            bl,
                            log_fn=self._daemon_log_fn,
                            on_update=self._daemon_on_update,
                        )

            self._daemon_thread = threading.Thread(target=_loop, daemon=True)
            self._daemon_thread.start()

    def stop_daemon(self) -> None:
        with self._lock:
            self._daemon_stop.set()
