import logging
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_cli import active_sessions


def test_resolve_max_concurrent_sessions_values(caplog):
    assert active_sessions.resolve_max_concurrent_sessions({}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": None}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": 0}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": -1}) is None
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "3"}) == 3
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"gateway": {"max_concurrent_sessions": 4}}
        )
        == 4
    )
    assert (
        active_sessions.resolve_max_concurrent_sessions(
            {"max_concurrent_sessions": 2, "gateway": {"max_concurrent_sessions": 4}}
        )
        == 2
    )

    caplog.set_level(logging.WARNING)
    assert active_sessions.resolve_max_concurrent_sessions({"max_concurrent_sessions": "many"}) is None
    assert any(
        "Ignoring invalid max_concurrent_sessions='many'" in record.message
        for record in caplog.records
    )












def test_cross_process_acquire_claims_only_one_last_slot(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    repo_root = Path(__file__).resolve().parents[2]
    ready_dir = tmp_path / "ready"
    ready_dir.mkdir()
    results_dir = tmp_path / "results"
    results_dir.mkdir()
    go_file = tmp_path / "go"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(repo_root)
    script = (
        "import os, time\n"
        "from pathlib import Path\n"
        "from hermes_cli.active_sessions import try_acquire_active_session\n"
        "idx = os.environ['WORKER_INDEX']\n"
        "worker_count = int(os.environ['WORKER_COUNT'])\n"
        "delayed_worker = os.environ.get('DELAYED_WORKER_INDEX')\n"
        "ready_dir = Path(os.environ['READY_DIR'])\n"
        "results_dir = Path(os.environ['RESULTS_DIR'])\n"
        "go_file = Path(os.environ['GO_FILE'])\n"
        "(ready_dir / idx).write_text('ready', encoding='utf-8')\n"
        "deadline = time.time() + 10\n"
        "while not go_file.exists():\n"
        "    if time.time() > deadline:\n"
        "        raise RuntimeError('timed out waiting for go file')\n"
        "    time.sleep(0.01)\n"
        "if idx == delayed_worker:\n"
        "    time.sleep(2.5)\n"
        "lease, message = try_acquire_active_session(\n"
        "    session_id=f'process-{idx}',\n"
        "    surface='cli',\n"
        "    config={'max_concurrent_sessions': 1},\n"
        ")\n"
        "if lease is None:\n"
        "    (results_dir / idx).write_text('BLOCK', encoding='utf-8')\n"
        "    print('BLOCK', flush=True)\n"
        "else:\n"
        "    (results_dir / idx).write_text('OK', encoding='utf-8')\n"
        "    print('OK', flush=True)\n"
        "    deadline = time.time() + 10\n"
        "    while len(list(results_dir.iterdir())) < worker_count:\n"
        "        if time.time() > deadline:\n"
        "            raise RuntimeError('timed out waiting for all workers to attempt acquire')\n"
        "        time.sleep(0.01)\n"
        "    lease.release()\n"
    )
    workers: list[subprocess.Popen[str]] = []
    try:
        for index in range(6):
            worker_env = env.copy()
            worker_env["WORKER_INDEX"] = str(index)
            worker_env["WORKER_COUNT"] = "6"
            worker_env["DELAYED_WORKER_INDEX"] = "5"
            worker_env["READY_DIR"] = str(ready_dir)
            worker_env["RESULTS_DIR"] = str(results_dir)
            worker_env["GO_FILE"] = str(go_file)
            workers.append(
                subprocess.Popen(
                    [sys.executable, "-c", script],
                    env=worker_env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )

        deadline = time.time() + 10
        while len(list(ready_dir.iterdir())) < len(workers):
            if time.time() > deadline:
                raise AssertionError("workers did not become ready")
            time.sleep(0.01)
        go_file.write_text("go", encoding="utf-8")

        outputs = []
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr
            outputs.append(stdout.strip())
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.communicate()

    assert outputs.count("OK") == 1
    assert outputs.count("BLOCK") == len(workers) - 1
    assert active_sessions.active_session_registry_snapshot() == []




def test_release_orphaned_leases_reclaims_only_unowned_own_pid_entries(tmp_path, monkeypatch):
    """A long-lived server must reclaim leases whose session skipped teardown.

    ``_prune_dead`` only fires when the owning pid dies, so a ``hermes
    dashboard`` running for days holds a leaked lease until restart. The
    process reconciles against the leases it still owns instead.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    cfg = {"max_concurrent_sessions": 5}
    kept, orphan = (
        active_sessions.try_acquire_active_session(
            session_id=sid, surface="desktop", config=cfg
        )[0]
        for sid in ("kept", "orphaned")
    )
    # Another live process's lease is not ours to reclaim.
    active_sessions._write_entries(
        active_sessions._state_path(),
        active_sessions._read_entries(active_sessions._state_path())
        + [{"lease_id": "elsewhere", "session_id": "other", "surface": "cli", "pid": os.getpid() }],
    )

    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None


def test_release_under_profile_home_override_targets_acquisition_registry(
    tmp_path, monkeypatch
):
    """Regression for #85431: a lease acquired against the root HERMES_HOME
    must release from the root registry even when ``release()`` runs inside a
    profile home override (native multiplex runs agent cleanup under
    ``_profile_runtime_scope``). Before the fix the root entry survived and
    the session cap filled with phantom leases."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    lease, error = active_sessions.try_acquire_active_session(
        session_id="agent:worker:telegram:dm:synthetic",
        surface="gateway:telegram",
        config={"max_concurrent_sessions": 2},
    )
    assert lease is not None and error is None
    root_registry = root / "runtime" / "active_sessions.json"
    assert root_registry.exists()

    token = set_hermes_home_override(str(profile))
    try:
        lease.release()
    finally:
        reset_hermes_home_override(token)

    assert lease.released is True
    remaining = active_sessions._read_entries(root_registry)
    assert remaining == []
    # No phantom registry created under the profile home.
    assert not (profile / "runtime" / "active_sessions.json").exists()


def test_transfer_under_profile_home_override_targets_acquisition_registry(
    tmp_path, monkeypatch
):
    """Sibling site of #85431: transfer must also update the registry the
    lease was acquired against, not one resolved from the current override."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    lease, error = active_sessions.try_acquire_active_session(
        session_id="before",
        surface="gateway:telegram",
        config={"max_concurrent_sessions": 2},
    )
    assert lease is not None and error is None

    token = set_hermes_home_override(str(profile))
    try:
        assert active_sessions.transfer_active_session(lease, session_id="after")
    finally:
        reset_hermes_home_override(token)

    root_registry = root / "runtime" / "active_sessions.json"
    entries = active_sessions._read_entries(root_registry)
    assert [entry["session_id"] for entry in entries] == ["after"]


def test_liveness_registry_corruption_fails_closed_without_overwrite(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    state_path.parent.mkdir(parents=True)
    corrupt = "{not-json"
    state_path.write_text(corrupt, encoding="utf-8")

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        with active_sessions.active_session_liveness_guard("session-1"):
            pass

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.active_session_registry_snapshot()

    assert state_path.read_text(encoding="utf-8") == corrupt

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.try_acquire_active_session(
            session_id="desktop-1",
            surface="desktop",
            config={},
            track_liveness=True,
        )
    assert state_path.read_text(encoding="utf-8") == corrupt

    # Ownership uncertainty fails CLOSED on every path now (#94595): a corrupt
    # registry must refuse the session — with a typed reason — rather than
    # silently readmitting a possible second writer. It still must not erase
    # the evidence.
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-1",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is None
    assert getattr(message, "reason", None) == (
        active_sessions.SESSION_COORDINATION_UNAVAILABLE
    )
    assert state_path.read_text(encoding="utf-8") == corrupt


def test_strict_registry_rejects_structurally_invalid_entries(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    base = {
        "lease_id": "lease-1",
        "session_id": "session-1",
        "surface": "desktop",
        "pid": os.getpid(),
        "track_liveness": True,
    }
    invalid_entries = (
        {key: value for key, value in base.items() if key != "lease_id"},
        {**base, "lease_id": ""},
        {key: value for key, value in base.items() if key != "session_id"},
        {**base, "session_id": "  "},
        {**base, "pid": 0},
        {**base, "pid": 1.5},
        {**base, "surface": 1},
        {**base, "track_liveness": "yes"},
        {**base, "metadata": []},
        {**base, "process_start_time": "not-a-number"},
        {**base, "process_start_time": "nan"},
    )

    for entry in invalid_entries:
        active_sessions._write_entries(state_path, [entry])
        original = state_path.read_text(encoding="utf-8")
        with pytest.raises(active_sessions.ActiveSessionRegistryError):
            with active_sessions.active_session_liveness_guard("session-1"):
                pass
        assert state_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    "second_session_id",
    ("session-a", "session-b"),
    ids=("exact-duplicate", "conflicting-duplicate"),
)
def test_strict_registry_rejects_duplicate_lease_ids(
    tmp_path, monkeypatch, second_session_id
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    active_sessions._write_entries(
        state_path,
        [
            {
                "lease_id": "duplicate-lease",
                "session_id": "session-a",
                "surface": "desktop",
                "pid": os.getpid(),
                "track_liveness": True,
            },
            {
                "lease_id": "duplicate-lease",
                "session_id": second_session_id,
                "surface": "desktop",
                "pid": os.getpid(),
                "track_liveness": True,
            },
        ],
    )
    original = state_path.read_text(encoding="utf-8")

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        active_sessions.active_session_registry_snapshot()

    assert state_path.read_text(encoding="utf-8") == original


def test_cap_transfer_does_not_overwrite_registry_corruption(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-old",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is not None and message is None

    corrupt = "{not-json"
    state_path.write_text(corrupt, encoding="utf-8")
    assert not active_sessions.transfer_active_session(
        lease,
        session_id="cli-new",
    )
    assert lease.session_id == "cli-old"
    assert state_path.read_text(encoding="utf-8") == corrupt

    lease.release()
    assert lease.released is True
    assert state_path.read_text(encoding="utf-8") == corrupt


def test_liveness_guard_rejects_unknown_pid_state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    state_path = home / "runtime" / "active_sessions.json"
    active_sessions._write_entries(
        state_path,
        [
            {
                "lease_id": "unknown-owner",
                "session_id": "session-1",
                "surface": "desktop",
                "pid": 12345,
                "track_liveness": True,
            }
        ],
    )
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda _pid: (_ for _ in ()).throw(OSError("pid lookup unavailable")),
    )

    with pytest.raises(active_sessions.ActiveSessionRegistryError):
        with active_sessions.active_session_liveness_guard("session-1"):
            pass

    original = state_path.read_text(encoding="utf-8")
    # An unknown pid state means dead-owner pruning cannot be trusted, which
    # means ownership cannot be proven. Fail closed (#94595), preserve the file.
    lease, message = active_sessions.try_acquire_active_session(
        session_id="cli-cap-session",
        surface="cli",
        config={"max_concurrent_sessions": 1},
    )
    assert lease is None
    assert getattr(message, "reason", None) == (
        active_sessions.SESSION_COORDINATION_UNAVAILABLE
    )
    assert state_path.read_text(encoding="utf-8") == original


def test_liveness_release_failure_is_retryable(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-1",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    original_write = active_sessions._write_entries
    monkeypatch.setattr(
        active_sessions,
        "_write_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        lease.release()
    assert lease.released is False

    monkeypatch.setattr(active_sessions, "_write_entries", original_write)
    lease.release()
    assert lease.released is True
    assert active_sessions.active_session_registry_snapshot() == []


def test_liveness_transfer_upserts_missing_entry_without_consuming_a_new_slot(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={"max_concurrent_sessions": 1},
        track_liveness=True,
    )
    assert lease is not None and message is None
    (home / "runtime" / "active_sessions.json").unlink()

    assert active_sessions.transfer_active_session(lease, session_id="session-new")
    snapshot = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in snapshot] == [
        (lease.lease_id, "session-new")
    ]

    blocked, limit_message = active_sessions.try_acquire_active_session(
        session_id="session-other",
        surface="desktop",
        config={"max_concurrent_sessions": 1},
        track_liveness=True,
    )
    assert blocked is None
    assert limit_message is not None
    lease.release()


def test_liveness_transfer_write_failure_keeps_old_id_for_retry(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    original_write = active_sessions._write_entries
    monkeypatch.setattr(
        active_sessions,
        "_write_entries",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    with pytest.raises(OSError, match="replace failed"):
        active_sessions.transfer_active_session(lease, session_id="session-new")
    assert lease.session_id == "session-old"

    monkeypatch.setattr(active_sessions, "_write_entries", original_write)
    assert active_sessions.transfer_active_session(lease, session_id="session-new")
    assert lease.session_id == "session-new"
    lease.release()


def test_release_wins_against_transfer_waiting_on_same_lease_lock(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    lease, message = active_sessions.try_acquire_active_session(
        session_id="session-old",
        surface="desktop",
        config={},
        track_liveness=True,
    )
    assert lease is not None and message is None

    release_wrote = threading.Event()
    allow_release = threading.Event()
    transfer_at_lock = threading.Event()
    original_write = active_sessions._write_entries
    original_enter = active_sessions._FileLock.__enter__

    def _blocking_write(path, entries):
        original_write(path, entries)
        if threading.current_thread().name == "lease-release":
            release_wrote.set()
            assert allow_release.wait(timeout=5)

    def _instrumented_enter(lock):
        if threading.current_thread().name == "lease-transfer":
            transfer_at_lock.set()
        return original_enter(lock)

    monkeypatch.setattr(active_sessions, "_write_entries", _blocking_write)
    monkeypatch.setattr(active_sessions._FileLock, "__enter__", _instrumented_enter)
    transfer_result: list[bool] = []
    release_thread = threading.Thread(target=lease.release, name="lease-release")
    transfer_thread = threading.Thread(
        target=lambda: transfer_result.append(
            active_sessions.transfer_active_session(lease, session_id="session-new")
        ),
        name="lease-transfer",
    )

    release_thread.start()
    assert release_wrote.wait(timeout=5)
    transfer_thread.start()
    assert transfer_at_lock.wait(timeout=5)
    allow_release.set()
    release_thread.join(timeout=5)
    transfer_thread.join(timeout=5)

    assert not release_thread.is_alive()
    assert not transfer_thread.is_alive()
    assert transfer_result == [False]
    assert lease.released is True
    assert active_sessions.active_session_registry_snapshot() == []


def test_refusal_message_names_holder_age_clock_and_next_steps(tmp_path, monkeypatch):
    """The refusal is the ONLY operator-facing surface when a session is locked —
    it must say who holds it (surface, pid), for how long (age), since when
    (wall clock), and what to do next. For a LIVE holder the next step is
    quitting that surface + `hermes status` — never --takeover, which only
    reclaims stale/dead leases."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="20260902_183916_bdcd79",
        surface="cli",
        config={},
        metadata={"live_session_id": "holder-live"},
    )
    assert holder is not None

    lease, refusal = active_sessions.try_acquire_active_session(
        session_id="20260902_183916_bdcd79",
        surface="desktop",
        config={},
        metadata={"live_session_id": "blocked-surface"},
    )

    assert lease is None
    assert refusal is not None
    assert refusal.reason == active_sessions.SESSION_NOT_OWNED
    message = str(refusal)
    # Who holds it: session id, holder surface, holder pid.
    assert "20260902_183916_bdcd79" in message
    assert "cli" in message
    assert f"pid {os.getpid()}" in message
    # For how long, and since when on the wall clock.
    assert "running" in message
    assert ", since " in message
    # What to do next for a LIVE holder: quit the holder surface first, and
    # the inspection command that actually shows live owners. --takeover must
    # NOT be advertised here — taking over from a live process creates two
    # writers on one session.
    assert "uit" in message  # "Quit that surface first (or wait)..."
    assert "hermes status" in message
    assert "--takeover" not in message
    holder.release()


