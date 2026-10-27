"""Route object fixtures into a real existing bucket without rewriting SDK responses."""
from copy import deepcopy
from urllib.parse import quote,unquote
from .scenarios import require_ok
from .suites import CaseNotRun

LIST_OPERATIONS={'list','list_page','list_v1_page','list_v2_observed','versions_list','mp_list'}
WRITES={'put','put_acl','put_encryption','put_sse_c','put_http','put_metadata','put_version',
        'copy','copy_replace','mp_create','mp_create_metadata','mp_create_sse_c','mp_create_tagging',
        'mp_complete','mp_complete_observed','atomic_stream'}


def observe_version(scope,response):
    """Use actual object response metadata instead of requiring bucket configuration permission."""
    version=response.get('version_id')
    marker=False
    for name,value in response.get('http_headers',{}).items():
        value=','.join(value) if isinstance(value,list) else str(value)
        if name.lower()=='x-amz-version-id':version=value
        if name.lower()=='x-amz-delete-marker' and value.lower()=='true':marker=True
    if version is not None or marker:scope.versioned=True
    # Missing VersionId cannot distinguish never-versioned from suspended null versions.
    return response


def inspect_object_version(scope,key,options=None):
    options=options or {}
    worker=scope.probe.ctx.worker
    if 'sse_headers' in options and 'head_sse_c' in worker.capabilities:
        request={'op':'head_sse_c','key':key,'sse_headers':options['sse_headers']}
    elif 'head_version' in worker.capabilities:
        request={'op':'head_version','key':key}
    else:return
    if scope.name is not None:request['bucket']=scope.name
    result=worker.call(request)
    observe_version(scope,result)
    if result.get('status_code')==404 and scope.versioned is None:
        scope.absent_keys.add(key)
    return result


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
    if op in {'delete','delete_many','delete_many_versions','delete_version'} or op.startswith('api.DeleteObject'):
        scope.deletion_attempted=True
        if 'key' in q:scope.written_keys.add(q['key'])
        if 'Key' in q.get('input',{}):scope.written_keys.add(q['input']['Key'])
        for item in q.get('objects',[]):scope.written_keys.add(item['key'])
        for item in q.get('input',{}).get('Delete',{}).get('Objects',[]):scope.written_keys.add(item['Key'])
    if op in WRITES and 'key' in q:scope.written_keys.add(q['key'])
    if op.startswith(('api.Put','api.Copy')) and 'Key' in q.get('input',{}):scope.written_keys.add(q['input']['Key'])
    if op.startswith('mp_create') or op=='atomic_stream' and 'multipart' in q.get('mode',''):
        scope.multipart_attempted=True
    return q


def request(scope,op,options):
    """Send the validated physical request and return the SDK result unchanged."""
    q=_prepare_request(scope,op,options)
    result=scope.probe.ctx.worker.call(q)
    if scope.versioned is None and result.get('ok') and op in WRITES and 'key' in q and not op.startswith('mp_create'):
        # A plain PUT may omit VersionId in the adapter result; HEAD uses object-read permission only.
        observe_version(scope,result) if 'version_id' in result else inspect_object_version(scope,q['key'],q)
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
        configured=scope.raw('versioning_get')
        scope.versioned=configured.get('versioning_status') in ('Enabled','Suspended') if configured.get('ok') else None
        if scope.versioned and not {'versions_list','delete_version'}<=worker.capabilities:
            raise CaseNotRun('Versioned existing bucket requires prefix-scoped history cleanup','versions_list and delete_version')
    if state is not None:state['active_object_scopes']=state.get('active_object_scopes',0)+1
    return scope


def cleanup_objects(scope):
    if scope.versioned is None:
        for key in scope.written_keys:
            inspect_object_version(scope,physical_key(scope,key))
            if scope.versioned is not None:break
        if scope.versioned is None and scope.written_keys:
            if all(physical_key(scope,key) in scope.absent_keys for key in scope.written_keys):
                # Failed writes with confirmed absent keys need no DELETE. A measured DELETE may have created a marker and requires history inventory.
                remaining=scope.call('list',prefix='')['keys']
                if not remaining and not getattr(scope,'deletion_attempted',False):return []
                if any(not key.startswith(scope.prefix) for key in remaining):raise RuntimeError('refusing cleanup outside object scope')
                if remaining:inspect_object_version(scope,remaining[0])
            # No current object metadata is available; only actual version listing can rule out historical resources.
            if scope.versioned is None:
                history=scope.raw('versions_list')
                if history.get('ok'):
                    scope.versioned=bool(history.get('versions') or history.get('delete_markers'))
                else:raise RuntimeError('Cannot establish version-history cleanup for this prefix: '+str(history))
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
        if scope.versioned:
            history=scope.call('versions_list')
            if history.get('versions') or history.get('delete_markers') or history.get('truncated') is not False:
                raise RuntimeError('object version history cleanup not confirmed: '+str(history)+'; '+str(failures))
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
