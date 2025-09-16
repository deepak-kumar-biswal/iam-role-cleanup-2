# IAM Cleanup – Part 2 (Quarantine & Deletion via CloudFormation)

This stack **consumes the Part 1 DynamoDB model** (`IamStackRoleUsage-<stack-name>`) and performs **safe cleanup**:
- Quarantines **unused** roles (deny-all trust) with S3 backups for instant rollback
- For **all-unused** stacks: **DeleteStack** (with safety checks)
- For **mixed** stacks: creates & executes **Change Sets** that remove only the unused IAM Role resources
- Optionally deletes **non‑CFN** roles (if encountered) after quarantine & detachment
- Tracks lifecycle in a dedicated **CleanupStatus** DynamoDB table

All automation is **CloudFormation-only** (Lambdas + Step Functions). No Terraform required.

---

## Architecture

- **Input table (from Part 1):** `IamStackRoleUsage-<part1-stack-name>`
  - Per-role rows: `Pk=<AccountId>#global#<StackName>`, `Sk=role#<RoleName>`, fields include `Used` and `RoleArn`
  - Per-stack summary: `Sk=summary#stack`, `Summary.State` ∈ `all-unused|mixed|all-used|pending`
- **This stack creates:**
  - **CleanupStatus** table – track per-stack plan & execution state
  - Lambdas:
    - `planner` – reads Part 1 summaries; writes CleanupStatus plan entries
    - `quarantine` – backs up trust policy & applies deny-all on unused roles
    - `cfn_plan` – builds CFN change sets (mixed) or marks stack for deletion (all-unused)
    - `cfn_exec` – executes change sets and/or deletes stacks
    - `finalize` – performs post-ops (delete non-CFN roles if any; mark status)
    - `notifier` – webhook/Slack messages
  - **Step Functions** orchestrator to run end-to-end

> NOTE: You must already have the **target execution role** (from Part 1) in each account. For Part 2, that role needs **mutating permissions** (IAM update/delete, CFN update/delete). Template provided below.

---

## Deploy

1) Ensure Part 1 is deployed and populated (Identification completed).
2) Deploy/Update the **target execution role** in all target accounts using `target-execution-role-part2.yaml`.
3) Zip & upload the Lambdas in `lambdas/*` to your S3 code bucket.
4) Deploy `cleanup-orchestrator.yaml` in the central account.

### Parameters (central stack)
- `InputTableName` – the **Part 1** table name, e.g. `IamStackRoleUsage-<part1-stack-name>`
- `CleanupTableName` – name for the Part 2 status table (default provided)
- `ExecutionRoleName` – cross-account role name (same as Part 1, e.g. `IAMCleanupExecutionRole`)
- `TargetAccountIds` – comma-delimited list of accounts
- `LambdaCodeBucket`, `PlannerKey`, `QuarantineKey`, `CFNPlanKey`, `CFNExecKey`, `FinalizeKey`, `NotifierKey`
- `SlackWebhookSSMParam` – optional webhook (SSM SecureString param name)

### Run
Start the state machine with:
```json
{
  "run_id": "cleanup-pilot",
  "accounts": ["111111111111","222222222222"],
  "modes": ["quarantine","plan","execute"]   // omit steps you don’t want
}
```

---

## Safety model

- **Quarantine-first**: roles are disabled by trust-policy **before** any deletion.
- **CFN-native**: change sets for **mixed** stacks and **DeleteStack** only for **all-unused**.
- **Backups**: original trust policies are stored in S3 (key prefix includes run_id).
- **Idempotent**: re-runs will skip completed steps by checking CleanupStatus rows.

---

## DynamoDB – CleanupStatus schema

**Table:** `<CleanupTableName>` (PAY_PER_REQUEST)

- **PK** `Pk` = `<AccountId>#<StackName>`
- **SK** `Sk` = one of:
  - `plan#stack` – plan doc for the stack
  - `role#<RoleName>` – per-role cleanup state
  - `exec#stack` – execution summary for the stack

**Items**
- `plan#stack`:
  ```json
  {
    "Pk":"111111111111#AppStack","Sk":"plan#stack",
    "State":"planned|quarantined|changeset-prepared|executed|deleted|skipped|error",
    "Mode":"all-unused|mixed|all-used|pending",
    "UnusedRoles":["RoleA","RoleB"],
    "UsedRoles":["RoleC"],
    "ChangeSetName":"remove-unused-2025-09-16",
    "DeleteStack": false,
    "UpdatedAt":"ISO8601"
  }
  ```
- `role#<RoleName>`:
  ```json
  {
    "Pk":"111111111111#AppStack","Sk":"role#AppRole",
    "State":"quarantined|deleted|kept|error",
    "BackupKey":"s3://.../backups/111111111111-AppStack-AppRole.json.gz",
    "UpdatedAt":"ISO8601"
  }
  ```

---

## Target Role (Part 2 permissions)

Use `target-execution-role-part2.yaml` to **update** the role in every target account. It grants:
- CFN update/delete, change sets
- IAM update assume role policy, detach inline/managed, instance profile removal, delete role

---

## Rollback

- Restore a role’s trust policy from S3 backup:
  - Assume into target account with the execution role
  - `aws iam update-assume-role-policy --role-name <RoleName> --policy-document file://backup.json`

---

## License

MIT