def test_refusal_message_advertises_takeover_only_for_dead_holder():
    """session_already_owned_message advertises --takeover ONLY when the holder
    pid is dead/stale; a live holder gets the quit-first + `hermes status` advice."""
    live_entry = {
        "surface": "desktop",
        "pid": os.getpid(),
        "process_start_time": None,
        "started_at": time.time(),
    }
    live_message = active_sessions.session_already_owned_message("s1", live_entry)
    assert "--takeover" not in live_message
    assert "hermes status" in live_message

    dead_entry = {
        "surface": "desktop",
        "pid": 0x7FFFFFFE,  # provably dead pid
        "process_start_time": None,
        "started_at": time.time(),
    }
    dead_message = active_sessions.session_already_owned_message("s1", dead_entry)
    assert "hermes chat --resume s1 --takeover" in dead_message
    assert "hermes status" in dead_message


def test_refusal_carries_machine_readable_holder_payload(tmp_path, monkeypatch):
    """The desktop renders "who owns this" from typed data, never from prose."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="held-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "holder-live"},
    )
    assert holder is not None

    lease, refusal = active_sessions.try_acquire_active_session(
        session_id="held-session",
        surface="cli",
        config={},
        metadata={"live_session_id": "blocked"},
    )

    assert lease is None
    assert refusal.reason == active_sessions.SESSION_NOT_OWNED
    payload = refusal.holder
    assert payload["session_id"] == "held-session"
    assert payload["surface"] == "desktop"
    assert payload["pid"] == os.getpid()
    assert payload["started_at"] is not None
    assert payload["age_s"] is not None
    assert payload["age_s"] >= 0


def test_takeover_refused_while_holder_is_alive(tmp_path, monkeypatch, caplog):
    """--takeover must NOT steal from a live holder: the old owner never
    re-reads the registry, so moving the entry would leave TWO processes
    writing the same session (the 2026-09-02 interleaved-turn class).
    Takeover only reclaims stale/dead leases."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="owned-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert holder is not None
    # A normal claim is refused while the holder lives.
    blocked, refusal = active_sessions.try_acquire_active_session(
        session_id="owned-session",
        surface="cli",
        config={},
        metadata={"live_session_id": "cli-taker"},
    )
    assert blocked is None
    assert refusal.reason == active_sessions.SESSION_NOT_OWNED

    with caplog.at_level(logging.INFO, logger="hermes_cli.active_sessions"):
        lease, message = active_sessions.takeover_active_session(
            session_id="owned-session",
            surface="cli",
            config={},
            metadata={"live_session_id": "cli-taker"},
        )

    assert lease is None
    assert message is not None
    assert getattr(message, "reason", None) == active_sessions.SESSION_NOT_OWNED
    text = str(message)
    assert (
        f"holder pid {os.getpid()} (desktop) is alive — quit it first (or wait); "
        "--takeover only reclaims stale/dead leases" in text
    )
    # The holder's lease is untouched — no steal, no log line.
    entries = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in entries] == [
        (holder.lease_id, "owned-session")
    ]
    assert not any(
        "took over session" in record.getMessage() for record in caplog.records
    )
    holder.release()


