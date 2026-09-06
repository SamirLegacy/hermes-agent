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



def _backdate_leases(*homes, age_seconds=600.0):
    """Age every lease in the given registries past the self-orphan grace."""
    for home in homes:
        state_path = active_sessions._state_path(home)
        entries = active_sessions._read_entries(state_path)
        for entry in entries:
            entry["started_at"] = time.time() - age_seconds
        active_sessions._write_entries(state_path, entries)


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

    _backdate_leases(tmp_path / ".hermes")
    assert active_sessions.release_orphaned_leases({kept.lease_id, "elsewhere"}) == 1
    assert sorted(
        entry["session_id"]
        for entry in active_sessions.active_session_registry_snapshot()
    ) == ["kept", "other"]
    assert orphan is not None


def test_release_orphaned_leases_sweeps_profile_runtime_registries(
    tmp_path, monkeypatch
):
    root = tmp_path / "hermes"
    profile = root / "profiles" / "worker"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))

    root_lease, root_error = active_sessions.try_acquire_active_session(
        session_id="root-orphan", surface="desktop", config={}, registry_home=root
    )
    profile_lease, profile_error = active_sessions.try_acquire_active_session(
        session_id="profile-orphan",
        surface="desktop",
        config={},
        registry_home=profile,
    )
    assert root_lease is not None and root_error is None
    assert profile_lease is not None and profile_error is None

    # A lease written seconds ago is never an orphan: a sibling finalize that
    # snapshotted its live ids before this acquire must not reap it (#101415).
    assert active_sessions.release_orphaned_leases(set()) == 0
    _backdate_leases(root, profile)
    assert active_sessions.release_orphaned_leases(set()) == 2
    assert active_sessions.active_session_registry_snapshot(root) == []
    assert active_sessions.active_session_registry_snapshot(profile) == []


def test_drop_self_orphans_spares_foreign_and_vouched_leases():
    own = os.getpid()
    entries = [
        {"lease_id": "orphan", "pid": own},
        {"lease_id": "live", "pid": own},
        {"lease_id": "foreign", "pid": own + 1},
    ]

    assert active_sessions._drop_self_orphans(entries, None) == entries
    assert active_sessions._drop_self_orphans(entries, {"live"}) == entries[1:]


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



def test_liveness_guard_keeps_a_just_acquired_own_lease_it_cannot_vouch_for(
    tmp_path, monkeypatch
):
    """Race in #101415's fix: the finalizing session snapshots its live lease
    ids, then a sibling session acquires a lease before the registry lock is
    taken. That lease is absent from the snapshot but is not an orphan."""
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    fresh, error = active_sessions.try_acquire_active_session(
        session_id="fresh", surface="desktop", config={}, registry_home=home
    )
    assert fresh is not None and error is None

    with active_sessions.active_session_liveness_guard(
        "fresh", registry_home=home, own_live_lease_ids=set()
    ) as active:
        assert active is True
    assert [e["lease_id"] for e in active_sessions.active_session_registry_snapshot(home)] == [fresh.lease_id]

    _backdate_leases(home)
    with active_sessions.active_session_liveness_guard(
        "fresh", registry_home=home, own_live_lease_ids=set()
    ) as active:
        assert active is False
    assert active_sessions.active_session_registry_snapshot(home) == []


def test_refusal_message_names_holder_age_clock_and_next_steps(tmp_path, monkeypatch):
    """The refusal is the ONLY operator-facing surface when a session is locked —
    it must say who holds it (surface, pid), for how long (age), since when
    (wall clock), and what to do next."""
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
    # Live holder: quit-first guidance, never a --takeover advertisement.
    assert "Quit that surface first" in message
    assert "--takeover" not in message
    assert "hermes status" in message


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
    assert payload["holder_live"] is True


def _write_dead_holder(tmp_path, monkeypatch, session_id="dead-holder-session"):
    """Registry entry whose pid is dead AND whose process start-time mismatches —
    the only holder class --takeover may reclaim."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id=session_id,
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
    return holder


def test_takeover_with_dead_holder_is_a_plain_acquire(tmp_path, monkeypatch, caplog):
    """A dead holder is already pruned by the normal claim path — the takeover
    must not log a steal against a corpse."""
    _write_dead_holder(tmp_path, monkeypatch, session_id="owned-session")

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
    assert not any("took over session" in record.getMessage() for record in caplog.records)


def test_takeover_refuses_live_holder(tmp_path, monkeypatch):
    """--takeover must NEVER steal from a live holder: the live surface keeps its
    in-memory lease, so stealing the registry entry would leave two writers."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="live-owned",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert holder is not None

    lease, refusal = active_sessions.takeover_active_session(
        session_id="live-owned",
        surface="cli",
        config={},
        metadata={"live_session_id": "cli-taker"},
    )

    assert lease is None
    assert refusal is not None
    assert refusal.reason == active_sessions.SESSION_NOT_OWNED
    assert "is alive" in str(refusal)
    assert refusal.holder is not None
    assert refusal.holder["holder_live"] is True
    # The registry still names the original holder.
    entries = active_sessions.active_session_registry_snapshot()
    assert [e["session_id"] for e in entries] == ["live-owned"]
    assert entries[0]["lease_id"] == holder.lease_id


def test_takeover_refuses_unverifiable_holder(tmp_path, monkeypatch):
    """Liveness None (cannot prove dead) is treated as live: fail closed."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    holder, _ = active_sessions.try_acquire_active_session(
        session_id="unknown-owned",
        surface="desktop",
        config={},
        metadata={"live_session_id": "desktop-owner"},
    )
    assert holder is not None
    state_path = active_sessions._state_path()
    entries = active_sessions._read_entries(state_path)
    entries[0]["pid"] = None  # unverifiable: no pid to probe
    entries[0]["process_start_time"] = None
    active_sessions._write_entries(state_path, entries)

    lease, refusal = active_sessions.takeover_active_session(
        session_id="unknown-owned",
        surface="cli",
        config={},
        metadata={"live_session_id": "cli-taker"},
    )

    # Fail closed either way: under this module's strict pruning an
    # unverifiable holder makes the ownership state unprovable, which surfaces
    # as SESSION_COORDINATION_UNAVAILABLE — refused, never stolen.
    assert lease is None
    assert refusal is not None
    assert refusal.reason == active_sessions.SESSION_COORDINATION_UNAVAILABLE


def test_takeover_of_own_live_session_replaces_without_steal_log(tmp_path, monkeypatch, caplog):
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
    assert [e["session_id"] for e in entries] == ["own-session"]
    assert not any("took over session" in r.getMessage() for r in caplog.records)


def test_takeover_unreadable_registry_fails_closed(tmp_path, monkeypatch):
    """An unprovable ownership state must not collapse into a go-ahead."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    state_path = active_sessions._state_path()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not json")

    lease, refusal = active_sessions.takeover_active_session(
        session_id="any-session",
        surface="cli",
        config={},
    )

    assert lease is None
    assert refusal is not None
    assert refusal.reason == active_sessions.SESSION_COORDINATION_UNAVAILABLE
