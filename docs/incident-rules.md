# Incident rules

Code: `app/services/incident_rules.py`. Change the rules there, not elsewhere.

- An incident opens after **2 consecutive failed checks** (`failures_to_open`).
- It resolves after **1 successful check** (`successes_to_resolve`).
- Only `up` and `down` results count. `pending`, `paused` and `maintenance` are ignored.
- A failed attempt that the monitor will retry is flagged `is_intermediate`. It does not
  count unless `count_intermediate_attempts` is switched on.
- Results further apart than `max_gap` do not form one streak. Build the rule with
  `IncidentRule.for_monitor(...)`, which sets `max_gap` to 3 × (check interval +
  retries × retry interval), so a streak breaks across a pause but survives one slow check.
- At most one unresolved incident exists per monitor (enforced by the database: the
  `incidents_one_active_per_monitor` index).
- Delivery of the alerts is at least once (see `app/services/notifications/`).

## Timestamps

`decide()` returns `since`: when it really happened, not when it was detected.

- Opening: `since` is the **first** failure of the streak. Use it as `started_at`.
- Resolving: `since` is the **first** success after the outage. Use it as `resolved_at`.

With a 1-minute interval and 2 failures to open, the incident is detected one minute
after the outage began. Using `since` keeps reported downtime (emails, status page,
uptime reports) accurate instead of short by one interval.

## Open questions for the team

- **Developer B:** does each retry attempt become its own heartbeat (then set
  `is_intermediate`), or only the final result of a check?
- **Multi-region:** `region` is stored but not used yet. Should one region failing
  open an incident, or a majority of regions?
