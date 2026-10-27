The NGINX rate and connection limit options defaulted to 20r/s, a burst
of 60 and 100 connections per client IP unconditionally, so upgrading
silently started throttling API clients on unhardened deployments.

Unset values now act as the "unconfigured" sentinel, matching the
convention already used for the BIND9 hardening options: the defaults
are applied only when hardening is active, and setting
api_rate_limit_rate explicitly still enables limiting without
hardening. Unset (rather than 0) is used for api_rate_limit_burst and
api_conn_limit so a burst of 0 (reject excess requests instead of
queueing them) stays configurable; both are validated with Int so a
fractional value cannot reach the NGINX config.

Resolves: LP:2171421