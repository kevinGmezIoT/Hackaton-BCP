# RB-091: TLS Certificate Renewal Failure

Conditions: SSLHandshakeException or 502 Bad Gateway errors on ingress or OAuth endpoints.

## Steps

1. Check certificate expiry timestamp on the affected ingress secret.
2. Trigger manual certificate issuance and verify DNS TXT record propagation.
3. Reload ingress controller pods after secret update.

