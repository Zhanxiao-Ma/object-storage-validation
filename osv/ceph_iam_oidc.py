"""Original IAM OIDC management vectors, including intentionally non-AWS-valid URLs/thumbprints."""
from .ceph_iam_scope import IamScope,rpc,checked,error
from .scenarios import Suite,assert_equal
from .suites import case,run_cases,CEPH

OLD='3768084dfb3d2b68b7897bf5f565da8efEXAMPLE'
NEW='3768084dfb3d2b68b7897bf5f565da8efSAMPLE1'
SOURCE=CEPH.replace('test_s3.py','test_iam.py')


def provider(p,mode):
    with IamScope(p) as scope:
        host=scope.path[1:]+'example.com';url='http://'+host;arn=None
        before=checked(rpc(p,'ListOpenIDConnectProviders')).get('OpenIDConnectProviderList',[]);existing={item['Arn'] for item in before}
        def call(operation,status=False,**values):
            result=rpc(p,operation,**values);data=checked(result)
            if status:assert_equal(result.get('status_code'),200)
            return data
        try:
            clients=['my-application-id'] if mode=='basic' else ['app-jee-jsp'] if mode=='add_new' else ['app-jee-jsp','app-profile-jsp']
            data=call('CreateOpenIDConnectProvider',mode!='basic',Url=url,ClientIDList=clients,ThumbprintList=[OLD]);arn=data['OpenIDConnectProviderArn']
            if arn in existing:raise AssertionError('CreateOpenIDConnectProvider returned an existing ARN; refusing ownership')
            if mode=='basic':
                assert_equal(arn.endswith(':oidc-provider/'+host),True);assert_equal(arn in [item['Arn'] for item in call('ListOpenIDConnectProviders')['OpenIDConnectProviderList']],True)
                data=call('GetOpenIDConnectProvider',OpenIDConnectProviderArn=arn);assert_equal(data['Url'],url);assert_equal(data['ClientIDList'],['my-application-id']);assert_equal(data['ThumbprintList'],[OLD])
                call('DeleteOpenIDConnectProvider',OpenIDConnectProviderArn=arn);assert_equal(arn in [item['Arn'] for item in call('ListOpenIDConnectProviders')['OpenIDConnectProviderList']],False)
                for operation in ('GetOpenIDConnectProvider','DeleteOpenIDConnectProvider'):error(rpc(p,operation,OpenIDConnectProviderArn=arn),'NoSuchEntity')
            elif mode in ('add_new','add_existing'):
                data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ClientIDList']),1 if mode=='add_new' else 2);assert_equal(data['ClientIDList'][0],'app-jee-jsp')
                if mode=='add_new':assert_equal(data['Url'],url)
                else:assert_equal(data['ClientIDList'][1],'app-profile-jsp')
                call('AddClientIDToOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn,ClientID='app-profile-jsp');data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ClientIDList']),2);assert_equal(data['ClientIDList'][0],'app-jee-jsp');assert_equal(data['ClientIDList'][1],'app-profile-jsp');call('DeleteOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn)
            elif mode=='remove':
                data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ClientIDList']),2)
                for client in clients:assert_equal(client in data['ClientIDList'],True)
                call('RemoveClientIDFromOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn,ClientID='app-profile-jsp');data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ClientIDList']),1);assert_equal(data['ClientIDList'][0],'app-jee-jsp');assert_equal('app-profile-jsp' in data['ClientIDList'],False);call('DeleteOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn)
            else:
                data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ThumbprintList']),1);assert_equal(data['ThumbprintList'][0],OLD);call('UpdateOpenIDConnectProviderThumbprint',True,OpenIDConnectProviderArn=arn,ThumbprintList=[NEW]);data=call('GetOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn);assert_equal(len(data['ThumbprintList']),1);assert_equal(data['ThumbprintList'][0],NEW);call('DeleteOpenIDConnectProvider',True,OpenIDConnectProviderArn=arn)
        finally:
            if arn is not None and arn not in existing:
                result=rpc(p,'GetOpenIDConnectProvider',OpenIDConnectProviderArn=arn)
                if result.get('ok'):
                    if checked(result)['Url']!=url:raise RuntimeError('OIDC returned URL no longer belongs to this case; refusing cleanup')
                    result=rpc(p,'DeleteOpenIDConnectProvider',OpenIDConnectProviderArn=arn)
                if not result.get('ok'):error(result,'NoSuchEntity')


SPECS=(('test_account_oidc_provider','basic'),('test_verify_add_new_client_id_to_oidc','add_new'),('test_verify_add_existing_client_id_to_oidc','add_existing'),('test_verify_remove_client_id_from_oidc','remove'),('test_verify_update_thumbprintlist_of_oidc','thumbprint'))
OPS='identity_iam_root api.iam.GetUser '+' '.join('api.iam.'+x for x in ('CreateOpenIDConnectProvider','ListOpenIDConnectProviders','GetOpenIDConnectProvider','DeleteOpenIDConnectProvider','AddClientIDToOpenIDConnectProvider','RemoveClientIDFromOpenIDConnectProvider','UpdateOpenIDConnectProviderThumbprint'))
CASES=tuple(case('ceph_iam_'+ref.removeprefix('test_'),'Ceph IAM OIDC原始管理向量','原http URL与EXAMPLE/SAMPLE1非hex thumbprint不修改；保持客户ID顺序/去重/删除、所有HTTP/错误断言；仅清理真实新建ARN且核对URL',OPS,lambda p,m=mode:provider(p,m),ref,SOURCE) for ref,mode in SPECS)


def register(registry):registry.add_suite(Suite('ceph_iam_oidc',frozenset(OPS.split()),lambda ctx:run_cases(ctx,CASES),tuple(c.metadata() for c in CASES),case_capabilities=True))
