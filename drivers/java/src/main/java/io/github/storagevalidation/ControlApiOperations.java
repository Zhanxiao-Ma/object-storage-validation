package io.github.storagevalidation;
import java.util.*;
/** Shares the pinned official SDK operation allowlist across control-plane bridges. Generates explicit service lists to prevent unsupported API dispatch. */
final class ControlApiOperations {
 static final List<String> IAM=List.of("PutUserPolicy","GetUserPolicy","ListUserPolicies","DeleteUserPolicy","CreateUser","GetUser","UpdateUser","DeleteUser","ListUsers","CreateAccessKey","UpdateAccessKey","DeleteAccessKey","ListAccessKeys","AttachUserPolicy","DetachUserPolicy","ListAttachedUserPolicies","CreateGroup","GetGroup","UpdateGroup","DeleteGroup","ListGroups","AddUserToGroup","RemoveUserFromGroup","ListGroupsForUser","PutGroupPolicy","GetGroupPolicy","ListGroupPolicies","DeleteGroupPolicy","AttachGroupPolicy","DetachGroupPolicy","ListAttachedGroupPolicies","CreateRole","GetRole","UpdateRole","DeleteRole","ListRoles","PutRolePolicy","GetRolePolicy","ListRolePolicies","DeleteRolePolicy","AttachRolePolicy","DetachRolePolicy","ListAttachedRolePolicies","CreateOpenIDConnectProvider","GetOpenIDConnectProvider","ListOpenIDConnectProviders","DeleteOpenIDConnectProvider","AddClientIDToOpenIDConnectProvider","RemoveClientIDFromOpenIDConnectProvider","UpdateOpenIDConnectProviderThumbprint","GetAccountSummary");
 static final List<String> STS=List.of("GetSessionToken","AssumeRole","AssumeRoleWithWebIdentity","GetCallerIdentity");
}
