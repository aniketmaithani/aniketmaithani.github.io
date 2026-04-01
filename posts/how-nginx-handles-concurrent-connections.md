---
title: "How NGINX Handles Hundreds of Thousands of Concurrent Connections"
date: 2026-01-12
author: Aniket Maithani
tags: [nginx, systems, linux, networking, backend, performance]
description: "A deep dive into NGINX's event-driven architecture: how the master-worker process model, epoll, non-blocking I/O, and upstream connection pooling combine to serve massive concurrency without the thread-per-connection overhead that Apache could never escape."
reading_time: 13
status: published
---

## The Problem Apache Could Not Solve

In the late 1990s and early 2000s, Apache was the dominant web server. Its architecture was straightforward: one process or one thread per incoming connection. Accept a connection, hand it to a worker, the worker blocks on I/O while it reads the request, generates the response, and writes back. When the request is done, the worker is freed.

This model is easy to reason about. It is also catastrophically unscalable.

A thread on Linux has a default stack size of 8MB. Ten thousand concurrent connections means 80GB of RAM consumed purely by thread stacks. The kernel has to schedule and context-switch between ten thousand threads. Context switching is not free: the CPU has to save and restore registers, flush TLB entries, and generally do a significant amount of work that has nothing to do with serving HTTP traffic. Benchmark this and you get the C10K problem: beyond roughly ten thousand concurrent connections, thread-per-connection servers fall apart.

NGINX was built specifically to solve this. Igor Sysoev started writing it in 2002, and the design decision that makes everything work is this: use a small, fixed number of worker processes, each handling thousands of connections simultaneously using non-blocking I/O and an event loop.

---

## The Process Model

NGINX starts one master process and one worker process per CPU core by default. On a 16-core machine, you get one master and sixteen workers.

The master process does not handle any client connections. Its job is lifecycle management: reading configuration, binding to privileged ports (80, 443), spawning and monitoring workers, and handling signals (SIGHUP for config reload, SIGQUIT for graceful shutdown). Because only the master touches the config and the sockets, a config reload is zero-downtime: the master spawns new workers with the new config, drains the old workers, and kills them once they go idle.

Each worker process runs a tight event loop. A single worker routinely handles ten to fifty thousand concurrent connections. On a properly tuned system, a single NGINX process with four workers can handle over a hundred thousand simultaneous connections on modest hardware.

The key question is: how can a single process handle fifty thousand connections without blocking?

---

## Non-Blocking I/O and epoll

The foundational primitive is the non-blocking file descriptor.

When you call `read()` on a blocking file descriptor and no data is available, the kernel puts your process to sleep until data arrives. Your worker is stuck doing nothing, waiting. This is exactly what kills Apache: fifty thousand connections means fifty thousand threads sleeping in `read()`.

Non-blocking I/O changes the contract. When you call `read()` on a non-blocking descriptor and no data is available, the call returns immediately with `EAGAIN`. Your process does not sleep. It is free to go do something else. When data becomes available, the kernel can notify you.

The notification mechanism on Linux is `epoll`. NGINX uses `epoll` for all I/O multiplexing on Linux systems.

The `epoll` API has three system calls:

```c
// Create an epoll instance
int epoll_fd = epoll_create1(0);

// Register a file descriptor for monitoring
struct epoll_event ev;
ev.events = EPOLLIN | EPOLLET;  // EPOLLET = edge-triggered
ev.data.fd = client_fd;
epoll_ctl(epoll_fd, EPOLL_CTL_ADD, client_fd, &ev);

// Wait for events (blocks until at least one fd is ready)
struct epoll_event events[MAX_EVENTS];
int n_ready = epoll_wait(epoll_fd, events, MAX_EVENTS, timeout_ms);
for (int i = 0; i < n_ready; i++) {
    handle_event(events[i].data.fd);
}
```

The key property of `epoll_wait` is that it returns only the file descriptors that are ready. If you have fifty thousand connections registered but only three have data to read right now, `epoll_wait` wakes up and gives you exactly those three. You do not iterate over all fifty thousand on every loop tick. This is O(ready events), not O(total connections). That is the entire reason NGINX scales.

