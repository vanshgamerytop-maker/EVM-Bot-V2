# EVM Bot V1 — Made by EVIL SAAD

Prefix-command Discord bot with invite tracking, SQLite claim records, plan dropdown, and optional LXD container provisioning with randomly selected shared-host ports that retry when the host port is already in use. Ubuntu image default is **`ubuntu:24.04`**.

## One-command dependency installation (Ubuntu/Debian)

Unzip the project, enter its folder, then run:

```bash
cd EVM-Bot-V1
sudo bash install.sh
```

The script installs Python/venv/pip/system dependencies, creates `.venv`, installs `requirements.txt`, creates `.env` if absent, and checks Python syntax. It does **not** start the bot or enable VPS provisioning automatically.

## Configure and start

```bash
nano .env
```

Set `DISCORD_TOKEN` and `OWNER_ID`. Keep `ENABLE_LXD_PROVISIONING=false` until LXD storage quotas, networking, public IP routing, and firewall rules are tested. Then start:

```bash
.venv/bin/python bot.py
```

## LXD image

The configured image is `ubuntu:24.04`. Verify it on the host:

```bash
lxc image list ubuntu:
lxc launch ubuntu:24.04 evm-test
lxc exec evm-test -- cat /etc/os-release
```

If the test container already exists, choose a different name or remove it only if you no longer need it.

## Discord setup

In Discord Developer Portal, enable **Message Content Intent** and **Server Members Intent**. For invite tracking, the bot needs permission to inspect server invites (Manage Server). Use the minimum permissions required for your server.

## Prefix commands

Default prefix is `$`: `$home`, `$help`, `$ping`, `$plans`, `$invites`, `$vps create`, `$claim`, `$ports list`. Admins can use `$addinvite @user 2`, `$setplan XS 6 2 25 6`, and `$setprefix !`.

## Provisioning limitations and safety

- LXD creates **containers**, not full KVM virtual machines.
- The host must have LXD installed and initialized; the `lxc` command must work as the same Linux user running the bot.
- `Pinggy TCP tunnel` must be an IPv4 address actually assigned/routed to the host. A private address such as `10.x.x.x` is not a public endpoint.
- Provider/network firewall must allow the selected TCP port range and upstream NAT must forward it if applicable.
- The container gets port forwarding to TCP 22 and 25565, but this bot does not install or configure SSH/Minecraft services inside the container. Those services must be installed separately.
- Do not set `ENABLE_LXD_PROVISIONING=true` until you have verified the above. Use only infrastructure you own or are authorized to administer.

## VPS management + private credentials

After successful provisioning, the bot attempts to DM the owner the SSH endpoint, username, generated password, container IPv4, and game port. If DMs are closed, open DMs from server members and use `!manage <claim_id>` (with your configured prefix) to request details by DM.

Commands (default prefix `$`):
- `$manage` — list your claims
- `$manage 7` — DM connection details
- `$manage 7 password` — DM connection details again
- `$manage 7 start`, `$manage 7 stop`, `$manage 7 restart` — lifecycle actions
- `$manage 7 reinstall CONFIRM` — destructive reinstall; deletes container data and creates a fresh Ubuntu 24.04 container with new credentials

Only the VPS owner (or server admin / configured bot owner) can manage a claim. Keep the database and `.env` private: the generated login password is stored in SQLite so it can be re-sent by DM. For production, protect the database file and host backups.


## Password-based SSH login (Ubuntu 24.04)

New deployments configure the `evmuser` account with a generated password and enable SSH password authentication using `/etc/ssh/sshd_config.d/99-evm-password-auth.conf`. The bot sends the endpoint, username, and generated password by Discord DM after successful provisioning. The bot does not create or require an SSH private key.

For an SSH client that only offers “Import/Add key”, switch its connection/authentication method to **Password** (or use a password-capable SSH client). A key-only client cannot accept a password merely because the server supports password authentication.

The configured public IP and allocated TCP port must be reachable through the VPS provider's firewall/NAT. Password authentication over the public internet has security trade-offs; use a strong generated password and change it after first login.


## Random ports and public IP notes

Each successful deployment chooses random, distinct SSH/game ports from `PORT_RANGE_START`–`PORT_RANGE_END`, checks actual LXD proxy binding by attempting to add the proxy devices, and retries other candidates when a port is already bound. If no two ports can be bound, deployment fails cleanly and the container is removed. The public IPv4 is **not randomly invented**: set `Pinggy TCP tunnel` to the real public/routed address of your host. If you have only one public IPv4, deployments share that IP and get different ports. A private host address (10.x/172.16–31.x/192.168.x) is not directly reachable from the public internet unless the provider forwards/routes it.


## SSHX browser terminal

For a VPS that you own, run ` $manage <claim_id> sshx ` in the Discord server.
The bot installs SSHX inside that container (if needed), starts a terminal as the
unprivileged `evmuser`, and DMs the detected session URL to the VPS owner.

**Security:** anyone who obtains an SSHX session URL may be able to use the shared
terminal. Keep the URL private and stop the SSHX process when finished. The bot
does not post the link in a public channel. This feature requires outbound internet
access from the container and successful SSHX service connectivity.


SSH login for new deployments is configured as `root` with a generated password. SSHX runs as root in `/root`; keep its session URL private because it grants root-level terminal access.


## Login DM format and networking
The login DM uses a RazorCloud-inspired field layout (server IP, SSH port, username, SSH command, password, game address, OS and claim ID). Values are populated from this bot's own configured `Pinggy TCP tunnel` and allocated LXD proxy ports. Do not copy another provider's IP, ports, or credentials: those belong to their infrastructure. Configure `Pinggy TCP tunnel` to an address assigned/routed to your own host, and ensure the provider firewall allows the selected port range.


### Pinggy SSH tunnel
This build uses a Pinggy TCP reverse tunnel for SSH and no longer requires `PUBLIC_IPV4`. The bot host must have the OpenSSH client installed and outbound access to `free.pinggy.io:443`. Free Pinggy endpoints may change and free tunnels may expire; keep the bot process running. The DM includes the hostname, allocated port, and a Termux command. This mode publishes SSH only; it does not publish Minecraft/game ports.


## Admin VPS lifecycle commands

Admins (or the configured bot owner) can manage VPS instances with the configured prefix (default `$`):

- `$vpsadmin help`
- `$vpsadmin create @member PLAN [days]` — provision a VPS for any server member without invite requirements; expiry defaults to 30 days, use `0` for no expiry.
- `$vpsadmin delete <claim_id>` — delete the container and its saved credentials/mapping.
- `$vpsadmin suspend <claim_id>` — stop the container.
- `$vpsadmin unsuspend <claim_id>` — start the container again.
- `$vpsadmin expiry <claim_id> <days>` — set expiry from now; `0` removes expiry.

Expired VPS containers are stopped automatically by the bot's periodic expiry worker. Deletion permanently removes the container data. Test on a non-production VPS first. Free Pinggy tunnels may expire independently and may not provide a stable 24/7 endpoint.
