# Ansible fleet deployment

`playbook.yml` installs/updates the agent, trusts the supplied monitoring CA,
enforces `SERVER_URL=https://10.54.168.27:5001/api/v1/telemetry`, and forwards
rsyslog over UDP to `10.54.168.27:5001`.

It is safe to run repeatedly. A device with `DEVICE_TOKEN` in
`/etc/agv-monitor/telemetry.conf` is not registered again, so its existing
server token is preserved. The telemetry URL, service files and rsyslog rule
are still checked and corrected. A device with no token is registered from the
device itself, satisfying the server's source-IP protection.

## First use

Copy `inventory.example.yml` to your real inventory and set each device's
`ansible_host`, `agv_monitor_device_ip`, and (optionally) its display name.
`agv_monitor_device_ip` must be the address the device uses to connect to the
monitoring server; it is deliberately separate from SSH access.

Run from this directory:

```sh
ansible-playbook -i inventory.yml playbook.yml
```

For a first-time registration, provide the monitoring web-app administrator
credentials as protected environment variables:

```sh
export AGV_MONITOR_ADMIN_USERNAME=maintenance
export AGV_MONITOR_ADMIN_PASSWORD='...'
ansible-playbook -i inventory.yml playbook.yml
```

Prefer Ansible Vault for CLI use. Do not commit a vault password or any
plaintext credentials.

## AWX / Automation Controller credentials

Create a **Custom Credential Type** named `AGV Monitoring registration` with
these input fields:

```yaml
fields:
  - id: username
    type: string
    label: Monitoring administrator username
  - id: password
    type: string
    label: Monitoring administrator password
    secret: true
required:
  - username
  - password
```

Use this injector configuration:

```yaml
env:
  AGV_MONITOR_ADMIN_USERNAME: '{{ username }}'
  AGV_MONITOR_ADMIN_PASSWORD: '{{ password }}'
```

Attach that credential to the Job Template alongside the normal Machine
credential and inventory. AWX masks the password; the playbook also marks
registration and token tasks `no_log`. The custom credential is only needed
while there are hosts without a device token.
