# docker-network-watcher

A small sidecar container that automatically **attaches / detaches Docker networks to containers** based on **labels**, using the **Docker HTTP API** (via a Socket Proxy).  
There is **no direct access to `/var/run/docker.sock`** from inside the watcher.

Typical use cases:

- Automatically plug your apps into shared networks (e.g. `traefikfront`, `socketproxy`, `psu`, `smtp`…)
- Create networks on demand when a label is present
- Flip networks between **internal** and **non-internal** based on labels
- Optionally **prune unused networks**
- Optionally enforce a **“labels-only”** network model for some or all containers

---

## How it works (high level)

1. The watcher connects to the Docker API via `DOCKER_HOST` (HTTP / TCP, usually a [Socket Proxy](https://github.com/traefik/traefik-library-image/tree/master/traefik#using-docker-socket-proxy)).
2. It subscribes to Docker **events** (`create`, `start`, `die`, `destroy`, `update`, …) and optionally performs **periodic rescans**.
3. For each container that has labels matching a configurable prefix (default: `network-watcher`), it:
   - Reads labels like `network-watcher.<network>` to know which networks to **attach** or **detach**
   - Ensures those networks **exist**, creating them if needed
   - Applies internal mode based on `network-watcher.<network>.internal`
   - Optionally **detaches other networks** (depending on global / per-container settings)
   - Optionally **prunes unused networks** when they are no longer used by any container

Containers with **no `network-watcher.*` labels** are left alone.

The watcher **never breaks its own connectivity**: it detects its own container ID and does not apply the “detach other” logic to itself.

---

## Labels

By default, the label prefix is `network-watcher`. You can change it via `NETWORK_WATCHER_PREFIX`.

### 1. Attach / detach a network

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.psu: "true"
  network-watcher.smtp: "false"
````

For a given prefix `<prefix>`:

* `<prefix>.<network>: "true"`
  → ensure the container is attached to the Docker network `<network>`.
* `<prefix>.<network>: "false"` or label removed
  → ensure the container is **detached** from `<network>` (if previously managed).

The watcher will also **create the network** if it does not exist yet, using the configured driver and internal mode (see below).

### 2. Internal networks

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.psu.internal: "true"
```

For each network name `<net>`:

* `<prefix>.<net>.internal: "true"` → this network should be **internal**
* No container with `<prefix>.<net>.internal: "true"` → this network should be **non-internal**

The watcher:

* Inspects the Docker network `<net>`
* If it does not exist, it **creates** it with:

  * `Driver`: `NETWORK_WATCHER_NETWORK_DRIVER` (default: `bridge`)
  * `Internal`: `true` or `false` depending on labels
* If it already exists but the `Internal` flag does not match, it:

  * Detaches all containers from that network
  * Deletes the network
  * Recreates it with the correct `Internal` setting + same driver
  * Reattaches:

    * All containers that were previously attached
    * All containers having `<prefix>.<net>: "true"` labels

> This works for **any Docker network**, not only those originally created by the watcher.

### 3. Alias label

```yaml
labels:
  network-watcher.psu: "true"
  network-watcher.alias: "my-custom-alias"
```

By default, if you don’t configure anything, the watcher uses the container name as the network alias.
You can override this with:

* Env: `NETWORK_WATCHER_ALIAS_LABEL` (default: `<prefix>.alias`)
* Label: `<prefix>.alias: "<alias>"`

### 4. Per-container detach behaviour (`detachdefault`)

```yaml
labels:
  network-watcher.traefikfront: "true"
  network-watcher.socketproxy: "true"
  network-watcher.detachdefault: "false"
```

Role of `detachdefault` depends on the **global** setting `NETWORK_WATCHER_DETACH_OTHER`:

#### Global OFF (default: `NETWORK_WATCHER_DETACH_OTHER=false`)

* No `network-watcher.detachdefault` label → do **not** touch other networks.
* `network-watcher.detachdefault: "true"` → this container is in **labels-only** mode:

  * Attach networks declared via labels
  * Detach all **other** networks (even if they are not “default” ones)

#### Global ON (`NETWORK_WATCHER_DETACH_OTHER=true`)

* Default behaviour: all containers with labels are considered **labels-only**:

  * Attach networks declared via labels
  * Detach all other networks
* `network-watcher.detachdefault: "false"` → **opt-out** for this container:

  * It keeps its other networks untouched
* `network-watcher.detachdefault: "true"` → explicitly confirmed labels-only (same as default when global is ON)

> The watcher container itself is never affected by `detachdefault` or `NETWORK_WATCHER_DETACH_OTHER` to avoid cutting off its own Docker API connectivity.

---

## Environment variables (watcher container)

### Connection

* `DOCKER_HOST`
  **Required.** Must point to a Docker HTTP/TCP endpoint, typically a Socket Proxy.

  Examples:

  * `DOCKER_HOST=http://socket-proxy:2375`
  * `DOCKER_HOST=tcp://socket-proxy:2375`

  `unix://` is **not** supported.

### Core behaviour

* `NETWORK_WATCHER_PREFIX`
  Default: `network-watcher`
  Prefix for all labels.

* `NETWORK_WATCHER_ALIAS_LABEL`
  Default: `<prefix>.alias`
  Label key used to fetch a custom network alias for containers.

* `INITIAL_ATTACH`
  Default: `"true"`
  If `true`, the watcher runs an initial pass on existing containers when it starts.

* `INITIAL_RUNNING_ONLY`
  Default: `"false"`
  If `true`, the initial pass only considers running containers; otherwise, all containers.

* `AUTO_DISCONNECT`
  Default: `"true"`
  If `true`, the watcher disconnects networks that are no longer desired (e.g. when label is set to `false` or removed).

* `RESCAN_SECONDS`
  Default: `"30"`
  Interval for periodic rescans.

  * `0` or negative → periodic rescan disabled.

* `DEBUG`
  Default: `"false"`
  If `true`, prints verbose logs about decisions (desired networks, detach decisions, etc.).

### Network creation / pruning

* `NETWORK_WATCHER_NETWORK_DRIVER`
  Default: `"bridge"`
  Driver to use when creating new Docker networks (`docker network create --driver <driver>`).

* `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS`
  Default: `"false"`
  If `true`, the watcher removes networks that it manages and that have **no containers attached** anymore.
  It only attempts to prune networks that it has managed via labels.

### Global “labels-only” mode

* `NETWORK_WATCHER_DETACH_OTHER`
  Default: `"false"`

  Controls whether containers with labels should be in **labels-only** mode:

  * `false`:

    * No `detachdefault` → **do not** touch other networks.
    * `network-watcher.detachdefault: "true"` → labels-only **for this container**.
  * `true`:

    * By default, containers with labels are labels-only (detach other networks).
    * `network-watcher.detachdefault: "false"` → opt-out: keep other networks.
    * `network-watcher.detachdefault: "true"` → explicit opt-in (same as default).

> The watcher container (the one running this script) is **never** subject to `detachdefault` or `NETWORK_WATCHER_DETACH_OTHER`.

### Build metadata (optional)

* `WATCHER_BUILD_HASH`
  Optional. If set (typically from your CI with `git rev-parse --short HEAD`), it is printed on startup:

  ```text
  Starting docker-network-watcher (build=abcd123)
  ```

---

## Behaviour summary

* The watcher only touches containers that:

  * have at least one label starting with `<prefix>.`, **or**
  * have previously been managed (stored in its internal `managed_networks` set).
* Containers with no `network-watcher.*` labels are **ignored**.
* For each labelled container:

  * Networks with `<prefix>.<net>: "true"` are **ensured and attached**.
  * Networks with `<prefix>.<net>: "false"` or where the label was removed are **detached** (if `AUTO_DISCONNECT=true`).
  * Networks with `<prefix>.<net>.internal: "true"` are forced to be Docker `internal` networks; others are forced to be non-internal.
  * Optionally, other networks are detached according to:

    * Global `NETWORK_WATCHER_DETACH_OTHER`
    * Per-container `network-watcher.detachdefault`
* If `NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS=true`, networks that the watcher manages and that have no containers attached are **removed**.

---

## Docker Compose example

A minimal example with a Socket Proxy and the watcher:

```yaml
version: "3.9"

networks:
  socketproxy:
    name: socketproxy
  traefikfront:
    name: traefikfront

services:
  socket-proxy:
    image: tecnativa/docker-socket-proxy
    container_name: socket-proxy
    environment:
      CONTAINERS: 1
      NETWORKS: 1
      EVENTS: 1
      # ...adjust permissions as you like...
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
    networks:
      - socketproxy
    restart: unless-stopped

  network-watcher:
    image: ghcr.io/your-user/docker-network-watcher:latest
    container_name: network-watcher
    environment:
      DOCKER_HOST: http://socket-proxy:2375
      NETWORK_WATCHER_PREFIX: network-watcher
      NETWORK_WATCHER_PRUNE_UNUSED_NETWORKS: "true"
      NETWORK_WATCHER_DETACH_OTHER: "true"   # global labels-only, overridable per container
      RESCAN_SECONDS: "30"
    networks:
      - socketproxy
    restart: unless-stopped
```

An example app container:

```yaml
  my-app:
    image: your/image
    container_name: my-app
    labels:
      network-watcher.traefikfront: "true"
      network-watcher.psu: "true"
      network-watcher.psu.internal: "true"
      # This app is in labels-only mode even if the global DETACH_OTHER is false:
      network-watcher.detachdefault: "true"
    networks:
      - traefikfront   # initial compose networks, watcher will reconcile according to labels
    restart: unless-stopped
```

---

## Sample compose file

You can find a complete example in this repository:
**[`docker-compose.sample.yml`](./compose.yml)**

Use it as a starting point and adapt:

* network names (e.g. `traefikfront`, `psu`, `socketproxy`, …),
* which containers should be in labels-only mode,
* whether you want global `NETWORK_WATCHER_DETACH_OTHER` or only per-container `detachdefault`.

---

## Notes & limitations

* Requires Docker API v1.52 (Docker 29) or compatible. The watcher auto-detects the API version from `/version` and falls back to `v1.52` on error.
* Only works over HTTP(S)/TCP (`DOCKER_HOST=http://...` or `tcp://...`), not over Unix sockets.
* Designed to be **read-only** on Docker objects except:

  * attaching / detaching containers to networks,
  * creating networks,
  * deleting/recreating networks when toggling internal mode,
  * pruning unused networks (if enabled).

If you run into edge cases or want to support more advanced patterns (e.g. driver-specific options on network creation), feel free to open an issue or extend the labels model.
