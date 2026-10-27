"""Route object fixtures into a real existing bucket without rewriting SDK responses."""
from copy import deepcopy
from urllib.parse import quote,unquote
from .scenarios import require_ok
from .suites import CaseNotRun

LIST_OPERATIONS={'list','list_page','list_v1_page','list_v2_observed','versions_list','mp_list'}
WRITES={'put','put_acl','put_encryption','put_sse_c','put_http','put_metadata','put_version',
        'copy','copy_replace','mp_create','mp_create_metadata','mp_create_sse_c','mp_create_tagging',
        'mp_complete','mp_complete_observed','atomic_stream'}


def physical_key(scope,value):
    if not isinstance(value,str):raise ValueError('object key must be a string')
    if value.startswith(scope.prefix):return value
    if value.startswith(scope.probe.ctx.prefix):
        raise ValueError('object key escapes this scope')
    result=scope.prefix+value
    if len(value.encode('utf-8'))<=1024<len(result.encode('utf-8')):
        raise CaseNotRun('The original key-length boundary cannot fit an additional isolation prefix',
                         'dedicated bucket for exact object key length boundary')
    return result


def _prepare_request(scope,op,options):
    """Change only request addressing; retain actual keys, tokens, errors and response bodies."""
    if 'bucket' in options:raise ValueError('cannot override the owned object scope')
    if op.startswith('bucket_') or op in {'versioning_put'} or op.startswith(('api.PutBucket','api.DeleteBucket','api.PutPublicAccessBlock','api.DeletePublicAccessBlock','api.PutObjectLockConfiguration')):
        raise CaseNotRun('Bucket mutation is prohibited in an existing-bucket object scope','isolated bucket management permission')
    if op=='atomic_stream' and options.get('mode') in ('bucket_gone','versioned_overlap'):
        raise CaseNotRun('This SDK callback mutates bucket state','isolated bucket management permission')
    q=deepcopy(options)
    if 'key' in q:q['key']=physical_key(scope,q['key'])
    if op in LIST_OPERATIONS:
        q['prefix']=physical_key(scope,q.get('prefix',''))
        for field in ('marker','start_after','key_marker'):
            if q.get(field):q[field]=physical_key(scope,q[field])
    if 'source_key' in q:
        # Cross-bucket cases keep their explicit fresh-bucket prerequisite; same-scope copies stay real.
        if q.get('source_bucket',scope.name)!=scope.name:
            raise CaseNotRun('Cross-bucket source is not a configured object scope','independent source test bucket')
        q['source_key']=physical_key(scope,q['source_key'])
    for item in q.get('objects',[]):
        item['key']=physical_key(scope,item['key'])
    if isinstance(q.get('input'),dict):
        data=q['input']
        if data.get('Bucket',scope.name)!=scope.name:raise ValueError('cannot override the owned API bucket')
        if scope.name is not None:data['Bucket']=scope.name
        if 'Key' in data:data['Key']=physical_key(scope,data['Key'])
        if op.startswith(('api.ListObjects','api.ListObjectVersions','api.ListMultipartUploads')):
            data['Prefix']=physical_key(scope,data.get('Prefix',''))
            for field in ('Marker','StartAfter','KeyMarker'):
                if data.get(field):data[field]=physical_key(scope,data[field])
        if 'CopySource' in data:
            source=unquote(data['CopySource']).lstrip('/')
            bucket,separator,key=source.partition('/')
            if not separator or bucket!=scope.name:
                raise CaseNotRun('CopySource requires an independently configured source bucket','source test bucket')
            data['CopySource']=bucket+'/'+physical_key(scope,key)
        for item in data.get('Delete',{}).get('Objects',[]):item['Key']=physical_key(scope,item['Key'])
    nested=[]
    for child in q.get('requests',[]):
        # Concurrent operations must enter this same validation/mapping path, not bypass it.
        if child.get('bucket',scope.name)!=scope.name:raise ValueError('nested request bucket escape')
        child=dict(child);child.pop('bucket',None)
        child_op=child.pop('op')
        if child_op=='concurrent_requests' or 'requests' in child:raise ValueError('nested request batches are prohibited')
        nested.append(_prepare_request(scope,child_op,child))
    if 'requests' in q:q['requests']=nested
    q.update(op=op)
    if scope.name is not None:q['bucket']=scope.name
    if op in WRITES and 'key' in q:scope.written_keys.add(q['key'])
    if op.startswith(('api.Put','api.Copy')) and 'Key' in q.get('input',{}):scope.written_keys.add(q['input']['Key'])
    if op.startswith('mp_create') or op=='atomic_stream' and 'multipart' in q.get('mode',''):
        scope.multipart_attempted=True
    return q