NGINX uses edge-triggered mode (`EPOLLET`) rather than level-triggered mode. Level-triggered means `epoll_wait` keeps notifying you as long as the fd is readable. Edge-triggered means it notifies you once, when the fd transitions from unready to ready. Edge-triggered mode requires you to drain the fd completely on each notification (loop until `EAGAIN`), but it eliminates redundant notifications and reduces system call overhead under high load.

---

## The Event Loop in Detail

Each NGINX worker runs a loop that looks roughly like this:

```c
while (1) {
    // Wait for I/O events, timer events (timeouts), or posted events
    int n_events = epoll_wait(epoll_fd, events, MAX_EVENTS, timer_delta_ms());

    // Update internal time (NGINX caches gettimeofday to reduce syscalls)
    ngx_time_update();

    // Process expired timers (request timeouts, keepalive timeouts)
    ngx_event_expire_timers();

    // Process all ready I/O events
    for (int i = 0; i < n_events; i++) {
        ngx_event_t *ev = events[i].data.ptr;
        ev->handler(ev);  // dispatch to the appropriate handler
    }

    // Process posted events (deferred callbacks from this iteration)
    ngx_event_process_posted(&ngx_posted_events);
}
```

Several important details are in here.

NGINX caches the current time with `ngx_time_update()` rather than calling `gettimeofday()` on every timestamp needed. Under high connection rates, `gettimeofday()` would be called millions of times per second. Caching it to once per event loop iteration saves a measurable amount of time.

Timer management uses a red-black tree of timer events sorted by expiry time. `timer_delta_ms()` computes how long until the next timer fires and passes that as the `epoll_wait` timeout. When `epoll_wait` returns, any events in the tree that have expired are processed. This handles request timeouts, keepalive timeouts, and upstream connection timeouts without a separate timer thread.

Each event has a handler function pointer. When `epoll_wait` returns a readable client fd, NGINX dispatches to the read handler for that connection. The handler reads whatever data is available, advances the HTTP state machine, and returns. If the response cannot be written immediately (the send buffer is full), NGINX registers the fd for `EPOLLOUT` and moves on. No blocking. The next `epoll_wait` call will fire when the send buffer has space.

---

## The HTTP State Machine

NGINX processes HTTP requests as a state machine, not as a linear read-parse-respond sequence. This is necessary because any I/O operation can return `EAGAIN` at any point.

The states are approximately:

```
READING_REQUEST_LINE
    -> READING_REQUEST_HEADERS
        -> READING_REQUEST_BODY (if Content-Length > 0)
            -> CONTENT_HANDLER (location matching, proxy, static file, etc.)
                -> SENDING_RESPONSE_HEADERS
                    -> SENDING_RESPONSE_BODY
                        -> KEEPALIVE_WAIT | CLOSE
```

At each state transition, if the required I/O would block (`EAGAIN`), NGINX saves the current state and returns to the event loop. When `epoll_wait` fires on that fd again, NGINX resumes from the saved state. The connection object carries all necessary state between event loop iterations.

This is fundamentally different from synchronous code. In synchronous code you can write:

```python
data = socket.recv(4096)       # blocks here
request = parse_http(data)
response = generate_response(request)
socket.send(response)          # blocks here
```

In NGINX's model, each of those operations might require multiple event loop iterations to complete. The state machine is what makes it possible to have fifty thousand connections in various stages of this pipeline simultaneously, all running in a single worker process.

---

## Memory: Pools, Not malloc

NGINX does not use `malloc` and `free` for per-request allocations. It uses a pool allocator.

When a connection is accepted, NGINX creates a connection pool (typically 256 bytes). When the HTTP request is created, a larger request pool is created (typically 4KB or 8KB). All request-lifetime allocations come from this pool: headers, URL buffers, parsed request state, response headers. When the request completes, the entire pool is freed in a single operation.

```c
// Create a pool with 4096-byte initial block
ngx_pool_t *pool = ngx_create_pool(4096, log);

// Allocate from the pool (no individual free needed)
ngx_str_t *header = ngx_palloc(pool, sizeof(ngx_str_t));

// At request end, free everything at once
ngx_destroy_pool(pool);
```

This eliminates per-allocation metadata overhead, avoids heap fragmentation across the lifetime of thousands of connections, and is cache-friendly because allocations from the same request are often physically adjacent in memory. Under fifty thousand concurrent connections the difference between pool allocation and `malloc` is measurable in CPU profiling.

---

## Upstream: Connection Pooling and Keepalive

