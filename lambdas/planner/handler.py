import os, boto3, datetime as dt
from boto3.dynamodb.conditions import Key

INPUT_TABLE = os.environ["INPUT_TABLE"]
CLEANUP_TABLE = os.environ["CLEANUP_TABLE"]

ddb = boto3.resource("dynamodb")
input_table = ddb.Table(INPUT_TABLE)
cleanup_table = ddb.Table(CLEANUP_TABLE)

def list_stacks_from_input(accounts):
    # Query Part1 summaries via GSI1 (Gsi1Pk='summary#stack')
    # If GSI not available to cross-table, fallback to scan.
    stacks = []
    scan = input_table.scan(ProjectionExpression="Pk, Sk, AccountId, StackName, Summary")
    for i in scan.get("Items", []):
        if i["Sk"] == "summary#stack" and i.get("AccountId") in accounts:
            stacks.append(i)
    return stacks

def put_plan_item(acct, stack, mode, unused_roles, used_roles):
    cleanup_table.put_item(Item={
        "Pk": f"{acct}#{stack}",
        "Sk": "plan#stack",
        "Mode": mode,
        "UnusedRoles": unused_roles,
        "UsedRoles": used_roles,
        "DeleteStack": True if mode == "all-unused" else False,
        "State": "planned",
        "UpdatedAt": dt.datetime.utcnow().isoformat()
    })

def lambda_handler(event, _ctx):
    accounts = event.get("accounts") or []
    stacks = list_stacks_from_input(accounts)
    planned = []
    for s in stacks:
        summary = (s.get("Summary") or {})
        mode = summary.get("State", "pending")
        if mode not in ("all-unused","mixed"):
            # Skip all-used/pending stacks
            continue
        # Fetch role rows to know which are unused vs used
        pk = f"{s['AccountId']}#global#{s['StackName']}"
        # Query role items
        resp = input_table.query(KeyConditionExpression=Key("Pk").eq(pk) & Key("Sk").begins_with("role#"))
        unused = [r["RoleName"] for r in resp.get("Items", []) if r.get("Used")=="unused"]
        used   = [r["RoleName"] for r in resp.get("Items", []) if r.get("Used")=="used"]
        put_plan_item(s["AccountId"], s["StackName"], mode, unused, used)
        planned.append({"AccountId": s["AccountId"], "StackName": s["StackName"], "Mode": mode, "UnusedCount": len(unused), "UsedCount": len(used)})
    return {"planned": planned}
