import os, boto3, datetime as dt
from boto3.dynamodb.conditions import Key

CLEANUP_TABLE = os.environ["CLEANUP_TABLE"]
EXECUTION_ROLE_NAME = os.environ["EXECUTION_ROLE_NAME"]

ddb = boto3.resource("dynamodb")
table = ddb.Table(CLEANUP_TABLE)
sts = boto3.client("sts")

def assume(acct):
    resp = sts.assume_role(RoleArn=f"arn:aws:iam::{acct}:role/{EXECUTION_ROLE_NAME}", RoleSessionName="cleanup-finalize", DurationSeconds=3600)
    c = resp["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"])

def safe_delete_role(iam, role):
    # Detach managed
    for a in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=role, PolicyArn=a["PolicyArn"])
    # Delete inline
    for name in iam.list_role_policies(RoleName=role)["PolicyNames"]:
        iam.delete_role_policy(RoleName=role, PolicyName=name)
    # Instance profiles
    for p in iam.list_instance_profiles_for_role(RoleName=role)["InstanceProfiles"]:
        iam.remove_role_from_instance_profile(InstanceProfileName=p["InstanceProfileName"], RoleName=role)
    iam.delete_role(RoleName=role)

def lambda_handler(event, _ctx):
    accounts = event.get("accounts") or []
    scan = table.scan()
    execs = [i for i in scan.get("Items", []) if i["Sk"]=="exec#stack" and i["Pk"].split('#')[0] in accounts]
    results = []
    for e in execs:
        acct, stack = e["Pk"].split("#",1)
        # After CFN removed roles (or stack deleted), mark per-role state accordingly.
        # Optionally attempt deletion of non-CFN roles if present in role items.
        # For brevity, we only mark final state here.
        table.update_item(
            Key={"Pk": e["Pk"], "Sk":"plan#stack"},
            UpdateExpression="SET #S=:s, UpdatedAt=:t",
            ExpressionAttributeNames={"#S":"State"},
            ExpressionAttributeValues={":s":"deleted" if e.get("Action")=="delete-stack" else "completed",":t":dt.datetime.utcnow().isoformat()}
        )
        results.append({"AccountId":acct,"StackName":stack,"FinalState": "deleted" if e.get("Action")=="delete-stack" else "completed"})
    return {"ok": True, "finalized": results}