When NGINX proxies to an upstream (Django, Gunicorn, a Node.js service), it does not open a new TCP connection to the upstream on every client request. That would be expensive: TCP three-way handshake on every request, plus whatever TLS overhead applies.

NGINX maintains a pool of keepalive connections to each upstream, configured per upstream block:

```nginx
upstream django_backend {
    server 127.0.0.1:8000;
    keepalive 64;        # maintain up to 64 idle keepalive connections
    keepalive_timeout 60s;
    keepalive_requests 1000;
}

server {
    location / {
        proxy_pass http://django_backend;
        proxy_http_version 1.1;
        proxy_set_header Connection "";   # required for upstream keepalive
    }
}
```

`keepalive 64` means that after a proxied request completes, the upstream connection is returned to the pool rather than closed. The next proxied request that needs that upstream grabs a connection from the pool. If the pool has no idle connections and the upstream is not at capacity, a new connection is established.

The `proxy_set_header Connection ""` line is required. HTTP/1.0 has `Connection: close` semantics by default. Setting the header to empty string tells the upstream that this is a persistent connection, enabling keepalive.

For a Django application serving ten thousand requests per second, the difference between no upstream keepalive and `keepalive 64` is roughly ten thousand TCP handshakes per second eliminated. That is a non-trivial amount of CPU and latency.

---

## Sendfile and Zero-Copy

For static file serving, NGINX uses the `sendfile()` system call.

The normal path for serving a static file in userspace is:

```
1. open(path) → fd
2. read(fd, kernel_buffer, n) → data copied to kernel buffer
3. copy kernel_buffer → userspace buffer
4. write(socket_fd, userspace_buffer, n) → data copied from userspace to socket send buffer
```

That is two copies: kernel to userspace, then userspace to socket buffer. For large files, this is a significant waste of CPU and memory bandwidth.

`sendfile(socket_fd, file_fd, offset, count)` tells the kernel to transfer data from the file descriptor to the socket directly, without copying it to userspace. One copy instead of two. On modern Linux with scatter-gather DMA, it may be zero copies from the CPU's perspective: the DMA controller reads directly from the file cache pages and writes to the NIC buffers.

```nginx
sendfile on;
tcp_nopush on;        # batch headers + first chunk into one TCP segment
tcp_nodelay on;       # disable Nagle for small writes (keepalive responses)
```

`tcp_nopush` tells the kernel to buffer data until the send buffer is full or `TCP_NOPUSH` is explicitly flushed. Combined with `sendfile`, this batches the HTTP response headers and the beginning of the file into a single TCP segment, reducing the number of packets for small files.

---

## Worker to Worker: No Shared State

One of the design principles of NGINX is that workers do not communicate with each other. Each worker has its own event loop, its own connection pool, its own memory allocator. There are no mutexes between workers. There is no shared state to lock.

The only shared structures are the shared memory zones: the `ngx_http_limit_req_module` rate limiting state, the `ngx_http_limit_conn_module` connection counting state, and any `lua_shared_dict` in OpenResty setups. These are explicitly shared memory regions with their own locking. The vast majority of NGINX operation involves none of these.

The accept mutex is worth understanding. When multiple workers are all blocked in `epoll_wait` on the same listening socket, a new connection will wake up only one of them (Linux guarantees this for `epoll` with `EPOLLEXCLUSIVE`, which NGINX uses on recent kernels). On older kernels, NGINX uses its own accept mutex: only one worker holds the mutex and accepts connections at a time, rotating regularly, to distribute connections evenly without thundering herd.

```c
// Simplified accept mutex logic
if (ngx_use_accept_mutex) {
    if (ngx_accept_disabled > 0) {
        // This worker is overloaded, skip the mutex
        ngx_accept_disabled--;
    } else {
        if (ngx_trylock_accept_mutex(cycle) == NGX_ERROR) {
            return;
        }
    }
}
```

`ngx_accept_disabled` is set to `(total_connections / 8) - free_connections` when a connection is accepted. If a worker has used more than 7/8 of its connection slots, it stops competing for the accept mutex. Overloaded workers stop accepting new connections and give other workers a chance to take them.

---

## What the Numbers Actually Look Like

On a single server with four workers, properly tuned:

