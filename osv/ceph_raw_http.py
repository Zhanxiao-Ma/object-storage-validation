"""Preserve original Ceph raw-HTTP forms/CORS while real SDKs own setup, readback and cleanup."""
from collections import OrderedDict
from contextlib import ExitStack
from datetime import datetime,timedelta,timezone
import base64
import hashlib
import hmac
import json
import os
import socket
import ssl
import time
from urllib.parse import urlsplit,urlunsplit,quote
from xml.etree import ElementTree as ET
from .bucket_scope import BucketScope,SCOPE_OPERATIONS
from .ceph_bucket_config import api,rejected
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CaseNotRun,CEPH


def normalized_endpoint(value):
    """Normalize only equivalent endpoint spellings; reject credentials/query/fragment rather than silently changing the destination."""
    if not isinstance(value,str):raise ValueError("raw HTTP endpoint must be a string")
    parts=urlsplit(value)
    if parts.scheme not in ("http","https") or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("raw HTTP endpoint must be an explicit http(s) URL without credentials, query or fragment")
    port=parts.port if parts.port is not None else (443 if parts.scheme=="https" else 80)
    if port==0:raise ValueError("raw HTTP endpoint port must be in 1..65535")
    return parts.scheme,parts.hostname.lower(),port,parts.path.rstrip("/")


class SourceHttp:
    """Bind source HTTP traffic to explicit matching SDK configuration; preserve original requests multipart/signing without claiming SDK transport or timing."""
    def __init__(self,p):
        config=p.ctx.workload.get("ceph_http")
        if not isinstance(config,dict):raise CaseNotRun("source raw HTTP requires explicit configuration","workload.ceph_http")
        missing=["workload.ceph_http."+name for name in ("endpoint","addressing_style") if name not in config]
        if missing:raise CaseNotRun("source raw HTTP configuration is incomplete",*missing)
        normalized_endpoint(config["endpoint"])
        sdk_endpoint=p.ctx.worker.info.get("endpoint")
        if not sdk_endpoint:raise CaseNotRun("raw HTTP requires an explicit real SDK endpoint","worker endpoint")
        self.endpoint=config["endpoint"].rstrip("/");self.parts=urlsplit(self.endpoint)
        if normalized_endpoint(self.endpoint)!=normalized_endpoint(sdk_endpoint):
            raise CaseNotRun("raw HTTP endpoint must match the real SDK endpoint","workload.ceph_http.endpoint equals worker endpoint")
        self.style=config["addressing_style"]
        if self.style not in ("path","virtual"):raise ValueError("raw HTTP addressing_style must be path or virtual")
        environment={**os.environ,**p.ctx.worker.target.get("env",{})}
        ak=config.get("access_key_env","OSV_ACCESS_KEY");sk=config.get("secret_key_env","OSV_SECRET_KEY")
        if not isinstance(ak,str) or not isinstance(sk,str) or not ak or not sk:raise ValueError("raw HTTP credential references must be environment variable names")
        missing=["environment "+name for name in (ak,sk) if not environment.get(name)]
        if missing:raise CaseNotRun("source HTTP form authentication requires configured credentials",*missing)
        self.access=environment[ak];self.secret=environment[sk]
        try:import requests
        except ImportError:raise CaseNotRun("source raw HTTP transport requires optional requests","optional dependency requests") from None
        self.requests=requests;self.timeout=p.ctx.worker.timeout
        proxy=environment.get('OSV_HTTP_PROXY','')
        self.proxies={'http':proxy,'https':proxy}
    def url(self,bucket,key=None):
        if self.style=="path":base=self.endpoint+"/"+quote(bucket,safe="")
        else:
            host=self.parts.hostname
            if ":" in host:raise ValueError("virtual raw HTTP addressing cannot use an IPv6 literal")
            authority=bucket+"."+host+(":"+str(self.parts.port) if self.parts.port is not None else "")
            base=urlunsplit((self.parts.scheme,authority,self.parts.path.rstrip("/"),"",""))
        return base+("/"+quote(key,safe="/") if key is not None else "")
    def request(self,method,url,**options):
        options.setdefault('proxies',self.proxies)
        return self.requests.request(method,url,timeout=self.timeout,verify=True,**options)
    @staticmethod
    def evidence():return {"request_transport":"raw_http","sdk_role":"bucket configuration, readback and cleanup only","sdk_performance_sample":False}


