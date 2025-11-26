#!/usr/bin/env python3
import json
import os
import sys
import threading
import time
from datetime import datetime
from typing import Dict, Set, Tuple

import requests
from requests import Response
from requests.exceptions import RequestException, ReadTimeout

# ---------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


# container_id -> set of network names we manage for this container
managed_networks: Dict[str, Set[str]] = {}


# ---------------------------------------------------------
# Docker HTTP client
# ---------------------------------------------------------


class DockerAPI:
    def __init__(self, base_url: str, timeout: int = 5) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.api_prefix = self._detect_api_prefix()

    def _detect_api_prefix(self) -> str:
        url = f"{self.base_url}/version"
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
            api_version = data.get("ApiVersion") or data.get("APIVersion") or "1.52"
            log(f"Detected Docker API version {api_version}")
            return f"/v{api_version}"
        except Exception as e:  # noqa: BLE001
            # Fallback to Docker 29 API version which is 1.52 today
            log(
                f"WARNING: failed to detect Docker API version ({e}); "
                "falling back to v1.52"
            )
            return "/v1.52"

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{self.api_prefix}{path}"

    def _get(
        self,
        path: str,
        params: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
    ) -> Response:
        url = self._url(path)
        return requests.get(
            url,
            params=params,
            timeout=timeout or self.timeout,
            stream=stream,
        )

    def _post(self, path: str, payload: dict, timeout: int | None = None) -> Response:
        url = self._url(path)
        return requests.post(
            url,
            json=payload,
            timeout=timeout or self.timeout,
        )

    # High-level helpers
    def list_containers(self, all_containers: bool = True) -> list[dict]:
        params = {"all": "1" if all_containers else "0"}
        resp = self._get("/containers/json", params=params)
        resp.raise_for_status()
        return resp.json()

    def inspect_container(self, container_id: str) -> dict:
        resp = self._get(f"/containers/{container_id}/json")
        resp.raise_for_status()
        return resp.json()

    def connect_network(
        self,
        network: str,
        container_id: str,
        alias: str | None = None,
    ) -> None:
        payload: dict = {"Container": container_id}
        if alias:
            payload["EndpointConfig"] = {"Aliases": [alias]}
        resp = self._post(f"/networks/{network}/connect", payload)
        resp.raise_for_status()

    def disconnect_network(
        self,
        network: str,
        container_id: str,
        force: bool = False,
    ) -> None:
        payload: dict = {"Container": container_id, "Force": force}
        resp = self._post(f"/networks/{network}/disconnect", payload)
        resp.raise_for_status()

    def inspect_network(self, network: str) -> dict:
        resp = self._get(f"/networks/{network}")
        resp.raise_for_status()
        return resp.json()

    def network_exists(self, network: str) -> bool:
        url = self._url(f"/networks/{network}")
        try:
            resp = requests.get(url, timeout=self.timeout)
        except RequestException:
            return False
        if resp.status_code == 404:
            return False
        try:
            resp.raise_for_status()
        except RequestException:
            return False
        return True

    def create_network(self, name: str, driver: str | None = None) -> dict:
        payload: dict = {
            "Name": name,
            "CheckDuplicate": True,
        }
        if driver:
            payload["Driver"] = driver
        resp = self._post("/networks/create", payload)
        resp.raise_for_status()
        return resp.json()

    def remove_network(self, network: str) -> None:
        url = self._url(f"/networks/{network}")
        resp = requests.delete(url, timeout=self.timeout)
        resp.raise_for_status()

    def events_stream(self, filters: dict | None = None):
        params = {}
        if filters:
            params["filters"] = json.dumps(filters)
        # Keep the stream open; use a larger read timeout so we can reconnect if idle.
        resp = self._get("/events", params=params, stream=True, timeout=60)
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield event


# ---------------------------------------------------------
# Config loading
# ---------------------------------------------------------


