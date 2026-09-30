"""Can an email's domain receive mail? A minimal MX lookup.

An address published on a website is only a lead if mail to it arrives. A
domain with no mail server bounces every message, and a bounce costs sender
reputation that the next, real lead pays for.

Kerb's core stays at two dependencies, and the standard library resolves
addresses but not MX records -- so this asks the system resolver directly, over
UDP, for the one record type it needs. Anything unexpected is "unknown", never
"no": a lookup that failed is not evidence about the business.
"""

from __future__ import annotations

import random
import socket
import struct
import threading
from typing import Dict, List, Optional

MX = 15
_CACHE: Dict[str, Optional[bool]] = {}
_LOCK = threading.Lock()
# Lookups that got no answer at all, in a row. A network that blocks DNS would
# otherwise cost every email its timeout; after a few silences Kerb stops
# asking for the rest of the process and reports "unknown".
_SILENT = {"run": 0}
GIVE_UP_AFTER = 3


def resolvers(path: str = "/etc/resolv.conf") -> List[str]:
    """The nameservers this machine is configured to use."""
    try:
        with open(path) as fh:
            return [line.split()[1] for line in fh
                    if line.startswith("nameserver") and len(line.split()) > 1]
    except OSError:
        return []


def build_query(domain: str, qtype: int = MX, qid: Optional[int] = None) -> bytes:
    """A standard recursive query for one record type."""
    qid = random.randint(0, 0xFFFF) if qid is None else qid
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    labels = domain.strip(".").encode("idna").split(b".")
    question = b"".join(bytes([len(p)]) + p for p in labels) + b"\x00"
    return header + question + struct.pack(">HH", qtype, 1)


def _skip_name(buf: bytes, i: int) -> int:
    while True:
        n = buf[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:                     # a compression pointer
            return i + 2
        i += 1 + n


def count_answers(resp: bytes, qid: int, qtype: int = MX) -> Optional[int]:
    """How many records of `qtype` the answer holds. 0 for a domain that does
    not exist; None for anything that cannot be trusted."""
    try:
        if len(resp) < 12:
            return None
        rid, flags, qd, an, _ns, _ar = struct.unpack(">HHHHHH", resp[:12])
        if rid != qid or not flags & 0x8000:
            return None
        rcode = flags & 0x000F
        if rcode == 3:                            # NXDOMAIN
            return 0
        if rcode != 0:
            return None
        i = 12
        for _ in range(qd):
            i = _skip_name(resp, i) + 4
        found = 0
        for _ in range(an):
            i = _skip_name(resp, i)
            rtype, _cls, _ttl, rdlen = struct.unpack(">HHIH", resp[i:i + 10])
            i += 10 + rdlen
            if rtype == qtype:
                found += 1
        return found
    except (IndexError, struct.error):
        return None


def mx_count(domain: str, timeout: float = 2.0) -> Optional[int]:
    """MX records for `domain`, asked of each configured resolver in turn."""
    if _SILENT["run"] >= GIVE_UP_AFTER:
        return None
    for server in resolvers()[:2]:
        qid = random.randint(0, 0xFFFF)
        try:
            with socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET,
                               socket.SOCK_DGRAM) as sock:
                sock.settimeout(timeout)
                sock.sendto(build_query(domain, MX, qid), (server, 53))
                data, _ = sock.recvfrom(4096)
        except (OSError, UnicodeError):
            continue
        got = count_answers(data, qid)
        if got is not None:
            _SILENT["run"] = 0
            return got
    _SILENT["run"] += 1
    return None


def has_mail(domain: str) -> Optional[bool]:
    """True if mail to the domain has somewhere to go, False if it cannot,
    None if that could not be established. Cached for the process.

    A domain with no MX record still receives mail at its address record (RFC
    5321's implicit MX), so only a domain with neither counts as False.
    """
    domain = (domain or "").strip().lower().strip(".")
    if not domain or "." not in domain:
        return None
    with _LOCK:
        if domain in _CACHE:
            return _CACHE[domain]
    mx = mx_count(domain)
    if mx is None:
        verdict: Optional[bool] = None
    elif mx > 0:
        verdict = True
    else:
        try:
            verdict = bool(socket.getaddrinfo(domain, 25))
        except (socket.gaierror, UnicodeError):
            verdict = False
        except OSError:
            verdict = None
    with _LOCK:
        _CACHE[domain] = verdict
    return verdict
