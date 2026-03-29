"""
OpenCPO Bastion — High Availability Module

Two identical gateway units at a site. One active, one standby.
Chargers connect to a VRRP virtual IP — failover is transparent (<3 seconds).

Architecture:
  - UDP broadcast on port 7701 for peer discovery / heartbeat
  - TCP on port 7700 for encrypted state replication
  - keepalived manages VRRP / VIP ownership
  - Shared encryption key auto-generated on first pairing, stored in keyvault

Roles:
  active   — owns the VIP, processes all charger traffic
  standby  — monitors active, receives state replication, ready to take over
  alone    — no peer found after 10s, operating in standalone mode

Split-brain protection:
  - Both units check Tailscale tunnel health independently
  - Only unit with working Tailscale tunnel can be active
  - If neither has tunnel → both run independently (degraded mode)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Protocol constants ────────────────────────────────────────────────────────

DISCOVERY_MAGIC = b"OPENCPO_HA_HELLO"
HEARTBEAT_MAGIC = b"OPENCPO_HA_BEAT"
STATE_MAGIC = b"OPENCPO_HA_STATE"

DISCOVERY_TIMEOUT = 10.0    # seconds to wait for peer before going standalone
REPLICATION_VERSION = 1


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class PeerInfo:
    ip: str
    priority: int
    has_tunnel: bool
    role: str          # "active" | "standby" | "alone"
    hostname: str
    last_seen: float = field(default_factory=time.monotonic)

    def is_alive(self, threshold: int = 5) -> bool:
        return (time.monotonic() - self.last_seen) < threshold


@dataclass
class ReplicatedState:
    """State snapshot sent from active → standby every sync_interval seconds."""
    timestamp: float = 0.0
    ocpp_sessions: list[dict] = field(default_factory=list)
    charger_registry: dict[str, dict] = field(default_factory=dict)
    cctv_cameras: list[dict] = field(default_factory=list)
    lpr_cache: list[dict] = field(default_factory=list)
    sensor_alerts: dict[str, dict] = field(default_factory=dict)

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self)).encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> "ReplicatedState":
        d = json.loads(data.decode())
        return cls(**d)


# ── keepalived config generation ─────────────────────────────────────────────

def generate_keepalived_conf(config) -> str:
    """
    Generate keepalived.conf from gateway HA config.

    config is a GatewayConfig with a .ha attribute (HAConfig).
    """
    ha = config.ha
    vip = ha.virtual_ip or _derive_vip(ha.interface)

    # Active unit gets higher priority; standby gets lower
    if ha.role == "primary":
        priority = max(ha.priority, 150)
        state = "MASTER"
    elif ha.role == "secondary":
        priority = min(ha.priority, 100)
        state = "BACKUP"
    else:
        # auto — will be set dynamically by negotiation
        priority = ha.priority
        state = "BACKUP"

    conf = f"""\
! OpenCPO Bastion keepalived configuration
! Auto-generated — do not edit manually. Regenerated on startup.

global_defs {{
    router_id opencpo_bastion_{socket.gethostname()}
    script_user root
    enable_script_security
}}

vrrp_script chk_tunnel {{
    script "/usr/local/bin/opencpo-check-tunnel.sh"
    interval 2
    weight -50
    fall 2
    rise 2
}}

vrrp_instance OPENCPO_GW {{
    state {state}
    interface {ha.interface}
    virtual_router_id {ha.vrrp_router_id}
    priority {priority}
    advert_int 1
    preempt_delay 10

    authentication {{
        auth_type PASS
        auth_pass {_keepalived_auth_pass(ha.encryption_key)}
    }}

    virtual_ipaddress {{
        {vip}/24 dev {ha.interface} label {ha.interface}:vip
    }}

    track_script {{
        chk_tunnel
    }}

    notify_master "/usr/local/bin/opencpo-ha-notify.sh MASTER"
    notify_backup "/usr/local/bin/opencpo-ha-notify.sh BACKUP"
    notify_fault  "/usr/local/bin/opencpo-ha-notify.sh FAULT"
}}
"""
    return conf


def _derive_vip(interface: str) -> str:
    """Derive a virtual IP from the interface's current IP (last octet → .100)."""
    try:
        import netifaces  # type: ignore
        addrs = netifaces.ifaddresses(interface)
        ip = addrs[netifaces.AF_INET][0]["addr"]
        parts = ip.split(".")
        parts[-1] = "100"
        return ".".join(parts)
    except Exception:
        # Fallback: use a sensible default for 192.168.x.x charger LANs
        return "192.168.100.100"


