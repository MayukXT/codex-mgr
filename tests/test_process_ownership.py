"""Owned descendants stop; unrelated processes and foreground launches survive."""

import os
import signal
import subprocess
import sys
import time

import pytest

from codex_mgr.core import rpc
from codex_mgr.core.rpc import CodexReader, CodexRpcError
from codex_mgr.utils import processes
from codex_mgr.utils.processes import BackgroundProcessError, OwnedBackgroundProcess


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Synthetic process did not reach the expected state.")


def heartbeat_script(tmp_path):
    path = tmp_path / "worker.py"
    path.write_text(
        """import os,signal,subprocess,sys,time
from pathlib import Path
root=Path(sys.argv[1]); depth=int(sys.argv[2])
if os.name!='nt': signal.signal(signal.SIGTERM,signal.SIG_IGN)
if depth: subprocess.Popen([sys.executable,__file__,str(root),str(depth-1)],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=subprocess.DEVNULL)
with (root / ('heartbeat-'+str(depth))).open('ab',buffering=0) as stream:
 while True:
  stream.write(b'x'); time.sleep(.02)
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("orphan", [False, True])
def test_close_kills_owned_children_even_after_native_parent_exits(tmp_path, orphan):
    worker = heartbeat_script(tmp_path)
    owned_dir, unrelated_dir = tmp_path / "owned", tmp_path / "unrelated"
    owned_dir.mkdir()
    unrelated_dir.mkdir()
    server = tmp_path / "server.py"
    server.write_text(
        """import json,subprocess,sys
from pathlib import Path
worker,root,orphan=sys.argv[1:]
subprocess.Popen([sys.executable,worker,root,'1'],stdin=subprocess.DEVNULL,stdout=sys.stdout,stderr=subprocess.DEVNULL)
for line in sys.stdin:
 message=json.loads(line)
 if message['method']=='initialize':
  print(json.dumps({'id':message['id'],'result':{}}),flush=True)
 elif message['method']=='initialized' and orphan=='yes':
  Path(root,'parent-exited').touch(); break
""",
        encoding="utf-8",
    )
    unrelated = subprocess.Popen(
        [sys.executable, str(worker), str(unrelated_dir), "0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    reader = None
    try:
        reader = CodexReader(
            command=[
                sys.executable,
                str(server),
                str(worker),
                str(owned_dir),
                "yes" if orphan else "no",
            ]
        )
        wait_until(lambda: all((owned_dir / f"heartbeat-{depth}").exists() for depth in (0, 1)))
        wait_until(lambda: (unrelated_dir / "heartbeat-0").exists())
        if orphan:
            wait_until(lambda: (owned_dir / "parent-exited").exists())
        started = time.monotonic()
        reader.close()
        reader.close()  # No later kill by a stale PID/group identity.
        assert time.monotonic() - started < 5
        assert reader.process.poll() is not None and not reader.reader.is_alive()
        files = [owned_dir / f"heartbeat-{depth}" for depth in (0, 1)]
        sizes = [path.stat().st_size for path in files]
        unrelated_size = (unrelated_dir / "heartbeat-0").stat().st_size
        time.sleep(0.2)
        assert sizes == [path.stat().st_size for path in files]
        assert unrelated.poll() is None
        assert (unrelated_dir / "heartbeat-0").stat().st_size > unrelated_size
    finally:
        if reader:
            reader.close()
        unrelated.kill()
        unrelated.wait(timeout=3)


def test_interrupted_rpc_startup_closes_its_owned_tree(monkeypatch, tmp_path):
    owned = []

    def owner(command):
        result = OwnedBackgroundProcess(command, shutdown_timeout=0.1)
        owned.append(result)
        return result

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(rpc, "OwnedBackgroundProcess", owner)
    monkeypatch.setattr(CodexReader, "_request", interrupted)
    with pytest.raises(KeyboardInterrupt):
        CodexReader(command=[sys.executable, "-c", "import time; time.sleep(60)"])
    assert owned[0].process.poll() is not None
    assert owned[0].process.stdin.closed and owned[0].process.stdout.closed


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object startup gate.")
@pytest.mark.parametrize("failure", [BackgroundProcessError, KeyboardInterrupt])
def test_job_assignment_failure_never_starts_target(monkeypatch, tmp_path, failure):
    children = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    def denied(*args):
        raise failure("Synthetic ownership failure.")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(processes._WindowsJob, "assign", denied)
    marker = tmp_path / "unexpected-target"
    with pytest.raises(failure):
        OwnedBackgroundProcess(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ]
        )
    assert not marker.exists()
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed and children[0].stderr.closed


def test_control_reader_start_failure_cleans_supervisor_before_gate(monkeypatch, tmp_path):
    children = []
    original = subprocess.Popen

    def popen(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    def denied(*args):
        raise RuntimeError("Synthetic thread start failure.")

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(processes.threading.Thread, "start", denied)
    marker = tmp_path / "unexpected-target"
    with pytest.raises(RuntimeError):
        OwnedBackgroundProcess(
            [
                sys.executable,
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()",
                str(marker),
            ]
        )
    assert not marker.exists()
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdin.closed and children[0].stdout.closed and children[0].stderr.closed


def test_full_rpc_queue_does_not_delay_reader_cleanup(tmp_path):
    server = tmp_path / "server.py"
    server.write_text(
        """import json,sys
for line in sys.stdin:
 message=json.loads(line)
 if message['method']=='initialize':
  print(json.dumps({'id':message['id'],'result':{}}),flush=True)
 elif message['method']=='initialized':
  for i in range(256): print(json.dumps({'method':'synthetic/notification','params':{}}),flush=True)
""",
        encoding="utf-8",
    )
    reader = CodexReader(command=[sys.executable, str(server)])
    try:
        wait_until(reader.messages.full)
    finally:
        reader.close()
    assert not reader.reader.is_alive() and reader.process.stdout.closed


def test_missing_background_command_has_generic_error(tmp_path):
    with pytest.raises(CodexRpcError) as error:
        CodexReader(command=[str(tmp_path / "synthetic-private-command")], timeout=2)
    assert "synthetic-private" not in str(error.value)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group leader.")
def test_supervisor_stays_alive_between_group_signals():
    owner = OwnedBackgroundProcess(
        [
            sys.executable,
            "-c",
            "import time; print('ready',flush=True); time.sleep(60)",
        ],
        shutdown_timeout=0.1,
    )
    try:
        assert owner.process.stdout.readline().strip() == "ready"
        os.killpg(owner.process.pid, signal.SIGTERM)
        assert owner._done.wait(timeout=3)  # Target gets ordinary SIGTERM.
        assert owner.process.poll() is None  # Leader remains allocated/alive.
    finally:
        owner.close()
        owner.process.stdout.close()
    assert owner.process.poll() == -signal.SIGKILL