def load_config() -> dict:
    label_prefix = os.getenv("NETWORK_WATCHER_PREFIX", "network-watcher").strip()
    alias_label = os.getenv(
        "NETWORK_WATCHER_ALIAS_LABEL",
        f"{label_prefix}.alias",
    )

    initial_attach = parse_bool(os.getenv("INITIAL_ATTACH", "true"), True)
    initial_running_only = parse_bool(
        os.getenv("INITIAL_RUNNING_ONLY", "false"),
        False,
    )
    auto_disconnect = parse_bool(os.getenv("AUTO_DISCONNECT", "true"), True)
    rescan_seconds_str = os.getenv("RESCAN_SECONDS", "30")

    try:
        rescan_seconds = int(rescan_seconds_str)
        if rescan_seconds < 0:
            rescan_seconds = 0
    except ValueError:
        rescan_seconds = 30

    debug = parse_bool(os.getenv("DEBUG", "false"), False)

    default_network_driver = os.getenv(
        "NETWORK_WATCHER_NETWORK_DRIVER",
        "bridge",
    ).strip() or "bridge"

    prune_unused_networks = parse_bool(
        os.getenv("NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS", "false"),
        False,
    )

    log("Loaded network-watcher configuration:")
    log(f" Label prefix: {label_prefix}")
    log(f" Alias label: {alias_label}")
    log(f" Initial attach: {initial_attach} (running only: {initial_running_only})")
    log(f" Auto-disconnect: {auto_disconnect}")
    log(f" Periodic rescan seconds: {rescan_seconds} (0 = disabled)")
    log(f" Debug: {debug}")
    log(f" Default network driver: {default_network_driver}")
    log(f" Prune unused networks: {prune_unused_networks}")

    return {
        "label_prefix": label_prefix,
        "alias_label": alias_label,
        "initial_attach": initial_attach,
        "initial_running_only": initial_running_only,
        "auto_disconnect": auto_disconnect,
        "rescan_seconds": rescan_seconds,
        "debug": debug,
        "default_network_driver": default_network_driver,
        "prune_unused_networks": prune_unused_networks,
    }


# ---------------------------------------------------------
# Core logic
# ---------------------------------------------------------


def get_base_url_from_env() -> str:
    host = os.getenv("DOCKER_HOST", "http://socket-proxy:2375").strip()

    # Normalize some common values
    if host.startswith("unix://"):
        log(
            "ERROR: unix:// DOCKER_HOST is not supported by this watcher "
            "(use a TCP SocketProxy instead)."
        )
        sys.exit(1)

    if host.startswith("tcp://"):
        host = "http://" + host[len("tcp://") :]

    if not host.startswith("http://") and not host.startswith("https://"):
        host = "http://" + host

    return host.rstrip("/")


def get_container_name(attrs: dict) -> str:
    name = attrs.get("Name")
    if isinstance(name, str) and name:
        return name.lstrip("/")
    names = attrs.get("Names")
    if isinstance(names, list) and names:
        return str(names[0]).lstrip("/")
    cid = attrs.get("Id") or attrs.get("ID")
    if isinstance(cid, str) and cid:
        return cid[:12]
    return "<unknown>"


def get_network_mode(attrs: dict) -> str | None:
    host_cfg = attrs.get("HostConfig") or {}
    mode = host_cfg.get("NetworkMode")
    if isinstance(mode, str):
        return mode
    return None


def extract_desired_networks(
    labels: dict,
    label_prefix: str,
) -> Tuple[Set[str], Set[str]]:
    """Return (desired_networks, referenced_networks) for a container.

    desired_networks -> networks where label value is truthy
    referenced_networks -> networks that have a label with this prefix
    regardless of the value, so we can later detach when label is removed.
    """
    desired: Set[str] = set()
    referenced: Set[str] = set()

    prefix = f"{label_prefix}."
    for key, value in labels.items():
        if not isinstance(key, str):
            continue
        if not key.startswith(prefix):
            continue
        net_name = key[len(prefix) :]
        if not net_name:
            continue
        referenced.add(net_name)
        if parse_bool(str(value), False):
            desired.add(net_name)

    return desired, referenced


def maybe_prune_network(
    api: DockerAPI,
    network: str,
    cfg: dict,
    reason: str = "prune",
) -> None:
    if not cfg.get("prune_unused_networks"):
        return

    debug: bool = cfg["debug"]

    try:
        info = api.inspect_network(network)
    except RequestException as e:
        if debug:
            log(f"[{reason}] Failed to inspect network '{network}' for pruning: {e}")
        return

    containers = info.get("Containers") or {}
    if containers:
        if debug:
            log(
                f"[{reason}] Network '{network}' not pruned: "
                f"{len(containers)} container(s) still attached.",
            )
        return

    try:
        api.remove_network(network)
        log(f"[{reason]()