def request(scope,op,options):
    """Send the validated physical request and return the SDK result unchanged."""
    q=_prepare_request(scope,op,options)
    result=scope.probe.ctx.worker.call(q)
    if result.get('ok') and 'key' in q:
        if result.get('upload_id'):scope.uploads[result['upload_id']]=q['key']
        if 'version_id' in result:scope.version_keys.setdefault(result['version_id'],set()).add(q['key'])
    return result


def enter(scope):
    state=scope.probe.ctx.scope_state
    if state is not None and state.get('active_object_scopes'):
        raise CaseNotRun('The original contract uses two distinct buckets; one configured bucket cannot prove cross-bucket behavior',
                         'second independently configured test bucket')
    initial=scope.call('list',prefix='')['keys']
    if initial:raise RuntimeError('generated object prefix is not empty; refusing reuse')
    worker=scope.probe.ctx.worker
    if 'versioning_get' in worker.capabilities:
        status=scope.call('versioning_get').get('versioning_status')
        scope.versioned=status in ('Enabled','Suspended')
        if scope.versioned and not {'versions_list','delete_version'}<=worker.capabilities:
            raise CaseNotRun('Versioned existing bucket requires prefix-scoped history cleanup','versions_list and delete_version')
    if state is not None:state['active_object_scopes']=state.get('active_object_scopes',0)+1
    return scope


def cleanup_objects(scope):
    if scope.versioned:
        from .version_scope import VersionScope
        return VersionScope.cleanup_objects(scope)
    failures=[]
    worker=scope.probe.ctx.worker
    def remove(keys):
        # Delete original opaque keys before LIST: XML parsers cannot represent every valid HTTP object key.
        ordinary=[]
        for key in sorted(keys):
            if 'delete_many' in worker.capabilities and all(ord(c)>=32 and ord(c) not in (0xfffe,0xffff) for c in key):
                ordinary.append(key);continue
            result=scope.raw('delete',key=key)
            if not result.get('ok'):failures.append(result)
        for start in range(0,len(ordinary),1000):
            batch=ordinary[start:start+1000]
            result=scope.raw('delete_many',objects=[{'key':key} for key in batch],quiet=True)
            if not result.get('ok') or result.get('errors'):
                failures.append(result)
                # A cleanup-only fallback cannot turn a measured/case operation into success.
                for key in batch:
                    deleted=scope.raw('delete',key=key)
                    if not deleted.get('ok'):failures.append(deleted)
    remove({physical_key(scope,key) for key in scope.written_keys})
    listed=scope.call('list',prefix='')['keys']
    if any(not isinstance(key,str) or not key.startswith(scope.prefix) for key in listed):
        raise RuntimeError('refusing cleanup outside object scope')
    remove(set(listed))
    return failures


def exit_scope(scope,error):
    try:
        if getattr(scope,'multipart_attempted',False) and 'mp_list' in scope.probe.ctx.worker.capabilities:
            listing=scope.call('mp_list')
            if listing.get('truncated'):raise RuntimeError('multipart cleanup inventory is truncated')
            for row in listing['uploads']:
                if not row['key'].startswith(scope.prefix):raise RuntimeError('multipart upload escaped object prefix')
                scope.uploads[row['upload_id']]=row['key']
        for ident,key in scope.uploads.items():
            op='mp_abort_raw' if 'mp_abort_raw' in scope.probe.ctx.worker.capabilities else 'mp_abort'
            response=scope.raw(op,key=key,upload_id=ident)
            if not response.get('ok') and response.get('status_code')!=404:
                raise RuntimeError('object-scope multipart cleanup failed: '+str(response))
        failures=scope.cleanup_objects()
        remaining=scope.call('list',prefix='')['keys']
        if remaining:raise RuntimeError('object prefix cleanup not confirmed: '+str(remaining)+'; '+str(failures))
        scope.cleanup_completed=True
    except Exception as cleanup_error:
        raise RuntimeError((str(error)+'; ' if error else '')+'object prefix cleanup failed for '+scope.prefix+': '+str(cleanup_error)) from cleanup_error
    finally:
        if scope.probe.ctx.scope_state is not None:
            state=scope.probe.ctx.scope_state
            state['active_object_scopes']=max(0,state.get('active_object_scopes',0)-1)
    return False