def test_takeover_dead_holder_via_liveness_recheck_steals(tmp_path, monkeypatch, caplog):
    """The one legitimate steal: the holder entry survived pruning (alive at
    prune time) but is provably dead at the takeover decision — a stale pid /
    process start-time mismatch is reclaimed instead of refused."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="owned-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert holder is not None

    real_liveness = active_sessions._pid_liveness

    def _holder_died_between_prune_and_steal(pid, process_start_time=None):
        try:
            target = int(pid)
        except (TypeError, ValueError):
            target = -1
        if target == os.getpid():
            return False  # holder's pid: dead at decision time
        return real_liveness(pid, process_start_time)

    monkeypatch.setattr(active_sessions, "_pid_liveness", _holder_died_between_prune_and_steal)

    with caplog.at_level(logging.INFO, logger="hermes_cli.active_sessions"):
        lease, message = active_sessions.takeover_active_session(
            session_id="owned-session",
            surface="cli",
            config={},
            metadata={"live_session_id": "cli-taker"},
        )

    assert lease is not None and message is None
    entries = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in entries] == [
        (lease.lease_id, "owned-session")
    ]
    assert any(
        f"took over session owned-session from pid={os.getpid()} surface=desktop"
        in record.getMessage()
        for record in caplog.records
    )


def test_takeover_with_dead_holder_is_a_plain_acquire(tmp_path, monkeypatch, caplog):
    """A dead holder is already pruned by the normal claim path — the takeover
    must not log a steal against a corpse."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="dead-holder-session",
        surface="desktop",
        config={},
        metadata={"live_session_id": "dead-owner"},
    )
    assert holder is not None
    state_path = active_sessions._state_path()
    entries = active_sessions._read_entries(state_path)
    entries[0]["pid"] = 0x7FFFFFFE
    entries[0]["process_start_time"] = 1.0
    active_sessions._write_entries(state_path, entries)

    with caplog.at_level(logging.INFO, logger="hermes_cli.active_sessions"):
        lease, message = active_sessions.takeover_active_session(
            session_id="dead-holder-session",
            surface="cli",
            config={},
            metadata={"live_session_id": "cli-taker"},
        )

    assert lease is not None and message is None
    entries = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in entries] == [
        (lease.lease_id, "dead-holder-session")
    ]
    assert not any(
        "took over session" in record.getMessage() for record in caplog.records
    )