| Parameter                            | Typical Value                                  |
| ------------------------------------ | ---------------------------------------------- |
| Worker connections                   | 10,000 per worker                              |
| Total connections                    | 40,000                                         |
| CPU cores used                       | 4 (one per worker)                             |
| RAM per connection (active)          | ~1.5KB - 3KB                                   |
| RAM for 10,000 connections           | ~15-30MB per worker                            |
| syscalls for 10,000 idle connections | 1 `epoll_wait` (not 10,000 `select`/`poll`)    |
| Static file serving throughput       | 50,000+ req/s on spinning disk, higher on NVMe |

Compare this to a thread-per-connection model:

| Parameter                          | Thread-per-connection                                  |
| ---------------------------------- | ------------------------------------------------------ |
| RAM per thread (stack)             | 8MB default                                            |
| RAM for 10,000 connections         | 80GB, just stack                                       |
| Context switches at 10,000 threads | Severe scheduler overhead                              |
| Practical ceiling                  | ~1,000-2,000 concurrent connections before degradation |

The RAM comparison alone explains why NGINX displaced Apache for high-traffic workloads.

---

## A Practical Tuning Checklist

Understanding the architecture tells you what to tune:

```nginx
# Number of worker processes. One per core.
worker_processes auto;

# Max connections per worker. Total = worker_processes * worker_connections.
events {
    worker_connections 10240;
    use epoll;          # explicit, though NGINX auto-selects on Linux
    multi_accept on;    # accept as many connections as possible per epoll_wait
}

http {
    # Keepalive: reuse connections instead of reconnecting on each request
    keepalive_timeout 65;
    keepalive_requests 1000;

    # Sendfile for static assets
    sendfile on;
    tcp_nopush on;
    tcp_nodelay on;

    # Buffer tuning: enough to hold a typical response in memory
    # If the response fits in the buffer, no temp file is needed
    proxy_buffer_size 16k;
    proxy_buffers 4 16k;
    proxy_busy_buffers_size 32k;

    # Rate limiting (uses shared memory)
    limit_req_zone $binary_remote_addr zone=api:10m rate=100r/s;
    limit_conn_zone $binary_remote_addr zone=conn:10m;

    upstream backend {
        server 127.0.0.1:8000;
        keepalive 64;
    }
}
```

The OS also needs tuning for high connection counts:

```bash
# Maximum number of open file descriptors (connections are file descriptors)
# Must be higher than worker_connections * worker_processes
ulimit -n 100000

# Kernel TCP settings
sysctl -w net.core.somaxconn=65535          # listen() backlog
sysctl -w net.ipv4.tcp_max_syn_backlog=65535
sysctl -w net.core.netdev_max_backlog=5000  # packet queue before drop
sysctl -w net.ipv4.ip_local_port_range="1024 65535"  # upstream port range
```

The file descriptor limit is the most commonly forgotten one. Each TCP connection is a file descriptor. A worker handling ten thousand connections needs ten thousand file descriptors available plus a margin for disk I/O. The system default of 1024 will cause silent connection drops long before you hit any NGINX-level limit.

---

## Why This Architecture Still Matters

NGINX's event-driven model predates Node.js, predates Go's goroutines, and predates async/await in Python and JavaScript. The problem it solved in 2004 is the same problem those runtimes solve today: how do you handle massive I/O concurrency without massive thread overhead.

The answer in every case is the same: non-blocking I/O, event notification via the OS (epoll on Linux, kqueue on BSD, IOCP on Windows), and a scheduler that multiplexes work over a small, fixed pool of OS threads or processes.

NGINX did it without a garbage collector, without a runtime, and without any dependency beyond libc and the Linux kernel. The architecture is worth understanding not just to configure NGINX correctly but because the same design patterns appear in every high-performance I/O-bound system, regardless of language or framework. The event loop, the state machine per connection, the pool allocator, the zero-copy file transfer: these are general solutions to general problems. NGINX just made them famous.

<!-- DIAGRAM: Insert the SVG architecture diagram here. See diagram.svg -->

## Architecture Diagram

The following diagram shows the full request path through NGINX: from client TCP connections through the kernel epoll layer, the master process, per-core workers running their event loops, the upstream keepalive pool, and the sendfile path for static assets.

