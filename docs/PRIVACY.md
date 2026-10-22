# Repository privacy notice — quantura-forecast-workers

Effective October 22, 2026. Contact: hello@quantura.studio.

## Scope

This notice covers the repository distribution and its documented local execution
paths. It is not a claim that every deployment shares the same data processing or
that a deployment is certified compliant with any privacy law. The operator of a
hosted deployment must publish its actual processing details and applicable rights.

## What can be processed

Purpose: quantura forecast workers.

Depending on the interface you enable, code can process uploaded datasets, model
inputs and predictions, identifiers and tenant/role information, execution records,
audit events, request timing, and error categories. Reports may reproduce supplied
data. Treat datasets, exports, checkpoints, application databases, request logs,
and backups as private unless you deliberately publish sanitized versions.

Current architecture: web/control-plane workers with independent model execution.
Current communications: GitHub workflow control and HTTPS downloads; model inference itself is one worker.

## Storage, recipients and retention

Local analysis stores data where you configure its files or database; some legacy
dashboards hold session and result state in memory. There is no universal automatic
deletion deadline across these interfaces. Deleting a browser session does not
necessarily delete exported files, database records, Git history or backups.
An operator must choose and enforce retention, deletion and backup-expiry periods
before collecting real customer data, and verify deletion in each storage location.

Enabled cloud/provider adapters, package/model downloads and remote transports
can communicate with separately operated services. Review those services' terms,
regions, retention and privacy notices. Do not assume local execution if you enable
a remote adapter. The GitHub hosting platform separately processes account and
repository activity under its privacy statement.

## Operator controls and requests

Minimize data collected, restrict access, encrypt sensitive storage and transit,
keep credentials in a secret manager, and avoid recording raw inputs or credentials
in shared logs. Use synthetic or properly authorized deidentified data for examples.
Contact the deployment operator to request access, correction, deletion, export or
other applicable rights; identity and lawful retention exceptions must be assessed
for that deployment. For maintainer-held support correspondence, contact the email
above. Do not send sensitive datasets or credentials in a public GitHub issue.

## Hosted application policy

Hosted account, billing, analytics, cookie and provider processing must be described
by the actual service policy. Existing application privacy/consent implementations
remain authoritative for that service and are not replaced by this local notice.
