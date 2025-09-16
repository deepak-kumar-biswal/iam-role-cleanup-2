import os, time, boto3, datetime as dt
from boto3.dynamodb.conditions import Key

CLEANUP_TABLE = os.environ["CLEANUP_TABLE"]
EXECUTION_ROLE_NAME = os.environ["EXECUTION_ROLE_NAME"]

ddb = boto3.resource("dynamodb")
table = ddb.Table(CLEANUP_TABLE)
sts = boto3.client("sts")

def assume(acct):
    resp = sts.assume_role(RoleArn=f"arn:aws:iam::{acct}:role/{EXECUTION_ROLE_NAME}", RoleSessionName="cleanup-cfn-exec", DurationSeconds=3600)
    c = resp["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"])

def wait_stack(cfn, stack):
    while True:
        st = cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
        if any(x in st for x in ("COMPLETE","FAILED","ROLLBACK")):
            return st
        time.sleep(10)

def lambda_handler(event, _ctx):
    accounts = event.get("accounts") or []
    scan = table.scan()
    plans = [i for i in scan.get("Items", []) if i["Sk"]=="plan#stack" and i["State"]=="changeset-prepared" and i["Pk"].split('#')[0] in accounts]
    results = []
    for p in plans:
        acct, stack = p["Pk"].split("#",1)
        sess = assume(acct); cfn = sess.client("cloudformation")
        if p.get("DeleteStack", False):
            cfn.delete_stack(StackName=stack)
            status = wait_stack(cfn, stack)
            action = "delete-stack"
        else:
            cs = p.get("ChangeSetName")
            cfn.execute_change_set(StackName=stack, ChangeSetName=cs)
            status = wait_stack(cfn, stack)
            action = "execute-changeset"
        table.put_item(Item={
            "Pk": p["Pk"],
            "Sk": "exec#stack",
            "Action": action,
            "Status": status,
            "UpdatedAt": dt.datetime.utcnow().isoformat()
        })
        table.update_item(
            Key={"Pk": p["Pk"], "Sk":"plan#stack"},
            UpdateExpression="SET #S=:s, UpdatedAt=:t",
            ExpressionAttributeNames={"#S":"State"},
            ExpressionAttributeValues={":s":"executed",":t":dt.datetime.utcnow().isoformat()}
        )
        results.append({"AccountId":acct,"StackName":stack,"Action":action,"Status":status})
    return {"ok": True, "executed": results}
