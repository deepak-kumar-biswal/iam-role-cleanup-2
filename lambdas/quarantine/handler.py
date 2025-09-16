import os, json, gzip, io, boto3, datetime as dt
from boto3.dynamodb.conditions import Key

CLEANUP_TABLE = os.environ["CLEANUP_TABLE"]
EXECUTION_ROLE_NAME = os.environ["EXECUTION_ROLE_NAME"]
ARTIFACT_BUCKET = os.environ["ARTIFACT_BUCKET"]

DENY_TRUST = {
  "Version":"2012-10-17",
  "Statement":[{"Effect":"Deny","Action":"sts:AssumeRole","Principal":"*"}]
}

ddb = boto3.resource("dynamodb")
table = ddb.Table(CLEANUP_TABLE)
s3 = boto3.client("s3")
sts = boto3.client("sts")

def assume(acct):
    resp = sts.assume_role(
        RoleArn=f"arn:aws:iam::{acct}:role/{EXECUTION_ROLE_NAME}",
        RoleSessionName="cleanup-quarantine",
        DurationSeconds=3600
    )
    c = resp["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"])

def gzip_put_json(obj, key):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as f:
        f.write(json.dumps(obj, indent=2).encode("utf-8"))
    s3.put_object(Bucket=ARTIFACT_BUCKET, Key=key, Body=buf.getvalue(), ContentType="application/json", ContentEncoding="gzip")

def list_plans():
    # brute-force scan; you can add a GSI later
    scan = table.scan()
    plans = [i for i in scan.get("Items", []) if i["Sk"]=="plan#stack" and i["State"] in ("planned","changeset-prepared")]
    return plans

def update_role_state(acct, stack, role, state, backup_key=None):
    table.put_item(Item={
        "Pk": f"{acct}#{stack}",
        "Sk": f"role#{role}",
        "State": state,
        "BackupKey": backup_key,
        "UpdatedAt": dt.datetime.utcnow().isoformat()
    })

def lambda_handler(event, _ctx):
    run_id = event.get("run_id","manual")
    accounts = event.get("accounts") or []
    plans = [p for p in list_plans() if p["Pk"].split("#")[0] in accounts]

    results = []
    for p in plans:
        acct, stack = p["Pk"].split("#",1)
        unused_roles = p.get("UnusedRoles", [])
        sess = assume(acct); iam = sess.client("iam")
        for role in unused_roles:
            # backup trust
            current = iam.get_role(RoleName=role)["Role"]["AssumeRolePolicyDocument"]
            key = f"part2/{run_id}/backups/{acct}-{stack}-{role}.json.gz"
            gzip_put_json(current, key)
            # apply deny
            iam.update_assume_role_policy(RoleName=role, PolicyDocument=json.dumps(DENY_TRUST))
            update_role_state(acct, stack, role, "quarantined", f"s3://{ARTIFACT_BUCKET}/{key}")
        # mark stack as quarantined
        table.update_item(
            Key={"Pk": p["Pk"], "Sk": "plan#stack"},
            UpdateExpression="SET #S=:s, UpdatedAt=:t",
            ExpressionAttributeNames={"#S":"State"},
            ExpressionAttributeValues={":s":"quarantined",":t":dt.datetime.utcnow().isoformat()}
        )
        results.append({"AccountId": acct, "StackName": stack, "Quarantined": len(unused_roles)})
    return {"ok": True, "quarantined": results}
