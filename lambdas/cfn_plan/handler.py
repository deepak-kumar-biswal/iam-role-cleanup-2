import os, json, boto3, copy, datetime as dt
from boto3.dynamodb.conditions import Key

INPUT_TABLE = os.environ["INPUT_TABLE"]
CLEANUP_TABLE = os.environ["CLEANUP_TABLE"]
EXECUTION_ROLE_NAME = os.environ["EXECUTION_ROLE_NAME"]

ddb = boto3.resource("dynamodb")
input_table = ddb.Table(INPUT_TABLE)
cleanup_table = ddb.Table(CLEANUP_TABLE)
sts = boto3.client("sts")

def assume(acct):
    resp = sts.assume_role(RoleArn=f"arn:aws:iam::{acct}:role/{EXECUTION_ROLE_NAME}", RoleSessionName="cleanup-cfn-plan", DurationSeconds=3600)
    c = resp["Credentials"]
    return boto3.Session(aws_access_key_id=c["AccessKeyId"], aws_secret_access_key=c["SecretAccessKey"], aws_session_token=c["SessionToken"])

def get_roles_for_stack(acct, stack):
    pk = f"{acct}#global#{stack}"
    resp = input_table.query(KeyConditionExpression=Key("Pk").eq(pk) & Key("Sk").begins_with("role#"))
    return resp.get("Items", [])

def lambda_handler(event, _ctx):
    accounts = event.get("accounts") or []
    # read plans that are quarantined
    scan = cleanup_table.scan()
    plans = [i for i in scan.get("Items", []) if i["Sk"]=="plan#stack" and i["State"]=="quarantined" and i["Pk"].split("#")[0] in accounts]
    results = []
    for p in plans:
        acct, stack = p["Pk"].split("#",1)
        mode = p["Mode"]
        sess = assume(acct); cfn = sess.client("cloudformation")
        if mode == "all-unused":
            # mark for DeleteStack (no changeset needed)
            cleanup_table.update_item(
                Key={"Pk": p["Pk"], "Sk":"plan#stack"},
                UpdateExpression="SET ChangeSetName=:c, DeleteStack=:d, #S=:s, UpdatedAt=:t",
                ExpressionAttributeNames={"#S":"State"},
                ExpressionAttributeValues={":c":"N/A",":d":True,":s":"changeset-prepared",":t":dt.datetime.utcnow().isoformat()}
            )
            results.append({"AccountId":acct,"StackName":stack,"Plan":"delete-stack"})
            continue

        # mixed: build change set to remove unused roles only
        roles = get_roles_for_stack(acct, stack)
        unused = set(p.get("UnusedRoles", []))
        # Fetch original template (as JSON if possible)
        tpl_resp = cfn.get_template(StackName=stack, TemplateStage="Original")
        body = tpl_resp.get("TemplateBody")
        template = json.loads(body) if isinstance(body, str) and body.strip().startswith("{") else None
        if not template or "Resources" not in template:
            # fallback – skip
            results.append({"AccountId":acct,"StackName":stack,"Plan":"skip-no-template"})
            continue
        # Map physical -> logical for roles
        res = cfn.describe_stack_resources(StackName=stack)["StackResources"]
        to_remove = []
        for r in res:
            if r["ResourceType"]=="AWS::IAM::Role":
                role_name = r["PhysicalResourceId"].split("/")[-1]
                if role_name in unused and r["LogicalResourceId"] in template["Resources"]:
                    to_remove.append(r["LogicalResourceId"])
        if not to_remove:
            results.append({"AccountId":acct,"StackName":stack,"Plan":"no-unused-logicals"})
            continue
        new_template = copy.deepcopy(template)
        for lid in to_remove:
            del new_template["Resources"][lid]
        cs_name = f"remove-unused-{dt.datetime.utcnow().strftime('%Y%m%d%H%M%S')}"
        cfn.create_change_set(
            StackName=stack,
            ChangeSetName=cs_name,
            ChangeSetType="UPDATE",
            UsePreviousTemplate=False,
            TemplateBody=json.dumps(new_template),
            Description="Remove unused IAM roles (automated)"
        )
        cleanup_table.update_item(
            Key={"Pk": p["Pk"], "Sk":"plan#stack"},
            UpdateExpression="SET ChangeSetName=:c, DeleteStack=:d, #S=:s, UpdatedAt=:t",
            ExpressionAttributeNames={"#S":"State"},
            ExpressionAttributeValues={":c":cs_name,":d":False,":s":"changeset-prepared",":t":dt.datetime.utcnow().isoformat()}
        )
        results.append({"AccountId":acct,"StackName":stack,"Plan":"changeset", "ChangeSetName":cs_name, "RemovedLogicalIds": to_remove})
    return {"ok": True, "plans": results}