def _keepalived_auth_pass(key: str) -> str:
    """Derive an 8-char keepalived auth pass from the shared key."""
    if not key:
        return "opencpo0"
    import hashlib
    h = hashlib.sha256(key.encode()).hexdigest()
    return h[:8]


def start_keepalived() -> None:
    """Write config and start/reload keepalived."""
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "keepalived"],
            capture_output=True, text=True
        )
        if result.stdout.strip() == "active":
            subprocess.run(["systemctl", "reload", "keepalived"], check=True)
            logger.info("keepalived reloaded")
        else:
            subprocess.run(["systemctl", "start", "keepalived"], check=True)
            logger.info("keepalived started")
    except subprocess.CalledProcessError as e:
        logger.error("Failed to start/reload keepalived: %s", e)
    except FileNotFoundError:
        logger.warning("systemctl not found — running outside systemd, skipping keepalived")


def stop_keepalived() -> None:
    """Stop keepalived (used during graceful failover)."""
    try:
        subprocess.run(["systemctl", "stop", "keepalived"], check=True)
        logger.info("keepalived stopped")
    except Exception as e:
        logger.warning("Failed to stop keepalived: %s", e)


def write_keepalived_conf(conf: str, path: str = "/etc/keepalived/keepalived.conf") -> None:
    """Write keepalived config to disk."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(conf)
    logger.debug("keepalived config written to %s", path)


def write_keepalived_scripts() -> None:
    """Write helper shell scripts used by keepalived."""
    # Tunnel check script — keepalived calls this to adjust VRRP priority
    check_script = """\
#!/bin/bash
# Returns 0 if Tailscale tunnel is up, 1 if down
tailscale status --json 2>/dev/null | python3 -c "
import sys, json
d = json.load(sys.stdin)
sys.exit(0 if d.get('BackendState') == 'Running' else 1)
" 2>/dev/null || exit 1
"""

    notify_script = """\
