"""Independent file operations; preparation, verification and cleanup are untimed."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import random
import time
import threading

from .scenarios import require_ok
from .benchmark import sync_inputs
from .worker import WorkerUnavailable

MIB = 1024**2
MIN_SECONDS = 5.0
MAX_WINDOW_BYTES = 256*MIB
MAX_ROUND_BYTES = 16*1024*MIB
DEFAULTS = dict(sizes=[4096, 4*MIB, 64*MIB], range_sizes=[65536, MIB, 4*MIB],
                range_object_size=64*MIB, list_counts=[10, 1001], delete_counts=[1, 100, 1001],
                multipart_sizes=[32*MIB, 64*MIB], part_sizes=[5*MIB, 16*MIB])


def normalize(value):
    """Validate a bounded size matrix before starting any SDK or creating objects."""
    if not isinstance(value, dict) or set(value)-set(DEFAULTS):
        raise ValueError('files contains unknown parameters')
    result = {**DEFAULTS, **value}
    for name, values in result.items():
        values = [values] if name == 'range_object_size' else values
        limit = 10000 if name in ('list_counts', 'delete_counts') else 256*MIB
        if (not isinstance(values, list) or not 1 <= len(values) <= 8
                or any(type(v) is not int or not 1 <= v <= limit for v in values) or len(set(values)) != len(values)):
            raise ValueError('invalid files.'+name)
    if max(result['range_sizes']) > result['range_object_size']:
        raise ValueError('files range exceeds source object')
    if min(result['part_sizes']) < 5*MIB:
        raise ValueError('files part_sizes must be at least 5 MiB')
    return result


def inventory(workload):
    """Every run includes all eight operations; concurrency and sizes are separate results."""
    f = workload['files']
    specs = [('head', {'size': f['sizes'][0]}, {'put','head'})]
    for action in ('get', 'put', 'copy'):
        specs += [(action, {'size': size}, {'put','get',action}) for size in f['sizes']]
    specs += [('range', {'size': f['range_object_size'], 'length': length, 'position': position}, {'put','get'})
              for length in f['range_sizes'] for position in ('head','middle','tail')]
    specs += [('multipart', {'size': size, 'part_size': part}, {'multipart_upload','put','get'})
              for size in f['multipart_sizes'] for part in f['part_sizes']]
    specs += [('list', {'size': 1, 'count': count}, {'put','list'}) for count in f['list_counts']]
    specs += [('delete', {'size': 1, 'count': 1}, {'put','delete','list'})]
    specs += [('delete_many', {'size': 1, 'count': count}, {'put','delete_files','list'}) for count in f['delete_counts']]
    for concurrency in dict.fromkeys((1, workload['concurrency'])):
        for action, parameters, required in specs:
            label = action+'_'+ '_'.join(str(v) for v in parameters.values())+f'_c{concurrency}'
            yield {'name': label, 'action': action, 'parameters': parameters, 'concurrency': concurrency,
                   'required': sorted(required | {'list','delete'}),
                   'metric': 'verified_mib_s' if action in ('get','range','put','copy','multipart') else
                             'verified_objects_s' if action in ('list','delete_many') else 'verified_ops_s'}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def iter_files(ctx, workers, report, target, phase, round_index):
    """Run one independent case at a time, retaining failed and unsupported evidence."""
    workload = ctx.workload
    if target in report.get('performance_stopped', {}):return
    count = workload['warmup_requests'] if phase == 'warmup' else workload['requests']
    if count == 0:
        return
    for case in inventory(workload):
        name, action, p = case['name'], case['action'], case['parameters']
        try:
            for worker in workers:worker.ensure_alive()
        except WorkerUnavailable as error:
            stop_target(report,target,str(error),workload)
            return
        count=max(count,case['concurrency'])
        missing = set(case['required']) - set.intersection(*(set(w.capabilities) for w in workers))
        if missing:
            if phase == 'measure' and round_index == 0:
                report.setdefault('performance_not_run', []).append({'target': target, 'op': name,
                    'reason': '驱动未提供操作：'+', '.join(sorted(missing))})
                report['execution_errors'].append({'target':target,'phase':'performance_setup','workload':action,
                    'error':name+': unsupported worker operations: '+', '.join(sorted(missing))})
            yield name
            continue
        report['operation_metrics'][name] = case['metric']
        directory = ctx.directory/name
        directory.mkdir(exist_ok=True)
        prefix = ctx.prefix+f'{phase}/{round_index}/{name}/'
        source = directory/'input'
        # One bounded file shared by all lanes; no dataset is placed in tmpfs.
        source.write_bytes(random.Random(workload['seed']).randbytes(p['size']))
        sync_inputs([source])
        expected_hash = digest(source)
        part_paths = []
        if action == 'multipart':
            with source.open('rb') as stream:
                while data := stream.read(p['part_size']):
                    path = directory/f'part-{len(part_paths)+1}'
                    path.write_bytes(data); part_paths.append(str(path))
            sync_inputs(map(Path,part_paths))
        offset = {'head':0,'middle':(p['size']-p.get('length',0))//2,'tail':p['size']-p.get('length',0)}.get(p.get('position'),0)
        if action == 'range':
            with source.open('rb') as stream:
                stream.seek(offset); expected_hash=hashlib.sha256(stream.read(p['length'])).hexdigest()
        done, measured = 0, 0.0
        minimum = MIN_SECONDS if phase == 'measure' and workers[0].info.get('api') != 'local' and action in ('get','range','put','copy','multipart') else 0
        # Every stored write/read result is verified; bound both temporary storage and auto-extension.
        transfer_size=p.get('length',p['size'])
        request_limit=65536 if action in ('get','range') else 4096
        limit = max(count, min(max(1,request_limit//p.get('count',1)), MAX_ROUND_BYTES//max(1,transfer_size)))
        window_count = max(1,max(case['concurrency'], min(128, MAX_WINDOW_BYTES//max(1,transfer_size), count)))
        rows = []
        listing_keys=[]
        source_keys=[]
        terminal_reason=None
        try:
            if action in ('get','range','head','copy'):
                for slot in range(case['concurrency']):
                    key=prefix+f'source/{slot}'
                    require_ok(workers[slot].call({'op':'put','key':key,'body_path':str(source)}))
                    source_keys.append(key)
            if action=='list':
                listing_keys=[prefix+f'listing/{j}' for j in range(p['count'])]
                for key in listing_keys:require_ok(workers[0].call({'op':'put','key':key,'body_path':str(source)}))
            with ThreadPoolExecutor(max_workers=case['concurrency']) as executor:
                while done < count or (measured < minimum and done < limit):
                    n = min(window_count, (count-done) if done<count else limit-done)
                    jobs=[]; cleanup=[]
                    for i in range(n):
                        key=prefix+f'{done+i}/object'; out=directory/f'output-{i}'
                        q={'op':action,'key':key}
                        expected_keys=[]
                        if action in ('get','range','head','copy'):
                            original=source_keys[i % case['concurrency']]
                            if action!='copy':q['key']=original
                        if action in ('put','multipart'):
                            q.update(body_path=str(source));cleanup.append(key)
                        if action=='multipart':q.update(op='multipart_upload',part_paths=part_paths)
                        if action=='copy':q.update(source_key=original);cleanup.append(key)
                        if action in ('get','range'):
                            q.update(op='get',output_path=str(out))
                            if action=='range':q.update(offset=offset,length=p['length'])
                        if action=='list':
                            q={'op':'list','prefix':prefix+'listing/'};expected_keys=listing_keys
                        if action in ('delete','delete_many'):
                            for j in range(p['count']):
                                item=key+f'/{j}' if action!='delete' else key
                                require_ok(workers[0].call({'op':'put','key':item,'body_path':str(source)}))
                                expected_keys.append(item);cleanup.append(item)
                            if action=='delete_many':q={'op':'delete_files','objects':[{'key':k} for k in expected_keys],'quiet':True}
                        jobs.append((q,out,expected_keys))
                    # Assign each lane a serial queue so its process is never raced; threads exist before timing.
                    ready=threading.Barrier(case['concurrency']+1)
                    start_gate=threading.Event()
                    def lane(slot):
                        ready.wait();start_gate.wait()
                        results=[]
                        for i in range(slot,len(jobs),case['concurrency']):
                            q,out,expected_keys=jobs[i]
                            row={'target':target,'op':name,'phase':phase,'round':round_index,'ok':False,'bytes':0,
                                 'elapsed_ns':0,'timing_scope':'file_operation','key':q.get('key',''), 'concurrency':case['concurrency']}
                            started=time.monotonic_ns()
                            try:
                                response=workers[slot].call(q);row['elapsed_ns']=response['elapsed_ns'];require_ok(response)
                                row['ok']=True
                            except Exception as error:
                                response={};row['error']=str(error)
                            row['rpc_wall_ns']=time.monotonic_ns()-started
                            results.append((i,row,response))
                            if workers[slot].process.poll() is not None or workers[slot]._terminal_error is not None:break
                        return results
                    futures=[executor.submit(lane,slot) for slot in range(case['concurrency'])]
                    ready.wait()
                    started=time.monotonic();start_gate.set()
                    observed=[item for future in futures for item in future.result()]
                    elapsed=time.monotonic()-started;measured+=elapsed
                    # Verification begins only after every timed request has finished.
                    for i,row,response in observed:
                        q,out,expected_keys=jobs[i]
                        try:
                            if row['ok']:
                                if action in ('put','copy','multipart'):
                                    require_ok(workers[0].call({'op':'get','key':q['key'],'output_path':str(out)}))
                                if action in ('get','range','put','copy','multipart'):
                                    size=p['length'] if action=='range' else p['size']
                                    if out.stat().st_size!=size or digest(out)!=expected_hash:raise AssertionError('file content/length differs')
                                    row['bytes']=size
                                elif action=='head':
                                    if response['size']!=p['size']:raise AssertionError('HEAD size differs')
                                elif action=='list':
                                    if sorted(response.get('keys',[]))!=sorted(expected_keys):raise AssertionError('listing differs (missing, extra or duplicate keys)')
                                    row['objects']=len(expected_keys)
                                else:
                                    if response.get('errors'):raise AssertionError('batch delete returned errors: '+str(response['errors']))
                                    remaining=require_ok(workers[0].call({'op':'list','prefix':prefix+f'{done+i}/'}))['keys']
                                    if remaining:raise AssertionError('deleted objects still present')
                                    row['objects']=len(expected_keys)
                        except Exception as error:
                            row.update(ok=False,bytes=0,objects=0,error=str(error))
                        finally:out.unlink(missing_ok=True)
                        rows.append(row)
                    for key in cleanup:require_ok(workers[0].call({'op':'delete','key':key}))
                    done+=n
                    if any(not row['ok'] for _,row,_ in observed):break
        except WorkerUnavailable as error:
            terminal_reason=str(error)
        except Exception as error:
            report['execution_errors'].append({'target':target,'phase':'file_case','workload':name,'error':str(error)})
        finally:
            report['samples'].extend(rows)
            if terminal_reason:stop_target(report,target,terminal_reason,workload)
            report['batches'].append({'target':target,'op':name,'phase':phase,'round':round_index,
                'wall_seconds':measured,'duration_satisfied':measured>=minimum,'effective_lanes':case['concurrency'],
                'minimum_seconds':minimum,'sample_limit':limit,'operation_bytes':transfer_size,
                'verification_outside_timing':True})
            for path in directory.iterdir():path.unlink()
            directory.rmdir()
            try:
                for worker in workers:worker.ensure_alive()
                for key in listing_keys+source_keys:require_ok(workers[0].call({'op':'delete','key':key}))
            except WorkerUnavailable as error:
                stop_target(report,target,str(error),workload)
            except Exception as error:
                report['execution_errors'].append({'target':target,'phase':'file_cleanup','workload':name,'error':str(error)})
        if target in report.get('performance_stopped',{}):return
        yield name


def stop_target(report,target,reason,workload):
    """Record one terminal failure and explicitly account for unexecuted operations."""
    stopped=report.setdefault('performance_stopped',{})
    if target in stopped:return
    stopped[target]=reason
    report['execution_errors'].append({'target':target,'phase':'worker_exit','error':reason})
    measured={s['op'] for s in report['samples'] if s['target']==target and s['phase']=='measure'}
    for case in inventory(workload):
        if case['name'] not in measured:
            report.setdefault('performance_not_run',[]).append({'target':target,'op':case['name'],'reason':'SDK 驱动失效：'+reason})