def test_takeover_of_own_live_session_replaces_without_steal_log(
    tmp_path, monkeypatch, caplog
):
    """A takeover re-claiming this process's own live session is re-entrancy, not a steal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    own, _ = active_sessions.try_acquire_active_session(
        session_id="own-session",
        surface="cli",
        config={},
        metadata={"live_session_id": "own-live"},
    )
    assert own is not None

    with caplog.at_level(logging.INFO, logger="hermes_cli.active_sessions"):
        lease, message = active_sessions.takeover_active_session(
            session_id="own-session",
            surface="cli",
            config={},
            metadata={"live_session_id": "own-live"},
        )

    assert lease is not None and message is None
    entries = active_sessions.active_session_registry_snapshot()
    assert [(entry["lease_id"], entry["session_id"]) for entry in entries] == [
        (lease.lease_id, "own-session")
    ]
    assert not any(
        "took over session" in record.getMessage() for record in caplog.records
    )


def test_takeover_fails_closed_on_unreadable_registry(tmp_path, monkeypatch):
    """An unprovable ownership state must not be collapsed into a steal."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    state_path = tmp_path / ".hermes" / "runtime" / "active_sessions.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not-json", encoding="utf-8")

    lease, message = active_sessions.takeover_active_session(
        session_id="any-session",
        surface="cli",
        config={},
        metadata={"live_session_id": "cli-taker"},
    )

    assert lease is None
    assert getattr(message, "reason", None) == (
        active_sessions.SESSION_COORDINATION_UNAVAILABLE
    )
    assert state_path.read_text(encoding="utf-8") == "{not-json"