<div style="overflow-x:auto; margin: 28px 0;">
<svg width="100%" viewBox="0 0 680 820" xmlns="http://www.w3.org/2000/svg" style="font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', sans-serif; max-width: 680px;">
<defs>
  <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
    <path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
  </marker>
  <style>
    .box-teal   { fill: #e1f5ee; stroke: #0f6e56; }
    .box-purple { fill: #eeedfe; stroke: #534ab7; }
    .box-amber  { fill: #faeeda; stroke: #854f0b; }
    .box-blue   { fill: #e6f1fb; stroke: #185fa5; }
    .box-green  { fill: #eaf3de; stroke: #3b6d11; }
    .box-gray   { fill: #f1efe8; stroke: #5f5e5a; }
    .box-coral  { fill: #faece7; stroke: #993c1d; }
    .label-teal   { fill: #085041; }
    .label-purple { fill: #3c3489; }
    .label-amber  { fill: #633806; }
    .label-blue   { fill: #0c447c; }
    .label-green  { fill: #27500a; }
    .label-gray   { fill: #444441; }
    .label-coral  { fill: #712b13; }
    .sub { fill: #6e6e73; }
    .leader { stroke: #aeaeb2; stroke-dasharray: 3 3; stroke-width: 0.5; fill: none; }
    .arr { stroke: #aeaeb2; stroke-width: 0.9; fill: none; }
  </style>
</defs>

<!-- CLIENTS -->
<rect class="box-teal" x="40" y="30" width="96" height="38" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-teal" x="88" y="53" text-anchor="middle" dominant-baseline="central">Client 1</text>

<rect class="box-teal" x="154" y="30" width="96" height="38" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-teal" x="202" y="53" text-anchor="middle" dominant-baseline="central">Client 2</text>

<rect class="box-teal" x="268" y="30" width="96" height="38" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-teal" x="316" y="53" text-anchor="middle" dominant-baseline="central">Client N</text>

<text font-size="12" class="sub" x="390" y="53" dominant-baseline="central">...50k+ connections</text>

<line x1="88"  y1="68" x2="88"  y2="112" class="arr" marker-end="url(#arrow)"/>
<line x1="202" y1="68" x2="202" y2="112" class="arr" marker-end="url(#arrow)"/>
<line x1="316" y1="68" x2="316" y2="112" class="arr" marker-end="url(#arrow)"/>

<!-- KERNEL / EPOLL -->
<rect class="box-purple" x="30" y="112" width="440" height="72" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-purple" x="250" y="138" text-anchor="middle" dominant-baseline="central">Linux Kernel — epoll</text>
<text font-size="12" class="sub" x="250" y="162" text-anchor="middle" dominant-baseline="central">Non-blocking fd registration · edge-triggered events · O(ready) wakeup</text>

<line x1="250" y1="184" x2="250" y2="228" class="arr" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="262" y="208">epoll_wait()</text>

<!-- MASTER PROCESS -->
<rect class="box-amber" x="130" y="228" width="240" height="56" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-amber" x="250" y="250" text-anchor="middle" dominant-baseline="central">Master process</text>
<text font-size="12" class="sub" x="250" y="270" text-anchor="middle" dominant-baseline="central">Config · port binding · worker lifecycle</text>

<line x1="200" y1="284" x2="120" y2="338" class="arr" marker-end="url(#arrow)"/>
<line x1="250" y1="284" x2="250" y2="338" class="arr" marker-end="url(#arrow)"/>
<line x1="300" y1="284" x2="382" y2="338" class="arr" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="143" y="322">fork()</text>

<!-- WORKERS -->
<rect class="box-blue" x="30" y="338" width="170" height="44" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-blue" x="115" y="364" text-anchor="middle" dominant-baseline="central">Worker 1 (core 0)</text>

<rect class="box-blue" x="165" y="338" width="170" height="44" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-blue" x="250" y="364" text-anchor="middle" dominant-baseline="central">Worker 2 (core 1)</text>

<rect class="box-blue" x="300" y="338" width="170" height="44" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-blue" x="385" y="364" text-anchor="middle" dominant-baseline="central">Worker N (core N)</text>

<!-- EVENT LOOP detail -->
<rect x="30" y="402" width="460" height="158" rx="10" fill="none" stroke="#c7c7cc" stroke-width="0.5" stroke-dasharray="4 3"/>
<text font-size="12" class="sub" x="46" y="418">Event loop (per worker)</text>

<rect class="box-gray" x="46" y="428" width="130" height="44" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-gray" x="111" y="447" text-anchor="middle" dominant-baseline="central">epoll_wait()</text>
<text font-size="12" class="sub" x="111" y="463" text-anchor="middle" dominant-baseline="central">block until ready</text>

<line x1="176" y1="450" x2="206" y2="450" class="arr" marker-end="url(#arrow)"/>

<rect class="box-gray" x="206" y="428" width="130" height="44" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-gray" x="271" y="447" text-anchor="middle" dominant-baseline="central">dispatch event</text>
<text font-size="12" class="sub" x="271" y="463" text-anchor="middle" dominant-baseline="central">ev->handler(ev)</text>

<line x1="336" y1="450" x2="366" y2="450" class="arr" marker-end="url(#arrow)"/>

<rect class="box-gray" x="366" y="428" width="108" height="44" rx="6" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-gray" x="420" y="447" text-anchor="middle" dominant-baseline="central">state machine</text>
<text font-size="12" class="sub" x="420" y="463" text-anchor="middle" dominant-baseline="central">advance HTTP state</text>

<path d="M420 472 L420 510 L111 510 L111 474" fill="none" stroke="#aeaeb2" stroke-width="0.8" stroke-dasharray="3 3" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="260" y="524" text-anchor="middle">EAGAIN: register EPOLLIN/EPOLLOUT, return to loop</text>

<!-- POOL ALLOCATOR -->
<rect class="box-coral" x="500" y="338" width="164" height="76" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-coral" x="582" y="362" text-anchor="middle" dominant-baseline="central">Pool allocator</text>
<text font-size="12" class="sub" x="582" y="380" text-anchor="middle" dominant-baseline="central">per-connection pool</text>
<text font-size="12" class="sub" x="582" y="396" text-anchor="middle" dominant-baseline="central">destroy on close</text>
<line x1="470" y1="360" x2="498" y2="365" class="leader"/>

<!-- UPSTREAM POOL -->
<line x1="250" y1="562" x2="250" y2="606" class="arr" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="262" y="584">proxy_pass</text>

<rect class="box-green" x="130" y="606" width="240" height="56" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-green" x="250" y="628" text-anchor="middle" dominant-baseline="central">Upstream keepalive pool</text>
<text font-size="12" class="sub" x="250" y="648" text-anchor="middle" dominant-baseline="central">idle TCP connections reused</text>

<line x1="250" y1="662" x2="250" y2="706" class="arr" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="262" y="684">HTTP/1.1 keepalive</text>

<!-- BACKEND -->
<rect class="box-coral" x="100" y="706" width="300" height="44" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-coral" x="250" y="732" text-anchor="middle" dominant-baseline="central">Upstream (Gunicorn / Django / Node)</text>

<!-- SENDFILE branch -->
<line x1="420" y1="562" x2="540" y2="630" class="arr" stroke-dasharray="3 2" marker-end="url(#arrow)"/>
<text font-size="12" class="sub" x="498" y="594" text-anchor="middle">static file</text>

<rect class="box-gray" x="490" y="630" width="160" height="56" rx="8" stroke-width="0.5"/>
<text font-size="13" font-weight="500" class="label-gray" x="570" y="652" text-anchor="middle" dominant-baseline="central">sendfile()</text>
<text font-size="12" class="sub" x="570" y="670" text-anchor="middle" dominant-baseline="central">zero-copy to socket</text>

<!-- LEGEND -->
<rect x="30" y="772" width="620" height="36" rx="6" fill="none" stroke="#e8e8e4" stroke-width="0.5"/>
<rect class="box-teal"   x="46"  y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="64"  y="792" dominant-baseline="central">Clients</text>
<rect class="box-purple" x="120" y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="138" y="792" dominant-baseline="central">Kernel/epoll</text>
<rect class="box-amber"  x="218" y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="236" y="792" dominant-baseline="central">Master</text>
<rect class="box-blue"   x="294" y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="312" y="792" dominant-baseline="central">Workers + event loop</text>
<rect class="box-green"  x="442" y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="460" y="792" dominant-baseline="central">Upstream pool</text>
<rect class="box-coral"  x="542" y="784" width="12" height="12" rx="2" stroke-width="0.5"/>
<text font-size="12" class="sub" x="560" y="792" dominant-baseline="central">Backend</text>
</svg>
</div>
