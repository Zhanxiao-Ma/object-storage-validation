"""Preserve remaining encrypted multipart helper inputs and optional torrent semantics."""
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .ceph_encryption_objects import key_id,ALGORITHM,KMS_KEY
from .ceph_multipart import uploading,source_data,MIB
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH


def get(bucket,p,key,**options):
    p.ctx.output.unlink(missing_ok=True)
    result=bucket.call('get_http',key=key,output_path=str(p.ctx.output),**options)
    return result,p.ctx.output.read_bytes()


def multipart(p,mode,data=None):
    """Keep original 30-MiB/six-part geometry, metadata, distinct KMS keys and source success-only invalid-chunk helpers; preserve actual whole-GET size producers in both range loops."""
    ident=None if mode=='s3_default' else key_id(p)
    secondary=p.ctx.workload.get('secondary_kms_key_id')
    if mode=='invalid_secondary' and secondary is None:
        raise CaseNotRun('source requires an independently configured secondary KMS key','workload.secondary_kms_key_id')
    content_type='text/bla' if mode=='invalid_secondary' else 'text/plain'
    initial={'Content-Type':content_type}
    if ident is not None:initial.update({ALGORITHM:'aws:kms',KMS_KEY:ident})
    part_headers={ALGORITHM:'aws:kms',KMS_KEY:secondary if mode=='invalid_secondary' else 'testkey-not-present'} if mode in ('invalid_secondary','invalid_missing') else initial
    key='multipart_enc';size=30*MIB;step=5*MIB
    if data is None:data=source_data(size,step,seed=11312)
    with BucketScope(p) as bucket:
        if mode=='s3_default':assert_equal(api(bucket,'PutBucketEncryption',ServerSideEncryptionConfiguration={'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'AES256'}}]})['status_code'],200)
        with uploading(bucket,p,key,metadata={'foo':'bar'},wire_headers=initial) as upload_id:
            parts=[]
            for index,start in enumerate(range(0,size,step)):
                p.ctx.body.write_bytes(data[start:start+step])
                actual=bucket.call('mp_part',key=key,upload_id=upload_id,part_number=index+1,body_path=str(p.ctx.body),wire_headers=part_headers)
                parts.append({'part_number':index+1,'etag':actual['etag'].strip('"')})
            # These two upstream tests merely call the helper, without asserting
            # a rejection or completion. Successful parts must remain a pass.
            if mode in ('invalid_secondary','invalid_missing'):return {'uploaded_parts':len(parts),'source_asserts_rejection':False}
            bucket.call('mp_complete_observed',key=key,upload_id=upload_id,parts=parts,wire_headers=initial)
            listed=bucket.call('list_v2_observed',prefix=key)
            assert_equal(len(listed['objects']),1);assert_equal(listed['objects'][0]['size'],size)
            # The source's unused UploadPart callback closes over an undefined
            # part_headers name after completion. It never executes; don't
            # introduce an eager error or attach its headers to GET requests.
            observed,body=get(bucket,p,key)
            assert_equal(observed['metadata'],{'foo':'bar'});assert_equal(observed['http_headers']['content-type'],content_type)
            if mode=='s3_default':assert_equal(observed['http_headers'][ALGORITHM],'AES256')
            assert_equal(body,data);assert_equal(len(body),observed['size'])
            for stride in (1000000,10000000):
                whole,_=get(bucket,p,key);observed_size=whole['size']
                for offset in range(0,observed_size,stride):
                    length=min(stride,observed_size-offset);end=offset+length-1
                    result,body=get(bucket,p,key,range_header=f'bytes={offset}-{end}')
                    assert_equal(result['size'],length);assert_equal(body,data[offset:end+1])
    return {'cleanup_confirmed':bucket.cleanup_completed}


def torrent(p):
    """Use original Avatar.mpg and seven-MiB A body; preserve either distinct actual torrent bytes or precise optional 404/NoSuchKey, without inventing torrent support."""
    data=b'A'*(7*MIB)
    with BucketScope(p) as bucket:
        bucket.put('Avatar.mpg',data)
        response=bucket.raw('get_http',key='Avatar.mpg',output_path=str(p.ctx.output),wire_query={'torrent':True})
        if response['ok']:assert_equal(p.ctx.output.read_bytes().decode()!=data.decode(),True)
        else:rejected(response,404,'NoSuchKey')


S=SCOPE_OPERATIONS+' mp_create_metadata mp_part mp_complete_observed mp_abort_raw wire_headers list_v2_observed get_http'
CASES=tuple(case('ceph_encryption_tail_'+mode,'Ceph 原始加密multipart '+mode,'原30MiB/6x5MiB、真实ETag、元数据/响应头/两组实际GET大小驱动Range；invalid helper无负面断言',S+(' api.PutBucketEncryption' if mode=='s3_default' else ''),lambda p,mode=mode:multipart(p,mode),reference,CEPH) for mode,reference in (('kms','test_sse_kms_multipart_upload'),('invalid_secondary','test_sse_kms_multipart_invalid_chunks_1'),('invalid_missing','test_sse_kms_multipart_invalid_chunks_2'),('s3_default','test_sse_s3_default_multipart_upload')))
CASES+=(case('ceph_encryption_tail_torrent','Ceph 原始torrent可选能力','原7MiB A对象；实际不同torrent正文或404/NoSuchKey均按源接受',SCOPE_OPERATIONS+' put get_http wire_query',torrent,'test_get_object_torrent',CEPH),)


def register(registry):
    registry.add_suite(Suite('ceph_encryption_tail',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
