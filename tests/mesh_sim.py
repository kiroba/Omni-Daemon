"""Userspace network faults for multi-daemon tests: no root, no tc/netem, runs on Linux, macOS and Windows.

A FaultProxy sits between two daemons' P2P ports. Point a daemon at the proxy's port instead of its
peer's, then change the link while the test runs:

    p = FaultProxy(target_port=25005, seed=1).start()   # daemon A dials p.port to reach B
    p.latency = 0.08         # seconds added in each direction, per forwarded chunk
    p.drop_rate = 0.2        # share of new connections reset at once (a lossy radio link)
    p.partitioned = True     # every connection reset at once until set back to False
    p.stop()

listen_host="::1" gives an IPv6-only hop in front of an IPv4 daemon, the shape of a Wi-Fi Aware
data path. Link pairs two proxies (one per direction) so a partition cuts both ways.
"""
import random
import select
import os
import socket
import struct
import threading
import time


class FaultProxy:
    def __init__(self, target_port, target_host="127.0.0.1", listen_host="127.0.0.1", seed=None):
        self.target = (target_host, target_port)
        self.listen_host = listen_host
        self.latency = 0.0
        self.drop_rate = 0.0
        self.partitioned = False
        self.stats = {"accepted": 0, "dropped": 0, "refused_partition": 0, "bytes": 0}
        self._rng = random.Random(seed)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        family = socket.AF_INET6 if ":" in listen_host else socket.AF_INET
        self._srv = socket.socket(family, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind((listen_host, 0))
        self._srv.listen(64)
        self._srv.settimeout(0.2)
        self.port = self._srv.getsockname()[1]

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass

    def _count(self, key, n=1):
        with self._lock:
            self.stats[key] += n

    def _accept_loop(self):
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if self.partitioned:
                self._count("refused_partition")
                _reset(client)
                continue
            with self._lock:
                drop = self._rng.random() < self.drop_rate
            if drop:
                self._count("dropped")
                _reset(client)
                continue
            self._count("accepted")
            threading.Thread(target=self._pipe, args=(client,), daemon=True).start()

    def _pipe(self, client):
        try:
            upstream = socket.create_connection(self.target, timeout=5)
        except OSError:
            _reset(client)
            return
        socks = [client, upstream]
        try:
            while not self._stop.is_set():
                if self.partitioned:          # a partition also cuts connections already open
                    break
                readable, _, _ = select.select(socks, [], [], 0.2)
                for s in readable:
                    data = s.recv(65536)
                    if not data:
                        return
                    if self.latency:
                        time.sleep(self.latency)
                    (upstream if s is client else client).sendall(data)
                    self._count("bytes", len(data))
        except OSError:
            pass
        finally:
            for s in socks:
                _reset(s)


class Link:
    """Two daemons joined through a FaultProxy in each direction."""

    def __init__(self, a_port, b_port, seed=None, listen_host="127.0.0.1"):
        self.to_b = FaultProxy(b_port, listen_host=listen_host, seed=seed).start()
        self.to_a = FaultProxy(a_port, listen_host=listen_host, seed=None if seed is None else seed + 1).start()

    def set(self, **kw):
        for p in (self.to_a, self.to_b):
            for k, v in kw.items():
                setattr(p, k, v)

    def stop(self):
        self.to_a.stop()
        self.to_b.stop()


def _reset(s):
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def ipv6_loopback_ok() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False