def post_vector(p,mode):
    """Keep source form ordering/filenames, exact policy JSON/HMAC and every status/body/redirect/metadata oracle; real SDK scopes clean all possible writes on failed assertions."""
    transport=SourceHttp(p)
    public=mode in {"anonymous","no_content_type","bad_access_key","success","invalid_success","redirect","tags_anonymous"}
    anonymous=mode in {"anonymous","success","invalid_success","tags_anonymous"}
    with ExitStack() as owned:
        bucket=owned.enter_context(BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":"public-read-write"})) if public else owned.enter_context(BucketScope(p))
        target=owned.enter_context(BucketScope(p)) if mode=="wrong_bucket" else bucket
        key=r"\$foo.txt" if mode in {"escaped","invalid_signature","invalid_access_key","invalid_date"} else "foo_cksum_test.txt" if mode=="checksum" else "foo.txt"
        content=b"x"*(2*1024*1024) if mode=="checksum" else "foo"*(1024*1024) if mode=="large" else "x"*(4*2**20+200) if mode=="chunk_bug" else "bar"
        minimum,maximum=(4*2**20,12*2**20) if mode=="chunk_bug" else (0,5*1024*1024) if mode in {"large","checksum"} else (512,1000) if mode=="below_minimum" else (0,0) if mode=="above_maximum" else (-1,0) if mode=="negative_length" else (0,1024)
        if mode=="chunk_bug":assert_equal(len(content)>minimum,True);assert_equal(len(content)<maximum,True)
        expires=datetime.now(timezone.utc)+timedelta(seconds=-6000 if mode=="expired" else 6000)
        condition_key=r"\$foo" if mode in {"escaped","invalid_signature","invalid_access_key","invalid_date","missing_signature","missing_bucket_condition"} else "foo_cksum_test" if mode=="checksum" else "foo"
        conditions=[{"bucket":bucket.name},["starts-with","$key",bucket.key(condition_key)],{"acl":"private"}]
        if mode!="no_content_type":conditions.append(["starts-with","$Content-Type","text/plain"])
        conditions.append(["content-length-range",minimum] if mode=="missing_length" else ["content-length-range",minimum,maximum])
        if mode=="no_key":conditions.pop(1)
        if mode=="missing_bucket_condition":conditions.pop(0)
        if mode in {"metadata","missing_metadata"}:conditions.append(["starts-with","$x-amz-meta-foo","bar"])
        if mode=="invalid_metadata":conditions.append(["eq","$x-amz-meta-foo",""])
        if mode=="tags_authenticated":conditions.append(["starts-with","$tagging",""])
        if mode=="redirect":conditions.insert(-1,["eq","$success_action_redirect",transport.url(bucket.name)])
        if mode=="case_insensitive":conditions=[{"bUcKeT":bucket.name},["StArTs-WiTh","$KeY",bucket.key("foo")],{"AcL":"private"},["StArTs-WiTh","$CoNtEnT-TyPe","text/plain"],["content-length-range",0,1024]]
        if mode=="empty_conditions":conditions=[{}]
        policy_document={"EXPIRATION" if mode=="expiration_case" else "expiration":str(expires) if mode=="invalid_date" else expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if mode!="no_conditions":policy_document["CONDITIONS" if mode=="conditions_case" else "conditions"]=conditions
        if mode=="no_expiration":policy_document.pop("expiration")
        policy=base64.b64encode(json.JSONEncoder().encode(policy_document).encode("utf-8"))
        signature=base64.b64encode(hmac.new(transport.secret.encode("utf-8"),policy,hashlib.sha1).digest())
        if mode=="invalid_signature":signature=signature[::-1]
        fields=OrderedDict()
        if mode!="no_key":fields["kEy" if mode=="case_insensitive" else "key"]=bucket.key("${filename}" if mode in {"filename","wrong_bucket"} else key)
        if mode=="wrong_bucket":fields["bucket"]=bucket.name
        if not anonymous:fields["AWSAccessKeyId"]="foo" if mode=="bad_access_key" else transport.access[::-1] if mode=="invalid_access_key" else transport.access
        fields["aCl" if mode=="case_insensitive" else "acl"]="public-read" if anonymous else "private"
        if not anonymous:
            if mode!="missing_signature":fields["signature"]=signature
            fields["pOLICy" if mode=="case_insensitive" else "policy"]=policy
        if mode=="tags_authenticated":fields["tagging"]="<Tagging><TagSet><Tag><Key>0</Key><Value>0</Value></Tag><Tag><Key>1</Key><Value>1</Value></Tag></TagSet></Tagging>"
        if mode in {"success","invalid_success"}:fields["success_action_status"]="201" if mode=="success" else "404"
        if mode!="no_content_type":fields["Content-Type"]="text/plain"
        if mode=="ignored":fields["x-ignore-foo"]="bar"
        if mode in {"metadata","invalid_metadata"}:fields["x-amz-meta-foo"]="barclamp"
        if mode=="redirect":fields["success_action_redirect"]=transport.url(bucket.name)
        if mode=="tags_anonymous":fields["tagging"]="<Tagging><TagSet><Tag><Key>0</Key><Value>0</Value></Tag><Tag><Key>1</Key><Value>1</Value></Tag></TagSet></Tagging>"
        if mode=="checksum":fields["x-amz-checksum-sha256"]="aTL9MeXa9HObn6eP93eygxsJlcwdCwCTysgGAZAgE7w="
        fields["file"]=("foo.txt",content) if mode in {"filename","wrong_bucket"} else content
        # Raw POST can write before a failing assertion. Record potential original
        # keys before transmission and rely on SDK listing for any service aliases.
        target.written_keys.add(key)
        response=transport.request("POST",transport.url(target.name),files=fields)
        forbidden={"bad_access_key","invalid_signature","invalid_access_key","missing_bucket_condition","missing_metadata","expired","wrong_bucket","invalid_metadata"}
        malformed={"invalid_date","no_key","missing_signature","conditions_case","expiration_case","no_expiration","no_conditions","above_maximum","missing_length","negative_length","below_minimum","empty_conditions"}
        expected=403 if mode in forbidden else 400 if mode in malformed else 201 if mode=="success" else 200 if mode=="redirect" else 204
        assert_equal(response.status_code,expected)
        if mode=="success":assert_equal(ET.fromstring(response.content).find("Key").text,bucket.key("foo.txt"))
        if mode=="invalid_success":assert_equal(response.content.decode(),"")
        reads={"anonymous","authenticated","no_content_type","large","filename","escaped","redirect","metadata","tags_anonymous","tags_authenticated"}
        if mode in reads:
            observed=bucket.call("get_metadata",key=key,output_path=str(p.ctx.output))
            if mode=="metadata":assert_equal(observed["metadata"]["foo"],"barclamp")
            elif mode=="redirect":assert_equal(response.url,transport.url(bucket.name)+"?bucket="+bucket.name+"&key=foo.txt&etag=%22"+observed["etag"].strip('"')+"%22")
            else:assert_equal(p.ctx.output.read_bytes().decode(),content.decode() if isinstance(content,bytes) else content)
        if mode=="tags_anonymous":assert_equal(bucket.call("tags_get",key=key)["tags"],[{"key":"0","value":"0"},{"key":"1","value":"1"}])
        if mode=="checksum":
            fields["x-amz-checksum-sha256"]="sailorjerry"
            assert_equal(transport.request("POST",transport.url(bucket.name),files=fields).status_code,400)
    return transport.evidence()


POSTS=(("anonymous","test_post_object_anonymous_request"),("authenticated","test_post_object_authenticated_request"),("no_content_type","test_post_object_authenticated_no_content_type"),("bad_access_key","test_post_object_authenticated_request_bad_access_key"),("success","test_post_object_set_success_code"),("invalid_success","test_post_object_set_invalid_success_code"),("large","test_post_object_upload_larger_than_chunk"),("filename","test_post_object_set_key_from_filename"),("ignored","test_post_object_ignored_header"),("case_insensitive","test_post_object_case_insensitive_condition_fields"),("escaped","test_post_object_escaped_field_values"),("redirect","test_post_object_success_redirect_action"),("invalid_signature","test_post_object_invalid_signature"),("invalid_access_key","test_post_object_invalid_access_key"),("invalid_date","test_post_object_invalid_date_format"),("no_key","test_post_object_no_key_specified"),("missing_signature","test_post_object_missing_signature"),("missing_bucket_condition","test_post_object_missing_policy_condition"),("metadata","test_post_object_user_specified_header"),("missing_metadata","test_post_object_request_missing_policy_specified_field"),("conditions_case","test_post_object_condition_is_case_sensitive"),("expiration_case","test_post_object_expires_is_case_sensitive"),("expired","test_post_object_expired_policy"),("wrong_bucket","test_post_object_wrong_bucket"),("invalid_metadata","test_post_object_invalid_request_field_value"),("no_expiration","test_post_object_missing_expires_condition"),("no_conditions","test_post_object_missing_conditions_list"),("above_maximum","test_post_object_upload_size_limit_exceeded"),("missing_length","test_post_object_missing_content_length_argument"),("negative_length","test_post_object_invalid_content_length_argument"),("below_minimum","test_post_object_upload_size_below_minimum"),("chunk_bug","test_post_object_upload_size_rgw_chunk_size_bug"),("empty_conditions","test_post_object_empty_conditions"),("tags_anonymous","test_post_object_tags_anonymous_request"),("tags_authenticated","test_post_object_tags_authenticated_request"),("checksum","test_post_object_upload_checksum"))
CASES=tuple(case("ceph_raw_post_"+mode,"Ceph 原始HTTP表单 "+mode,"保留源原始requests files顺序、policy/HMAC/非法输入及全部状态/正文/元数据断言；HTTP协议证据不计SDK性能",SCOPE_OPERATIONS+" get_metadata"+(" bucket_create_acl" if mode in {"anonymous","no_content_type","bad_access_key","success","invalid_success","redirect","tags_anonymous"} else "")+(" tags_get" if mode=="tags_anonymous" else ""),lambda p,mode=mode:post_vector(p,mode),reference,CEPH) for mode,reference in POSTS)


def cors(p,mode):
    """Preserve the source SDK CORS configuration and literal anonymous HTTP matrix, including absent response headers and the original three-second wait."""
    transport=SourceHttp(p)
    with BucketScope(p,create_operation="bucket_create_acl",create_options={"acl":"public-read"}) as bucket:
        rejected(bucket.raw("api.GetBucketCors",input={}),404)
        rules=[{"AllowedMethods":["GET"],"AllowedOrigins":["*suffix"]},{"AllowedMethods":["GET"],"AllowedOrigins":["start*end"]},
            {"AllowedMethods":["GET"],"AllowedOrigins":["prefix*"]},{"AllowedMethods":["PUT"],"AllowedOrigins":["*.put"]}] if mode=="origins" else [{"AllowedMethods":["GET"],"AllowedOrigins":["*"]}]
        if mode=="header":rules[0]["ExposeHeaders"]=["x-amz-meta-header1"]
        api(bucket,"PutBucketCors",CORSConfiguration={"CORSRules":rules});time.sleep(3)
        bucket.written_keys.add("bar")
        def checked(method,key,headers,status,origin=None,methods=None):
            response=transport.request(method,transport.url(bucket.name,key),headers=headers)
            assert_equal(response.status_code,status)
            assert_equal(response.headers.get("access-control-allow-origin"),origin)
            assert_equal(response.headers.get("access-control-allow-methods"),methods)
        if mode=="wildcard":
            checked("GET",None,None,200)
            checked("GET",None,{"Origin":"example.origin"},200,"*","GET")
        elif mode=="header":checked("OPTIONS","bar",{"Origin":"example.origin","Access-Control-Request-Headers":"x-amz-meta-header2","Access-Control-Request-Method":"GET"},403)
        else:
            checked("GET",None,None,200)
            for origin,allowed in (("foo.suffix",True),("foo.bar",False),("foo.suffix.get",False),("startend",True),("start1end",True),("start12end",True),("0start12end",False),("prefix",True),("prefix.suffix",True),("bla.prefix",False)):
                checked("GET",None,{"Origin":origin},200,origin if allowed else None,"GET" if allowed else None)
            checked("GET","bar",{"Origin":"foo.suffix"},404,"foo.suffix","GET")
            for requested,allowed in (("GET",True),("PUT",False),("DELETE",False),(None,False)):
                headers={"Origin":"foo.suffix","content-length":"0"}
                if requested is not None:headers["Access-Control-Request-Method"]=requested
                checked("PUT","bar",headers,403,"foo.suffix" if allowed else None,"GET" if allowed else None)
            checked("PUT","bar",{"Origin":"foo.put","content-length":"0"},403,"foo.put","PUT")
            checked("GET","bar",{"Origin":"foo.suffix"},404,"foo.suffix","GET")
            for headers in (None,{"Origin":"foo.suffix"},{"Origin":"bla"}):checked("OPTIONS",None,headers,400)
            checked("OPTIONS","bar",{"Origin":"foo.suffix","Access-Control-Request-Method":"GET","content-length":"0"},200,"foo.suffix","GET")
            for origin,method,allowed in (("foo.bar","GET",False),("foo.suffix.get","GET",False),("startend","GET",True),("start1end","GET",True),("start12end","GET",True),("0start12end","GET",False),("prefix","GET",True),("prefix.suffix","GET",True),("bla.prefix","GET",False),("foo.put","GET",False),("foo.put","PUT",True)):
                checked("OPTIONS",None,{"Origin":origin,"Access-Control-Request-Method":method},200 if allowed else 403,origin if allowed else None,method if allowed else None)
    return transport.evidence()


def interim_status(transport,bucket):
    """Read the original raw interim response without transmitting the declared 123-byte body; preserve five-second socket timeout and strict HTTP status token parsing."""
    url=urlsplit(transport.url(bucket,"testobj"));host=url.hostname;port=url.port or (443 if url.scheme=="https" else 80)
    request=("PUT "+url.path+" HTTP/1.1\r\nHost: "+host+"\r\nAccept-Encoding: identity\r\nContent-Length: 123\r\nExpect: 100-continue\r\n\r\n").encode()
    connection=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
    try:
        if url.scheme=="https":connection=ssl.create_default_context().wrap_socket(connection,server_hostname=host)
        connection.settimeout(5);connection.connect((host,port));connection.send(request);response=connection.recv(1024)
    finally:connection.close()
    tokens=response.decode().split(" ");assert_equal(tokens[0].startswith("HTTP"),True)
    return tokens[1]


def continue_interim(p):
    """Preserve the source's unauthenticated private-403 then public-read-write-100 socket exchange; SDK performs setup and cleanup only."""
    transport=SourceHttp(p)
    with BucketScope(p) as bucket:
        assert_equal(interim_status(transport,bucket.name),"403")
        api(bucket,"PutBucketAcl",ACL="public-read-write")
        assert_equal(interim_status(transport,bucket.name),"100")
    return transport.evidence()


def continue_retry(p):
    """Retain source Expect-driven missing-bucket 404 then valid bar upload using the same official SDK client; this is SDK connection reuse evidence, not a raw signer substitute."""
    with BucketScope(p) as bucket:
        missing=bucket.name+"-but-doesnt-exist"
        rejected(p.ctx.worker.call({"op":"bucket_head","bucket":missing}),404)
        p.ctx.body.write_bytes(b"bar")
        rejected(p.ctx.worker.call({"op":"put_http","bucket":missing,"key":"foo","body_path":str(p.ctx.body),"wire_headers":{"Expect":"100-continue"}}),404)
        bucket.call("put_http",key="foo",body_path=str(p.ctx.body),wire_headers={"Expect":"100-continue"})
    return {"request_transport":"sdk_http","sdk_role":"both requests use the same cached compatibility SDK client","sdk_performance_sample":False}


CASES+=tuple(case("ceph_raw_cors_"+mode,"Ceph 原始HTTP CORS "+mode,"保留源CORS规则/3秒等待、全部匿名GET/PUT/OPTIONS状态及响应头有无；SDK仅配置清理",SCOPE_OPERATIONS+" bucket_create_acl api.GetBucketCors api.PutBucketCors",lambda p,mode=mode:cors(p,mode),reference,CEPH) for mode,reference in (("origins","test_cors_origin_response"),("wildcard","test_cors_origin_wildcard"),("header","test_cors_header_option")))
CASES+=(case("ceph_raw_continue_interim","Ceph 原始100 Continue中间响应","未发送body的原始socket请求，私有桶403、公开写入桶100，真实SDK配置和清理",SCOPE_OPERATIONS+" api.PutBucketAcl",continue_interim,"test_100_continue",CEPH),
 case("ceph_raw_continue_retry","Ceph 100 Continue错误后连接复用","相同兼容SDK客户端真实Expect头请求：不存在桶404后原bar写入成功",SCOPE_OPERATIONS+" put_http wire_headers",continue_retry,"test_100_continue_error_retry",CEPH))


def run_raw_cases(ctx,cases=CASES):
    """Keep transport boundaries visible even when a source assertion or prerequisite fails before returning case evidence."""
    results=run_cases(ctx,cases)
    for row in results:
        metadata={"request_transport":"sdk_http" if row["case"]=="ceph_raw_continue_retry" else "raw_http",
            "sdk_role":"SDK connection reuse" if row["case"]=="ceph_raw_continue_retry" else "configuration, readback and cleanup only","sdk_performance_sample":False}
        row["evidence"]={**row.get("evidence",{}),**metadata}
    return results


def register(registry):
    registry.add_suite(Suite("ceph_raw_http",frozenset().union(*(c.operations for c in CASES)),run_raw_cases,tuple(c.metadata() for c in CASES),case_capabilities=True))