#!/bin/bash
# Called by keepalived on role transitions
# $1 = MASTER | BACKUP | FAULT
ROLE="$1"
logger -t opencpo-ha "keepalived transition: $ROLE"
# Signal the Python HA manager via a flag file
echo "$ROLE" > /run/opencpo-ha-role
"""

    for path, content in [
        ("/usr/local/bin/opencpo-check-tunnel.sh", check_script),
        ("/usr/local/bin/opencpo-ha-notify.sh", notify_script),
    ]:
        try:
            Path(path).write_text(content)
            Path(path).chmod(0o755)
        except PermissionError:
            logger.warning("Cannot write %s (not root) — keepalived scripts skipped", path)


# ── HA Manager ────────────────────────────────────────────────────────────────

class HAManager:
    """
    Manages peer discovery, role negotiation, state replication, and failover.

    Usage:
        ha = HAManager(config, vault)
        await ha.start()          # blocks until role is decided
        if ha.is_active:
            bind_proxy_to_vip()
        # pass ha to other modules so they can call ha.get_replicated_state()
        asyncio.create_task(ha.run())
    """

    def __init__(self, config, vault=None):
        self.config = config
        self.ha = config.ha
        self.vault = vault

        self._role: str = "alone"            # "active" | "standby" | "alone"
        self._peer: Optional[PeerInfo] = None
        self._state = ReplicatedState()
        self._last_sync: float = 0.0
        self._replication_lag: float = 0.0
        self._encryption_key: bytes = b""
        self._running = False
        self._failover_event = asyncio.Event()
        self._vip_owner: Optional[str] = None

        # Flag file written by keepalived notify script
        self._role_flag = Path("/run/opencpo-ha-role")

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_active(self) -> bool:
        return self._role == "active"

    @property
    def is_standby(self) -> bool:
        return self._role == "standby"

    @property
    def is_standalone(self) -> bool:
        return self._role == "alone"

    @property
    def role(self) -> str:
        return self._role

    @property
    def virtual_ip(self) -> str:
        return self.ha.virtual_ip or _derive_vip(self.ha.interface)

    def get_status(self) -> dict:
        peer_info = None
        if self._peer:
            peer_info = {
                "ip": self._peer.ip,
                "role": self._peer.role,
                "priority": self._peer.priority,
                "has_tunnel": self._peer.has_tunnel,
                "hostname": self._peer.hostname,
                "alive": self._peer.is_alive(self.ha.failover_threshold * 2),
            }
        return {
            "enabled": self.ha.enabled != "false",
            "role": self._role,
            "vip": self.virtual_ip,
            "vip_owner": self._vip_owner,
            "peer": peer_info,
            "last_sync": self._last_sync,
            "replication_lag": round(self._replication_lag, 3),
            "ha_mode": self._role != "alone",
        }

    def update_replicated_state(self, **kwargs) -> None:
        """Called by other modules to push their state into the replication snapshot."""
        for k, v in kwargs.items():
            if hasattr(self._state, k):
                setattr(self._state, k, v)

    def get_replicated_state(self) -> ReplicatedState:
        return self._state

    # ── Startup ───────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Run peer discovery. Returns once role is decided.
        Caller should then start run() as a background task.
        """
        if self.ha.enabled == "false":
            logger.info("HA disabled by config — standalone mode")
            self._role = "alone"
            return

        await self._load_or_generate_key()

        if self.ha.enabled == "auto":
            logger.info("HA mode: auto — broadcasting for peer on %s:%d ...",
                        self.ha.interface, self.ha.heartbeat_port)
            peer = await self._discover_peer()
        else:
            # enabled=true means HA is forced on; peer discovery still needed
            peer = await self._discover_peer()

        if peer is None:
            logger.info("No peer found after %.0fs — operating standalone", DISCOVERY_TIMEOUT)
            self._role = "alone"
            return

        self._peer = peer
        await self._negotiate_role()
        self._setup_keepalived()
        self._running = True
        logger.info("HA initialized: role=%s, peer=%s (%s)", self._role, peer.ip, peer.role)

    async def run(self) -> None:
        """Background task: heartbeat sender + replication loop."""
        if self._role == "alone":
            return  # nothing to do in standalone mode

        await asyncio.gather(
            self._heartbeat_loop(),
            self._heartbeat_receiver(),
            self._replication_loop(),
            self._role_monitor(),
        )

    # ── Peer discovery ────────────────────────────────────────────────────────

    async def _discover_peer(self) -> Optional[PeerInfo]:
        """
        Broadcast discovery packet on charger LAN. Wait up to DISCOVERY_TIMEOUT.
        Returns first responding peer, or None.
        """
        loop = asyncio.get_running_loop()
        found: Optional[PeerInfo] = asyncio.Queue()  # type: ignore
        result_q: asyncio.Queue[Optional[PeerInfo]] = asyncio.Queue()

        # Start listener first
        listener_task = asyncio.create_task(
            self._discovery_listener(result_q)
        )

        # Broadcast every 1s until timeout
        deadline = asyncio.get_event_loop().time() + DISCOVERY_TIMEOUT
        hostname = socket.gethostname()
        has_tunnel = self._check_tunnel()

        payload = json.dumps({
            "magic": DISCOVERY_MAGIC.decode(),
            "hostname": hostname,
            "priority": self.ha.priority,
            "has_tunnel": has_tunnel,
            "role": self.ha.role,
            "peer_port": self.ha.peer_port,
        }).encode()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setblocking(False)

            while asyncio.get_event_loop().time() < deadline:
                try:
                    await loop.sock_sendto(sock, payload, ("<broadcast>", self.ha.heartbeat_port))
                except OSError as e:
                    logger.debug("Discovery broadcast error: %s", e)

                try:
                    peer = await asyncio.wait_for(result_q.get(), timeout=1.0)
                    if peer:
                        listener_task.cancel()
                        return peer
                except asyncio.TimeoutError:
                    pass

        finally:
            sock.close()
            listener_task.cancel()

        return None

    async def _discovery_listener(self, result_q: asyncio.Queue) -> None:
        """Listen for discovery broadcasts from peer."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.ha.heartbeat_port))
        sock.setblocking(False)
        own_hostname = socket.gethostname()

        try:
            while True:
                try:
                    data, addr = await loop.sock_recvfrom(sock, 4096)
                    msg = json.loads(data.decode())
                    if msg.get("magic") != DISCOVERY_MAGIC.decode():
                        continue
                    if msg.get("hostname") == own_hostname:
                        continue  # our own broadcast

                    peer = PeerInfo(
                        ip=addr[0],
                        priority=msg.get("priority", 100),
                        has_tunnel=msg.get("has_tunnel", False),
                        role=msg.get("role", "auto"),
                        hostname=msg.get("hostname", addr[0]),
                    )
                    await result_q.put(peer)
                    return
                except (json.JSONDecodeError, KeyError):
                    pass
        finally:
            sock.close()

    # ── Role negotiation ──────────────────────────────────────────────────────

    async def _negotiate_role(self) -> None:
        """
        Determine active/standby role based on:
        1. Explicit config (role: primary/secondary)
        2. Tunnel health (unit with tunnel wins)
        3. VRRP priority
        4. Hostname tiebreak (alphabetical)
        """
        ha = self.ha
        peer = self._peer
        own_tunnel = self._check_tunnel()
        own_hostname = socket.gethostname()

        # Explicit config wins
        if ha.role == "primary":
            self._role = "active"
            return
        if ha.role == "secondary":
            self._role = "standby"
            return

        # Tunnel-based split-brain protection
        if own_tunnel and not peer.has_tunnel:
            self._role = "active"
        elif peer.has_tunnel and not own_tunnel:
            self._role = "standby"
        elif ha.priority > peer.priority:
            self._role = "active"
        elif peer.priority > ha.priority:
            self._role = "standby"
        else:
            # Tiebreak by hostname (consistent, deterministic)
            self._role = "active" if own_hostname < peer.hostname else "standby"

        # Update peer's expected role
        peer.role = "standby" if self._role == "active" else "active"

    # ── keepalived setup ──────────────────────────────────────────────────────

    def _setup_keepalived(self) -> None:
        """Generate config, write scripts, start keepalived."""
        # Temporarily override role for config generation
        original_role = self.ha.role
        self.ha.role = "primary" if self._role == "active" else "secondary"

        try:
            conf = generate_keepalived_conf(self.config)
            write_keepalived_conf(conf)
            write_keepalived_scripts()
            start_keepalived()
        except Exception as e:
            logger.error("keepalived setup failed: %s", e)
        finally:
            self.ha.role = original_role

    # ── Heartbeat loop ────────────────────────────────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Send UDP heartbeat to peer every heartbeat_interval seconds."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        own_hostname = socket.gethostname()

        try:
            while self._running:
                if self._peer:
                    payload = json.dumps({
                        "magic": HEARTBEAT_MAGIC.decode(),
                        "role": self._role,
                        "has_tunnel": self._check_tunnel(),
                        "hostname": own_hostname,
                        "priority": self.ha.priority,
                        "ts": time.time(),
                    }).encode()
                    try:
                        await loop.sock_sendto(
                            sock, payload,
                            (self._peer.ip, self.ha.heartbeat_port)
                        )
                    except OSError as e:
                        logger.debug("Heartbeat send error: %s", e)

                await asyncio.sleep(self.ha.heartbeat_interval)
        finally:
            sock.close()

    async def _heartbeat_receiver(self) -> None:
        """Receive heartbeats from peer. Track liveness."""
        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", self.ha.heartbeat_port))
        sock.setblocking(False)
        own_hostname = socket.gethostname()
        miss_count = 0

        try:
            while self._running:
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, 4096),
                        timeout=self.ha.heartbeat_interval * 2,
                    )
                    msg = json.loads(data.decode())
                    if msg.get("magic") != HEARTBEAT_MAGIC.decode():
                        continue
                    if msg.get("hostname") == own_hostname:
                        continue

                    if self._peer:
                        self._peer.last_seen = time.monotonic()
                        self._peer.has_tunnel = msg.get("has_tunnel", False)
                        self._peer.role = msg.get("role", self._peer.role)
                    miss_count = 0

                except asyncio.TimeoutError:
                    miss_count += 1
                    if miss_count >= self.ha.failover_threshold:
                        logger.warning(
                            "Peer missed %d heartbeats — peer may be down", miss_count
                        )
                        if self._role == "standby":
                            logger.info("Peer appears down — keepalived will take over VIP")
                except (json.JSONDecodeError, KeyError):
                    pass
        finally:
            sock.close()

    # ── State replication ─────────────────────────────────────────────────────

    async def _replication_loop(self) -> None:
        """
        Active → Standby: send encrypted state snapshot every sync_interval.
        Standby: listen for state from active.
        """
        if self._role == "active":
            await self._replication_sender()
        else:
            await self._replication_receiver()

    async def _replication_sender(self) -> None:
        """TCP server that pushes state to standby every sync_interval."""
        while self._running:
            await asyncio.sleep(self.ha.sync_interval)

            if not self._peer or self._role != "active":
                continue

            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self._peer.ip, self.ha.peer_port),
                    timeout=5.0,
                )
                self._state.timestamp = time.time()
                raw = self._state.to_bytes()
                encrypted = self._encrypt(raw)

                # Send length-prefixed frame
                length = struct.pack(">I", len(encrypted))
                writer.write(STATE_MAGIC + length + encrypted)
                await writer.drain()
                writer.close()
                await writer.wait_closed()

                self._last_sync = time.time()
                logger.debug("State replicated to peer %s (%d bytes)", self._peer.ip, len(raw))

            except asyncio.TimeoutError:
                logger.warning("State replication timeout — peer unreachable")
            except ConnectionRefusedError:
                logger.debug("Standby replication port not ready yet")
            except Exception as e:
                logger.warning("Replication error: %s", e)

    async def _replication_receiver(self) -> None:
        """TCP listener that receives state from active."""
        try:
            server = await asyncio.start_server(
                self._handle_replication_connection,
                "0.0.0.0",
                self.ha.peer_port,
            )
            async with server:
                await server.serve_forever()
        except OSError as e:
            logger.error("Cannot start replication listener on port %d: %s",
                         self.ha.peer_port, e)

    async def _handle_replication_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            magic = await asyncio.wait_for(reader.read(len(STATE_MAGIC)), timeout=5.0)
            if magic != STATE_MAGIC:
                return

            length_bytes = await asyncio.wait_for(reader.read(4), timeout=5.0)
            length = struct.unpack(">I", length_bytes)[0]

            if length > 10 * 1024 * 1024:  # 10MB sanity limit
                logger.warning("Replication packet too large: %d bytes", length)
                return

            encrypted = await asyncio.wait_for(reader.read(length), timeout=10.0)
            raw = self._decrypt(encrypted)
            new_state = ReplicatedState.from_bytes(raw)

            lag = time.time() - new_state.timestamp
            self._replication_lag = lag
            self._state = new_state
            self._last_sync = time.time()
            logger.debug("Received state from active (lag=%.3fs)", lag)

        except asyncio.TimeoutError:
            logger.warning("Replication connection timed out")
        except Exception as e:
            logger.warning("Replication receive error: %s", e)
        finally:
            writer.close()

    # ── Role monitor ─────────────────────────────────────────────────────────

    async def _role_monitor(self) -> None:
        """
        Watch keepalived role flag file for transitions.
        Also handles split-brain detection.
        """
        last_role = self._role

        while self._running:
            await asyncio.sleep(2.0)

            # Check keepalived notify flag
            if self._role_flag.exists():
                try:
                    new_role_raw = self._role_flag.read_text().strip()
                    self._role_flag.unlink(missing_ok=True)

                    if new_role_raw == "MASTER" and self._role != "active":
                        logger.info("keepalived: promoted to ACTIVE")
                        self._role = "active"
                        self._vip_owner = socket.gethostname()
                        # Switch replication direction
                        asyncio.create_task(self._replication_sender())

                    elif new_role_raw == "BACKUP" and self._role != "standby":
                        logger.info("keepalived: demoted to STANDBY")
                        self._role = "standby"

                    elif new_role_raw == "FAULT":
                        logger.warning("keepalived: FAULT state")

                except Exception as e:
                    logger.warning("Role flag read error: %s", e)

            # Split-brain: if neither has tunnel, both go degraded
            if self._peer and not self._check_tunnel() and not self._peer.has_tunnel:
                if last_role != "alone":
                    logger.warning(
                        "Split-brain: neither unit has Tailscale tunnel — degraded mode"
                    )
                    # Don't fight over VIP — let keepalived handle its priority
                    # Both units operate on their own local IP

    # ── Tunnel check ─────────────────────────────────────────────────────────

    def _check_tunnel(self) -> bool:
        """Check if Tailscale tunnel is up."""
        try:
            r = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True, text=True, timeout=3,
            )
            import json as _json
            d = _json.loads(r.stdout)
            return d.get("BackendState") == "Running"
        except Exception:
            return False

    # ── Encryption ────────────────────────────────────────────────────────────

    async def _load_or_generate_key(self) -> None:
        """Load shared encryption key from keyvault, or generate and store it."""
        if self.ha.encryption_key:
            self._encryption_key = self.ha.encryption_key.encode()[:32].ljust(32, b"0")
            return

        # Try vault
        if self.vault:
            try:
                key = await self.vault.get_secret("ha_encryption_key")
                if key:
                    self._encryption_key = key.encode()[:32].ljust(32, b"0")
                    return
            except Exception:
                pass

        # Generate new key
        key = secrets.token_hex(32)
        self._encryption_key = key.encode()[:32]
        logger.info("Generated new HA encryption key")

        if self.vault:
            try:
                await self.vault.store_secret("ha_encryption_key", key)
            except Exception as e:
                logger.warning("Could not store HA key in vault: %s", e)

    def _encrypt(self, data: bytes) -> bytes:
        """Encrypt state replication payload with AES-GCM."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key = self._encryption_key[:32].ljust(32, b"0")
            nonce = secrets.token_bytes(12)
            ct = AESGCM(key).encrypt(nonce, data, None)
            return nonce + ct
        except ImportError:
            # cryptography not available — pass through (not ideal, logged)
            logger.warning("cryptography library not available — state replication unencrypted")
            return data
        except Exception as e:
            logger.error("Encryption error: %s", e)
            return data

    def _decrypt(self, data: bytes) -> bytes:
        """Decrypt state replication payload."""
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            key = self._encryption_key[:32].ljust(32, b"0")
            nonce, ct = data[:12], data[12:]
            return AESGCM(key).decrypt(nonce, ct, None)
        except ImportError:
            return data
        except Exception as e:
            logger.error("Decryption error: %s", e)
            return data

    # ── Graceful failover ─────────────────────────────────────────────────────

    async def trigger_failover(self) -> dict:
        """
        POST /ha/failover — intentional handoff (e.g. before update).
        Demotes this unit to standby, peer takes over.
        """
        if self._role != "active":
            return {"ok": False, "error": "Not currently active"}

        if not self._peer or not self._peer.is_alive():
            return {"ok": False, "error": "No live peer to hand off to"}

        logger.info("Initiating graceful failover to %s", self._peer.ip)

        # Lower our VRRP priority so peer wins election
        try:
            # Reload keepalived with reduced priority
            self.ha.priority = max(50, self.ha.priority - 60)
            conf = generate_keepalived_conf(self.config)
            write_keepalived_conf(conf)
            start_keepalived()  # reload

            # Wait for peer to take VIP (max 5s)
            for _ in range(10):
                await asyncio.sleep(0.5)
                if self._role == "standby":
                    break

            return {"ok": True, "new_active": self._peer.ip}

        except Exception as e:
            return {"ok": False, "error": str(e)}
