"""Preserve literal and generated bucket-name constraints without touching unowned buckets."""
from contextlib import contextmanager
import uuid
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_request_headers import recover_creation
from .ceph_bucket_config import rejected
from .scenarios import Suite,assert_equal,require_ok
from .suites import case,run_cases,CaseNotRun,CEPH


class NamingScope(BucketScope):
    """Own exact source-generated or authorized literal names; retain Bucket-only creation and inherited verified cleanup instead of altering tested naming rules."""
    def __init__(self,p,name,location=None):
        super().__init__(p);self.name=name;self.location=location
    def raw(self,op,**options):
        if op=='bucket_create_raw':
            options.pop('location',None)
            if self.location is not None:options['location']=self.location
        return super().raw(op,**options)


@contextmanager
def attempted(p,name,location=None):
    """Verify absence before any create; even ambiguous successes require independent ACL owner confirmation before destructive cleanup."""
    bucket=NamingScope(p,name,location)
    absent=bucket.raw('bucket_head')
    if absent.get('ok') or (absent.get('error_kind'),absent.get('status_code'))!=('not_found',404):
        raise CaseNotRun('exact source bucket name is not demonstrably absent','HEAD 404 for '+name)
    try:
        result=bucket.raw('bucket_create_raw')
        yield bucket,result
    finally:
        try:recover_creation(bucket)
        except Exception as error:raise RuntimeError('bucket creation cleanup is unconfirmed; manually inspect '+name+': '+str(error)) from error


def prefix():return 'osv-'+uuid.uuid4().hex


def good_name(p,mode):
    """Preserve generated prefix boundaries, exact 60..63 lengths and original suffixes; consume actual service creation status and V1 empty listing."""
    generated=prefix()
    if mode.startswith('long_'):name=generated+'a'*(int(mode[5:])-len(generated));assert_equal(len(generated)<63,True)
    elif mode=='list_long':name=generated+'a'*(61-len(generated))
    elif mode=='dns_long':
        assert_equal(len(generated)<50,True);name=generated+'a'*(63-len(generated))
    elif mode=='starts_alpha':name='a'+generated+'foo'
    elif mode=='starts_digit':name='0'+generated+'foo'
    else:name=generated+{'period':'aaa.111','hyphen':'aaa-111'}[mode]
    with attempted(p,name) as (bucket,response):
        require_ok(response)
        if mode=='list_long':assert_equal(len(bucket.call('list_v1_page')['keys'])==0,True)
        else:assert_equal(response['status_code'],200)
    return {'exact_name':name,'cleanup_confirmed':bucket.cleanup_completed}


LITERALS={'short_one':'a','short_two':'aa','ip':'192.168.5.123','underscore':'foo_bar','dash_end':'foo-',
 'dot_dot':'foo..bar','dot_dash':'foo.-bar','dash_dot':'foo-.bar'}


def bad_name(p,mode):
    """Keep fixed invalid literals intact and gate them on dedicated isolation; invalid generated leading underscore remains UUID isolated; require actual 400/InvalidBucketName."""
    if mode=='nonalpha':name='_'+prefix()
    else:
        if p.ctx.workload.get('ceph_literal_bucket_names_authorized') is not True:
            raise CaseNotRun('fixed source bucket names require a dedicated isolated environment','workload.ceph_literal_bucket_names_authorized=true')
        name=LITERALS[mode]
    with attempted(p,name) as (bucket,response):rejected(response,400,'InvalidBucketName')


def location(p):
    """Create with independently configured original API location constraint and compare real GetBucketLocation; never derive expected location from the response."""
    expected=p.ctx.workload.get('ceph_api_name')
    if not expected:raise CaseNotRun('source requires independently configured main api_name','workload.ceph_api_name')
    with attempted(p,prefix(),expected) as (bucket,response):
        require_ok(response);assert_equal(bucket.call('bucket_location')['location'],expected)


def recreate(p,nonowner=False):
    """Preserve source Bucket-only recreation, real alternate-client conflict and original e.status attribute-access defect on the same-owner exception branch."""
    if nonowner and 'identity_alt' not in p.ctx.worker.capabilities:raise CaseNotRun('source requires a real alternate identity','SDK identity_alt')
    with attempted(p,prefix()) as (bucket,response):
        require_ok(response)
        actual=bucket.raw('bucket_create_raw',**({'identity':'alt'} if nonowner else {}))
        if nonowner:rejected(actual,409,'BucketAlreadyExists')
        elif not actual['ok']:
            # The original ClientError exposes response but not status/error_code.
            # Its except block computes status/error_code, then reads e.status
            # instead. Preserve that source failure; do not silently repair it.
            raise AttributeError("source ClientError has no attribute 'status'; actual SDK result="+str(actual))


GOOD=(('long_60','test_bucket_create_naming_good_long_60'),('long_61','test_bucket_create_naming_good_long_61'),('long_62','test_bucket_create_naming_good_long_62'),('long_63','test_bucket_create_naming_good_long_63'),('list_long','test_bucket_list_long_name'),('dns_long','test_bucket_create_naming_dns_long'),('starts_alpha','test_bucket_create_naming_good_starts_alpha'),('starts_digit','test_bucket_create_naming_good_starts_digit'),('period','test_bucket_create_naming_good_contains_period'),('hyphen','test_bucket_create_naming_good_contains_hyphen'))
BAD=(('nonalpha','test_bucket_create_naming_bad_starts_nonalpha'),('short_one','test_bucket_create_naming_bad_short_one'),('short_two','test_bucket_create_naming_bad_short_two'),('ip','test_bucket_create_naming_bad_ip'),('underscore','test_bucket_create_naming_dns_underscore'),('dash_end','test_bucket_create_naming_dns_dash_at_end'),('dot_dot','test_bucket_create_naming_dns_dot_dot'),('dot_dash','test_bucket_create_naming_dns_dot_dash'),('dash_dot','test_bucket_create_naming_dns_dash_dot'))
S=SCOPE_OPERATIONS+' api.GetBucketAcl'
CASES=tuple(case('ceph_names_'+mode,'Ceph 原始合法桶命名 '+mode,'原始首字符/后缀/精确长度及创建200；独立拥有者确认后清理',S+(' list_v1_page' if mode=='list_long' else ''),lambda p,mode=mode:good_name(p,mode),reference,CEPH) for mode,reference in GOOD)
CASES+=tuple(case('ceph_names_'+mode,'Ceph 原始非法桶命名 '+mode,'不改写字面输入，HEAD404及专用隔离前提，服务400/InvalidBucketName',S,lambda p,mode=mode:bad_name(p,mode),reference,CEPH) for mode,reference in BAD)
CASES+=(case('ceph_names_location','Ceph 原始桶位置','独立main api_name与实际GetBucketLocation一致',S+' bucket_location',location,'test_bucket_get_location',CEPH),
 case('ceph_names_recreate','Ceph 原始同拥有者重建','保留成功路径及原ClientError.status属性缺陷',S,recreate,'test_bucket_create_exists',CEPH),
 case('ceph_names_recreate_nonowner','Ceph 原始非拥有者重建','真实alt身份创建相同桶409/BucketAlreadyExists',S+' identity_alt',lambda p:recreate(p,True),'test_bucket_create_exists_nonowner',CEPH))


def register(registry):
    registry.add_suite(Suite('ceph_bucket_names',frozenset().union(*(c.operations for c in CASES)),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
