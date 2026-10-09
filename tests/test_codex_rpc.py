"""Exercise stdio handshake, paging, read-only enforcement and child cleanup."""

import sys

import pytest

from codex_mgr.core.rpc import CodexReader, CodexRpcError


def test_rpc_handshake_paging_and_no_mutation(tmp_path):
    script = tmp_path / "server.py"
    script.write_text("""import json,sys
initialized=False
for line in sys.stdin:
 message=json.loads(line)
 if message['method']=='initialize':
  print(json.dumps({'id':message['id'],'result':{'userAgent':'synthetic'}}),flush=True)
 elif message['method']=='initialized': initialized=True
 elif message['method']=='model/list':
  assert initialized
  cursor=message['params'].get('cursor')
  result={'data':[{'id':'second' if cursor else 'first'}],'nextCursor':None if cursor else 'next'}
  print(json.dumps({'method':'unrelated/notification','params':{}}),flush=True)
  print(json.dumps({'id':message['id'],'result':result}),flush=True)
 else:
  print(json.dumps({'id':message['id'],'error':{'message':'synthetic-private'}}),flush=True)
""")
    with CodexReader(command=[sys.executable, str(script)], timeout=2) as reader:
        assert [m["id"] for m in reader.pages("model/list")] == ["first", "second"]
        with pytest.raises(CodexRpcError):
            reader.request("turn/start")
        with pytest.raises(CodexRpcError) as error:
            reader.request("config/read")
        assert "synthetic-private" not in str(error.value)
    assert reader.process.poll() is not None


def test_unresponsive_server_is_terminated(tmp_path):
    script = tmp_path / "server.py"
    script.write_text("import time\ntime.sleep(20)\n")
    with pytest.raises(CodexRpcError, match="timed out"):
        CodexReader(command=[sys.executable, str(script)], timeout=0.1)