def test_status_prints_live_owners_without_a_configured_cap(tmp_path, monkeypatch, capsys):
    """Exclusivity engages with no cap, so the holder table must not hide behind one."""
    from types import SimpleNamespace

    from hermes_cli import status as status_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(status_mod, "load_config", lambda: {}, raising=False)
    monkeypatch.setattr(
        status_mod, "get_hermes_home", lambda: tmp_path / ".hermes", raising=False
    )

    lease, _ = active_sessions.try_acquire_active_session(
        session_id="owned-session",
        surface="cli",
        config={},
        metadata={"live_session_id": "owner-live"},
    )
    assert lease is not None

    status_mod.show_status(SimpleNamespace(all=False, deep=False))

    output = capsys.readouterr().out
    assert "Live owners:" in output
    assert "owned-session" in output
    assert "cli" in output
    assert f"pid {os.getpid()}" in output


def test_prompt_submit_refusal_carries_reason_and_holder_fields(tmp_path, monkeypatch):
    """methods_prompt 4090 data propagation: the desktop payload keeps
    reason=SESSION_NOT_OWNED and adds the holder fields the refusal names."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    import tui_gateway.server as server

    from hermes_cli.active_sessions import try_acquire_active_session

    holder_lease, _ = try_acquire_active_session(
        session_id="owned-key",
        surface="cli",
        config={},
        metadata={"live_session_id": "foreign-owner"},
    )
    assert holder_lease is not None

    sid = "runtime-sid-owned"
    session = {
        "session_key": "owned-key",
        "title": "",
        "source": "desktop",
        "history_lock": threading.Lock(),
        "running": False,
    }
    server._sessions[sid] = session
    try:
        response = server._methods["prompt.submit"](
            "r1", {"session_id": sid, "text": "hi"}
        )
    finally:
        server._sessions.pop(sid, None)
        holder_lease.release()

    error = response["error"]
    assert error["code"] == 4090
    assert "already has a live owner" in error["message"]
    assert error["data"]["reason"] == active_sessions.SESSION_NOT_OWNED
    holder = error["data"]["holder"]
    assert holder["surface"] == "cli"
    assert holder["pid"] == os.getpid()
    assert holder["session_id"] == "owned-key"
    assert holder["started_at"] is not None
    assert holder["age_s"] is not None

