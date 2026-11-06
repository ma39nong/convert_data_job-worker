import json
import threading
import urllib.request
from test_subtasks import dataset
from convest.subtasks.server import Server


def test_saved_export_does_not_wait_for_model_job(tmp_path,monkeypatch):
    ep=dataset(tmp_path/'data');server=Server(('127.0.0.1',0),[ep.root],tmp_path/'review')
    entered=threading.Event();release=threading.Event()
    def model():entered.set();release.wait(5);return {'episodes':[]}
    job=server.submit(model,lock_store=False)['job'];assert entered.wait(1)
    # Exercise real file export while an unrelated model job owns the job executor.
    stage=server.workspace.stages(ep)['coarse'];saved=server.workspace.save_stage(ep,'coarse',stage)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        body={'id':ep.id,'stage':'coarse','revision':saved['revision'],'target':'local','root':str(tmp_path/'chosen'),'kind':'json'}
        req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/workspace/export-direct',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Subtasks-Token':server.token})
        result=json.load(urllib.request.urlopen(req,timeout=3))['result']
        assert result['host']=='local' and result['stage']=='coarse'
        assert server.jobs[job]['state']=='running'
        assert not server.export_lock.locked()
        # Failure also releases the independent export slot.
        body['revision']=-1;req.data=json.dumps(body).encode()
        try:urllib.request.urlopen(req,timeout=3)
        except urllib.error.HTTPError as error:assert error.code==400
        else:raise AssertionError('Stale export should fail')
        assert not server.export_lock.locked()
    finally:release.set();server.shutdown();server.server_close();worker.join()
